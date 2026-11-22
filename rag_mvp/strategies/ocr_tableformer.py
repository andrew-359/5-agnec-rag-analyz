"""Read a scanned TableFormer grid and verify its assignments with positioned OCR."""

import re
from collections import defaultdict
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path
from statistics import median
from typing import TYPE_CHECKING

from ..core.domain import DATASET_DESCRIPTION, NUMBER, QUESTION_DATASET, Box, ReadCell, ReadTable, Remark, Source
from ..run import Evidence, GridCell, TableGrid, TableRead

if TYPE_CHECKING:
    from ..table_region import TableRegion


DATASET_LABEL = re.compile(r"(?=.*[A-Za-z])[\w.-]+")
METRIC = re.compile(r"EM|F1|Precision|Recall|Accuracy|BLEU(?:-\d+)?|ROUGE-[\w-]+|BERTScore", re.IGNORECASE)
GENERATOR_HEADER = re.compile(r"generator|model|llm|(?:backend|backbone) (?:model|llm)", re.IGNORECASE)
SINGLE_VALUE = re.compile(rf"{NUMBER}\s*%?")


@dataclass(frozen=True)
class PositionedCell:
    row: int
    column: int
    row_span: int
    column_span: int
    text: str
    header: bool
    box: Box | None

    @property
    def position(self) -> tuple[int, int]:
        return self.row, self.column

    @property
    def evidence_id(self) -> str:
        return f"tableformer-{self.row}-{self.column}"


def center(box: Box, axis: int) -> float:
    return (box[axis] + box[axis + 2]) / 2


def coverage(inside: Box, outside: Box) -> float:
    area = (inside[2] - inside[0]) * (inside[3] - inside[1])
    width = min(inside[2], outside[2]) - max(inside[0], outside[0])
    height = min(inside[3], outside[3]) - max(inside[1], outside[1])
    return max(width, 0) * max(height, 0) / area


def mentions(text: str, label: str) -> bool:
    return bool(re.search(rf"(?<![\w-]){re.escape(label)}(?![\w-])", text, re.IGNORECASE))


def close_position(centers: dict[int, float], actual: float) -> int | None:
    distances = sorted((abs(actual - expected), index) for index, expected in centers.items())
    if len(distances) >= 2 and distances[0][0] < 0.65 * distances[1][0]:
        return distances[0][1]
    return None


