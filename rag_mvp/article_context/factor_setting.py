"""Produce scoped assertions for a table of retrieval ablations."""

import re
from typing import Iterable

from ..core.domain import Evidence, GridCell, ReadTable, TableTarget
from ..core.measurement_binding import Assertion, Scope
from .captions import table_caption_line, text_for_matching

ABLATION_CAPTION = re.compile(
    r"[:.\s–—-]*ablation stud(?:y|ies) on (?P<dataset>[\w-]+)(?=\s*\.|$)", re.I,
)
DEFAULT_LEGEND = re.compile(
    r"\bthe default settings of (?P<method>[\w.-]+) are marked with [*∗⋆]", re.I,
)
ABLATION_METHOD = (
    r"\bablation stud(?:y|ies) (?:performed for each influential factor in|of|for) "
    r"(?P<method>[\w.-]+) (?:is|are) (?:shown|presented|reported) in Table {number}(?!\w|[.-]\w)"
)


def factor_setting_axes(headers: Iterable[GridCell]) -> tuple[GridCell, GridCell] | None:
    axes: dict[str, list[GridCell]] = {"factor": [], "setting": []}
    for cell in headers:
        label = cell.text.strip().casefold()
        if cell.role == "column_header" and cell.column_span == 1 and label in axes:
            axes[label].append(cell)
    if len(axes["factor"]) == len(axes["setting"]) == 1:
        factor, setting = axes["factor"][0], axes["setting"][0]
        if factor.column < setting.column and factor.row == setting.row:
            return factor, setting
    return None


def ablation_dataset(table: ReadTable) -> str | None:
    caption = table_caption_line(text_for_matching(table.caption))
    if caption and caption["label"] == str(table.number):
        match = ABLATION_CAPTION.match(caption["tail"])
        if match:
            return match["dataset"]
    return None


def ablation_method_mentions(table: ReadTable, fragments: Iterable[Evidence]) -> Iterable[tuple[Evidence, str]]:
    pattern = re.compile(ABLATION_METHOD.format(number=table.number), re.I)
    for fragment in fragments:
        if fragment.page <= table.page and fragment.precision == "text_fragment" and fragment.box:
            for match in pattern.finditer(text_for_matching(fragment.text)):
                yield fragment, match["method"]


def ablation_assertions(
    table: ReadTable, axes: tuple[GridCell, GridCell], cells: Iterable[GridCell],
    context: tuple[Evidence, ...], caption: Evidence, dataset: str,
) -> Iterable[Assertion]:
    target = TableTarget(table.page, table.number)
    mentions = tuple(ablation_method_mentions(table, context))
    for fragment, method in mentions:
        yield Assertion("method", method, Scope(target), (fragment.id,))
    yield Assertion("dataset", dataset, Scope(target), (caption.id,))

    legend = DEFAULT_LEGEND.search(text_for_matching(caption.text))
    default_marked = legend is not None and {name.casefold() for _, name in mentions} == {legend["method"].casefold()}
    factor, setting = axes
    for cell in cells:
        label = cell.text.strip()
        if not label or label in {"-", "—", "–"} or cell.column_span != 1:
            continue
        if cell.column == factor.column and cell.role == "row_header" and cell.row >= factor.row + factor.row_span:
            scope = Scope(target, row=cell.row, row_span=cell.row_span)
            yield Assertion("factor", label, scope, (*cell.evidence_ids, *factor.evidence_ids))
        if (cell.column == setting.column and cell.role in {"row_header", "data"}
                and cell.row >= setting.row + setting.row_span and cell.row_span == 1):
            scope = Scope(target, row=cell.row)
            refs = (*cell.evidence_ids, *setting.evidence_ids)
            if default_marked and label.endswith(("*", "∗", "⋆")):
                label = label[:-1].rstrip()
                if not label:
                    continue
                yield Assertion("setting_default", "default", scope, (*refs, caption.id))
            yield Assertion("setting", label, scope, refs)
