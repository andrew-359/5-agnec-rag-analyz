import { z } from 'zod'
import type { FigureStatus } from './article-plot.ts'
import { boxSchema, idSchema } from './schema.ts'
import type { PublishedVersion } from './publication.ts'

export const articleRunReceiptSchema = z.object({ id: idSchema })

const pageNumber = z.number().int().positive()

const overviewExcerpt = z.object({ text: z.string().min(1), page: pageNumber, box: boxSchema.nullable() })
export const articleOverviewSchema = z.object({
  title: overviewExcerpt.nullable(),
  topic: z.string().nullable().transform(topic => topic === 'Генерация с поиском (RAG)' ? null : topic),
  abstract: overviewExcerpt.nullable(),
  conclusion: overviewExcerpt.nullable(),
  has_text: z.boolean(),
})
export type ArticleOverview = z.infer<typeof articleOverviewSchema>
export type OverviewExcerpt = z.infer<typeof overviewExcerpt>
export const overviewTranslationSchema = z.object({
  abstract: z.string().min(1).nullable(),
  conclusion: z.string().min(1).nullable(),
})
export type ArticleOverviewStatus = { runId: string; label: string; resultRunId?: string | null } | null

export const tableRegionSchema = z.object({
  box: boxSchema,
  bounds_status: z.enum(['confirmed', 'needs_review']),
  caption_status: z.enum(['confirmed', 'missing', 'needs_review']),
  designation: z.string().min(1).nullable(),
  caption_box: boxSchema.nullable(),
  caption_text: z.string().min(1).nullable(),
  alternative_boxes: z.array(boxSchema).optional(),
})
export type TableRegion = z.infer<typeof tableRegionSchema>

const tableReadFeaturesSchema = z.object({
  text_layer: z.enum(['usable', 'ocr-required']),
  header: z.enum(['method-top-k', 'other', 'unconfirmed']),
  header_labels: z.array(z.string()).optional(),
})
export type TableReadFeatures = z.infer<typeof tableReadFeaturesSchema>

export function tableStageTitle(stage: { table: number; region?: TableRegion }) {
  return stage.region ? stage.region.designation ?? 'Область таблицы' : `Таблица ${stage.table}`
}

export const inventorySchema = z.object({
  schema_version: z.literal(1),
  document: z.string(),
  sha256: z.string().regex(/^[0-9a-f]{64}$/),
  page_count: pageNumber,
  overview: articleOverviewSchema.optional(),
  pages: z.array(z.object({
    page: pageNumber,
    text_chars: z.number().int().nonnegative().optional(),
    image_boxes: z.array(boxSchema).default([]),
  })).default([]),
  warnings: z.array(z.object({ page: pageNumber, reason: z.string() })),
  limitations: z.array(z.string()).default([]),
  candidates: z.array(z.object({
    id: z.string(),
    kind: z.enum(['section', 'table', 'figure']),
    page: pageNumber,
    label: z.string().nullable(),
    text: z.string(),
    box: boxSchema.nullable(),
    role: z.string().optional(),
    discovery: z.literal('ocr').optional(),
    region: tableRegionSchema.optional(),
    table_read_features: tableReadFeaturesSchema.optional(),
    extract_arguments: z.array(z.string()).optional(),
  })),
})

export const uploadLimit = 64 * 1024 * 1024

export type ArticleRunAttempt = {
  number: number
  status: 'running' | 'succeeded' | 'failed' | 'interrupted'
  startedAt: number
  finishedAt: number | null
  error: string | null
  strategy: string | null
  reason: string | null
  outcome?: string | null
  hasGridArtifact: boolean
}

export type ArticleReadStage = {
  id: string
  kind: 'article-text' | 'table-grid'
  status: 'queued' | 'running' | 'done' | 'failed'
  strategy: string
  strategyReason?: string
  region?: TableRegion
  error: string | null
  attempts: ArticleRunAttempt[]
  stopReason?: string | null
}

