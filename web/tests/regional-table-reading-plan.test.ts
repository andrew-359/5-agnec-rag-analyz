import assert from 'node:assert/strict'
import { mkdtemp, mkdir, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { test } from 'node:test'
import { createApp } from '../src/server/app.ts'
import type { Article, ArticleRun } from '../src/shared/article.ts'

test('the map and both reading commands use the text layer and header of each table region', async () => {
  const store = await mkdtemp(join(tmpdir(), 'rag-regional-table-plan-'))
  const id = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
  const folder = join(store, id)
  const shutdown = new AbortController()
  shutdown.abort()
  const strategies = [
    ['docling-tableformer', /пригодный текстовый слой/, null],
    ['docling-ocr-tableformer', /слой.*Method.*Top-k/, null],
    ['docling-rapidocr', /Top-k = 2.*Top-k = 5/, 'docling-ocr-tableformer'],
  ] as const
  try {
    await mkdir(join(folder, 'map'), { recursive: true })
    await writeFile(join(folder, 'name.txt'), 'regional-tables.pdf')
    const features = [
      { text_layer: 'usable', header: 'unconfirmed' },
      { text_layer: 'ocr-required', header: 'other' },
      { text_layer: 'ocr-required', header: 'method-top-k', header_labels: ['Method', 'Top-k = 2', 'Top-k = 5'] },
    ]
    const candidates = features.map((table_read_features, index) => {
      const top = .15 + index * .25
      const box = [.1, top, .9, top + .2]
      const designation = `Table ${['I', 'II', 'III'][index]}`
      return {
        id: `candidate-${index}`, kind: 'table', page: 1, label: ['I', 'II', 'III'][index],
        text: `${designation}. Evaluation.`, box, table_read_features,
        region: { box, bounds_status: 'confirmed', caption_status: 'confirmed',
          designation, caption_box: [.2, top - .03, .8, top - .01], caption_text: `${designation}. Evaluation.` },
      }
    })
    await writeFile(join(folder, 'map', 'inventory.json'), JSON.stringify({
      schema_version: 1, document: 'source.pdf', sha256: 'b'.repeat(64), page_count: 1, candidates, warnings: [],
    }))
    const app = createApp(async () => null, {
      pdfPath: '', articleRoot: resolve('..'), articleStore: store,
      articleDelivery: { url: 'amqp://127.0.0.1:1', shutdown: shutdown.signal, queue: 'unused-regional-plan' },
    })
    const base = `/api/articles/${id}`
    const mapped = await (await app.request(base)).json() as Article
    const accepted = await app.request(`${base}/runs`, { method: 'POST' })
    assert.equal(accepted.status, 202)
    const full = await (await app.request(accepted.headers.get('location')!)).json() as ArticleRun
    assert.equal(full.stages.length, 3)
    for (const [index, [strategy, reason, fallback]] of strategies.entries()) {
      const [stage] = full.stages.filter(item => item.candidateId === candidates[index].id)
      assert.equal(stage.gridRead?.strategy, strategy)
      assert.ok(stage.strategyPlan)
      assert.match(stage.strategyPlan.reason, reason)
      assert.equal(stage.strategyPlan.fallback?.strategy ?? null, fallback)
      const mapCandidate = mapped.candidates.find(item => item.id === stage.candidateId)
      assert.deepEqual(mapCandidate?.strategyPlan, stage.strategyPlan)
      const singleAccepted = await app.request(`${base}/tables/${stage.candidateId}`, { method: 'POST' })
      assert.equal(singleAccepted.status, 202)
      const single = await (await app.request(singleAccepted.headers.get('location')!)).json()
      assert.deepEqual(single.stages[0].strategyPlan, stage.strategyPlan)
    }
    const explicit = await app.request(`${base}/runs`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ readers: { table: 'docling-ocr-tableformer', text: 'pdfplumber-ocr' }, publish: false }),
    })
    assert.equal(explicit.status, 202)
    const overridden = await (await app.request(explicit.headers.get('location')!)).json() as ArticleRun
    assert.equal(overridden.articleText?.strategy, 'pdfplumber-ocr')
    assert.ok(overridden.stages.every(stage => stage.gridRead?.strategy === 'docling-ocr-tableformer'))
    assert.ok(overridden.stages.every(stage => stage.strategyPlan?.fallback == null))

    const oldPaddlePython = process.env.RAG_PADDLE_PYTHON
    try {
      process.env.RAG_PADDLE_PYTHON = '/opt/paddle/bin/python'
      const auto = await app.request(`${base}/runs`, { method: 'POST' })
      assert.equal(auto.status, 202)
      const configured = await (await app.request(auto.headers.get('location')!)).json() as ArticleRun
      assert.deepEqual(configured.stages.map(stage => stage.strategyPlan?.fallback?.strategy ?? null), [
        null, 'paddle-structure', 'docling-ocr-tableformer',
      ])
      assert.equal(configured.stages[1].gridRead?.strategy, 'docling-ocr-tableformer')
    } finally {
      if (oldPaddlePython === undefined) delete process.env.RAG_PADDLE_PYTHON
      else process.env.RAG_PADDLE_PYTHON = oldPaddlePython
    }

    const invalid = await app.request(`${base}/runs`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ readers: { table: 'not-a-reader' } }),
    })
    assert.equal(invalid.status, 400)

  } finally {
    await rm(store, { recursive: true, force: true })
  }
})
