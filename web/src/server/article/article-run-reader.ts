import type { DatabaseSync } from 'node:sqlite'
import type { ArticleCoverage, ArticleCoveragePlan, ArticleReadStage, ArticleRun, ArticleRunAttempt, ArticleRunStage, FigureRunStage, TableTask } from '../../shared/article.ts'
import { tableRegionSchema } from '../../shared/article.ts'
import type { openArticleReleaseStore } from './article-release-store.ts'

const stageColumns = `stage.id, stage.kind, stage.candidate_id AS candidateId, stage.page,
  stage.table_number AS "table", stage.table_region AS tableRegion, stage.strategy, stage.status, stage.result_count AS resultCount,
  stage.remark_count AS remarkCount, stage.ambiguous_binding_count AS ambiguousBindingCount,
  stage.empty_result_reason AS emptyResultReason,
  CASE WHEN stage.status = 'failed' AND stage.generation >= stage.attempt_limit
    THEN 'Исчерпан лимит попыток (' || stage.attempt_limit || '). ' || stage.error
    ELSE stage.error END AS error`
const taskColumns = `${stageColumns}, run.id AS runId, run.article_id AS articleId, run.document`
type PersistedRun = Pick<ArticleRun, 'id' | 'articleId' | 'document' | 'pdfSha256'> & { coveragePlan: string | null }
type PersistedStage = Omit<ArticleRunStage, 'attempts' | 'gridRead' | 'kind' | 'strategyPlan'> & {
  tableRegion: string | null
  kind: ArticleRunStage['kind'] | ArticleReadStage['kind'] | 'figure'
  figureLabel: string | null
  figurePlan: string | null
  gridStageId: string | null
  plannedStrategy: string | null
  strategyReason: string | null
  fallbackStrategy: string | null
  fallbackReason: string | null
  attemptHistory: string
}
type PersistedTask = Omit<TableTask, 'region'> & { tableRegion: string | null }

function readRegion(saved: string | null) {
  return saved === null ? undefined : tableRegionSchema.parse(JSON.parse(saved))
}

function readTasks(rows: unknown[]) {
  return (rows as PersistedTask[]).map(({ tableRegion, ...task }) => ({ ...task, region: readRegion(tableRegion) }))
}

function summarizeCoverage(plan: ArticleCoveragePlan, stages: ArticleRunStage[]): ArticleCoverage {
  const coverage = {
    ...plan,
    deferredFigures: plan.deferredFigures ?? [],
    detectedCandidates: stages.length + plan.excludedCandidates.length,
    plannedStages: stages.length,
    completedStages: 0,
    readTables: 0,
    failedStages: 0,
    confirmedResults: 0,
    ambiguousBindings: 0,
    otherRemarks: 0,
  }

  for (const stage of stages) {
    // Modern runs record grid reading separately. Legacy runs only record the table stage.
    if ((stage.gridRead?.status ?? stage.status) === 'done') coverage.readTables++
    if (stage.status === 'failed') coverage.failedStages++
    if (stage.status !== 'done') continue

    const ambiguous = stage.ambiguousBindingCount ?? 0
    coverage.completedStages++
    coverage.confirmedResults += stage.resultCount ?? 0
    coverage.ambiguousBindings += ambiguous
    coverage.otherRemarks += (stage.remarkCount ?? 0) - ambiguous
  }
  return coverage
}

