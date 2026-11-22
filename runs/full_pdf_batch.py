"""Run full PDFs through the same HTTP/queue workflow as the web, saving checkpoints."""
import argparse
import csv
import hashlib
from datetime import datetime
import io
import json
from pathlib import Path
import re
import time
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen
import zipfile


def save(path, data):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)


def request(origin, path, method='GET', body=None, headers=None, timeout=660):
    with urlopen(Request(origin + path, data=body, method=method, headers=headers or {}), timeout=timeout) as response:
        return response.read()


def json_request(origin, path, **kwargs):
    return json.loads(request(origin, path, **kwargs))


def run_attempts(run):
    tables = run['stages']
    stages = [*tables, *run.get('figures', []),
              *(stage['gridRead'] for stage in tables if stage.get('gridRead'))]
    if run.get('articleText'):
        stages.append(run['articleText'])
    return [attempt for stage in stages for attempt in stage.get('attempts', [])]


def table_diagnostic(stage, artifact):
    """Count observed cells, never infer unobserved ground-truth table coverage."""
    cells = (artifact or {}).get('cells', [])
    numeric = [cell for cell in cells if re.search(r'(?<![A-Za-z])(?:\d+\.\d+|\.\d+|\d+)', cell.get('text', ''))]
    results = (artifact or {}).get('results', [])
    remarks = (artifact or {}).get('remarks', [])
    if stage['status'] == 'failed':
        reading = stage.get('gridRead') or {}
        attempts = reading.get('attempts') or []
        reason = ('grid_not_read' if reading.get('status') == 'failed' and attempts
                  and attempts[-1].get('hasGridArtifact') else 'process_failed')
    elif artifact is None:
        reason = 'artifact_unavailable'
    elif not artifact.get('grid'):
        reason = 'grid_not_read'
    elif not numeric:
        reason = 'no_numeric_cells_observed'
    elif not results:
        reason = 'numbers_read_no_confirmed_results'
    else:
        reason = 'partial_or_confirmed'  # Not a claim that every numeric cell is a metric.
    return {'page': stage['page'], 'table': stage['table'], 'status': stage['status'],
            'strategy': stage['strategy'], 'cells': len(cells), 'numeric_cells_observed': len(numeric),
            'results': len(results), 'remarks': len(remarks), 'reason': reason,
            'empty_result_reason': stage.get('emptyResultReason'), 'error': stage.get('error'),
            'remark_reasons': sorted({item['reason'] for item in remarks}),
            'attempts': stage.get('attempts', []), 'reading': stage.get('gridRead')}


def collect(origin, folder, run):
    diagnostics = []
    for stage in run['stages']:
        artifact = None
        if stage['status'] == 'done':
            try:
                artifact = json_request(origin, f"/api/article-runs/{run['id']}/stages/{stage['id']}")['run']
                save(folder / (stage['id'] + '.json'), artifact)
            except (HTTPError, KeyError, ValueError) as error:
                save(folder / (stage['id'] + '-error.json'), {'error': str(error)})
        reading = stage.get('gridRead')
        attempts = (reading or {}).get('attempts') or []
        saved_failure = reading and reading['status'] == 'failed' and attempts and attempts[-1].get('hasGridArtifact')
        if reading and (reading['status'] == 'done' or saved_failure):
            try:
                route = f"/api/article-runs/{run['id']}/stages/{reading['id']}"
                if saved_failure:
                    route += f"/attempts/{attempts[-1]['number']}"
                grid_artifact = json_request(origin, route + '/table-grid.json')
                save(folder / (reading['id'] + '-grid.json'), grid_artifact)
                grid = grid_artifact['read']['grid']
                artifact = {**(artifact or {}), 'grid': grid, 'cells': grid['cells'] if grid else []}
            except (HTTPError, KeyError, ValueError) as error:
                save(folder / (reading['id'] + '-error.json'), {'error': str(error)})
        diagnostics.append(table_diagnostic(stage, artifact))
    publication = run.get('publication')
    bundle = None
    if publication:
        # Archive bytes are downloaded from this exact version, not the current/latest URL.
        bundle = request(origin, publication['downloadUrl'])
        (folder / 'export.zip').write_bytes(bundle)
        with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
            manifest = json.loads(archive.read('manifest.json'))
            if manifest['versionId'] != publication['versionId']:
                raise ValueError('Downloaded export is from another publication version.')
            rows = list(csv.DictReader(io.StringIO(archive.read('results.csv').decode('utf-8-sig'))))
            report = json.loads(archive.read('report.json'))
            save(folder / 'export-manifest.json', manifest)
    attempts = run_attempts(run)
    execution_seconds = sum((attempt['finishedAt'] - attempt['startedAt']) / 1000
                            for attempt in attempts if attempt.get('finishedAt') is not None)
    completed_at = (datetime.fromisoformat(manifest['publishedAt'].replace('Z', '+00:00')).timestamp()
                    if bundle is not None else max((attempt['finishedAt'] / 1000 for attempt in attempts
                                                    if attempt.get('finishedAt') is not None), default=None))
    warnings = (run.get('coverage') or {}).get('discoveryWarnings', [])
    return {'run_id': run['id'], 'publication': publication,
            'publication_status': run.get('publicationStatus'), 'publication_error': run.get('publicationError'),
            'coverage': run.get('coverage'), 'tables': diagnostics, 'figures': run.get('figures', []),
            'confirmed_table_results': sum(item['results'] for item in diagnostics),
            'observed_numeric_cells': sum(item['numeric_cells_observed'] for item in diagnostics),
            'attempt_execution_seconds': round(execution_seconds, 2),
            'completion_at': completed_at,
            'attempt_count': len(attempts),
            'exported_results': len(rows) if bundle is not None else 0,
            'checker': report if bundle is not None else None,
            'discovery_gaps': [{'page': item['page'], 'reason': 'discovery_requires_review', 'detail': item['reason']} for item in warnings],
            'undetected_tables': None,  # Requires independent page review; zero candidates is not zero actual tables.
            'notes': ['Numeric cells include headers and conditions; their count is not recall.',
                      'Publication and table counts may differ due to prose and figures.']}


