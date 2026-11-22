import assert from 'node:assert/strict'
import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { DatabaseSync } from 'node:sqlite'
import { test } from 'node:test'
import { createApp } from '../src/server/app.ts'
import { openArticleRuns } from '../src/server/article/article-run-store.ts'

test('embedding Hono does not recover article deliveries from ambient RabbitMQ configuration', async (t) => {
  const root = await mkdtemp(join(tmpdir(), 'rag-embedded-app-'))
  t.after(() => rm(root, { recursive: true, force: true }))
  const store = join(root, 'output/web-articles')
  const runs = openArticleRuns(store)
  const { id } = runs.create({ articleId: 'article', document: 'D001.pdf', pdfSha256: '0'.repeat(64) }, [
    { candidateId: 'table-1', page: 6, table: 1, strategy: 'docling-tableformer' },
  ])
  const db = new DatabaseSync(join(store, 'article-runs.sqlite'))
  try {
    db.prepare("UPDATE article_run_stages SET status = 'running', lease_expires_at = 1 WHERE run_id = ?").run(id)
  } finally {
    db.close()
  }
  const env = process.env
  process.env = { ...env, RAG_AMQP_URL: 'amqp://127.0.0.1:1', RAG_ARTICLE_STORE: store }
  t.after(() => { process.env = env })

  const app = createApp(async () => null, { pdfPath: join(root, 'source.pdf'), articleRoot: root })
  const response = await app.request(`/api/article-runs/${id}`)
  assert.equal(response.status, 200)
  assert.equal((await response.json()).stages[0].status, 'running')
})

test('opening a legacy run database preserves saved results and requeues interrupted work', async (t) => {
  const store = await mkdtemp(join(tmpdir(), 'rag-legacy-runs-'))
  t.after(() => rm(store, { recursive: true, force: true }))
  const db = new DatabaseSync(join(store, 'article-runs.sqlite'))
  try {
    db.exec(`
      CREATE TABLE article_runs (
        id TEXT PRIMARY KEY,
        article_id TEXT NOT NULL,
        document TEXT NOT NULL,
        pdf_sha256 TEXT NOT NULL,
        created_at INTEGER NOT NULL
      );
      CREATE TABLE article_run_stages (
        id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES article_runs(id),
        candidate_id TEXT NOT NULL,
        page INTEGER NOT NULL,
        table_number INTEGER NOT NULL,
        strategy TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'done', 'failed')),
        result_count INTEGER,
        error TEXT
      );
      INSERT INTO article_runs VALUES ('old-run', 'old-article', 'D001.pdf', 'saved-hash', 1);
      INSERT INTO article_run_stages VALUES
        ('saved', 'old-run', 'table-1', 6, 1, 'docling-tableformer', 'done', 252, NULL),
        ('interrupted', 'old-run', 'table-2', 7, 2, 'docling-tableformer', 'running', NULL, NULL);
    `)
  } finally {
    db.close()
  }

  const runs = openArticleRuns(store)
  assert.deepEqual(runs.get('old-run')?.stages.map(({ id, status, resultCount }) => ({ id, status, resultCount })), [
    { id: 'saved', status: 'done', resultCount: 252 },
    { id: 'interrupted', status: 'queued', resultCount: null },
  ])
  assert.equal(runs.result('old-run', 'saved')?.folder, join(store, 'runs', 'old-run', 'saved'))
  const delivery = { id: 'interrupted', generation: 1 }
  assert.deepEqual(runs.pendingPublication().map(stage => ({ ...stage })), [delivery])

  runs.recordPublication(delivery)
  const reopened = openArticleRuns(store)
  assert.deepEqual(reopened.pendingPublication(), [])
  assert.deepEqual(reopened.get('old-run'), runs.get('old-run'))
})
