"""Translate a claimed stage into an extraction command and check its artifacts."""

import hashlib
import json
import os
from collections.abc import Callable
from pathlib import Path
from sqlite3 import Row

from .article_stage_store import StageOutcome
from .run import IncompatibleArtifact
from .table_region import TableRegion


ARTICLE_TEXT = "article-text.json"
TABLE_GRID = "table-grid.json"

RunCommand = Callable[[list[str]], None]


class TableGridMissing(ValueError):
    """The CLI saved a diagnostic run without reading a table grid."""


def execute_stage(store: Path, stage: Row, folder: Path, run: RunCommand) -> StageOutcome:
    pdf = _source_pdf(store, stage)
    kind = stage["kind"]

    if kind == "article-text":
        return _read_article_text(pdf, stage, folder, run)

    if kind == "figure":
        return _read_figure(pdf, store, stage, folder, run)

    strategy = stage["grid_strategy"] or stage["strategy"]
    args = [
        "extract", str(pdf),
        "--page", str(stage["page"]),
        "--table", str(stage["table_number"]),
        "--strategy", strategy,
        "--out", str(folder),
    ]
    if region := _saved_region(stage):
        args.extend(("--table-region", region))
    if kind == "table-grid":
        return _read_grid(args, stage, strategy, folder, run)

    return _read_table(args, store, stage, folder, run)


def _saved_region(stage: Row) -> str | None:
    return stage["table_region"] if "table_region" in stage.keys() else None


def _check_region(payload: dict, stage: Row) -> None:
    saved = _saved_region(stage)
    try:
        expected = TableRegion.from_json(saved) if saved else None
        actual = TableRegion.parse(payload["region"]) if "region" in payload else None
    except ValueError as error:
        raise IncompatibleArtifact(f"Некорректная область таблицы. {error}") from error
    if actual != expected:
        raise IncompatibleArtifact("Область таблицы не совпадает с планом прогона.")


def _source_pdf(store: Path, stage: Row) -> Path:
    pdf = store / stage["article_id"] / "map" / "source.pdf"
    with pdf.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != stage["pdf_sha256"]:
            raise IncompatibleArtifact("SHA-256 сохранённого PDF не совпадает с планом прогона.")
    return pdf


def _dependency_path(store: Path, stage: Row, name: str, filename: str) -> Path:
    attempt = stage[f"{name}_attempt"]
    if stage[f"{name}_status"] != "done" or attempt is None:
        reason = stage[f"{name}_error"]
        detail = f" {reason}" if reason else ""
        raise IncompatibleArtifact(f"Обязательный артефакт {filename} недоступен.{detail}")

    path = store / "runs" / stage["run_id"] / stage[f"{name}_stage_id"] / "attempts" / attempt / filename
    if not path.is_file():
        raise IncompatibleArtifact(f"Отсутствует сохранённый артефакт {filename}.")
    return path


def _read_article_text(pdf: Path, stage: Row, folder: Path, run: RunCommand) -> StageOutcome:
    run(["read-article-text", str(pdf), "--strategy", stage["strategy"], "--out", str(folder)])

    result = json.loads((folder / ARTICLE_TEXT).read_text(encoding="utf-8"))
    if result["sha256"] != stage["pdf_sha256"]:
        raise IncompatibleArtifact("SHA-256 общего текста не совпадает с версией PDF прогона.")
    return StageOutcome()


def _read_figure(pdf: Path, store: Path, stage: Row, folder: Path, run: RunCommand) -> StageOutcome:
    plan_json = stage["figure_plan"] if "figure_plan" in stage.keys() else None
    target = json.loads(plan_json)["target"] if plan_json is not None else None
    region = target if target and target["stage"] == "region" else None
    if region and (region["page"] != stage["page"] or region["pdf_sha256"] != stage["pdf_sha256"]):
        raise IncompatibleArtifact("Адрес области отличается от плана прогона.")
    args = [
        "read-article-figure", str(pdf),
        "--inventory", str(pdf.with_name("inventory.json")),
        "--candidate", stage["candidate_id"],
        "--out", str(folder),
    ]
    if not region or stage["text_status"] == "done" or stage["text_failure_kind"] == "incompatible-artifact":
        text = _dependency_path(store, stage, "text", ARTICLE_TEXT)
        args.extend(("--article-text", str(text)))
    if plan_json is not None:
        args.extend(("--figure-plan", plan_json))
    run(args)

    result = json.loads((folder / "extracted.json").read_text(encoding="utf-8"))
    if result["source"]["sha256"] != stage["pdf_sha256"]:
        raise IncompatibleArtifact("Результат рисунка относится к другой версии PDF.")
    if region and result.get("target") != region:
        raise IncompatibleArtifact("Прочитанная область отличается от плана прогона.")
    remarks = result["remarks"]
    ambiguous_bindings = sum(remark.get("kind") == "ambiguous-binding" for remark in remarks)
    actual_strategy = result.get("attempted_strategy") if "attempted_strategy" in result else result["strategy"]
    return StageOutcome(len(result["results"]), len(remarks), ambiguous_bindings, strategy=actual_strategy)