def run_batch(origin, files, output, timeout, interval, refresh=False):
    output.mkdir(parents=True, exist_ok=True)
    checkpoint = output / 'batch.json'
    state = json.loads(checkpoint.read_text()) if checkpoint.exists() else {'origin': origin, 'items': []}
    if state['origin'] != origin:
        raise ValueError('Resume requires the same API origin.')
    known = {item['pdf']: item for item in state['items']}
    for pdf in files:
        address = str(pdf.resolve())
        fingerprint = hashlib.sha256(pdf.read_bytes()).hexdigest()
        if address in known:
            item = known[address]
            if item.get('sha256', fingerprint) != fingerprint:
                raise ValueError('Input PDF changed since this batch was started; use a new output directory.')
            if item.get('run_id') or item.get('launch_error'):
                continue
        else:
            item = {'pdf': address, 'sha256': fingerprint, 'started_at': time.time()}
            state['items'].append(item)
            known[address] = item
        save(checkpoint, state)
        try:
            if not item.get('article_id'):
                item['upload_started_at'] = time.time()
                article = json_request(origin, '/api/articles', method='POST', body=pdf.read_bytes(),
                                       headers={'Content-Type': 'application/pdf', 'X-PDF-Name': quote(pdf.name)})
                item['article_id'] = article['id']
                folder = output / article['id']
                folder.mkdir(exist_ok=True)
                save(folder / 'article.json', article)
                item['discovery_seconds'] = round(time.time() - item['upload_started_at'], 2)
                save(checkpoint, state)
            accepted = json_request(origin, f"/api/articles/{item['article_id']}/runs", method='POST', body=b'')
            item['run_id'] = accepted['id']
            item['url'] = origin + '/?articleRun=' + accepted['id']
        except (HTTPError, OSError, ValueError) as error:
            item['launch_error'] = str(error)
        save(checkpoint, state)
    if refresh:
        for item in state['items']:
            if not item.get('finished_at'):
                continue
            folder = output / item['article_id']
            summary = collect(origin, folder, json.loads((folder / 'run.json').read_text()))
            save(folder / 'summary.json', summary)
            item['summary'] = summary
        save(checkpoint, state)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pending = False
        for item in state['items']:
            if 'run_id' not in item or item.get('finished_at'):
                continue
            pending = True
            try:
                run = json_request(origin, '/api/article-runs/' + item['run_id'])
                folder = output / item['article_id']
                save(folder / 'run.json', run)
                if run.get('publicationStatus') in ('done', 'empty', 'failed'):
                    summary = collect(origin, folder, run)
                    item['finished_at'] = time.time()
                    item['elapsed_seconds'] = round(item['finished_at'] - item['started_at'], 2)
                    if summary['completion_at'] is not None:
                        item['completed_elapsed_seconds'] = round(summary['completion_at'] - item.get('upload_started_at', item['started_at']), 2)
                    save(folder / 'summary.json', summary)
                    item['summary'] = summary
                    print(f"{Path(item['pdf']).name}: {summary['publication_status']}, export={summary['exported_results']}, {item['elapsed_seconds']}s", flush=True)
                item.pop('poll_error', None)
            except (HTTPError, OSError, ValueError, KeyError, zipfile.BadZipFile) as error:
                item['poll_error'] = str(error)
            save(checkpoint, state)
        if not pending:
            return 1 if any(item.get('launch_error') or item.get('summary', {}).get('publication_status') == 'failed'
                            for item in state['items']) else 0
        time.sleep(interval)
    print('Time budget reached; checkpoint retained. Resume with the same command.', flush=True)
    return 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('pdf', nargs='+', type=Path)
    parser.add_argument('--origin', required=True)
    parser.add_argument('--out', required=True, type=Path)
    parser.add_argument('--timeout', type=float, default=1800)
    parser.add_argument('--interval', type=float, default=3)
    parser.add_argument('--refresh', action='store_true', help='Recompute completed summaries from saved run IDs, without extraction.')
    args = parser.parse_args()
    if args.timeout <= 0 or args.interval < 0:
        parser.error('Timeout must be positive and interval nonnegative.')
    raise SystemExit(run_batch(args.origin.rstrip('/'), args.pdf, args.out, args.timeout, args.interval, args.refresh))


if __name__ == '__main__':
    main()
