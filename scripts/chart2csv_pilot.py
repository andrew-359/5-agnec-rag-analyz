"""Run and score the local Docling Chart2CSV pilot for D001 Figure 5."""

import argparse
import csv
from decimal import Decimal, InvalidOperation
import hashlib
from importlib.metadata import version
import io
import json
import logging
import os
from pathlib import Path
import platform
import re
import shlex
import sys
import tempfile
import time


PRESET = "granite_vision_v3_3"
SERIES = {
    "KG-Triplets": "KG-Triplets",
    "KG-Texts": "KG-Texts",
    "KG-Paths": "KG-Paths",
    "STG-Paths": "STG-Paths",
    "STG-Paths (Ours)": "STG-Paths",
}
DATASETS = {
    "HotpotQA": "HotpotQA",
    "2Wiki": "2WikiMultihopQA",
    "2WikiMultihopQA": "2WikiMultihopQA",
    "MuSiQue": "MuSiQue",
}


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def extract(args):
    from docling.datamodel.accelerator_options import AcceleratorOptions, AcceleratorDevice
    from docling.datamodel.chart_extraction_options import ChartExtractionVlmEngineOptions
    from docling.datamodel.vlm_engine_options import TransformersVlmEngineOptions
    from docling.models.inference_engines.vlm import VlmEngineInput, create_vlm_engine
    from huggingface_hub import snapshot_download, try_to_load_from_cache
    import pypdfium2
    import torch

    args.out.mkdir(parents=True, exist_ok=False)
    device = AcceleratorDevice(args.device)
    engine_options = TransformersVlmEngineOptions(
        device=device, torch_dtype=args.dtype, quantized=False, compile_model=False,
        trust_remote_code=False,
    )
    options = ChartExtractionVlmEngineOptions.from_preset(PRESET, engine_options=engine_options)
    model = options.model_spec
    with args.pdf.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    source = {
        "pdf": str(args.pdf.resolve()), "sha256": digest, "page": args.page,
        "pdf_link": f"{args.pdf.resolve().as_uri()}#page={args.page}",
        "figure": "Figure 5", "bbox": args.bbox, "coordinates": "pdf_points_top_left",
        "precision": "figure_region", "preview": "figure-5.png",
    }
    attempt = {
        "status": "running", "stage": "crop", "source": source, "preset": PRESET,
        "command": shlex.join([sys.executable, *sys.argv]), "scale": args.scale,
        "torch_threads": args.threads, "offline": os.environ.get("HF_HUB_OFFLINE") == "1",
        "model": model.model_dump(mode="json"), "engine": engine_options.model_dump(mode="json"),
        "versions": {"python": platform.python_version()} | {
            name: version(name)
            for name in ("docling", "docling-core", "transformers", "torch", "pypdfium2", "huggingface-hub")
        },
    }
    manifest = args.out / "attempt.json"
    write_json(manifest, attempt)
    start = time.monotonic()
    try:
        with pypdfium2.PdfDocument(args.pdf) as document:
            if not 1 <= args.page <= len(document):
                raise ValueError(f"Page {args.page} is outside the PDF")
            page = document[args.page - 1]
            width, height = page.get_size()
            left, top, right, bottom = args.bbox
            if not (0 <= left < right <= width and 0 <= top < bottom <= height):
                raise ValueError(f"Bounding box is outside {width} × {height} pt")
            bitmap = page.render(scale=args.scale, crop=(left, height - bottom, width - right, top))
            image = bitmap.to_pil().convert("RGB")
        image.save(args.out / source["preview"])
        attempt["input_pixels"] = list(image.size)
        attempt["stage"] = "model_weights"
        write_json(manifest, attempt)
        repo, revision = model.default_repo_id, model.revision
        index = "model.safetensors.index.json"
        if attempt["offline"]:
            cached_index = try_to_load_from_cache(repo, index, revision=revision)
            if not isinstance(cached_index, str):
                raise FileNotFoundError(f"Missing {repo}@{revision} / {index} in the local cache")
            snapshot = Path(cached_index).parent
        else:
            snapshot = Path(snapshot_download(
                repo, revision=revision, allow_patterns=["*.json", "*.jinja", "*.txt", "*.safetensors"],
            ))
        weights = set(json.loads((snapshot / index).read_text(encoding="utf-8"))["weight_map"].values())
        missing = sorted(name for name in weights if not (snapshot / name).is_file())
        if missing:
            raise FileNotFoundError(f"Missing {repo}@{revision} weights: {', '.join(missing)}")

        attempt["stage"] = "model_load"
        write_json(manifest, attempt)
        torch.set_num_threads(args.threads)
        with tempfile.TemporaryDirectory(prefix="chart2csv-model-") as directory:
            artifacts = Path(directory)
            (artifacts / repo.replace("/", "--")).symlink_to(snapshot.resolve(), target_is_directory=True)
            engine = create_vlm_engine(
                options=engine_options, model_spec=model, artifacts_path=artifacts,
                accelerator_options=AcceleratorOptions(device=device, num_threads=args.threads),
                enable_remote_services=False,
            )
            attempt["stage"] = "inference"
            write_json(manifest, attempt)
            request = VlmEngineInput(
                image=image, prompt=model.prompt, temperature=model.temperature,
                max_new_tokens=model.max_new_tokens, stop_strings=list(model.stop_strings),
                extra_generation_config=model.get_runtime_input_extra_config(engine_options.engine_type),
            )
            prediction, = engine.predict_batch([request])
        (args.out / "raw-output.txt").write_text(prediction.text, encoding="utf-8")
        attempt.update(status="completed", stop_reason=prediction.stop_reason)
    except Exception as error:
        attempt.update(status="failed", error={"type": type(error).__name__, "message": str(error)})
        raise
    finally:
        attempt["elapsed_seconds"] = round(time.monotonic() - start, 3)
        write_json(manifest, attempt)


