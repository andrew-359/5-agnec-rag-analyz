"""Store the reusable part of a successful Docling page conversion."""

import hashlib
import json
import os
from contextlib import ExitStack
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import NamedTuple

from docling.datamodel.base_models import ConversionStatus, ErrorItem
from docling_core.types.doc import DoclingDocument
from pydantic_core import to_jsonable_python


class CachedDoclingPage(NamedTuple):
    document: DoclingDocument
    status: ConversionStatus = ConversionStatus.SUCCESS
    errors: tuple[ErrorItem, ...] = ()


def cache_file(directory: Path, pdf: Path, page: int, versions: dict, options: dict) -> Path:
    with pdf.open("rb") as stream:
        pdf_sha256 = hashlib.file_digest(stream, "sha256").hexdigest()
    identity = {
        "format": 1, "reader": "docling-tableformer", "versions": versions,
        "pdf_sha256": pdf_sha256, "page": page, "pipeline": options,
    }
    # The JSON-mode dump turns option sets into lists in process hash order.
    def encode_option(value):
        if isinstance(value, (set, frozenset)):
            return sorted(value, key=str)
        return to_jsonable_python(value)

    key = hashlib.sha256(json.dumps(identity, sort_keys=True, default=encode_option).encode()).hexdigest()
    return directory / f"{key}.json"


def read_page(path: Path, page: int) -> CachedDoclingPage | None:
    try:
        entry = json.loads(path.read_bytes())
        payload = entry["document"]
        if entry["checksum"] != _checksum(path, payload):
            return None
        document = DoclingDocument.model_validate_json(payload)
        return CachedDoclingPage(document) if page in document.pages else None
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def write_page(path: Path, document: DoclingDocument) -> None:
    payload = document.model_dump_json(by_alias=True)
    with ExitStack() as cleanup:
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            cleanup.callback(temporary.unlink, missing_ok=True)
            json.dump({"checksum": _checksum(path, payload), "document": payload}, stream)
        temporary.replace(path)


def lock_page(path: Path, resources: ExitStack) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        # Keep the lock file in place so concurrent workers always lock the same inode.
        from fcntl import LOCK_EX, flock

        lock = resources.enter_context(path.with_suffix(".lock").open("ab"))
        flock(lock.fileno(), LOCK_EX)


def _checksum(path: Path, payload: str) -> str:
    return hashlib.sha256((path.stem + payload).encode()).hexdigest()
