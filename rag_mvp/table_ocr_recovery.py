"""Recover isolated values from a second OCR without taking its cell assignments."""

from collections import defaultdict
from dataclasses import dataclass, replace
import re
from statistics import median

from .core.domain import Box, Evidence, GridCell, ReadCell
from .strategies.ocr_tableformer import GENERATOR_HEADER, SINGLE_VALUE, center, close_position, coverage
from .run import TableRead
from .table_reading_retry import REREAD_SOURCE_PREFIX


SINGLE_LEXEME = re.compile(r"\S*\d\S*(?:[ \t]*%)?")
OCR_CELL_COVERAGE = 0.8


def _position(cell):
    return cell.row, cell.column


def _inside(token, box):
    return coverage(token.box, box) >= OCR_CELL_COVERAGE


def _row_labels(cells, boxes, tokens, anchors):
    methods = {cell.column for cell in cells if cell.role == "column_header"
               and cell.column_span == 1 and cell.text.casefold() == "method"}
    generators = {cell.column for cell in cells if cell.role == "column_header"
                  and cell.column_span == 1 and GENERATOR_HEADER.fullmatch(cell.text)}
    if len(methods) != 1 or len(generators) != 1 or not anchors:
        return {}, {}, 0
    method_column, = methods
    generator_column, = generators
    singles = {_position(cell): cell for cell in cells if cell.role == "row_header"
               and cell.row_span == cell.column_span == 1 and _position(cell) in boxes}
    method_at = {}
    for (row, column), cell in singles.items():
        if column != method_column:
            continue
        box = boxes[row, column]
        matches = [token for token in tokens.values() if token.text == cell.text and _inside(token, box)
                   and coverage(box, token.box) >= OCR_CELL_COVERAGE]
        if len(matches) == 1:
            method_at[row] = matches[0]

    row_y = {row: center(token.box, 1) for row, token in method_at.items()}
    rows = sorted(row_y)
    pitches = [row_y[b] - row_y[a] for a, b in zip(rows, rows[1:]) if b == a + 1]
    if not pitches or min(pitches) <= 0 or any(row_y[a] >= row_y[b] for a, b in zip(rows, rows[1:])):
        return {}, {}, 0
    tolerance = median(pitches) / 3
    pairs = {(method, generator) for _, method, generator, _, _ in anchors}
    generator_shift = median(center(generator.box, 1) - center(method.box, 1) for method, generator in pairs)
    labels = {}
    for row, method in method_at.items():
        cell = singles.get((row, generator_column))
        if cell is None:
            continue
        fragments = sorted((token for token in tokens.values() if _inside(token, boxes[row, generator_column])),
                           key=lambda token: (center(token.box, 1), center(token.box, 0)))
        # A merged reading may be split only if the primary OCR accounts for the complete cell text.
        if " ".join(token.text for token in fragments) != cell.text:
            continue
        aligned = []
        for token in fragments:
            projected = center(token.box, 1) - generator_shift
            if abs(projected - row_y[row]) <= tolerance and close_position(row_y, projected) == row:
                aligned.append(token)
        if len(aligned) == 1:
            labels[row] = method, aligned[0]
    return labels, row_y, tolerance


def _column_labels(anchors):
    grouped = defaultdict(list)
    for cell, method, _, dataset, metric in anchors:
        grouped[cell.column].append((cell, method, dataset, metric))

    labels, column_x, shifts = {}, {}, {}
    for column, cells in grouped.items():
        pairs = {(dataset, metric) for _, _, dataset, metric in cells}
        if len({cell.row for cell, *_ in cells}) < 2 or len(pairs) != 1:
            continue
        labels[column], = pairs
        column_x[column] = median(center(cell.box, 0) for cell, *_ in cells)
        shifts[column] = median(center(cell.box, 1) - center(method.box, 1) for cell, method, _, _ in cells)
    return labels, column_x, shifts


@dataclass(frozen=True)
class _RecoveryGeometry:
    boxes: dict[tuple[int, int], Box]
    column_x: dict[int, float]
    row_y: dict[int, float]
    tolerance: float
    targets: tuple[tuple[GridCell, tuple[Evidence, Evidence, Evidence, Evidence], float], ...]
    primary_tokens: tuple[Evidence, ...]


