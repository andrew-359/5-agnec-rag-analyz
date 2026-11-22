"""Extract explicit experimental conditions with references to their sources."""

import argparse
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Literal

from ..run import Evidence
from .captions import caption_model, comparison_exception, text_for_matching
from .context import Basis
from ..strategies.text_pdfplumber import validate_sources


MODEL = r"[\w][\w./-]*-[\w][\w./-]*"
CITATION = r"(?:\s*\([^()]{1,160}\))?"
METHOD = r"[A-Z][A-Za-z0-9-]*"
SCOPE = r"[A-Za-z0-9][A-Za-z0-9_, -]{0,100}"
BACKBONE = re.compile(
    rf"\b(?:we|the authors)\s+(?:employ|use|used)\s+(?P<model>{MODEL}){CITATION}"
    rf"\s+as\s+the\s+(?:primary\s+)?(?:backbone|generator|generative model)\s+for\s+(?P<scope>{SCOPE})(?=[.!?;]|$)",
    re.IGNORECASE,
)
SPECIALIZED_GENERATORS = re.compile(
    r"\bofficial model checkpoints\b[^.!?;:]{0,100}:\s*"
    rf"\b(?P<first_scope>{METHOD})\s+employs its specialized\s+(?P<first_model>{MODEL})\s+variant{CITATION}"
    rf"(?:,\s+and\s+(?P<second_scope>{METHOD})\s+utilizes\s+(?P<second_model>{MODEL}){CITATION})?"
    r"(?=[.!?;]|$)",
)
SCOPED_MODEL = re.compile(
    rf"\b(?P<scope>In (?:these|this) experiments?),\s+we\s+(?:use|used|employ)\s+"
    rf"(?P<model>{MODEL}){CITATION}\s+as\s+(?:the|our)\s+(?:generator|generative model)\b",
    re.IGNORECASE,
)
HOLDOUT_TEST_SET = re.compile(
    r"\s+and\s+(?:a|the)\s+(?P<dataset>holdout set from [^.!?;]{1,100}? dataset)"
    r"\s+as\s+(?:our|the)\s+test set\b", re.IGNORECASE,
)
RETRIEVAL_K = re.compile(
    r"\b(?:number of re-?trieved passages\s+to\s+k|retrieval top[- ]k)\s*=\s*(?P<value>[1-9]\d*)"
    r"\s+(?P<scope>(?:across|for)\s+all\s+(?:methods|systems|configurations))\b",
    re.IGNORECASE,
)
RETRIEVED_ANSWER_MODEL = re.compile(
    rf"\b(?P<scope>{METHOD})(?: and other [\w -]+ re-?trievers)? provide(?:s)? only retrieved documents,"
    rf"\s*not the answers themselves\.\s*To extract answers, we use\s+(?:the\s+)?(?P<model>{MODEL})\s+model\b",
    re.IGNORECASE,
)
ConditionField = Literal["generator_model", "retrieval_top_k", "dataset"]


@dataclass(frozen=True)
class ConditionAssertion:
    field: ConditionField
    value: str | int
    scope: str
    evidence_id: str


@dataclass(frozen=True)
class RetrievedAnswerAssertion(ConditionAssertion):
    """The model answers from documents supplied by the named retriever."""


def retriever_in_variant(retriever: str, method: str) -> bool:
    tokens = method.split()
    return bool(tokens and tokens[0].casefold() == retriever.casefold())


@dataclass(frozen=True)
class CaptionAssertion(ConditionAssertion):
    scope_kind: Literal["table_default", "method_exception"]
    table: str
    page: int
    excluded_methods: tuple[str, ...] = ()
    supporting_evidence_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ArticleAssertions:
    sha256: str
    assertions: tuple[ConditionAssertion, ...]
    evidence: tuple[Evidence, ...]


