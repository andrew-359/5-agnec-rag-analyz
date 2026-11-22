"""Propose conditions for a selected table from saved text fragments."""

from dataclasses import asdict
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory

from .article_context import load_article_fragments
from .core.domain import TableTarget
from .qwen_condition_comparison import baseline_for, compare
from .qwen_ollama import DEFAULT_MODEL, OllamaReply, RAW_RESPONSE, ollama_chat, ollama_settings
from .qwen_prompts import EVIDENCE_POLICY, prompt_provenance
from .run_output import write_json


OPTIONS = {"num_ctx": 8192, "num_predict": 1536, "temperature": 0, "seed": 42}
PROMPT_REVISION = "qwen-text-conditions-v1"
TASK = """Find conditions for the selected table: the generator model and the retrieval top-k.
Exclude evaluator thresholds, k in metrics, candidate pools, example counts and repetitions.
Keep the experiment scope and the authors' quantifiers. Do not turn 'most' into a list of methods.
Quote verbatim, including the original line breaks.
If a name and its definition occur in different fragments, cite both.
Return only JSON: {"proposals":[{"field":"generator_model or retrieval_top_k","value":"model name or a positive JSON integer",
"scope":"scope supported by the sources","citations":[{"fragment_id":"source ID","quote":"verbatim substring"}]}]}.
Return {"proposals":[]} if no such conditions are supported."""


