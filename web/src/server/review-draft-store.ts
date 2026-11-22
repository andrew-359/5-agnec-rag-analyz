import { randomUUID } from 'node:crypto'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'
import { DatabaseSync } from 'node:sqlite'
import type { DraftCase, StoredDraft } from '../shared/review-draft.ts'

export class DraftConflict extends Error {}
export class DraftMissing extends Error {}
const caseKey = (value: DraftCase & { field?: string }) => JSON.stringify([
  value.publicationId, value.versionId, value.resultId ?? null, value.remarkId ?? null, value.field ?? null,
])

/** Every write is one transaction; no publication files are opened by this module. */
export function createReviewDraftStore(directory: string) {
  function database<T>(action: (db: DatabaseSync) => T, write = false): T {
    mkdirSync(directory, { recursive: true, mode: 0o700 })
    const db = new DatabaseSync(join(directory, 'review-drafts.sqlite'))
    try {
      db.exec(`PRAGMA busy_timeout = 5000; PRAGMA journal_mode = WAL;
        CREATE TABLE IF NOT EXISTS review_drafts (
          id TEXT PRIMARY KEY, case_key TEXT NOT NULL UNIQUE,
          publication_id TEXT NOT NULL, version_id TEXT NOT NULL,
          result_id TEXT, remark_id TEXT, payload TEXT NOT NULL, revision INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS review_draft_requests (
          request_id TEXT PRIMARY KEY, payload_hash TEXT NOT NULL, draft_id TEXT NOT NULL
        );`)
      if (write) db.exec('BEGIN IMMEDIATE')
      try {
        const result = action(db)
        if (write) db.exec('COMMIT')
        return result
      } catch (error) {
        if (write) db.exec('ROLLBACK')
        throw error
      }
    } finally { db.close() }
  }
  const decode = (row: unknown) => row ? JSON.parse((row as { payload: string }).payload) as StoredDraft : null
  function get(db: DatabaseSync, id: string) {
    return decode(db.prepare('SELECT payload FROM review_drafts WHERE id = ?').get(id))
  }
  return {
    get(id: string) { return database(db => get(db, id)) },
    list(address: DraftCase) {
      return database(db => db.prepare(`SELECT payload FROM review_drafts WHERE publication_id = ? AND version_id = ?
        AND result_id IS ? AND remark_id IS ? ORDER BY rowid DESC`)
        .all(address.publicationId, address.versionId, address.resultId ?? null, address.remarkId ?? null)
        .map(row => decode(row)!))
    },
    create(input: Omit<StoredDraft, 'id' | 'revision' | 'createdAt' | 'updatedAt'>, requestId: string, payloadHash: string) {
      return database(db => {
        const replay = db.prepare('SELECT payload_hash AS hash, draft_id AS id FROM review_draft_requests WHERE request_id = ?')
          .get(requestId) as { hash: string; id: string } | undefined
        if (replay) {
          if (replay.hash !== payloadHash) throw new DraftConflict('Этот запрос уже использован для другого предложения.')
          const draft = get(db, replay.id)
          if (!draft) throw new DraftConflict('Этот черновик уже удалён. Обновите список.')
          return { draft, replay: true }
        }
        if (db.prepare('SELECT id FROM review_drafts WHERE case_key = ?').get(caseKey(input))) {
          throw new DraftConflict('Черновик этого поля уже существует. Перечитайте и измените его.')
        }
        const now = new Date().toISOString()
        const draft: StoredDraft = { ...input, id: randomUUID(), revision: 1, createdAt: now, updatedAt: now }
        db.prepare(`INSERT INTO review_drafts (id, case_key, publication_id, version_id, result_id, remark_id, payload, revision)
          VALUES (?, ?, ?, ?, ?, ?, ?, ?)`).run(draft.id, caseKey(draft), draft.publicationId, draft.versionId,
          draft.resultId ?? null, draft.remarkId ?? null, JSON.stringify(draft), 1)
        db.prepare('INSERT INTO review_draft_requests VALUES (?, ?, ?)').run(requestId, payloadHash, draft.id)
        return { draft, replay: false }
      }, true)
    },
    update(id: string, expectedRevision: number, update: Pick<StoredDraft, 'disposition' | 'proposedValue' | 'evidenceIds' | 'reason'>) {
      return database(db => {
        const current = get(db, id)
        if (!current) throw new DraftMissing('Черновик не найден или уже удалён.')
        if (current.revision !== expectedRevision) throw new DraftConflict('Черновик уже изменён в другом окне. Перечитайте сохранённую версию.')
        const next = { ...current, ...update, revision: current.revision + 1, updatedAt: new Date().toISOString() }
        db.prepare('UPDATE review_drafts SET payload = ?, revision = ? WHERE id = ?').run(JSON.stringify(next), next.revision, id)
        return next
      }, true)
    },
    delete(id: string, expectedRevision: number) {
      return database(db => {
        const current = get(db, id)
        if (!current) throw new DraftMissing('Черновик не найден или уже удалён.')
        if (current.revision !== expectedRevision) throw new DraftConflict('Черновик уже изменён. Перечитайте его перед удалением.')
        db.prepare('DELETE FROM review_drafts WHERE id = ?').run(id)
      }, true)
    },
  }
}
