import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { mkdir, mkdtemp, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { DatabaseSync } from 'node:sqlite'
import { test } from 'node:test'
import { createApp } from '../src/server/app.ts'
import { openArticleRuns } from '../src/server/article/article-run-store.ts'
import { reconciliationPath } from '../src/server/article/article-publication-artifacts.ts'

test('run routes serve only the files assigned to each completed stage kind', async (t) => {
  const root = await mkdtemp(join(tmpdir(), 'rag-stage-files-'))
  t.after(() => rm(root, { recursive: true, force: true }))
  const store = join(root, 'output', 'web-articles')
  const runs = openArticleRuns(store)
  const { id } = runs.create(
    { articleId: 'article', document: 'D001.pdf', pdfSha256: '0'.repeat(64) },
    [{ candidateId: 'table-1', page: 6, table: 1, strategy: 'docling-tableformer' }],
    null,
    { figures: [{ candidateId: 'figure-3', page: 8, label: '3' }] },
  )
  const db = new DatabaseSync(join(store, 'article-runs.sqlite'))
  try {
    db.prepare("UPDATE article_run_stages SET status = 'done' WHERE run_id = ?").run(id)
  } finally {
    db.close()
  }

  const run = runs.get(id)
  assert.ok(run)
  const [table] = run.stages
  const [figure] = run.figures
  const csv = 'metric,value\nEM,0.422\n'
  const plot = JSON.stringify({ results: [{ value: 0.528 }] })
  for (const stage of [table, figure]) {
    const saved = runs.result(id, stage.id)
    assert.ok(saved)
    await mkdir(saved.folder, { recursive: true })
    await writeFile(join(saved.folder, 'results.csv'), csv)
    await writeFile(join(saved.folder, 'extracted.json'), plot)
    await writeFile(join(saved.folder, 'run.json'), '{}')
  }

  const app = createApp(async () => null, { pdfPath: join(root, 'source.pdf'), articleRoot: root })
  const value = await app.request(`/api/article-runs/${id}/stages/${table.id}/results.csv`)
  assert.equal(value.status, 200)
  assert.equal(value.headers.get('content-disposition'), 'attachment; filename="results.csv"')
  assert.equal(await value.text(), csv)

  const observation = await app.request(`/api/article-runs/${id}/stages/${figure.id}/extracted.json`)
  assert.equal(observation.status, 200)
  assert.equal(observation.headers.get('cache-control'), 'no-store')
  assert.equal(await observation.text(), plot)

  for (const [stage, file] of [[table, 'extracted.json'], [figure, 'results.csv'], [table, 'run.json']] as const) {
    const response = await app.request(`/api/article-runs/${id}/stages/${stage.id}/${file}`)
    assert.equal(response.status, 404)
  }
})

test('an experiment serves prose from its reconciliation without publication', async (t) => {
  const root = await mkdtemp(join(tmpdir(), 'rag-reconciled-prose-'))
  t.after(() => rm(root, { recursive: true, force: true }))
  const store = join(root, 'output', 'web-articles')
  const runs = openArticleRuns(store)
  const sha256 = '1'.repeat(64)
  const { id } = runs.create({ articleId: 'article', document: 'sample.pdf', pdfSha256: sha256 }, [], null,
    { reconcile: true })
  const saved = { source: { sha256 }, results: [], remarks: [], evidence: [] }
  await mkdir(join(store, 'runs', id), { recursive: true })
  await writeFile(reconciliationPath(store, id), JSON.stringify({
    schema_version: 1, run_id: id, source: { sha256 },
    artifacts: [{ id: 'prose-results', kind: 'prose', data: saved }], relationships: [],
  }))
  const app = createApp(async () => null, { pdfPath: join(root, 'source.pdf'), articleRoot: root })
  const response = await app.request(`/api/article-runs/${id}/prose-results.json`)
  assert.equal(response.status, 200)
  assert.deepEqual(await response.json(), saved)
  assert.equal(runs.get(id)?.publicationStatus, undefined)
})

test('a run card preserves grid history and counts reading separately from confirmed results', async (t) => {
  const root = await mkdtemp(join(tmpdir(), 'rag-run-history-'))
  t.after(() => rm(root, { recursive: true, force: true }))
  const store = join(root, 'output', 'web-articles')
  const runs = openArticleRuns(store)
  const { id } = runs.create(
    { articleId: 'article', document: 'D001.pdf', pdfSha256: '0'.repeat(64) },
    [
      { candidateId: 'table-1', page: 6, table: 1, strategy: 'docling-tableformer' },
      { candidateId: 'table-2', page: 7, table: 2, strategy: 'docling-tableformer' },
    ],
    { excludedCandidates: [], discoveryWarnings: [], discoveryLimitations: [] },
    { withText: true },
  )
  const db = new DatabaseSync(join(store, 'article-runs.sqlite'))
  const grid = db.prepare(`SELECT id FROM article_run_stages
    WHERE run_id = ? AND candidate_id = 'table-2' AND kind = 'table-grid'`).get(id)?.id as string
  assert.ok(grid)
  try {
    db.prepare("UPDATE article_run_stages SET status = 'done' WHERE run_id = ? AND kind != 'table'").run(id)
    db.prepare(`UPDATE article_run_stages SET status = 'done', result_count = 12, remark_count = 5, ambiguous_binding_count = 2
      WHERE run_id = ? AND kind = 'table' AND candidate_id = 'table-1'`).run(id)
    db.prepare(`UPDATE article_run_stages SET status = 'failed', generation = attempt_limit, error = 'Нет привязки.'
      WHERE run_id = ? AND kind = 'table' AND candidate_id = 'table-2'`).run(id)
    const addAttempt = db.prepare(`INSERT INTO article_run_attempts
      (id, stage_id, number, status, started_at, has_grid_artifact) VALUES (?, ?, ?, ?, ?, ?)`)
    addAttempt.run('accepted-grid', grid, 2, 'succeeded', 2, 1)
    addAttempt.run('earlier-grid', grid, 1, 'failed', 1, 0)
  } finally {
    db.close()
  }
  const folder = join(store, 'runs', id, grid, 'attempts', 'accepted-grid')
  const artifact = '{"page":7,"table":2}'
  await mkdir(folder, { recursive: true })
  await writeFile(join(folder, 'table-grid.json'), artifact)

  const app = createApp(async () => null, { pdfPath: '', articleRoot: root })
  const url = `/api/article-runs/${id}`
  const response = await app.request(url)
  assert.equal(response.status, 200)
  const run = await response.json()
  assert.deepEqual(run.coverage, {
    excludedCandidates: [], discoveryWarnings: [], discoveryLimitations: [], deferredFigures: [],
    detectedCandidates: 2, plannedStages: 2, completedStages: 1, readTables: 2, failedStages: 1,
    confirmedResults: 12, ambiguousBindings: 2, otherRemarks: 3,
  })
  const failed = run.stages[1]
  assert.equal(failed.error, 'Исчерпан лимит попыток (3). Нет привязки.')
  assert.equal(failed.gridRead.id, grid)
  assert.deepEqual(failed.gridRead.attempts.map(({ number, hasGridArtifact }: { number: number; hasGridArtifact: boolean }) =>
    [number, hasGridArtifact]), [[1, false], [2, true]])

  const attemptUrl = `${url}/stages/${grid}/attempts`
  assert.equal((await app.request(`${attemptUrl}/1/table-grid.json`)).status, 404)
  const saved = await app.request(`${attemptUrl}/2/table-grid.json`)
  assert.equal(saved.status, 200)
  assert.equal(await saved.text(), artifact)
})

test('a region launch rejects malformed page geometry before attempting to queue work', async (t) => {
  const root = await mkdtemp(join(tmpdir(), 'rag-region-request-'))
  t.after(() => rm(root, { recursive: true, force: true }))
  const store = join(root, 'output', 'web-articles')
  const runs = openArticleRuns(store)
  const pdfSha256 = '1'.repeat(64)
  const { id } = runs.create({ articleId: 'article', document: 'sample.pdf', pdfSha256 }, [])
  const app = createApp(async () => null, { pdfPath: '', articleRoot: root })
  const route = `/api/article-runs/${id}/region-runs`
  for (const payload of [
    { pdfSha256, page: 0, box: [0.1, 0.2, 0.6, 0.9] },
    { pdfSha256, page: 1, box: [0.6, 0.2, 0.1, 0.9] },
    { pdfSha256, page: 1, box: [0.1, 0.2, 0.6, 1.1] },
    { pdfSha256, page: 1, box: [0.1, 0.2, 0.1, 0.9] },
  ]) {
    const response = await app.request(route, { method: 'POST', body: JSON.stringify(payload) })
    assert.equal(response.status, 400)
  }
  const switched = await app.request(route, { method: 'POST', body: JSON.stringify({
    pdfSha256: '2'.repeat(64), page: 1, box: [0.1, 0.2, 0.6, 0.9],
  }) })
  assert.equal(switched.status, 409)
})

test('the run PDF remains pinned to the saved document digest', async (t) => {
  const root = await mkdtemp(join(tmpdir(), 'rag-run-pdf-'))
  t.after(() => rm(root, { recursive: true, force: true }))
  const store = join(root, 'output', 'web-articles')
  const articleId = 'article'
  const source = join(store, articleId, 'map', 'source.pdf')
  const pdf = Buffer.from('%PDF-1.4\noriginal')
  const pdfSha256 = createHash('sha256').update(pdf).digest('hex')
  const runs = openArticleRuns(store)
  const { id } = runs.create({ articleId, document: 'sample.pdf', pdfSha256 }, [])
  await mkdir(join(store, articleId, 'map'), { recursive: true })
  await writeFile(source, pdf)
  const app = createApp(async () => null, { pdfPath: '', articleRoot: root })
  const url = `/api/article-runs/${id}/source.pdf`
  const original = await app.request(url + '?sha256=' + pdfSha256)
  assert.equal(original.status, 200)
  assert.deepEqual(Buffer.from(await original.arrayBuffer()), pdf)
  assert.equal((await app.request(url + '?sha256=' + '0'.repeat(64))).status, 404)
  await writeFile(source, Buffer.from('%PDF-1.4\nchanged'))
  assert.equal((await app.request(url)).status, 404)
  assert.equal((await app.request(url + '?sha256=' + pdfSha256)).status, 404)
})
