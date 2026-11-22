"""Prepare a local D009 run from a checked grid and article quotations.

Run from the repository root with python -m tests.fixtures.table_context_binding.prepare_d009.
"""

import argparse
import hashlib
import json
import shutil
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from rag_mvp.__main__ import write_csv, write_json
from rag_mvp.core.domain import ConditionAssertion, ReadCell, ReadTable
from rag_mvp.run import Evidence, GridCell, TableGrid, TableRead


FIXTURE = Path(__file__).with_name("d009-table1.json")
ROOT = FIXTURE.parents[3]
DEFAULT_PDF = ROOT / "docs/context/RAG_ВИНИТИ_участникам_2026-11-20/dev/D009.pdf"


def read_d009() -> tuple[TableRead, tuple[ConditionAssertion, ...]]:
    saved = json.loads(FIXTURE.read_text(encoding="utf-8"))
    evidence = tuple(Evidence(**{**item, "box": tuple(item["box"])}) for item in saved["evidence"])
    by_id = {item.id: item for item in evidence}
    cells = tuple(
        GridCell(**{**item, "evidence_ids": tuple(item.get("evidence_ids", ()))})
        for item in saved["grid"]["cells"]
    )
    grid = TableGrid(**{**saved["grid"], "cells": cells})
    headings = [cell for cell in grid.cells if cell.role == "column_header"]
    method_column = next(cell.column for cell in headings if cell.text == "Method")
    type_column = next(cell.column for cell in headings if cell.text == "Type")

    def row_text(row: int, column: int) -> str:
        return next(
            cell.text for cell in grid.cells
            if cell.column == column and cell.row <= row < cell.row + cell.row_span
        )

    read_cells = tuple(
        ReadCell(
            row=cell.row,
            column=cell.column,
            method=row_text(cell.row, method_column),
            headers=tuple(
                heading.text for heading in headings
                if heading.column <= cell.column < heading.column + heading.column_span
                and heading.row < cell.row
            ),
            text=cell.text,
            box=by_id[cell.evidence_ids[0]].box,
            is_baseline=saved["baseline_groups"].get(row_text(cell.row, type_column)),
        )
        for cell in grid.cells if cell.role == "data"
    )
    context_ids = tuple(saved["context_evidence_ids"])
    table = ReadTable(
        page=saved["page"],
        number=saved["table"],
        caption=by_id[saved["grid"]["caption_evidence_id"]].text,
        context="\n".join(by_id[ref].text for ref in context_ids),
        cells=read_cells,
    )
    assertions = tuple(ConditionAssertion(**item) for item in saved["assertions"])
    return TableRead(table=table, grid=grid, evidence=evidence, context_evidence_ids=context_ids), assertions


def prepare_d009(pdf: Path, out: Path) -> None:
    from pypdfium2 import PdfDocument

    saved = json.loads(FIXTURE.read_text(encoding="utf-8"))
    if out.exists():
        raise ValueError(f"Каталог уже существует: {out}")

    read, assertions = read_d009()
    collected = read.collect(assertions)
    page = saved["page"]
    out.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".d009-context-", dir=out.parent) as temp:
        run = Path(temp)
        snapshot = run / "source.pdf"
        shutil.copyfile(pdf, snapshot)
        with snapshot.open("rb") as stream:
            sha256 = hashlib.file_digest(stream, "sha256").hexdigest()
        if sha256 != saved["sha256"]:
            raise ValueError("PDF не совпадает с версией D009, по которой проверялась фикстура.")
        with PdfDocument(snapshot) as document:
            with closing(document[page - 1]) as preview:
                preview.render(scale=2).to_pil().save(run / "page.png")
        write_json(run / "run.json", {
            "document": saved["document"],
            "page": page,
            "table": saved["table"],
            "strategy": "checked-grid",
            "raw_artifacts": [],
            "source": {
                "name": saved["document"],
                "sha256": sha256,
                "pdf": "source.pdf",
                "page": page,
                "preview": "page.png",
                "previews": {page: "page.png"},
            },
            **read.serialize(collected, page),
        })
        write_csv(run / "results.csv", collected.results)
        for asset in (ROOT / "rag_mvp/viewer").iterdir():
            shutil.copyfile(asset, run / asset.name)
        run.rename(out)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Собрать проверенный прогон D009 из сохранённой сетки и цитат.")
    parser.add_argument("--pdf", type=Path, default=DEFAULT_PDF)
    parser.add_argument("--out", type=Path, default=ROOT / "runs/D009-context")
    options = parser.parse_args()
    try:
        prepare_d009(options.pdf, options.out)
    except (OSError, ValueError) as error:
        parser.exit(1, f"{error}\n")
    print(f"Прогон: {options.out.resolve()}")
