import json
import os
import re
import subprocess
from dataclasses import replace
from math import isfinite
from pathlib import Path
from tempfile import TemporaryDirectory

from ..core.domain import NUMBER, Box, Remark, Source
from ..run import Evidence, GridCell, TableGrid, TableRead


def run_command(command: list[str], env: dict[str, str], label: str) -> None:
    try:
        process = subprocess.run(command, env=env, capture_output=True, text=True, errors="replace")
    except OSError as error:
        raise ValueError(f"{label}: {error}") from error
    if process.returncode:
        message = (process.stderr.strip() or process.stdout.strip())[-1800:]
        raise ValueError(f"{label} (код {process.returncode}): {message}")


def worker_output(pdf: Path, page: int) -> tuple[dict, dict]:
    value = os.environ.get("RAG_UNSTRUCTURED_PYTHON")
    if not value:
        raise ValueError("Для стратегии unstructured задайте RAG_UNSTRUCTURED_PYTHON с путём к Python отдельного окружения.")
    python = Path(value).expanduser().absolute()
    if not python.is_file() or not os.access(python, os.X_OK):
        raise ValueError(f"RAG_UNSTRUCTURED_PYTHON указывает на недоступный интерпретатор: {python}")

    env = dict(os.environ, HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", HF_HUB_DISABLE_TELEMETRY="1",
               DO_NOT_TRACK="1", SCARF_NO_ANALYTICS="1", AUTO_DOWNLOAD_NLTK="0", CUDA_VISIBLE_DEVICES="")
    label = f"Не удалось запустить unstructured ({python})"
    with TemporaryDirectory(prefix="unstructured-", dir=pdf.parent) as directory:
        output = Path(directory)
        run_command(["pdfseparate", "-f", str(page), "-l", str(page), str(pdf), str(output / "page-%d.pdf")],
                    env, "Unstructured: не удалось подготовить страницу через pdfseparate")
        worker = Path(__file__).with_name("unstructured_worker.py").absolute()
        run_command([str(python), "-I", "-B", str(worker), str(output / f"page-{page}.pdf"), str(page), str(output)],
                    env, label)

        def load(filename: str):
            return json.loads((output / filename).read_text(encoding="utf-8"))

        try:
            raw = {filename: load(filename) for filename in ("elements.json", "table-transformer-cells.json")}
            metadata = load("engine.json")
        except (OSError, ValueError) as error:
            raise ValueError(f"{label}: не удалось прочитать выход движка ({error})") from error
    return raw, metadata


def page_box(bounds: list[float], width: int, height: int) -> Box | None:
    if len(bounds) != 4 or width <= 0 or height <= 0 or not all(map(isfinite, bounds)):
        return None
    left, top, right, bottom = [max(0.0, min(1.0, value / scale))
                                for value, scale in zip(bounds, (width, height, width, height))]
    if left < right and top < bottom:
        return left, top, right, bottom
    return None


def element_box(element: dict) -> Box | None:
    coordinates = element["metadata"].get("coordinates") or {}
    if coordinates.get("system") != "PixelSpace" or not coordinates.get("points"):
        return None
    x, y = zip(*coordinates["points"])
    return page_box([min(x), min(y), max(x), max(y)], coordinates["layout_width"], coordinates["layout_height"])


def read_cells(logical: list[dict], model: dict, table: Evidence) -> tuple[tuple[GridCell, ...], tuple[Evidence, ...]]:
    model_cells = {(tuple(cell["row_nums"]), tuple(cell["column_nums"])): cell for cell in model.get("raw_cells", [])}
    cells = []
    evidence = []
    for raw in sorted(logical, key=lambda item: (item["y"], item["x"])):
        row, column, height, width, text = (raw[key] for key in ("y", "x", "h", "w", "content"))
        ranges = (tuple(range(row, row + height)), tuple(range(column, column + width)))
        matched = model_cells.get(ranges)
        role = None
        refs = (table.id,)
        if matched and matched["cell text"] == text:
            role = "column_header" if matched["column header"] else ("row_header" if column == 0 else "data")
            if model.get("crop_bbox"):
                x, y, _, _ = model["crop_bbox"]
                bounds = [value + offset for value, offset in zip(matched["bbox"], (x, y, x, y))]
                box = page_box(bounds, *model["raster_size"])
                if box:
                    identifier = f"source-cell-{row}-{column}"
                    evidence.append(Evidence(identifier, table.page, text, box, "estimated_cell"))
                    refs = (identifier, table.id)
        cells.append(GridCell(row, column, text, height, width, role, refs))
    return tuple(cells), tuple(evidence)


