import { z } from 'zod'
import { boxSchema, idSchema } from './schema.ts'

export const reconciliationFilename = 'reconciliation.json'
export const articleContextFilename = 'article-context.json'
const page = z.number().int().positive()
const fragment = z.looseObject({
  id: idSchema, page, text: z.string().min(1), box: boxSchema.nullish(),
  precision: z.enum(['page', 'text_fragment', 'figure']),
})
const basis = z.object({ kind: z.string().min(1), source: fragment })
const selection = {
  schema_version: z.literal(1),
  sha256: z.string().regex(/^[a-f0-9]{64}$/),
  reason: z.string().min(1),
}
const tableContext = z.looseObject({
  ...selection,
  target: z.looseObject({ page, number: page, candidate: z.never().optional() }),
  status: z.enum(['supported', 'ambiguous', 'not_found']),
  candidates: z.array(z.object({ fragments: z.array(fragment), basis: z.array(basis) })),
  engine_version: z.string().min(1).nullable(),
  setup_fragments: z.array(fragment),
  followup_fragments: z.array(fragment),
  fixed_depths: z.array(z.object({ scope: z.string().min(1), depth: page, declaration: fragment, definition: fragment, rows: z.array(fragment).min(1) })),
  local_basis: z.array(basis),
}).refine(({ status, candidates }) => {
  if (status === 'supported') return candidates.length === 1
  return status === 'ambiguous' ? candidates.length >= 1 : candidates.length === 0
})
const figureContext = z.looseObject({
  ...selection,
  target: z.looseObject({ page, candidate: idSchema, number: z.never().optional() }),
  status: z.enum(['supported', 'caption_only']),
  basis: z.array(basis).min(1),
})
export const articleContextSchema = z.union([tableContext, figureContext])
const observationSchema = z.object({ artifact_id: idSchema, result_id: idSchema })
export const measurementRelationshipSchema = z.object({
  status: z.enum(['agreement', 'conflict', 'open']),
  members: z.array(observationSchema).min(2).refine(
    members => new Set(members.map(item => JSON.stringify([item.artifact_id, item.result_id]))).size === members.length,
    'В связи повторяется наблюдение.',
  ),
}).refine(({ status, members }) => {
  const sources = new Set(members.map(item => item.artifact_id)).size
  return sources >= 2 && (status === 'open' || sources === members.length)
}, 'Для связи нужны наблюдения из независимых артефактов.')
export type MeasurementRelationship = z.infer<typeof measurementRelationshipSchema>
export const measurementRelationshipsSchema = z.array(measurementRelationshipSchema).refine(relationships => {
  const decided = relationships.filter(item => item.status !== 'open').flatMap(item => item.members)
  return new Set(decided.map(item => JSON.stringify([item.artifact_id, item.result_id]))).size === decided.length
}, 'У наблюдения больше одного итогового отношения.')

export const reconciliationSchema = z.object({
  schema_version: z.literal(1),
  run_id: idSchema,
  source: z.object({ sha256: z.string().regex(/^[a-f0-9]{64}$/) }),
  artifacts: z.array(z.object({ id: idSchema, kind: z.enum(['table', 'figure', 'prose']), data: z.unknown() })),
  relationships: measurementRelationshipsSchema,
  summary: z.object({
    confirmed: z.array(observationSchema),
    disputed: z.array(measurementRelationshipSchema),
    stage_remarks: z.array(z.object({ artifact_id: idSchema, remark_id: idSchema })),
  }).optional(),
  contexts: z.array(z.object({ artifact_id: idSchema, data: articleContextSchema })).default([]),
  rejectedArtifacts: z.array(z.object({
    id: idSchema, kind: z.enum(['table', 'figure']), file: z.string(), reason: z.string(),
    page: z.number().optional(), table: z.number().optional(), figure: z.string().nullish(),
  })).default([]),
  proseError: z.string().nullish(),
})
