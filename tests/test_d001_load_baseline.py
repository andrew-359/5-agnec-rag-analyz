import hashlib
import io
import json
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from urllib.parse import parse_qs, urlsplit
from zipfile import ZipFile


ROOT = Path(__file__).resolve().parents[1]
PDF = b'fixture for the HTTP boundary'
HASH = hashlib.sha256(PDF).hexdigest()
CONTROL = {'id': 'R235', 'method': 'ThreadRAG', 'rawValue': '.422', 'value': '0.422',
           'dataset': 'HotpotQA', 'metric': 'EM', 'generator': 'LLaMA3-8B-Instruct',
           'evidenceIds': ['cell']}
CELL = {'id': 'cell', 'page': 6, 'table': 1, 'row': 12, 'column': 7,
        'precision': 'cell', 'box': [0.3, 0.4, 0.32, 0.42],
        'text': '.422', 'headers': ['LLaMA3-8B-Instruct', 'HotpotQA', 'EM']}


class D001LoadBaselineTest(unittest.TestCase):
    def fixture(self, *, wrong_cell=None, pending_first=False, stalled_get=False, overlap=True):
        first_get_waiting = threading.Event()
        release_first_get = threading.Event()

        class Api(BaseHTTPRequestHandler):
            uploads = 0
            runs = []
            polls = []
            sources = []
            manifests = {}
            second_polled_during_stall = False

            def do_POST(self):
                self.rfile.read(int(self.headers.get('Content-Length', 0)))
                if self.path == '/api/articles':
                    type(self).uploads += 1
                    self.reply({'id': 'article'}, 201)
                elif self.path == '/api/articles/article/runs':
                    run_id = f'run-{len(self.runs) + 1}'
                    self.runs.append(run_id)
                    if not overlap and run_id == 'run-2':
                        self.record_manifest(run_id, datetime.now(timezone.utc) - timedelta(seconds=5))
                    self.reply({'id': run_id, 'status': 'queued'}, 202)
                else:
                    self.send_error(404)

            def do_GET(self):
                url = urlsplit(self.path)
                if url.path.startswith('/api/article-runs/'):
                    run_id = url.path.rsplit('/', 1)[-1]
                    self.polls.append((run_id, len(self.runs)))
                    if stalled_get and run_id == 'run-2':
                        first_get_waiting.set()
                        release_first_get.wait(timeout=10)
                        return
                    if stalled_get and run_id == 'run-3':
                        type(self).second_polled_during_stall = first_get_waiting.wait(timeout=5)
                    pending = pending_first and run_id == 'run-2'
                    if not pending and run_id not in self.manifests:
                        self.record_manifest(run_id)
                    self.reply({'id': run_id, 'articleId': 'article', 'pdfSha256': HASH,
                                'publicationStatus': 'running' if pending else 'done',
                                'publication': None if pending else self.version(run_id),
                                'stages': [], 'figures': [],
                                'articleText': {'id': 'text', 'status': 'running',
                                                'attempts': []} if pending else None})
                    return
                if url.path.startswith('/trpc/'):
                    inputs = json.loads(parse_qs(url.query)['input'][0])
                    version_id = inputs['versionId']
                    run_id = version_id.removeprefix('version-')
                    cell = {**CELL, **wrong_cell} if wrong_cell and run_id == 'run-2' else CELL
                    pdf = {'url': f'/api/publications/D001/versions/{version_id}/source.pdf',
                           'sha256': HASH}
                    if url.path == '/trpc/publications.review':
                        data = {'version': self.version(run_id), 'run': {'id': version_id},
                                'pdf': pdf, 'results': [CONTROL], 'evidence': [cell]}
                    else:
                        self.sources.append((version_id, inputs['resultId'], inputs['evidenceId']))
                        data = {'evidence': cell, 'result': CONTROL, 'pdf': pdf,
                                'run': {'id': version_id}}
                    self.reply({'result': {'data': data}})
                    return
                version_id, name = url.path.split('/')[-2:]
                run_id = version_id.removeprefix('version-')
                if name == 'source.pdf':
                    self.reply(PDF)
                elif name == 'manifest.json':
                    self.reply(self.manifests[run_id])
                elif name == 'export.zip':
                    archive = io.BytesIO()
                    with ZipFile(archive, 'w') as bundle:
                        bundle.writestr('manifest.json', json.dumps(self.manifests[run_id]))
                        bundle.writestr('report.json', '{"exit_code": 0}')
                        bundle.writestr('results.csv', 'result_id\nR235\n')
                    self.reply(archive.getvalue())
                else:
                    self.send_error(404)

            def version(self, run_id):
                version_id = f'version-{run_id}'
                url = f'/publications/D001/versions/{version_id}'
                return {'publicationId': 'D001', 'versionId': version_id, 'runId': run_id,
                        'url': url, 'downloadUrl': f'/api{url}/export.zip', 'pdfSha256': HASH}

            def record_manifest(self, run_id, published_at=None):
                self.manifests[run_id] = {**self.version(run_id), 'publishedAt':
                                          (published_at or datetime.now(timezone.utc)).isoformat()}

            def reply(self, value, status=200):
                self.send_response(status)
                self.end_headers()
                self.wfile.write(value if isinstance(value, bytes) else json.dumps(value).encode())

            def log_message(self, *args):
                pass

        with tempfile.TemporaryDirectory() as temporary, ThreadingHTTPServer(('127.0.0.1', 0), Api) as server:
            folder = Path(temporary)
            pdf = folder / 'D001.pdf'
            pdf.write_bytes(PDF)
            server_thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.05})
            server_thread.start()
            output = folder / 'evidence'
            timeout = '2' if pending_first or stalled_get else '5'
            try:
                run = subprocess.run([sys.executable, str(ROOT / 'runs/d001_load_baseline.py'), str(pdf),
                                      '--origin', f'http://127.0.0.1:{server.server_port}',
                                      '--out', str(output), '--timeout', timeout, '--interval', '0.1'],
                                     capture_output=True, text=True, timeout=10)
            finally:
                release_first_get.set()
                server.shutdown()
                server_thread.join()
            self.assertTrue((output / 'baseline.json').is_file(), run.stderr)
            report = json.loads((output / 'baseline.json').read_text())
            cards = [json.loads(path.read_text()) for path in output.glob('*/run.json')]
            return run, report, Api, cards

    def test_one_upload_and_three_pinned_sources_with_pair_accepted_together(self):
        run, report, api, _ = self.fixture()
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(api.uploads, 1)
        self.assertCountEqual(api.polls, [('run-1', 1), ('run-2', 3), ('run-3', 3)])
        self.assertEqual(api.sources, [(f'version-run-{n}', 'R235', 'cell') for n in (1, 2, 3)])
        self.assertEqual([item['outcome'] for item in report['runs']], ['published'] * 3)
        self.assertTrue(report['pair']['overlap_confirmed'])

    def test_poll_order_cannot_prove_overlap(self):
        run, report, api, _ = self.fixture(overlap=False)
        self.assertEqual(run.returncode, 1)
        self.assertCountEqual(api.polls, [('run-1', 1), ('run-2', 3), ('run-3', 3)])
        self.assertFalse(report['pair']['overlap_confirmed'])

    def test_wrong_cell_fails_only_its_own_verification(self):
        run, report, _, _ = self.fixture(wrong_cell={'page': 7})
        self.assertEqual(run.returncode, 1)
        self.assertEqual([item['outcome'] for item in report['runs']],
                         ['published', 'verification_error', 'published'])

    def test_same_table_and_page_still_require_the_correct_cell_text_and_headers(self):
        for wrong_cell in ({'text': '.423'}, {'headers': ['LLaMA3-8B-Instruct', '2WikiMultihopQA', 'EM']}):
            with self.subTest(wrong_cell=wrong_cell):
                run, report, _, _ = self.fixture(wrong_cell=wrong_cell)
                self.assertEqual(run.returncode, 1)
                self.assertEqual([item['outcome'] for item in report['runs']],
                                 ['published', 'verification_error', 'published'])

    def test_timeout_retains_card_and_other_run_can_publish(self):
        run, report, _, cards = self.fixture(pending_first=True)
        self.assertEqual(run.returncode, 1)
        self.assertEqual([item['outcome'] for item in report['runs']], ['published', 'timeout', 'published'])
        self.assertEqual(report['runs'][1]['last_status'], 'running')
        self.assertEqual(next(card for card in cards if card['id'] == 'run-2')['articleText']['status'],
                         'running')

    def test_stalled_status_request_does_not_hold_up_the_other_run(self):
        run, report, api, cards = self.fixture(stalled_get=True)
        self.assertEqual(run.returncode, 1, run.stderr)
        self.assertTrue(api.second_polled_during_stall)
        _, first, second = report['runs']
        self.assertEqual(first['outcome'], 'timeout')
        self.assertEqual(second['outcome'], 'published')
        self.assertLess(second['duration_seconds'], report['timeout_seconds'] / 2)
        self.assertEqual(first['accepted_status'], 'queued')
        self.assertIsNone(first['last_status'])
        self.assertCountEqual([card['id'] for card in cards], ['run-1', 'run-3'])

    def test_cli_rejects_nonfinite_timeout_and_interval(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            pdf = folder / 'D001.pdf'
            pdf.write_bytes(PDF)
            for option in ('--timeout', '--interval'):
                for value in ('nan', 'inf'):
                    with self.subTest(option=option, value=value):
                        run = subprocess.run(
                            [sys.executable, str(ROOT / 'runs/d001_load_baseline.py'), str(pdf),
                             '--origin', 'http://127.0.0.1:1', '--out', str(folder / 'evidence'), option, value],
                            capture_output=True, text=True, timeout=5,
                        )
                        self.assertEqual(run.returncode, 2, run.stderr)
                        self.assertIn('finite and positive', run.stderr)
                        self.assertFalse((folder / 'evidence').exists())
