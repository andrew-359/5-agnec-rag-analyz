import type { DatabaseSync } from 'node:sqlite'

export type StagePublication = { id: string; generation: number }
const interruption = 'Исполнитель перестал подтверждать выполнение этапа.'

export function openArticleStageDelivery(db: DatabaseSync) {
  const pendingPublication = db.prepare(`SELECT id, generation FROM article_run_stages
    WHERE status = 'queued' AND published_at IS NULL AND available_at <= ?
      AND NOT EXISTS (
        SELECT 1 FROM article_run_stages dep
        WHERE dep.id IN (article_run_stages.text_stage_id, article_run_stages.grid_stage_id)
          AND dep.status IN ('queued', 'running')
      )
    ORDER BY available_at, rowid LIMIT 32`)
  const recordPublication = db.prepare('UPDATE article_run_stages SET published_at = ? WHERE id = ? AND generation = ?')
  const hasExpired = db.prepare("SELECT 1 FROM article_run_stages WHERE status = 'running' AND lease_expires_at <= ? LIMIT 1")
  const interruptAttempts = db.prepare(`UPDATE article_run_attempts SET status = 'interrupted', finished_at = ?, error = ?
    WHERE id IN (SELECT attempt_id FROM article_run_stages WHERE status = 'running' AND lease_expires_at <= ?)`)
  const retainInterruptedGrid = db.prepare(`WITH retained AS (
    SELECT stage.id, (
      SELECT id FROM article_run_attempts
      WHERE stage_id = stage.id AND status = 'succeeded' AND has_grid_artifact = 1 ORDER BY number DESC LIMIT 1
    ) AS attemptId
    FROM article_run_stages stage WHERE stage.kind = 'table-grid' AND stage.status = 'running'
      AND stage.lease_expires_at <= ? AND stage.generation >= stage.attempt_limit
  ) UPDATE article_run_stages SET status = 'done', attempt_id = retained.attemptId, lease_expires_at = NULL,
    empty_result_reason = 'Исчерпан лимит попыток (' || attempt_limit || '). Доступна прочитанная часть.'
    FROM retained WHERE article_run_stages.id = retained.id AND retained.attemptId IS NOT NULL`)
  // Recovery owns expired leases. The worker finalizes live attempts and delays retries after extraction errors.
  const resumeStages = db.prepare(`UPDATE article_run_stages SET
    status = CASE WHEN generation < attempt_limit THEN 'queued' ELSE 'failed' END,
    error = CASE WHEN generation < attempt_limit THEN NULL ELSE ? END,
    generation = CASE WHEN generation < attempt_limit THEN generation + 1 ELSE generation END,
    available_at = ?, published_at = NULL, attempt_id = NULL, lease_expires_at = NULL,
    transition_reason = 'Повтор после прерывания выполнения. ' || ?
    WHERE status = 'running' AND lease_expires_at <= ?`)

  return {
    recoverInterrupted() {
      if (!hasExpired.get(Date.now())) return
      db.exec('BEGIN IMMEDIATE')
      try {
        const now = Date.now()
        interruptAttempts.run(now, interruption, now)
        retainInterruptedGrid.run(now)
        resumeStages.run(interruption, now, interruption, now)
        db.exec('COMMIT')
      } catch (error) {
        db.exec('ROLLBACK')
        throw error
      }
    },
    pendingPublication: () => pendingPublication.all(Date.now()) as StagePublication[],
    recordPublication(stage: StagePublication) {
      recordPublication.run(Date.now(), stage.id, stage.generation)
    },
  }
}

export type ArticleStageDeliveryStore = ReturnType<typeof openArticleStageDelivery>
