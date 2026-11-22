"""Find the text that describes the experiment behind a selected table."""

import argparse
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from ..run import Evidence
from .captions import TABLE_LINE, TABLE_WORD, caption_model, comparison_exception, table_caption_lines, text_for_matching
from ..strategies.text_pdfplumber import ArticleFragments, validate_sources
from .benchmark_latency import latency_table_sources
from .watermark_table import table_sources


HEADING = re.compile(r"^([A-Z](?:\.\d+)*|\d+(?:\.\d+)*)\.?\s+([A-Z][^.!?]{1,100})$")
SETUP = re.compile(r"\b(?:experiment(?:al|s)? (?:setup|settings)|implementation details)\b", re.I)
RESULTS = re.compile(r"\b(?:results|performance comparison)\b", re.I)
CONDITIONS = re.compile(
    r"\b(?:implementation details|experimental settings|as (?:our|the) (?:generative|generator|base) model"
    r"|retrieved passages|we (?:use|used) .{0,60} as (?:our|the) test set)\b", re.I,
)
REPORTING = re.compile(
    r"\b(?:results?|comparison|performance|findings|scores?)\b.{0,180}"
    r"\b(?:in|by)\s+(?:the\s+)?$",
    re.I,
)
TABLE_REPORTS = re.compile(
    r"^\s+(?:shows?|reports?|summari[sz]es?|presents?)\b\s+[^.!?]{0,100}"
    r"\b(?:results?|comparisons?|performance|findings|scores?)\b", re.I,
)
SUPPLEMENT = re.compile(
    r"\b(?:supplement\w*|extend\w*|reproduc\w*|revisit\w*|reanaly[sz]\w*)\b.{0,65}$", re.I,
)
NAMED_QUESTION_DATASETS = re.compile(
    r"\bmultiple[- ]choice datasets\s*,\s*i\.e\.\s*,?\s+([^.!?;]+)", re.IGNORECASE,
)
DATASET_ALIAS = re.compile(r"\(([A-Z][A-Z0-9-]+)\)")
BasisKind = Literal["caption", "results_reference", "section", "setup_section", "experiment_section", "conditions",
                    "model_comparison", "peer_caption", "dataset_definition", "metric_definition", "latency_reference"]
ContextStatus = Literal["supported", "ambiguous", "not_found"]


@dataclass(frozen=True)
class Basis:
    kind: BasisKind
    source: Evidence


@dataclass(frozen=True)
class ContextCandidate:
    fragments: tuple[Evidence, ...]
    basis: tuple[Basis, ...]


@dataclass(frozen=True)
class TableContext:
    sha256: str
    table: str
    page: int
    status: ContextStatus
    candidates: tuple[ContextCandidate, ...]
    reason: str


@dataclass(frozen=True)
class Section:
    key: str
    title: str
    heading: Evidence
    fragments: tuple[Evidence, ...]


def sections(fragments: tuple[Evidence, ...]) -> list[Section]:
    result = []
    key, title, heading = "", "", None
    body = []
    for fragment in fragments:
        first, _, _ = fragment.text.partition("\n")
        match = HEADING.fullmatch(first.strip())
        if match:
            if heading is not None:
                result.append(Section(key, title, heading, tuple(body)))
            key, title = match.groups()
            heading = fragment
            body = []
        if heading is not None:
            body.append(fragment)
    if heading is not None:
        result.append(Section(key, title, heading, tuple(body)))
    return result


def condition_blocks(section: Section) -> list[tuple[Evidence, ...]]:
    blocks = []
    for index, fragment in enumerate(section.fragments):
        prose = fragment.text
        if fragment.id == section.heading.id:
            prose = prose.partition("\n")[2]
        candidate_text = " ".join(prose.split())
        if not CONDITIONS.search(candidate_text):
            continue
        block = (fragment,)
        if index + 1 < len(section.fragments):
            following = section.fragments[index + 1]
            if column_continuation(fragment, following):
                block += (following,)
        blocks.append(block)
    return blocks


def column_continuation(left: Evidence, right: Evidence) -> bool:
    return (
        left.page == right.page
        and left.box is not None and right.box is not None
        and left.box[2] <= right.box[0] and right.box[3] <= left.box[1]
        and not left.text.rstrip().endswith((".", "!", "?"))
        and bool(re.match(r"[a-z]", right.text.lstrip()))
    )


def is_results_reference(text: str, reference: re.Pattern) -> bool:
    # Limit evidence to the clause leading into the citation.
    normalized = " ".join(text.split())
    for match in reference.finditer(normalized):
        prefix = normalized[max(0, match.start() - 200):match.start()]
        prefix = re.split(r"[.!?]\s+", prefix)[-1]
        suffix = normalized[match.end():]
        if not SUPPLEMENT.search(prefix) and (REPORTING.search(prefix) or TABLE_REPORTS.match(suffix)):
            return True
    return False


def results_links(section: Section, reference: re.Pattern, caption_ids: set[str]) -> tuple[Basis, ...]:
    return tuple(
        Basis("results_reference", fragment)
        for fragment in section.fragments
        if fragment.id not in caption_ids and is_results_reference(fragment.text, reference)
    )


