import { z } from 'zod'
import { evidenceSchema, idSchema } from './schema.ts'

export const draftFields = {
  value: 'Числовое значение', unit: 'Единица', generator: 'Генератор',
  retrievalTopK: 'Retrieval top-k', protocol: 'Протокол',
} as const
export const draftFieldSchema = z.enum(['value', 'unit', 'generator', 'retrievalTopK', 'protocol'])
const versionId = z.string().regex(/^version-[a-f0-9]{40}$/)
export const draftCaseSchema = z.object({
  publicationId: idSchema, versionId,
  resultId: idSchema.optional(), remarkId: idSchema.optional(),
}).strict().refine(value => !!(value.resultId || value.remarkId), 'Выберите результат или замечание.')

const content = {
  disposition: z.enum(['proposal', 'leave-open']),
  proposedValue: z.union([z.string().max(4000), z.number().finite(), z.null()]).optional(),
  evidenceIds: z.array(idSchema).max(50).refine(ids => new Set(ids).size === ids.length, 'Источник указан дважды.'),
  reason: z.string().trim().min(1, 'Объясните причину.').max(4000),
}
export const createDraftSchema = z.object({
  publicationId: idSchema, versionId, pdfSha256: z.string().regex(/^[a-f0-9]{64}$/),
  resultId: idSchema.optional(), remarkId: idSchema.optional(), field: draftFieldSchema.optional(),
  requestId: z.uuid(), ...content,
}).strict()
export const updateDraftSchema = z.object({ expectedRevision: z.number().int().positive(), ...content }).strict()
export const deleteDraftSchema = z.object({ expectedRevision: z.number().int().positive() }).strict()
export const reviewDraftSchema = createDraftSchema.omit({ requestId: true }).extend({
  id: idSchema, revision: z.number().int().positive(),
  originalValue: z.union([z.string(), z.number(), z.null()]),
  proposedValue: z.union([z.string(), z.number(), z.null()]),
  createdAt: z.iso.datetime(), updatedAt: z.iso.datetime(),
  evidence: z.array(evidenceSchema.pick({ id: true, page: true, box: true, text: true,
    headers: true, table: true, figure: true, precision: true })),
})
export const draftListSchema = z.object({ drafts: z.array(reviewDraftSchema) })
export type DraftField = z.infer<typeof draftFieldSchema>
export type DraftCase = z.infer<typeof draftCaseSchema>
export type CreateDraft = z.infer<typeof createDraftSchema>
export type UpdateDraft = z.infer<typeof updateDraftSchema>
export type ReviewDraft = z.infer<typeof reviewDraftSchema>
export type StoredDraft = Omit<ReviewDraft, 'evidence'>

export function proposalError(field: DraftField | undefined, value: unknown) {
  if (!field) return 'Выберите поле.'
  if (field === 'value') return typeof value === 'string' && value.length <= 128 && /^[+-]?(?:\d+(?:\.\d+)?|\.\d+)$/.test(value)
    ? undefined : 'Введите десятичное число без экспоненты, например 0.204. Точная запись будет сохранена.'
  if (field === 'retrievalTopK') return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0
    ? undefined : 'Top-k должен быть целым неотрицательным числом в допустимом диапазоне.'
  return typeof value === 'string' && value.trim().length > 0 ? undefined : 'Введите непустое значение поля.'
}
