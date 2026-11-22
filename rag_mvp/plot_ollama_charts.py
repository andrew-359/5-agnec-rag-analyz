"""Read native chart tables from Ollama as unreviewed PDF observations."""

import csv
from io import StringIO
import re

from .plot_image import render_chart_crop
from .qwen_ollama import RAW_RESPONSE, ollama_chat, ollama_settings
from .qwen_prompts import prompt_provenance


GRANITE_MODEL = "hf.co/ibm-granite/granite-vision-4.1-4b-GGUF:Q4_K_M"
PADDLE_MODEL = "hf.co/PaddlePaddle/PaddleOCR-VL-1.6-GGUF:latest"
MODEL_OPTIONS = {"num_ctx": 4096, "num_predict": 2048, "temperature": 0, "seed": 42}
NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d+)?|\.\d+)(?:[eE][+-]?\d+)?")


def _granite_rows(content):
    fenced = re.fullmatch(r"```(?:csv)?[ \t]*\r?\n(.*?)\r?\n```", content, re.IGNORECASE | re.DOTALL)
    if fenced:
        content = fenced[1]
    return list(csv.reader(StringIO(content, newline=""), strict=True))


def _paddle_rows(content):
    return [line.split("|") for line in content.splitlines() if line.strip()]


def _observations(rows, metric, source):
    rows = [[cell.strip() for cell in row] for row in rows if row]
    if not rows:
        return []
    header, *data = rows
    column_labels = header[1:]
    if not column_labels or not all(column_labels) or len(set(column_labels)) != len(column_labels):
        raise ValueError("Ожидалась таблица с уникальными непустыми заголовками столбцов данных.")
    # Normally columns are series. An explicit series header puts series on rows.
    series_on_rows = header[0].casefold() in {"method", "model", "series"}
    observations = []
    for row_label, *values in data:
        if len(values) != len(column_labels):
            raise ValueError("Число ячеек в строке не совпадает с заголовком графика.")
        if not row_label:
            continue
        for column_label, value in zip(column_labels, values, strict=True):
            if NUMBER.fullmatch(value):
                series, category = (row_label, column_label) if series_on_rows else (column_label, row_label)
                observations.append({
                    "series": series, "category": category, "metric": metric, "value": value,
                    "approximate": True, "status": "unreviewed", "source": source,
                })
    return observations


def _reply_content(payload):
    if not isinstance(payload, dict) or payload.get("done") is not True or payload.get("done_reason") != "stop":
        raise ValueError("Ollama не завершил ответ.")
    message = payload.get("message")
    if not isinstance(message, dict) or not isinstance(message.get("content"), str):
        raise ValueError("Ollama не вернул текст таблицы.")
    return message["content"].strip()


def _read_chart(page, args, output, evidence, *, model, reader_name, revision, prompt, parse_rows):
    box = args.panel_box
    left, top, right, bottom = page.bbox
    if box is None or len(box) != 4 or not (left <= box[0] < box[2] <= right and top <= box[1] < box[3] <= bottom):
        raise ValueError(f"{reader_name}: требуется рамка графика в пределах PDF-страницы.")
    source = output["source"] = {**evidence(box), "precision": "figure"}
    output["region"] = {"selection": "crop"}
    output["proposals"] = []
    messages = [{"role": "user", "content": prompt}]
    attempt = output["model_attempt"] = {
        "requested_model": model,
        "options": MODEL_OPTIONS,
        "prompt": prompt_provenance(revision, messages),
    }

    def finish(status, reason=None):
        attempt["status"] = output["status"] = status
        if reason is not None:
            output["reason"] = reason
            output["issues"].append({"message": reason, "source": source})

    try:
        port, timeout = ollama_settings(reader_name=reader_name)
    except ValueError as error:
        finish("unavailable", str(error))
        return
    attempt["timeout_seconds"] = timeout
    image, attempt["image_size_px"] = render_chart_crop(page, box)
    messages[0]["images"] = [image]
    reply = ollama_chat({
        "model": model, "messages": messages, "options": MODEL_OPTIONS, "stream": False, "keep_alive": 0,
    }, port, timeout, attempt, reader_name=reader_name)
    if reply.raw is not None:
        output["private_artifacts"] = {RAW_RESPONSE: reply.raw}
    if reply.status != "succeeded":
        finish(reply.status, reply.reason)
        return

    try:
        content = _reply_content(reply.payload)
        # Native chart tables lack per-cell evidence for precision and can omit the metric.
        output["proposals"] = _observations(parse_rows(content), args.y_axis.strip() or None, source)
    except (ValueError, csv.Error) as error:
        finish("failed", f"{reader_name}: {error}")
        return
    if output["proposals"]:
        finish("succeeded")
    else:
        finish("no_observations", f"{reader_name}: в ответе нет чисел с подписью категории и серии.")


def read_granite_chart(page, args, output, evidence):
    """Convert one cropped panel using Granite's Chart2CSV task."""
    _read_chart(page, args, output, evidence, model=GRANITE_MODEL, reader_name="Granite Vision",
                revision="granite-chart2csv-v1", prompt="<chart2csv>", parse_rows=_granite_rows)


def read_paddle_chart(page, args, output, evidence):
    """Convert one cropped panel using PaddleOCR's chart recognition task."""
    _read_chart(page, args, output, evidence, model=PADDLE_MODEL, reader_name="PaddleOCR",
                revision="paddleocr-chart-v1", prompt="Chart Recognition:", parse_rows=_paddle_rows)
