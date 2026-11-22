"""Measure one D001 control run and two successive starts on an isolated local API."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import hashlib
from http.client import HTTPException
import json
from math import isfinite
from pathlib import Path
from threading import Lock
import time
from urllib.parse import quote, urlencode
from zipfile import BadZipFile

from full_pdf_batch import collect, json_request, request, run_attempts, save


TERMINAL = ('done', 'empty', 'failed')
HTTP_ERRORS = (OSError, HTTPException, ValueError, KeyError, TypeError)
POLL_REQUEST_TIMEOUT = 5
CONTROL = {'method': 'ThreadRAG', 'rawValue': '.422', 'value': '0.422',
           'dataset': 'HotpotQA', 'metric': 'EM', 'generator': 'LLaMA3-8B-Instruct'}
CONTROL_HEADERS = [CONTROL[key] for key in ('generator', 'dataset', 'metric')]


def trpc(origin, route, fields):
    query = urlencode({'input': json.dumps(fields)})
    path = f'/trpc/{route}?{query}'
    return path, json_request(origin, path)['result']['data']


def check(condition, message):
    if not condition:
        raise ValueError(message)


def check_version(origin, folder, run, fingerprint):
    version = run['publication']
    run_id = run['id']
    version_id = version['versionId']
    publication_id = version['publicationId']
    version_path = f'/publications/{publication_id}/versions/{version_id}'
    check(version['runId'] == run_id and version['pdfSha256'] == run['pdfSha256'] == fingerprint
          and version['url'] == version_path and version['downloadUrl'] == f'/api{version_path}/export.zip',
          'Run card points to a different run, PDF or version.')
    manifest = json_request(origin, f'/api{version_path}/manifest.json')
    save(folder / 'manifest.json', manifest)
    check(manifest['runId'] == run_id and manifest['versionId'] == version_id
          and manifest['pdfSha256'] == fingerprint, 'Published manifest belongs to another run, version or PDF.')

    pinned = {'publicationId': publication_id, 'versionId': version_id}
    _, review = trpc(origin, 'publications.review', pinned)
    check(review['version']['runId'] == run_id and review['run']['id'] == version_id,
          'Published results belong to another run or version.')
    matches = [(row, source) for row in review['results']
               if all(row.get(key) == value for key, value in CONTROL.items())
               for source in review['evidence']
               if source['id'] in row['evidenceIds'] and source['page'] == 6
               and source['table'] == 1 and source['precision'] == 'cell']
    check(len(matches) == 1, 'Expected one ThreadRAG .422 result linked to a cell in Table 1 on PDF page 6.')
    row, source = matches[0]
    check(source.get('text') == CONTROL['rawValue'] and source.get('headers') == CONTROL_HEADERS,
          'Source cell text or headers differ from the expected ThreadRAG result.')
    box = source.get('box')
    check(box is not None and len(box) == 4 and 0 <= box[0] < box[2] <= 1
          and 0 <= box[1] < box[3] <= 1 and source.get('row') is not None
          and source.get('column') is not None, 'Missing or invalid source cell coordinates.')

    path, evidence = trpc(origin, 'evidence.get', {**pinned, 'resultId': row['id'], 'evidenceId': source['id']})
    save(folder / 'control-evidence.json', {'route': path, 'response': evidence})
    check(evidence['run']['id'] == version_id and evidence['evidence'] == source and evidence['result'] == row,
          'Version-specific evidence route returned different evidence or result.')
    pdf_path = f'/api{version_path}/source.pdf'
    check(evidence['pdf']['url'] == pdf_path and evidence['pdf']['sha256'] == fingerprint
          and hashlib.sha256(request(origin, pdf_path)).hexdigest() == fingerprint,
          'Version-specific source endpoint did not return the uploaded PDF.')
    return {'result_id': row['id'], 'evidence_id': source['id'], 'route': path,
            'page': 6, 'table': 1, 'box': box,
            'url': f"{version_path}/evidence/{source['id']}?resultId={row['id']}"}


def start(origin, article_id, label, timeout):
    sent = time.time()
    start_tick = time.monotonic()
    item = {'label': label, 'run_id': None, 'outcome': 'launch_error', 'sent_at': sent,
            'duration_seconds': None, 'first_attempt_wait_seconds': None}
    try:
        response = json_request(origin, f'/api/articles/{article_id}/runs', method='POST',
                                body=b'', timeout=timeout)
        item.update(run_id=response['id'], accepted_at=time.time(), accepted_status=response.get('status'),
                    accepted=response, outcome=None, last_status=None)
    except HTTP_ERRORS as error:
        item['error'] = str(error)
    return item, start_tick


def wait_for_runs(origin, output, checkpoint, report, launches, timeout, interval):
    checkpoint_lock = Lock()

    def poll(item, tick):
        folder = output / item['label']
        folder.mkdir(exist_ok=True)
        while True:
            updates = {}
            remaining = tick + timeout - time.monotonic()
            if remaining > 0:
                try:
                    run = json_request(origin, f"/api/article-runs/{item['run_id']}",
                                       timeout=min(remaining, POLL_REQUEST_TIMEOUT))
                    observed = time.time()
                    elapsed = time.monotonic() - tick
                    check(run['id'] == item['run_id'] and run['articleId'] == report['article_id'],
                          'Run card belongs to another run or article.')
                    save(folder / 'run.json', run)
                    status = run.get('publicationStatus')
                    updates.update(last_status=status, last_observed_at=observed)
                    if elapsed <= timeout and status in TERMINAL:
                        updates.update(outcome={'done': 'published', 'empty': 'empty', 'failed': 'failed'}[status],
                                       terminal_observed_seconds=round(elapsed, 3),
                                       publication=run.get('publication'),
                                       publication_error=run.get('publicationError') or run.get('publicationNote'))
                        if status == 'done':
                            updates.update(duration_seconds=round(elapsed, 3), publication_observed_at=observed)
                except HTTP_ERRORS as error:
                    updates['poll_error'] = str(error)
            with checkpoint_lock:
                if 'last_status' in updates:
                    item.pop('poll_error', None)
                item.update(updates)
                remaining = tick + timeout - time.monotonic()
                if not item['outcome'] and remaining <= 0:
                    item['outcome'] = 'timeout'
                save(checkpoint, report)
            if item['outcome']:
                print(f"{item['label']}: {item['run_id']} {item['outcome']}", flush=True)
                return
            time.sleep(min(interval, remaining))

    pending = [(item, tick) for item, tick in launches if item['run_id']]
    if pending:
        with ThreadPoolExecutor(max_workers=len(pending)) as executor:
            polls = [executor.submit(poll, item, tick) for item, tick in pending]
            for poll_result in polls:
                poll_result.result()


def diagnose(origin, output, item, fingerprint):
    folder = output / item['label']
    card = folder / 'run.json'
    if not card.exists():
        return
    run = json.loads(card.read_text())
    first_attempt = min((attempt['startedAt'] / 1000 for attempt in run_attempts(run)), default=None)
    item['first_attempt_at'] = first_attempt
    if first_attempt is not None and item['sent_at'] <= first_attempt <= item['last_observed_at']:
        item['first_attempt_wait_seconds'] = round(first_attempt - item['sent_at'], 3)
    if item['outcome'] == 'published':
        try:
            item['control'] = check_version(origin, folder, run, fingerprint)
        except HTTP_ERRORS as error:
            item.update(outcome='verification_error', error=str(error))
    manifest = folder / 'manifest.json'
    if manifest.exists():
        stamp = json.loads(manifest.read_text()).get('publishedAt')
        if stamp:
            item['manifest_published_at'] = stamp
            try:
                item['manifest_published_epoch'] = datetime.fromisoformat(stamp.replace('Z', '+00:00')).timestamp()
            except ValueError as error:
                item['manifest_error'] = str(error)
    try:
        save(folder / 'summary.json', collect(origin, folder, run))
    except (*HTTP_ERRORS, BadZipFile) as error:
        item['diagnostic_error'] = str(error)


def measure(origin, pdf, output, timeout, interval):
    output.mkdir(parents=True)
    checkpoint = output / 'baseline.json'
    source = pdf.read_bytes()
    fingerprint = hashlib.sha256(source).hexdigest()
    report = {'origin': origin, 'pdf': str(pdf.resolve()), 'pdf_sha256': fingerprint,
              'timeout_seconds': timeout, 'interval_seconds': interval, 'runs': []}
    try:
        upload_at = time.monotonic()
        article = json_request(origin, '/api/articles', method='POST', body=source,
                               headers={'Content-Type': 'application/pdf', 'X-PDF-Name': quote(pdf.name)})
        report.update(article_id=article['id'], preparation_seconds=round(time.monotonic() - upload_at, 3))
        save(output / 'article.json', article)
        print(f"D001 prepared once: {article['id']} ({report['preparation_seconds']}s)", flush=True)
    except HTTP_ERRORS as error:
        report['preparation_error'] = str(error)
        save(checkpoint, report)
        return 1

    control = start(origin, article['id'], 'control', timeout)
    report['runs'].append(control[0])
    save(checkpoint, report)
    wait_for_runs(origin, output, checkpoint, report, [control], timeout, interval)
    if control[0]['outcome'] == 'published':
        pair = [start(origin, article['id'], label, timeout) for label in ('pair-first', 'pair-second')]
        report['runs'].extend(item for item, _ in pair)
        save(checkpoint, report)
        wait_for_runs(origin, output, checkpoint, report, pair, timeout, interval)
    else:
        report['runs'].extend({'label': label, 'run_id': None, 'outcome': 'not_started',
                               'duration_seconds': None, 'first_attempt_wait_seconds': None}
                              for label in ('pair-first', 'pair-second'))

    for item in report['runs']:
        if item['run_id']:
            diagnose(origin, output, item, fingerprint)
            save(checkpoint, report)
    _, first, second = report['runs']
    accepted = second.get('accepted_at')
    manifest_time = first.get('manifest_published_epoch')
    overlap = (accepted is not None and manifest_time is not None
               and accepted < manifest_time <= first['publication_observed_at'])
    report['pair'] = {'overlap_confirmed': overlap, 'second_accepted_at': accepted,
                      'first_manifest_published_at': first.get('manifest_published_at'),
                      'method': 'Second POST response predates the first version manifest written by Hono before commit.'}
    runs = report['runs']
    run_ids = {item['run_id'] for item in runs}
    versions = {item['publication']['versionId'] for item in runs if item.get('publication')}
    report['acceptance_passed'] = (len(run_ids) == len(versions) == 3
                                   and all(item['outcome'] == 'published' for item in runs) and overlap)
    save(checkpoint, report)
    return 0 if report['acceptance_passed'] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('pdf', type=Path)
    parser.add_argument('--origin', required=True, help='Isolated Hono API on the same host and clock as the runner.')
    parser.add_argument('--out', required=True, type=Path, help='Fresh directory for JSON and per-run evidence.')
    parser.add_argument('--timeout', type=float, default=1800, help='Seconds from each individual start.')
    parser.add_argument('--interval', type=float, default=1, help='Seconds between status polls.')
    args = parser.parse_args()
    if not all(isfinite(value) and value > 0 for value in (args.timeout, args.interval)):
        parser.error('Timeout and interval must be finite and positive.')
    if args.out.exists():
        parser.error('Use a fresh output directory for each measurement.')
    raise SystemExit(measure(args.origin.rstrip('/'), args.pdf, args.out, args.timeout, args.interval))


if __name__ == '__main__':
    main()
