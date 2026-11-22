"""Compare the experimental output with rules and a manually scoped control."""

from dataclasses import asdict, replace
from hashlib import sha256
import json

from .article_context.assertions import extract_condition_assertions
from .article_table_binding import bind_article_context, scoped_retrieval
from .core.domain import TableTarget
from .table_grid_artifact import load_table_grid
from .table_region import TableRegion


def baseline_for(document, fragments, target, grid_path):
    extracted = extract_condition_assertions(document.sha256, fragments)
    retrieval, _ = scoped_retrieval(fragments, target)
    baseline = {
        "assertions": [asdict(item) for item in extracted.assertions if item.field != "dataset"]
        + [asdict(item) for item in retrieval],
    }
    if grid_path is None:
        return baseline

    grid_bytes = grid_path.read_bytes()
    try:
        data = json.loads(grid_bytes)
        region = TableRegion.parse(data["region"]) if "region" in data else None
        read = load_table_grid(grid_path, document.sha256, target.page, target.number, data["strategy"], region=region)
    except (KeyError, TypeError) as error:
        raise ValueError("Некорректный артефакт сетки таблицы.") from error
    if grid_bytes != grid_path.read_bytes():
        raise ValueError("Сохранённая сетка изменилась во время чтения.")
    selected = replace(document, pages=tuple(sorted({item.page for item in fragments})), fragments=fragments)
    _, context = bind_article_context(selected, document.sha256, read, region=region)
    baseline["grid"] = {"path": str(grid_path.resolve()), "sha256": sha256(grid_bytes).hexdigest(),
                        "strategy": data["strategy"]}
    baseline["bound_assertions"] = (
        [asdict(item) for item in context.assertions if item.field in ("generator", "retrieval_top_k")]
        if context is not None else []
    )
    return baseline


def normalize(value):
    return " ".join(value.split()).casefold() if isinstance(value, str) else value


def compare(baseline, proposals, conditions, path):
    assertions = [*baseline["assertions"], *baseline.get("bound_assertions", [])]
    checks = [review_proposal(proposal, conditions) for proposal in proposals]
    controls = []
    for index, condition in enumerate(conditions):
        matches = [number for number, check in enumerate(checks)
                   if index in check.get("matched_controls", [])]
        field = condition["field"]
        baseline_match = any(
            assertion["field"] in (field, "generator" if field == "generator_model" else field)
            and normalize(assertion["value"]) == normalize(condition["value"])
            and normalize(assertion["scope"]) == normalize(condition["scope"])
            for assertion in assertions
        )
        controls.append({"condition": condition, "baseline_match": baseline_match, "matching_proposals": matches})
    return {
        "control": str(path.resolve()) if path is not None else None,
        "conditions": controls,
        "proposal_checks": checks,
    }


def review_proposal(proposal, conditions):
    if proposal["status"] != "unreviewed":
        return {"status": proposal["status"]}
    same_field = [(index, item) for index, item in enumerate(conditions) if item["field"] == proposal["field"]]
    same_scope = [(index, item) for index, item in same_field
                  if normalize(item["scope"]) == normalize(proposal["scope"])]
    same_value = [(index, item) for index, item in same_field
                  if normalize(item["value"]) == normalize(proposal["value"])]
    same_claim = [(index, item) for index, item in same_scope if (index, item) in same_value]
    if same_claim:
        supported = [
            index for index, item in same_claim
            if all(any(cite["fragment_id"] == expected["fragment_id"] and expected["quote"] in cite["quote"]
                       for cite in proposal["citations"]) for expected in item["required_citations"])
        ]
        status = "matches_control" if supported else "insufficient_citations"
        return {"status": status, "matched_controls": supported}
    if same_scope and same_value:
        return {"status": "value_scope_mismatch"}
    if same_scope:
        return {"status": "value_mismatch"}
    if same_value:
        return {"status": "scope_mismatch"}
    return {"status": "open"}
