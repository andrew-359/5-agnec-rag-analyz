"""Read short, source-located excerpts from the authors' labeled sections."""

import re
from hashlib import file_digest
from statistics import median

import pdfplumber
from pdfplumber.utils import cluster_objects

from .strategies.text_pdfplumber import gutter, page_text_blocks, source_box


ABSTRACT = re.compile(r"^(?:abstract|аннотация)\s*(?:\s*[:.—–-]\s*(.*))?$", re.I)
SPACED_ABSTRACT = re.compile(r"\b" + r"\s+".join("ABSTRACT") + r"\b", re.I)
ABSTRACT_WORD = re.compile(r"\b" + r"\s*".join("ABSTRACT") + r"\b", re.I)
CONCLUSION = re.compile(
    r"^(?:(?:summary (?:and|&) )?conclusions?(?: (?:and|&) (?:discussion|future (?:work|directions)))?"
    r"|concluding remarks|заключение|выводы(?: и заключение)?)\s*[:.]?$", re.I,
)
NUMBER = re.compile(r"^((?:\d+(?:\.\d+)*)\.?|[IVXLCDM]+\.)\s+", re.I)
BOUNDARY = re.compile(
    r"^(?:introduction|background|keywords?|index terms|discussion|limitations?|acknowledg(?:e)?ments?"
    r"|ethic(?:s|al) statement|references|bibliography|appendi(?:x|ces)(?:\s+[A-Z0-9]+)?"
    r"|введение|ключевые слова|ограничения|список литературы|приложение)(?:\s*[:.—–-].*)?$", re.I,
)
REFERENCES = re.compile(r"^(?:references|bibliography|appendi(?:x|ces)(?:\s+[A-Z0-9]+)?|список литературы|приложение)\s*[:.]?$", re.I)
SENTENCE_END = re.compile(r"[.!?](?=\s|$)")
FOOTER_MARGIN = .045
HEADING_CLEARANCE = .05
MAX_CONTINUATION_GAP = 2.5
MIN_EXCERPT_WORDS = 6
# A short opening excerpt is enough to link readers to the complete author section.
EXCERPT_LIMIT = 1800


def title(line):
    return NUMBER.sub("", line, count=1)


def heading_boxes(page, upright_lines, pattern, spaced=False):
    for line in upright_lines:
        match = pattern.fullmatch(title(line.text))
        if match and (box := source_box([line], page.bbox)):
            yield line.text, box, None
    if spaced:
        # Some publisher layouts put a spaced ABSTRACT alongside ARTICLE INFO.
        for match in page.search(ABSTRACT_WORD, return_chars=False):
            left, top, right, bottom = page.bbox
            box = ((match["x0"] - left) / (right - left), (match["top"] - top) / (bottom - top),
                   (match["x1"] - left) / (right - left), (match["bottom"] - top) / (bottom - top))
            yield "Abstract", box, (match["x0"] - .25, right)


def side_for(page, box):
    left, top, right, bottom = page.bbox
    start = top + page.height * box[1]
    words = page.extract_words(x_tolerance=1)
    measured = [word for word in words if start < word["top"] and word["bottom"] < bottom - page.height * .035]
    if not measured:
        return left, right
    height = median(word["bottom"] - word["top"] for word in measured)
    rows = [sorted(row, key=lambda word: word["x0"])
            for row in cluster_objects(measured, "top", tolerance=max(1, height / 4))]
    split = gutter(rows, left, page.width, height)
    if split is not None:
        if left + box[2] * page.width <= split:
            return left, split
        if left + box[0] * page.width >= split:
            return split, right
    return left, right


def section_lines(page, heading, box, markers, sides):
    left, right = sides
    x, y, _right, bottom = page.bbox
    start = y + box[3] * page.height + HEADING_CLEARANCE
    end = bottom - page.height * FOOTER_MARGIN
    number = NUMBER.match(heading)
    parent = number[1].rstrip(".") if number else None

    for item in markers:
        boundary = item["box"]
        top = y + boundary[1] * page.height
        if top <= start or x + boundary[2] * page.width <= left or x + boundary[0] * page.width >= right:
            continue
        if item["kind"] != "section":
            end = min(end, top)
            continue
        if item.get("role") == "possible_appendix" and not REFERENCES.fullmatch(title(item["text"])):
            # A bold bullet beginning with “A” can look like an appendix in the map.
            continue
        child = NUMBER.match(item["text"])
        if not (parent and child and child[1].startswith(parent + ".")):
            end = min(end, top)

    if start >= end:
        return []
    return read_excerpt(page.within_bbox((left, start, right, end)), heading)


