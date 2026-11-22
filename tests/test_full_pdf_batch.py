import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import zipfile
from runs.full_pdf_batch import collect, table_diagnostic, run_batch


class BatchDiagnosticsTest(unittest.TestCase):
    def test_resume_refuses_a_changed_pdf_instead_of_reusing_its_old_run(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            pdf = root / 'input.pdf'
            pdf.write_bytes(b'changed PDF')
            (root / 'batch.json').write_text(json.dumps({'origin': 'http://local', 'items': [
                {'pdf': str(pdf.resolve()), 'sha256': 'old-hash', 'run_id': 'old-run'}]}))
            with self.assertRaisesRegex(ValueError, 'Input PDF changed'):
                run_batch('http://local', [pdf], root, 1, 0)

    def stage(self, **updates):
        return {'id': 'stage', 'page': 5, 'table': 1, 'status': 'done', 'strategy': 'reader', **updates}

    def test_reading_failure_is_not_an_ambiguous_binding(self):
        result = table_diagnostic(self.stage(), {'grid': None, 'cells': [], 'results': [], 'remarks': []})
        self.assertEqual(result['reason'], 'grid_not_read')
        result = table_diagnostic(self.stage(), {'grid': {'rows': 2}, 'cells': [{'text': '.204'}],
            'results': [], 'remarks': [{'reason': 'Генератор не установлен'}]})
        self.assertEqual(result['numeric_cells_observed'], 1)
        self.assertEqual(result['reason'], 'numbers_read_no_confirmed_results')
        self.assertEqual(result['remark_reasons'], ['Генератор не установлен'])
        result = table_diagnostic(self.stage(status='failed', error='worker unavailable'), None)
        self.assertEqual(result['reason'], 'process_failed')
        result = table_diagnostic(self.stage(status='failed', gridRead={'status': 'failed',
            'attempts': [{'hasGridArtifact': True}]}), None)
        self.assertEqual(result['reason'], 'grid_not_read')
        result = table_diagnostic(self.stage(), None)
        self.assertEqual(result['reason'], 'artifact_unavailable')

    def test_empty_scanned_map_is_unknown_coverage_not_a_claim_of_no_tables(self):
        run = {'id': 'run', 'stages': [], 'publicationStatus': 'empty', 'coverage': {
            'discoveryWarnings': [{'page': 6, 'reason': 'Мало текста'}]}}
        with TemporaryDirectory() as folder:
            report = collect('', Path(folder), run)
        self.assertIsNone(report['undetected_tables'])
        self.assertEqual(report['discovery_gaps'][0]['page'], 6)
        self.assertEqual(report['discovery_gaps'][0]['reason'], 'discovery_requires_review')
        self.assertEqual(report['exported_results'], 0)

    def test_download_from_another_version_is_refused(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as archive:
            archive.writestr('manifest.json', json.dumps({'versionId': 'other'}))
        run = {'id': 'run', 'stages': [], 'publication': {'versionId': 'expected', 'downloadUrl': '/exact-version/export.zip'}}
        with TemporaryDirectory() as folder, patch('runs.full_pdf_batch.request', return_value=buffer.getvalue()) as fetch:
            with self.assertRaisesRegex(ValueError, 'another publication version'):
                collect('http://local', Path(folder), run)
            fetch.assert_called_once_with('http://local', '/exact-version/export.zip')

    def test_short_web_response_does_not_hide_successfully_read_cells(self):
        run = {'id': 'run', 'stages': [self.stage(gridRead={'id': 'grid-stage', 'status': 'done'})]}
        responses = [
            {'run': {'results': [], 'remarks': [{'reason': 'Unknown generator'}]}},
            {'read': {'grid': {'cells': [{'text': '.204', 'role': 'body'}]}}},
        ]
        with TemporaryDirectory() as folder, patch('runs.full_pdf_batch.json_request', side_effect=responses):
            report = collect('', Path(folder), run)
        self.assertEqual(report['observed_numeric_cells'], 1)
        self.assertEqual(report['tables'][0]['reason'], 'numbers_read_no_confirmed_results')

    def test_runner_submits_all_inputs_and_refresh_never_starts_new_extractions(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import threading
        requests = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                self.rfile.read(int(self.headers.get('Content-Length', 0)))
                requests.append(self.path)
                count = sum(path == '/api/articles' for path in requests)
                value = {'id': f'article-{count}'} if self.path == '/api/articles' else {'id': f'run-{count}'}
                self.send_response(200);self.end_headers();self.wfile.write(json.dumps(value).encode())
            def do_GET(self):
                self.send_response(200);self.end_headers();self.wfile.write(json.dumps({
                    'id': self.path.rsplit('/', 1)[-1], 'publicationStatus': 'empty', 'stages': []}).encode())
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True);thread.start()
        try:
            with TemporaryDirectory() as folder:
                root = Path(folder);inputs = [root / 'a.pdf', root / 'b.pdf']
                for pdf in inputs: pdf.write_bytes(b'PDF input')
                output = root / 'output';origin = f'http://127.0.0.1:{server.server_port}'
                self.assertEqual(run_batch(origin, inputs, output, 2, 0), 0)
                self.assertEqual(requests, ['/api/articles', '/api/articles/article-1/runs', '/api/articles', '/api/articles/article-2/runs'])
                checkpoint = json.loads((output / 'batch.json').read_text())
                self.assertEqual(len(checkpoint['items']), 2)
                self.assertTrue(all(item.get('finished_at') for item in checkpoint['items']))
                requests.clear()
                self.assertEqual(run_batch(origin, inputs, output, 2, 0, refresh=True), 0)
                self.assertEqual(requests, [])
        finally:
            server.shutdown();server.server_close();thread.join()