def _geometry(read: TableRead) -> _RecoveryGeometry | None:
    if read.table is None or read.grid is None or not read.table.caption_evidence_id:
        return None
    page = read.table.page
    evidence = {item.id: item for item in read.evidence if item.page == page and item.box}
    tokens = {ref: item for ref, item in evidence.items()
              if ref.startswith(f"ocr-{page}-") and item.precision == "text_fragment"}
    boxes = {_position(cell): fragment.box for cell in read.grid.cells
             if len(cell.evidence_ids) == 1 and (fragment := evidence.get(cell.evidence_ids[0]))
             and fragment.precision in {"cell", "estimated_cell"}}
    anchors = []
    for cell in read.table.cells:
        if cell.box is None:
            continue
        refs = [cell.verified_condition_sources.get(field, ()) for field in ("method", "generator", "dataset", "metric")]
        if all(len(ids) == 1 and ids[0] in tokens for ids in refs):
            anchors.append((cell, *(tokens[ids[0]] for ids in refs)))

    rows, row_y, tolerance = _row_labels(read.grid.cells, boxes, tokens, anchors)
    columns, column_x, shifts = _column_labels(anchors)
    if len(columns) < 2:
        return None
    occupied = {_position(cell) for cell in read.table.cells}
    defects = {(remark.source.row, remark.source.column) for remark in read.remarks if remark.kind == "reading-defect"}
    eligible = boxes.keys() & (defects - occupied)
    targets = tuple((cell, (*rows[cell.row], *columns[cell.column]), shifts[cell.column])
                    for cell in read.grid.cells if cell.role == "data" and cell.row_span == cell.column_span == 1
                    and (not cell.text or SINGLE_LEXEME.fullmatch(cell.text))
                    and _position(cell) in eligible and cell.row in rows and cell.column in columns)
    if not targets:
        return None
    return _RecoveryGeometry(boxes, column_x, row_y, tolerance, targets, tuple(tokens.values()))


def can_recover_cells(read: TableRead) -> bool:
    return _geometry(read) is not None


def _decimal_token(token: Evidence) -> bool:
    return "." in token.text and SINGLE_VALUE.fullmatch(token.text) is not None


def recover_cells(primary: TableRead, retry: TableRead) -> tuple[TableRead, set[tuple[int, int]]]:
    if retry.grid is None or (geometry := _geometry(primary)) is None:
        return primary, set()
    page = primary.table.page
    token_ids = set(retry.grid.unplaced_evidence_ids)
    tokens = [item for item in retry.evidence
              if item.id in token_ids and item.page == page and item.box and item.precision == "text_fragment"]
    accepted = {}
    readings, evidence = [], []
    for cell, labels, shift in geometry.targets:
        position = _position(cell)
        box = geometry.boxes[position]

        def aligned(token: Evidence) -> bool:
            projected = center(token.box, 1) - shift
            rows = [row for row, expected in geometry.row_y.items() if abs(projected - expected) <= geometry.tolerance]
            return (close_position(geometry.column_x, center(token.box, 0)) == cell.column
                    and rows == [cell.row] and close_position(geometry.row_y, projected) == cell.row)

        inside = [token for token in tokens if _inside(token, box)]
        if len(inside) != 1:
            continue
        token, = inside
        if not _decimal_token(token) or not aligned(token):
            continue
        if sum(_inside(token, other) for other in geometry.boxes.values()) != 1:
            continue
        if any(_decimal_token(other) and other.text != token.text and _inside(other, box) and aligned(other)
               for other in geometry.primary_tokens):
            continue
        method, generator, dataset, metric = labels
        token = replace(token, id=REREAD_SOURCE_PREFIX + token.id)
        evidence.append(token)
        accepted[position] = replace(cell, text=token.text, evidence_ids=(token.id, *cell.evidence_ids))
        readings.append(ReadCell(
            cell.row, cell.column, method.text,
            (f"Generator: {generator.text}", f"Dataset: {dataset.text}", f"Metric: {metric.text}"), token.text, token.box,
            verified_condition_sources={
                "method": (method.id,), "generator": (generator.id,), "dataset": (dataset.id,),
                "metric": (metric.id,), "experiment": (primary.table.caption_evidence_id,),
            },
        ))
    if not accepted:
        return primary, set()
    return replace(
        primary,
        table=replace(primary.table, cells=(*primary.table.cells, *readings)),
        grid=replace(primary.grid, cells=tuple(accepted.get(_position(cell), cell) for cell in primary.grid.cells)),
        evidence=(*primary.evidence, *evidence),
        remarks=tuple(remark for remark in primary.remarks if remark.kind != "reading-defect"
                      or (remark.source.row, remark.source.column) not in accepted),
    ), set(accepted)
