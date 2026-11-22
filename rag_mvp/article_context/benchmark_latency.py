"""Bind paired latency and accuracy measurements in an explicitly cited benchmark."""

from collections import Counter
from decimal import Decimal
import re

from ..core.domain import Collection, Evidence, GridCell, NUMBER, ReadTable, Remark, Result, Source
from .captions import TABLE_WORD, table_caption_line, text_for_matching


LATENCY = r"end-to-end latency"
LATENCY_CAPTION = re.compile(rf"\s*[:. —–-]+\s*{LATENCY}\b", re.I)
DATASET_TASK = re.compile(
    r"\b(?P<name>[A-Z][\w-]+(?:-[ \t]*\n[ \t]*\w+)?)\s*\([^()]*\b(?:19|20)\d\d\)"
    r"\s*,\s*or\s+(?P<alias>[A-Z][A-Z0-9-]+),\s*for\s+"
    r"(?P<task>(?:multi|single)-\s*hop\s+question\s+answering)\b"
)
METRICS = {"latency (s)": ("Latency", "s"), "acc": ("Accuracy", None), "accuracy": ("Accuracy", None)}


def latency_table_sources(
    fragments: tuple[Evidence, ...], label: str, caption: Evidence,
) -> tuple[Evidence, Evidence | None] | None:
    title = table_caption_line(text_for_matching(caption.text))
    if not title or title["label"].casefold() != label.casefold() or not LATENCY_CAPTION.match(title["tail"]):
        return None

    reference = re.compile(
        rf"\bwe (?:present|report)\s+(?:the\s+)?{LATENCY}\b[^.!?]{{0,120}}"
        rf"\bin\s+{TABLE_WORD}\s+{re.escape(label)}(?!\w|[.\-]\w)",
        re.I,
    )
    readable = [(fragment, text_for_matching(fragment.text)) for fragment in fragments
                if fragment.precision == "text_fragment" and fragment.box]
    declarations = [fragment for fragment, content in readable
                    if fragment.id != caption.id and fragment.page == caption.page and reference.search(content)]
    definitions = [fragment for fragment, _ in readable
                   if fragment.page <= caption.page and DATASET_TASK.search(fragment.text)]
    if len(declarations) == 1:
        return declarations[0], definitions[0] if len(definitions) == 1 else None
    return None


def recover_latency_benchmark(
    table: ReadTable, cells: tuple[GridCell, ...], evidence: tuple[Evidence, ...], context: tuple[Evidence, ...],
    collected: Collection, reader_remarks: tuple[Remark, ...],
) -> Collection:
    if collected.results or not context:
        return collected
    positioned = {(cell.row, cell.column): cell for cell in cells}
    read_positions = {(cell.row, cell.column) for cell in table.cells}
    if len(positioned) != len(cells) or len(read_positions) != len(table.cells):
        return collected

    by_id = {fragment.id: fragment for fragment in evidence if fragment.box}
    caption = by_id.get(table.caption_evidence_id)
    if caption is None or caption.precision != "text_fragment" or caption.page != table.page or caption.text != table.caption:
        return collected
    title = table_caption_line(text_for_matching(caption.text))
    sources = latency_table_sources(context, title["label"], caption) if title else None
    if not sources:
        return collected
    declaration, definition = sources

    def fragment_at(grid_cell: GridCell | None, role: str) -> Evidence | None:
        if not grid_cell or grid_cell.role != role or grid_cell.row_span != 1 or len(grid_cell.evidence_ids) != 1:
            return None
        fragment = by_id.get(grid_cell.evidence_ids[0])
        if fragment and fragment.precision == "cell" and fragment.page == table.page and fragment.text == grid_cell.text:
            return fragment
        return None

    headings = [cell for cell in cells if cell.row == 0 and cell.column_span == 2
                and fragment_at(cell, "column_header")]
    if len(headings) != 1:
        return collected
    heading, = headings
    tasks = [match for match in DATASET_TASK.finditer(definition.text) if match["alias"] == heading.text] if definition else []
    dataset = re.sub(r"-[ \t]*\n[ \t]*", "", tasks[0]["name"]) if len(tasks) == 1 else None
    task = re.sub(r"-\s+", "-", " ".join(tasks[0]["task"].split())) if dataset else None
    model_column = heading.column - 1
    axis = positioned.get((1, model_column))
    if not axis or axis.text.casefold() != "model" or axis.column_span != 1 or not fragment_at(axis, "row_header"):
        return collected
    columns = {column: positioned.get((1, column)) for column in range(heading.column, heading.column + heading.column_span)}
    if any(not header or header.column_span != 1 or header.text.casefold() not in METRICS
           or not fragment_at(header, "column_header") for header in columns.values()):
        return collected
    if {METRICS[header.text.casefold()][0] for header in columns.values()} != {"Latency", "Accuracy"}:
        return collected

    row_labels = Counter(cell.text for cell in cells if cell.column == model_column and cell.role == "row_header")
    defects = {(remark.source.row, remark.source.column) for remark in (*reader_remarks, *collected.remarks)
               if remark.kind == "reading-defect"}
    results = []
    unresolved = {}
    for cell in table.cells:
        position = (cell.row, cell.column)
        if cell.row < 2 or position in defects or cell.column not in columns:
            continue
        row = positioned.get((cell.row, model_column))
        value = positioned.get(position)
        method = fragment_at(row, "row_header")
        number = fragment_at(value, "data")
        header = columns[cell.column]
        if not (method and row.column_span == 1 and row_labels[method.text] == 1 and method.text == cell.method
                and number and value.column_span == 1 and number.box == cell.box and number.text == cell.text
                and cell.headers == (heading.text, header.text)
                and cell.header_evidence_ids == (*heading.evidence_ids, *header.evidence_ids)
                and re.fullmatch(NUMBER, cell.text)):
            continue
        parsed_value = Decimal(cell.text)
        if parsed_value < 0:
            continue
        metric, unit = METRICS[header.text.casefold()]
        source = Source(table.page, table.number, cell.row, cell.column, cell.headers, cell.box, (number.id,))
        condition_sources = {
            "method": (method.id,), "dataset": heading.evidence_ids, "metric": header.evidence_ids,
            "experiment": (caption.id, declaration.id),
        }
        if not dataset:
            unresolved[position] = Remark(
                f"Не подтверждено, какой датасет обозначен {heading.text}.", cell.text, source, "dataset",
                {"method": method.text, "metric": metric}, condition_sources, kind="ambiguous-binding",
            )
            continue
        condition_sources["dataset"] += (definition.id,)
        condition_sources["task"] = (definition.id,)
        results.append(Result(
            method=method.text, generator="", dataset=dataset, metric=metric, task=task,
            experiment=caption.text, protocol=None, raw_value=cell.text, value=str(parsed_value), unit=unit,
            statistic=None, retrieval_top_k=None,
            source=source, condition_sources=condition_sources,
        ))
    resolved = {(result.source.row, result.source.column) for result in results}
    return Collection(tuple(results), tuple(
        unresolved.get((remark.source.row, remark.source.column), remark)
        if remark.kind == "ambiguous-binding" else remark
        for remark in collected.remarks
        if remark.kind != "ambiguous-binding" or (remark.source.row, remark.source.column) not in resolved
    ))
