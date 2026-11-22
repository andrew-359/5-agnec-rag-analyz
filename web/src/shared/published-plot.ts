import { z } from 'zod'
import { resultSchema, sourceSchema } from './saved-run.ts'
import { plotRunSchema } from './article-plot.ts'

export const publishedPlotSchema = z.object({
  kind: z.literal('published-plot'),
  version_id: z.string().regex(/^plot-[a-f0-9]{20}$/),
  publication_id: z.string().min(1),
  pdf: z.object({ sha256: z.string().regex(/^[a-f0-9]{64}$/), url: z.string() }),
  results: z.array(resultSchema.extend({
    id: z.string(), approximate: z.literal(true), absolute_tolerance: z.number().positive(),
    parameter: z.object({ name: z.string(), value: z.number() }),
    source: sourceSchema.extend({ table: z.null(), figure: z.string() }),
  })).min(1),
  coverage: z.object({ figure: z.string(), panel: z.string(), point_indices: z.array(z.number().int().positive()).min(1), whole_publication: z.literal(false) }),
})

export const publishedPlotDetailSchema = z.object({
  run: publishedPlotSchema,
  plot: plotRunSchema,
  check: z.object({ errors: z.array(z.string()), warnings: z.array(z.string()), exit_code: z.number().int() }),
})
export const publishedPlotsSchema = z.object({ versions: z.array(publishedPlotSchema), unavailable: z.array(z.string()) })
export type PublishedPlot = z.infer<typeof publishedPlotSchema>
export type PublishedPlotDetail = z.infer<typeof publishedPlotDetailSchema>
