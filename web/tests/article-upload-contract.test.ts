import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { mkdtemp, readFile, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { test, type TestContext } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createApp } from '../src/server/app.ts'

const root = fileURLToPath(new URL('../../', import.meta.url))

async function fixture(t: TestContext) {
  const directory = await mkdtemp(join(tmpdir(), 'rag-upload-contract-'))
  t.after(() => rm(directory, { recursive: true, force: true }))
  const openApp = () => createApp(async () => null, { pdfPath: '', articleRoot: root,
    articleStore: join(directory, 'articles'), reviewDraftStore: join(directory, 'drafts') })
  const app = openApp()
  function upload(body: Uint8Array | string, type = 'application/pdf', name = 'Пример.pdf') {
    return app.request('/api/articles', {
      method: 'POST', headers: { 'Content-Type': type, 'X-PDF-Name': encodeURIComponent(name) },
      body: typeof body === 'string' ? body : new Uint8Array(body),
    })
  }
  return { app, openApp, upload }
}

test('article ingestion rejects a wrong content type or file signature without adding a library item', async t => {
  const { app, upload } = await fixture(t)
  for (const [body, type] of [['%PDF-1.4\n', 'text/plain'], ['plain text', 'application/pdf']]) {
    assert.equal((await upload(body, type)).status, 415)
  }
  assert.deepEqual(await (await app.request('/api/articles')).json(), { articles: [], unavailable: 0 })
})

test('real Python rejects a broken PDF, then builds a persistent map and serves the original bytes', {
  skip: process.env.RAG_PYTHON ? false : 'Set RAG_PYTHON for real map extraction',
}, async t => {
  const { app, openApp, upload } = await fixture(t)
  const broken = await upload('%PDF-1.4\ninvalid body')
  assert.equal(broken.status, 422)
  assert.ok((await broken.json()).error)
  assert.deepEqual(await (await app.request('/api/articles')).json(), { articles: [], unavailable: 0 })
  const input = await readFile(new URL('../../qa/java/src/test/resources/synthetic.pdf', import.meta.url))
  const response = await upload(input)
  assert.equal(response.status, 201, await response.clone().text())
  const accepted = await response.json()
  const digest = createHash('sha256').update(input).digest('hex')
  assert.equal(accepted.sha256, digest)
  assert.equal(accepted.document, 'Пример.pdf')
  assert.equal(accepted.page_count, 2)
  const reopened = openApp()
  const library = await (await reopened.request('/api/articles')).json()
  assert.equal(library.articles.length, 1)
  assert.deepEqual([library.articles[0].id, library.articles[0].pageCount, library.articles[0].runId],
    [accepted.id, 2, null])
  assert.equal((await (await reopened.request('/api/articles/' + accepted.id)).json()).sha256, digest)
  const source = await reopened.request('/api/articles/' + accepted.id + '/source.pdf')
  assert.equal(source.status, 200)
  assert.match(source.headers.get('Content-Type') ?? '', /^application\/pdf\b/)
  assert.deepEqual(Buffer.from(await source.arrayBuffer()), input)
})
