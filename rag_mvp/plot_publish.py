"""Publish an explicitly reviewed plot observation with its PDF and linked CSV package."""

import argparse
import csv
from decimal import Decimal
import hashlib
from html import escape
import json
import math
from pathlib import Path
import re
import shutil
import tempfile
import subprocess
import sys

import pdfplumber

from .core.domain import Result, Source
from .plot_preview import write_preview


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def normalize(text):
    return " ".join(text.split())


def reviewed_result(plot, context, pdf):
    """Validate the selected observation and its human-reviewed semantic mapping."""
    if plot["sha256"] != digest(pdf) or context["sha256"] != plot["sha256"]:
        raise ValueError("PDF, plot and reviewed context must have identical SHA-256.")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", context["publication_id"]):
        raise ValueError("Invalid publication ID.")
    if context.get("reviewed") is not True:
        raise ValueError("Explicitly reviewed context is required before publication.")
    selected = [point for point in plot["points"] if point["index"] == context["point_index"]]
    if len(selected) != 1:
        raise ValueError("Selected point must exist exactly once.")
    point = selected[0]
    if point["source"]["page"] != plot["source"]["page"]:
        raise ValueError("Point and plot must refer to the same PDF page.")
    if context["value_axis"] != "y" or context["parameter_axis"] != "x":
        raise ValueError("This first slice publishes y as metric and x as experiment parameter.")
    if not all(math.isfinite(point[axis]) for axis in ("x", "y")):
        raise ValueError("Point coordinates must be finite.")
    if plot["axes"]["y"].get("scale") != "linear":
        raise ValueError("Only confirmed linear metric scales are supported.")
    if context["unit"] not in {"fraction", "%"}:
        raise ValueError("Unsupported metric unit.")
    if context["metric"] != "EM":
        raise ValueError("This slice accepts reviewed Exact Match observations.")
    limit = 1 if context["unit"] == "fraction" else 100
    if not 0 <= point["y"] <= limit:
        raise ValueError("EM value is outside its declared scale.")
    tolerance = context["absolute_tolerance"]
    if not isinstance(tolerance, (int, float)) or not math.isfinite(tolerance) or tolerance <= 0:
        raise ValueError("A positive finite reading tolerance is required.")
    box = point["source"]["box"]
    if not (len(box) == 4 and 0 <= box[0] < box[2] <= 1 and 0 <= box[1] < box[3] <= 1):
        raise ValueError("Invalid point evidence coordinates.")
    import pypdfium2

    with pdfplumber.open(pdf) as document, pypdfium2.PdfDocument(pdf) as text_document:
        if not 1 <= point["source"]["page"] <= len(document.pages):
            raise ValueError("Point source is outside PDF.")
        for field in ("method", "dataset", "metric", "task", "unit", "parameter"):
            if not context.get(field):
                raise ValueError(f"Missing reviewed field: {field}")
            refs = context["field_evidence"].get(field, [])
            if not refs:
                raise ValueError(f"Missing evidence for {field}")
            for reference in refs:
                page_index = reference["page"] - 1
                if not 0 <= page_index < len(document.pages):
                    raise ValueError(f"Invalid source page for {field}.")
                text = document.pages[page_index].extract_text(x_tolerance=1) or ""
                pdf_page = text_document[page_index]
                text_page = pdf_page.get_textpage()
                try:
                    alternative = text_page.get_text_range()
                finally:
                    text_page.close()
                    pdf_page.close()
                if not reference["text"].strip() or not any(
                    normalize(reference["text"]) in normalize(candidate) for candidate in (text, alternative)
                ):
                    raise ValueError(f"Unverified quote for {field}.")
        evidence = context["field_evidence"]
        source = Source(
            page=point["source"]["page"], table=None, figure="Figure " + str(plot["settings"]["figure"]),
            box=tuple(box), evidence_ids=("plot-point",),
            headers=(context["dataset"], context["metric"], f'{context["parameter"]}={point["x"]:g}'),
        )
        # Geometry yields an approximation, not a literal printed number.
        raw = f'~{point["y"]:.8g}'
        value = str(Decimal(str(point["y"])))
        result = Result(
            method=context["method"], generator=context.get("generator", ""), dataset=context["dataset"],
            metric=context["metric"], task=context["task"], experiment="E1",
            protocol=context["protocol"], raw_value=raw, value=value, unit=context["unit"],
            statistic=None, retrieval_top_k=None, source=source,
        )
    return result, point, evidence


