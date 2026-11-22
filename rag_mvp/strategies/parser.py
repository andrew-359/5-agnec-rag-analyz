import os
import re
from contextlib import ExitStack, closing
from importlib.metadata import version
from pathlib import Path

from docling.datamodel.accelerator_options import AcceleratorDevice, AcceleratorOptions
from docling.datamodel.base_models import ConversionStatus, InputFormat
from docling.datamodel.document import ConversionResult
from docling.datamodel.pipeline_options import PdfPipelineOptions, TableFormerMode, TableStructureOptions
from docling.document_converter import DocumentConverter, PdfFormatOption
from docling_core.types.doc import BoundingBox, DoclingDocument, Size, TableItem
from docling_core.types.doc.items.node import DocItem
from docling_core.types.doc.labels import DocItemLabel
from pypdfium2 import PdfDocument

from ..core.domain import (
    MULTI_HOP_TASK, NUMBER, Box, ReadCell, ReadTable, Remark, RowGroup, Source,
    caption_has_answer_f1_sem, generator_group_dataset,
)
from ..run import Evidence, GridCell, TableGrid, TableRead
from ..table_region import TableRegion, shared_area
from .docling_page_cache import CachedDoclingPage, cache_file, lock_page, read_page, write_page


def normalized_box(bounds: BoundingBox | None, size: Size) -> Box | None:
    if bounds is None:
        return None
    box = bounds.to_top_left_origin(size.height).normalized(size)
    if not (0 <= box.l < box.r <= 1 and 0 <= box.t < box.b <= 1):
        return None
    return box.as_tuple()


def anchored_table(document: DoclingDocument, page: int, number: int) -> TableItem:
    caption_start = re.compile(rf"^\s*Table\s+{number}\b", re.IGNORECASE)
    matches = [table for table in document.tables if caption_start.match(table.caption_text(document))
               and any(location.page_no == page for location in table.prov)]
    if len(matches) != 1:
        raise ValueError(f"На странице {page} ожидалась одна Table {number}, найдено {len(matches)}.")
    return matches[0]


def table_in_region(document: DoclingDocument, page: int, region: TableRegion) -> TableItem:
    candidates = []
    for table in document.tables:
        box = item_evidence(table, document, page, "table", "").box
        if box is not None and shared_area(box, region.box) >= .65:
            candidates.append(table)
    if len(candidates) != 1:
        raise ValueError(f"В заданной области страницы {page} ожидалась одна таблица, найдено {len(candidates)}.")
    return candidates[0]


def region_evidence(page: int, region: TableRegion) -> tuple[Evidence, Evidence]:
    designation = region.designation or ""
    caption = region.caption_text if region.caption_status == "confirmed" else None
    caption_box = region.caption_box if caption else None
    return (
        Evidence("table", page, designation, region.box, "table"),
        Evidence("caption", page, caption or "", caption_box, "text_fragment" if caption_box else "page"),
    )


def item_evidence(item: DocItem, document: DoclingDocument, page: int, identifier: str, text: str) -> Evidence:
    location = next((location for location in item.prov if location.page_no == page), None)
    box = normalized_box(location.bbox, document.pages[page].size) if location else None
    precision = "table" if isinstance(item, TableItem) else "text_fragment"
    return Evidence(identifier, page, text, box, precision if box else "page")


def table_evidence(table: TableItem, document: DoclingDocument, page: int, number: int) -> tuple[Evidence, ...]:
    boxes = []
    for ref in table.captions:
        item = ref.resolve(document)
        if not item.prov or any(location.page_no != page for location in item.prov):
            boxes.clear()
            break
        bounds = [normalized_box(location.bbox, document.pages[page].size) for location in item.prov]
        if any(box is None for box in bounds):
            boxes.clear()
            break
        boxes.extend(bounds)
    box = None
    if boxes:
        left, top, right, bottom = zip(*boxes, strict=True)
        box = (min(left), min(top), max(right), max(bottom))
    return (
        item_evidence(table, document, page, "table", f"Table {number}"),
        Evidence("caption", page, table.caption_text(document), box, "text_fragment" if box else "page"),
    )


