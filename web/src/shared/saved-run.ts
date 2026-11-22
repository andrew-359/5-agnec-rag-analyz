import { z } from 'zod'
import { boxSchema } from './schema.ts'
import { tableRegionSchema } from './article.ts'

export const sourceSchema = z.object({
  page: z.number().int().positive(),
  table: z.number().int().positive(),
  row: z.number().nullable().optional(),
  column: z.number().nullable().optional(),
  headers: z.array(z.string()).default([]),
  box: boxSchema.nullable().optional(),
  evidence_ids: z.array(z.string()).optional(),
})

const conditionValueSchema = z.union([z.string(), z.number()])
const conditionSourcesSchema = z.record(z.string(), z.array(z.string()))
export const savedRemarkOptionSchema = z.object({
  value: conditionValueSchema,
  evidence_ids: z.array(z.string()),
  binding: z.array(z.string()).optional(),
  retrieval_top_k: z.number().int().nonnegative().nullable().optional(),
  condition_sources: conditionSourcesSchema.optional(),
})

export const resultSchema = z.object({
  id: z.string().optional(),
  method: z.string(),
  generator: z.string(),
  dataset: z.string(),
  metric: z.string(),
  raw_value: z.string(),
  value: z.string(),
  unit: z.string().nullable(),
  statistic: z.string().nullable().optional(),
  retrieval_top_k: z.number().nullable().optional(),
  experiment: z.string(),
  task: z.string(),
  protocol: z.string().nullable(),
  source: sourceSchema,
  condition_sources: conditionSourcesSchema.optional(),
  components: z.record(z.string(), z.boolean()).optional(),
})

const remarkSchema = z.object({
  id: z.string().optional(),
  reason: z.string(),
  kind: z.enum(['ambiguous-binding', 'scale-interpretation', 'reading-defect', 'reading-conflict']).nullable().optional(),
  raw_text: z.string(),
  source: sourceSchema,
  field: z.string().nullable().optional(),
  conditions: z.record(z.string(), conditionValueSchema).optional(),
  condition_sources: conditionSourcesSchema.optional(),
  options: z.array(savedRemarkOptionSchema).optional(),
  result_ids: z.array(z.string()).optional(),
})

const evidenceSchema = z.object({
  id: z.string(), page: z.number().int().positive(), box: boxSchema.nullable(), precision: z.string(),
  text: z.string().optional(),
  strategy: z.string().optional(),
})

export const runSchema = z.object({
  document: z.string(),
  page: z.number().int().positive(),
  table: z.number().int().positive(),
  region: tableRegionSchema.optional(),
  results: z.array(resultSchema),
  remarks: z.array(remarkSchema),
  evidence: z.array(evidenceSchema).optional(),
  article_text: z.object({
    sha256: z.string().regex(/^[0-9a-f]{64}$/),
    artifact_sha256: z.string().regex(/^[0-9a-f]{64}$/),
    schema_version: z.literal(1),
  }).optional(),
  reading_followup: z.object({
    reason: z.string(),
    checked_fragments: z.number().int().nonnegative(),
    opened: z.number().int().positive(),
    remaining: z.number().int().nonnegative(),
  }).optional(),
  reading_retry: z.object({
    primary_strategy: z.string(), retry_strategy: z.string(),
    recovered: z.number().int().nonnegative(), preserved: z.number().int().nonnegative(), conflicts: z.number().int().nonnegative(),
  }).optional(),
})

export type Source = z.infer<typeof sourceSchema>
export type Result = z.infer<typeof resultSchema>
export type Run = z.infer<typeof runSchema>
export type RunRemark = z.infer<typeof remarkSchema>
export type RunEvidence = z.infer<typeof evidenceSchema>
export type RunResponse = { run: Run; demo: boolean; assets: { image: boolean; pdf: boolean; csv: boolean }; reviewArtifactId?: string }
