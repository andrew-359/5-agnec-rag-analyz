"""Scoped accuracy captions where the lower column header supplies a condition."""

from dataclasses import dataclass
import re
from typing import Iterable

from ..core.domain import ConditionOption, Evidence, GridCell, ReadTable, TableTarget
from ..core.measurement_binding import Assertion, Observation, Scope
from .captions import table_caption_line, text_for_matching


EVIDENCE_EVALUATION = re.compile(
    r"^(?:RAG\s+)?(?:(?P<accuracy>accuracy)|performance|results)"
    r"\s*(?P<percent>\(\s*%\s*\))?\s+using different evidence types across datasets and LLMs\.",
    re.I,
)
MODEL_GROUPS = re.compile(r"\bResults are grouped by dataset \(([^()]+)\) and LLM\b", re.I)
CULTURAL_EVALUATION = re.compile(
    r"^(?:(?P<accuracy>accuracy)|results|performance)\s*(?P<percent>\(\s*%\s*\))?"
    r"\s+on cultural QA benchmarks with (?P<generator>[\w./-]+) for a generator\.(?:\s|$)", re.I,
)
MODEL_ALIASES = re.compile(r"([\w./-]+)(?:\s*\[\d+\])?\s*\(denoted as ([\w./-]+)\)", re.I)
LANGUAGE_LEGEND = re.compile(r"^Number of MCQs per country and source language\b", re.I)
LANGUAGE_HEADING = re.compile(r"^Source Lang\.?\s+Country\b", re.I)
LANGUAGE_ENTRY = re.compile(r"(?m)^[ \t]*[A-Z][\w -]+\s+\(([a-z]{2,3})\)(?=\s|$)")


@dataclass(frozen=True)
class CaptionEvaluation:
    metric: str | None
    unit: str | None
    model_datasets: tuple[str, ...] = ()
    generator: str | None = None


def caption_evaluation(text: str, number: int) -> CaptionEvaluation | None:
    title = table_caption_line(text_for_matching(text))
    if not title or title["label"] != str(number):
        return None
    # Keep sentence punctuation for the declaration patterns.
    description = title["tail"].lstrip(" :.—–-")
    if (match := EVIDENCE_EVALUATION.match(description)) and (groups := MODEL_GROUPS.search(description)):
        datasets = tuple(item.strip() for item in groups[1].split(",") if item.strip())
        if datasets:
            return CaptionEvaluation("Accuracy" if match["accuracy"] else None,
                                     "%" if match["percent"] else None, datasets)
    if match := CULTURAL_EVALUATION.match(description):
        return CaptionEvaluation("Accuracy" if match["accuracy"] else None,
                                 "%" if match["percent"] else None, generator=match["generator"])
    return None


def model_aliases(text: str) -> list[tuple[str, str]]:
    normalized = text_for_matching(text)
    return MODEL_ALIASES.findall(normalized) if re.search(r"\bAnalyzed LLMs\.", normalized, re.I) else []


def language_dataset(text: str, dataset: str) -> bool:
    text = text_for_matching(text)
    return bool(re.match(rf"^{re.escape(dataset)}\s*(?:\([^()]+\)\s*)?evaluates\b", text, re.I)
                and re.search(r"\bsource-language communities\b", text, re.I))


def language_catalog(fragments: tuple[Evidence, ...]) -> tuple[Evidence, ...]:
    headings = {f.page for f in fragments if LANGUAGE_HEADING.match(text_for_matching(f.text))}
    legends = {}
    for fragment in fragments:
        title = table_caption_line(text_for_matching(fragment.text))
        if title and LANGUAGE_LEGEND.match(title["tail"].lstrip(" :.—–-")):
            legends.setdefault(fragment.page, []).append(fragment)
    pages = {page for page in headings if len(legends.get(page, ())) == 1}
    return tuple(f for f in fragments if f.page in pages and (
        LANGUAGE_ENTRY.search(f.text) or LANGUAGE_HEADING.match(text_for_matching(f.text)) or f in legends[f.page]
    ))


def repeated_generator_captions(table: ReadTable, fragments: tuple[Evidence, ...]) -> tuple[Evidence, ...]:
    title = table_caption_line(text_for_matching(table.caption))
    if not title:
        return ()
    return tuple(f for f in fragments if f.precision == "text_fragment" and f.box
                 and (peer := table_caption_line(text_for_matching(f.text)))
                 and peer["label"].casefold() != title["label"].casefold()
                 and peer["tail"].strip().casefold() == title["tail"].strip().casefold())


def accuracy_fragments(table: ReadTable, fragments: tuple[Evidence, ...]) -> tuple[Evidence, ...]:
    evaluation = caption_evaluation(table.caption, table.number)
    if not evaluation:
        return ()
    located = tuple(f for f in fragments if f.precision == "text_fragment" and f.box and f.page <= table.page)
    if evaluation.model_datasets:
        aliases = {cell.headers[1].casefold() for cell in table.cells if len(cell.headers) == 2}
        return tuple(f for f in located if any(alias.casefold() in aliases for _, alias in model_aliases(f.text)))

    datasets = {cell.headers[0] for cell in table.cells if len(cell.headers) == 2}
    definitions = tuple(f for f in located if any(language_dataset(f.text, label) for label in datasets))
    peers = repeated_generator_captions(table, fragments) if evaluation.generator else ()
    return tuple(dict.fromkeys((*definitions, *language_catalog(located), *peers))) if definitions else peers


def accuracy_claims(
    observation: Observation, evaluation: CaptionEvaluation, caption: Evidence,
    path: tuple[GridCell, GridCell], method: ConditionOption | None, context: tuple[Evidence, ...],
    *, generator_peers: tuple[Evidence, ...] = (),
) -> Iterable[Assertion]:
    source = observation.source
    target = TableTarget(source.page, source.table)
    table_scope = Scope(target)
    column = Scope(target, column=source.column)
    row = Scope(target, row=source.row)
    dataset_header, condition_header = path
    dataset, condition = dataset_header.text.strip(), condition_header.text.strip()

    if evaluation.metric:
        yield Assertion("metric", evaluation.metric, table_scope, (caption.id,))
    if evaluation.unit:
        yield Assertion("unit", evaluation.unit, table_scope, (caption.id,))
    if method:
        yield Assertion("method", method.value, row, method.evidence_ids)
    if evaluation.model_datasets:
        if dataset.casefold() in {name.casefold() for name in evaluation.model_datasets}:
            yield Assertion("dataset", dataset, column, (*dataset_header.evidence_ids, caption.id))
        for fragment in context:
            for name, alias in model_aliases(fragment.text):
                if alias.casefold() == condition.casefold():
                    yield Assertion("generator", name, column, (*condition_header.evidence_ids, fragment.id))
    elif evaluation.generator:
        if not generator_peers:
            yield Assertion("generator", evaluation.generator, table_scope, (caption.id,))
        definitions = tuple(f.id for f in context if language_dataset(f.text, dataset))
        if not definitions:
            return
        yield Assertion("dataset", dataset, column, (*dataset_header.evidence_ids, *definitions))
        for fragment in language_catalog(context):
            if condition in LANGUAGE_ENTRY.findall(fragment.text):
                yield Assertion("source_language", condition, column, (*condition_header.evidence_ids, fragment.id))
