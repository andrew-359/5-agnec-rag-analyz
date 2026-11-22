"""Read a scanned comparison table from Docling's anchors and OCR text lines."""

import re
from importlib.metadata import version
from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass, replace
from pathlib import Path
from statistics import median

import torch
from docling.datamodel.base_models import ConversionStatus
from docling.datamodel.pipeline_options import OcrMode, RapidOcrOptions
from docling_core.types.doc import DoclingDocument, DocItemLabel, TextItem
from docling_core.types.doc.page import SegmentedPdfPage

from ..core.domain import NUMBER, Box, ReadCell, ReadTable, Remark, Source
from .parser import (
    anchored_table, converter_for, docling_options, docling_versions, item_evidence, normalized_box, record_conversion,
    region_evidence, table_evidence,
)
from ..run import Evidence, GridCell, TableGrid, TableRead
from ..ocr_discovery import ocr_backend
from ..table_read_features import TOP_K
from ..table_region import TableRegion


TASK = re.compile(r"\bopen[- ]domain (?:multi-hop )?(?:QA|question answering)\b", re.IGNORECASE)
QA = re.compile(r"\b(?:QA|question answering)\b", re.IGNORECASE)
SECTION_NUMBER = re.compile(r"^(\d+(?:\.\d+)*)\.?\s+")
MEASUREMENT = re.compile(rf"^\s*{NUMBER}(?:\s|\(|%|$)")


@dataclass(frozen=True)
class Fragment:
    id: str
    text: str
    box: Box

    @property
    def x(self) -> float:
        return (self.box[0] + self.box[2]) / 2

    @property
    def y(self) -> float:
        return (self.box[1] + self.box[3]) / 2


def nearest(anchors: list[float], value: float) -> int:
    return min(range(len(anchors)), key=lambda index: abs(anchors[index] - value))


def recognize_grid(fragments: list[Fragment], number: int) -> tuple[TableGrid, tuple[ReadCell, ...]]:
    method_headers = [item for item in fragments if item.text.casefold() == "method"]
    groups = sorted((item for item in fragments if TOP_K.fullmatch(item.text)), key=lambda item: item.x)
    if len(method_headers) != 1 or not groups:
        raise ValueError("Не удалось восстановить заголовки методов и групп top-k по OCR.")
    method_header = method_headers[0]
    group_centers = [group.x for group in groups]
    headers: list[Fragment] = []
    for item in fragments:
        if item.x <= method_header.box[2] or not re.fullmatch(r"[A-Za-z][\w-]*", item.text):
            continue
        group = groups[nearest(group_centers, item.x)]
        if 0 < item.y - group.y <= group.box[3] - group.box[1]:
            headers.append(item)
    headers.sort(key=lambda item: item.x)
    if not headers:
        raise ValueError("Не удалось восстановить заголовки датасетов по OCR.")

    header_groups = [nearest(group_centers, header.x) for header in headers]
    groups_to_columns = [[column for column, g in enumerate(header_groups, 1) if g == index]
                         for index in range(len(groups))]
    datasets = [{headers[column - 1].text.casefold() for column in columns} for columns in groups_to_columns]
    regular_headers = all(datasets[0] == dataset_group and len(dataset_group) == len(columns) > 0
                          for dataset_group, columns in zip(datasets, groups_to_columns, strict=True))
    if len({TOP_K.fullmatch(group.text)[1] for group in groups}) != len(groups):
        regular_headers = False

    first_data = (method_header.x + headers[0].x) / 2
    methods = sorted((item for item in fragments if item.x < first_data and item.y > method_header.y),
                     key=lambda item: item.y)
    if len(methods) < 2:
        raise ValueError("Не удалось восстановить строки методов по OCR.")
    row_centers = [item.y for item in methods]
    row_pitch = median(b - a for a, b in zip(row_centers, row_centers[1:]))
    if row_pitch <= 0:
        raise ValueError("Положения строк методов неоднозначны.")

    columns: list[list[Fragment]] = [[] for _ in headers]
    header_centers = [header.x for header in headers]
    for item in sorted(fragments, key=lambda fragment: fragment.y):
        if item.x >= first_data:
            column = nearest(header_centers, item.x)
            if item.y > headers[column].y + row_pitch / 2:
                columns[column].append(item)

    # Complete columns establish the vertical shift of the scan at their X positions.
    # Match incomplete columns by interpolating that shift and checking the residual.
    offsets = [(median(item.x for item in methods), 0.0)]
    for header, column in zip(headers, columns, strict=True):
        if len(column) == len(methods):
            shift = median(item.y - method.y for item, method in zip(column, methods, strict=True))
            if all(abs(item.y - method.y - shift) < row_pitch / 4
                   for item, method in zip(column, methods, strict=True)):
                offsets.append((header.x, shift))
    if len(offsets) < 2:
        raise ValueError("Не удалось подтвердить взаимное положение строк и столбцов по OCR.")
    offset_centers = [x for x, _ in offsets]

    def vertical_shift(x: float) -> float:
        index = min(max(bisect_right(offset_centers, x) - 1, 0), len(offsets) - 2)
        start, a = offsets[index]
        end, b = offsets[index + 1]
        return a + (b - a) * (x - start) / (end - start)

    grid = [GridCell(0, 0, method_header.text, row_span=2, role="column_header", evidence_ids=(method_header.id,))]
    for group, group_columns in zip(groups, groups_to_columns, strict=True):
        if group_columns:
            grid.append(GridCell(0, group_columns[0], group.text, column_span=len(group_columns),
                                 role="column_header", evidence_ids=(group.id,)))
    for column, item in enumerate(headers, 1):
        grid.append(GridCell(1, column, item.text, role="column_header", evidence_ids=(item.id,)))
    for row, item in enumerate(methods, 2):
        grid.append(GridCell(row, 0, item.text, role="row_header", evidence_ids=(item.id,)))

    readings: list[ReadCell] = []
    for column, (header, group_index, items) in enumerate(zip(headers, header_groups, columns, strict=True), 1):
        by_row: dict[int, list[Fragment]] = defaultdict(list)
        for item in items:
            y = item.y - vertical_shift(header.x)
            row = nearest(row_centers, y)
            if abs(row_centers[row] - y) < row_pitch / 4:
                by_row[row].append(item)
        for index, method in enumerate(methods):
            row = index + 2
            assigned = by_row[index]
            text = "\n".join(item.text for item in assigned)
            refs = tuple(item.id for item in assigned)
            grid.append(GridCell(row, column, text, role="data", evidence_ids=refs or ("table",)))
            path = (groups[group_index].text, header.text) if regular_headers and len(assigned) <= 1 else ()
            name = re.sub(r"^Ours\s*\((.+)\)$", r"\1", method.text, flags=re.IGNORECASE)
            readings.append(ReadCell(
                row, column, name, path, text, assigned[0].box if len(assigned) == 1 else None,
                header_evidence_ids=(groups[group_index].id, header.id) if path else (),
            ))

    grid.sort(key=lambda cell: (cell.row, cell.column))
    readings.sort(key=lambda cell: (cell.row, cell.column))
    return TableGrid(f"Table {number}", len(methods) + 2, len(headers) + 1, ("table",), "caption", tuple(grid)), tuple(readings)


