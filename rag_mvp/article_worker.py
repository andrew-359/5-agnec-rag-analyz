"""Deliver a saved article stage to the existing PDF extraction command."""

import json
import os
import re
from collections.abc import Callable
from contextlib import closing
from functools import partial
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
from tempfile import TemporaryFile
from time import monotonic, sleep

import pika
from pika.adapters.utils.connection_workflow import AMQPConnectorException

from .article_stage_extraction import TableGridMissing, execute_stage
from .article_stage_store import LeaseLost, StageOutcome, claim_stage, finish_attempt, renew_lease
from .run import IncompatibleArtifact


ROOT = Path(__file__).resolve().parents[1]
EXTRACTION_TIMEOUT = 600
RENEW_INTERVAL = 2.0
# Earlier publishers sent only the ID. Such deliveries belong to the first generation.
STAGE_MESSAGE = re.compile(r"([0-9a-f-]{36})(?::([1-9][0-9]{0,5}))?")


def run_extractor(
    connection: pika.BlockingConnection, renew: Callable[[], None], args: list[str], *, docling_cache: Path | None,
) -> None:
    if sys.platform == "linux":
        command = [sys.executable, "-m", "rag_mvp.article_extractor", str(os.getpid()), *args]
    else:
        command = [sys.executable, "-m", "rag_mvp", *args]
    env = None if docling_cache is None else {**os.environ, "RAG_DOCLING_PAGE_CACHE": str(docling_cache)}
    with TemporaryFile() as stderr, subprocess.Popen(command, cwd=ROOT, stderr=stderr, env=env) as process:
        deadline = monotonic() + EXTRACTION_TIMEOUT
        try:
            while process.poll() is None:
                if monotonic() >= deadline:
                    raise ValueError("Обработка заняла слишком много времени.")
                renew()
                connection.process_data_events(time_limit=0.5)
        finally:
            if process.poll() is None:
                process.kill()
        if process.returncode:
            stderr.seek(max(0, stderr.tell() - 4096))
            diagnostic = stderr.read().decode(errors="replace").strip().splitlines()
            error = IncompatibleArtifact if process.returncode == 3 else ValueError
            raise error(diagnostic[-1][:500] if diagnostic else "Не удалось выполнить этап извлечения.")


def extract_stage(
    connection: pika.BlockingConnection, store: Path, stage: sqlite3.Row,
    folder: Path, renew: Callable[[], None],
) -> StageOutcome:
    docling_cache = None
    if stage["kind"] in ("table-grid", "table") and (stage["grid_strategy"] or stage["strategy"]) == "docling-tableformer":
        docling_cache = store / "runs" / stage["run_id"] / "docling-page-cache"
    return execute_stage(store, stage, folder, partial(run_extractor, connection, renew, docling_cache=docling_cache))


def log_table_event(event: str, fields: dict, **details: object) -> None:
    try:
        print(json.dumps({"event": event, **fields, **details}), flush=True)
    except OSError:
        # Logging must not interrupt an attempt or change delivery handling.
        pass


def process_stage(connection: pika.BlockingConnection, store: Path, stage_id: str, generation: int) -> None:
    database = store / "article-runs.sqlite"
    with closing(sqlite3.connect(f"{database.as_uri()}?mode=rw", uri=True)) as db:
        db.row_factory = sqlite3.Row
        stage = claim_stage(db, stage_id, generation)
        if stage is None:
            return
        token = stage["attempt_id"]
        # The CLI installs its completed output by renaming into this attempt's own directory.
        folder = store / "runs" / stage["run_id"] / stage_id / "attempts" / token
        renew_at = monotonic() + RENEW_INTERVAL

        def renew():
            nonlocal renew_at
            if monotonic() >= renew_at:
                renew_lease(db, token, stage_id)
                renew_at = monotonic() + RENEW_INTERVAL

        started_at = monotonic()
        table_log = None
        if stage["kind"] in ("table-grid", "table"):
            table_log = {
                "run_id": stage["run_id"], "stage_id": stage_id, "attempt_id": token,
                "stage_kind": stage["kind"], "generation": generation,
                "page": stage["page"], "table": stage["table_number"],
                "strategy": stage["grid_strategy"] or stage["strategy"],
            }
            log_table_event("table_extraction_started", table_log)
        status = "interrupted"
        result_counts = {}
        try:
            outcome = extract_stage(connection, store, stage, folder, renew)
        except LeaseLost:
            status = "lease_lost"
            return
        except (OSError, ValueError, KeyError, TypeError) as error:
            accepted = finish_attempt(
                db, stage, None, str(error) or "Не удалось обработать таблицу.",
                retryable=not isinstance(error, (TableGridMissing, IncompatibleArtifact)),
                failure_kind="incompatible-artifact" if isinstance(error, IncompatibleArtifact) else None,
                missing_grid=isinstance(error, TableGridMissing),
            )
            status = "failed" if accepted else "lease_lost"
        else:
            accepted = finish_attempt(db, stage, outcome, None)
            status = "succeeded" if accepted else "lease_lost"
            if accepted:
                result_counts = {"results": outcome.results, "remarks": outcome.remarks}
        finally:
            if table_log is not None:
                log_table_event("table_extraction_finished", table_log, outcome=status,
                                duration_ms=round((monotonic() - started_at) * 1000), **result_counts)


def consume(parameters: pika.URLParameters, queue: str, store: Path) -> None:
    with pika.BlockingConnection(parameters) as connection:
        channel = connection.channel()
        channel.queue_declare(queue, durable=True)
        channel.basic_qos(prefetch_count=1)
        print(f"Исполнитель прогона слушает {queue}", flush=True)
        for method, _, body in channel.consume(queue):
            message = STAGE_MESSAGE.fullmatch(body.decode("ascii", errors="replace"))
            if message:
                process_stage(connection, store, message[1], int(message[2] or "1"))
            channel.basic_ack(method.delivery_tag)


def main() -> None:
    url = os.environ.get("RAG_AMQP_URL")
    if not url:
        sys.exit("Укажите RAG_AMQP_URL для подключения исполнителя к RabbitMQ.")
    queue = os.environ.get("RAG_AMQP_QUEUE") or "rag.article-extraction"
    store = Path(os.environ.get("RAG_ARTICLE_STORE") or ROOT / "output/web-articles").resolve()
    parameters = pika.URLParameters(url)
    if parameters.blocked_connection_timeout is None:
        parameters.blocked_connection_timeout = 8.0
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    last_error = None
    try:
        while True:
            try:
                consume(parameters, queue, store)
            except (pika.exceptions.AMQPError, AMQPConnectorException, sqlite3.OperationalError) as error:
                message = f"{type(error).__name__}: {error}"
                if message != last_error:
                    print(f"Повторное подключение исполнителя. {message}", file=sys.stderr, flush=True)
                last_error = message
            sleep(1)
    except KeyboardInterrupt:
        return


if __name__ == "__main__":
    main()