def read_grid(
    cells: tuple[PositionedCell, ...], tokens: tuple[Evidence, ...], page: int, number: int, caption: Evidence,
) -> tuple[tuple[GridCell, ...], tuple[ReadCell, ...], tuple[Evidence, ...], tuple[Remark, ...]]:
    def tokens_in(cell: PositionedCell) -> tuple[Evidence, ...]:
        if cell.box is None:
            return ()
        return tuple(token for token in tokens if token.box and coverage(token.box, cell.box) >= 0.8)

    tokens_by_cell = {cell.position: tokens_in(cell) for cell in cells}

    def matching_token(cell: PositionedCell) -> Evidence | None:
        exact = tuple(token for token in tokens_by_cell[cell.position] if token.text == cell.text and token.box
                      and cell.box and coverage(cell.box, token.box) >= 0.8)
        return exact[0] if len(exact) == 1 else None

    def row_header(pattern: re.Pattern) -> int | None:
        cols = []
        for cell in cells:
            if not (cell.header and cell.column_span == 1 and pattern.fullmatch(cell.text)):
                continue
            parts = sorted(tokens_by_cell[cell.position], key=lambda item: (center(item.box, 1), center(item.box, 0)))
            if " ".join(part.text for part in parts).casefold() == cell.text.casefold():
                cols.append(cell.column)
        return cols[0] if len(cols) == 1 else None

    method_column = row_header(re.compile(r"method", re.IGNORECASE))
    generator_column = row_header(GENERATOR_HEADER)
    data = tuple(cell for cell in cells if not cell.header and re.search(r"\d", cell.text)
                 and cell.column not in {method_column, generator_column})
    single_tokens = {
        cell.position: token for cell in cells
        if not cell.header and cell.row_span == cell.column_span == 1 and (token := matching_token(cell))
    }
    values_by_column = defaultdict(list)
    row_centers = {
        row: center(token.box, 1) for (row, column), token in single_tokens.items()
        if column == generator_column
    }
    for cell in data:
        token = single_tokens.get(cell.position)
        if token and SINGLE_VALUE.fullmatch(cell.text):
            values_by_column[cell.column].append((cell.row, token))
    column_centers = {
        column: median(center(token.box, 0) for _, token in values)
        for column, values in values_by_column.items() if len(values) >= 2
    }
    columns_with_rows = {
        **values_by_column,
        method_column: [(row, token) for (row, column), token in single_tokens.items() if column == method_column],
    }
    offsets = {}
    for column, values in columns_with_rows.items():
        shifts = [center(token.box, 1) - row_centers[row] for row, token in values if row in row_centers]
        if len(shifts) >= 2:
            offsets[column] = median(shifts)

    def agrees_with_row(row: int, column: int, token: Evidence) -> bool:
        return column in offsets and close_position(row_centers, center(token.box, 1) - offsets[column]) == row

    datasets = defaultdict(list)
    for cell in cells:
        if not (cell.header and cell.row == 0 and cell.column_span > 1):
            continue
        columns = range(cell.column, cell.column + cell.column_span)
        centers = [column_centers.get(column) for column in columns]
        if any(value is None for value in centers) or centers != sorted(centers) or centers[0] == centers[-1]:
            continue
        middle = (centers[0] + centers[-1]) / 2
        pitch = (centers[-1] - centers[0]) / (cell.column_span - 1)
        names = [
            token for token in tokens_by_cell[cell.position]
            if DATASET_LABEL.fullmatch(token.text) and not METRIC.fullmatch(token.text)
            and DATASET_DESCRIPTION.search(caption.text) and mentions(caption.text, token.text)
            and abs(center(token.box, 0) - middle) < 0.35 * pitch
        ]
        for column in columns:
            datasets[column].append(names[0] if len(names) == 1 else None)

    header_boxes = [cell.box for cell in cells if cell.header and cell.box]
    metrics = defaultdict(list)
    if header_boxes:
        top = min(box[1] for box in header_boxes)
        bottom = max(box[3] for box in header_boxes)
        for token in tokens:
            if token.box and top <= center(token.box, 1) <= bottom and METRIC.fullmatch(token.text):
                column = close_position(column_centers, center(token.box, 0))
                if column is not None:
                    metrics[column].append(token)

    result_cells = []
    remarks = []
    published_fragments = {}
    for cell in data:
        row, column = cell.position
        token = single_tokens.get(cell.position)
        method = single_tokens.get((row, method_column))
        generator = single_tokens.get((row, generator_column))
        dataset = datasets[column][0] if len(datasets[column]) == 1 else None
        metric = metrics[column][0] if len(metrics[column]) == 1 else None
        reason = None
        if token is None or not SINGLE_VALUE.fullmatch(cell.text):
            reason = "OCR не подтверждает одно число в ячейке."
        elif not dataset or not metric:
            reason = "Не подтверждена связь с датасетом или метрикой в шапке таблицы."
        elif not (method and generator and agrees_with_row(row, method_column, method)
                  and agrees_with_row(row, column, token)):
            reason = "OCR не подтверждает принадлежность числа строке метода и генератора."
        if reason:
            evidence_ids = (cell.evidence_id,) if cell.box else ("table",)
            remarks.append(Remark(reason, cell.text, Source(page, number, row, column, box=cell.box, evidence_ids=evidence_ids),
                                  kind="reading-defect"))
            continue
        published_fragments[cell.position] = Evidence(cell.evidence_id, page, token.text, token.box, "cell")
        result_cells.append(ReadCell(
            row, column, method.text,
            (f"Generator: {generator.text}", f"Dataset: {dataset.text}", f"Metric: {metric.text}"),
            token.text, token.box, verified_condition_sources={
                "method": (method.id,), "generator": (generator.id,), "dataset": (dataset.id,),
                "metric": (metric.id,), "experiment": (caption.id,),
            },
        ))

    grid = tuple(
        GridCell(cell.row, cell.column, cell.text, cell.row_span, cell.column_span,
                 "column_header" if cell.header else "row_header" if cell.column in {method_column, generator_column}
                 else "data", (cell.evidence_id,) if cell.box else ("table",))
        for cell in cells
    )
    fragments = tuple(
        published_fragments.get(cell.position) or Evidence(
            cell.evidence_id, page, cell.text, cell.box, "estimated_cell",
        )
        for cell in cells if cell.box
    )
    return grid, tuple(result_cells), (*fragments, *tokens), tuple(remarks)