type TableStrategyFallback = { strategy: string; reason: string }

type TableStrategyPlan = {
  strategy: string
  reason: string
  fallback: TableStrategyFallback | null
}

export type ArticleRunStage = {
  kind: 'table'
  gridRead: ArticleReadStage | null
  id: string
  candidateId: string
  page: number
  table: number
  region?: TableRegion
  strategy: string
  strategyPlan?: TableStrategyPlan
  status: 'queued' | 'running' | 'done' | 'failed'
  resultCount: number | null
  remarkCount: number | null
  ambiguousBindingCount: number | null
  emptyResultReason: string | null
  error: string | null
  attempts: ArticleRunAttempt[]
}

export type ExcludedTableCandidate = {
  candidateId: string
  page: number
  label: string | null
  text: string
  reason: 'unsupported-address' | 'duplicate-address' | 'prompt-template' | 'table-bounds-review'
  region?: TableRegion
}

export type DeferredFigure = Omit<ExcludedTableCandidate, 'reason'> & { reason: 'figure-reading-deferred' }

export type ArticleTableTarget = Pick<ArticleRunStage, 'candidateId' | 'page' | 'table' | 'strategy' | 'region'> & {
  strategyReason?: string
  fallback?: TableStrategyFallback
}

export type ArticleCoveragePlan = {
  excludedCandidates: ExcludedTableCandidate[]
  deferredFigures?: DeferredFigure[]
  discoveryWarnings: z.infer<typeof inventorySchema>['warnings']
  discoveryLimitations: string[]
}

export type ArticleCoverage = ArticleCoveragePlan & {
  deferredFigures: DeferredFigure[]
  detectedCandidates: number
  plannedStages: number
  completedStages: number
  readTables: number
  failedStages: number
  confirmedResults: number
  ambiguousBindings: number
  otherRemarks: number
}

export type ArticlePublicationStatus = 'queued' | 'running' | 'done' | 'empty' | 'failed'

export type FigureTarget = { candidateId: string; page: number; label: string | null; plan?: import('./figure-run.ts').FigurePlan }
export type FigureRunStage = FigureTarget & Pick<ArticleRunStage,
  'id' | 'status' | 'resultCount' | 'remarkCount' | 'error' | 'attempts'> & {
    remarks?: Array<{ reason: string; kind: string }>
}

export type ArticleRun = {
  id: string
  articleId: string
  document: string
  pdfSha256: string
  stages: ArticleRunStage[]
  figures: FigureRunStage[]
  articleText: ArticleReadStage | null
  coverage: ArticleCoverage | null
  publication?: PublishedVersion | null
  publicationStatus?: ArticlePublicationStatus
  reconciliationStatus?: ArticlePublicationStatus
  reconciliationError?: string | null
  publicationError?: string | null
  publicationNote?: string | null
}

export type TableTask = Omit<ArticleRunStage, 'attempts' | 'gridRead'> & { runId: string; articleId: string; document: string }

export type Candidate = z.infer<typeof inventorySchema>['candidates'][number]
export type Article = {
  sha256?: string
  id: string
  document: string
  page_count: number
  overview?: ArticleOverview
  overviewTranslationAvailable?: boolean
  rasterPlot?: { page: number; figure: string }
  warnings: z.infer<typeof inventorySchema>['warnings']
  candidates: Array<Omit<Candidate, 'extract_arguments'> & {
    extractable: boolean
    figureStatus?: FigureStatus
    strategyPlan?: TableStrategyPlan
    resultCount: number | null
    task: TableTask | null
  }>
}

export type LibraryArticle = {
  id: string
  document: string
  title: string
  pageCount: number
  uploadedAt: number
  status: string
  runId: string | null
  resultRunId?: string | null
  resultUrl: string | null
}
export type ArticleLibraryResponse = { articles: LibraryArticle[]; unavailable: number }
