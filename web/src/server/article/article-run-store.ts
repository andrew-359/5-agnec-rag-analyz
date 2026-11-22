import { randomUUID } from 'node:crypto'
import { join } from 'node:path'
import type { FigureDecision } from '../../shared/article-plot.ts'
import type { ArticleCoveragePlan, ArticleRun, ArticleTableTarget, FigureTarget } from '../../shared/article.ts'
import { openArticleReleaseStore } from './article-release-store.ts'
import { openArticleRunDatabase } from './article-run-database.ts'
import { openArticleRunReader } from './article-run-reader.ts'
import { reconciliationPath } from './article-publication-artifacts.ts'
import { openArticleStageDelivery } from './article-stage-delivery-store.ts'

export function openArticleRuns(store: string, articleContext = false) {
  const db = openArticleRunDatabase(store)
  const delivery = openArticleStageDelivery(db)

  const addRun = db.prepare(`INSERT INTO article_runs
    (id, article_id, document, pdf_sha256, created_at, coverage_plan, article_context) VALUES (?, ?, ?, ?, ?, ?, ?)`)
  const addStage = db.prepare(`INSERT INTO article_run_stages
    (id, run_id, candidate_id, page, table_number, strategy, kind, text_stage_id, grid_stage_id,
     fallback_strategy, fallback_reason, planned_strategy, strategy_reason, table_region, status)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued')`)
  const addFigure = db.prepare(`INSERT INTO article_run_stages
    (id, run_id, candidate_id, page, table_number, strategy, kind, figure_label, text_stage_id, figure_plan, status)
    VALUES (?, ?, ?, ?, 0, ?, 'figure', ?, ?, ?, 'queued')`)
  const artifactFolder = (runId: string, stageId: string, attemptId: string | null) => {
    const stageFolder = join(store, 'runs', runId, stageId)
    return attemptId ? join(stageFolder, 'attempts', attemptId) : stageFolder
  }
  const { enqueueRelease, readRelease, ...publicationWork } = openArticleReleaseStore(db, artifactFolder,
    runId => reconciliationPath(store, runId))
  const reader = openArticleRunReader(db, artifactFolder, readRelease)

  const latestFullRun = db.prepare(`SELECT id FROM article_runs
    WHERE article_id = ? AND coverage_plan IS NOT NULL ORDER BY created_at DESC, rowid DESC LIMIT 1`)
  const listFigureDecisions = db.prepare('SELECT candidate_id AS candidateId, decision FROM article_figure_decisions WHERE article_id = ?')
  const saveFigureDecision = db.prepare(`INSERT INTO article_figure_decisions (article_id, candidate_id, decision)
    VALUES (?, ?, ?) ON CONFLICT(article_id, candidate_id) DO UPDATE SET decision = excluded.decision`)

  return {
    latestFullRunId(articleId: string) {
      return (latestFullRun.get(articleId) as { id: string } | undefined)?.id
    },
    figureDecisions(articleId: string) {
      const rows = listFigureDecisions.all(articleId) as Array<{ candidateId: string; decision: FigureDecision }>
      return new Map(rows.map(({ candidateId, decision }) => [candidateId, decision]))
    },
    setFigureDecision(articleId: string, candidateId: string, decision: FigureDecision) {
      saveFigureDecision.run(articleId, candidateId, decision)
    },
    create(
      article: Pick<ArticleRun, 'articleId' | 'document' | 'pdfSha256'>,
      stages: ArticleTableTarget[],
      coveragePlan: ArticleCoveragePlan | null = null,
      options: {
        figures?: FigureTarget[]; publish?: boolean; reconcile?: boolean; withText?: boolean
        textStrategy?: string; textStrategyReason?: string
      } = {},
    ) {
      const id = randomUUID()
      const { figures: figureTargets = [], publish = false, reconcile = publish, withText = articleContext, textStrategy = 'pdfplumber' } = options
      db.exec('BEGIN IMMEDIATE')
      try {
        addRun.run(id, article.articleId, article.document, article.pdfSha256, Date.now(),
          coveragePlan && JSON.stringify(coveragePlan), Number(withText))
        if (reconcile || publish) enqueueRelease(id, publish)
        const textStageId = withText ? randomUUID() : null
        if (textStageId) addStage.run(textStageId, id, 'article-text', 0, 0, textStrategy, 'article-text', null, null,
          null, null, null, options.textStrategyReason ?? null, null)
        for (const { strategy, strategyReason, candidateId, page, table, fallback, region } of stages) {
          const gridStageId = textStageId ? randomUUID() : null
          const tableRegion = region ? JSON.stringify(region) : null
          if (gridStageId) addStage.run(gridStageId, id, candidateId, page, table, strategy, 'table-grid', null, null,
            fallback?.strategy ?? null, fallback?.reason ?? null, null, null, tableRegion)
          const directFallback = gridStageId ? undefined : fallback
          addStage.run(randomUUID(), id, candidateId, page, table, strategy, 'table', textStageId, gridStageId,
            directFallback?.strategy ?? null, directFallback?.reason ?? null, strategy, strategyReason ?? null, tableRegion)
        }
        for (const { candidateId, page, label, plan } of figureTargets) {
          const strategy = plan?.selection.mode === 'explicit' ? plan.selection.strategy_id : 'figure-auto'
          addFigure.run(randomUUID(), id, candidateId, page, strategy, label, textStageId, plan ? JSON.stringify(plan) : null)
        }
        db.exec('COMMIT')
        return { id }
      } catch (error) {
        db.exec('ROLLBACK')
        throw error
      }
    },
    ...reader,
    ...delivery,
    ...publicationWork,
  }
}
