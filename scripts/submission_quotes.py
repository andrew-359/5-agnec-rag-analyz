"""Restore the PDF spelling of typographic marks in exported citations."""

from collections import Counter, defaultdict
from importlib.util import module_from_spec, spec_from_file_location
import re


MARKS = "↑↓†‡*∗"
MARK_SPACING = re.compile(rf" *([{MARKS}]) *")
TYPOGRAPHY = re.compile(rf"([{MARKS}]|(?<![\w.])[-−](?=\d|\.\d))")


def pdf_spelling(quote, variants, norm):
    needle = norm(quote)
    if not needle or not TYPOGRAPHY.search(needle) or any(needle in text for text in variants):
        return quote
    chunks = TYPOGRAPHY.split(MARK_SPACING.sub(r"\1", needle))
    literals = iter(chunks[::2])
    pattern = re.escape(next(literals))
    for mark, literal in zip(chunks[1::2], literals, strict=True):
        if mark in MARKS:
            pattern += f" ?{re.escape(mark)} ?"
        else:
            pattern += "[-−]"
        pattern += re.escape(literal)
    if re.match(r"[+−-]?(?:\d|\.\d)", needle):
        pattern = r"(?<![\w.+−-])" + pattern
    if re.search(rf"[\d{MARKS}]$", needle):
        pattern += r"(?![\w.])"
    matches = {match[0].strip() for text in variants for match in re.finditer(pattern, text)}
    return next(iter(matches)) if len(matches) == 1 else quote


def repair_citations(tables, pdf_dir):
    checker_path = pdf_dir.parent / "tools/check_submission.py"
    if not checker_path.is_file():
        return {"status": "checker_unavailable", "rewrites": {}}
    spec = spec_from_file_location("submission_pdf_checker", checker_path)
    checker = module_from_spec(spec)
    spec.loader.exec_module(checker)

    by_paper = defaultdict(list)
    for table in ("results", "issues"):
        for row in tables[table]:
            quote = row.get("source_fragment")
            if quote and TYPOGRAPHY.search(checker.norm(quote)) and row.get("source_page"):
                by_paper[row["publication_id"]].append((table, row))

    rewrites = Counter()
    unavailable = {}
    for paper, rows in by_paper.items():
        try:
            pages = [[checker.norm(text) for text in variants]
                     for variants in checker.read_pages(pdf_dir / f"{paper}.pdf")]
        except (OSError, ValueError) as error:
            unavailable[paper] = str(error)
            continue
        for table, row in rows:
            page = int(row["source_page"])
            if not 1 <= page <= len(pages):
                continue
            quote = row["source_fragment"]
            restored = pdf_spelling(quote, pages[page - 1], checker.norm)
            if restored != quote:
                row["source_fragment"] = restored
                rewrites[table] += 1
    return {"status": "done" if not unavailable else "partial", "rewrites": dict(rewrites),
            "unavailable_pdfs": unavailable}