def publish(plot_path, context_path, pdf, template, destination):
    plot = json.loads(Path(plot_path).read_text())
    context = json.loads(Path(context_path).read_text())
    result, point, field_evidence = reviewed_result(plot, context, pdf)
    checker = Path(template).parent / "tools" / "check_submission.py"
    inputs = {
        "plot": digest(plot_path), "reviewed_context": digest(context_path),
        "schema": digest(Path(template) / "schema.json"), "checker": digest(checker),
        "publisher": digest(__file__), "preview": digest(Path(__file__).with_name("plot_preview.py")),
    }
    identity = hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()
    version = "plot-" + identity[:20]
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / version
    if target.exists():
        raise ValueError(f"Published version already exists: {target}")
    schema = json.loads((Path(template) / "schema.json").read_text())
    with tempfile.TemporaryDirectory(prefix=".publish-", dir=destination) as temporary:
        folder = Path(temporary) / version
        folder.mkdir()
        shutil.copyfile(pdf, folder / "source.pdf")
        if digest(folder / "source.pdf") != plot["sha256"]:
            raise ValueError("PDF changed while preparing publication.")
        shutil.copyfile(plot_path, folder / "plot-input.json")
        shutil.copyfile(context_path, folder / "reviewed-context.json")
        pid = context["publication_id"]
        locator = context["field_evidence"]["dataset"][0]
        notes = (
            f'Figure {plot["settings"]["figure"]}, panel {plot["settings"]["panel"]}, point {point["index"]}; '
            f'{context["parameter"]}={point["x"]:g}; approximate, tolerance {context["absolute_tolerance"]} '
            f'{result.unit}; geometry is not a printed numeric label; version {version}.'
        )
        rows = {
            "publications": [{"publication_id": pid, "title": context.get("title", ""),
                              "in_scope": "true"}],
            "configurations": [{"publication_id": pid, "config_id": "C1", "rag_variant": result.method,
                                "generator_model": result.generator, "note": notes}],
            "datasets": [{"publication_id": pid, "dataset_id": "DS1", "dataset_name": result.dataset}],
            "tasks": [{"publication_id": pid, "task_id": "T1", "task_type": result.task}],
            "experiments": [{"publication_id": pid, "experiment_id": "E1",
                             "evaluation_protocol": result.protocol, "note": notes}],
            "results": [{
                "publication_id": pid, "result_id": "R1", "experiment_id": "E1", "config_id": "C1",
                "dataset_id": "DS1", "task_id": "T1", "metric_group": "answer quality",
                "metric_name": result.metric, "metric_variant": "exact match",
                "metric_direction": "higher_is_better", "metric_value": result.value,
                "metric_unit": result.unit, "raw_value": result.raw_value,
                "normalized_value": result.value, "normalized_unit": result.unit,
                "result_origin": "primary", "source_page": result.source.page,
                "source_figure": result.source.figure, "source_fragment": locator["text"],
                "extraction_status": "extracted", "note": notes,
            }],
            "issues": [{"publication_id": pid, "issue_id": "I1", "record_ids": "R1",
                        "check_name": "plot_digitization", "description": notes,
                        "source_page": locator["page"], "source_fragment": locator["text"]}],
        }
        for name, columns in schema["tables"].items():
            with (folder / (name + ".csv")).open("w", newline="", encoding="utf-8") as stream:
                writer = csv.DictWriter(stream, fieldnames=columns)
                writer.writeheader()
                writer.writerows(rows[name])
        write_json(folder / "schema.json", schema)
        record = {"id": "R1", "approximate": True, "absolute_tolerance": context["absolute_tolerance"],
                  "parameter": {"name": context["parameter"], "value": point["x"]}, **result.to_dict()}
        write_json(folder / "run.json", {
            "schema_version": 1, "kind": "published-plot", "version_id": version,
            "publication_id": pid, "results": [record], "remarks": rows["issues"],
            "pdf": {"sha256": plot["sha256"], "url": f"/published/{version}/source.pdf"},
            "evidence": {"plot-point": {**point["source"], "url": f"/published/{version}/source.pdf#page={result.source.page}"},
                         "conditions": field_evidence},
            "coverage": {"figure": result.source.figure, "panel": plot["settings"]["panel"],
                         "point_indices": [point["index"]], "whole_publication": False},
        })
        preview = {**plot, "pdf": str(folder / "source.pdf"), "points": [point],
                   "source": {**plot["source"], "url": "source.pdf#page=" + str(result.source.page)}}
        write_preview(preview, folder / "index.html")
        html = (folder / "index.html").read_text()
        description = escape(f"{result.method} · {result.dataset} · {result.metric} ≈ {result.value} {result.unit}")
        links = " · ".join(f'<a href="{name}.csv">{name}.csv</a>' for name in rows)
        html = html.replace('<nav aria-label="Результаты">', f'<nav aria-label="Результаты"><p>{description}</p>'
                            f'<p>{escape(notes)}</p><p>{links}</p>')
        (folder / "index.html").write_text(html, encoding="utf-8")
        (folder / "PASSPORT.md").write_text(
            "# Частичная публикация графика\n\n"
            + notes + "\n\nUTF-8 CSV, decimal point. Empty means unspecified. "
            "IDs are local to publication. One y value is an EM result; x is a configuration parameter. "
            "raw_value starts with ~ for geometric reading. Units are unchanged. "
            "Source quotes locate the figure; visual review is required for the point. "
            "Conditions are explicitly reviewed in reviewed-context.json. "
            "run.json and CSV refer to the same version. Schema columns are in schema.json.\n",
            encoding="utf-8",
        )
        with tempfile.TemporaryDirectory(prefix="plot-check-") as check_folder:
            shutil.copyfile(folder / "source.pdf", Path(check_folder) / (pid + ".pdf"))
            checked = subprocess.run(
                [sys.executable, str(checker), str(folder), check_folder,
                 "--json-report", str(folder / "check-report.json")],
                capture_output=True, text=True,
            )
        if checked.returncode not in (0, 2):
            raise ValueError("Submission check failed: " + checked.stdout + checked.stderr)
        report = json.loads((folder / "check-report.json").read_text())
        if report["errors"]:
            raise ValueError("Submission checker reported structural errors.")
        status = "Проверка формата и цитат пройдена" if checked.returncode == 0 else "Проверка требует внимания: см. отчёт"
        html = (folder / "index.html").read_text(encoding="utf-8")
        html = html.replace('<nav aria-label="Результаты">', '<nav aria-label="Результаты">'
                            '<p>Частичная публикация: одна точка Figure 3(a), не вся статья. '
                            'Метод, задача и смысл осей проверены вручную. Генератор не установлен.</p>'
                            f'<p>{status}. <a href="check-report.json">Отчёт проверки</a> · '
                            '<a href="PASSPORT.md">Паспорт</a> · <a href="run.json">Запись JSON</a> · '
                            '<a href="reviewed-context.json">Проверенные условия</a></p>')
        (folder / "index.html").write_text(html, encoding="utf-8")
        manifest = {"version_id": version, "publication_id": pid, "pdf_sha256": plot["sha256"],
                    "inputs": inputs,
                    "files": {path.name: digest(path) for path in folder.iterdir()}}
        write_json(folder / "manifest.json", manifest)
        folder.rename(target)
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plot", type=Path, required=True)
    parser.add_argument("--context", type=Path, required=True)
    parser.add_argument("--pdf", type=Path, required=True)
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    try:
        target = publish(args.plot, args.context, args.pdf, args.template, args.out)
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(1, f"{error}\n")
    print(target)


if __name__ == "__main__":
    main()