def ocr_fragments(parsed: SegmentedPdfPage, document: DoclingDocument, page: int, region: Box) -> list[Fragment]:
    size = document.pages[page].size
    fragments = []
    for index, line in enumerate(parsed.textline_cells):
        box = normalized_box(line.rect.to_bounding_box(), size)
        if box is None or not line.text.strip():
            continue
        fragment = Fragment(f"ocr-{page}-{index}", line.text, box)
        if region[0] <= fragment.x <= region[2] and region[1] <= fragment.y <= region[3]:
            fragments.append(fragment)
    return fragments


def experiment_evidence(document: DoclingDocument, page: int, label: str, datasets: set[str]) -> tuple[Evidence, ...]:
    table_reference = re.compile(rf"\bTable\s+{re.escape(label)}\s+(?:reports|shows|presents)\b", re.IGNORECASE)
    direct_task = re.compile(rf"{table_reference.pattern}\s+(?:(?:the|main|final|results|for|on|of)\s+)*{TASK.pattern}",
                             re.IGNORECASE)
    sections: list[tuple[tuple[str, ...], str, list[TextItem]]] = [((), "", [])]
    for item in document.texts:
        if item.label == DocItemLabel.SECTION_HEADER:
            heading = SECTION_NUMBER.match(item.text)
            path = tuple(heading[1].split(".")) if heading else ()
            sections.append((path, item.text, []))
        elif item.label == DocItemLabel.TEXT:
            sections[-1][2].append(item)

    def cite(*items: TextItem) -> tuple[Evidence, ...]:
        return tuple(item_evidence(item, document, page, f"experiment-{index}", item.text)
                     for index, item in enumerate(items))

    names = [re.compile(rf"(?<![\w-]){re.escape(name)}(?![\w-])", re.IGNORECASE) for name in sorted(datasets)]
    transfer = re.compile(rf"\bbeyond\s+{TASK.pattern}", re.IGNORECASE)
    for index, (path, _, paragraphs) in enumerate(sections):
        for comparison in paragraphs:
            if direct_task.search(comparison.text):
                return cite(comparison)
            if len(path) < 2 or not table_reference.search(comparison.text):
                continue
            if not all(name.search(comparison.text) for name in names):
                continue

            # A following transfer subsection can clarify the scope if the table reference,
            # dataset setup and QA overview already describe the same experiment.
            parent = path[:-1]
            overview = setup = None
            for previous, title, text in reversed(sections[:index]):
                if previous == parent and re.search(r"\bexperiments?\b", title, re.IGNORECASE):
                    overview = next((item for item in text if QA.search(item.text)), None)
                    break
                if previous[:-1] != parent:
                    break
                if re.search(r"\bexperimental setup\b", title, re.IGNORECASE):
                    setup = next((item for item in text if all(name.search(item.text) for name in names)), None)
            if overview and setup and index + 1 < len(sections):
                following, title, text = sections[index + 1]
                if following[:-1] == parent and re.search(r"\btransfer\b", title, re.IGNORECASE):
                    scope = next((item for item in text if transfer.search(item.text)), None)
                    if scope:
                        return cite(overview, setup, comparison, scope)
    return ()


