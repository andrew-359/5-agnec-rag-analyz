"""Use a named dataset in a caption only in the scope it establishes."""

import re

from ..core.domain import Evidence, GridCell, TableTarget
from ..core.measurement_binding import Assertion, Observation, Scope
from .captions import table_caption_line, text_for_matching

NAME = r"[A-Za-z][\w-]*"
SEPARATOR = r"\s+(?:and|or)\s+|\s*,\s*(?:(?:and|or)\s+)?"
NAMES = rf"{NAME}(?:(?:{SEPARATOR}){NAME})*"
CAPTION_SEPARATOR = rf"(?:{SEPARATOR})(?:(?:on|in)\s+(?:the\s+)?)?"
CAPTION_DATASETS = re.compile(
    rf"\b(?:on|in)\s+(?:the\s+)?(?:datasets?\s+)?"
    rf"(?P<names>{NAME}(?:{CAPTION_SEPARATOR}{NAME})*)(?:\s+datasets?)?$", re.I,
)
DECLARED_LIST = re.compile(rf"\b(?:datasets?|benchmarks?)\b[^.!?;,]*,\s*({NAMES})", re.I)


def caption_dataset_names(text: str, table: int) -> tuple[str, ...]:
    caption = table_caption_line(text_for_matching(text))
    if not caption or caption["label"] != str(table):
        return ()
    title = re.split(r"[.!?](?:\s+|$)", caption["tail"].lstrip(" :.—–-"), maxsplit=1)[0]
    match = CAPTION_DATASETS.search(title.rstrip())
    if not match:
        return ()
    return tuple(dict.fromkeys(name.casefold() for name in re.split(CAPTION_SEPARATOR, match["names"], flags=re.I)))


def dataset_definitions(names: tuple[str, ...], fragments: tuple[Evidence, ...]) -> dict[str, tuple[str, Evidence]]:
    definitions = {}
    for name in names:
        if name in {"same", "dataset", "datasets", "benchmark", "benchmarks"}:
            continue
        token = rf"(?<![\w-])({re.escape(name)})(?![\w-])"
        adjacent = re.compile(
            rf"{token}\s+(?:datasets?|benchmarks?)\b|\b(?:datasets?|benchmarks?)\s*[: ,]\s*{token}", re.I,
        )
        for fragment in fragments:
            if fragment.precision != "text_fragment" or not fragment.box:
                continue
            text = text_for_matching(fragment.text)
            if table_caption_line(text):
                continue
            if match := adjacent.search(text):
                definitions[name] = (match[1] or match[2], fragment)
                break
            for mention in DECLARED_LIST.finditer(text):
                if match := re.search(token, mention[1], re.I):
                    definitions[name] = (match[1], fragment)
                    break
            if name in definitions:
                break
    return definitions


def dataset_claim(
    observation: Observation, caption: Evidence, names: tuple[str, ...],
    definitions: dict[str, tuple[str, Evidence]], path: tuple[GridCell, ...],
) -> Assertion | None:
    # A partially identified list cannot supply a table-wide fallback.
    if not names or not all(name in definitions for name in names):
        return None
    target = TableTarget(observation.source.page, observation.source.table)
    if len(names) == 1:
        name, = names
        label, definition = definitions[name]
        return Assertion("dataset", label, Scope(target), (caption.id, definition.id))

    groups = [header for header in path[:-1] if header.text.strip().casefold() in names]
    if len(groups) != 1:
        return None
    group, = groups
    label, definition = definitions[group.text.strip().casefold()]
    return Assertion("dataset", label, Scope(target, column=observation.source.column),
                     (caption.id, *group.evidence_ids, definition.id))