def _read_grid(args: list[str], stage: Row, strategy: str, folder: Path, run: RunCommand) -> StageOutcome:
    run([*args, "--grid-only"])

    result = json.loads((folder / TABLE_GRID).read_text(encoding="utf-8"))
    if result["sha256"] != stage["pdf_sha256"]:
        raise IncompatibleArtifact("SHA-256 сетки не совпадает с версией PDF прогона.")
    address = (result["page"], result["table"], result["strategy"])
    if address != (stage["page"], stage["table_number"], strategy):
        raise IncompatibleArtifact("Адрес или стратегия сетки не совпадают с планом прогона.")
    _check_region(result, stage)

    read = result["read"]
    remarks = read["remarks"]
    if read["grid"] is None:
        raise TableGridMissing(_grid_failure(remarks))
    defects = list(dict.fromkeys([
        *result.get("reading_defects", ()),
        *(remark["reason"] for remark in remarks if remark.get("kind") == "reading-defect"),
    ]))
    fallback_eligible = True
    if stage["fallback_strategy"] == "paddle-structure" and strategy == "docling-ocr-tableformer":
        from .table_grid_artifact import load_table_grid
        from .table_ocr_recovery import can_recover_cells

        region = TableRegion.from_json(saved) if (saved := _saved_region(stage)) else None
        fallback_eligible = bool(region and defects and can_recover_cells(load_table_grid(
            folder / TABLE_GRID, stage["pdf_sha256"], stage["page"], stage["table_number"], strategy, region=region,
        )))
    return StageOutcome(remarks=len(remarks), grid_defect=" ".join(defects) or None, fallback_eligible=fallback_eligible)


def _read_table(args: list[str], store: Path, stage: Row, folder: Path, run: RunCommand) -> StageOutcome:
    if stage["grid_stage_id"] is not None:
        grid = _dependency_path(store, stage, "grid", TABLE_GRID)
        args.extend(("--grid-input", str(grid)))
        if stage["previous_grid_attempt"] is not None:
            previous = grid.parent.parent / stage["previous_grid_attempt"] / TABLE_GRID
            args.extend(("--previous-grid-input", stage["previous_grid_strategy"], str(previous)))
    if stage["text_stage_id"] is not None:
        # A reader outage still permits table-local evidence. An incompatible
        # input must reach the dependency check so it keeps its failure reason.
        if stage["text_status"] != "failed" or stage["text_failure_kind"] == "incompatible-artifact":
            text = _dependency_path(store, stage, "text", ARTICLE_TEXT)
            args.extend(("--article-text", str(text)))
    elif stage["grid_stage_id"] is None:
        article_context = stage["article_context"]
        # Older plans did not have shared text or recorded context mode.
        if article_context == 1 or (article_context is None and os.environ.get("RAG_ARTICLE_CONTEXT") == "1"):
            args.append("--article-context")

    run(args)

    result = json.loads((folder / "run.json").read_text(encoding="utf-8"))
    if result["source"]["sha256"] != stage["pdf_sha256"]:
        raise IncompatibleArtifact("Результат относится к другой версии PDF.")
    _check_region(result, stage)
    remarks = result["remarks"]
    if result["grid"] is None:
        raise TableGridMissing(_grid_failure(remarks))

    results = len(result["results"])
    ambiguous_bindings = sum(remark.get("kind") == "ambiguous-binding" for remark in remarks)
    empty_reason = None
    if results == 0:
        if ambiguous_bindings:
            empty_reason = "Сетка прочитана. Привязка значений к условиям осталась открытой."
        elif any(remark.get("kind") == "reading-conflict" for remark in remarks):
            empty_reason = "Попытки чтения дали разные варианты ячеек. Требуется подтверждение."
        else:
            reason = next(
                (remark["reason"] for remark in remarks if remark.get("reason")),
                "В сетке не найдено распознаваемых результатов метрик.",
            )
            empty_reason = f"Сетка прочитана. {reason}"
    return StageOutcome(results, len(remarks), ambiguous_bindings, empty_reason)


def _grid_failure(remarks: list[dict]) -> str:
    reasons = (remark["reason"] for remark in remarks if remark.get("reason"))
    return " ".join(reasons) or "Не удалось прочитать сетку таблицы."
