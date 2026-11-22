import { randomUUID } from 'node:crypto';
import { mkdirSync } from 'node:fs';
import { join } from 'node:path';
import { DatabaseSync } from 'node:sqlite';
import type { ReviewChatUsage, ReviewUsage } from '../shared/review-usage.ts';

export const reviewTimeoutMs = 90_000;
type Chat = { route: string; artifactId: string; chatId: string };

function sumUsage(calls: ReviewUsage[]): ReviewUsage {
  let tokens: number | null = null;
  for (const call of calls) {
    if (call.tokens !== null) tokens = (tokens ?? 0) + call.tokens;
  }
  return { tokens, complete: calls.length > 0 && calls.every(call => call.complete) };
}

export function openReviewUsage(directory: string) {
  mkdirSync(directory, { recursive: true, mode: 0o700 });
  const db = new DatabaseSync(join(directory, 'review-usage.sqlite'));
  db.exec(`
    PRAGMA busy_timeout = 5000;
    PRAGMA journal_mode = WAL;
    CREATE TABLE IF NOT EXISTS review_answers (
      id TEXT PRIMARY KEY,
      route TEXT NOT NULL,
      artifact_id TEXT NOT NULL,
      chat_id TEXT NOT NULL,
      label TEXT NOT NULL,
      tokens INTEGER,
      complete INTEGER NOT NULL DEFAULT 0,
      pending_until INTEGER NOT NULL
    );
    CREATE INDEX IF NOT EXISTS review_answers_chat ON review_answers (route, artifact_id, chat_id);
  `);
  const insert = db.prepare(`INSERT INTO review_answers (id, route, artifact_id, chat_id, label, pending_until)
    VALUES (?, ?, ?, ?, ?, ?)`);
  const update = db.prepare('UPDATE review_answers SET tokens = ?, complete = ? WHERE id = ?');
  const end = db.prepare('UPDATE review_answers SET pending_until = 0 WHERE id = ?');
  const list = db.prepare(`SELECT id, label, tokens, complete, pending_until FROM review_answers
    WHERE route = ? AND artifact_id = ? AND chat_id = ? ORDER BY rowid`);

  return {
    begin(chat: Chat, label: string) {
      const id = randomUUID();
      insert.run(id, chat.route, chat.artifactId, chat.chatId, label, Date.now() + reviewTimeoutMs + 5_000);
      const calls: ReviewUsage[] = [];
      return {
        id,
        startCall() {
          calls.push({ tokens: null, complete: false });
          const { tokens, complete } = sumUsage(calls);
          update.run(tokens, Number(complete), id);
        },
        recordCall(usage: ReviewUsage) {
          calls[calls.length - 1] = usage;
          const { tokens, complete } = sumUsage(calls);
          update.run(tokens, Number(complete), id);
        },
        finish() { end.run(id); },
      };
    },
    read(chat: Chat): ReviewChatUsage {
      const now = Date.now();
      const rows = list.all(chat.route, chat.artifactId, chat.chatId) as Array<{
        id: string; label: string; tokens: number | null; complete: number; pending_until: number;
      }>;
      const answers = rows.map(row => ({ id: row.id, label: row.label, tokens: row.tokens,
        complete: Boolean(row.complete), pending: row.pending_until > now }));
      return { answers, total: sumUsage(answers) };
    },
  };
}
