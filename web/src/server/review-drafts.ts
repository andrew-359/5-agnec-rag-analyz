import { createHash } from 'node:crypto'
import { Hono } from 'hono'
import { bodyLimit } from 'hono/body-limit'
import { ZodError } from 'zod'
import { idSchema } from '../shared/schema.ts'
import { createDraftSchema, updateDraftSchema, deleteDraftSchema, draftCaseSchema, proposalError } from '../shared/review-draft.ts'
import type { DraftCase, DraftField, StoredDraft, UpdateDraft } from '../shared/review-draft.ts'
import type { PublicationStore } from './publication/publication-store.ts'
import { createReviewDraftStore, DraftConflict, DraftMissing } from './review-draft-store.ts'

class BadDraft extends Error {}
export function createReviewDraftsApi({ publications, directory }: { publications: Pick<PublicationStore, 'read'>; directory: string }) {
  const app = new Hono()
  const store = createReviewDraftStore(directory)
  app.use('*', async (c, next) => { c.header('Cache-Control', 'no-store'); await next() })
  app.use('*', bodyLimit({ maxSize: 32 * 1024, onError: c => c.json({ error: 'Слишком большой черновик.' }, 413) }))
  async function context(address: DraftCase & { pdfSha256?: string; field?: DraftField }) {
    draftCaseSchema.parse({ publicationId: address.publicationId, versionId: address.versionId,
      resultId: address.resultId, remarkId: address.remarkId })
    const data = await publications.read(address.publicationId, address.versionId)
    if (!data) throw new DraftMissing('Опубликованная версия не найдена.')
    if (address.pdfSha256 && address.pdfSha256 !== data.pdf.sha256) throw new BadDraft('Черновик относится к другому PDF.')
    const result = address.resultId ? data.results.find(item => item.id === address.resultId) : undefined
    const remark = address.remarkId ? data.remarks.find(item => item.id === address.remarkId) : undefined
    if (address.resultId && !result) throw new BadDraft('Результат не найден в выбранной версии.')
    if (address.remarkId && !remark) throw new BadDraft('Замечание не найдено в выбранной версии.')
    if (result && remark && !remark.resultIds?.includes(result.id)) throw new BadDraft('Результат не связан с выбранным замечанием.')
    if (address.field && !result) throw new BadDraft('Нельзя исправить поле без явно связанного результата.')
    return { data, result, remark }
  }
  async function validate(address: DraftCase & { pdfSha256: string; field?: DraftField }, content: Omit<UpdateDraft, 'expectedRevision'>) {
    const resolved = await context(address)
    const error = content.disposition === 'proposal' ? proposalError(address.field, content.proposedValue) : undefined
    if (error) throw new BadDraft(error)
    if (content.disposition === 'proposal' && !content.evidenceIds.length) throw new BadDraft('Выберите подтверждающий фрагмент.')
    if (content.evidenceIds.some(id => !resolved.data.evidence.some(item => item.id === id))) {
      throw new BadDraft('Источник не принадлежит выбранной версии PDF.')
    }
    return { ...resolved, content: { disposition: content.disposition, reason: content.reason,
      evidenceIds: content.evidenceIds, proposedValue: content.disposition === 'leave-open' ? null : content.proposedValue! } }
  }
  async function present(draft: StoredDraft) {
    const { data } = await context(draft)
    return { ...draft, evidence: draft.evidenceIds.map(id => {
      const evidence = data.evidence.find(item => item.id === id)
      if (!evidence) throw new BadDraft('Сохранённый источник недоступен в этой версии.')
      return evidence
    }) }
  }
  app.get('/drafts', async c => {
    const address = draftCaseSchema.parse(c.req.query())
    await context(address)
    const drafts = await Promise.all(store.list(address).map(present))
    return c.json({ drafts })
  })
  app.post('/drafts', async c => {
    const body = createDraftSchema.parse(await c.req.json().catch(() => null))
    const { data, result, content } = await validate(body, body)
    const { requestId, ...address } = body
    const saved = store.create({ ...address, ...content, pdfSha256: data.pdf.sha256,
      originalValue: body.field && result ? result[body.field] : null }, requestId,
      createHash('sha256').update(JSON.stringify(body)).digest('hex'))
    return c.json(await present(saved.draft), saved.replay ? 200 : 201)
  })
  app.patch('/drafts/:id', async c => {
    const id = idSchema.parse(c.req.param('id'))
    const body = updateDraftSchema.parse(await c.req.json().catch(() => null))
    const current = store.get(id)
    if (!current) throw new DraftMissing('Черновик не найден.')
    const { content } = await validate(current, body)
    return c.json(await present(store.update(id, body.expectedRevision, content)))
  })
  app.delete('/drafts/:id', async c => {
    const id = idSchema.parse(c.req.param('id'))
    const body = deleteDraftSchema.parse(await c.req.json().catch(() => null))
    const current = store.get(id)
    if (!current) throw new DraftMissing('Черновик не найден.')
    await context(current)
    store.delete(id, body.expectedRevision)
    return c.body(null, 204)
  })
  app.onError((error, c) => {
    if (error instanceof DraftConflict) return c.json({ error: error.message }, 409)
    if (error instanceof DraftMissing) return c.json({ error: error.message }, 404)
    if (error instanceof BadDraft) return c.json({ error: error.message }, 400)
    if (error instanceof ZodError) return c.json({ error: error.issues[0]?.message ?? 'Проверьте поля черновика.' }, 400)
    return c.json({ error: 'Не удалось прочитать или сохранить черновик. Ввод не применён; проверьте хранилище и повторите.' }, 500)
  })
  return app
}
