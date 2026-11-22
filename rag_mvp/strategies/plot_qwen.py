"""Read one cropped PDF figure through an optional loopback Ollama server."""

from decimal import Decimal, InvalidOperation
import json
import os
import re

from ..plot_image import render_chart_crop
from ..qwen_ollama import DEFAULT_MODEL, RAW_RESPONSE, ollama_chat, ollama_settings
from ..qwen_prompts import EVIDENCE_POLICY, prompt_provenance


STRATEGY = "qwen-vision"
MODEL_OPTIONS = {"num_ctx": 4096, "num_predict": 1024, "temperature": 0, "seed": 42}
NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d+)?|\.\d+)(?:[eE][+-]?\d+)?")
TEXT_FIELDS = ("series", "category", "metric", "value")
TEXT_LIMIT = 120
OBSERVATION_LIMIT = 40
CHART_RESPONSE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["observations"],
    "properties": {
        "observations": {
            "type": "array",
            "maxItems": OBSERVATION_LIMIT,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [*TEXT_FIELDS, "approximate"],
                "properties": {
                    **{field: {"type": "string", "minLength": 1, "maxLength": TEXT_LIMIT} for field in TEXT_FIELDS},
                    "approximate": {"type": "boolean"},
                },
            },
        },
    },
}
PROMPT_REVISION = "qwen-vision-v3"
CHART_INSTRUCTION = """Read the visible numeric values and their series and groups from this chart crop. Return JSON as
{"observations":[{"series":"legend label","category":"category or exact x value",
"metric":"metric or y-axis label","value":"numeric value","approximate":true}]}.
The value must be a quoted string, preserving printed trailing zeros.
Keep category and series distinct. Use printed labels verbatim. Omit observations
whose value, series, category or metric is unclear. Set approximate=false only for
an explicitly printed number, and keep its printed digits. For visual estimates
from a scale, set approximate=true and round to what can be read on the scale.
Do not imply more precision than the source supports. If none are readable, return
an empty observations list. Do not include coordinates, commentary or markdown."""


def _observation(item, source):
    if not isinstance(item, dict):
        raise ValueError("Qwen: наблюдение должно быть объектом.")
    parsed = {key: item.get(key) for key in TEXT_FIELDS}
    if any(not isinstance(value, str) or not value.strip() or len(value) > TEXT_LIMIT for value in parsed.values()):
        raise ValueError("Qwen: неполное или слишком длинное наблюдение.")
    value = parsed["value"]
    try:
        if not NUMBER.fullmatch(value) or not Decimal(value).is_finite():
            raise ValueError("Qwen: значение наблюдения должно быть конечным числом.")
    except InvalidOperation as error:
        raise ValueError("Qwen: некорректное число.") from error
    if type(item.get("approximate")) is not bool:
        raise ValueError("Qwen: не указана приблизительность наблюдения.")
    return {**parsed, "approximate": item["approximate"], "status": "unreviewed", "source": source}


def _read_reply(payload, source):
    if not isinstance(payload, dict) or payload.get("done") is not True or payload.get("done_reason") != "stop":
        raise ValueError("Qwen: Ollama не завершил ответ.")
    try:
        observations = json.loads(payload["message"]["content"])["observations"]
    except (KeyError, TypeError, ValueError, RecursionError) as error:
        raise ValueError("Qwen: не удалось разобрать JSON наблюдений.") from error
    if not isinstance(observations, list) or len(observations) > OBSERVATION_LIMIT:
        raise ValueError(f"Qwen: ожидался список наблюдений длиной до {OBSERVATION_LIMIT}.")
    return [_observation(item, source) for item in observations]


def read_qwen(page, args, output, evidence):
    box = args.panel_box
    if box is None or not (page.bbox[0] <= box[0] < box[2] <= page.bbox[2]
                           and page.bbox[1] <= box[1] < box[3] <= page.bbox[3]):
        raise ValueError("Qwen: требуется рамка графика в пределах PDF-страницы.")
    output["source"] = {**evidence(box), "precision": "figure"}
    output["region"] = {"selection": "crop"}
    output["proposals"] = []
    model = os.getenv("RAG_QWEN_MODEL", DEFAULT_MODEL).strip()
    messages = [
        {"role": "system", "content": EVIDENCE_POLICY},
        {"role": "user", "content": CHART_INSTRUCTION},
    ]
    attempt = output["model_attempt"] = {
        "requested_model": model,
        "options": MODEL_OPTIONS,
        "prompt": prompt_provenance(PROMPT_REVISION, messages),
    }

    def finish(status, reason=None):
        attempt["status"] = output["status"] = status
        if reason is not None:
            output["reason"] = reason
            output["issues"].append({"message": reason, "source": output["source"]})

    try:
        endpoint, timeout = ollama_settings()
    except ValueError as error:
        finish("unavailable", str(error))
        return
    if not model:
        finish("unavailable", "Qwen: задайте модель Ollama на воркере.")
        return

    attempt["timeout_seconds"] = timeout
    image, attempt["image_size_px"] = render_chart_crop(page, box)
    messages[-1]["images"] = [image]
    reply = ollama_chat({
        "model": model, "messages": messages,
        "stream": False, "format": CHART_RESPONSE_SCHEMA, "options": MODEL_OPTIONS, "keep_alive": 0,
    }, endpoint, timeout, attempt)
    if reply.raw is not None:
        output["private_artifacts"] = {RAW_RESPONSE: reply.raw}
    if reply.status != "succeeded":
        finish(reply.status, reply.reason)
        return
    try:
        output["proposals"] = _read_reply(reply.payload, output["source"])
    except ValueError as error:
        finish("failed", str(error))
        return
    if output["proposals"]:
        finish("succeeded")
    else:
        finish("no_observations", "Qwen: Ollama не вернул читаемых наблюдений.")
