"""Record full metric-mismatch addresses and build the paired dev/P020 score input."""
import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path

from entities import COMPOUND, DELTA, DESCRIPTIVE, SCALAR, clean, passport, sources_of_lexicon
from extract import caption_title, headers_for, load_csv, table_records, write_json
from prepare import digest, read, saved_table
from score_dev_gold import address, number_keys, table_key, tabular


def p020_control(args):
    grid, article_text = read(args.grid), read(args.article_text)
    sha = digest(args.pdf)
    if {grid["sha256"], article_text["sha256"]} != {sha}:
        raise ValueError("P020 grid and text must come from the supplied PDF")
    table = saved_table(args.grid)
    if not table or (table["page"], table["label"]) != (5, "1"):
        raise ValueError("Expected a populated P020 page 5 / Table 1")
    text = article_text["fragments"]
    metrics = sources_of_lexicon(load_csv(args.gold / "results.csv"), ("metric_name",))
    datasets = sources_of_lexicon(load_csv(args.gold / "datasets.csv"), ("dataset_name",))
    entities = passport({"doc": "P020", "text": text}, metrics, datasets)
    records = list(table_records(table, entities, text, pdf_path=args.pdf, pdf_sha=sha))
    write_json(args.output, {"read_tables": [[5, 1]], "grid_sha256": digest(args.grid),
                            "text_sha256": digest(args.article_text), "cells": len(table["cells"]),
                            "predictions": records})


def mismatches(gold, bundle):
    expected = defaultdict(list)
    for row in tabular(load_csv(gold / "results.csv")):
        expected[(row["publication_id"], *address(row)[:2])].append(row)
    with (bundle / "provenance.jsonl").open(encoding="utf-8") as stream:
        provenance = {(row["publication_id"], row["result_id"]): row for row in map(json.loads, stream)}
    groups = {}
    for row in tabular(load_csv(bundle / "results.csv")):
        page, table, identity = address(row)
        doc = row["publication_id"]
        other = expected[(doc, page, table)]
        if not other or any(address(candidate)[2] == identity for candidate in other):
            continue
        observed = provenance[(doc, row["result_id"])]
        metric = observed["fields"]["metric"]
        anchors = tuple(item["text"] for item in metric["sources"] if item["precision"] == "cell")
        key = (doc, page, table, row["metric_name"], metric["rule"], anchors)
        group = groups.setdefault(key, {"article": doc, "page": page, "table": table,
                                       "metric": row["metric_name"], "rule": metric["rule"],
                                       "grid_labels": anchors, "rows": []})
        candidates = [candidate for candidate in other if number_keys(row) & number_keys(candidate)]
        group["rows"].append({"result_id": row["result_id"], "cell": [observed["row"], observed["column"]],
                              "raw_value": row["raw_value"], "metric_sources": metric["sources"],
                              "same_number_candidates": [{"id": item["result_id"], "metric": item["metric_name"],
                                                          "cutoff": item["metric_k"], "unit": item["metric_unit"]}
                                                         for item in candidates]})
    for group in groups.values():
        group["count"] = len(group["rows"])
    return sorted(groups.values(), key=lambda group: (-group["count"], group["article"]))


def bundle_rows(bundle):
    configs = {(row["publication_id"], row["config_id"]): row
               for row in load_csv(bundle / "configurations.csv")}
    datasets = {(row["publication_id"], row["dataset_id"]): row["dataset_name"]
                for row in load_csv(bundle / "datasets.csv")}
    rows = defaultdict(list)
    for row in tabular(load_csv(bundle / "results.csv")):
        doc = row["publication_id"]
        config = configs.get((doc, row["config_id"]), {})
        rows[doc].append({**row, "method": config.get("rag_variant"), "generator": config.get("generator_model"),
                          "dataset": datasets.get((doc, row["dataset_id"]))})
    return rows


