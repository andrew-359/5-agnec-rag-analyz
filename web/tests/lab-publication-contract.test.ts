import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { test, type TestContext } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createApp } from '../src/server/app.ts'
import { createPublicationStore } from '../src/server/publication/publication-store.ts'
import { datasetSchema } from '../src/shared/schema.ts'

const resources = new URL('../../qa/java/src/test/resources/', import.meta.url)
const root = fileURLToPath(new URL('../../', import.meta.url))
const python = process.env.RAG_QA_PYTHON || 'python3'
const title = 'Работа, "кавычки"\nи перенос'

async function fixture(t: TestContext) {
  const directory = await mkdtemp(join(tmpdir(), 'rag-lab-contract-'))
  t.after(() => rm(directory, { recursive: true, force: true }))
  const pdfPath = fileURLToPath(new URL('synthetic.pdf', resources))
  const pdf = await readFile(pdfPath)
  const pdfSha256 = createHash('sha256').update(pdf).digest('hex')
  const saved = datasetSchema.parse(JSON.parse(await readFile(new URL('publication.json', resources), 'utf8')))
  assert.equal(saved.pdf.sha256, pdfSha256)
  const checkerPath = join(directory, 'test_checker.py')
  // The official checker is distributed with the participant kit. Exercise its packaging boundary.
  await writeFile(checkerPath, [
    'import json, sys',
    'from pathlib import Path',
    'Path(sys.argv[-1]).write_text(json.dumps({"exit_code": 2, "warnings": ["QA fixture"]}))',
    'sys.exit(2)',
  ].join('\n'))
  const store = createPublicationStore({ directory: join(directory, 'versions'), root, checkerPath, python })
  const app = createApp(async () => null, { pdfPath, publicationStore: store, reviewDraftStore: join(directory, 'drafts') })

  async function publish(runId: string, { publicationId = 'Paper', page = 1, value = '0' } = {}) {
    const template = saved.results[0]
    return store.publish({ publicationId, runId, pdfPath, pdfSha256, document: publicationId + '.pdf',
      publication: { title }, coverage: { partial: true, summary: 'Контрольная таблица' },
      artifacts: [{ id: 'table', runId, data: {
        source: { sha256: pdfSha256 }, page, table: 1,
        results: [
          { ...template, id: 'zero', generator: '', raw_value: value, value, retrieval_top_k: 5, metric_k: 1,
            metric_direction: 'higher_is_better', source: { page, table: 1, row: 1, column: 1 } },
          { ...template, id: 'second', method: 'Beta', generator: '', raw_value: '.5', value: '0.5',
            source: { page, table: 1, row: 2, column: 1 } },
          { ...template, id: 'different', metric: 'F1', raw_value: '.7', value: '0.7',
            source: { page, table: 1, row: 3, column: 2 } },
        ],
        remarks: [{ id: 'unclear', reason: 'Единица неизвестна', raw_text: '—',
          source: { page, table: 1, row: 4 }, result_ids: ['zero'] }],
      } }],
    })
  }

  async function rpc(name: string, input: object) {
    const response = await app.request('/trpc/' + name + '?' + new URLSearchParams({ input: JSON.stringify(input) }))
    return { status: response.status, body: await response.json() }
  }

  return { app, directory, pdf, pdfSha256, publish, rpc, store }
}

test('API-03/10/11/12/14: pinned tRPC data preserves filters, addresses and evidence across two versions', async t => {
  const f = await fixture(t)
  const first = await f.publish('first')
  const second = await f.publish('second', { page: 2, value: '0.1' })
  const address = { publicationId: first.publicationId, versionId: first.versionId }
  const filters = { publicationId: first.publicationId, runId: first.versionId, dataset: 'HotpotQA', metric: 'EM' }
  const listed = await f.rpc('results.list', filters)
  assert.equal(listed.status, 200)
  assert.equal(listed.body.result.data.count, 2)
  const [row] = listed.body.result.data.results
  assert.equal(row.value, '0')
  const empty = await f.rpc('results.list', { ...filters, dataset: 'Missing' })
  assert.deepEqual(empty.body.result.data, { results: [], count: 0, options: listed.body.result.data.options })
  assert.equal((await f.rpc('results.list', { ...filters, metric: '' })).status, 400)
  const evidence = { ...address, evidenceId: row.evidenceIds[0] }
  const source = await f.rpc('evidence.get', { ...evidence, resultId: row.id })
  assert.equal(source.status, 200)
  assert.equal(source.body.result.data.evidence.page, 1)
  const [remark] = (await f.store.read(first.publicationId, first.versionId))!.remarks
  assert.equal((await f.rpc('evidence.get', { ...evidence, remarkId: remark.id })).status, 400)
  assert.equal((await f.rpc('evidence.get', { ...evidence, evidenceId: 'missing' })).status, 404)
  assert.equal((await f.rpc('publications.review', { ...address, versionId: 'version-' + '0'.repeat(40) })).status, 404)
  assert.equal((await f.rpc('publications.review', { publicationId: first.publicationId })).body.result.data.version.versionId, second.versionId)
  const updated = await f.rpc('evidence.get', { ...evidence, resultId: row.id, versionId: second.versionId })
  assert.equal(updated.body.result.data.evidence.page, 2)
  assert.equal(updated.body.result.data.result.value, '0.1')
  assert.deepEqual(await f.rpc('results.list', filters), listed)
})

