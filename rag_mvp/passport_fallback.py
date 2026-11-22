"""Carry source-backed passport metrics into the existing partial-release remarks."""
import re
from collections import defaultdict
from dataclasses import asdict, replace
from typing import TYPE_CHECKING

from .article_context.captions import text_for_matching
from .article_context.passport_metrics import ranked_caption_metric, rate_columns
from .core.domain import Collection, LABELED_HEADER, Remark

if TYPE_CHECKING:
    from .run import TableRead


SCALAR = re.compile(r"[+−-]?(?:\d+(?:\.\d+)?|\.\d+)\s*%?")
MODEL_GROUP = re.compile(r"(?:Qwen|DeepSeek|GLM|GPT|Llama|Claude|Gemini|Mistral)[-.\w]+", re.I)
ROW_AXES = {"method": "method", "dataset": "dataset", "model": "generator", "generator": "generator"}


def passport_remarks(read: "TableRead", collected: Collection) -> tuple[Remark, ...]:
    table, grid = read.table, read.grid
    original = collected.remarks
    if not table or not grid or not grid.caption_evidence_id:
        return original
    evidence = {item.id: item for item in read.evidence if item.page == table.page and item.box}
    caption = evidence.get(grid.caption_evidence_id)
    if not caption or caption.precision != "text_fragment" or table.caption_evidence_id != caption.id:
        return original
    title = text_for_matching(caption.text)
    headers = [cell for cell in grid.cells if cell.role == "column_header"]
    code_by_cell = rate_columns(title, [{**asdict(cell), "id": cell.id} for cell in headers])
    ranked = ranked_caption_metric(title) if any(
        cell.column == 0 and cell.text.strip().casefold() == "method" for cell in headers
    ) else None
    if not headers or not (ranked or code_by_cell):
        return original
    last_header = max(cell.row + cell.row_span for cell in headers)
    full = {(item.source.row, item.source.column) for item in collected.results}
    grouped = defaultdict(list)
    for index, remark in enumerate(original):
        if (remark.source.page, remark.source.table) == (table.page, table.number):
            grouped[remark.source.row, remark.source.column].append((index, remark))
    row_labels = defaultdict(list)
    for cell in grid.cells:
        if cell.column_span == 1 and cell.row_span == 1 and cell.row >= last_header:
            row_labels[cell.row].append(cell)
    reader_defects = {(r.source.row, r.source.column) for r in read.remarks if r.kind == "reading-defect"
                      and (r.source.page, r.source.table) == (table.page, table.number)}
    output = list(original)

    def supported(cell):
        return tuple(ref for ref in cell.evidence_ids if ref in evidence
                     and evidence[ref].precision == "cell"
                     and text_for_matching(evidence[ref].text) == text_for_matching(cell.text))

    for cell in grid.cells:
        address = cell.row, cell.column
        remarks = grouped.get(address, ())
        if (cell.row < last_header or not cell.column or cell.role == "row_header"
                or (cell.row_span, cell.column_span) != (1, 1) or address in full or address in reader_defects
                or not remarks
                or any(r.kind == "reading-defect" or r.conditions.get("metric")
                       or (r.field == "metric" and r.options) for _, r in remarks)
                or not SCALAR.fullmatch(cell.text.strip()) or len(cell.evidence_ids) != 1):
            continue
        witness = evidence.get(cell.evidence_ids[0])
        if not witness or witness.precision != "cell" or witness.text.strip() != cell.text.strip():
            continue
        eligible = [(index, r) for index, r in remarks
                    if r.kind == "ambiguous-binding" and r.statistic is None
                    and r.raw_text.strip() == cell.text.strip() and r.source.box == witness.box
                    and r.source.evidence_ids[:1] in ((), (witness.id,))]
        if not eligible:
            continue
        path = [header for header in headers if header.column <= cell.column < header.column + header.column_span]
        conditions, refs = {}, {}
        if ranked:
            conditions = {"metric": ranked[0], "metric_k": ranked[1]}
            refs = {"metric": (caption.id,), "metric_k": (caption.id,)}
        elif len(rates := [header for header in path if header.id in code_by_cell]) == 1:
            leaf, = rates
            metric, group = code_by_cell[leaf.id]
            parent = next((header for header in path if header.id == group["id"]), None)
            if not parent or not (leaf_refs := supported(leaf)) or not (group_refs := supported(parent)):
                continue
            conditions = {"metric": metric, "unit": "%"}
            refs = {"metric": (*leaf_refs, *group_refs, caption.id), "unit": (caption.id,)}
            name = re.sub(r"-\s+", "-", text_for_matching(parent.text))
            if MODEL_GROUP.fullmatch(name):
                conditions["generator"], refs["generator"] = name, group_refs
        if not conditions:
            continue
        candidates = defaultdict(dict)
        for header in path:
            if (match := LABELED_HEADER.fullmatch(header.text.strip())) and (header_refs := supported(header)):
                role = match[1].lower()
                value = match[2].strip()
                if value:
                    candidates[ROW_AXES.get(role, role)][value] = header_refs
        if set(candidates["metric"]) - {conditions["metric"]}:
            continue
        for axis in headers:
            role = ROW_AXES.get(axis.text.strip().casefold())
            if role is None or axis.column_span != 1 or axis.column >= cell.column or not (axis_refs := supported(axis)):
                continue
            for label in row_labels[cell.row]:
                text = label.text.strip()
                if label.column == axis.column and text and text[0].isalnum() and (label_refs := supported(label)):
                    candidates[role][text] = (*label_refs, *axis_refs)
        if cell.text.strip().endswith("%"):
            candidates["unit"]["%"] = (witness.id,)
        for field in ("method", "dataset", "generator", "unit"):
            if field in conditions:
                candidates[field][conditions[field]] = refs[field]
            if len(candidates[field]) == 1:
                [(conditions[field], refs[field])] = candidates[field].items()
            else:
                conditions.pop(field, None)
                refs.pop(field, None)
        protected = {field for _, r in remarks for field, value in r.conditions.items() if value not in ("", None)}
        protected.update(r.field for _, r in remarks if r.options)
        fields = conditions.keys() - protected
        additions = {field: conditions[field] for field in conditions if field in fields}
        sources = {field: refs[field] for field in conditions if field in fields}
        for index, remark in eligible:
            resolved = remark.field in fields
            output[index] = replace(
                remark, field=None if resolved else remark.field,
                reason=("Метрика подтверждена источником. Проверьте оставшиеся условия измерения."
                        if resolved or remark.field is None else remark.reason),
                conditions={**remark.conditions, **additions},
                condition_sources={**remark.condition_sources, **sources},
            )
    return tuple(output)
