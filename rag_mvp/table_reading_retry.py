"""Combine two interpretations of the same, independently read PDF region."""

from collections import Counter
from decimal import Decimal

from .core.domain import Box, Collection, Result
from .run import TableRead


REREAD_SOURCE_PREFIX = "reread-"


def _overlaps(a: Box | None, b: Box | None) -> bool:
    if a is None or b is None:
        return False
    width = min(a[2], b[2]) - max(a[0], b[0])
    height = min(a[3], b[3]) - max(a[1], b[1])
    return width > 0 and height > 0 and width * height >= 0.5 * min(
        (a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1]),
    )


def _measurement(row: dict) -> tuple:
    return (
        Decimal(row["value"]), row["unit"], row["method"], row["generator"], row["dataset"], row["metric"],
        row["task"], row["experiment"], row["retrieval_top_k"], row["components"],
    )


def combine_readings(
    first: tuple[str, TableRead, Collection], second: tuple[str, TableRead, Collection], page: int,
) -> tuple[tuple[Result, ...], dict, dict]:
    primary_strategy, primary, primary_collection = first
    retry_strategy, retry, retry_collection = second
    base = primary.serialize(primary_collection, page, strategy=primary_strategy)
    alternative = retry.serialize(retry_collection, page, strategy=retry_strategy, source_prefix=REREAD_SOURCE_PREFIX)
    before, after = base["results"], alternative["results"]
    retry_evidence = {item["id"]: item for item in alternative["evidence"]}

    # Row and column numbers are local to each reader. Compare the boxes on the original PDF and keep statistics separate.
    matches = [
        [i for i, old in enumerate(before) if old["statistic"] == new["statistic"]
         and _overlaps(old["source"]["box"], new["source"]["box"])]
        for new in after
    ]
    match_counts = Counter(index for indices in matches for index in indices)
    discarded = set()
    recovered = []
    conflicts = []
    for index, (new, indices) in enumerate(zip(after, matches, strict=True)):
        if not indices:
            source = new["source"]
            fragment = retry_evidence[source["evidence_ids"][0]]
            if source["box"] and fragment["precision"] in ("cell", "text_fragment") and fragment["box"] == source["box"]:
                recovered.append(index)
            continue
        if len(indices) == 1 and match_counts[indices[0]] == 1 and _measurement(before[indices[0]]) == _measurement(new):
            continue
        discarded.update(indices)
        rows = [*(before[i] for i in indices), new]
        options = [{
            "value": row["raw_value"], "evidence_ids": row["source"]["evidence_ids"],
            "binding": [row[field] for field in ("method", "generator", "dataset", "metric")],
            "retrieval_top_k": row["retrieval_top_k"],
            "condition_sources": {field: refs for field, refs in row["condition_sources"].items()
                                  if field == "retrieval_top_k"},
        } for row in rows]
        source = {**rows[0]["source"],
                  "evidence_ids": list(dict.fromkeys(ref for option in options for ref in option["evidence_ids"]))}
        conflicts.append({
            "reason": "Чтения одной ячейки расходятся по значению или привязке.",
            "kind": "reading-conflict", "raw_text": " / ".join(option["value"] for option in options),
            "source": source, "options": options,
        })

    accepted = [(0, index) for index in range(len(before)) if index not in discarded]
    accepted.extend((1, index) for index in recovered)
    collections = (primary_collection, retry_collection)
    artifacts = (base, alternative)
    result_ids = ({}, {})
    results = []
    for origin, index in accepted:
        item = artifacts[origin]["results"][index]
        result_id = f"result-{len(results)}"
        result_ids[origin][item["id"]] = result_id
        results.append({**item, "id": result_id})
    remarks = []
    other_accepted = (
        [after[index] for index in recovered],
        [before[index] for index in range(len(before)) if index not in discarded],
    )
    for artifact, refs, other_rows in zip(artifacts, result_ids, other_accepted, strict=True):
        for remark in artifact["remarks"]:
            if remark["kind"] == "reading-defect" and any(
                _overlaps(remark["source"]["box"], row["source"]["box"])
                and (remark.get("statistic") is None or remark["statistic"] == row["statistic"])
                for row in other_rows
            ):
                continue
            item = {**remark, "id": f"remark-{len(remarks)}"}
            if "result_ids" in item:
                item["result_ids"] = [refs[ref] for ref in item["result_ids"] if ref in refs]
            remarks.append(item)
    first_conflict = len(remarks)
    remarks.extend({**remark, "id": f"remark-{first_conflict + index}"} for index, remark in enumerate(conflicts))
    reading = {
        "primary_strategy": primary_strategy, "retry_strategy": retry_strategy,
        "recovered": len(recovered), "preserved": len(before) - len(discarded), "conflicts": len(conflicts),
    }
    evidence = {item["id"]: item for artifact in artifacts for item in artifact["evidence"]}
    combined = {
        **base,
        "evidence": list(evidence.values()),
        "reread_grid": {"grid": alternative["grid"], "cells": alternative["cells"]},
        "context_evidence_ids": list(dict.fromkeys((*base["context_evidence_ids"], *alternative["context_evidence_ids"]))),
        "results": results, "remarks": remarks,
    }
    return tuple(collections[origin].results[index] for origin, index in accepted), combined, reading