def read_proposals(payload, fragments):
    if not isinstance(payload, dict) or payload.get("done") is not True or payload.get("done_reason") != "stop":
        raise ValueError("Qwen: Ollama не завершил ответ.")
    try:
        proposals = json.loads(payload["message"]["content"])["proposals"]
        # Preserve rejected proposals as valid UTF-8 JSON in the attempt.
        json.dumps(proposals, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (KeyError, TypeError, ValueError, RecursionError) as error:
        raise ValueError("Qwen: не удалось разобрать JSON предложений.") from error
    if not isinstance(proposals, list) or len(proposals) > 40:
        raise ValueError("Qwen: ожидался список предложений длиной до 40.")
    return [check_proposal(item, fragments) for item in proposals]


def check_proposal(item, fragments):
    record = {"proposal": item, "applicability": "open"}
    if not isinstance(item, dict):
        return {**record, "status": "invalid_proposal"}
    field, value, scope = (item.get(key) for key in ("field", "value", "scope"))
    record = {"field": field, "value": value, "scope": scope, "applicability": "open"}
    if field not in ("generator_model", "retrieval_top_k"):
        return {**record, "status": "unsupported_field"}
    if field == "generator_model" and not nonempty_text(value):
        return {**record, "status": "invalid_value"}
    if field == "retrieval_top_k" and (type(value) is not int or value < 1):
        return {**record, "status": "invalid_value"}
    if not nonempty_text(scope):
        return {**record, "status": "invalid_scope"}
    citations = item.get("citations")
    if not isinstance(citations, list) or not 1 <= len(citations) <= 10:
        return {**record, "status": "invalid_citation"}
    checked = [check_citation(citation, fragments) for citation in citations]
    status = "unreviewed" if all(cite["status"] == "verified" for cite in checked) else "invalid_citation"
    return {**record, "citations": checked, "status": status}


def nonempty_text(value):
    return isinstance(value, str) and bool(value.strip())


def check_citation(item, fragments):
    if not isinstance(item, dict):
        return {"citation": item, "status": "invalid_citation"}
    ident, quote = item.get("fragment_id"), item.get("quote")
    record = {"fragment_id": ident, "quote": quote}
    fragment = fragments.get(ident) if isinstance(ident, str) else None
    if fragment is None:
        return {**record, "status": "unknown_fragment"}
    source = {"id": fragment.id, "page": fragment.page, "box": fragment.box, "precision": fragment.precision}
    status = "verified" if nonempty_text(quote) and quote in fragment.text else "quote_missing"
    return {**record, "status": status, "source": source}


def load_control(path, digest, target, fragments):
    try:
        data = json.loads(path.read_bytes())
        if (data["schema_version"], data["pdf_sha256"], data["page"], data["table"]) != (
            1, digest, target.page, target.number,
        ):
            raise ValueError("Контроль должен относиться к выбранному адресу и версии PDF.")
        conditions = data["conditions"]
        if not isinstance(conditions, list) or not conditions:
            raise ValueError("Контроль должен содержать проверяемые условия.")
        for item in conditions:
            expected = {"field": item["field"], "value": item["value"], "scope": item["scope"],
                        "citations": item["required_citations"]}
            if check_proposal(expected, fragments)["status"] != "unreviewed":
                raise ValueError("Условие контроля должно иметь дословные основания в выбранных фрагментах.")
    except (KeyError, TypeError) as error:
        raise ValueError("Некорректный артефакт контроля.") from error
    return conditions


def propose(args):
    if args.out.exists():
        raise ValueError(f"Каталог результата уже существует: {args.out}")
    try:
        document = load_article_fragments(args.article_text)
    except (KeyError, TypeError) as error:
        raise ValueError("Некорректный артефакт общего текста.") from error
    selected_ids = {args.caption, *args.fragment}
    fragments = {fragment.id: fragment for fragment in document.fragments if fragment.id in selected_ids}
    missing = selected_ids - fragments.keys()
    if missing:
        raise ValueError("Фрагменты не найдены в сохранённом тексте: " + ", ".join(sorted(missing)))
    if fragments[args.caption].page != args.page:
        raise ValueError("Подпись должна находиться на странице выбранной таблицы.")
    target = {"page": args.page, "table": args.table, "caption_id": args.caption}
    source_data = {"target": target, "fragments": [
        {"id": fragment.id, "page": fragment.page, "text": fragment.text} for fragment in fragments.values()
    ]}
    prompt = EVIDENCE_POLICY + "\n\n" + TASK + "\n\n" + json.dumps(source_data, ensure_ascii=False)
    if len(prompt.encode("utf-8")) > 24_000:
        raise ValueError("Сократите подборку фрагментов: инструкция превышает 24000 байт.")
    model = os.getenv("RAG_QWEN_TEXT_MODEL", DEFAULT_MODEL).strip()
    table_target = TableTarget(args.page, args.table)
    baseline = baseline_for(document, tuple(fragments.values()), table_target, args.grid_input)
    controls = (
        load_control(args.control, document.sha256, table_target, fragments)
        if args.control is not None else []
    )
    messages = [{"role": "user", "content": prompt}]
    attempt = {"requested_model": model, "options": OPTIONS, "prompt": prompt_provenance(PROMPT_REVISION, messages)}
    request = {
        "model": model, "messages": messages,
        "stream": False, "format": "json", "options": OPTIONS, "keep_alive": 0, "think": False,
    }
    proposals = []
    try:
        endpoint, timeout = ollama_settings()
        if not model:
            raise ValueError("Qwen: задайте модель Ollama в RAG_QWEN_TEXT_MODEL.")
    except ValueError as error:
        reply = OllamaReply("unavailable", str(error))
    else:
        attempt.update(port=endpoint.port or (443 if endpoint.scheme == "https" else 80), timeout_seconds=timeout)
        reply = ollama_chat(request, endpoint, timeout, attempt)
    status, reason = reply.status, reply.reason
    if status == "succeeded":
        try:
            proposals = read_proposals(reply.payload, fragments)
        except ValueError as error:
            status, reason = "failed", str(error)
        else:
            if not proposals:
                status = "no_proposals"
            elif not any(item["status"] == "unreviewed" for item in proposals):
                status = "rejected_proposals"
    artifact = {
        "schema_version": 1, "strategy": "qwen-text", "status": status,
        "input": {
            "article_text": str(args.article_text.resolve()), "pdf_sha256": document.sha256,
            "document": document.document, "target": target,
            "fragments": [asdict(fragment) for fragment in fragments.values()],
        },
        "model_attempt": {
            **attempt, "status": status,
            **{key: request[key] for key in ("stream", "format", "keep_alive", "think")},
        },
        "proposals": proposals,
        "baseline": baseline,
        "comparison": compare(baseline, proposals, controls, args.control),
    }
    if reason is not None:
        artifact["reason"] = reason
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".rag-conditions-", dir=args.out.parent) as temporary:
        directory = Path(temporary)
        if reply.raw is not None:
            artifact["model_attempt"]["raw_response"] = RAW_RESPONSE
            with (directory / RAW_RESPONSE).open("xb") as private:
                os.fchmod(private.fileno(), 0o600)
                private.write(reply.raw)
        write_json(directory / "attempt.json", artifact)
        directory.rename(args.out)
