import assert from 'node:assert/strict'
import { mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { DatabaseSync } from 'node:sqlite'
import { test } from 'node:test'
import { setTimeout } from 'node:timers/promises'
import { loadReleaseArtifacts, reconciliationPath } from '../src/server/article/article-publication-artifacts.ts'
import { startArticlePublication } from '../src/server/article/article-publication.ts'
import { articleRunRoutes } from '../src/server/article/article-run-routes.ts'
import { openArticleRuns } from '../src/server/article/article-run-store.ts'
import type { PublicationStore } from '../src/server/publication/publication-store.ts'
import { buildPublication, parsePublicationArtifact, publicationInputSchema } from '../src/server/publication/publication-data.ts'
import { publishPartialTable } from '../src/server/publication/partial-table.ts'

test('the normal release publishes a table containing only partial measurements', async t => {
  const store = await mkdtemp(join(tmpdir(), 'rag-release-partial-'))
  const shutdown = new AbortController()
  t.after(async () => { shutdown.abort(); await rm(store, { recursive: true, force: true }) })
  const runs = openArticleRuns(store)
  const sha = '0'.repeat(64)
  const { id } = runs.create({ articleId: 'article', document: 'P001.pdf', pdfSha256: sha },
    [{ candidateId: 'table', page: 7, table: 1, strategy: 'docling-tableformer' }],
    { excludedCandidates: [], discoveryWarnings: [], discoveryLimitations: [] }, { publish: true, withText: false })
  const stage = runs.get(id)!.stages[0].id
  const box = [0.1, 0.2, 0.3, 0.4]
  const source = { page: 7, table: 1, row: 3, column: 3, headers: ['recall@15'], box, evidence_ids: ['cell'] }
  const data = { source: { sha256: sha }, page: 7, table: 1,
    evidence: [
      { id: 'cell', page: 7, text: '0.702', precision: 'cell', box },
      { id: 'metric', page: 7, text: 'recall@15', precision: 'cell' },
      { id: 'label', page: 7, text: 'Our embedding model', precision: 'cell' },
    ],
    results: [],
    remarks: [{ id: 'i1', kind: 'ambiguous-binding', field: 'method', reason: 'Открыт метод',
      raw_text: '0.702', source, conditions: { metric: 'recall@15', metric_k: 15, 'Model type': 'Our embedding model' },
      condition_sources: { metric: ['metric'], metric_k: ['metric'], 'Model type': ['label'] } }],
  }
  const db = new DatabaseSync(join(store, 'article-runs.sqlite'))
  db.prepare("UPDATE article_run_stages SET status = 'done' WHERE id = ?").run(stage)
  db.close()
  const destination = reconciliationPath(store, id)
  await mkdir(join(store, 'runs', id), { recursive: true })
  await writeFile(destination, JSON.stringify({ schema_version: 1, run_id: id, source: { sha256: sha },
    artifacts: [{ id: stage, kind: 'table', data }], relationships: [] }))
  let published: ReturnType<typeof buildPublication> | undefined
  startArticlePublication(runs, { store, sourcePdf: () => 'unused.pdf', shutdown: shutdown.signal,
    pythonTask: async () => { throw new Error('Expected saved reconciliation') },
    publications: { forRun: async () => null, publish: async input => {
      published = buildPublication(publicationInputSchema.parse(input), 'test-version', sha)
      return published.dataset.version!
    } },
  })
  const deadline = Date.now() + 3_000
  while (runs.get(id)?.publicationStatus !== 'done' && Date.now() < deadline) await setTimeout(30)
  assert.equal(runs.get(id)?.publicationStatus, 'done')
  assert.ok(published)
  assert.deepEqual(published.dataset.results.map(r => [r.partial, r.metric, r.rawValue, r.value]),
    [[true, 'Recall@15', '0.702', null]])
  const [row] = published.tables.results
  assert.equal(row.metric_k, 15)
  assert.equal(row.config_id, undefined)
  assert.equal(row.source_page, 7)
  assert.ok(published.tables.issues.some(i => i.record_ids === row.result_id
    && i.check_name === 'method' && String(i.description).includes('Our embedding model')))
  const alreadyPublished = { ...data, results: [{ id: 'existing', source, method: 'known', metric: 'Recall@15',
    task: 'retrieval', dataset: 'known', experiment: 'known', raw_value: '0.702', value: '0.702' }] }
  const full = parsePublicationArtifact(alreadyPublished, 'stage', sha)
  assert.equal(publishPartialTable(full), full)
  const noMetric = parsePublicationArtifact({ ...data, remarks: data.remarks.map(r => ({ ...r, conditions: {} })) }, 'stage', sha)
  assert.equal(publishPartialTable(noMetric), noMetric)
})

test('publication waits for extraction and only the current lease can install its artifact', async (t) => {
  const store = await mkdtemp(join(tmpdir(), 'rag-release-lease-'))
  t.after(() => rm(store, { recursive: true, force: true }))
  const runs = openArticleRuns(store)
  const { id } = runs.create(
    { articleId: 'article', document: 'D001.pdf', pdfSha256: '0'.repeat(64) },
    [{ candidateId: 'table-1', page: 6, table: 1, strategy: 'docling-tableformer' }],
    null,
    { publish: true },
  )

  assert.equal(runs.get(id)?.publicationStatus, 'queued')
  assert.equal(runs.claimRelease(), null)
  const db = new DatabaseSync(join(store, 'article-runs.sqlite'))
  try {
    db.prepare("UPDATE article_run_stages SET status = 'done' WHERE run_id = ?").run(id)
    const first = runs.claimRelease()
    assert.ok(first)
    assert.equal(first.runId, id)

    db.prepare('UPDATE article_run_publications SET available_at = 0 WHERE run_id = ?').run(id)
    const current = runs.claimRelease()
    assert.ok(current)
    assert.equal(current.runId, id)
    assert.notEqual(current.token, first.token)

    const temporary = join(store, 'prose.tmp')
    const destination = join(store, 'prose.json')
    await writeFile(temporary, '{}')
    assert.throws(() => runs.saveReleaseArtifact(id, first.token, temporary, destination), /Истёк срок/)
    assert.equal(runs.renewRelease(id, first.token), false)
    runs.finishRelease(id, first.token, null)
    assert.equal(runs.get(id)?.publicationStatus, 'running')

    assert.equal(runs.renewRelease(id, current.token), true)
    runs.saveReleaseArtifact(id, current.token, temporary, destination)
    assert.equal(await readFile(destination, 'utf8'), '{}')
    runs.finishRelease(id, current.token, null, 'Нет подтверждённых результатов.')
    assert.equal(runs.get(id)?.publicationStatus, 'empty')
    assert.equal(runs.get(id)?.publicationNote, 'Нет подтверждённых результатов.')
  } finally {
    db.close()
  }
})

test('packaging retries consume the same saved reconciliation after the stage file disappears', async t => {
  const store = await mkdtemp(join(tmpdir(), 'rag-release-retry-'))
  const shutdown = new AbortController()
  const db = new DatabaseSync(join(store, 'article-runs.sqlite'))
  t.after(async () => { shutdown.abort(); db.close(); await rm(store, { recursive: true, force: true }) })
  const runs = openArticleRuns(store)
  const fixture = new URL('../../runs/snapshots/D001/run.json', import.meta.url)
  const raw = JSON.parse(await readFile(fixture, 'utf8'))
  const { id } = runs.create({ articleId: 'article', document: 'D001.pdf', pdfSha256: raw.source.sha256 },
    [{ candidateId: 'table-1', page: 6, table: 1, strategy: 'docling-tableformer' }],
    { excludedCandidates: [], deferredFigures: [], discoveryWarnings: [], discoveryLimitations: [] },
    { publish: true },
  )
  const stage = runs.get(id)!.stages[0].id
  const stageFolder = join(store, 'runs', id, stage)
  const path = join(stageFolder, 'run.json')
  await mkdir(stageFolder, { recursive: true })
  await writeFile(path, JSON.stringify(raw))
  db.prepare("UPDATE article_run_stages SET status = 'done' WHERE id = ?").run(stage)
  let computations = 0
  const snapshots: string[] = []
  const publications = {
    forRun: async () => null,
    async publish(input) {
      snapshots.push(await readFile(reconciliationPath(store, id), 'utf8'))
      if (snapshots.length === 1) await rm(path)
      if (snapshots.length <= 3) throw new Error('Управляемый сбой упаковки')
      return { publicationId: input.publicationId, versionId: 'test-version', runId: id, pdfSha256: input.pdfSha256,
        coverage: input.coverage, url: '/test', downloadUrl: '/test/zip' }
    },
  } satisfies Pick<PublicationStore, 'forRun' | 'publish'>
  startArticlePublication(runs, {
    store, sourcePdf: () => join(store, 'unused.pdf'),
    shutdown: shutdown.signal, publications,
    async pythonTask(args) {
      computations++
      const report = { schema_version: 1, run_id: id, source: { sha256: raw.source.sha256 },
        artifacts: [{ id: stage, kind: 'table', data: raw }], relationships: [] }
      await writeFile(args[args.indexOf('--out') + 1], JSON.stringify(report))
      return Buffer.alloc(0)
    },
  })
  const deadline = Date.now() + 8_000
  while (runs.get(id)?.publicationStatus !== 'failed' && Date.now() < deadline) {
    db.prepare("UPDATE article_run_publications SET available_at = 0 WHERE run_id = ? AND status = 'queued'").run(id)
    await setTimeout(50)
  }
  assert.equal(runs.get(id)?.publicationStatus, 'failed')
  const endpoint = articleRunRoutes(store, runs, () => join(store, 'unused.pdf'))
  const requestPath = `/${id}/publication/retry`
  assert.equal((await endpoint.request(requestPath, { method: 'POST' })).status, 202)
  assert.equal((await endpoint.request(requestPath, { method: 'POST' })).status, 409)
  while (runs.get(id)?.publicationStatus !== 'done' && Date.now() < deadline) await setTimeout(50)
  assert.equal(runs.get(id)?.publicationStatus, 'done')
  assert.equal(runs.get(id)?.reconciliationStatus, 'done')
  assert.equal(computations, 1)
  assert.equal(snapshots.length, 4)
  assert.equal(new Set(snapshots).size, 1)
})

test('a saved table with a different address does not discard other publication artifacts', async (t) => {
  const store = await mkdtemp(join(tmpdir(), 'rag-release-sources-'))
  t.after(() => rm(store, { recursive: true, force: true }))
  const fixture = new URL('../../runs/snapshots/D001/run.json', import.meta.url)
  const raw = await readFile(fixture, 'utf8')
  const hash = JSON.parse(raw).source.sha256
  const runs = openArticleRuns(store)
  const { id } = runs.create({ articleId: 'article', document: 'D001.pdf', pdfSha256: hash }, [
    { candidateId: 'table-1', page: 6, table: 1, strategy: 'docling-tableformer' },
    { candidateId: 'table-2', page: 7, table: 2, strategy: 'docling-tableformer' },
  ])
  const run = runs.get(id)
  assert.ok(run)
  const [first, second] = run.stages
  const saved = join(store, 'run.json')
  await writeFile(saved, raw)

  const { sourceArtifacts, rejectedArtifacts } = await loadReleaseArtifacts(run, [
    { id: first.id, kind: 'table', path: saved },
    { id: second.id, kind: 'table', path: saved },
  ])

  assert.equal(sourceArtifacts.length, 1)
  assert.equal(sourceArtifacts[0].id, first.id)
  assert.equal(sourceArtifacts[0].data.results.length, 252)
  assert.deepEqual(rejectedArtifacts, [{
    id: second.id,
    kind: 'table',
    file: 'run.json',
    page: 7,
    table: 2,
    figure: undefined,
    reason: 'Адрес сохранённой таблицы отличается от адреса этапа.',
  }])
})
