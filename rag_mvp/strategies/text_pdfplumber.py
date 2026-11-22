import json
import re
from dataclasses import dataclass
from hashlib import file_digest
from importlib.metadata import version
from math import isfinite
from pathlib import Path
from statistics import median
from typing import Iterable

import pdfplumber
from pdfplumber.page import Page
from pdfplumber.utils import cluster_objects

from ..core.domain import Box
from ..run import Evidence


READER_ID = re.compile(r"(?P<digest>[0-9a-f]{12})-p(?P<page>\d+)-text-\d+")


def validate_sources(sha256: str, fragments: Iterable[Evidence]) -> None:
    seen: dict[str, Evidence] = {}
    for fragment in fragments:
        if fragment.id in seen and seen[fragment.id] != fragment:
            raise ValueError(f"Разные данные у фрагментов с ID {fragment.id}.")
        match = READER_ID.fullmatch(fragment.id)
        if match and (match["digest"] != sha256[:12] or int(match["page"]) != fragment.page):
            raise ValueError(f"Фрагмент {fragment.id} относится к другой версии PDF или странице.")
        seen[fragment.id] = fragment


@dataclass(frozen=True)
class ArticleFragments:
    document: str
    sha256: str
    pages: tuple[int, ...]
    fragments: tuple[Evidence, ...]
    engine_version: str
    schema_version: int = 1


def load_article_fragments(
    path: Path, expected_sha256: str | None = None, *, expected_run_id: str | None = None,
) -> ArticleFragments:
    """Read the saved v1 artifact and validate its pages and source locators."""
    return decode_article_fragments(path.read_bytes(), expected_sha256, expected_run_id=expected_run_id)


def decode_article_fragments(
    payload: bytes, expected_sha256: str | None = None, *, expected_run_id: str | None = None,
) -> ArticleFragments:
    data = json.loads(payload)
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ValueError("Ожидается версия 1 формата фрагментов статьи.")
    if expected_run_id is not None and "run_id" in data and data["run_id"] != expected_run_id:
        raise ValueError("Общий текст относится к другому прогону.")
    sha256 = data["sha256"]
    if not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise ValueError("У общего текста отсутствует корректный SHA-256 PDF.")
    if expected_sha256 is not None and sha256 != expected_sha256:
        raise ValueError("SHA-256 общего текста не совпадает с версией PDF прогона.")
    pages = tuple(data["pages"])
    if any(type(page) is not int or page < 1 for page in pages) or tuple(sorted(set(pages))) != pages:
        raise ValueError("Страницы общего текста должны идти в порядке PDF.")
    fragments = []
    seen = set()
    previous_page = 0
    for item in data["fragments"]:
        # Blank fragments still reserve their IDs and participate in page ordering.
        fragment = parse_text_fragment(item, allow_blank=True)
        if fragment.id in seen or fragment.page not in pages or fragment.page < previous_page:
            raise ValueError("Некорректный фрагмент общего текста.")
        fragments.append(fragment)
        seen.add(fragment.id)
        previous_page = fragment.page
    validate_sources(sha256, fragments)
    document, engine = data["document"], data["engine_version"]
    if not isinstance(document, str) or not document or not isinstance(engine, str) or not engine:
        raise ValueError("Общему тексту нужны имя документа и версия читателя.")
    return ArticleFragments(document, sha256, pages, tuple(f for f in fragments if f.text.strip()), engine)


def parse_text_fragment(item: dict, *, allow_blank: bool = False) -> Evidence:
    box = item["box"]
    if box is not None:
        if (not isinstance(box, list) or len(box) != 4
                or any(type(value) not in (int, float) or not isfinite(value) for value in box)
                or not (0 <= box[0] < box[2] <= 1 and 0 <= box[1] < box[3] <= 1)):
            raise ValueError("Некорректная область фрагмента общего текста.")
        box = tuple(box)
    fragment = Evidence(**{**item, "box": box})
    if (not isinstance(fragment.id, str) or not fragment.id
            or type(fragment.page) is not int or fragment.page < 1
            or not isinstance(fragment.text, str) or (not allow_blank and not fragment.text.strip())
            or fragment.precision != ("text_fragment" if box else "page")):
        raise ValueError("Некорректный фрагмент общего текста.")
    return fragment


@dataclass(frozen=True)
class Line:
    text: str
    left: float
    top: float
    right: float
    bottom: float


def make_line(words: list[dict]) -> Line:
    return Line(
        " ".join(word["text"] for word in words),
        words[0]["x0"],
        min(word["top"] for word in words),
        max(word["x1"] for word in words),
        max(word["bottom"] for word in words),
    )


