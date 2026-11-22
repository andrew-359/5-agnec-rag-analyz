import { randomUUID } from 'node:crypto'
import { existsSync, renameSync } from 'node:fs'
import { join } from 'node:path'
import type { DatabaseSync } from 'node:sqlite'
import type { ArticlePublicationStatus } from '../../shared/article.ts'
import type { PublishedVersion } from '../../shared/publication.ts'

const releaseLease = 12_000

export function openArticleReleaseStore(
  db: DatabaseSync,
  artifactFolder: (runId: string, stageId: string, attemptId: string | null) => string,
  reconciliationPath: (runId: string) => string,
) {
  const addRelease = db.prepare("INSERT INTO article_run_publications (run_id, status, publish) VALUES (?, 'queued', ?)")
  const releaseStatus = db.prepare('SELECT status, version, error, publish FROM article_run_publications WHERE run_id = ?')
  const claimRelease = db.prepare(`UPDATE article_run_publications SET status = 'running', available_at = ?, token = ?
    WHERE run_id = (
      SELECT release.run_id FROM article_run_publications release JOIN article_runs run ON run.id = release.run_id
      WHERE release.status IN ('queued', 'running') AND release.available_at <= ?
        AND NOT EXISTS (
          SELECT 1 FROM article_run_stages stage
          WHERE stage.run_id = release.run_id AND stage.status IN ('queued', 'running')
        )
      ORDER BY run.created_at, run.rowid LIMIT 1
    ) RETURNING run_id AS runId, publish`)
  const ownsRelease = db.prepare(`SELECT 1 FROM article_run_publications
    WHERE run_id = ? AND status = 'running' AND token = ? AND available_at > ?`)
  const renewRelease = db.prepare(`UPDATE article_run_publications SET available_at = ?
    WHERE run_id = ? AND status = 'running' AND token = ? AND available_at > ?`)
  const finishRelease = db.prepare(`UPDATE article_run_publications SET status = ?, version = ?, error = ?, token = NULL
    WHERE run_id = ? AND status = 'running' AND token = ? AND available_at > ?`)
  const retryRelease = db.prepare(`UPDATE article_run_publications SET
    status = CASE WHEN failure_count < 2 THEN 'queued' ELSE 'failed' END,
    available_at = ?, token = NULL, failure_count = failure_count + 1, error = ?
    WHERE run_id = ? AND status = 'running' AND token = ? AND available_at > ?`)
  const resumeRelease = db.prepare(`UPDATE article_run_publications SET
    status = 'queued', available_at = 0, token = NULL, failure_count = 0, error = NULL
    WHERE run_id = ? AND status = 'failed' AND publish = 1`)
  const applyRelease = db.prepare(`UPDATE article_run_publications SET
    status = 'queued', publish = 1, available_at = 0, token = NULL, failure_count = 0, error = NULL
    WHERE run_id = ? AND (publish = 0 OR (publish = 1 AND status = 'failed'))`)
  const releaseStages = db.prepare(`SELECT id, kind, attempt_id AS attemptId FROM article_run_stages
    WHERE run_id = ? AND status = 'done' AND kind IN ('table', 'figure')
    ORDER BY page, table_number, id`)

  return {
    enqueueRelease(runId: string, publish: boolean) {
      addRelease.run(runId, Number(publish))
    },
    readRelease(runId: string) {
      const saved = releaseStatus.get(runId) as {
        status: ArticlePublicationStatus
        version: string | null
        error: string | null
        publish: number
      } | undefined
      if (!saved) return undefined

      const savedReport = existsSync(reconciliationPath(runId))
      const reconciliationStatus: ArticlePublicationStatus | undefined = savedReport ? 'done' : saved.status === 'done' || saved.status === 'empty'
        ? undefined : saved.status
      if (!saved.publish) return { reconciliationStatus,
        ...(saved.status === 'failed' && !savedReport ? { reconciliationError: saved.error } : {}),
      }
      return {
        reconciliationStatus,
        publicationStatus: saved.status,
        publication: saved.version ? JSON.parse(saved.version) as PublishedVersion : null,
        publicationError: saved.status === 'failed' ? saved.error : null,
        publicationNote: saved.status === 'empty' ? saved.error : null,
      }
    },
    claimRelease() {
      const now = Date.now()
      const token = randomUUID()
      const release = claimRelease.get(now + releaseLease, token, now) as { runId: string; publish: number } | undefined
      return release ? { runId: release.runId, publish: Boolean(release.publish), token } : null
    },
    renewRelease(runId: string, token: string) {
      const now = Date.now()
      return renewRelease.run(now + releaseLease, runId, token, now).changes !== 0
    },
    saveReleaseArtifact(runId: string, token: string, temporary: string, destination: string) {
      db.exec('BEGIN IMMEDIATE')
      try {
        if (!ownsRelease.get(runId, token, Date.now())) throw new Error('Истёк срок выполнения публикации.')
        renameSync(temporary, destination)
        db.exec('COMMIT')
      } catch (error) {
        db.exec('ROLLBACK')
        throw error
      }
    },
    finishRelease(runId: string, token: string, version: PublishedVersion | null, diagnostic?: string, reconciledOnly = false) {
      const status = version || reconciledOnly ? 'done' : 'empty'
      finishRelease.run(status, version && JSON.stringify(version), diagnostic ?? null, runId, token, Date.now())
    },
    retryRelease(runId: string, token: string, error: string) {
      const now = Date.now()
      retryRelease.run(now + 3_000, error, runId, token, now)
    },
    resumePublication(runId: string) {
      return existsSync(reconciliationPath(runId)) && resumeRelease.run(runId).changes !== 0
    },
    applyPublication(runId: string) {
      if (!existsSync(reconciliationPath(runId))) return false
      if (applyRelease.run(runId).changes !== 0) return true
      const current = releaseStatus.get(runId)
      return Boolean(current?.publish && ['queued', 'running', 'done'].includes(String(current.status)))
    },
    releaseArtifacts(runId: string) {
      const rows = releaseStages.all(runId) as { id: string; kind: 'table' | 'figure'; attemptId: string | null }[]
      return rows.map(({ id, kind, attemptId }) => ({
        id, kind, path: join(artifactFolder(runId, id, attemptId), kind === 'table' ? 'run.json' : 'extracted.json'),
      }))
    },
  }
}

export type ArticleReleaseWork = Omit<ReturnType<typeof openArticleReleaseStore>, 'enqueueRelease' | 'readRelease'>