def read_table(pdf: Path, page: int, number: int, *, region: "TableRegion | None" = None) -> TableRead:
    import torch
    from docling.datamodel.base_models import ConversionStatus
    from docling.datamodel.pipeline_options import OcrMode, RapidOcrOptions
    from docling_core.types.doc import DocItemLabel

    from ..ocr_discovery import ocr_backend
    from .parser import (
        anchored_table, converter_for, docling_options, docling_versions, item_evidence, normalized_box,
        record_conversion, region_evidence, table_evidence, table_in_region,
    )

    backend = ocr_backend()
    versions = docling_versions("rapidocr")
    if backend == "onnxruntime":
        versions["onnxruntime"] = version("onnxruntime")
    options = docling_options(
        do_ocr=True, do_table_structure=True, generate_parsed_pages=True,
        ocr_options=RapidOcrOptions(backend=backend, lang=["en"], mode=OcrMode.FULL_PAGE, scale=2),
    )
    torch.set_num_threads(options.accelerator_options.num_threads)
    converter = converter_for(options)
    result = converter.convert(pdf, page_range=(page, page), raises_on_error=False)
    raw = {}
    remarks = record_conversion(result, raw, Source(page, number, evidence_ids=(f"page-{page}",)))
    settings = {"pipeline": options.model_dump(mode="json")}
    metadata = dict(versions=versions, settings=settings, raw_artifacts=raw)
    evidence = region_evidence(page, region) if region else ()
    if result.status not in {ConversionStatus.SUCCESS, ConversionStatus.PARTIAL_SUCCESS}:
        return TableRead(evidence=evidence, remarks=remarks, **metadata)

    document = result.document
    parsed = next((item.parsed_page for item in result.pages if item.page_no == page), None)
    if parsed is not None:
        raw["ocr-page.json"] = parsed.model_dump(mode="json")
    try:
        table = table_in_region(document, page, region) if region else anchored_table(document, page, number)
        table_fragment, caption = evidence or table_evidence(table, document, page, number)
        evidence = (table_fragment, caption)
        if parsed is None or table_fragment.box is None:
            raise ValueError("Docling не вернул координаты таблицы или OCR-фрагменты страницы.")
    except ValueError as error:
        return TableRead(evidence=evidence, remarks=(*remarks, Remark(str(error), "", Source(page, number))), **metadata)

    size = document.pages[page].size
    tokens = tuple(
        Evidence(f"ocr-{page}-{index}", page, line.text.strip(), box, "text_fragment")
        for index, line in enumerate(parsed.textline_cells)
        if line.text.strip() and line.confidence >= 0.85
        and (box := normalized_box(line.rect.to_bounding_box(), size)) and coverage(box, table_fragment.box) >= 0.8
    )
    positioned_cells = tuple(
        PositionedCell(
            cell.start_row_offset_idx, cell.start_col_offset_idx, cell.row_span, cell.col_span,
            cell.text.strip(), cell.column_header, normalized_box(cell.bbox, size),
        ) for cell in table.data.table_cells
    )
    grid, readings, fragments, grid_remarks = read_grid(positioned_cells, tokens, page, number, caption)

    datasets = {header.removeprefix("Dataset: ") for cell in readings for header in cell.headers
                if header.startswith("Dataset: ")}
    context = []
    designation = region.designation if region else f"Table {number}"
    if datasets and designation:
        reference = re.compile(rf"\b{re.escape(designation)}\b", re.IGNORECASE)
        comparison = [
            item_evidence(item, document, page, f"comparison-{index}", item.text)
            for index, item in enumerate(document.texts)
            if item.label == DocItemLabel.TEXT and reference.search(item.text)
            and any(mentions(item.text, dataset) for dataset in datasets)
        ]
        linked = {dataset for dataset in datasets if any(mentions(item.text, dataset) for item in comparison)}

        def dataset_context(source, source_page: int) -> list[Evidence]:
            return [
                item_evidence(item, source, source_page, f"dataset-{source_page}-{index}", item.text)
                for index, item in enumerate(source.texts)
                if item.label == DocItemLabel.TEXT and DATASET_DESCRIPTION.search(item.text)
                and QUESTION_DATASET.search(" ".join(item.text.split())) and any(mentions(item.text, name) for name in linked)
            ]

        context = comparison + dataset_context(document, page)
        if linked and page > 1 and not all(
            any(mentions(item.text, dataset) and QUESTION_DATASET.search(item.text) for item in context)
            for dataset in linked
        ):
            context_options = options.model_copy(update={"do_table_structure": False, "generate_parsed_pages": False})
            context_result = converter_for(context_options).convert(pdf, page_range=(page - 1, page - 1), raises_on_error=False)
            settings["context"] = {"page": page - 1, "pipeline": context_options.model_dump(mode="json")}
            context_remarks = record_conversion(
                context_result, raw, Source(page, number, evidence_ids=("table",)), context=True,
            )
            if context_remarks:
                remarks += context_remarks
            if context_result.status in {ConversionStatus.SUCCESS, ConversionStatus.PARTIAL_SUCCESS}:
                context += dataset_context(context_result.document, page - 1)

    return TableRead(
        table=ReadTable(page, number, caption.text, "\n".join(item.text for item in context), readings, caption.id),
        grid=TableGrid(table_fragment.text, table.data.num_rows, table.data.num_cols, (table_fragment.id,), caption.id, grid),
        evidence=(*evidence, *fragments, *context),
        context_evidence_ids=tuple(item.id for item in context),
        remarks=(*remarks, *grid_remarks),
        **metadata,
    )
