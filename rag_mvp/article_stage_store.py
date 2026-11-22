"""Own SQLite transactions for article stage attempts and leases."""

import sqlite3
from dataclasses import dataclass
from time import time_ns
from uuid import uuid4


LEASE_MS = 10_000


class LeaseLost(Exception):
    pass


@dataclass(frozen=True)
class StageOutcome:
    results: int = 0
    remarks: int = 0
    ambiguous: int = 0
    empty_reason: str | None = None
    grid_defect: str | None = None
    strategy: str | None = None
    fallback_eligible: bool = True


def now_ms() -> int:
    return time_ns() // 1_000_000


def claim_stage(db: sqlite3.Connection, stage_id: str, generation: int) -> sqlite3.Row | None:
    token = str(uuid4())
    with db:
        db.execute("BEGIN IMMEDIATE")
        now = now_ms()
        claimed = db.execute(
            """UPDATE article_run_stages SET status = 'running', attempt_id = ?, lease_expires_at = ?
               WHERE id = ? AND generation = ? AND status = 'queued' AND available_at <= ?
               AND NOT EXISTS (
                 SELECT 1 FROM article_run_stages dep
                 WHERE dep.id IN (article_run_stages.text_stage_id, article_run_stages.grid_stage_id)
                   AND dep.status IN ('queued', 'running')
               )""",
            (token, now + LEASE_MS, stage_id, generation, now),
        )
        if not claimed.rowcount:
            return None
        db.execute(
            """INSERT INTO article_run_attempts (id, stage_id, number, status, started_at, strategy, reason)
               SELECT ?, stage.id, ?, 'running', ?, COALESCE(grid_attempt.strategy, grid.strategy, stage.strategy), stage.transition_reason
               FROM article_run_stages stage LEFT JOIN article_run_stages grid ON grid.id = stage.grid_stage_id
               LEFT JOIN article_run_attempts grid_attempt ON grid_attempt.id = grid.attempt_id
               WHERE stage.id = ?""",
            (token, generation, now, stage_id),
        )
        return db.execute(
            """SELECT stage.*, run.article_id, run.pdf_sha256, run.article_context,
                 text.status AS text_status, text.attempt_id AS text_attempt, text.error AS text_error,
                 text.failure_kind AS text_failure_kind,
                 grid.status AS grid_status, grid.attempt_id AS grid_attempt, grid.error AS grid_error,
                 COALESCE(grid_attempt.strategy, grid.strategy) AS grid_strategy,
                 previous.id AS previous_grid_attempt, previous.strategy AS previous_grid_strategy
               FROM article_run_stages stage
               JOIN article_runs run ON run.id = stage.run_id
               LEFT JOIN article_run_stages text ON text.id = stage.text_stage_id AND text.run_id = stage.run_id
               LEFT JOIN article_run_stages grid ON grid.id = stage.grid_stage_id AND grid.run_id = stage.run_id
               LEFT JOIN article_run_attempts grid_attempt ON grid_attempt.id = grid.attempt_id
               LEFT JOIN article_run_attempts previous ON previous.id = (
                 SELECT id FROM article_run_attempts
                 WHERE stage_id = grid.id AND status = 'succeeded' AND has_grid_artifact = 1
                   AND number < grid_attempt.number AND strategy <> grid_attempt.strategy
                 ORDER BY number DESC LIMIT 1)
               WHERE stage.id = ?""",
            (stage_id,),
        ).fetchone()


def renew_lease(db: sqlite3.Connection, token: str, stage_id: str) -> None:
    with db:
        db.execute("BEGIN IMMEDIATE")
        now = now_ms()
        renewed = db.execute(
            """UPDATE article_run_stages SET lease_expires_at = ?
               WHERE id = ? AND attempt_id = ? AND status = 'running' AND lease_expires_at > ?""",
            (now + LEASE_MS, stage_id, token, now),
        )
        if not renewed.rowcount:
            raise LeaseLost