def extract_condition_assertions(sha256: str, fragments: Iterable[Evidence]) -> ArticleAssertions:
    fragments = tuple(fragments)
    validate_sources(sha256, fragments)
    assertions: dict[ConditionAssertion, None] = {}
    evidence: dict[str, Evidence] = {}
    for fragment in fragments:
        # The original text keeps its line breaks. In the search text, keep
        # the hyphen in split identifiers such as Qwen2.5-7B-Instruct.
        text = text_for_matching(fragment.text)
        found: list[tuple[int, ConditionAssertion]] = []
        for pattern in (BACKBONE, SCOPED_MODEL):
            for match in pattern.finditer(text):
                found.append((match.start(), ConditionAssertion(
                    "generator_model", match["model"], match["scope"], fragment.id,
                )))
                if pattern is SCOPED_MODEL and (dataset := HOLDOUT_TEST_SET.match(text, match.end())):
                    found.append((dataset.start(), ConditionAssertion(
                        "dataset", dataset["dataset"], match["scope"], fragment.id,
                    )))
        for match in SPECIALIZED_GENERATORS.finditer(text):
            # The variants follow an explicit generator statement in the same paragraph.
            if not BACKBONE.search(text, 0, match.start()):
                continue
            for part in ("first", "second"):
                if match[f"{part}_model"]:
                    found.append((match.start(f"{part}_scope"), ConditionAssertion(
                        "generator_model", match[f"{part}_model"], match[f"{part}_scope"], fragment.id,
                    )))
        for match in RETRIEVED_ANSWER_MODEL.finditer(text):
            found.append((match.start(), RetrievedAnswerAssertion(
                "generator_model", match["model"], match["scope"], fragment.id,
            )))
        for match in RETRIEVAL_K.finditer(text):
            found.append((match.start(), ConditionAssertion(
                "retrieval_top_k", int(match["value"]), match["scope"], fragment.id,
            )))
        if found:
            evidence[fragment.id] = fragment
            for _, assertion in sorted(found, key=lambda entry: entry[0]):
                assertions[assertion] = None
    return ArticleAssertions(sha256, tuple(assertions), tuple(evidence.values()))


def extract_table_assertions(sha256: str, table: str, page: int, fragments: tuple[Evidence, ...],
                             basis: tuple[Basis, ...]) -> ArticleAssertions:
    """Extract from validated prose and an explicitly scoped table caption."""
    validate_sources(sha256, (*fragments, *(item.source for item in basis)))
    target_sources = {item.source.id: item.source for item in basis if item.kind == "caption"}
    if len(target_sources) != 1:
        return extract_condition_assertions(sha256, fragments)
    target = next(iter(target_sources.values()))
    caption = caption_model(target.text)
    if (caption is None or target.page != page or caption["table"].casefold() != table.casefold()
            or target not in fragments):
        return extract_condition_assertions(sha256, fragments)

    peers = {}
    for item in basis:
        if item.kind != "peer_caption":
            continue
        peer = caption_model(item.source.text)
        if peer and peer["table"].casefold() != table.casefold() and (
            peer["scope"].casefold() == caption["scope"].casefold()
        ):
            peers[peer["model"].casefold()] = item.source

    exceptions = []
    sources = {target.id: target}
    for item in basis:
        if item.kind != "model_comparison" or item.source not in fragments:
            continue
        match = comparison_exception(item.source.text, caption["model"], peers.keys())
        if match is None:
            continue
        sources[item.source.id] = item.source
        supporting = ()
        if caption["model"].casefold() != match.anchor:
            peer_source = peers[match.anchor]
            sources[peer_source.id] = peer_source
            supporting = (peer_source.id,)
        exceptions.append(CaptionAssertion(
            "generator_model", match.model, match.method, item.source.id, "method_exception", table, page,
            supporting_evidence_ids=supporting,
        ))
    default = CaptionAssertion(
        "generator_model", caption["model"], caption["scope"], target.id, "table_default", table, page,
        excluded_methods=tuple(dict.fromkeys(item.scope for item in exceptions)),
        supporting_evidence_ids=tuple(source_id for source_id in sources if source_id != target.id),
    )
    prose = extract_condition_assertions(sha256, (item for item in fragments if item.id not in sources))
    sources.update((item.id, item) for item in prose.evidence)
    return ArticleAssertions(sha256, (default, *dict.fromkeys(exceptions), *prose.assertions), tuple(sources.values()))


def source_fragments(data: dict) -> list[dict]:
    if "fragments" in data and data.get("schema_version") == 1:
        return data["fragments"]
    candidates = data.get("candidates", [])
    if data.get("status") == "supported" and len(candidates) == 1:
        return candidates[0]["fragments"]
    raise ValueError("Передайте фрагменты статьи версии 1 или результат поиска с одним подтверждённым кандидатом.")


def read_source(item: dict) -> Evidence:
    return Evidence(**{**item, "box": tuple(item["box"]) if item["box"] is not None else None})


def main() -> None:
    parser = argparse.ArgumentParser(description="Извлечь утверждения из фрагментов статьи или контекста таблицы.")
    parser.add_argument("input", type=Path)
    args = parser.parse_args()
    try:
        data = json.loads(args.input.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("sha256"), str):
            raise ValueError("В JSON нужен SHA-256 исходного PDF.")
        fragments = tuple(map(read_source, source_fragments(data)))
        if data.get("status") == "supported":
            basis = tuple(Basis(item["kind"], read_source(item["source"])) for item in data["candidates"][0]["basis"])
            result = extract_table_assertions(data["sha256"], data["table"], data["page"], fragments, basis)
        else:
            result = extract_condition_assertions(data["sha256"], fragments)
    except (KeyError, OSError, TypeError, ValueError) as error:
        parser.exit(1, f"{error}\n")
    print(json.dumps(asdict(result), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
