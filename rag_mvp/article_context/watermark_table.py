"""Bind a watermark robustness table when the authors name its metric and method."""

from collections import Counter
from decimal import Decimal
import re

from ..core.domain import Collection, Evidence, GridCell, NUMBER, ReadTable, Remark, Result, Source
from .captions import TABLE_LINE, TABLE_WORD, text_for_matching


ATTACK_CAPTION = re.compile(r"\bKnowledge Injection Attack\b", re.I)
METRIC = "WRR"
METRIC_DEFINITION = re.compile(rf"\bWatermark Recovery Rate \({METRIC}\)\s*:\s*{METRIC} mea-?sures the ratio\b", re.I)
INJECTION_RATE = "Injection Rate"
INJECTION_RATE_PARAMETER = f"{INJECTION_RATE} (%)"
PERCENT = re.compile(r"(100|[1-9]?\d)\s*%")


def table_sources(
    fragments: tuple[Evidence, ...], label: str, caption: Evidence,
) -> tuple[tuple[Evidence, str], Evidence] | None:
    if not ATTACK_CAPTION.search(text_for_matching(caption.text)):
        return None
    reference = re.compile(
        rf"\bresults?\s+(?:in|from)\s+{TABLE_WORD}\s*{re.escape(label)}\s+"
        rf"(?:show|indicate|demonstrate)\s+that\s+(?:the\s+)?{METRIC}s?\s+of\s+([\w.-]+)\s+across\b",
        re.I,
    )
    readable = [(fragment, text_for_matching(fragment.text)) for fragment in fragments if fragment.box]
    declarations = [(fragment, match[1]) for fragment, text in readable for match in reference.finditer(text)]
    definitions = [fragment for fragment, text in readable if METRIC_DEFINITION.search(text)]
    if len(declarations) == len(definitions) == 1:
        return declarations[0], definitions[0]
    return None


def recover_watermark_measurements(
    table: ReadTable, cells: tuple[GridCell, ...], evidence: tuple[Evidence, ...], context: tuple[Evidence, ...],
    collected: Collection, reader_remarks: tuple[Remark, ...],
) -> Collection:
    if collected.results or not context:
        return collected
    first_line = table.caption.partition("\n")[0]
    label = TABLE_LINE.match(first_line)
    refs = {fragment.id: fragment for fragment in evidence if fragment.box}
    caption = refs.get(table.caption_evidence_id)
    if not label or not caption or caption.page != table.page or text_for_matching(caption.text) != text_for_matching(table.caption):
        return collected

    axes = [cell for cell in cells if cell.role == "column_header" and cell.row == 0
            and cell.text.casefold() == INJECTION_RATE.casefold() and cell.column_span == 1]
    sources = table_sources(context, label["label"], caption)
    if len(axes) != 1 or not sources:
        return collected
    axis, = axes
    axis_refs = tuple(ref for ref in axis.evidence_ids if ref in refs)
    if not axis_refs:
        return collected

    (declaration, method), definition = sources
    positioned = {(cell.row, cell.column): cell for cell in cells
                  if cell.row_span == cell.column_span == 1}
    row_labels = Counter(cell.text for cell in positioned.values() if cell.column == axis.column and cell.role == "row_header")
    column_labels = Counter(cell.text.casefold() for cell in positioned.values() if cell.row == axis.row)
    defects = {(item.source.row, item.source.column) for item in (*reader_remarks, *collected.remarks)
               if item.kind == "reading-defect"}
    results = []
    for cell in table.cells:
        if (cell.row, cell.column) in defects:
            continue
        row = positioned.get((cell.row, axis.column))
        header = positioned.get((axis.row, cell.column))
        value = positioned.get((cell.row, cell.column))
        if not (row and row.role == "row_header" and header and header.role == "column_header"
                and value and value.role == "data" and cell.box and tuple(cell.headers) == (header.text,)
                and cell.method == row.text and cell.text == value.text
                and row_labels[row.text] == column_labels[header.text.casefold()] == 1):
            continue
        rate = PERCENT.fullmatch(row.text.strip())
        number = cell.text.strip()
        if not rate or not re.fullmatch(NUMBER, number) or not Decimal(0) <= Decimal(number) <= Decimal(1):
            continue
        if not all(item.evidence_ids and all(ref in refs and refs[ref].precision == "cell" for ref in item.evidence_ids)
                   for item in (row, header, value)):
            continue
        results.append(Result(
            method=method, generator="", dataset=header.text, metric=METRIC, task="watermark bit recovery",
            experiment=caption.text, protocol=None, raw_value=number, value=str(Decimal(number)), unit="fraction",
            statistic=None, retrieval_top_k=None,
            source=Source(table.page, table.number, cell.row, cell.column, cell.headers, cell.box, value.evidence_ids),
            condition_sources={
                "method": (declaration.id,), "dataset": header.evidence_ids,
                "metric": (declaration.id, definition.id), "task": (definition.id,), "experiment": (caption.id,),
                INJECTION_RATE_PARAMETER: (*axis_refs, *row.evidence_ids),
            },
            parameters={INJECTION_RATE_PARAMETER: int(rate[1])},
        ))
    resolved = {(result.source.row, result.source.column) for result in results}
    return Collection(tuple(results), tuple(
        remark for remark in collected.remarks
        if remark.kind != "ambiguous-binding" or (remark.source.row, remark.source.column) not in resolved
    ))
