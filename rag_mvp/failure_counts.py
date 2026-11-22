"""Read reported counts and attach their category and sample evidence."""

import re
from collections.abc import Iterable, Iterator
from dataclasses import asdict

from .article_context.context import sections
from .strategies.text_pdfplumber import ArticleFragments
from .run import Evidence


SAMPLED_ERRORS = re.compile(
    r"\bon the (?P<dataset>[\w-]+) dataset(?:\s+\([^()]*\))?\.\s+"
    r"Specifically, we randomly sampled (?P<size>[1-9][0-9]*) incorrect\s+prediction cases\b",
    re.I,
)
BULLET_COUNT = re.compile(
    r"^[ \t]*•[ \t]*(?P<quote>(?P<category>[A-Za-z][A-Za-z \t-]*?)\s+"
    r"\((?:\d+(?:\.\d+)?%,\s*)?(?P<count>[0-9]+)\s+cases\)):",
    re.M,
)
REVIEWED_FAILURES = re.compile(
    r"\b(?:manual\s+review|analysis)\s+of\s+(?:the\s+)?"
    r"(?P<size>[1-9][0-9]*)\s+(?:failed|unsuccessful)\s+cases\b",
    re.I,
)
REVIEW_FINDINGS = re.compile(r",\s*we\s+(?:find|found)\s*:\s*", re.I)
NUMBERED_COUNT = re.compile(
    r"\((?P<item>[ivx]+)\)\s+(?P<count>[0-9]+)\s+(?P<category>[^;]+?)"
    r"(?=\s*;\s*(?:and\s+)?\([ivx]+\)|\.(?=\s|$))",
    re.S,
)
NUMBERED_SEPARATOR = re.compile(r"\s*;\s*(?:and\s+)?")
ORIGINAL_QUESTIONS = re.compile(
    r"\baccuracy\s+of\s+(?P<subject>[\w\s-]+?)\s+to\s+(?P<size>[1-9][0-9]*)\s+questions"
    r"(?:,\s*resulting in\s+\d+(?:\.\d+)?%)?\.\s*(?:Upon\s+)?$",
    re.I,
)


def quote(fragment: Evidence, start: int, end: int) -> dict:
    return {
        **asdict(fragment), "id": f"{fragment.id}-quote-{start}-{end}",
        "text": fragment.text[start:end], "fragment_id": fragment.id, "text_range": (start, end),
    }


def passage_quotes(fragments: tuple[Evidence, ...], start: int, end: int) -> list[dict]:
    """Map spans of the reading-order passage back to unmodified saved fragments."""
    result = []
    offset = 0
    for fragment in fragments:
        limit = offset + len(fragment.text)
        if start < limit and end > offset:
            result.append(quote(fragment, max(start - offset, 0), min(end, limit) - offset))
        offset = limit + 1  # A newline separates consecutive fragments.
    return result


def display_text(text: str) -> str:
    return " ".join(re.sub(r"(?<=\w)-\n(?=\w)", "", text).split())


def source(quotation: dict, section: str | None) -> dict:
    locator = {
        "page": quotation["page"], "table": None, "box": quotation["box"],
        "evidence_ids": [quotation["id"]],
    }
    if section:
        locator["section"] = section
    return locator


def add_observation(artifact: dict, count: str, category: str, value_source: dict,
                    conditions: dict[str, list[dict]], fields: dict, section: str | None) -> None:
    artifact["results"].append({
        "id": f"{value_source['id']}-failure-count",
        "metric_group": "failure analysis", "metric": "failure count", "metric_variant": category,
        "raw_value": count, "raw_unit": "cases", "value": count, "unit": "cases",
        "source": source(value_source, section), "source_fragment": value_source["text"],
        "condition_sources": {field: [item["id"] for item in items] for field, items in conditions.items()},
        **fields,
    })
    artifact["evidence"].extend([value_source, *(item for items in conditions.values() for item in items)])


def add_ambiguity(artifact: dict, quotation: dict, reason: str, section: str | None = None) -> None:
    artifact["remarks"].append({
        "id": f"{quotation['id']}-failure-binding", "kind": "ambiguous-binding",
        "raw_text": quotation["text"], "reason": reason, "source": source(quotation, section),
    })
    artifact["evidence"].append(quotation)


