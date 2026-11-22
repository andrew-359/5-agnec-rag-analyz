import re
from importlib.metadata import version
from pathlib import Path

import pdfplumber
from pdfplumber.page import Page

from ..core.domain import Box, Remark, Source
from ..run import Evidence, GridCell, TableGrid, TableRead


TABLE_SETTINGS = {"vertical_strategy": "text", "horizontal_strategy": "text", "min_words_vertical": 2}
COORDINATE_SYSTEM = "PDF points, top-left, x0/top/x1/bottom"
CAPTION_MARGIN = 8
CAPTION_DEPTH = 40


def normalized_box(box: Box, page_box: Box) -> Box:
    left, top, right, bottom = box
    x, y, x1, y1 = page_box
    return ((left - x) / (x1 - x), (top - y) / (y1 - y), (right - x) / (x1 - x), (bottom - y) / (y1 - y))


def table_caption(page: Page, area: Box, page_box: Box) -> tuple[Evidence | None, int | None, Box]:
    left, _, right, bottom = area
    x0, _, x1, page_bottom = page_box
    search_box = (max(x0, left - CAPTION_MARGIN), bottom, min(x1, right + CAPTION_MARGIN),
                  min(page_bottom, bottom + CAPTION_DEPTH))
    if search_box[1] >= search_box[3]:
        return None, None, search_box

    lines = page.within_bbox(search_box).extract_text_lines(return_chars=False)
    for index, line in enumerate(lines):
        label = re.match(r"^Table\s+(\d+)\b", line["text"], re.IGNORECASE)
        if label is None:
            continue
        caption_lines = [line]
        for continuation in lines[index + 1:]:
            if (continuation["top"] - caption_lines[-1]["bottom"] > 4
                    or abs(continuation["x0"] - line["x0"]) > 4
                    or re.match(r"^Table\s+\d+\b", continuation["text"], re.IGNORECASE)):
                break
            caption_lines.append(continuation)
        text = "\n".join(item["text"] for item in caption_lines)
        box = (min(item["x0"] for item in caption_lines), line["top"],
               max(item["x1"] for item in caption_lines), caption_lines[-1]["bottom"])
        caption = Evidence("caption", page.page_number, text, normalized_box(box, page_box), "text_fragment")
        return caption, int(label[1]), search_box
    return None, None, search_box


def read_table(pdf: Path, page: int, number: int, *, crop: Box) -> TableRead:
    with pdfplumber.open(pdf) as document:
        source_page = document.pages[page - 1]
        media = source_page.mediabox
        visible = source_page.cropbox
        page_box = (max(media[0], visible[0]), max(media[1], visible[1]),
                    min(media[2], visible[2]), min(media[3], visible[3]))
        x0, top, x1, bottom = crop
        left, page_top, right, page_bottom = page_box
        if not (0 <= x0 < x1 <= right - left and 0 <= top < bottom <= page_bottom - page_top):
            raise ValueError(f"--crop должен задавать ненулевую область внутри страницы {page} в порядке X0 TOP X1 BOTTOM.")
        area = source_page.crop((left + x0, page_top + top, left + x1, page_top + bottom))
        tables = [{"box": table.bbox, "rows": table.extract(), "cell_boxes": [row.cells for row in table.rows]}
                  for table in area.find_tables(TABLE_SETTINGS)]
        raw = {"page": page, "page_box": page_box, "crop_box": area.bbox, "tables": tables, "words": area.extract_words()}
        metadata = dict(
            versions={name: version(name) for name in ("pdfplumber", "pdfminer.six")},
            settings={"crop": crop, "coordinate_system": COORDINATE_SYSTEM, "table_settings": TABLE_SETTINGS},
            raw_artifacts={"pdfplumber.json": raw},
        )
        label = f"Table {number}"
        source = Source(page, number)
        if len(tables) != 1:
            reason = f"В заданной области для {label} ожидалась одна таблица, найдено {len(tables)}."
            return TableRead(remarks=(Remark(reason, "", source),), **metadata)

        (table,) = tables
        caption_area = (area.bbox[0], area.bbox[1], area.bbox[2], table["box"][3])
        caption, observed_number, search_box = table_caption(source_page, caption_area, page_box)
        raw.update(caption_search_box=search_box, caption=caption.text if caption else None,
                   observed_table=observed_number)

    confirmed = observed_number == number
    grid_label = label if confirmed else "Область таблицы"
    if caption and not confirmed:
        grid_label += f" · найдена подпись Table {observed_number}"
    evidence = [Evidence("table", page, grid_label, normalized_box(table["box"], page_box), "table")]
    remarks = []
    if caption:
        evidence.append(caption)
        if not confirmed:
            remarks.append(Remark(f"Запрошена {label}, рядом с областью найдена подпись Table {observed_number}.",
                                  caption.text, Source(page, number, evidence_ids=(caption.id,))))
    else:
        remarks.append(Remark(f"Подпись {label} не подтверждена вблизи области.", "", source))
    rows = [(texts, boxes) for texts, boxes in zip(table["rows"], table["cell_boxes"], strict=True)
            if any(text and text.strip() for text in texts)]
    columns = max((len(texts) for texts, _ in rows), default=0)
    if not rows:
        remarks.append(Remark("В заданной области нет читаемых ячеек.", "", source))
        return TableRead(evidence=tuple(evidence), remarks=tuple(remarks), **metadata)

    latency = bool(confirmed and caption and re.search(r"\binference latency\b", caption.text, re.IGNORECASE))
    cells = []
    for row, (texts, boxes) in enumerate(rows):
        for column, (text, box) in enumerate(zip(texts, boxes, strict=True)):
            if box is None:
                continue
            fragment_id = f"pdfplumber-{row}-{column}"
            evidence.append(Evidence(fragment_id, page, text or "", normalized_box(box, page_box), "cell"))
            role = None
            if latency:
                role = "column_header" if row == 0 else "row_header" if column == 0 else "data"
            cells.append(GridCell(row, column, text or "", role=role, evidence_ids=(fragment_id,)))

    if latency:
        reason = "Для замера задержки неизвестны генератор, датасет, оборудование и длина генерации."
    else:
        reason = "Привязка результатов метрик к условиям эксперимента для этой сетки не подтверждена."
    grid = TableGrid(grid_label, len(rows), columns, ("table",), caption.id if caption and confirmed else None, tuple(cells))
    remarks.append(Remark(reason, "", source))
    return TableRead(grid=grid, evidence=tuple(evidence), remarks=tuple(remarks), **metadata)
