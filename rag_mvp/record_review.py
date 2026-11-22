"""Recheck a selected table remark on its saved grid and source fragments."""

import argparse
import json
from dataclasses import asdict, replace
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from .strategies.text_pdfplumber import load_article_fragments
from .article_context.selection import CONTEXT_ARTIFACT, PreparedTableContext, load_table_context
from .core.domain import TableTarget
from .reconciliation import report
from .run import TableRead
from .run_output import write_json
from .table_extraction import interpret_grid
from .table_grid_artifact import load_table_grid
from .table_region import TableRegion


def locate(items: list[dict], key: str, value: str, label: str) -> dict:
    matches = [item for item in items if item[key] == value]
    if len(matches) != 1:
        raise ValueError(f"Не удалось однозначно найти {label} {value}.")
    return matches[0]


def cell_address(row: dict) -> tuple:
    source = row["source"]
    return tuple(source.get(field) for field in ("page", "table", "row", "column", "cell_id"))


def citation_ids(row: dict) -> set[str]:
    direct = row["source"].get("evidence_ids", [])
    conditions = (row.get("condition_sources") or {}).values()
    alternatives = (item["evidence_ids"] for item in row.get("options", ()))
    return {*direct, *(ref for refs in (*conditions, *alternatives) for ref in refs)}


def include_sources(existing: list[dict], additions: list[dict], needed: set[str]) -> dict[str, dict]:
    evidence = {item["id"]: item for item in existing}
    for item in additions:
        ref = item["id"]
        if ref in needed:
            if ref in evidence and evidence[ref] != item:
                raise ValueError(f"Изменился сохранённый источник {ref}.")
            evidence[ref] = item
    if missing := needed.difference(evidence):
        raise ValueError(f"Отсутствуют сохранённые источники: {', '.join(sorted(missing))}.")
    return evidence


def serialize_selection(grid: TableRead, selection: PreparedTableContext, context_path: Path) -> dict:
    prepared, collected, _ = interpret_grid(grid, None, None, selection, context_path)
    return json.loads(json.dumps(prepared.serialize(collected, selection.target.page)))


def same_remark(observed: dict, original: dict) -> bool:
    fields = ("kind", "field", "raw_text", "source", "conditions", "condition_sources", "options")
    return all(observed.get(field) == original.get(field) for field in fields)


def isolate_cell(grid: TableRead, row: int, column: int) -> TableRead:
    if grid.table is None:
        raise ValueError("В сохранённом чтении отсутствует таблица.")
    cells = tuple(item for item in grid.table.cells if (item.row, item.column) == (row, column))
    if len(cells) != 1:
        raise ValueError("В сохранённой сетке не найдена выбранная ячейка.")
    return replace(grid, table=replace(grid.table, cells=cells))


def context_payload(previous: dict, artifact_id: str, path: Path | None) -> bytes:
    saved = [item["data"] for item in previous.get("contexts", []) if item["artifact_id"] == artifact_id]
    if len(saved) > 1:
        raise ValueError("В итоге сохранены разные подборки для выбранной таблицы.")
    if path is None:
        if not saved:
            raise ValueError("Подборка отсутствует в итоге. Укажите --context-input.")
        return json.dumps(saved[0], ensure_ascii=False).encode("utf-8")
    payload = path.read_bytes()
    if saved and saved[0] != json.loads(payload):
        raise ValueError("Подборка отличается от сохранённого входа прогона.")
    return payload


def update_record(data: dict, remark: dict, candidate: dict, result: dict | None) -> dict:
    findings = [item for item in candidate["remarks"] if cell_address(item) == cell_address(remark)
                and item["kind"] == remark["kind"] and item["field"] == remark["field"]]
    if result is None and len(findings) != 1:
        return data
    replacement = result if result is not None else {**findings[0], "id": remark["id"]}
    remaining = [item for item in data["remarks"] if result is None or item["id"] != remark["id"]]
    remarks = [replacement if item["id"] == remark["id"] else item for item in remaining]
    results = [*data["results"], result] if result is not None else data["results"]
    evidence = include_sources(data["evidence"], candidate["evidence"], citation_ids(replacement))
    return {**data, "remarks": remarks, "results": results, "evidence": list(evidence.values())}


