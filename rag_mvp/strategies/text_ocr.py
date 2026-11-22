"""Fill pages without native text using positioned Docling OCR blocks."""

from collections.abc import Iterable
from dataclasses import replace
from importlib.metadata import version
from pathlib import Path

from ..core.domain import Evidence
from .text_pdfplumber import ArticleFragments, read_article_fragments as read_native_fragments


def read_article_fragments(path: Path, pages: Iterable[int] | None = None) -> ArticleFragments:
    native = read_native_fragments(path, pages)
    populated = {fragment.page for fragment in native.fragments}
    missing = tuple(page for page in native.pages if page not in populated)

    if not missing:
        return native

    recovered, engine_version = read_ocr_pages(path, native.sha256, missing)
    fragments = sorted((*native.fragments, *recovered), key=lambda fragment: fragment.page)

    return replace(native, fragments=tuple(fragments), engine_version=f"{native.engine_version}; {engine_version}")


def read_ocr_pages(path: Path, digest: str, pages: tuple[int, ...]) -> tuple[list[Evidence], str]:
    from docling.datamodel.base_models import ConversionStatus
    from docling.datamodel.pipeline_options import OcrMode, RapidOcrOptions
    from docling_core.types.doc import DocItemLabel, TextItem

    from ..ocr_discovery import ocr_backend
    from .parser import converter_for, docling_options, normalized_box

    labels = {
        DocItemLabel.TITLE, DocItemLabel.SECTION_HEADER, DocItemLabel.TEXT,
        DocItemLabel.PARAGRAPH, DocItemLabel.LIST_ITEM, DocItemLabel.CAPTION,
    }
    backend = ocr_backend()
    converter = converter_for(docling_options(
        do_ocr=True,
        do_table_structure=False,
        ocr_options=RapidOcrOptions(backend=backend, lang=["en"], mode=OcrMode.FULL_PAGE, scale=2),
    ))
    fragments = []

    for page in pages:
        result = converter.convert(path, page_range=(page, page), raises_on_error=False)
        if result.status != ConversionStatus.SUCCESS:
            raise ValueError(f"OCR страницы {page} не завершён: {result.status.value}.")

        document = result.document
        if page not in document.pages:
            raise ValueError(f"OCR не вернул страницу {page}.")

        for index, (item, _) in enumerate(document.iterate_items(page_no=page), 1):
            if not isinstance(item, TextItem) or item.label not in labels or len(item.prov) != 1:
                continue
            location = item.prov[0]
            text = item.text.strip()
            if location.page_no != page or not text:
                continue

            box = normalized_box(location.bbox, document.pages[page].size)
            fragments.append(Evidence(
                id=f"{digest[:12]}-p{page}-text-{index}",
                page=page,
                text=text,
                box=box,
                precision="text_fragment" if box is not None else "page",
            ))

    engine = f"Docling {version('docling')} / RapidOCR {version('rapidocr')} ({backend})"
    return fragments, engine