def finish_attempt(
    db: sqlite3.Connection, stage: sqlite3.Row, outcome: StageOutcome | None, error: str | None,
    *, retryable: bool = True, failure_kind: str | None = None, missing_grid: bool = False,
) -> bool:
    token = stage["attempt_id"]
    generation = stage["generation"]
    with db:
        db.execute("BEGIN IMMEDIATE")
        now = now_ms()
        owns_stage = db.execute(
            """SELECT 1 FROM article_run_stages
               WHERE id = ? AND attempt_id = ? AND status = 'running' AND lease_expires_at > ?""",
            (stage["id"], token, now),
        ).fetchone()
        if not owns_stage:
            return False

        defect = outcome.grid_defect if outcome else None
        is_grid = stage["kind"] == "table-grid"
        fallback = stage["fallback_strategy"]
        switch_strategy = (missing_grid or defect) and fallback not in (None, stage["strategy"])
        if is_grid and fallback == "paddle-structure":
            # Paddle supplies tokens for an existing TableFormer grid and has one slot in the attempt budget.
            switch_strategy = switch_strategy and outcome is not None and outcome.fallback_eligible
            retryable = retryable and stage["strategy"] != fallback
        has_time = generation < stage["attempt_limit"]
        if has_time and (switch_strategy or (outcome is None and retryable)):
            reason = (f"{stage['fallback_reason']} Предыдущая стратегия: {defect or error}" if switch_strategy
                      else f"Повтор после временного сбоя. {error}")
            db.execute(
                """UPDATE article_run_stages SET status = 'queued', generation = generation + 1,
                   available_at = ?, published_at = NULL, attempt_id = NULL, lease_expires_at = NULL,
                   strategy = ?, transition_reason = ?, remark_count = COALESCE(?, remark_count) WHERE id = ?""",
                (now + generation * 1000, stage["fallback_strategy"] if switch_strategy else stage["strategy"],
                 reason, outcome.remarks if outcome else None, stage["id"]),
            )
        elif outcome is not None:
            stop = None
            if defect:
                if not has_time:
                    stop = f"Исчерпан лимит попыток ({stage['attempt_limit']}). Доступна прочитанная часть."
                elif fallback is None:
                    stop = "Подходящий запасной ридер отсутствует. Доступна прочитанная часть."
                elif not outcome.fallback_eligible:
                    stop = "Для запасного чтения нет пригодных опор. Доступна прочитанная часть."
                else:
                    stop = "Запасной ридер уже использован. Доступна прочитанная часть."
            db.execute(
                """UPDATE article_run_stages SET status = 'done', result_count = ?, remark_count = ?,
                   ambiguous_binding_count = ?, empty_result_reason = ?, strategy = COALESCE(?, strategy),
                   error = NULL, lease_expires_at = NULL
                   WHERE id = ?""",
                (outcome.results, outcome.remarks, outcome.ambiguous, stop or outcome.empty_reason,
                 outcome.strategy, stage["id"]),
            )
        else:
            retained = db.execute(
                """SELECT id FROM article_run_attempts WHERE stage_id = ? AND status = 'succeeded'
                   AND has_grid_artifact = 1 ORDER BY number DESC LIMIT 1""", (stage["id"],),
            ).fetchone() if is_grid else None
            if retained:
                reason = (f"Исчерпан лимит попыток ({stage['attempt_limit']})." if not has_time
                          else "Запасное чтение остановлено.")
                db.execute(
                    """UPDATE article_run_stages SET status = 'done', empty_result_reason = ?, attempt_id = ?,
                       lease_expires_at = NULL WHERE id = ?""",
                    (f"{reason} Доступна прочитанная часть.", retained["id"], stage["id"]),
                )
            else:
                db.execute(
                    """UPDATE article_run_stages SET status = 'failed', error = ?, failure_kind = ?, attempt_id = NULL,
                       lease_expires_at = NULL WHERE id = ?""",
                    (error, failure_kind, stage["id"]),
                )
        db.execute(
            """UPDATE article_run_attempts SET status = ?, finished_at = ?, error = ?, has_grid_artifact = ?,
               outcome = ?, strategy = CASE WHEN ? THEN ? ELSE COALESCE(?, strategy) END WHERE id = ?""",
            ("succeeded" if outcome is not None else "failed", now, error,
             is_grid and (outcome is not None or missing_grid),
             (defect or "Сетка прочитана без обнаруженных дефектов.") if is_grid and outcome is not None else None,
             stage["kind"] == "figure",
             outcome.strategy if outcome else None, outcome.strategy if outcome else None, token),
        )
    return True