def read_excerpt(page, heading):
    parts = []
    parent = NUMBER.match(heading)
    previous = None
    for block in page_text_blocks(page):
        for line in block:
            value = line.text.strip()
            if BOUNDARY.fullmatch(title(value)):
                return parts
            if previous and line.top - previous.bottom > (previous.bottom - previous.top) * MAX_CONTINUATION_GAP:
                return parts
            previous = line
            child = NUMBER.match(value)
            if parent and child and child[1].startswith(parent[1].rstrip(".") + "."):
                continue
            if not re.search(r"[^\W\d_]", value) or value.startswith(("©", "Findings of the Association")):
                continue
            parts.append(value)
    return parts


def join_lines(lines, words):
    combined = ""
    for line in lines:
        previous = re.search(r"([\w-]+)-$", combined)
        next_word = re.match(r"\w+", line)
        if previous and next_word:
            left, right = previous[1], next_word[0]
            if (left + right).lower() in words and f"{left}-{right}".lower() not in words:
                combined = combined[:-1]
            combined += line
        else:
            combined += f" {line}" if combined else line
    return combined


def extract_overview(pdf_path, inventory):
    selected = {"abstract": None, "conclusion": None}
    markers = {}
    for item in inventory["candidates"]:
        if item.get("box"):
            markers.setdefault(item["page"], []).append(item)
    vocabulary = set(re.findall(r"\w+(?:-\w+)*", " ".join(item.get("text", "") for item in inventory.get("pages", [])).lower()))
    with pdf_path.open("rb") as stream:
        if file_digest(stream, "sha256").hexdigest() != inventory["sha256"]:
            raise ValueError("Карта статьи относится к другой версии PDF.")
        stream.seek(0)
        with pdfplumber.open(stream) as document:
            for item in inventory.get("pages", []):
                number = item["page"]
                if not 1 <= number <= len(document.pages) or not item.get("text"):
                    continue
                # Skip pages without a heading in the saved text and stop before bibliography entries.
                lines = item["text"].splitlines()
                page_markers = markers.get(number, [])
                reference_headings = [marker for marker in page_markers
                                      if marker["kind"] == "section" and REFERENCES.fullmatch(title(marker["text"]))]
                candidates = [(key, pattern) for key, pattern in (("abstract", ABSTRACT), ("conclusion", CONCLUSION))
                              if not selected[key] and (key != "abstract" or number <= 2)
                              and any(pattern.fullmatch(title(line)) or (key == "abstract" and SPACED_ABSTRACT.search(line))
                                      for line in lines)]
                if not candidates:
                    if reference_headings:
                        break
                    continue
                page = document.pages[number - 1].filter(lambda obj: obj.get("upright", True))
                upright_lines = [line for block in page_text_blocks(page) for line in block]
                upright_text = {line.text for line in upright_lines}
                # Rotated running marks can turn an ordinary line into a false numbered heading.
                verified_markers = [marker for marker in page_markers
                                    if marker["kind"] != "section" or marker["text"] in upright_text]
                for key, pattern in candidates:
                    spaced = key == "abstract" and any(SPACED_ABSTRACT.search(line) for line in lines)
                    for heading, box, sides in heading_boxes(page, upright_lines, pattern, spaced):
                        sides = sides or side_for(page, box)
                        left, right = ((edge - page.bbox[0]) / page.width for edge in sides)
                        if any(marker["box"][2] <= left
                               or (marker["box"][0] < right and marker["box"][1] < box[1])
                               for marker in reference_headings):
                            continue
                        inline = pattern.fullmatch(title(heading))
                        initial = [inline[1]] if key == "abstract" and inline[1] else []
                        region = section_lines(page, heading, box, verified_markers, sides)
                        words = join_lines(initial + region, vocabulary)
                        sentences = list(SENTENCE_END.finditer(words[:EXCERPT_LIMIT]))
                        excerpt = words[:sentences[-1].end()] if sentences else ""
                        if len(excerpt.split()) < MIN_EXCERPT_WORDS:
                            continue
                        selected[key] = {"page": number, "text": excerpt}
                        break
                if all(selected.values()) or reference_headings:
                    break
    return selected
