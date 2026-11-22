"""Extract short, addressable excerpts for the article overview."""

import argparse
import json
import re
from collections import Counter
from pathlib import Path
from tempfile import TemporaryDirectory

import pdfplumber

from .strategies.text_pdfplumber import page_text_blocks, source_box
from .article_overview_sections import extract_overview


ABSTRACT = re.compile(r"^(?:abstract|аннотация)(?:\s*[:.—–-]\s*(?P<inline>.+))?$", re.I)
CONCLUSION = re.compile(
    r"^(?:\d+(?:\.\d+)*\.?\s+)?(?:conclusions?|concluding remarks|"
    r"(?:discussion and conclusions?)|заключение|выводы)$", re.I,
)
STOP = re.compile(r"^(?:\d+(?:\.\d+)*\.?\s+\D|references\b|bibliography\b|"
                  r"acknowledg\w*\b|limitations\b|appendix\b|keywords\b|литература\b)", re.I)
CONTRIBUTION = re.compile(r"\b(?:we (?:propose|present|introduce|develop|investigate|study)|"
                          r"this (?:paper|work) (?:proposes|presents|introduces|investigates)|"
                          r"предлагаем|представляем)\b", re.I)
SENTENCE_END = re.compile(r"[.!?](?=\s+[A-ZА-Я]|$)")
TOPICS = (
    (r"\b(?:graph[ -]rag|knowledge graphs?|graph topology)\b", "Графовый RAG"),
    (r"\b(?:process reward|process-level supervision)\b", "Обучение RAG с оценкой шагов"),
    (r"\b(?:multimodal|multi-modal|visual retrieval)\b", "Мультимодальный поиск"),
)


def excerpt(page, lines, text):
    return {"text": text, "page": page.page_number, "box": source_box(lines, page.bbox)}


def read_title(page, blocks, body_size):
    candidates = []
    for line in (line for block in blocks for line in block):
        if line.top > page.bbox[1] + page.height * .32:
            break
        if ABSTRACT.fullmatch(line.text):
            break
        if line.bottom - line.top < body_size * 1.25 or not any(c.isalpha() for c in line.text):
            if candidates:
                break
            continue
        if candidates and line.top - candidates[-1].bottom > body_size * 1.4:
            break
        candidates.append(line)
    title = " ".join(line.text for line in candidates)
    return excerpt(page, candidates, title) if 12 <= len(title) <= 260 else None


def section_excerpt(page, blocks, pattern):
    for block_index, block in enumerate(blocks):
        for i, line in enumerate(block):
            match = pattern.fullmatch(line.text.strip())
            if not match:
                continue
            inline = match.groupdict().get("inline")
            body = block[i + 1:]
            if not body and not inline and block_index + 1 < len(blocks):
                next_block = blocks[block_index + 1]
                if next_block and abs(next_block[0].left - line.left) <= page.width * .15:
                    body = next_block
            lines, parts = ([line], [inline]) if inline else ([], [])
            for next_line in body:
                if STOP.match(next_line.text) or ABSTRACT.fullmatch(next_line.text) or CONCLUSION.fullmatch(next_line.text):
                    break
                if len(lines) >= 32:
                    break
                lines.append(next_line)
                parts.append(next_line.text)
            raw = "\n".join(parts)
            ends = [match.end() for match in SENTENCE_END.finditer(raw)]
            spans = list(zip([0, *ends], [*ends, len(raw)]))
            chosen = next(((start, end) for start, end in spans
                           if 40 <= end - start <= 420 and CONTRIBUTION.search(raw[start:end])), None)
            if pattern is CONCLUSION:
                complete = [end for end in ends if 65 <= end <= 350]
                chosen = (0, complete[-1]) if complete else chosen
            elif chosen is None:
                complete = [end for end in ends if 65 <= end <= 300]
                chosen = (0, complete[-1]) if complete else None
            start, end = chosen or (0, min(len(raw), 280))
            while start < end and raw[start].isspace():
                start += 1
            fragment = raw[start:end]
            text = re.sub(r"(?<=\w)-\n(?=\w)", "", fragment)
            text = " ".join(text.split())
            if len(text) >= 40:
                first_line = raw.count("\n", 0, start)
                last_line = raw.count("\n", 0, end) + 1
                if chosen is None and not text.endswith((".", "!", "?")):
                    text = text.rstrip("- ") + "…"
                return excerpt(page, lines[first_line:last_line], text)
    return None


class ArticleOverview:
    def __init__(self):
        self.title = self.abstract = self.conclusion = None
        self.has_text = False

    def read_page(self, page, blocks, body_size):
        self.has_text |= bool(blocks)
        if page.page_number == 1:
            self.title = read_title(page, blocks, body_size)
        if page.page_number <= 2 and self.abstract is None:
            self.abstract = section_excerpt(page, blocks, ABSTRACT)
        if self.conclusion is None:
            self.conclusion = section_excerpt(page, blocks, CONCLUSION)

    def result(self):
        context = " ".join(item["text"] for item in (self.title, self.abstract) if item)
        topic = next((label for pattern, label in TOPICS if re.search(pattern, context, re.I)), None)
        return {"title": self.title, "abstract": self.abstract, "conclusion": self.conclusion,
                "topic": topic, "has_text": self.has_text}


def normalized_excerpt(text):
    return "".join(char for char in text.casefold() if char.isalnum())


def refine_overview(pdf, inventory, overview):
    if not overview["has_text"]:
        return overview
    for section, confirmed in extract_overview(pdf, inventory).items():
        if not confirmed:
            continue
        current = overview[section]
        # Keep the short excerpt and its rectangle when the isolated column confirms it.
        if current and current["page"] == confirmed["page"] and (
            normalized_excerpt(current["text"]) in normalized_excerpt(confirmed["text"])
        ):
            continue
        # A correct page proves the text source. It does not establish its highlight rectangle.
        overview[section] = {**confirmed, "box": None}
    return overview


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("pdf", type=Path)
    cli.add_argument("--inventory", type=Path)
    args = cli.parse_args()
    if args.inventory:
        inventory = json.loads(args.inventory.read_text(encoding="utf-8"))
        overview = ArticleOverview()
        with pdfplumber.open(args.pdf) as pdf:
            for page in pdf.pages:
                sizes = Counter(round(char["size"], 1) for char in page.chars if char["text"].strip())
                overview.read_page(page, page_text_blocks(page), sizes.most_common(1)[0][0] if sizes else 10)
                page.close()
        result = refine_overview(args.pdf, inventory, overview.result())
    else:
        from .inventory import inspect_pdf

        with TemporaryDirectory(prefix="rag-overview-") as folder:
            result = inspect_pdf(args.pdf, Path(folder) / "map", discover_scans=False)["overview"]
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