test('API-02/03/05/06/08/09: real Hono compares explicitly scoped records and rejects invalid inputs', async t => {
  const f = await fixture(t)
  const version = await f.publish('main')
  const other = await f.publish('also-main', { publicationId: 'Other' })
  const address = { publicationId: version.publicationId, versionId: version.versionId }
  const [first, second, different] = (await f.store.read(version.publicationId, version.versionId))!.results
  const a = { ...address, resultId: first.id }, b = { ...address, resultId: second.id }
  const post = (path: string, body: string) => f.app.request('/api/comparisons' + path,
    { method: 'POST', headers: { 'Content-Type': 'application/json' }, body })
  const compare = (left: object, right: object) => post('', JSON.stringify({ a: left, b: right }))
  for (const [left, right] of [[a, b], [b, a]]) {
    const response = await compare(left, right)
    assert.equal(response.status, 200)
    const verdict = await response.json()
    assert.equal(verdict.status, 'insufficient')
    assert.equal(verdict.delta, undefined)
    assert.deepEqual(verdict.a.ref, left)
  }
  const mismatch = await (await compare(a, { ...b, resultId: different.id })).json()
  assert.equal(mismatch.status, 'different')
  assert.equal(mismatch.delta, undefined)
  assert.equal((await compare(a, { ...b, versionId: 'version-' + '0'.repeat(40) })).status, 404)
  for (const payload of ['{', JSON.stringify({ a }), JSON.stringify({ a, b: { ...b, value: '999' } })]) {
    assert.equal((await post('', payload)).status, 400)
  }
  assert.equal((await post('', ' '.repeat(8193))).status, 413)
  assert.deepEqual(await (await post('/recommendations', JSON.stringify(address))).json(), { recommendations: [] })
  for (const [scope, expected] of [['article', version], ['corpus', other]] as const) {
    const response = await post('/candidates', JSON.stringify({ ...a, scope }))
    assert.equal(response.status, 200)
    const { candidates } = await response.json()
    assert.ok(candidates.length > 0)
    assert.ok(candidates.every((item: { ref: { versionId: string }; status: string }) =>
      item.ref.versionId === expected.versionId && item.status === 'insufficient'))
  }
})

test('API-13/DATA-02/03/04/06/08/09/12: HTTP serves one valid ZIP, passport, CSV relations and immutable PDF', async t => {
  const f = await fixture(t)
  const v = await f.publish('archived')
  await f.publish('latest', { page: 2 })
  const base = '/api' + v.url
  const source = await f.app.request(base + '/source.pdf')
  assert.equal(source.headers.get('Content-Type'), 'application/pdf')
  assert.deepEqual(Buffer.from(await source.arrayBuffer()), f.pdf)
  const response = await f.app.request(v.downloadUrl)
  assert.equal(response.status, 200)
  assert.equal(response.headers.get('Content-Type'), 'application/zip')
  const archive = join(f.directory, 'bundle.zip')
  await writeFile(archive, Buffer.from(await response.arrayBuffer()))
  const contents = JSON.parse(execFileSync(python, ['-c', [
    'import csv, io, json, sys, zipfile',
    'with zipfile.ZipFile(sys.argv[1]) as archive:',
    '    assert archive.testzip() is None',
    '    files = {name: archive.read(name).decode("utf-8") for name in archive.namelist()}',
    '    rows = {name: list(csv.DictReader(io.StringIO(text))) for name, text in files.items() if name.endswith(".csv")}',
    '    print(json.dumps({"files": files, "rows": rows}))',
  ].join('\n'), archive], { encoding: 'utf8' }))
  const { files, rows } = contents
  assert.deepEqual(Object.keys(rows).sort(),
    ['configurations', 'datasets', 'experiments', 'issues', 'publications', 'results', 'tasks'].map(name => name + '.csv'))
  const manifest = JSON.parse(files['manifest.json'])
  assert.deepEqual([manifest.versionId, manifest.runId, manifest.pdfSha256], [v.versionId, v.runId, f.pdfSha256])
  const passport = await f.app.request(base + '/PASSPORT.md')
  assert.equal(await passport.text(), files['PASSPORT.md'])
  assert.ok(files['PASSPORT.md'].includes(v.versionId))
  assert.ok(files['PASSPORT.md'].includes(f.pdfSha256))
  assert.equal(JSON.parse(files['report.json']).exit_code, 2)
  assert.equal(rows['publications.csv'][0].title, title)
  const zero = rows['results.csv'].find((row: Record<string, string>) => row.raw_value === '0')
  assert.deepEqual([zero.normalized_value, zero.source_page, zero.metric_k, zero.metric_direction], ['0', '1', '1', 'higher_is_better'])
  for (const [name, key] of [['configurations', 'config_id'], ['datasets', 'dataset_id'],
    ['experiments', 'experiment_id'], ['tasks', 'task_id']]) {
    assert.ok(rows[name + '.csv'].some((row: Record<string, string>) => row[key] === zero[key] && row.publication_id === zero.publication_id))
  }
  const config = rows['configurations.csv'].find((row: Record<string, string>) => row.config_id === zero.config_id)
  assert.equal(config.top_k, '5')
  assert.equal(config.generator_model, '')
  assert.ok(rows['issues.csv'].some((row: Record<string, string>) => row.record_ids === zero.result_id))
  assert.equal((await f.app.request(base + '/unknown.txt')).status, 404)
})
