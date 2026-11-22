"""Read a confirmed PDF table region with PP-StructureV3."""

from contextlib import closing
from dataclasses import replace
import json
from math import isfinite
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING

from ..core.domain import Box, Evidence, ReadTable, Remark, Source
from .ocr_tableformer import PositionedCell, coverage, read_grid
from ..run import TableGrid, TableRead
from ..table_region import shared_area

if TYPE_CHECKING:
    from ..table_region import TableRegion


RASTER_SCALE = 2
PREDICTION_TIMEOUT_SECONDS = 480
TABLE_OVERLAP = 0.65
OCR_MIN_CONFIDENCE = 0.85
REGION_COVERAGE = 0.85


def predict_page(pdf: Path, page: int) -> tuple[dict, tuple[int, int]]:
    from pypdfium2 import PdfDocument

    interpreter = os.environ.get("RAG_PADDLE_PYTHON")
    if not interpreter:
        raise ValueError("Для стратегии paddle-structure задайте RAG_PADDLE_PYTHON.")
    python = Path(interpreter).expanduser().absolute()
    if not python.is_file() or not os.access(python, os.X_OK):
        raise ValueError(f"Интерпретатор Paddle недоступен: {python}")

    worker = Path(__file__).with_name("paddle_structure_worker.py")
    env = {**os.environ, "CUDA_VISIBLE_DEVICES": "", "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1",
           "MKL_NUM_THREADS": "1", "HF_HUB_DISABLE_TELEMETRY": "1"}
    with TemporaryDirectory(prefix="paddle-") as temporary:
        image = Path(temporary) / "page.png"
        output = image.with_name("paddle.json")
        with PdfDocument(pdf) as document, closing(document[page - 1]) as source:
            with closing(source.render(scale=RASTER_SCALE)) as bitmap:
                raster = bitmap.to_pil()
                size = raster.size
                raster.save(image)
        try:
            subprocess.run([str(python), "-I", "-B", str(worker), str(image), str(output)],
                           env=env, capture_output=True, text=True, errors="replace", check=True,
                           timeout=PREDICTION_TIMEOUT_SECONDS)
        except subprocess.CalledProcessError as error:
            lines = (error.stderr or error.stdout).strip().splitlines()
            detail = lines[-1] if lines else f"код {error.returncode}"
            raise ValueError(f"Не удалось прочитать страницу через PaddleOCR: {detail}") from error
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ValueError(f"Не удалось выполнить PaddleOCR: {error}") from error
        payload = json.loads(output.read_text(encoding="utf-8"))
    return payload, size


def page_box(bounds: list[float], size: tuple[int, int]) -> Box | None:
    if len(bounds) != 4 or not all(type(value) in (int, float) and isfinite(value) for value in bounds):
        return None
    width, height = size
    left, top, right, bottom = bounds
    if 0 <= left < right <= width and 0 <= top < bottom <= height:
        return left / width, top / height, right / width, bottom / height
    return None


def table_in_region(payload: dict, size: tuple[int, int], region: Box) -> dict:
    matches = [
        block for block in payload["table_blocks"]
        if (box := page_box(block["box"], size)) is not None and shared_area(box, region) >= TABLE_OVERLAP
    ]
    if len(matches) != 1:
        raise ValueError(f"В заданной области ожидалась одна таблица PaddleOCR, найдено {len(matches)}.")
    tables = [table for table in payload["table_res_list"] if table["pred_html"] == matches[0]["html"]]
    if len(tables) != 1:
        raise ValueError("PaddleOCR не связал область с единственной HTML-таблицей.")
    return tables[0]


def region_boxes(table: dict, size: tuple[int, int], region: Box) -> tuple[Box | None, ...]:
    boxes = (page_box(bounds, size) for bounds in table["cell_box_list"])
    return tuple(box if box is not None and coverage(box, region) >= REGION_COVERAGE else None for box in boxes)


