"""Recognize directly stated caption conditions and comparison exceptions."""

import re
from collections.abc import Collection, Iterable, Iterator
from dataclasses import dataclass

TABLE_WORD = r"(?:Table|Tab\.|Таблица|Табл\.)"
TABLE_LABEL = r"(?:[A-ZА-Я]?[.-]?\d+(?:[.-]\d+)*[A-ZА-Я]?|[IVXLCDM]+)"
TABLE_LINE = re.compile(
    rf"^[ \t]*(?P<word>{TABLE_WORD})[ \t]*(?:№[ \t]*)?"
    rf"(?P<label>{TABLE_LABEL})(?!\w|[.-]\w)(?P<tail>[^\n]*)$",
    re.IGNORECASE,
)
REFERENCE_AFTER_PERIOD = re.compile(
    r"^(?:specifically|however|in particular|for example|from the results|shows?|presents?|"
    r"в частности|однако|например|показывает|содержит)\b", re.IGNORECASE,
)
RESULTS_CAPTION_TITLE = re.compile(r"(?:Main|Overall) experimental results\b")


def table_caption_line(line: str) -> re.Match[str] | None:
    """Find a caption marker at the start of a physical line."""
    match = TABLE_LINE.match(line)
    if not match:
        return None
    tail = match["tail"].strip()
    if not tail or tail[0] in ":—–-":
        return match
    if RESULTS_CAPTION_TITLE.match(tail):
        return match
    if tail.startswith("."):
        title = tail[1:].lstrip()
        if not title or (title[0].isupper() and not REFERENCE_AFTER_PERIOD.match(title)):
            return match
    return None


def table_caption_lines(lines: Iterable[str]) -> Iterator[tuple[int, re.Match[str]]]:
    """Use strong delimiters within blocks and stricter rules for bare labels."""
    at_start = True
    for index, line in enumerate(lines):
        match = table_caption_line(line)
        if match and (at_start or match["tail"].lstrip().startswith((":", "—", "–", "-"))):
            yield index, match
        if any(char.isalnum() for char in line):
            at_start = False


MODEL_NAME = r"[A-Za-z][\w./-]*-[\w./-]+(?:\s+\([\w. -]+\))?"
CAPTION = re.compile(
    rf"^Table\s+(?P<table>[\w.-]+)\s*:\s*"
    rf"(?P<scope>Results\s+[^.!?]{{1,180}}?)\s+with\s+(?P<model>{MODEL_NAME})"
    r"\s+as\s+the\s+(?:LLM|generator|generative model)\b",
    re.IGNORECASE,
)
COMPARISON = re.compile(
    r"^Table\s+[\w.-]+\s*:.*?"
    r"(?P<method>[A-Za-z][\w-]*)[∗*]?\s+is trained with a different base LLM,\s+namely\s+"
    r"(?P<family>[\w.-]+)(?:\s*\([^()]+\))?\s*;\s*"
    rf"therefore,\s+we compare the results of\s+(?P<anchor>{MODEL_NAME})\s+with the results from\s+"
    r"(?P=method)\s+with\s+(?P<anchor_model>(?P=family)\s*\([\w. -]+\))\s+and the\s+"
    r"results of others\s+with the results from\s+(?P=method)\s+with\s+"
    r"(?P<other_model>(?P=family)\s*\([\w. -]+\))",
    re.IGNORECASE,
)


def text_for_matching(text: str) -> str:
    text = re.sub(r"(?<=\w)-[ \t]*\n\s*(?=\w)", "-", text)
    return " ".join(text.split())


def caption_model(text: str) -> re.Match[str] | None:
    return CAPTION.match(text_for_matching(text))


@dataclass(frozen=True)
class ModelException:
    method: str
    model: str
    anchor: str


def comparison_exception(text: str, model: str, peer_models: Collection[str]) -> ModelException | None:
    match = COMPARISON.match(text_for_matching(text))
    if not match:
        return None
    anchor = match["anchor"].casefold()
    if model.casefold() == anchor:
        return ModelException(match["method"], match["anchor_model"], anchor)
    # "Others" requires a peer in the same caption series which names the
    # model explicitly used as the comparison anchor.
    if anchor in peer_models:
        return ModelException(match["method"], match["other_model"], anchor)
    return None