def find_grouped_table(document: DoclingDocument, page: int, number: int) -> tuple[TableItem, Evidence] | None:
    """Recover a generator comparison table whose caption was detached by Docling."""
    start = re.compile(rf"^\s*Table\s+{number}\b", re.IGNORECASE)
    captions = [
        item_evidence(item, document, page, "caption", item.text)
        for item in document.texts if item.label == DocItemLabel.CAPTION
    ]
    matches = [item for item in captions if start.match(item.text) and generator_group_dataset(item.text)]
    if len(matches) != 1 or matches[0].box is None:
        return None
    caption = matches[0]
    left, top, right, _ = caption.box
    center = (left + right) / 2
    grids = [(table, item_evidence(table, document, page, "table", "").box) for table in document.tables]
    tables = []
    for table, box in grids:
        if box is None or not (box[0] <= center <= box[2] and box[3] <= top):
            continue
        if any(item.box and item.box[0] <= center <= item.box[2] and box[3] <= item.box[1] < top
               for item in captions):
            continue
        if table.captions:
            if len(table.captions) != 1:
                continue
            attached = table.captions[0].resolve(document)
            attached_box = item_evidence(attached, document, page, "attached-caption", "").box
            if attached_box is None or not (attached_box[0] <= center <= attached_box[2]):
                continue
            # A caption between stacked grids can be attached to the wrong one.
            # Only detach it when a unique uncaptioned grid above it is closer.
            gap = box[1] - attached_box[3]
            preceding = [other for other, bounds in grids
                         if not other.captions and bounds and bounds[0] <= center <= bounds[2]
                         and 0 <= attached_box[1] - bounds[3] < gap]
            if len(preceding) != 1:
                continue
        tables.append(table)
    return (tables[0], caption) if len(tables) == 1 else None


def recover_f1_cell_text(pdf: Path, page: int, box: Box, docling_text: str) -> str:
    with PdfDocument(pdf) as document, closing(document[page - 1]) as pdf_page:
        width, height = pdf_page.get_size()
        left, top, right, bottom = box
        with closing(pdf_page.get_textpage()) as text_page:
            text = text_page.get_text_bounded(left * width, (1 - bottom) * height, right * width, (1 - top) * height)
    # Recover the missing delimiters only when both readers agree on the numeric text.
    if (re.fullmatch(rf"\s*{NUMBER}\s*\(\s*±\s*{NUMBER}\s*\)\s*", text)
            and re.sub(r"[\s(±]", "", text) == re.sub(r"[\s(±]", "", docling_text)):
        return text.strip()
    return docling_text


def docling_options(**overrides) -> PdfPipelineOptions:
    return PdfPipelineOptions(
        table_structure_options=TableStructureOptions(mode=TableFormerMode.ACCURATE),
        accelerator_options=AcceleratorOptions(num_threads=4, device=AcceleratorDevice.CPU),
        **overrides,
    )


def converter_for(options: PdfPipelineOptions) -> DocumentConverter:
    return DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)})


def docling_versions(*extra: str) -> dict[str, str]:
    return {name: version(name) for name in ("docling", "docling-core", "docling-parse", "docling-ibm-models", "torch", *extra)}


def record_conversion(
    result: ConversionResult | CachedDoclingPage, raw: dict, source: Source, *, context: bool = False,
) -> tuple[Remark, ...]:
    stem = "docling-context" if context else "docling"
    raw[f"{stem}.json"] = result.document.export_to_dict()
    raw[f"{stem}-diagnostics.json"] = {
        "status": result.status.value,
        "errors": [item.model_dump(mode="json") for item in result.errors],
    }
    if result.status == ConversionStatus.SUCCESS and not result.errors:
        return ()
    location = "страницы контекста" if context else "страницы таблицы"
    reason = f"Статус обработки {location} в Docling — {result.status.value}."
    details = "\n".join(item.error_message for item in result.errors)
    return (Remark(reason, details, source),)


