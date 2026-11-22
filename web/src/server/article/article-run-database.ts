import { mkdirSync } from 'node:fs'
import { join } from 'node:path'
import { DatabaseSync } from 'node:sqlite'

export function openArticleRunDatabase(store: string) {
  mkdirSync(store, { recursive: true, mode: 0o700 })
  const db = new DatabaseSync(join(store, 'article-runs.sqlite'))
  db.exec(`
    PRAGMA busy_timeout = 5000;
    PRAGMA journal_mode = WAL;
    CREATE TABLE IF NOT EXISTS article_runs (
      id TEXT PRIMARY KEY,
      article_id TEXT NOT NULL,
      document TEXT NOT NULL,
      pdf_sha256 TEXT NOT NULL,
      coverage_plan TEXT,
      article_context INTEGER,
      created_at INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS article_run_publications (
      run_id TEXT PRIMARY KEY REFERENCES article_runs(id),
      status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'done', 'empty', 'failed')),
      available_at INTEGER NOT NULL DEFAULT 0,
      token TEXT,
      failure_count INTEGER NOT NULL DEFAULT 0,
      publish INTEGER NOT NULL DEFAULT 1,
      version TEXT,
      error TEXT
    );
    CREATE TABLE IF NOT EXISTS article_run_stages (
      id TEXT PRIMARY KEY,
      run_id TEXT NOT NULL REFERENCES article_runs(id),
      candidate_id TEXT NOT NULL,
      page INTEGER NOT NULL,
      table_number INTEGER NOT NULL,
      strategy TEXT NOT NULL,
      kind TEXT NOT NULL DEFAULT 'table',
      figure_label TEXT,
      text_stage_id TEXT REFERENCES article_run_stages(id),
      grid_stage_id TEXT REFERENCES article_run_stages(id),
      status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'done', 'failed')),
      result_count INTEGER,
      remark_count INTEGER,
      ambiguous_binding_count INTEGER,
      published_at INTEGER,
      generation INTEGER NOT NULL DEFAULT 1,
      attempt_limit INTEGER NOT NULL DEFAULT 3,
      available_at INTEGER NOT NULL DEFAULT 0,
      attempt_id TEXT,
      lease_expires_at INTEGER,
      error TEXT,
      failure_kind TEXT
    );
    CREATE TABLE IF NOT EXISTS article_run_attempts (
      id TEXT PRIMARY KEY,
      stage_id TEXT NOT NULL REFERENCES article_run_stages(id),
      number INTEGER NOT NULL,
      status TEXT NOT NULL CHECK (status IN ('running', 'succeeded', 'failed', 'interrupted')),
      started_at INTEGER NOT NULL,
      finished_at INTEGER,
      error TEXT,
      UNIQUE (stage_id, number)
    );
    CREATE TABLE IF NOT EXISTS article_figure_decisions (
      article_id TEXT NOT NULL,
      candidate_id TEXT NOT NULL,
      decision TEXT NOT NULL CHECK (decision IN ('data', 'illustration')),
      PRIMARY KEY (article_id, candidate_id)
    );
    CREATE INDEX IF NOT EXISTS article_runs_article ON article_runs(article_id, created_at);
    CREATE INDEX IF NOT EXISTS article_run_stages_run ON article_run_stages(run_id);
  `)
  db.exec('BEGIN IMMEDIATE')
  try {
    const runColumns = db.prepare('PRAGMA table_info(article_runs)').all()
    const releases = db.prepare('PRAGMA table_info(article_run_publications)').all()
    if (!releases.some(column => column.name === 'publish')) {
      db.exec('ALTER TABLE article_run_publications ADD COLUMN publish INTEGER NOT NULL DEFAULT 1')
    }
    if (!runColumns.some(column => column.name === 'coverage_plan')) db.exec('ALTER TABLE article_runs ADD COLUMN coverage_plan TEXT')
    if (!runColumns.some(column => column.name === 'article_context')) db.exec('ALTER TABLE article_runs ADD COLUMN article_context INTEGER')
    const columns = db.prepare('PRAGMA table_info(article_run_stages)').all()
    if (!columns.some(column => column.name === 'kind')) db.exec(`
      ALTER TABLE article_run_stages ADD COLUMN kind TEXT NOT NULL DEFAULT 'table';
      ALTER TABLE article_run_stages ADD COLUMN text_stage_id TEXT REFERENCES article_run_stages(id);
      ALTER TABLE article_run_stages ADD COLUMN grid_stage_id TEXT REFERENCES article_run_stages(id);
    `)
    if (!columns.some(column => column.name === 'figure_label')) db.exec('ALTER TABLE article_run_stages ADD COLUMN figure_label TEXT')
    if (!columns.some(column => column.name === 'figure_plan')) db.exec('ALTER TABLE article_run_stages ADD COLUMN figure_plan TEXT')
    if (!columns.some(column => column.name === 'table_region')) db.exec('ALTER TABLE article_run_stages ADD COLUMN table_region TEXT')
    if (!columns.some(column => column.name === 'failure_kind')) db.exec('ALTER TABLE article_run_stages ADD COLUMN failure_kind TEXT')
    if (!columns.some(column => column.name === 'planned_strategy')) db.exec(`
      ALTER TABLE article_run_stages ADD COLUMN planned_strategy TEXT;
      ALTER TABLE article_run_stages ADD COLUMN strategy_reason TEXT;
    `)
    if (!columns.some(column => column.name === 'fallback_strategy')) db.exec(`
      ALTER TABLE article_run_stages ADD COLUMN fallback_strategy TEXT;
      ALTER TABLE article_run_stages ADD COLUMN fallback_reason TEXT;
      ALTER TABLE article_run_stages ADD COLUMN transition_reason TEXT;
      ALTER TABLE article_run_stages ADD COLUMN empty_result_reason TEXT;
    `)
    const attemptColumns = db.prepare('PRAGMA table_info(article_run_attempts)').all()
    if (!attemptColumns.some(column => column.name === 'strategy')) db.exec(`
      ALTER TABLE article_run_attempts ADD COLUMN strategy TEXT;
      ALTER TABLE article_run_attempts ADD COLUMN reason TEXT;
      ALTER TABLE article_run_attempts ADD COLUMN has_grid_artifact INTEGER NOT NULL DEFAULT 0;
    `)
    if (!attemptColumns.some(column => column.name === 'outcome')) db.exec('ALTER TABLE article_run_attempts ADD COLUMN outcome TEXT')
    if (!columns.some(column => column.name === 'remark_count')) db.exec('ALTER TABLE article_run_stages ADD COLUMN remark_count INTEGER')
    if (!columns.some(column => column.name === 'ambiguous_binding_count')) db.exec('ALTER TABLE article_run_stages ADD COLUMN ambiguous_binding_count INTEGER')
    if (!columns.some(column => column.name === 'published_at')) db.exec('ALTER TABLE article_run_stages ADD COLUMN published_at INTEGER')
    if (!columns.some(column => column.name === 'generation')) {
      db.exec(`
        ALTER TABLE article_run_stages ADD COLUMN generation INTEGER NOT NULL DEFAULT 1;
        ALTER TABLE article_run_stages ADD COLUMN attempt_limit INTEGER NOT NULL DEFAULT 3;
        ALTER TABLE article_run_stages ADD COLUMN available_at INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE article_run_stages ADD COLUMN attempt_id TEXT;
        ALTER TABLE article_run_stages ADD COLUMN lease_expires_at INTEGER;
        UPDATE article_run_stages SET status = 'queued', published_at = NULL WHERE status = 'running';
      `)
    }
    db.exec(`
      CREATE INDEX IF NOT EXISTS article_run_stages_delivery ON article_run_stages(available_at)
        WHERE status = 'queued' AND published_at IS NULL;
      CREATE INDEX IF NOT EXISTS article_run_stages_leases ON article_run_stages(lease_expires_at) WHERE status = 'running';
    `)
    db.exec('COMMIT')
  } catch (error) {
    db.exec('ROLLBACK')
    throw error
  }

  return db
}