def read_table(pdf: Path, page: int, number: int) -> TableRead:
    raw, metadata = worker_output(pdf, page)
    run = TableRead(versions=metadata["versions"], settings=metadata["settings"], raw_artifacts=raw)
    source = Source(page, number)
    label = f"Table {number}"
    elements = [item for item in raw["elements.json"] if item["metadata"].get("page_number") == page]
    landmarks = [(index, item) for index, item in enumerate(elements) if item["type"] in {"Table", "FigureCaption"}]
    caption = re.compile(rf"^\s*Table\s+{number}\b", re.IGNORECASE)
    matches = [(index, item, next_item) for (index, item), (_, next_item) in zip(landmarks, landmarks[1:])
               if item["type"] == "Table" and next_item["type"] == "FigureCaption" and caption.match(next_item["text"])]
    if not matches:
        return replace(run, remarks=(Remark(f"Не удалось найти {label} с подписью на странице.", "", source),))
    if len(matches) > 1:
        reason = f"Положение {label} неоднозначно. Найдено таблиц с этой подписью: {len(matches)}."
        return replace(run, remarks=(Remark(reason, "", source),))
    (index, item, caption_item), = matches

    box = element_box(item)
    table = Evidence("table", page, item["text"], box, "table" if box else "page")
    caption_box = element_box(caption_item)
    title = Evidence("caption", page, caption_item["text"], caption_box, "text_fragment" if caption_box else "page")
    evidence = (table, title)
    source = replace(source, evidence_ids=(table.id,))
    logical = item["metadata"].get("table_as_cells") or []
    if not logical:
        return replace(run, evidence=evidence, remarks=(Remark("Не удалось прочитать сетку таблицы.", item["text"], source),))

    models = [candidate for candidate in raw["table-transformer-cells.json"]
              if candidate["page"] == page and box and page_box(candidate["layout_bbox"], *candidate["raster_size"]) == box]
    cells, cell_evidence = read_cells(logical, models[0] if len(models) == 1 else {}, table)
    grid = TableGrid(label, max(cell.row + cell.row_span for cell in cells),
                     max(cell.column + cell.column_span for cell in cells), (table.id,), title.id, cells)

    context_ids = ()
    for context in reversed(elements[:index]):
        if context["type"] == "NarrativeText" and re.search(r"\bROC[-‐‑–\s]+AUC\b", context["text"], re.IGNORECASE):
            context_box = element_box(context)
            if box and context_box and context_box[3] <= box[1]:
                evidence += (Evidence("metric-context", page, context["text"], context_box, "text_fragment"),)
                context_ids = ("metric-context",)
                break

    remarks = [Remark("Для подтверждения результатов метрик нужно сверить шапку и условия эксперимента.", "", source)]
    numeric_columns = {cell.column for cell in cells if cell.role == "data" and re.fullmatch(NUMBER, cell.text.strip())}
    for cell in cells:
        cell_source = Source(page, number, cell.row, cell.column)
        if (cell.role == "column_header" and cell.text.strip() and
                any(cell.column <= column < cell.column + cell.column_span for column in numeric_columns)):
            remarks.append(Remark("Сверьте распознанный заголовок столбца с оригиналом.", cell.text, cell_source))
        if cell.role == "data" and not re.fullmatch(NUMBER, cell.text.strip()):
            remarks.append(Remark("OCR не прочитал числовую ячейку целиком.", cell.text, cell_source))
        if cell.role == "row_header" and cell.text.endswith("|"):
            remarks.append(Remark("В названии строки OCR мог добавить лишний символ.", cell.text, cell_source))

    return replace(run, grid=grid, evidence=evidence + cell_evidence, remarks=tuple(remarks), context_evidence_ids=context_ids)