def convert_table_page(
    pdf: Path, page: int, options: PdfPipelineOptions, versions: dict[str, str],
) -> ConversionResult | CachedDoclingPage:
    directory = os.environ.get("RAG_DOCLING_PAGE_CACHE")
    with ExitStack() as resources:
        path = None
        if directory:
            try:
                candidate = cache_file(Path(directory), pdf, page, versions, options.model_dump())
                lock_page(candidate, resources)
            except OSError:
                pass  # Reading the PDF still works if the cache is unavailable.
            else:
                path = candidate
                if cached := read_page(path, page):
                    return cached

        result = converter_for(options).convert(pdf, page_range=(page, page), raises_on_error=False)
        if path is not None and result.status == ConversionStatus.SUCCESS and not result.errors:
            try:
                write_page(path, result.document)
            except OSError:
                pass
        return result


def read_table(pdf: Path, page: int, number: int, *, region: TableRegion | None = None) -> TableRead:
    options = docling_options(do_ocr=False, do_table_structure=True)
    versions = docling_versions()
    result = convert_table_page(pdf, page, options, versions)
    document = result.document
    source = Source(page, number)
    raw = {}
    remarks = record_conversion(result, raw, Source(page, number, evidence_ids=(f"page-{page}",)))
    metadata = dict(
        versions=versions,
        settings={"pipeline": options.model_dump(mode="json")},
        raw_artifacts=raw,
    )
    if result.status not in {ConversionStatus.SUCCESS, ConversionStatus.PARTIAL_SUCCESS}:
        return TableRead(remarks=remarks, **metadata)
    if region is not None:
        evidence = list(region_evidence(page, region))
        try:
            table = table_in_region(document, page, region)
        except ValueError as error:
            source = Source(page, number, evidence_ids=("table",))
            return TableRead(evidence=tuple(evidence), remarks=(*remarks, Remark(str(error), "", source)), **metadata)
    else:
        try:
            table = anchored_table(document, page, number)
        except ValueError as error:
            recovered = find_grouped_table(document, page, number)
            if recovered is None:
                return TableRead(remarks=(*remarks, Remark(str(error), "", source)), **metadata)
            table, caption_fragment = recovered
            evidence = [item_evidence(table, document, page, "table", f"Table {number}"), caption_fragment]
        else:
            evidence = list(table_evidence(table, document, page, number))

    caption = evidence[1].text
    designation = evidence[0].text or "Table region"
    grouped_dataset = generator_group_dataset(caption)
    size = document.pages[page].size
    cells = table.data.table_cells
    headers = sorted((cell for cell in cells if cell.column_header), key=lambda cell: cell.start_row_offset_idx)
    header_refs = {
        (header.start_row_offset_idx, header.start_col_offset_idx):
            f"docling-{header.start_row_offset_idx}-{header.start_col_offset_idx}"
        for header in headers if normalized_box(header.bbox, size)
    }
    header_columns = {
        cell.text.strip().casefold(): cell.start_col_offset_idx
        for cell in headers if cell.col_span == 1 and cell.start_row_offset_idx == 0
    }
    method_column = header_columns.get("method", 0)
    f1_column = header_columns.get("f1") if grouped_dataset and caption_has_answer_f1_sem(caption) else None
    type_column = header_columns.get("type") if "method" in header_columns else None
    row_header_columns = {method_column, type_column}
    methods = {
        cell.start_row_offset_idx: cell.text.strip()
        for cell in cells
        if not cell.column_header and cell.start_col_offset_idx == method_column and cell.col_span == 1
    }
    groups = {
        cell.start_row_offset_idx: cell
        for cell in cells
        if grouped_dataset and cell.row_section and cell.start_col_offset_idx == method_column
    }
    groups_by_row: dict[int, RowGroup | None] = {}
    group = None
    for row in range(table.data.num_rows):
        if row in groups:
            header = groups[row]
            group = None
            if header.text.strip() and normalized_box(header.bbox, size):
                group = RowGroup(header.text, f"docling-{row}-{method_column}")
        groups_by_row[row] = group
    items: list[ReadCell] = []
    grid: list[GridCell] = []
    for cell in cells:
        row, column = cell.start_row_offset_idx, cell.start_col_offset_idx
        box = normalized_box(cell.bbox, size)
        text = cell.text
        if (column == f1_column and row not in groups and not cell.column_header and box
                and cell.col_span == cell.row_span == 1 and "(" not in text and text.rstrip().endswith(")")):
            text = recover_f1_cell_text(pdf, page, box, text)
        fragment_id = f"docling-{row}-{column}"
        if box:
            evidence.append(Evidence(fragment_id, page, text, box, "cell"))
        role = "column_header" if cell.column_header else "row_header" if column in row_header_columns else "data"
        grid.append(GridCell(row, column, text, cell.row_span, cell.col_span, role,
                             (fragment_id,) if box else ("table",)))
        if cell.column_header or column in row_header_columns or row in groups:
            continue
        covering = [
            header for header in headers
            if header.start_col_offset_idx <= column < header.end_col_offset_idx and header.end_row_offset_idx <= row
        ]
        path = ()
        header_evidence_ids = ()
        separate_levels = len({header.start_row_offset_idx for header in covering}) == len(covering)
        if cell.col_span == cell.row_span == 1 and separate_levels:
            path = tuple(header.text.strip() for header in covering)
            header_evidence_ids = tuple(
                header_refs.get((header.start_row_offset_idx, header.start_col_offset_idx)) for header in covering
            )
        items.append(ReadCell(
            row=row,
            column=column,
            method=methods.get(row, ""),
            headers=path,
            text=text,
            box=box,
            row_group=groups_by_row.get(row),
            header_evidence_ids=header_evidence_ids,
        ))

    context: list[str] = []
    context_evidence: list[Evidence] = []
    scope = r"\bablation study\b" if re.search(r"\bablation study\b", caption, re.I) else r"\bexperimental setup\b"
    for fragment in document.texts:
        if fragment.label == DocItemLabel.SECTION_HEADER:
            if context:
                break
            if not re.search(scope, fragment.text, re.IGNORECASE):
                continue
        elif not (context and fragment.label == DocItemLabel.TEXT):
            continue
        context.append(fragment.text)
        context_evidence.append(item_evidence(fragment, document, page, f"context-{len(context)}", fragment.text))

    if grouped_dataset and not MULTI_HOP_TASK.search(" ".join(context)):
        dataset = re.compile(rf"(?<!\w){re.escape(grouped_dataset)}(?!\w)", re.IGNORECASE)
        for context_page in range(page, max(0, page - 2), -1):
            context_document = document
            if context_page != page:
                context_options = docling_options(do_ocr=False, do_table_structure=False)
                context_result = convert_table_page(pdf, context_page, context_options, versions)
                metadata["settings"]["context"] = {"page": context_page, "pipeline": context_options.model_dump(mode="json")}
                context_remarks = record_conversion(
                    context_result, raw, Source(context_page, number, evidence_ids=("context-page",)), context=True,
                )
                if context_remarks:
                    remarks += context_remarks
                    evidence.append(Evidence("context-page", context_page, "", None, "page"))
                context_document = context_result.document
            linked = [
                item for item in context_document.texts
                if item.label == DocItemLabel.TEXT and dataset.search(item.text) and MULTI_HOP_TASK.search(item.text)
            ]
            if linked:
                for item in linked:
                    context.append(item.text)
                    context_evidence.append(item_evidence(
                        item, context_document, context_page, f"context-{len(context)}", item.text,
                    ))
                break

    return TableRead(
        table=ReadTable(page, number, caption, "\n".join(context), tuple(items), caption_evidence_id="caption"),
        grid=TableGrid(designation, table.data.num_rows, table.data.num_cols, ("table",), "caption", tuple(grid)),
        evidence=(*evidence, *context_evidence),
        remarks=remarks,
        context_evidence_ids=tuple(item.id for item in context_evidence),
        **metadata,
    )
