"""Keep merged values from Docling grids whose cells were all tagged as headers."""

from collections import defaultdict
import re
from typing import TYPE_CHECKING

from .core.domain import NUMBER, Evidence, GridCell, Remark, Source

if TYPE_CHECKING:
    from .run import TableRead


def column_range(cell: GridCell) -> range:
    return range(cell.column, cell.column + cell.column_span)


def text_label(cell: GridCell | None) -> bool:
    return cell is not None and cell.row_span == 1 and any(char.isalpha() for char in cell.text)


def settings_row(row: dict[int, GridCell], groups: tuple[GridCell, ...], columns: int) -> bool:
    label = row.get(0)
    if row.keys() != set(range(columns)) or not text_label(label) or label.column_span != 1:
        return False
    if any(cell.column_span != 1 or cell.row_span != 1 or not re.fullmatch(NUMBER, cell.text.strip())
           for column, cell in row.items() if column):
        return False
    sequences = {tuple(row[column].text.strip() for column in column_range(group)) for group in groups}
    return len(sequences) == 1 and len(set(next(iter(sequences)))) > 1


def numeric_header_remarks(read: "TableRead") -> tuple[Remark, ...]:
    table, grid = read.table, read.grid
    if not table or table.cells or not grid or "docling" not in read.versions:
        return ()
    if any(cell.role != "column_header" for cell in grid.cells):
        return ()
    rows: dict[int, dict[int, GridCell]] = defaultdict(dict)
    for cell in grid.cells:
        if cell.column in rows[cell.row]:
            return ()
        rows[cell.row][cell.column] = cell

    groups = tuple(sorted((cell for cell in rows[0].values() if cell.column), key=lambda cell: cell.column))
    if (len(groups) < 2 or any(not text_label(group) or group.column_span < 2 for group in groups)
            or [column for group in groups for column in column_range(group)] != list(range(1, grid.columns))):
        return ()

    sources = {item.id: item for item in read.evidence
               if item.page == table.page and item.precision == "cell" and item.box}

    def located(cell: GridCell) -> Evidence | None:
        if len(cell.evidence_ids) == 1 and (source := sources.get(cell.evidence_ids[0])) and source.text == cell.text:
            return source
        return None

    remarks = {}
    for row_number, row in sorted(rows.items()):
        if not settings_row(row, groups, grid.columns):
            continue
        # A repeated parameter row separates merged comparison values from the
        # following per-setting rows. Only inspect the adjacent rows above it.
        for previous in range(row_number - 1, 0, -1):
            values = rows.get(previous, {})
            label = values.get(0)
            if not text_label(label) or label.column_span != 1 or not located(label):
                break
            found = []
            for group in groups:
                cell = values.get(group.column)
                if not cell or (cell.row_span, cell.column_span) != (1, group.column_span):
                    continue
                number = cell.text.strip()
                if not (re.fullmatch(NUMBER, number) and "." in number):
                    continue
                source = located(cell)
                if source and located(group):
                    found.append((cell, group, source))
            if len(found) < 2:
                break
            for cell, group, source in found:
                address = (cell.row, cell.column)
                remarks[address] = Remark(
                    "Docling пометил число заголовком. Проверьте по PDF, является ли оно "
                    "значением для этой строки и группы столбцов, и уточните метрику и условия.",
                    cell.text,
                    Source(table.page, table.number, *address, (group.text,), source.box, cell.evidence_ids),
                    conditions={"reader_role": cell.role, "row_label": label.text, "column_label": group.text},
                    condition_sources={"reader_role": cell.evidence_ids, "row_label": label.evidence_ids,
                                       "column_label": group.evidence_ids},
                    kind="reading-defect",
                )
    occupied = {(remark.source.row, remark.source.column) for remark in read.remarks}
    return tuple(remarks[address] for address in sorted(remarks.keys() - occupied))