def setup_section(ordered: list[Section], index: int) -> int | None:
    scope = ordered[index]
    for previous_index in range(index, -1, -1):
        previous = ordered[previous_index]
        if previous.key.partition(".")[0] != scope.key.partition(".")[0]:
            break
        if (previous_index == index or scope.key.startswith(previous.key + ".")) and SETUP.search(previous.title):
            return previous_index
    return None


def section_links(ordered: list[Section], index: int, setup_index: int | None,
                  reference: re.Pattern, caption_ids: set[str]) -> tuple[Basis, ...]:
    scope = ordered[index]
    links = results_links(scope, reference, caption_ids)
    if setup_index is None:
        return links
    parent, _, _ = ordered[setup_index].key.rpartition(".")
    if not parent:
        return links

    # A repeated section number in another part of the document is not a shared parent.
    parent_scope = None
    for previous in reversed(ordered[:setup_index]):
        if previous.key == parent:
            parent_scope = previous
            break
        if not previous.key.startswith(parent + "."):
            break
    if parent_scope is None:
        return links

    sibling_links = []
    for later in ordered[index + 1:]:
        if not later.key.startswith(parent + "."):
            break
        if SETUP.search(later.title) and later.key.rpartition(".")[0] == parent:
            break
        if later.key.rpartition(".")[0] == parent and RESULTS.search(later.title):
            sibling_links.extend(results_links(later, reference, caption_ids))
    if sibling_links:
        return (*links, Basis("experiment_section", parent_scope.heading), *sibling_links)
    return links


def caption_conditions(source: Evidence, fragments: tuple[Evidence, ...]) -> ContextCandidate | None:
    target = caption_model(source.text)
    if target is None:
        return None
    peers = {}
    for fragment in fragments:
        if fragment.id == source.id:
            continue
        peer = caption_model(fragment.text)
        if peer and peer["table"].casefold() != target["table"].casefold() and (
            peer["scope"].casefold() == target["scope"].casefold()
        ):
            peers.setdefault(peer["model"].casefold(), fragment)

    references = [Basis("caption", source)]
    conditions = [source]
    for fragment in fragments:
        if fragment.id == source.id:
            continue
        exception = comparison_exception(fragment.text, target["model"], peers.keys())
        if exception is None:
            continue
        references.append(Basis("model_comparison", fragment))
        conditions.append(fragment)
        if exception.anchor in peers:
            peer_source = peers[exception.anchor]
            if not any(item.source.id == peer_source.id for item in references):
                references.append(Basis("peer_caption", peer_source))
    return ContextCandidate(tuple(conditions), tuple(references))


def question_dataset_aliases(text: str) -> set[str]:
    statement = NAMED_QUESTION_DATASETS.search(text_for_matching(text))
    return set(DATASET_ALIAS.findall(statement[1])) if statement else set()


def comparison_datasets(text: str) -> set[str]:
    comparison = re.search(r"\bcomparison on ([^.]+)\.", text_for_matching(text), re.I)
    if not comparison:
        return set()
    names = set(re.split(r"\s+and\s+|\s*,\s*", comparison[1]))
    return names if all(re.fullmatch(r"[A-Z][A-Z0-9-]+", name) for name in names) else set()


def caption_datasets(caption: Basis, ordered: list[Section]) -> tuple[ContextCandidate, ...]:
    # A named dataset definition identifies the task and dataset only.
    names = comparison_datasets(caption.source.text)
    if not names:
        return ()
    return tuple(
        ContextCandidate((fragment,), (caption, Basis("dataset_definition", fragment)))
        for section in ordered if section.title.casefold() == "datasets"
        for fragment in section.fragments
        if names <= question_dataset_aliases(fragment.text)
    )