def decimal(value):
    try:
        number = Decimal(value)
        return number if number.is_finite() else None
    except InvalidOperation:
        return None


def chart_records(raw, source):
    fenced = re.search(r"```(?:csv)?[ \t]*\n(.*?)```", raw, re.DOTALL | re.IGNORECASE)
    table = fenced.group(1) if fenced else raw
    # The Docling preset normally emits comma-separated data. A semicolon or
    # tab can also delimit an otherwise unmodified model table.
    try:
        delimiter = csv.Sniffer().sniff(table, delimiters=",;\t").delimiter
    except csv.Error:
        delimiter = ","
    rows = [row for row in csv.reader(io.StringIO(table), delimiter=delimiter) if any(row)]
    if len(rows) < 2:
        return [], [{"reason": "no_table", "source": source}]
    headers = [label.strip() for label in rows[0][1:]]
    row_names = [row[0].strip() for row in rows[1:]]
    series_columns = sum(label in SERIES for label in headers) + sum(label in DATASETS for label in row_names)
    dataset_columns = sum(label in DATASETS for label in headers) + sum(label in SERIES for label in row_names)
    if series_columns == dataset_columns:
        return [], [{"reason": "unclear_table_axes", "headers": headers, "rows": row_names, "source": source}]

    records = []
    remarks = []
    for row_number, row in enumerate(rows[1:], start=2):
        for col_number, value in enumerate(row[1:], start=2):
            if not value.strip():
                continue
            row_label = row[0].strip()
            col_label = headers[col_number - 2] if col_number - 2 < len(headers) else ""
            raw_series, raw_dataset = (col_label, row_label) if series_columns > dataset_columns else (row_label, col_label)
            observation = {
                "row": row_number, "column": col_number, "source": source,
                "metric": "EM", "raw_series": raw_series, "raw_dataset": raw_dataset,
                "raw_value": value,
            }
            number = decimal(value.strip())
            if number is None:
                remarks.append({"reason": "invalid_number", **observation})
                continue
            records.append({
                **observation, "series": SERIES.get(raw_series, raw_series),
                "dataset": DATASETS.get(raw_dataset, raw_dataset), "value": str(number),
            })
    return records, remarks