def compare(args):
    baseline, updated = map(bundle_rows, (args.before, args.after))
    old_control, new_control = map(read, (args.p020_before, args.p020_after))
    for key in ("grid_sha256", "text_sha256", "read_tables", "cells"):
        if old_control[key] != new_control[key]:
            raise ValueError(f"Changed P020 input: {key}")
    old_docs = set(read(args.before / "manifest.json")["articles"])
    new_docs = set(read(args.after / "manifest.json")["articles"])
    if old_docs != new_docs or len(old_docs) != 21:
        raise ValueError("Expected the same 21 articles in both bundles")
    paired = [{"article": doc, "baseline": baseline[doc], "updated": updated[doc]} for doc in sorted(old_docs)]
    paired.append({"article": "P020", "baseline": old_control["predictions"], "updated": new_control["predictions"],
                   "read_tables": old_control["read_tables"]})
    write_json(args.output, paired)
    write_json(args.mismatches, mismatches(args.gold, args.before))


def empty_tables(args):
    gold = defaultdict(list)
    gold_rows = load_csv(args.gold / "results.csv")
    for row in tabular(gold_rows):
        gold[(row["publication_id"], *address(row)[:2])].append(row)
    published = {(row["publication_id"], *address(row)[:2])
                 for row in tabular(load_csv(args.bundle / "results.csv"))}
    metric_docs = sources_of_lexicon(gold_rows, ("metric_name",))
    dataset_docs = sources_of_lexicon(load_csv(args.gold / "datasets.csv"), ("dataset_name",))
    diagnostics = []
    for path in sorted(args.inputs.glob("D*.json")):
        record = read(path)
        entities = passport(record, metric_docs, dataset_docs)
        for table in table_addresses(record["tables"]):
            page, label = str(table["page"]), table_key(table["label"])
            key = record["doc"], page, label
            if key not in gold or key in published:
                continue
            caption = table["caption"]
            title = caption_title(caption)
            cells = table["cells"]
            positioned = {item["id"] for item in table["evidence"] if item.get("box")}
            numeric = []
            if cells:
                headers = headers_for(cells, entities)
                left = min(cell["column"] for cell in cells)
                numeric = [[cell["row"], cell["column"], cell["text"]] for cell in cells
                           if cell not in headers and cell["column"] > left
                           and positioned.intersection(cell["evidence_ids"])
                           and any(pattern.fullmatch(clean(cell["text"]))
                                   for pattern in (SCALAR, COMPOUND, DELTA))]
            if DESCRIPTIVE.search(title):
                gate = "descriptive-caption-rule"
            elif not numeric:
                gate = "no-addressed-numeric-body"
            elif not title or not caption.get("box"):
                gate = "numbers-but-no-substantive-addressed-caption"
            else:
                gate = "numbers-and-caption-but-no-output"
            diagnostics.append({"article": key[0], "page": page, "table": label, "gate": gate,
                                "gold_rows": len(gold[key]), "reader": table["reader"], "caption": caption,
                                "numeric_cells": numeric,
                                "expected_metrics": dict(Counter(row["metric_name"] for row in gold[key]))})
    totals = defaultdict(lambda: {"tables": 0, "gold_rows": 0})
    for table in diagnostics:
        totals[table["gate"]]["tables"] += 1
        totals[table["gate"]]["gold_rows"] += table["gold_rows"]
    write_json(args.output, {"gates": dict(totals), "tables": diagnostics})


def table_addresses(tables):
    seen = set()
    for table in tables:
        key = table["page"], table_key(table["label"])
        if key not in seen:
            seen.add(key)
            yield table


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(required=True)
    control = commands.add_parser("control")
    for name in ("pdf", "grid", "article_text", "gold", "output"):
        control.add_argument(name, type=Path)
    control.set_defaults(run=p020_control)
    comparison = commands.add_parser("compare")
    for name in ("before", "after", "p020_before", "p020_after", "gold", "output", "mismatches"):
        comparison.add_argument(name, type=Path)
    comparison.set_defaults(run=compare)
    empty = commands.add_parser("empty")
    for name in ("inputs", "bundle", "gold", "output"):
        empty.add_argument(name, type=Path)
    empty.set_defaults(run=empty_tables)
    args = parser.parse_args()
    args.run(args)
