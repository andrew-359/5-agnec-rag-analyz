"""Available readers for text fragments in the original PDF."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .text_pdfplumber import ArticleFragments


@dataclass(frozen=True)
class TextReader:
    module: str
    description: str


DEFAULT_ARTICLE_TEXT_READER = "pdfplumber"
ARTICLE_TEXT_READERS = {
    DEFAULT_ARTICLE_TEXT_READER: TextReader(".text_pdfplumber", "Текстовый слой PDF."),
    "pdfplumber-ocr": TextReader(".text_ocr", "Текстовый слой PDF с OCR для страниц без текста."),
}


def read_article_fragments(
    path: Path,
    pages: Iterable[int] | None = None,
    *,
    strategy: str = DEFAULT_ARTICLE_TEXT_READER,
) -> ArticleFragments:
    reader = ARTICLE_TEXT_READERS.get(strategy)
    if reader is None:
        raise ValueError(f"Неизвестная стратегия чтения общего текста: {strategy}.")
    return import_module(reader.module, __package__).read_article_fragments(path, pages)
