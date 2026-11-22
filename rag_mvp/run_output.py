"""Serialize table results and diagnostics into run artifacts."""

import csv
import json
from pathlib import Path

from .core.domain import Result


def write_json(path: Path, data: object) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_csv(path: Path, results: tuple[Result, ...]) -> None:
    columns: tuple[str, ...] = (
        "method", "generator", "dataset", "metric", "value", "unit", "raw_value", "statistic",
        "retrieval_top_k", "experiment", "task", "protocol", "page", "table", "row", "column", "header_path",
    )
    columns += tuple(field for field in ("metric_k", "factor", "setting", "setting_default", "source_language")
                     if any(getattr(result, field) is not None for result in results))
    if any(result.parameters for result in results):
        columns += ("parameters",)
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list[str](columns))
        writer.writeheader()
        for result in results:
            row = result.to_dict()
            row.pop("source")
            row.pop("condition_sources")
            row.pop("components")
            parameters = row.pop("parameters")
            if "parameters" in columns:
                row["parameters"] = json.dumps(parameters, ensure_ascii=False) if parameters else ""
            source = result.source
            writer.writerow({
                **row,
                "page": source.page,
                "table": source.table,
                "row": source.row,
                "column": source.column,
                "header_path": " → ".join(source.headers),
            })
