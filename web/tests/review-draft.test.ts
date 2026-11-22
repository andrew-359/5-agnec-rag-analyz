import assert from 'node:assert/strict'
import { test } from 'node:test'
import { mkdtemp, readFile, writeFile, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { randomUUID } from 'node:crypto'
import { createReviewDraftsApi } from '../src/server/review-drafts.ts'
import type { Dataset } from '../src/shared/schema.ts'

async function fixture() {
  const directory = await mkdtemp(join(tmpdir(), 'rag-review-draft-'))
  const data: Dataset = JSON.parse(await readFile(new URL('../../qa/java/src/test/resources/publication.json', import.meta.url), 'utf8'))
  const versionId = 'version-' + 'a'.repeat(40)
  data.results[0].generator = ''
  data.remarks = [{ id: 'review-case', runId: data.run.id, reason: 'Generator unknown', rawText: data.results[0].rawValue,
    evidenceIds: [data.results[0].evidenceIds[0]], resultIds: [data.results[0].id] },
    { id: 'unlinked-case', runId: data.run.id, reason: 'No explicit result', rawText: '', evidenceIds: [] }]
  const path = join(directory, 'publication.json')
  await writeFile(path, JSON.stringify(data))
  const publications = { read: async (publicationId: string, version: string) =>
    publicationId === data.publication.id && version === versionId ? JSON.parse(await readFile(path, 'utf8')) as Dataset : null }
  const options = { publications, directory: join(directory, 'drafts') }
  const address = { publicationId: data.publication.id, versionId, pdfSha256: data.pdf.sha256,
    resultId: data.results[0].id, remarkId: 'review-case', field: 'value' }
  return { directory, data, path, options, address, evidenceId: data.results[0].evidenceIds[0],
    app: createReviewDraftsApi(options) }
}

test('a precise proposal survives reopening SQLite and never changes the published source', async () => {
  const f = await fixture()
  try {
    const originalBytes = await readFile(f.path)
    const body = { ...f.address, requestId: randomUUID(), disposition: 'proposal', proposedValue: '0.00000000000000000001',
      evidenceIds: [f.evidenceId], reason: 'Read the original cell' }
    const response = await f.app.request('/drafts', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
    assert.equal(response.status, 201, await response.clone().text())
    const saved = await response.json()
    assert.equal(saved.originalValue, f.data.results[0].value)
    assert.equal(saved.proposedValue, '0.00000000000000000001')
    assert.equal(saved.revision, 1)
    const reopened = createReviewDraftsApi(f.options)
    const query = new URLSearchParams({ publicationId: f.address.publicationId, versionId: f.address.versionId,
      resultId: f.address.resultId, remarkId: f.address.remarkId })
    const listed = await (await reopened.request('/drafts?' + query)).json()
    assert.equal(listed.drafts.length, 1)
    assert.equal(listed.drafts[0].id, saved.id)
    assert.equal(listed.drafts[0].evidence[0].id, f.evidenceId)
    assert.deepEqual(listed.drafts[0].evidence[0].box, f.data.evidence.find(item => item.id === f.evidenceId)!.box)
    assert.deepEqual(await readFile(f.path), originalBytes)
  } finally { await rm(f.directory, { recursive: true, force: true }) }
})

const write = (app: ReturnType<typeof createReviewDraftsApi>, path: string, method: string, body: unknown) =>
  app.request(path, { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })

test('retries are idempotent and concurrent edits or deletes require the current revision', async () => {
  const f = await fixture()
  try {
    const body = { ...f.address, requestId: randomUUID(), disposition: 'proposal', proposedValue: '0', evidenceIds: [f.evidenceId], reason: 'Zero is printed' }
    const first = await (await write(f.app, '/drafts', 'POST', body)).json()
    const again = await write(f.app, '/drafts', 'POST', body)
    assert.equal(again.status, 200)
    assert.equal((await again.json()).id, first.id)
    assert.equal((await write(f.app, '/drafts', 'POST', { ...body, proposedValue: '1' })).status, 409)
    assert.equal((await write(f.app, '/drafts', 'POST', { ...body, requestId: randomUUID() })).status, 409)
    const changed = await write(f.app, `/drafts/${first.id}`, 'PATCH', {
      expectedRevision: 1, disposition: 'leave-open', evidenceIds: [], reason: 'The figure leaves the unit open',
    })
    assert.equal(changed.status, 200)
    const second = await changed.json()
    assert.equal(second.revision, 2)
    assert.equal(second.originalValue, f.data.results[0].value)
    assert.equal(second.proposedValue, null)
    assert.equal(second.createdAt, first.createdAt)
    assert.equal((await write(f.app, `/drafts/${first.id}`, 'PATCH', {
      expectedRevision: 1, disposition: 'proposal', proposedValue: '1', evidenceIds: [f.evidenceId], reason: 'Stale edit',
    })).status, 409)
    assert.equal((await write(f.app, `/drafts/${first.id}`, 'DELETE', { expectedRevision: 1 })).status, 409)
    assert.equal((await write(f.app, `/drafts/${first.id}`, 'DELETE', { expectedRevision: 2 })).status, 204)
    const query = new URLSearchParams({ publicationId: f.address.publicationId, versionId: f.address.versionId,
      resultId: f.address.resultId, remarkId: f.address.remarkId })
    assert.deepEqual((await (await f.app.request('/drafts?' + query)).json()).drafts, [])
    assert.equal((await write(f.app, '/drafts', 'POST', body)).status, 409, 'Retry must not resurrect a deleted draft')
  } finally { await rm(f.directory, { recursive: true, force: true }) }
})

test('the server rejects invented originals, wrong versions, false remark links, invalid types and foreign evidence', async () => {
  const f = await fixture()
  try {
    const base = { ...f.address, disposition: 'proposal', proposedValue: '0', evidenceIds: [f.evidenceId], reason: 'Check the cell' }
    const variants = [
      { originalValue: '999' }, { pdfSha256: 'b'.repeat(64) }, { field: 'arbitraryJson' },
      { proposedValue: 'NaN' }, { proposedValue: '1e-8' }, { field: 'retrievalTopK', proposedValue: 1.5 },
      { field: 'retrievalTopK', proposedValue: -1 }, { field: 'retrievalTopK', proposedValue: '3' },
      { field: 'retrievalTopK', proposedValue: Number.MAX_SAFE_INTEGER + 1 },
      { evidenceIds: [] }, { evidenceIds: ['foreign-version-evidence'] },
      { evidenceIds: [f.evidenceId, f.evidenceId] }, { remarkId: 'unlinked-case' },
      { resultId: 'missing-result' }, { reason: '   ' }, { field: 'generator', proposedValue: '' },
      { evidence: [{ id: f.evidenceId, box: [0, 0, 1, 1] }] },
    ]
    for (const changes of variants) {
      const response = await write(f.app, '/drafts', 'POST', { ...base, ...changes, requestId: randomUUID() })
      assert.equal(response.status, 400, JSON.stringify(changes) + ': ' + await response.clone().text())
    }
    const absent = await write(f.app, '/drafts', 'POST', { ...base, versionId: 'version-' + 'b'.repeat(40), requestId: randomUUID() })
    assert.equal(absent.status, 404)
    const query = new URLSearchParams({ publicationId: f.address.publicationId, versionId: f.address.versionId,
      resultId: f.address.resultId, remarkId: 'unlinked-case' })
    assert.equal((await f.app.request('/drafts?' + query)).status, 400)
  } finally { await rm(f.directory, { recursive: true, force: true }) }
})

test('an unlinked remark can stay open without creating a result or clearing published values', async () => {
  const f = await fixture()
  try {
    const original = await readFile(f.path)
    const body = { publicationId: f.address.publicationId, versionId: f.address.versionId, pdfSha256: f.address.pdfSha256,
      remarkId: 'unlinked-case', disposition: 'leave-open', evidenceIds: [], reason: 'No source establishing the generator', requestId: randomUUID() }
    const response = await write(f.app, '/drafts', 'POST', body)
    assert.equal(response.status, 201)
    const saved = await response.json()
    assert.equal(saved.resultId, undefined)
    assert.equal(saved.field, undefined)
    assert.equal(saved.disposition, 'leave-open')
    assert.equal(saved.proposedValue, null)
    assert.deepEqual(saved.evidence, [])
    assert.deepEqual(await readFile(f.path), original)
    assert.equal((await write(f.app, '/drafts', 'POST', { ...body, field: 'generator', requestId: randomUUID() })).status, 400)
    // Different fields have independent drafts; unknown remains an empty original, never an invented model.
    const generator = await (await write(f.app, '/drafts', 'POST', { ...f.address, field: 'generator', disposition: 'proposal',
      proposedValue: 'Model X', evidenceIds: [f.evidenceId], reason: 'Explicitly cited', requestId: randomUUID() })).json()
    assert.equal(generator.originalValue, '')
    const number = await write(f.app, '/drafts', 'POST', { ...f.address, disposition: 'proposal',
      proposedValue: '0', evidenceIds: [f.evidenceId], reason: 'Explicit zero', requestId: randomUUID() })
    assert.equal(number.status, 201)
  } finally { await rm(f.directory, { recursive: true, force: true }) }
})

test('a disk failure reports a failed save instead of a successful draft', async () => {
  const f = await fixture()
  try {
    const badDirectory = join(f.directory, 'not-a-directory')
    await writeFile(badDirectory, 'occupied')
    const app = createReviewDraftsApi({ ...f.options, directory: badDirectory })
    const response = await write(app, '/drafts', 'POST', { ...f.address, requestId: randomUUID(), disposition: 'proposal',
      proposedValue: '0', evidenceIds: [f.evidenceId], reason: 'Read the source' })
    assert.equal(response.status, 500)
    const body = await response.text()
    assert.match(body, /Не удалось/)
    assert.ok(!body.includes(badDirectory))
  } finally { await rm(f.directory, { recursive: true, force: true }) }
})

test('the central Hono app mounts the drafts endpoint with an isolated directory', async () => {
  const { createApp } = await import('../src/server/app.ts')
  const { createPublicationStore } = await import('../src/server/publication/publication-store.ts')
  const f = await fixture()
  try {
    const publications = { ...createPublicationStore({ root: '..', directory: join(f.directory, 'unused-publications') }), ...f.options.publications }
    const app = createApp(async () => null, { pdfPath: '', publicationStore: publications,
      reviewDraftStore: f.options.directory })
    const body = { ...f.address, requestId: randomUUID(), disposition: 'proposal', proposedValue: '0', evidenceIds: [f.evidenceId], reason: 'Zero' }
    assert.equal((await write(app, '/api/review/drafts', 'POST', body)).status, 201)
    const query = new URLSearchParams({ publicationId: f.address.publicationId, versionId: f.address.versionId,
      resultId: f.address.resultId, remarkId: f.address.remarkId })
    const response = await app.request('/api/review/drafts?' + query)
    assert.equal(response.status, 200)
    assert.equal((await response.json()).drafts.length, 1)
  } finally { await rm(f.directory, { recursive: true, force: true }) }
})