def positioned_cells(html: str, boxes: tuple[Box | None, ...]) -> tuple[PositionedCell, ...]:
    from lxml import html as markup

    document = markup.fromstring(html)
    tables = document.xpath("descendant-or-self::table")
    if len(tables) != 1:
        raise ValueError("PaddleOCR не вернул одну HTML-таблицу.")
    rows = tables[0].xpath("./tr | ./thead/tr | ./tbody/tr | ./tfoot/tr")
    if sum(len(row.xpath("./td | ./th")) for row in rows) != len(boxes):
        raise ValueError("PaddleOCR вернул разное число элементов HTML и рамок ячеек.")

    cells = []
    occupied = set()
    for row, element in enumerate(rows):
        column = 0
        for td in element.xpath("./td | ./th"):
            while (row, column) in occupied:
                column += 1
            height = int(td.get("rowspan", "1"))
            width = int(td.get("colspan", "1"))
            if not (1 <= height <= len(rows) - row and 1 <= width <= len(boxes)):
                raise ValueError("PaddleOCR вернул некорректное объединение ячеек.")
            span = {(r, c) for r in range(row, row + height) for c in range(column, column + width)}
            if occupied.intersection(span):
                raise ValueError("PaddleOCR вернул пересекающиеся объединённые ячейки.")
            occupied.update(span)
            text = " ".join(td.text_content().split())
            header = element.getparent().tag == "thead" or td.tag == "th"
            cells.append(PositionedCell(row, column, height, width, text, header, boxes[len(cells)]))
            column += width
    return tuple(cells)


def positioned_tokens(table: dict, size: tuple[int, int], page: int) -> tuple[Evidence, ...]:
    ocr = table["table_ocr_pred"]
    return tuple(
        Evidence(f"paddle-ocr-{page}-{index}", page, " ".join(text.split()), box, "text_fragment")
        for index, (text, bounds, score) in enumerate(zip(ocr["rec_texts"], ocr["rec_boxes"], ocr["rec_scores"], strict=True))
        if text.strip() and score >= OCR_MIN_CONFIDENCE and (box := page_box(bounds, size))
    )


def read_table(pdf: Path, page: int, number: int, *, region: "TableRegion | None" = None) -> TableRead:
    from lxml.etree import ParserError

    from .parser import region_evidence

    if region is None or region.bounds_status != "confirmed":
        return TableRead(remarks=(Remark("Для PaddleOCR нужна подтверждённая область таблицы.", "",
                                         Source(page, number), kind="reading-defect"),))

    primary = region_evidence(page, region)
    table_fragment, caption = primary
    source = Source(page, number, box=region.box, evidence_ids=(table_fragment.id,))
    payload, size = predict_page(pdf, page)
    read = TableRead(evidence=primary, versions=payload["versions"], settings={
        "raster_scale": RASTER_SCALE, "raster_size": size, "pipeline": payload.get("options", {}),
    }, raw_artifacts={"paddle.json": payload})

    try:
        candidate = table_in_region(payload, size, region.box)
        cells = positioned_cells(candidate["pred_html"], region_boxes(candidate, size, region.box))
        tokens = tuple(token for token in positioned_tokens(candidate, size, page)
                       if coverage(token.box, region.box) >= REGION_COVERAGE)
        if not cells:
            raise ValueError("PaddleOCR вернул пустую сетку.")
    except (KeyError, ValueError, TypeError, ParserError) as error:
        return replace(read, remarks=(Remark(str(error), "", source, kind="reading-defect"),))

    grid, results, evidence, remarks = read_grid(cells, tokens, page, number, caption)
    if any(cell.box is None for cell in cells if cell.text):
        reason = "PaddleOCR вывел часть текстовых ячеек за подтверждённые границы таблицы."
        remarks = (Remark(reason, "", source, kind="reading-defect"), *remarks)
        # A shifted structure can move several cells while keeping their text and rectangles internally consistent.
        results = ()
    return replace(
        read,
        table=ReadTable(page, number, caption.text, "", results, caption.id),
        grid=TableGrid(table_fragment.text, max(cell.row + cell.row_span for cell in grid),
                       max(cell.column + cell.column_span for cell in grid), (table_fragment.id,), caption.id, grid,
                       tuple(token.id for token in tokens)),
        evidence=(*primary, *evidence),
        remarks=remarks,
    )
