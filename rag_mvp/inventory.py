"""Алгоритмический обзор PDF с сохранением текста и адресов кандидатов."""

import hashlib
import json
import re
import shutil
from collections import Counter
from importlib.metadata import version
from pathlib import Path
from statistics import median
from tempfile import TemporaryDirectory

import pdfplumber

from .article_overview import ArticleOverview, refine_overview
from .article_context.captions import REFERENCE_AFTER_PERIOD, table_caption_lines
from .strategies.text_pdfplumber import Line, page_text_blocks, source_box
from .table_read_features import read_features
from .table_region import area, overlap


FIGURE_CAPTION = re.compile(
    r"^(Figure|Fig\.|Рисунок|Рис\.)\s*"
    r"([A-Z]?[.\-]?\d+(?:[.\-]\d+)*[a-z]?|[IVXLC]+)\s*"
    r"([:.—–-])\s*(.*)", re.IGNORECASE,
)
SECTION = re.compile(r"^(?:\d{1,2}(?:\.\d+)*|[A-Z](?:\.\d+)*)\.?\s+[^\W\d_]", re.UNICODE)
SPECIAL = re.compile(
    r"^(?:abstract|аннотация|references|bibliography|appendix|appendices|supplementary material|"
    r"supplemental material|список литературы|приложени[ея])(?:\b|$)", re.IGNORECASE,
)


def normalized_box(item: dict, width: float, height: float) -> list[float] | None:
    """Вернуть область в долях страницы от верхнего левого угла."""
    left, top = max(0, item["x0"]), max(0, item["top"])
    right, bottom = min(width, item["x1"]), min(height, item["bottom"])
    if left >= right or top >= bottom:
        return None
    return [round(left / width, 6), round(top / height, 6),
            round(right / width, 6), round(bottom / height, 6)]


def is_heading(line: Line, page, body_size: float) -> bool:
    text = line.text
    if len(text) > 150 or len(text.split()) > 20:
        return False
    special = SPECIAL.match(text)
    if not special and not SECTION.match(text):
        return False
    chars = [char for char in page.chars if
             line.left <= char["x0"] < line.right
             and abs(char["top"] - line.top) < 5]
    styled = bool(chars) and (
        median(char["size"] for char in chars) >= body_size * 1.08
        or sum(any(marker in char.get("fontname", "").lower() for marker in ("bold", "-medi"))
               for char in chars) >= len(chars) / 2
    )
    # Обычная фраза «appendix to support…» не становится заголовком.
    return styled or bool(special and not text[special.end():].strip())


def image_near_caption(image, caption) -> bool:
    left, top, right, bottom = caption
    x0, y0, x1, y1 = image
    return (min(right, x1) - max(left, x0) >= (right - left) / 2
            and y1 - y0 >= .05 and top - y1 <= .04 and y0 - bottom <= .04)


def substantial_raster(image) -> bool:
    left, top, right, bottom = image
    # Schedule region detection for a table-sized raster even without a caption.
    # The layout detector still has to find rows and columns inside the page.
    return right - left >= .20 and bottom - top >= .05 and area(image) >= .02


def _has_native_table_text(page, box) -> bool:
    from .table_region_discovery import RegionToken, has_rows_and_columns

    width, height = page.width, page.height
    left, top, right, bottom = box
    crop = page.crop((left * width, top * height, right * width, bottom * height), strict=False)
    tokens = []
    for word in crop.extract_words():
        bounds = normalized_box(word, width, height)
        if bounds:
            tokens.append(RegionToken(word["text"], bounds))
    return has_rows_and_columns(tokens)


def _merge_regions(pdf, candidates, found, mixed_pages) -> None:
    with pdfplumber.open(pdf) as document:
        for region in found:
            if region["page"] in mixed_pages and _has_native_table_text(document.pages[region["page"] - 1], region["box"]):
                region.pop("discovery", None)
                region["table_read_features"] = read_features(region["box"], [], ocr=False)
            caption = region["region"]["caption_box"]
            matches = [item for item in candidates if item["kind"] == "table" and "region" not in item
                       and item["page"] == region["page"] and item["label"] == region["label"]
                       and item["box"] and caption and overlap(item["box"], caption) >= area(item["box"]) / 2]
            if len(matches) == 1:
                item = matches[0]
                item.update({key: region[key] for key in ("box", "region", "text", "table_read_features")})
                if "discovery" in region:
                    item["discovery"] = region["discovery"]
            else:
                candidates.append(region)


