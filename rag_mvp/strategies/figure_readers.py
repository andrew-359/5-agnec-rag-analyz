"""Machine-readable catalogue and selection validation for figure readers."""

import argparse
import hashlib
import json
import math
from pathlib import Path

import pdfplumber
from jsonschema import Draft202012Validator

from ..inventory import normalized_box
from .plot_extract import BAR_PANEL_MARGINS, PLOT_READERS, find_plot_frames
from ..run import IncompatibleArtifact


def catalogue():
    return {"readers": [
        {"stage": "figure", "id": identifier, "input": "figure-region", "description": reader.help,
         "parameters_schema": reader.parameters_schema}
        for identifier, reader in PLOT_READERS.items()
    ]}


def prepare_target(pdf, inventory, candidate_id):
    """Freeze the existing frame detector's input geometry independently of a reader."""
    with pdf.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    if inventory.get("schema_version") != 1 or inventory.get("sha256") != digest:
        raise IncompatibleArtifact("Карта статьи относится к другой версии PDF или имеет неизвестный формат.")
    matches = [item for item in inventory["candidates"] if item["kind"] == "figure" and item["id"] == candidate_id]
    if len(matches) != 1:
        raise IncompatibleArtifact(f"На карте нет единственного рисунка с адресом {candidate_id}.")
    candidate = matches[0]
    target = {"stage": "figure", "candidate_id": candidate_id, "page": candidate["page"],
              "figure": candidate["label"], "box": candidate["box"]}
    with pdfplumber.open(pdf) as document:
        if not 1 <= candidate["page"] <= len(document.pages):
            raise IncompatibleArtifact("Страница рисунка отсутствует в PDF.")
        page = document.pages[candidate["page"] - 1]
        try:
            frames = find_plot_frames(page, candidate["label"]) if not page.rotation else []
        except ValueError:
            frames = []
        panels = []
        margin_left, margin_top, margin_right, margin_bottom = BAR_PANEL_MARGINS
        # Line readers already need 32 points for the y-axis labels.
        margin_left = max(32, margin_left)
        margin_right = max(8, margin_right)
        for index, (left, top, right, bottom) in enumerate(frames, 1):
            box = [max(left - margin_left, 0), max(top - margin_top, 0),
                   min(right + margin_right, page.width), min(bottom + margin_bottom, page.height)]
            panels.append({"panel": str(index), "panel_box_pt": box, "plot_box_pt": [left, top, right, bottom]})
        if panels:
            boxes = [panel["panel_box_pt"] for panel in panels]
            bounds = {"x0": min(box[0] for box in boxes), "top": min(box[1] for box in boxes),
                      "x1": max(box[2] for box in boxes), "bottom": max(box[3] for box in boxes)}
            region = {"box": normalized_box(bounds, page.width, page.height), "precision": "figure", "panels": panels}
        else:
            region = {"box": [0, 0, 1, 1], "precision": "page", "panels": []}
    return {**target, "region": region}


def _finite_numbers(value):
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("Figure reader parameters must contain only finite numbers.")
    if isinstance(value, dict):
        for item in value.values():
            _finite_numbers(item)
    elif isinstance(value, list):
        for item in value:
            _finite_numbers(item)


def validate_selection(selection):
    if (not isinstance(selection, dict) or set(selection) != {"mode", "strategy_id", "parameters"}
            or selection.get("mode") != "explicit"):
        raise ValueError("Figure selection requires mode=explicit, strategy_id and parameters.")
    identifier = selection["strategy_id"]
    if not isinstance(identifier, str) or identifier not in PLOT_READERS:
        raise ValueError(f"Unknown figure reader: {identifier!r}.")
    parameters = selection["parameters"]
    _finite_numbers(parameters)
    errors = sorted(Draft202012Validator(PLOT_READERS[identifier].parameters_schema).iter_errors(parameters),
                    key=lambda error: str(list(error.path)))
    if errors:
        error = errors[0]
        address = ".".join(str(part) for part in error.path) or "parameters"
        raise ValueError(f"Invalid figure reader {identifier} {address}: {error.message}")
    return identifier, parameters


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare-target", type=Path, metavar="PDF")
    parser.add_argument("--inventory", type=Path)
    parser.add_argument("--candidate")
    args = parser.parse_args()
    try:
        if args.prepare_target is not None:
            if args.inventory is None or args.candidate is None:
                parser.error("--prepare-target requires --inventory and --candidate")
            inventory = json.loads(args.inventory.read_bytes())
            payload = prepare_target(args.prepare_target, inventory, args.candidate)
        else:
            if args.inventory is not None or args.candidate is not None:
                parser.error("--inventory and --candidate require --prepare-target")
            payload = catalogue()
    except IncompatibleArtifact as error:
        parser.exit(3, f"{error}\n")
    except (ValueError, OSError) as error:
        parser.exit(1, f"{error}\n")
    print(json.dumps(payload, ensure_ascii=False))
