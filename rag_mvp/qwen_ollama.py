"""Shared Ollama transport and local GPU slot for model readers."""

from contextlib import contextmanager
from dataclasses import dataclass
from http.client import HTTPConnection, HTTPException, HTTPSConnection
import json
import os
import re
from time import monotonic
from urllib.parse import urlsplit


DEFAULT_MODEL = "qwen3-vl:4b-instruct-q4_K_M"
RESPONSE_LIMIT = 64 * 1024
GPU_LOCK_PATH = "/tmp/rag-qwen-ollama-gpu.lock"
RAW_RESPONSE = "ollama-response.txt"


class GpuSlotUnavailable(Exception):
    pass


@contextmanager
def _gpu_slot():
    # One inode for all local workers, independent of the run, queue or Ollama port.
    # Keep the file in place so waiting and newly starting processes share that inode.
    try:
        from fcntl import LOCK_EX, flock

        fd = os.open(GPU_LOCK_PATH, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    except (ImportError, OSError) as error:
        raise GpuSlotUnavailable from error
    try:
        try:
            flock(fd, LOCK_EX)
        except OSError as error:
            raise GpuSlotUnavailable from error
        yield
    finally:
        os.close(fd)


def _chat(request, endpoint, timeout):
    # Send source material only to the configured endpoint, without redirects or proxy settings.
    connect = HTTPSConnection if endpoint.scheme == "https" else HTTPConnection
    connection = connect(endpoint.hostname, endpoint.port, timeout=timeout)
    deadline = monotonic() + timeout
    try:
        connection.request("POST", "/api/chat", json.dumps(request), {"Content-Type": "application/json"})
        response = connection.getresponse()
        chunks = bytearray()
        while len(chunks) <= RESPONSE_LIMIT:
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise TimeoutError
            if connection.sock is not None:
                connection.sock.settimeout(remaining)
            chunk = response.read1(min(8192, RESPONSE_LIMIT + 1 - len(chunks)))
            if not chunk:
                break
            chunks.extend(chunk)
        return response.status, bytes(chunks)
    finally:
        connection.close()


def _ollama_endpoint(url, reader_name):
    try:
        endpoint = urlsplit(url)
        port = endpoint.port
        if (endpoint.scheme in {"http", "https"} and endpoint.hostname and port != 0
                and endpoint.username is None and endpoint.path in {"", "/"}
                and not any(ch.isspace() or ch in "?#" for ch in url)):
            return endpoint
    except ValueError:
        pass
    raise ValueError(f"{reader_name}: RAG_OLLAMA_URL должен задавать HTTP(S)-адрес Ollama без пути, параметров и логина.")


def ollama_settings(*, reader_name="Qwen"):
    url = os.getenv("RAG_OLLAMA_URL", "").strip()
    try:
        timeout = float(os.getenv("RAG_QWEN_TIMEOUT_SECONDS", "60"))
        port = 11434 if url else int(os.getenv("RAG_QWEN_PORT", "11434"))
    except ValueError:
        timeout, port = 0, 0
    if not (0 < timeout <= 90 and 0 < port <= 65535):
        raise ValueError(f"{reader_name}: недопустимый порт или таймаут Ollama (не более 90 с).")
    return _ollama_endpoint(url or f"http://127.0.0.1:{port}", reader_name), timeout


@dataclass(frozen=True)
class OllamaReply:
    status: str
    reason: str | None = None
    raw: bytes | None = None
    payload: object = None


def ollama_chat(request, endpoint, timeout, attempt, *, reader_name="Qwen"):
    try:
        with _gpu_slot():
            started = monotonic()
            try:
                status, response = _chat(request, endpoint, timeout)
            finally:
                attempt["elapsed_seconds"] = round(monotonic() - started, 3)
    except TimeoutError:
        return OllamaReply("timed_out", f"{reader_name}: истёк таймаут запроса к Ollama.")
    except GpuSlotUnavailable:
        return OllamaReply("unavailable", f"{reader_name}: не удалось получить блокировку общего GPU.")
    except (OSError, HTTPException):
        return OllamaReply("unavailable", f"{reader_name}: Ollama недоступен по настроенному адресу.")

    attempt["http_status"] = status
    raw = response[:RESPONSE_LIMIT]
    if len(response) > RESPONSE_LIMIT:
        return OllamaReply("failed", f"{reader_name}: превышен лимит размера ответа Ollama.", raw)
    if status != 200:
        outcome = "unavailable" if status in (404, 429, 503) else "failed"
        return OllamaReply(outcome, f"{reader_name}: Ollama вернул HTTP {status}.", raw)
    try:
        payload = json.loads(response)
    except (ValueError, RecursionError):
        return OllamaReply("failed", f"{reader_name}: Ollama вернул некорректный JSON.", raw)
    if isinstance(payload, dict):
        reported_model = payload.get("model")
        if isinstance(reported_model, str) and re.fullmatch(r"[\w:./@-]{1,160}", reported_model):
            attempt["reported_model"] = reported_model
        attempt["usage"] = {key: payload[field] for key, field in
                            (("input_tokens", "prompt_eval_count"), ("output_tokens", "eval_count"))
                            if type(payload.get(field)) is int and payload[field] >= 0}
    return OllamaReply("succeeded", raw=raw, payload=payload)
