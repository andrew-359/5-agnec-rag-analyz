import type { z } from 'zod'
import type { createArticlePythonRunner } from './article-python.ts'
import { figureReaderCatalogSchema } from '../../shared/strategy-catalog.ts'

// Python owns the catalogue and parameter rules. Cache successful loads only.
export function figureReaderCatalog(python: ReturnType<typeof createArticlePythonRunner>) {
  let cached: Promise<z.infer<typeof figureReaderCatalogSchema>> | undefined
  return () => cached ??= python(['-m', 'rag_mvp.figure_readers'], 30_000)
    .then(output => figureReaderCatalogSchema.parse(JSON.parse(output.toString('utf8'))))
    .catch(error => { cached = undefined; throw error })
}
