import assert from 'node:assert/strict'
import { mkdtemp, mkdir, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { test } from 'node:test'
import { createApp } from '../src/server/app.ts'
import type { ArticleRun } from '../src/shared/article.ts'

const articleId = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
const sourceRoot = fileURLToPath(new URL('../..', import.meta.url))
const scanPage = { page: 1, text_chars: 0, image_boxes: [[.02, .02, .98, .98]] }
const nativePage = { page: 2, text_chars: 1800, image_boxes: [] }

async function plannedText(pages?: object[], reader?: string, pageCount = pages?.length ?? 1) {
  const store = await mkdtemp(join(tmpdir(), 'rag-text-selection-'))
  const folder = join(store, articleId)
  try {
    await mkdir(join(folder, 'map'), { recursive: true })
    await writeFile(join(folder, 'name.txt'), 'article.pdf')
    await writeFile(join(folder, 'map', 'inventory.json'), JSON.stringify({
      schema_version: 1, document: 'source.pdf', sha256: 'b'.repeat(64), page_count: pageCount,
      pages, candidates: [], warnings: [],
    }))
    const app = createApp(async () => null, {
      pdfPath: '', articleRoot: sourceRoot, articleStore: store,
      articleDelivery: { url: 'amqp://127.0.0.1:1', shutdown: AbortSignal.abort(), queue: 'unused-text-plan' },
    })
    const accepted = await app.request(`/api/articles/${articleId}/runs`, {
      method: 'POST',
      body: reader ? JSON.stringify({ readers: { text: reader } }) : undefined,
    })
    assert.equal(accepted.status, 202, await accepted.clone().text())
    const response = await app.request(accepted.headers.get('location')!)
    assert.equal(response.status, 200)
    return (await response.json() as ArticleRun).articleText
  } finally {
    await rm(store, { recursive: true, force: true })
  }
}

test('Auto plans positioned OCR for a PDF with only scanned pages', async () => {
  const text = await plannedText([scanPage, { ...scanPage, page: 2 }])
  assert.equal(text?.strategy, 'pdfplumber-ocr')
  assert.match(text?.strategyReason ?? '', /нативного текста.*растровое изображение/)
})

test('Auto keeps native reading for a digital PDF with an empty page', async () => {
  const text = await plannedText([{ page: 1, text_chars: 0, image_boxes: [] }, nativePage])
  assert.equal(text?.strategy, 'pdfplumber')
})

test('Auto keeps native reading when the map mixes scanned and native pages', async () => {
  const text = await plannedText([scanPage, nativePage], 'auto')
  assert.equal(text?.strategy, 'pdfplumber')
})

test('Auto keeps native reading when the map has no evidence of a full-page scan', async () => {
  for (const text of [
    await plannedText(),
    await plannedText([{ page: 1, image_boxes: scanPage.image_boxes }]),
    await plannedText([scanPage], undefined, 2),
    await plannedText([{ page: 1, text_chars: 0, image_boxes: [] }]),
    await plannedText([{ page: 1, text_chars: 0, image_boxes: [[.1, .1, .3, .3]] }]),
    await plannedText([{ ...scanPage, text_chars: 53 }]),
  ]) {
    assert.equal(text?.strategy, 'pdfplumber')
  }
})

test('an explicit reader wins over the automatic text choice', async () => {
  assert.equal((await plannedText([scanPage], 'pdfplumber'))?.strategy, 'pdfplumber')
  assert.equal((await plannedText([{ ...nativePage, page: 1 }], 'pdfplumber-ocr'))?.strategy, 'pdfplumber-ocr')
})