def gutter(rows: list[list[dict]], page_left: float, width: float, height: float) -> float | None:
    """Locate a persistent central gap between rows of two text columns."""
    center = page_left + width / 2
    offsets = range(-6, 7)
    options = [center + width * offset / 100 for offset in offsets]
    votes = []
    for x in options:
        count = 0
        previous_side = 0
        for row in rows:
            first, last = row[0], row[-1]
            side = 0
            if last["x1"] - first["x0"] >= width * 0.15:
                side = -1 if last["x1"] <= x else 1 if first["x0"] >= x else 0
            # The two columns may have offset line baselines.
            if side and previous_side == -side:
                count += 1
            previous_side = side
            for left, right in zip(row, row[1:]):
                if left["x1"] <= x <= right["x0"] and right["x0"] - left["x1"] >= max(6, height * 0.7):
                    count += 1
                    break
        votes.append(count)
    best = max(range(len(options)), key=lambda i: (votes[i], -abs(options[i] - center)))
    return options[best] if votes[best] >= max(4, len(rows) * 0.18) else None


def paragraphs(lines: list[Line], line_height: float) -> list[list[Line]]:
    if not lines:
        return []
    gaps = [b.top - a.top for a, b in zip(lines, lines[1:]) if a.top < b.top <= a.top + 2 * line_height]
    pitch = median(gaps) if gaps else line_height * 1.25
    left = min(line.left for line in lines)
    groups = [[lines[0]]]
    for previous, line in zip(lines, lines[1:]):
        paragraph_gap = line.top - previous.top > max(pitch * 1.4, line_height * 1.5)
        indent = line.left - max(left, previous.left) > line_height * 0.65
        if paragraph_gap or indent:
            groups.append([])
        groups[-1].append(line)
    return groups


def reading_order(rows: list[list[dict]], left: float, width: float, height: float) -> list[list[Line]]:
    """Read each vertical band, ordering text columns before the next wide block."""
    bands: list[list[list[dict]]] = []
    previous_top = None
    for row in rows:
        top = min(word["top"] for word in row)
        if previous_top is None or top - previous_top > height * 3:
            bands.append([])
        bands[-1].append(row)
        previous_top = top

    result = []
    for band in bands:
        split = gutter(band, left, width, height)
        if split is None:
            result.extend(paragraphs([make_line(row) for row in band], height))
            continue
        columns: tuple[list[Line], list[Line]] = ([], [])
        wide = []
        for row in band:
            left_words = [word for word in row if word["x1"] <= split]
            right_words = [word for word in row if word["x0"] >= split]
            crosses = len(left_words) + len(right_words) < len(row)
            tight = left_words and right_words and right_words[0]["x0"] - left_words[-1]["x1"] < height * 0.5
            if crosses or tight:
                for column in columns:
                    result.extend(paragraphs(column, height))
                    column.clear()
                wide.append(make_line(row))
            else:
                result.extend(paragraphs(wide, height))
                wide.clear()
                for words, column in zip((left_words, right_words), columns):
                    if words:
                        column.append(make_line(words))
        for lines in (*columns, wide):
            result.extend(paragraphs(lines, height))
    return result


def source_box(lines: list[Line], bounds: tuple[float, float, float, float]) -> Box | None:
    left, top, right, bottom = bounds
    x0 = (min(line.left for line in lines) - left) / (right - left)
    y0 = (min(line.top for line in lines) - top) / (bottom - top)
    x1 = (max(line.right for line in lines) - left) / (right - left)
    y1 = (max(line.bottom for line in lines) - top) / (bottom - top)
    box = tuple(round(value, 6) for value in (x0, y0, x1, y1))
    l, t, r, b = box
    return (l, t, r, b) if 0 <= l < r <= 1 and 0 <= t < b <= 1 else None


def page_text_blocks(page: Page) -> list[list[Line]]:
    words = page.extract_words(x_tolerance=1)
    if not words:
        return []
    height = median(word["bottom"] - word["top"] for word in words)
    rows = [sorted(row, key=lambda word: word["x0"])
            for row in cluster_objects(words, "top", tolerance=max(1, height / 4))]
    return reading_order(rows, page.bbox[0], page.width, height)


def read_article_fragments(path: Path, pages: Iterable[int] | None = None) -> ArticleFragments:
    """Read pages in PDF order. A page with no text layer contributes no fragments."""
    fragments = []
    with path.open("rb") as stream:
        sha256 = file_digest(stream, "sha256").hexdigest()
        stream.seek(0)
        with pdfplumber.open(stream) as pdf:
            selected = tuple(range(1, len(pdf.pages) + 1)) if pages is None else tuple(sorted(set(pages)))
            if any(number < 1 or number > len(pdf.pages) for number in selected):
                raise ValueError(f"Номер страницы должен быть от 1 до {len(pdf.pages)}.")
            for number in selected:
                page = pdf.pages[number - 1]
                for index, lines in enumerate(page_text_blocks(page), 1):
                    box = source_box(lines, page.bbox)
                    fragments.append(Evidence(
                        id=f"{sha256[:12]}-p{number}-text-{index}",
                        page=number,
                        text="\n".join(line.text for line in lines),
                        box=box,
                        precision="text_fragment" if box is not None else "page",
                    ))
                page.close()
    return ArticleFragments(path.name, sha256, selected, tuple(fragments), version("pdfplumber"))
