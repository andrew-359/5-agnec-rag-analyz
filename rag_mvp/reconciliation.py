"""Build a reproducible reconciliation report from saved article artifacts."""

import argparse
import json
import re
from pathlib import Path

from .strategies.text_pdfplumber import load_article_fragments
from .core.measurement_reconciliation import parse_observations, reconcile_measurements, summarize_observations
from .prose_results import build_prose_artifact
from .run_output import write_json


ARTIFACT_KINDS = ("table", "figure", "prose")
PROSE_ID = "prose-results"


def check_run_origin(path: Path, run_id: str, declared_run: str | None = None) -> None:
    # Queue outputs live at runs/<run>/<stage>/attempts/<attempt>/<file>.
    folder = path.resolve().parent
    if folder.parent.name == "attempts" and folder.parent.parent.parent.parent.name == "runs":
        directory_run = folder.parent.parent.parent.name
        if directory_run != run_id:
            raise ValueError(f"{path.name}: файл сохранён в другом прогоне ({directory_run}).")
    if declared_run is not None and declared_run != run_id:
        raise ValueError(f"{path.name}: артефакт относится к другому прогону ({declared_run}).")


def load_artifact(path: Path, sha256: str, run_id: str) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    check_run_origin(path, run_id, data.get("run_id"))
    if data["source"]["sha256"] != sha256:
        raise ValueError(f"{path.name}: артефакт относится к другой версии PDF.")
    if any(not isinstance(data[field], list) for field in ("results", "remarks", "evidence")):
        raise ValueError(f"{path.name}: ожидались списки результатов, замечаний и источников.")
    for field in ("results", "remarks", "evidence"):
        ids = [item["id"] for item in data[field]]
        if len(set(ids)) != len(ids):
            raise ValueError(f"{path.name}: одинаковые идентификаторы в {field}.")
    sources = {item["id"]: item for item in data["evidence"]}
    for row in (*data["results"], *data["remarks"]):
        source = row["source"]
        direct = source.get("evidence_ids", [])
        supporting = (ref for refs in (row.get("condition_sources") or {}).values() for ref in refs)
        for ref in (*direct, *supporting):
            if ref not in sources:
                raise ValueError(f"{path.name}: отсутствует источник {ref}.")
        if any(sources[ref]["page"] != source["page"] for ref in direct):
            raise ValueError(f"{path.name}: источники {row['id']} относятся к разным страницам.")
    return data


def report(run_id: str, sha256: str, artifacts: list[dict], article_text: Path | None) -> dict:
    ids = [entry["id"] for entry in artifacts]
    if len(set(ids)) != len(ids) or (article_text is not None and PROSE_ID in ids):
        raise ValueError("Повторяется идентификатор артефакта.")
    if article_text is not None:
        check_run_origin(article_text, run_id)
        fragments = load_article_fragments(article_text, sha256, expected_run_id=run_id)
        data = build_prose_artifact(fragments, [entry["data"] for entry in artifacts if entry["kind"] == "table"])
        data.update(strategy="prose-comparison", versions={"pdfplumber": fragments.engine_version})
        artifacts = [*artifacts, {"id": PROSE_ID, "kind": "prose", "data": data}]
    relationships = reconcile_measurements(parse_observations(artifacts))
    return {
        "schema_version": 1, "run_id": run_id, "source": {"sha256": sha256},
        "artifacts": artifacts, "relationships": relationships,
        "summary": summarize_observations(artifacts, relationships),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Сверить сохранённые наблюдения одной статьи.")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--pdf-sha256", required=True)
    parser.add_argument("--article-text", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    for kind in ARTIFACT_KINDS:
        parser.add_argument(f"--{kind}", nargs=2, action="append", default=[], metavar=("ID", "JSON"))
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-f]{64}", args.pdf_sha256):
        parser.error("Ожидается SHA-256 исходного PDF.")
    try:
        artifacts = [
            {"id": id_, "kind": kind, "data": load_artifact(Path(path), args.pdf_sha256, args.run_id)}
            for kind in ARTIFACT_KINDS for id_, path in getattr(args, kind)
        ]
        write_json(args.out, report(args.run_id, args.pdf_sha256, artifacts, args.article_text))
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