def find_table_context(document: ArticleFragments, table: str, page: int,
                       *, located_caption: Evidence | None = None) -> TableContext:
    """Use section scope and traceable references to find a table's context."""
    label = re.sub(rf"^{TABLE_WORD}\s*(?:№\s*)?", "", table.strip(), flags=re.I)
    if not label or page < 1:
        raise ValueError("Укажите метку таблицы и страницу PDF с 1.")
    validate_sources(document.sha256, document.fragments)
    reference = re.compile(rf"\b{TABLE_WORD}\s*(?:№\s*)?{re.escape(label)}(?!\w|[.\-]\w)", re.I)
    all_captions = tuple(
        Basis("caption", fragment)
        for fragment in document.fragments
        if any(match["label"].casefold() == label.casefold()
               for _, match in table_caption_lines(fragment.text.splitlines()))
    )
    caption_ids = {item.source.id for item in all_captions}
    captions = tuple(item for item in all_captions if item.source.page == page)
    used_located_caption = False
    if not captions and located_caption is not None and located_caption.page == page and (
        region_match := TABLE_LINE.match(located_caption.text.partition("\n")[0])
    ) and region_match["label"].casefold() == label.casefold():
        validate_sources(document.sha256, (*document.fragments, located_caption))
        captions = (Basis("caption", located_caption),)
        all_captions += captions
        used_located_caption = True
    if len(captions) == len(all_captions) == 1 and (sources := table_sources(document.fragments, label, captions[0].source)):
        (declaration, _), definition = sources
        candidate = ContextCandidate((declaration, definition), (
            captions[0], Basis("results_reference", declaration), Basis("metric_definition", definition),
        ))
        return TableContext(document.sha256, label, page, "supported", (candidate,),
                            "Абзац с явной ссылкой подтверждает метод и метрику таблицы.")
    ordered = sections(document.fragments)
    supported = []
    possible = []
    for index, scope in enumerate(ordered):
        blocks = condition_blocks(scope)
        if not blocks:
            continue
        setup_index = setup_section(ordered, index)
        links = section_links(ordered, index, setup_index, reference, caption_ids)
        # Several paragraphs can describe the same experiment. They share the
        # section's link to the table, but each retains its own source.
        fragments = tuple({item.id: item for block in blocks for item in block}.values())
        section_basis = (Basis("setup_section", ordered[setup_index].heading)
                         if setup_index is not None else Basis("section", scope.heading))
        subsection = (Basis("section", scope.heading),) if setup_index not in (None, index) else ()
        conditions = tuple(Basis("conditions", block[0]) for block in blocks)
        candidate = ContextCandidate(fragments, (*captions, section_basis, *subsection, *links, *conditions))
        if captions and len(all_captions) == 1 and links:
            supported.append(candidate)
        elif links or (setup_index is not None and any(fragment.page == page for fragment in fragments)):
            possible.append(candidate)

    direct = (caption_conditions(captions[0].source, document.fragments)
              if not used_located_caption and captions and len(all_captions) == 1 else None)
    if direct and not supported:
        return TableContext(document.sha256, label, page, "supported", (direct,),
                            "Модель указана непосредственно в подписи выбранной таблицы.")
    if direct and len(supported) == 1:
        prose = supported[0]
        merged = {fragment.id: fragment for fragment in (*prose.fragments, *direct.fragments)}
        basis = {(item.kind, item.source.id): item for item in (*prose.basis, *direct.basis)}
        supported[0] = ContextCandidate(tuple(merged.values()), tuple(basis.values()))
    elif direct:
        possible.append(direct)

    if not supported and not possible and not used_located_caption and captions and len(all_captions) == 1:
        datasets = caption_datasets(captions[0], ordered)
        if len(datasets) == 1:
            return TableContext(document.sha256, label, page, "supported", datasets,
                                "Подпись таблицы и определения подтверждают наборы данных.")
        possible.extend(datasets)

    if len(supported) == 1:
        return TableContext(document.sha256, label, page, "supported", tuple(supported),
                            "Подпись и ссылка в результатах связывают условия с выбранной таблицей.")
    if supported or possible:
        candidates = (*supported, *possible)
        if len(all_captions) > 1:
            reason = "В PDF несколько подписей с этой меткой. Текстовая ссылка не уточняет страницу таблицы."
        elif len(supported) > 1:
            reason = "Несколько описаний условий имеют основания для связи с таблицей."
        else:
            reason = "Недостаточно оснований, чтобы связать описание условий с таблицей."
        return TableContext(document.sha256, label, page, "ambiguous", candidates,
                            reason)
    if len(captions) == len(all_captions) == 1 and (
        latency_sources := latency_table_sources(document.fragments, label, captions[0].source)
    ):
        declaration, definition = latency_sources
        fragments = (declaration, definition) if definition else (declaration,)
        basis = (captions[0], Basis("latency_reference", declaration))
        if definition:
            basis += (Basis("dataset_definition", definition),)
        candidate = ContextCandidate(fragments, basis)
        return TableContext(document.sha256, label, page, "supported", (candidate,),
                            "Абзац со ссылкой подтверждает замер задержки.")
    if used_located_caption:
        reason = ("В доступном тексте статьи не найдены условия, связанные с этой таблицей. "
                  "Подпись подтверждена отдельно по области таблицы.")
    elif captions:
        reason = "Подпись таблицы найдена, описание условий не связано с ней."
    else:
        reason = ("В доступном тексте статьи не найдены условия, связанные с этой таблицей. "
                  "В текстовых фрагментах указанной страницы нет её подписи.")
    return TableContext(document.sha256, label, page, "not_found", (), reason)


def main() -> None:
    parser = argparse.ArgumentParser(description="Найти контекст таблицы в JSON с фрагментами статьи.")
    parser.add_argument("fragments", type=Path)
    parser.add_argument("--table", required=True)
    parser.add_argument("--page", required=True, type=int)
    args = parser.parse_args()
    try:
        data = json.loads(args.fragments.read_text(encoding="utf-8"))
        if data.get("schema_version") != 1:
            raise ValueError("Ожидается версия 1 формата фрагментов статьи.")
        data["fragments"] = tuple(
            Evidence(**{**item, "box": tuple(item["box"]) if item["box"] is not None else None})
            for item in data["fragments"]
        )
        data["pages"] = tuple(data["pages"])
        result = find_table_context(ArticleFragments(**data), args.table, args.page)
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(1, f"{error}\n")
    print(json.dumps(asdict(result), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
