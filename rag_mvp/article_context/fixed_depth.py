"""Connect a printed Fixed-k variant to its explicitly defined retrieval depth."""

import re
from dataclasses import dataclass

from ..core.domain import Box, Evidence
from ..table_region import overlap
from .captions import table_caption_lines, text_for_matching
from .context import Section


FIXED_VARIANT = re.compile(r"\bFixed-(?P<depth>[1-9]\d{0,8})\s+RAG\b", re.I)
DEPTH_DEFINITION = re.compile(r"^Fixed-k RAG[.:].*?\bretriev\w+\b.{0,40}\btop-\s*k\b", re.I)
COMPARISON = re.compile(r"\bwe compare\b", re.I)


@dataclass(frozen=True)
class FixedDepth:
    scope: str
    depth: int
    definition: Evidence
    declaration: Evidence
    rows: tuple[Evidence, ...]


def fixed_depths(
    sections: list[Section], fragments: tuple[Evidence, ...], caption: Evidence, region: Box | None,
) -> tuple[FixedDepth, ...]:
    page = tuple(item for item in fragments if item.page == caption.page)
    if region is None and sum(len(tuple(table_caption_lines(item.text.splitlines()))) for item in page) != 1:
        return ()
    definitions = [(section, fragment) for section in sections for fragment in section.fragments
                   if DEPTH_DEFINITION.search(text_for_matching(fragment.text))]
    if len(definitions) != 1:
        return ()
    section, definition = definitions[0]
    declarations = [fragment for fragment in section.fragments if COMPARISON.search(text_for_matching(fragment.text))]
    if len(declarations) != 1:
        return ()
    declaration, = declarations
    result = []
    for variant in FIXED_VARIANT.finditer(text_for_matching(declaration.text)):
        scope = variant[0]
        row = re.compile(rf"^\s*{re.escape(scope)}\s+(?:[+-]?\d|\.[0-9])", re.I)
        printed = tuple(
            item for item in page if any(row.match(line) for line in item.text.splitlines())
            and item.box and (region is None or overlap(item.box, region) > 0)
        )
        if printed:
            result.append(FixedDepth(scope, int(variant["depth"]), definition, declaration, printed))
    return tuple(result)