def add_bullet_counts(artifact: dict, document: ArticleFragments) -> None:
    for section in sections(document.fragments):
        samples = [(index, match) for index, fragment in enumerate(section.fragments)
                   for match in SAMPLED_ERRORS.finditer(fragment.text)]
        if not samples and not re.search(r"\b(?:error|failure)\s+analysis\b", section.title, re.I):
            continue
        sample_index, sample = samples[0] if len(samples) == 1 else (-1, None)
        in_list = False
        for index, fragment in enumerate(section.fragments):
            if index == sample_index:
                in_list = True
            elif in_list:
                previous = section.fragments[index - 1]
                lead = fragment.text.lstrip()
                in_list = (previous.page == fragment.page and (
                    lead.startswith("•") or (
                        re.match(r"[a-z]", lead) and not previous.text.rstrip().endswith((".", "?", "!"))
                    )
                ))
            start = sample.end() if index == sample_index else 0
            for category in BULLET_COUNT.finditer(fragment.text, start):
                counted = quote(fragment, *category.span("quote"))
                if not in_list:
                    add_ambiguity(artifact, counted, "Для категории не установлена выборка анализа.", section.key)
                    continue
                denominator = quote(section.fragments[sample_index], *sample.span())
                add_observation(
                    artifact, category["count"], " ".join(category["category"].split()), counted,
                    {"dataset": [denominator], "sample_size": [denominator],
                     "experiment": [quote(section.heading, 0, len(section.heading.text.splitlines()[0]))],
                     "metric_variant": [quote(fragment, *category.span("category"))]},
                    {"experiment": section.title, "dataset": sample["dataset"], "sample_size": int(sample["size"])},
                    section.key,
                )


def numbered_counts(passage: str, offset: int) -> list[re.Match]:
    """A complete first sentence with consecutive numbered categories supplies the scope."""
    counts = []
    for item in ("i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x"):
        count = NUMBERED_COUNT.match(passage, offset)
        if count is None or count["item"] != item:
            return []
        counts.append(count)
        offset = count.end()
        if passage[offset:].lstrip().startswith("."):
            return counts
        separator = NUMBERED_SEPARATOR.match(passage, offset)
        if separator is None:
            return []
        offset = separator.end()
    return []


def same_column_following(left: Evidence, right: Evidence) -> bool:
    if left.page != right.page or left.box is None or right.box is None:
        return False
    line_height = (left.box[3] - left.box[1]) / len(left.text.splitlines())
    gap = right.box[1] - left.box[3]
    return abs(left.box[0] - right.box[0]) <= line_height and 0 <= gap <= line_height


def review_passages(fragments: tuple[Evidence, ...], index: int) -> Iterator[tuple[Evidence, ...]]:
    """Consider directly adjacent lines and starts of continuations in the other column."""
    first = fragments[index]
    yield (first,)
    tail = fragments[index + 1:]
    for position, following in enumerate(tail):
        if following.page != first.page:
            break
        same_column = position == 0 and same_column_following(first, following)
        next_column = (first.box and following.box and first.box[2] <= following.box[0]
                       and following.box[3] <= first.box[3])
        if (next_column and position > 0 and same_column_following(tail[position - 1], following)):
            continue
        if not (same_column or next_column) or not re.match(r"[a-z(]", following.text.lstrip()):
            continue
        passage = [first, following]
        for line in tail[position + 1:]:
            if not same_column_following(passage[-1], line):
                break
            passage.append(line)
        yield tuple(passage)


def add_review_counts(artifact: dict, document: ArticleFragments) -> None:
    for index, fragment in enumerate(document.fragments):
        for sample in REVIEWED_FAILURES.finditer(fragment.text):
            findings = REVIEW_FINDINGS.match(fragment.text, sample.end())
            if findings is None:
                continue
            denominator = quote(fragment, *sample.span())
            candidates = []
            for fragments in review_passages(document.fragments, index):
                text = "\n".join(item.text for item in fragments)
                counts = numbered_counts(text, findings.end())
                if counts:
                    candidates.append((fragments, counts))
                    if len(fragments) == 1:
                        break
            if len(candidates) != 1:
                add_ambiguity(artifact, denominator, "Не установлена однозначная связь выборки с категориями случаев.")
                continue
            fragments, counts = candidates[0]
            fields = {"sample_size": int(sample["size"])}
            conditions = {"sample_size": [denominator]}
            if original := ORIGINAL_QUESTIONS.search(fragment.text, 0, sample.start()):
                fields["original_sample_size"] = int(original["size"])
                fields["experiment"] = display_text(original["subject"])
                conditions["original_sample_size"] = [quote(fragment, *original.span())]
                conditions["experiment"] = [quote(fragment, *original.span("subject"))]
            for count in counts:
                value_source, = passage_quotes(fragments, *count.span("count"))
                add_observation(
                    artifact, count["count"], display_text(count["category"]), value_source,
                    {**conditions, "metric_variant": passage_quotes(fragments, *count.span("category"))},
                    fields, None,
                )


def add_failure_counts(artifact: dict, document: ArticleFragments) -> None:
    add_bullet_counts(artifact, document)
    add_review_counts(artifact, document)
    artifact["evidence"] = list({item["id"]: item for item in artifact["evidence"]}.values())