def review(args: argparse.Namespace) -> None:
    if args.out.exists():
        raise ValueError(f"Каталог проверки уже существует: {args.out}")

    payload = args.reconciliation.read_bytes()
    previous = json.loads(payload)
    if type(previous["schema_version"]) is not int or previous["schema_version"] != 1:
        raise ValueError("Неподдерживаемая версия итога сверки.")
    pdf_hash = previous["source"]["sha256"]
    entry = locate(previous["artifacts"], "id", args.artifact, "артефакт")
    data = entry["data"]
    if entry["kind"] != "table":
        raise ValueError("Для проверки нужен артефакт таблицы.")
    if any(item["data"]["source"]["sha256"] != pdf_hash for item in previous["artifacts"]):
        raise ValueError("Итог содержит артефакты другой версии PDF.")
    remark = locate(data["remarks"], "id", args.remark, "замечание")
    address = cell_address(remark)
    row, column = address[2:4]
    method = remark["conditions"].get("method")
    field = remark.get("field")
    if (remark["kind"] != "ambiguous-binding" or not field or not method
            or type(row) is not int or type(column) is not int):
        raise ValueError("Замечание должно указывать на открытое условие конкретной ячейки.")

    target = TableTarget(data["page"], data["table"])
    region = TableRegion.parse(data["region"]) if "region" in data else None
    grid = load_table_grid(args.grid_input, pdf_hash, target.page, target.number, data["strategy"], region=region)
    grid = isolate_cell(grid, row, column)
    article = load_article_fragments(args.article_text, pdf_hash)
    sources = {item.id: item for item in article.fragments}
    if args.fragment not in sources:
        raise ValueError(f"В сохранённом тексте отсутствует фрагмент {args.fragment}.")
    fragment = sources[args.fragment]
    base_context = context_payload(previous, entry["id"], args.context_input)
    saved_contexts = previous.get("contexts", [])

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".rag-review-", dir=args.out.parent) as temporary:
        folder = Path(temporary)
        context_path = folder / CONTEXT_ARTIFACT
        context_path.write_bytes(base_context)
        original = load_table_context(context_path, pdf_hash, target, region, max(article.pages))
        if original.context.status != "supported" or original.engine_version is None:
            raise ValueError("Для проверки нужна сохранённая подборка с подтверждённой связью с таблицей.")
        baseline = serialize_selection(grid, original, context_path)
        if sum(same_remark(item, remark) for item in baseline["remarks"]) != 1:
            raise ValueError("Сохранённые входы не воспроизводят выбранное замечание.")

        # The usual follow-up accepts an explicit, method-specific citation for the open field.
        revised = replace(original, followup_fragments=tuple(dict.fromkeys((*original.followup_fragments, fragment))))
        candidate = serialize_selection(grid, revised, context_path)
        matches = [item for item in candidate["results"] if cell_address(item) == address and item["method"] == method
                   and fragment.id in item["condition_sources"].get(field, ())]
        result = matches[0] if len(matches) == 1 else None
        if result is not None:
            result["id"] = "review-" + uuid4().hex
        updated = update_record(data, remark, candidate, result)
        artifacts = [{**entry, "data": updated} if item is entry else item for item in previous["artifacts"]]

        current = report("review-" + uuid4().hex, pdf_hash, artifacts, None)
        status = "open"
        if result is not None:
            observation = {"artifact_id": entry["id"], "result_id": result["id"]}
            outcomes = {relation["status"] for relation in current["relationships"] if observation in relation["members"]}
            status = "conflict" if "conflict" in outcomes else "open" if "open" in outcomes else "confirmed"
        refs = citation_ids(remark) | {fragment.id}
        evidence = include_sources(data["evidence"], [json.loads(json.dumps(asdict(fragment)))], refs)
        outcome = {
            "base_run_id": previous["run_id"],
            "base_report_sha256": sha256(payload).hexdigest(),
            "base_context_sha256": sha256(base_context).hexdigest(),
            "artifact_id": entry["id"],
            "remark": remark,
            "added_fragment": fragment.id,
            "evidence": [evidence[ref] for ref in sorted(refs)],
            "status": status,
            "result_id": result["id"] if result else None,
        }
        contexts = [item for item in saved_contexts if item["artifact_id"] != entry["id"]]
        contexts.append({"artifact_id": entry["id"], "data": revised.artifact()})
        write_json(folder / "reconciliation.json", {**previous, **current, "contexts": contexts, "review": outcome})
        folder.rename(args.out)


def main() -> None:
    parser = argparse.ArgumentParser(description="Перепроверить открытое условие одной табличной записи.")
    for name in ("reconciliation", "grid-input", "article-text", "out"):
        parser.add_argument(f"--{name}", required=True, type=Path)
    parser.add_argument("--context-input", type=Path, help="Подборка, если её нет в сохранённом итоге.")
    for name in ("artifact", "remark", "fragment"):
        parser.add_argument(f"--{name}", required=True)
    try:
        review(parser.parse_args())
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
