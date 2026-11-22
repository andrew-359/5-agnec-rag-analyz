"""Read addressable text fragments from digital PDFs."""

from ..strategies.article_text_readers import read_article_fragments
from ..strategies.text_pdfplumber import ArticleFragments, load_article_fragments

__all__ = ["ArticleFragments", "load_article_fragments", "read_article_fragments"]