def table_observation(row: dict, run: dict, evidence: dict[str, dict], prefix: str) -> tuple[dict, list[dict]] | None:
    if row.get("metric") != "failure count" or row.get("unit") != "cases":
        return None
    locator = row.get("source", {})
    if not locator.get("table") or (locator.get("page"), locator["table"]) != (run.get("page"), run.get("table")):
        return None
    value = row.get("value")
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]+", value):
        return None
    cells = [evidence[ref] for ref in locator.get("evidence_ids", ()) if ref in evidence
             and evidence[ref].get("precision") in {"cell", "estimated_cell"}
             and evidence[ref].get("page") == locator["page"]
             and evidence[ref].get("box") and evidence[ref]["box"] == locator.get("box")
             and evidence[ref].get("text", "").strip() == row.get("raw_value")]
    if len(cells) != 1:
        return None
    cell, = cells
    count = re.fullmatch(r"([0-9]+)(?:\s+cases)?", cell["text"].strip(), re.I)
    if count is None or count[1].lstrip("0") != value.lstrip("0"):
        return None

    conditions = row.get("condition_sources", {})
    refs = dict.fromkeys([cell["id"], *(ref for ids in conditions.values() for ref in ids if ref in evidence)])
    sources = [{**evidence[ref], "id": prefix + ref} for ref in refs]
    observation = {
        **row, "id": prefix + row["id"], "source_fragment": cell["text"],
        "source": {**locator, "evidence_ids": [prefix + cell["id"]]},
        "condition_sources": {field: [prefix + ref for ref in ids if ref in evidence]
                              for field, ids in conditions.items()},
    }
    return observation, sources


def citation_location(item: dict) -> tuple:
    return (item.get("fragment_id"), tuple(item.get("text_range", ())), item.get("text"),
            item.get("page"), tuple(item.get("box") or ()), item.get("precision"))


def same_scope(prose: dict, table: dict, prose_evidence: dict[str, dict],
               table_evidence: dict[str, dict]) -> bool:
    if not prose.get("experiment") or type(table.get("sample_size")) is not int:
        return False
    if any(prose.get(field) != table.get(field) for field in (
        "experiment", "metric_variant", "sample_size", "original_sample_size", "dataset",
        "method", "baseline_method", "generator", "task", "protocol", "statistic", "retrieval_top_k", "components",
    )):
        return False
    if table["metric_variant"] not in table["source"].get("headers", ()):
        return False

    # The numeric denominator alone cannot identify a cohort, even within one experiment.
    for field in ("experiment", "sample_size"):
        prose_refs = prose["condition_sources"].get(field, ())
        table_refs = table["condition_sources"].get(field, ())
        if not any(
            citation_location(prose_evidence[left]) == citation_location(table_evidence[right])
            for left in prose_refs for right in table_refs
        ):
            return False
    return True


def reconcile_table_counts(artifact: dict, runs: Iterable[dict]) -> None:
    prose_counts = [row for row in artifact["results"] if row["metric"] == "failure count"]
    evidence = {item["id"]: item for item in artifact["evidence"]}
    for index, run in enumerate(runs, 1):
        if run.get("source", {}).get("sha256") != artifact["source"]["sha256"]:
            continue
        table_evidence = {item["id"]: item for item in run.get("evidence", ())}
        for row in run.get("results", ()):
            result = table_observation(row, run, table_evidence, f"failure-count-table-{index}-")
            if result is None:
                continue
            table, sources = result
            scoped_sources = {item["id"]: item for item in sources}
            related = [
                prose for prose in prose_counts
                if prose["metric_variant"] == table.get("metric_variant")
                and all(table.get(field) in (None, prose.get(field)) for field in ("experiment", "sample_size"))
            ]
            matches = [prose for prose in related if same_scope(prose, table, evidence, scoped_sources)]
            if len(matches) == 1:
                prose, = matches
                if prose["value"].lstrip("0") == table["value"].lstrip("0"):
                    prose.setdefault("additional_sources", []).append(table["source"])
                    for ref in table["source"]["evidence_ids"]:
                        evidence[ref] = scoped_sources[ref]
                    continue
                artifact["remarks"].append({
                    "id": table["id"] + "-conflict", "kind": "reading-conflict",
                    "raw_text": f"{prose['raw_value']} / {table['raw_value']}",
                    "reason": "Проза и таблица дают разные числа для одной категории, эксперимента и выборки.",
                    "source": prose["source"], "result_ids": [prose["id"], table["id"]],
                    "options": [{"value": item["raw_value"], "evidence_ids": item["source"]["evidence_ids"]}
                                for item in (prose, table)],
                })
            elif related:
                artifact["remarks"].append({
                    "id": table["id"] + "-scope", "kind": "ambiguous-binding",
                    "raw_text": table["raw_value"],
                    "reason": "Не установлена однозначная связь с экспериментом и выборкой в тексте.",
                    "source": table["source"], "result_ids": [table["id"]],
                })
            artifact["results"].append(table)
            evidence.update(scoped_sources)
    artifact["evidence"] = list(evidence.values())
