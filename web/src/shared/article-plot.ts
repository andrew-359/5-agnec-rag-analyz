import { z } from 'zod'
import { boxSchema } from './schema.ts'

export const figureDecisionSchema = z.enum(['data', 'illustration'])
export type FigureDecision = z.infer<typeof figureDecisionSchema>
export type FigureStatus = FigureDecision | 'review'

const plotSource = z.object({ page: z.number().int().positive(), box: boxSchema.nullable() })

export const plotRunSchema = z.object({
  sha256: z.string(),
  source: plotSource,
  settings: z.object({
    figure: z.string(), panel: z.string(), series: z.string(),
    x_axis: z.string(), y_axis: z.string(),
    strategy: z.enum(['vector-line', 'vector-scatter', 'raster-line']),
  }),
  points: z.array(z.object({ index: z.number().int().positive(), x: z.number(), y: z.number(), source: plotSource })),
  issues: z.array(z.object({
    message: z.string(), code: z.string().optional(), sample_x: z.number().optional(), source: plotSource,
  })),
})

export type PlotRun = z.infer<typeof plotRunSchema>