def gold_records(folder):
    def d001_rows(name):
        with (folder / f"{name}.csv").open(encoding="utf-8-sig", newline="") as stream:
            return [row for row in csv.DictReader(stream) if row["publication_id"] == "D001"]

    series = {row["config_id"]: row["rag_variant"].removeprefix("ThreadRAG representation: ")
              for row in d001_rows("configurations")}
    datasets = {row["dataset_id"]: row["dataset_name"] for row in d001_rows("datasets")}
    return {
        (series[row["config_id"]], datasets[row["dataset_id"]]): {
            "id": row["result_id"], "value": row["normalized_value"],
            "series": series[row["config_id"]], "dataset": datasets[row["dataset_id"]],
        }
        for row in d001_rows("results")
        if row["source_figure"] == "Figure 5" and row["metric_name"] == "EM"
        and row["result_id"] in {f"R{index}" for index in range(354, 366)}
    }


def compare(args):
    source = json.loads((args.run / "attempt.json").read_text(encoding="utf-8"))["source"]
    raw = (args.run / "raw-output.txt").read_text(encoding="utf-8")
    records, remarks = chart_records(raw, source)
    write_json(args.run / "matched-values.json", records)
    expected = gold_records(args.gold)
    reported = {}
    results = {"matched": [], "missing": [], "extra": [], "value_errors": [], "binding_errors": [], "ambiguous": remarks}
    for record in records:
        key = (record["series"], record["dataset"])
        if key in expected:
            reported.setdefault(key, []).append(record)
        else:
            results["extra"].append({"reason": "unknown_pair", "observed": record})
    for pair, gold in expected.items():
        observed = reported.get(pair, [])
        if not observed:
            results["missing"].append({"expected": gold, "source": source})
        elif len(observed) > 1:
            results["ambiguous"].append({"reason": "duplicate_pair", "expected": gold, "observed": observed})
        else:
            record, = observed
            value = Decimal(record["value"])
            entry = {"expected": gold, "observed": record}
            candidates = [row["id"] for key, row in expected.items()
                          if key != pair and Decimal(row["value"]) == value]
            if value == Decimal(gold["value"]):
                results["matched"].append(entry)
            elif len(candidates) == 1:
                results["binding_errors"].append({**entry, "value_would_match": candidates})
            elif candidates:
                results["ambiguous"].append({**entry, "reason": "multiple_candidate_pairs", "value_would_match": candidates})
            else:
                results["value_errors"].append(entry)
    counts = {"expected": len(expected)} | {key: len(items) for key, items in results.items()}
    write_json(args.run / "comparison.json", {"counts": counts, "gold": str(args.gold.resolve()), **results})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(required=True)
    run = commands.add_parser("extract")
    run.add_argument("pdf", type=Path)
    run.add_argument("--out", type=Path, required=True)
    run.add_argument("--page", type=int, required=True)
    run.add_argument("--bbox", type=float, nargs=4, required=True, metavar=("LEFT", "TOP", "RIGHT", "BOTTOM"))
    run.add_argument("--scale", type=float, default=3)
    run.add_argument("--device", choices=("cpu", "cuda"), default="cpu")
    run.add_argument("--dtype", choices=("float32", "bfloat16", "float16"), default="float32")
    run.add_argument("--threads", type=int, default=8)
    run.set_defaults(action=extract)
    score = commands.add_parser("compare")
    score.add_argument("run", type=Path)
    score.add_argument("--gold", type=Path, required=True)
    score.set_defaults(action=compare)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    args.action(args)


if __name__ == "__main__":
    main()
