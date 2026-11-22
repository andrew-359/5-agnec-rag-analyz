import { z } from 'zod'
import { boxSchema, idSchema } from './schema.ts'

export const figureSelectionSchema = z.discriminatedUnion('mode', [
  z.object({ mode: z.literal('explicit'), strategy_id: z.string().min(1),
    parameters: z.record(z.string(), z.unknown()) }).strict(),
  z.object({ mode: z.literal('auto') }).strict(),
])
export const figureRunRequestSchema = z.object({
  target: z.object({ stage: z.literal('figure'), candidate_id: z.string().min(1) }).strict(),
  selection: figureSelectionSchema,
}).strict()

export const preparedFigureTargetSchema = figureRunRequestSchema.shape.target.extend({
  page: z.number().int().positive(), figure: z.string().nullable(), box: boxSchema.nullable(),
  region: z.object({ box: boxSchema, precision: z.enum(['figure', 'page']), panels: z.array(z.object({
    panel: z.string(), panel_box_pt: z.tuple([z.number(), z.number(), z.number(), z.number()]),
    plot_box_pt: z.tuple([z.number(), z.number(), z.number(), z.number()]),
  }).strict()) }).strict(),
}).strict()
export const regionRunRequestSchema = z.object({
  pdfSha256: z.string().regex(/^[a-f0-9]{64}$/),
  page: z.number().int().positive(),
  box: boxSchema,
  candidateId: idSchema.optional(),
}).strict()
export type RegionRunRequest = z.infer<typeof regionRunRequestSchema>
export type RegionTarget = {
  stage: 'region'
  page: number
  box: RegionRunRequest['box']
  pdf_sha256: string
  parent_run_id: string
  candidate_id: string | null
}

export type CandidateFigurePlan = {
  target: z.infer<typeof preparedFigureTargetSchema>
  selection: z.infer<typeof figureSelectionSchema>
}

export type FigurePlan = CandidateFigurePlan | { target: RegionTarget; selection: { mode: 'auto' } }