def read_table(pdf: Path, page: int, number: int, *, region: TableRegion | None = None) -> TableRead:
    versions = docling_versions("rapidocr")
    backend = ocr_backend()
    if backend == 'onnxruntime':
        versions['onnxruntime'] = version('onnxruntime')
    options = docling_options(
        do_ocr=True,
        do_table_structure=True,
        generate_parsed_pages=True,
        ocr_options=RapidOcrOptions(backend=backend, lang=["en"], mode=OcrMode.FULL_PAGE, scale=3),
    )
    torch.set_num_threads(options.accelerator_options.num_threads)
    result = converter_for(options).convert(pdf, page_range=(page, page), raises_on_error=False)
    document = result.document
    parsed = next((item.parsed_page for item in result.pages if item.page_no == page), None)
    raw = {}
    remarks = record_conversion(result, raw, Source(page, number, evidence_ids=(f"page-{page}",)))
    if parsed is not None:
        raw["ocr-page.json"] = parsed.model_dump(mode="json")
    settings = {"pipeline": options.model_dump(mode="json")}
    evidence: tuple[Evidence, ...] = ()
    if result.status not in {ConversionStatus.SUCCESS, ConversionStatus.PARTIAL_SUCCESS}:
        return TableRead(remarks=remarks, versions=versions, settings=settings, raw_artifacts=raw)

    try:
        if region is None:
            table = anchored_table(document, page, number)
            evidence = table_evidence(table, document, page, number)
        else:
            evidence = region_evidence(page, region)
        bounds = evidence[0].box
        if parsed is None or bounds is None:
            raise ValueError("Docling не вернул координаты таблицы или OCR-фрагменты страницы.")
        fragments = ocr_fragments(parsed, document, page, bounds)
        evidence += tuple(Evidence(item.id, page, item.text, item.box, "text_fragment") for item in fragments)
        grid, cells = recognize_grid(fragments, number)
        if region is not None:
            grid = replace(grid, label=region.designation or "Table region")
    except ValueError as error:
        source = Source(page, number, evidence_ids=("table",) if evidence else ())
        return TableRead(evidence=evidence, remarks=(*remarks, Remark(str(error), "", source)),
                         versions=versions, settings=settings, raw_artifacts=raw)

    placed_ids = {identifier for cell in grid.cells for identifier in cell.evidence_ids}
    unplaced = [item for item in fragments if item.id not in placed_ids]
    grid = replace(grid, unplaced_evidence_ids=tuple(item.id for item in unplaced))
    grid_remarks = []
    for item in unplaced:
        numeric = MEASUREMENT.match(item.text) is not None
        text = "числовой OCR-фрагмент" if numeric else "OCR-фрагмент"
        grid_remarks.append(Remark(f"Не удалось однозначно расположить {text} в сетке.", item.text,
                                  Source(page, number, box=item.box, evidence_ids=(item.id,)),
                                  kind="reading-defect" if numeric else None))
    for cell in cells:
        if cell.headers and cell.box is not None:
            continue
        if not cell.text.strip():
            reason = "В ожидаемой ячейке сетки не найден OCR-фрагмент."
        elif cell.box is None:
            reason = "Несколько OCR-фрагментов отнесены к одной ячейке."
        else:
            reason = "Не подтверждена шапка числового столбца."
        grid_remarks.append(Remark(reason, cell.text, Source(page, number, cell.row, cell.column, box=cell.box),
                                  kind="reading-defect"))
    remarks += tuple(grid_remarks)

    caption = evidence[1].text
    datasets = {cell.text for cell in grid.cells if cell.row == 1 and cell.role == "column_header"}
    label = region.author_label if region is not None else str(number)
    context = experiment_evidence(document, page, label, datasets) if label else ()
    if label and not context and not TASK.search(caption) and page < result.input.page_count:
        next_page = page + 1
        context_options = options.model_copy(update={"do_table_structure": False, "generate_parsed_pages": False})
        context_result = converter_for(context_options).convert(
            pdf, page_range=(next_page, next_page), raises_on_error=False,
        )
        settings["context"] = {"page": next_page, "pipeline": context_options.model_dump(mode="json")}
        context_source = Source(next_page, number, evidence_ids=("context-page",))
        context_remarks = record_conversion(context_result, raw, context_source, context=True)
        if context_remarks:
            remarks += context_remarks
            evidence += (Evidence("context-page", next_page, "", None, "page"),)
        if context_result.status in {ConversionStatus.SUCCESS, ConversionStatus.PARTIAL_SUCCESS}:
            context = experiment_evidence(context_result.document, next_page, label, datasets)

    return TableRead(
        table=ReadTable(page, number, caption, "\n".join(item.text for item in context), cells,
                        caption_evidence_id=grid.caption_evidence_id),
        grid=grid,
        evidence=(*evidence, *context),
        remarks=remarks,
        context_evidence_ids=tuple(item.id for item in context),
        versions=versions,
        settings=settings,
        raw_artifacts=raw,
    )
