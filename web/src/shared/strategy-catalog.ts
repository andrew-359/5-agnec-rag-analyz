import { z } from 'zod'

export const strategyCatalogRoute = '/strategies'

const reader = z.object({
  id: z.string().min(1),
  description: z.string(),
})

export const articleReaderCatalogSchema = z.object({
  text: z.array(reader),
  table: z.array(reader.extend({
    requires_crop: z.boolean(),
    accepts_region: z.boolean(),
  })),
})

const figureReader = reader.extend({
  stage: z.literal('figure'),
  input: z.literal('figure-region'),
  parameters_schema: z.record(z.string(), z.unknown()),
}).passthrough()

export const figureReaderCatalogSchema = z.object({
  readers: z.array(figureReader),
})

export const strategyCatalogSchema = articleReaderCatalogSchema.extend({
  figure: z.array(figureReader),
})