export function openArticleRunReader(
  db: DatabaseSync,
  artifactFolder: (runId: string, stageId: string, attemptId: string | null) => string,
  readRelease: ReturnType<typeof openArticleReleaseStore>['readRelease'],
) {
  const byRun = db.prepare('SELECT id, article_id AS articleId, document, pdf_sha256 AS pdfSha256, coverage_plan AS coveragePlan FROM article_runs WHERE id = ?')
  const stagesByRun = db.prepare(`SELECT ${stageColumns}, stage.grid_stage_id AS gridStageId, stage.figure_label AS figureLabel,
    stage.figure_plan AS figurePlan,
    stage.planned_strategy AS plannedStrategy, stage.strategy_reason AS strategyReason,
    COALESCE(grid.fallback_strategy, stage.fallback_strategy) AS fallbackStrategy,
    COALESCE(grid.fallback_reason, stage.fallback_reason) AS fallbackReason, (
    SELECT json_group_array(json_object('number', number, 'status', status,
      'startedAt', started_at, 'finishedAt', finished_at, 'error', error, 'strategy', strategy, 'reason', reason,
      'outcome', outcome,
      'hasGridArtifact', json(CASE WHEN has_grid_artifact THEN 'true' ELSE 'false' END)) ORDER BY number)
    FROM article_run_attempts WHERE stage_id = stage.id
  ) AS attemptHistory FROM article_run_stages stage
    LEFT JOIN article_run_stages grid ON grid.id = stage.grid_stage_id AND grid.run_id = stage.run_id
    WHERE stage.run_id = ? ORDER BY stage.page, stage.table_number`)
  const completedStage = db.prepare(`SELECT run.document, stage.kind, stage.attempt_id AS attemptId FROM article_run_stages stage
    JOIN article_runs run ON run.id = stage.run_id WHERE run.id = ? AND stage.id = ? AND stage.status = 'done'`)
  const savedGridAttempt = db.prepare(`SELECT attempt.id FROM article_run_attempts attempt
    JOIN article_run_stages stage ON stage.id = attempt.stage_id
    WHERE stage.run_id = ? AND stage.id = ? AND stage.kind = 'table-grid' AND attempt.number = ? AND attempt.has_grid_artifact = 1`)

  const recentTasks = db.prepare(`SELECT ${taskColumns} FROM article_run_stages stage
    JOIN article_runs run ON run.id = stage.run_id
    WHERE stage.kind = 'table' AND (stage.status IN ('queued', 'running') OR stage.id IN (
      SELECT s.id FROM article_run_stages s JOIN article_runs r ON r.id = s.run_id
      WHERE s.kind = 'table' AND s.status IN ('done', 'failed') ORDER BY r.created_at DESC LIMIT 8
    )) ORDER BY run.created_at DESC`)
  const articleTasks = db.prepare(`SELECT ${taskColumns} FROM article_run_stages stage
    JOIN article_runs run ON run.id = stage.run_id WHERE run.article_id = ? AND stage.kind = 'table' ORDER BY run.created_at DESC, run.rowid DESC`)

  return {
    get(id: string): ArticleRun | null {
      const saved = byRun.get(id) as PersistedRun | undefined
      if (!saved) return null
      const { coveragePlan, ...run } = saved
      const rows = stagesByRun.all(id) as unknown as PersistedStage[]
      const reads = new Map<string, ArticleReadStage>()
      for (const { id, error, kind, status, strategy, strategyReason, attemptHistory, tableRegion, emptyResultReason } of rows) {
        if (kind !== 'article-text' && kind !== 'table-grid') continue
        const attempts = JSON.parse(attemptHistory) as ArticleRunAttempt[]
        const savedGrid = kind === 'table-grid' && status === 'done'
          ? attempts.findLast(attempt => attempt.status === 'succeeded' && attempt.hasGridArtifact)
          : undefined
        reads.set(id, {
          id, kind, status, strategy: savedGrid?.strategy ?? strategy, error, region: readRegion(tableRegion), attempts,
          ...(strategyReason ? { strategyReason } : {}),
          ...(emptyResultReason ? { stopReason: emptyResultReason } : {}),
        })
      }
      const articleText = [...reads.values()].find(stage => stage.kind === 'article-text') ?? null
      const stages: ArticleRunStage[] = []
      const figures: FigureRunStage[] = []
      for (const { attemptHistory, gridStageId, figureLabel, figurePlan, kind, plannedStrategy, strategyReason, fallbackStrategy, fallbackReason, tableRegion, ...stage } of rows) {
        if (kind === 'table') stages.push({
          ...stage, kind, region: readRegion(tableRegion), gridRead: gridStageId ? reads.get(gridStageId) ?? null : null,
          strategyPlan: plannedStrategy && strategyReason ? {
            strategy: plannedStrategy, reason: strategyReason,
            fallback: fallbackStrategy && fallbackReason ? { strategy: fallbackStrategy, reason: fallbackReason } : null,
          } : undefined,
          attempts: JSON.parse(attemptHistory) as ArticleRunAttempt[],
        })
        if (kind === 'figure') {
          const { id, candidateId, page, status, resultCount, remarkCount, error } = stage
          figures.push({ id, candidateId, page, label: figureLabel, status, resultCount, remarkCount, error,
            ...(figurePlan ? { plan: JSON.parse(figurePlan) } : {}),
            attempts: JSON.parse(attemptHistory) as ArticleRunAttempt[] })
        }
      }
      const plan = coveragePlan ? JSON.parse(coveragePlan) as ArticleCoveragePlan : null
      const coverage = plan && summarizeCoverage(plan, stages)
      return { ...run, stages, figures, articleText, coverage, ...readRelease(id) }
    },
    result(runId: string, stageId: string) {
      const stage = completedStage.get(runId, stageId) as { document: string; kind: PersistedStage['kind']; attemptId: string | null } | undefined
      if (!stage) return null
      return { document: stage.document, kind: stage.kind, folder: artifactFolder(runId, stageId, stage.attemptId) }
    },
    gridAttempt(runId: string, stageId: string, number: number) {
      const attempt = savedGridAttempt.get(runId, stageId, number) as { id: string } | undefined
      return attempt ? artifactFolder(runId, stageId, attempt.id) : null
    },
    recentTasks: () => readTasks(recentTasks.all()),
    articleTasks: (articleId: string) => readTasks(articleTasks.all(articleId)),
  }
}