def inspect_pdf(pdf: Path, output: Path, *, discover_scans: bool = True) -> dict:
    """Сохранить обзор атомарно в новый каталог, сохранив оригинал PDF."""
    if not pdf.is_file():
        raise ValueError(f"PDF не найден: {pdf}")
    if output.exists():
        raise ValueError(f"Каталог обзора уже существует: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".rag-inventory-", dir=output.parent) as temporary:
        folder = Path(temporary)
        snapshot = folder / "source.pdf"
        shutil.copyfile(pdf, snapshot)
        with snapshot.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        pages, candidates, warnings = [], [], []
        overview = ArticleOverview()
        try:
            with pdfplumber.open(snapshot) as document:
                for page in document.pages:
                    width, height = page.width, page.height
                    sizes = Counter(round(char["size"], 1) for char in page.chars if char["text"].strip())
                    body_size = sizes.most_common(1)[0][0] if sizes else 10
                    blocks = page_text_blocks(page)
                    overview.read_page(page, blocks, body_size)
                    text = "\n\n".join("\n".join(line.text for line in block) for block in blocks)
                    text_chars = sum(not char.isspace() for char in text)
                    state = "text_available" if text_chars >= 80 else "needs_review"
                    images = [normalized_box(image, width, height) for image in page.images]
                    images = [box for box in images if box]
                    page_candidates = []
                    for block in blocks:
                        table_captions = dict(table_caption_lines(line.text for line in block))
                        for index, line in enumerate(block):
                            table = table_captions.get(index)
                            figure = FIGURE_CAPTION.match(line.text)
                            if figure and figure[3] == "." and REFERENCE_AFTER_PERIOD.match(figure[4]):
                                figure = None
                            label = None
                            if table:
                                kind, label = "table", table["label"]
                            elif figure:
                                kind, label = "figure", figure[2]
                            elif is_heading(line, page, body_size):
                                kind = "section"
                            else:
                                continue
                            item = {
                                "id": f"{digest[:12]}-p{page.page_number}-{len(page_candidates) + 1}",
                                "kind": kind,
                                "page": page.page_number,
                                "label": label,
                                "text": line.text,
                                "box": source_box([line], page.bbox),
                                "anchor": "heading" if kind == "section" else "caption",
                                "status": "candidate",
                            }
                            if kind == "section":
                                lowered = line.text.lower()
                                if lowered.startswith(("references", "bibliography", "список литературы")):
                                    item["role"] = "references"
                                elif lowered.startswith(("abstract", "аннотация")):
                                    item["role"] = "abstract"
                                elif SPECIAL.match(line.text):
                                    item["role"] = "appendix"
                                elif re.match(r"^[A-Z](?:\.\d+)*\.?\s", line.text):
                                    item["role"] = "possible_appendix"
                                else:
                                    item["role"] = "body"
                            if table and table["word"].lower() == "table" and label.isdecimal():
                                item["extract_arguments"] = ["--page", str(page.page_number), "--table", label]
                            page_candidates.append(item)
                    candidates.extend(page_candidates)
                    if state == "needs_review":
                        warnings.append({"page": page.page_number, "reason": "Мало извлекаемого текста. Возможны скан, рисунок или пустая страница."})
                    pages.append({
                        "page": page.page_number, "width": width, "height": height,
                        "rotation": page.rotation, "status": state, "text": text, "text_chars": text_chars,
                        "image_boxes": images, "vector_objects": len(page.lines) + len(page.rects) + len(page.curves),
                        "candidate_ids": [item["id"] for item in page_candidates],
                    })
                    page.close()
        except Exception as error:
            raise ValueError(f"Не удалось прочитать PDF: {error}") from error
        mixed_pages = {item['page'] for item in candidates if item['kind'] == 'table' and item['box']
                       and pages[item['page'] - 1]['text_chars'] >= 80
                       and any(image_near_caption(image, item['box']) for image in pages[item['page'] - 1]['image_boxes'])}
        mixed_pages.update(item['page'] for item in pages if item['text_chars'] >= 80
                           and any(substantial_raster(image) for image in item['image_boxes']))
        image_pages = sorted(mixed_pages | {item['page'] for item in pages if item['text_chars'] < 80 and item['image_boxes']})
        digital_pages = sorted({item['page'] for item in candidates if item['kind'] == 'table'
                                and item['label'] and re.fullmatch(r'[IVXLCDM]+', item['label'], re.I)} - set(image_pages))
        if discover_scans:
            for detection_pages, ocr in ((image_pages, True), (digital_pages, False)):
                if not detection_pages:
                    continue
                try:
                    from .ocr_discovery import discover_tables
                    found, failures = discover_tables(snapshot, detection_pages, digest, folder, ocr=ocr)
                    _merge_regions(snapshot, candidates, found, mixed_pages)
                    warnings.extend(failures)
                except Exception as error:
                    warnings.extend({'page': page, 'reason': f'Поиск областей таблиц недоступен: {type(error).__name__}: {str(error)[:300]}'}
                                    for page in detection_pages)
            for item in pages:
                item['candidate_ids'] = [candidate['id'] for candidate in candidates if candidate['page'] == item['page']]
                if item['page'] in image_pages:
                    item['ocr_discovery'] = 'attempted'
        counts = Counter(item["kind"] for item in candidates)
        summary = {kind: counts[kind] for kind in ("section", "table", "figure")}
        result = {
            "schema_version": 1, "document": pdf.name, "sha256": digest,
            "engine": {"name": "pdfplumber", "version": version("pdfplumber")},
            "settings": {"word_x_tolerance": 1, "layout": "column_blocks", "minimum_text_chars": 80,
                         "ocr_discovery": {"pages": image_pages, "artifacts": sorted(item.name for item in folder.glob("discovery-ocr-*.json"))},
                         "digital_discovery": {"pages": digital_pages, "artifacts": sorted(item.name for item in folder.glob("discovery-layout-*.json"))}},
            "page_count": len(pages), "summary": summary,
            "limitations": [
                "Карта показывает найденные области, подписи и заголовки. Полнота требует сверки с PDF.",
                "Поле region задаёт область таблицы. У остальных кандидатов box указывает подпись или заголовок.",
                ("OCR использован для поиска областей и подписей таблиц на страницах с растровым содержимым. Значения и условия здесь не извлекались."
                 if discover_scans and image_pages else "OCR, чтение значений и интерпретация научного смысла не выполнялись."),
            ],
            "overview": overview.result(),
            "pages": pages, "candidates": candidates, "warnings": warnings,
        }
        result["overview"] = refine_overview(snapshot, result, result["overview"])
        (folder / "inventory.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        report = [f"# Обзор {pdf.name}", "", f"Страниц — {len(pages)}. Кандидаты — {summary}.", "",
                  "Карта показывает области таблиц, подписи и заголовки. Полнота обнаружения требует сверки с PDF.", "",
                  "| Вид | Страница | Метка | Исходный фрагмент |", "| --- | --- | --- | --- |"]
        for item in candidates:
            safe = item["text"].replace("|", "\\|")
            report.append(f"| {item['kind']} | {item['page']} | {item['label'] or ''} | {safe} |")
        report.extend(["", "## Места для проверки", ""])
        report.extend(f"- Страница {item['page']} — {item['reason']}" for item in warnings)
        if not warnings:
            report.append("Страниц с малым количеством текста не обнаружено. Полнота карты этим не подтверждается.")
        (folder / "inventory.md").write_text("\n".join(report) + "\n", encoding="utf-8")
        folder.rename(output)
    return result
