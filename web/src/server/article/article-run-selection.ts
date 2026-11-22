import { z } from 'zod'
import type { createArticlePythonRunner } from './article-python.ts'
import type { figureReaderCatalog } from './figure-reader-catalog.ts'
import type { FigurePlan } from '../../shared/figure-run.ts'
import { articleReaderCatalogSchema } from '../../shared/strategy-catalog.ts'

export const articleRunRequestSchema = z.object({
  readers: z.object({
    table: z.string().min(1).optional(),
    text: z.string().min(1).optional(),
    figure: z.string().min(1).optional(),
  }).strict().default({}),
  figure_parameters: z.record(z.string(), z.unknown()).default({}),
  publish: z.literal(false).optional(),
}).strict()

export function articleReaderCatalog(python: ReturnType<typeof createArticlePythonRunner>) {
  let cached: Promise<z.infer<typeof articleReaderCatalogSchema>> | undefined
  return () => cached ??= python(['-m', 'rag_mvp.reader_catalog', '--details'], 30_000)
    .then(output => articleReaderCatalogSchema.parse(JSON.parse(output.toString('utf8'))))
    .catch(error => { cached = undefined; throw error })
}

export async function figureSelectionError(
  catalog: ReturnType<typeof figureReaderCatalog>,
  selection: FigurePlan['selection'],
) {
  if (selection.mode === 'auto') return
  const reader = (await catalog()).readers.find(item => item.id === selection.strategy_id)
  if (!reader) return { error: `Неизвестный ридер: ${selection.strategy_id}` }

  const parsed = z.fromJSONSchema(reader.parameters_schema).safeParse(selection.parameters)
  if (!parsed.success) return { error: 'Некорректные параметры ридера.', details: parsed.error.issues }
}
