"""Publish aggressive table drafts from frozen PDF grids using the existing assembler."""
import argparse
from collections import defaultdict
import csv
from decimal import Decimal
import json
from pathlib import Path
import re
import sys

from entities import (
    COMPACT_RATE, COMPOUND, DELTA, DESCRIPTIVE, K, MODEL, MODEL_FAMILY, SCALAR, SHORT_MODEL, VERSION,
    caption_indicators, clean, entity_key, evaluation_header_metric, explicit_unit, known_metric, metric_key,
    passport, sources_of_lexicon, token_in,
)

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from rag_mvp.core.domain import Source, TableTarget
from rag_mvp.core.measurement_binding import Assertion, Observation, Scope, assemble
from rag_mvp.article_context.passport_metrics import declared_rate_names, ranked_caption_metric, rate_columns
from paired_comparison import comparison_headers, comparison_records


GUESS_RULES = {"header-model-as-generator", "left-column-as-method-variant"}
UNKNOWN_LABELS = {"", "-", "—", "n/a", "na", "mean", "average", "std", "median", "total", "none"}
RULE = re.compile(r"\b(?:standard deviation|sample SD|mean\s*\(SD\)|mean\s*±\s*SD)\b", re.I)
TEXT_QA = re.compile(r"\b(?:(multi-hop|single-hop)\s+)?(?:question answering|QA)\b", re.I)
METHOD_HEADER = re.compile(r"^(?:method|approach|system|retriever|framework|variant|configuration|strategy)s?$", re.I)
ANSWER_ACCURACY = re.compile(
    r"\baccuracy\s*[—–,(]\s*based on whether\b[^.!?;]{0,100}"
    r"\b(?:response|answer)\s+match(?:ed|es)?\s+(?:the )?(?:correct|reference) answer\b", re.I,
)
SCORING_PAIR = re.compile(r"^Accuracy\s*\(%\)\s+and\s+[\w-]+-Score\s*\(%\)", re.I)
POINT_SCORE = re.compile(r"^Overall performance scores? for each method out of [\d,]+ points\b", re.I)
SCORE_CORRELATION = re.compile(r"^Correlation between LLM scores and (?:human|hu-\s*man) scores\b", re.I)
ORDERED_LIST = r"[A-Za-z][\w.-]*(?:,\s+[A-Za-z][\w.-]*)*?,?\s+and\s+[A-Za-z][\w.-]*"
METRIC_ALIAS_PREFIX = rf"(?:^|[.!?]\s+)(?P<aliases>{ORDERED_LIST})\s+represent\s+"
METRIC_ALIASES_IN_CAPTION = re.compile(
    rf"{METRIC_ALIAS_PREFIX}(?P<names>{ORDERED_LIST}),?\s+respectively\b", re.I,
)
RULE_ACCURACY = re.compile(r"\brule-based accuracy\s*\(Acc\)", re.I)
EXACT_MATCH_SUMMARY = re.compile(
    r"\bexact[- ]match percentages are reported as mean\s*\(sample (?:SD|standard deviation)\)", re.I,
)
METRIC_AXIS = re.compile(r"(?:[\w-]+\s+)*metrics?", re.I)
METRIC_SECTION = re.compile(r"^(?:[\w]+\.)+\d*\s+(?:Metrics Details|Metric Definitions)\b", re.I)
NUMBERED_SECTION = re.compile(r"^(?:[\w]+\.)+\d*\s+[A-Z]")
METRIC_CODE = re.compile(r"\(([A-Z][A-Z0-9]{1,10})\)\s*:")
AVERAGE_CODE = re.compile(r"\(([A-Z][A-Z0-9]{1,10})\s+avg\s*\)")


TABLE_REFERENCE = re.compile(r"\b(?:Table|Tab\.)\s+([A-Za-z]?\d+(?:\.\d+)?[A-Za-z]?)\b", re.I)
SENTENCE_END = re.compile(r"(?<!\bTab\.)(?<=[.!?])\s+(?=[A-Z(])")
TABLE_REPORT = re.compile(r"\s+(?:reports?|shows?|presents?|summari[sz]es?|gives?)\b", re.I)
REPORT_LOCATION = re.compile(r"\bin\s*$", re.I)
CUTOFF_LABEL = re.compile(r"@\s*\d+")
ANCILLARY_HEADER = re.compile(
    r"^(?:[#№]|(?:no\.|number|count) of\b|(?:time|cost|size|length)(?:$|[\s(-]))", re.I,
)
SCOPED_RECALL = re.compile(r"\b(?:the )?recall(?: accuracy)? is measured (?:with|using) Recall\s*@\s*(\d+)\b", re.I)
RECALL_SCORE_COLUMNS = re.compile(r"(?:dense|lexical|sparse|hybrid|best recall|recall)(?:\s+(?:score|retrieval))?", re.I)
DATASET_METRIC_HEADER = re.compile(
    r"([\w][\w .-]{0,38}?)\s*\((acc(?:uracy)?|str[- ]em|em|f1|rouge(?:-l)?|bleu|"
    r"(?:ndcg|recall|precision|hit)\s*@\s*\d+)\)", re.I,
)
CAPTION_ACCURACY_ALIAS = re.compile(r"\b((?:bounding[- ]box|answer|retrieval|document|passage) accuracy)"
                                    r"\s*\(([A-Za-z][A-Za-z0-9-]{1,12})\)", re.I)
WINNING_RATE = re.compile(r"\bwinning rates?\s*\(\s*%\s*\)", re.I)
WINNING_ASPECT = re.compile(r"(?:comprehensiveness|diversity|empowerment|overall|"
                            r"relevance|correctness|faithfulness|coherence)", re.I)
INTEGER_COUNT = re.compile(r"(?:0|[1-9]\d*|[1-9]\d{0,2}(?:,\d{3})+)")
ACCURACY_LEVEL = re.compile(r"L[1-9]\d*", re.I)
REPORTED_RESULTS = re.compile(
    r"\b(?:results?|findings?)\s+(?:(?:are|were|is)\s+(?:(?:reported|given|shown|presented)\s+)?)?in\s*$",
    re.I,
)
BENCHMARK_ACCURACY = re.compile(
    r"\b(?:accuracy\s+as\s+(?:the\s+)?(?:performance|evaluation) metrics?|"
    r"(?:affects?|improves?)\s+(?:the\s+)?accuracy\b)", re.I,
)
DIAGNOSTIC_ACCURACY = re.compile(r"\bdiagnostic accuracy\b", re.I)


def reported_accuracy(fragments, table, *, levels=()):
    """Read the metric in a local paragraph that explicitly introduces this table's results."""
    matches = []
    for fragment in fragments:
        if not fragment.get("box") or abs(fragment["page"] - table["page"]) > 1:
            continue
        text = clean(fragment["text"])
        refs = list(TABLE_REFERENCE.finditer(text))
        if len(refs) != 1:
            continue
        ref = refs[0]
        if ref[1].casefold() != table["label"].casefold():
            continue
        if not REPORT_LOCATION.search(text[:ref.start()]):
            continue
        if levels:
            mentions = set(re.findall(r"\bL[1-9]\d*\b", text, re.I))
            if DIAGNOSTIC_ACCURACY.search(text) and (not mentions or set(levels) <= mentions):
                matches.append(fragment)
        elif BENCHMARK_ACCURACY.search(text) and (
            REPORTED_RESULTS.search(text[:ref.start()]) or TABLE_REPORT.match(text[ref.end():])
        ):
            matches.append(fragment)
    return matches[0] if len(matches) == 1 else None


def failure_total_columns(headers):
    registered = {}
    for total in headers:
        if total["column_span"] != 1 or clean(total["text"]).casefold() != "total failures":
            continue
        groups = path_for(headers, total)
        if not groups:
            continue
        group = groups[0]
        components = [head for head in headers if head["row"] == total["row"]
                      and head["column_span"] == 1 and group["column"] <= head["column"] < total["column"]]
        labels = {clean(head["text"]).casefold() for head in components}
        if (len(components) >= 2 and len(components) == len(labels)
                and "" not in labels and "total failures" not in labels):
            registered[total["id"]] = group, components
    return registered


def dataset_metric_header(text, entities):
    match = DATASET_METRIC_HEADER.fullmatch(clean(text))
    if not match:
        return None
    dataset, label = match.groups()
    if dataset.casefold() in UNKNOWN_LABELS | {"all", "overall", "score", "dataset"}:
        dataset = None
    if label.casefold().replace(" ", "-") == "str-em":
        return dataset, "STR-EM"
    metric = known_metric(re.sub(r"\s*@\s*", "@", label), entities, header=False)
    return (dataset, metric[0]) if metric else None


def cited_table_metric(fragments, table, metrics):
    """Accept a metric only when a PDF sentence assigns it to this table."""
    label = table["label"].casefold()
    vocabulary = sorted({entry["value"] for entry in metrics.values()})
    patterns = [(value, re.compile(rf"(?<![\w-]){re.escape(value)}(?![\w-]|@)", re.I)) for value in vocabulary]
    evidence = {}
    for fragment in fragments:
        if not fragment.get("box") or fragment["id"] == table["caption"]["id"]:
            continue
        text = clean(fragment["text"])
        if not TABLE_REFERENCE.search(text):
            continue
        for sentence in SENTENCE_END.split(text):
            refs = list(TABLE_REFERENCE.finditer(sentence))
            if len(refs) != 1 or refs[0][1].casefold() != label:
                continue
            ref = refs[0]
            if not (TABLE_REPORT.match(sentence[ref.end():]) or REPORT_LOCATION.search(sentence[:ref.start()])):
                continue
            mentions = [(match.span(), value) for value, pattern in patterns for match in pattern.finditer(sentence)]
            distinct = {metric_key(value): value for (start, end), value in mentions
                        if not any(a <= start and end <= b and (a, b) != (start, end) for (a, b), _ in mentions)}
            if len(distinct) > 1:
                return None
            if distinct:
                key, value = next(iter(distinct.items()))
                evidence.setdefault(key, (value, fragment))
    return next(iter(evidence.values())) if len(evidence) == 1 else None


def load_csv(path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def write_csv(path, fields, rows):
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def numbers(raw, metric, text):
    raw = clean(raw)
    match = SCALAR.fullmatch(raw) or DELTA.fullmatch(raw)
    if match:
        yield metric, match["value"].replace("−", "-").replace(",", ""), match["unit"], "scalar"
        return
    if match := COMPOUND.fullmatch(raw):
        yield metric, match["value"].replace("−", "-").replace(",", ""), match["unit"], "first-number-of-reported-pair"
        if RULE.search(text):
            yield metric + " sample SD", (match["sd"] or match["plus"]).replace("−", "-").replace(",", ""), match["unit"], "explicit-standard-deviation"


def headers_for(cells, entities):
    marked = [cell for cell in cells if cell.get("role") == "column_header"]
    if marked:
        return marked
    by_row = defaultdict(list)
    for cell in cells:
        by_row[cell["row"]].append(cell)
    # Some saved readers leave the role empty. Recover only the leading header row.
    first = by_row[min(by_row)]
    return first if any(known_metric(cell["text"], entities, header=True) for cell in first) else []


def path_for(headers, cell):
    return sorted((header for header in headers
                   if header["row"] + header["row_span"] <= cell["row"]
                   and header["column"] <= cell["column"] < header["column"] + header["column_span"]),
                  key=lambda item: (item["row"], item["column"]), reverse=True)


def named(text, kind, entities, *, anywhere=False):
    if not anywhere:
        return entities[kind].get(entity_key(text))
    found = {}
    for entry in entities[kind].values():
        if len(entry["value"]) > 2 and token_in(entry["value"], text):
            found[entity_key(entry["value"])] = entry
    # Long names shadow their shorter prefixes, such as a model family name.
    distinct = [entry for entry in found.values()
                if not any(entry != other and token_in(entry["value"], other["value"]) for other in found.values())]
    return distinct[0] if len(distinct) == 1 else None


def texts_for_caption(record):
    match = TEXT_QA.search(record)
    if not match:
        return None
    return ((match[1].lower() + " ") if match[1] else "") + "question answering"


def caption_title(caption):
    return re.sub(r"^(?:Table|Tab\.)\s*[A-Za-z0-9.-]+\s*[:.—–-]?\s*", "", clean(caption["text"]), flags=re.I)


def caption_metric_aliases(title, entities):
    definitions = list(METRIC_ALIASES_IN_CAPTION.finditer(title))
    if len(definitions) != 1:
        return {}
    definition, = definitions
    aliases = [entity_key(item) for item in re.split(r",\s*|\band\s+", definition["aliases"], flags=re.I) if item.strip()]
    names = [item.strip() for item in re.split(r",\s*|\band\s+", definition["names"], flags=re.I) if item.strip()]
    if len(aliases) != len(set(aliases)) or len(aliases) != len(names):
        return {}
    metrics = [known_metric(name, entities) for name in names]
    if not all(metrics):
        return {}
    return {alias: metric[0] for alias, metric in zip(aliases, metrics, strict=True)}


def caption_continuation(table, fragments):
    caption = table["caption"]
    boxes = [item["box"] for item in table["evidence"] if item.get("box") and item.get("precision") == "table"]
    if caption_title(caption) or not caption.get("box") or len(boxes) != 1:
        return None
    left, top, _, bottom = caption["box"]
    page = table["page"]
    candidates = [item for item in fragments if item["page"] == page and item.get("box")]
    for marker, following in zip(candidates, candidates[1:]):
        if clean(marker["text"]) != clean(caption["text"]):
            continue
        if abs(marker["box"][0] - left) > .005 or abs(marker["box"][1] - top) > .005:
            continue
        x, y, _, end = following["box"]
        if abs(x - left) <= .015 and 0 <= y - bottom <= .018 and end <= boxes[0][1]:
            if not re.match(r"(?:table|tab\.|figure|fig\.)\s", clean(following["text"]), re.I):
                return following
    return None


def answer_accuracy_sources(text, page, label):
    report = re.compile(rf"\b(?:evaluation )?results (?:are )?(?:shown|reported|presented) in "
                        rf"(?:table|tab\.)\s+{re.escape(label)}(?![\w-]|\.\d)", re.I)
    fragments = [item for item in text if item["page"] == page and item.get("box")]
    for index, reference in enumerate(fragments):
        if not report.search(clean(reference["text"])):
            continue
        nearby = fragments[max(0, index - 3):index + 1]
        definitions = [item for item in nearby if ANSWER_ACCURACY.search(clean(item["text"]))]
        if len(definitions) == 1:
            return tuple(dict.fromkeys((definitions[0]["id"], reference["id"])))
    return ()


def split_scoring_definition(title, text, page):
    if not (SCORING_PAIR.match(title)
            and re.search(r"\bAcc is calculated by the verification method\b", title, re.I)
            and re.search(r"\bScore is (?:the score )?verified by (?:an? )?LLM\b", title, re.I)):
        return None
    definitions = [item for item in text if item.get("box") and item["page"] in {page - 1, page}
                   and RULE_ACCURACY.search(clean(item["text"]))]
    return definitions[0] if len(definitions) == 1 else None


def metric_code_sources(fragments):
    definitions = {}
    in_section = False
    for item in fragments:
        if not item.get("box"):
            continue
        text = clean(item["text"])
        if NUMBERED_SECTION.match(text):
            in_section = bool(METRIC_SECTION.match(text))
        if in_section:
            for code in METRIC_CODE.findall(text):
                definitions.setdefault(code, item)
    averages = {code + "avg": item for item in fragments if item.get("box")
                for code in AVERAGE_CODE.findall(clean(item["text"])) if code in definitions}
    return definitions | averages


def table_records(table, entities, article_text, *, pdf_path=None, pdf_sha=None):
    cells = [{**cell, "id": cell.get("id") or f"cell-{cell['row']}-{cell['column']}"} for cell in table["cells"]]
    evidence = {item["id"]: item for item in table["evidence"] if item.get("box")}
    caption = table["caption"]
    if caption.get("box"):
        evidence[caption["id"]] = caption
    title = caption_title(caption)
    if not cells or DESCRIPTIVE.search(title):
        return
    if pdf_path is not None and caption["id"] in evidence and (codes := declared_rate_names(title)):
        if layout := comparison_headers(cells, title):
            yield from comparison_records(table, cells, evidence, layout, codes, pdf_path, pdf_sha)
            return
    page = table["page"]
    label = table["label"]
    headers = headers_for(cells, entities)
    coverage_header = next((item for item in headers if metric_key(item["text"]) == "coverage"), None)
    rate_header = next((item for item in headers if COMPACT_RATE.fullmatch(clean(item["text"]))), None)
    header_ids = {cell["id"] for cell in headers}
    axes = {cell["column"] + cell["column_span"]: cell for cell in headers
            if cell["column_span"] == 1 and METRIC_AXIS.fullmatch(clean(cell["text"]))}
    codes = metric_code_sources(article_text) if axes else {}
    left_col = min(cell["column"] for cell in cells)
    by_body_row = defaultdict(list)
    for cell in cells:
        if cell["id"] in header_ids:
            continue
        for row in range(cell["row"], cell["row"] + cell["row_span"]):
            by_body_row[row].append(cell)
    caption_metrics = {metric_key(entry["value"]): entry for entry in entities["metrics"].values()
                       if len(entry["value"]) > 2 and token_in(entry["value"], title)}
    explicit_caption = dict(caption_indicators(title))
    for name in explicit_caption:
        caption_metrics[metric_key(name)] = {"value": name, "source": None}
    has_alias_declaration = re.search(METRIC_ALIAS_PREFIX, title, re.I) is not None
    excluded_caption_term = any(len(entry["value"]) > 2 and token_in(entry["value"], title)
                                for entry in entities["non_metrics"].values())
    caption_metric, = caption_metrics.values() if (
        len(caption_metrics) == 1 and not (excluded_caption_term or has_alias_declaration)
    ) else (None,)
    caption_unit = explicit_caption.get(caption_metric["value"]) if caption_metric else None
    metric_aliases = caption_metric_aliases(title, entities) if caption["id"] in evidence else {}
    failure_totals = (failure_total_columns(headers)
                      if caption["id"] in evidence and re.search(r"\bfailures\b", title, re.I) else {})
    levels = {clean(head["text"]).upper() for head in headers if ACCURACY_LEVEL.fullmatch(clean(head["text"]))}
    level_source = reported_accuracy(article_text, table, levels=levels) if len(levels) >= 2 else None
    if caption_metric and caption_metric["value"] == "Wall-clock runtime":
        if unit := re.search(r"\b(?:reported|measured|expressed) in (?:rounded )?(milliseconds?|seconds?|minutes?|hours?)\b", title, re.I):
            caption_unit = unit[1].lower()
    has_header_metric = any(known_metric(item["text"], entities, header=True) for item in headers)
    benchmark_source = (reported_accuracy(article_text, table)
                        if not (has_header_metric or caption_metrics)
                        and re.search(r"\b(?:results?|performance|ablation|evaluation)\b", title, re.I) else None)
    referenced_metric = None
    if not (caption_metrics or excluded_caption_term or has_alias_declaration or has_header_metric) and any(
        CUTOFF_LABEL.fullmatch(clean(item["text"])) or named(item["text"], "datasets", entities)
        for item in headers
    ):
        referenced_metric = cited_table_metric(article_text, table, entities["metrics"])
    continuation = caption_continuation(table, article_text)
    ranked_title = clean(continuation["text"]) if continuation else title
    ranked_score = ranked_caption_metric(ranked_title) if caption["id"] in evidence else None
    if ranked_score and caption_metric:
        base = metric_key(ranked_score[0]).partition("@")[0]
        if metric_key(caption_metric["value"]) != base:
            ranked_score = None
    rates = rate_columns(title, headers) if caption["id"] in evidence else {}
    exact_match_summary = caption["id"] in evidence and EXACT_MATCH_SUMMARY.search(title)
    located_caption = caption["id"] in evidence
    recall = SCOPED_RECALL.search(title) if located_caption else None
    accuracy_aliases = ({code.casefold(): name.capitalize() for name, code in CAPTION_ACCURACY_ALIAS.findall(title)}
                        if located_caption else {})
    # The pair proves that these labels distinguish measurements in the same header.
    if len(accuracy_aliases) < 2:
        accuracy_aliases = {}
    winning = bool(located_caption and WINNING_RATE.search(title) and any(
        item["column"] == left_col and WINNING_ASPECT.fullmatch(clean(item["text"]))
        for items in by_body_row.values() for item in items
    ))
    accuracy_sources = ()
    if caption["id"] in evidence and caption_metric and metric_key(caption_metric["value"]) == "accuracy":
        accuracy_sources = answer_accuracy_sources(article_text, page, label)
        evidence.update({"article:" + item["id"]: item for item in article_text if item["id"] in accuracy_sources})
    scoring_definition = split_scoring_definition(title, article_text, page) if caption["id"] in evidence else None
    caption_dataset = named(title, "datasets", entities, anywhere=True)
    caption_method = named(title, "methods", entities, anywhere=True)
    caption_model = named(title, "models", entities, anywhere=True)
    article_task = next(((task, item) for item in article_text if item.get("box")
                         and (task := texts_for_caption(item["text"]))), None)

    def cell_refs(cell):
        return tuple(ref for ref in cell["evidence_ids"] if ref in evidence)

    for cell in cells:
        row, col = cell["row"], cell["column"]
        refs = cell_refs(cell)
        if cell["id"] in header_ids or col == left_col or not refs:
            continue
        raw = cell["text"]
        if not any(pattern.fullmatch(clean(raw)) for pattern in (SCALAR, COMPOUND, DELTA)):
            continue
        path = [item for item in path_for(headers, cell) if cell_refs(item)]
        labels = sorted((item for item in by_body_row[row] if item["column"] < col and cell_refs(item)),
                        key=lambda item: item["column"])
        claims = {}
        level_variant = None
        target = TableTarget(page, label)
        row_scope = Scope(target, row=row)
        column_scope = Scope(target, column=col)
        table_scope = Scope(target)

        def put(field, value, rule, ids, scope, definition=None):
            if value is None or field in claims or not ids:
                return
            if definition and definition.get("box"):
                ref = "article:" + definition["id"]
                evidence[ref] = definition
                ids = (*ids, ref)
            claims[field] = (rule, Assertion(field, value, scope, tuple(dict.fromkeys(ids))))

        aspect = next((item for item in labels if item["column"] == left_col
                       and WINNING_ASPECT.fullmatch(clean(item["text"]))), None) if winning else None
        if path:
            leaf = path[0]
            leaf_text = clean(leaf["text"]).strip(" ↑↓")
            leaf_refs = cell_refs(leaf)
            if leaf_text.upper() in levels and level_source:
                level_variant = leaf_text.upper()
                put("metric", "Diagnostic accuracy", "table-linked-diagnostic-level",
                    leaf_refs, column_scope, level_source)
            if benchmark_source and named(leaf_text, "datasets", entities):
                put("metric", "Accuracy", "table-linked-benchmark-accuracy",
                    leaf_refs, column_scope, benchmark_source)
            if leaf["id"] in failure_totals and INTEGER_COUNT.fullmatch(clean(raw)):
                group, components = failure_totals[leaf["id"]]
                inputs = [item for item in by_body_row[row] if item["column"] in {h["column"] for h in components}
                          and item["row"] == row and item["column_span"] == 1 and cell_refs(item)
                          and INTEGER_COUNT.fullmatch(clean(item["text"]))]
                context = tuple(ref for head in (leaf, group, *components) for ref in cell_refs(head))
                if (len({item["column"] for item in inputs}) == len(inputs) == len(components)
                        and all(cell_refs(head) for head in (group, *components))
                        and sum(int(clean(item["text"]).replace(",", "")) for item in inputs)
                        == int(clean(raw).replace(",", ""))):
                    put("metric", "Total failures", "sum-verified-failure-count",
                        (*context, caption["id"]), Scope(target, row=row, column=col))
                    put("method", clean(group["text"]), "failure-count-method", cell_refs(group), column_scope)
                    for item in labels:
                        for head in path_for(headers, item):
                            if clean(head["text"]).casefold() == "llm":
                                put("generator", clean(item["text"]), "model-under-test",
                                    (*cell_refs(item), *cell_refs(head)), row_scope)
            if combined := dataset_metric_header(leaf_text, entities):
                dataset, name = combined
                put("metric", name, "header-metric", leaf_refs, column_scope)
                put("dataset", dataset, "header-dataset", leaf_refs, column_scope)
            if name := accuracy_aliases.get(leaf_text.casefold()):
                put("metric", name, "caption-decoded-header-metric", (*leaf_refs, caption["id"]), column_scope)
            if recall and RECALL_SCORE_COLUMNS.fullmatch(leaf_text):
                put("metric", "Recall@" + recall[1], "caption-defined-recall-column",
                    (*leaf_refs, caption["id"]), column_scope)
            if aspect and SCALAR.fullmatch(clean(raw)):
                aspect_refs = (*cell_refs(aspect), *leaf_refs, caption["id"])
                cell_scope = Scope(target, row=row, column=col)
                put("metric", "Winning rate", "caption-defined-winning-rate", aspect_refs, cell_scope)
                put("unit", "%", "caption-winning-rate-unit", (caption["id"],), cell_scope)

        metric_axis = next(((axis, group) for group in path
                            if (axis := axes.get(group["column"]))
                            and axis["row"] == group["row"] and cell_refs(axis)), None)
        if metric_axis and codes:
            axis, group = metric_axis
            row_label = next((item for item in labels if item["column"] == axis["column"]), None)
            if row_label is None:
                continue
            # A reader may attach text from the adjacent dataset cell to the metric label.
            printed = re.search(r"(?:^|\s)([A-Z][A-Z0-9]{1,10}(?:\s?avg)?)\s*[↑↓]?$",
                                clean(row_label["text"]))
            code = printed[1].replace(" ", "") if printed else ""
            if code not in codes:
                continue
            put("metric", code, "declared-row-metric-in-column-block",
                (*cell_refs(row_label), *cell_refs(axis), *cell_refs(group)),
                Scope(target, row=row, column=col), codes[code])

        if scoring_definition and path:
            leaf = path[0]
            name = {"acc": "Rule-based accuracy", "score": "LLM Score"}.get(clean(leaf["text"]).casefold())
            if name:
                metric_refs = (*cell_refs(leaf), caption["id"])
                put("metric", name, "caption-defined-column-score", metric_refs, column_scope,
                    scoring_definition if name == "Rule-based accuracy" else None)
                put("unit", "%", "caption-score-unit", metric_refs, column_scope)

        if path and caption["id"] in evidence:
            leaf = path[0]
            header = clean(leaf["text"])
            metric_refs = (*cell_refs(leaf), caption["id"])
            if metric := metric_aliases.get(entity_key(header)):
                put("metric", metric, "caption-defined-header-alias", metric_refs, column_scope)
            elif header.casefold() == "score" and POINT_SCORE.search(title):
                put("metric", "Overall performance score", "caption-defined-column-score", metric_refs, column_scope)
                put("unit", "points", "caption-score-unit", (caption["id"],), table_scope)
            elif header.casefold() in {"interest", "novelty"} and SCORE_CORRELATION.search(title):
                put("metric", header + " correlation", "caption-defined-column-score", metric_refs, column_scope)

        if path and path[0]["id"] in rates:
            metric, group = rates[path[0]["id"]]
            if cell_refs(group):
                metric_refs = (*cell_refs(path[0]), *cell_refs(group), caption["id"])
                put("metric", metric, "caption-defined-failure-rate-column", metric_refs, column_scope)
                put("unit", "%", "caption-failure-rate-unit", metric_refs, column_scope)

        for area, role, scope in ((path, "header", column_scope), (labels, "row", row_scope)):
            for item in area:
                text = clean(item["text"])
                source_ids = cell_refs(item)
                relevance_refs = ()
                if (role == "header" and text.casefold() == "relevance" and coverage_header and rate_header
                        and coverage_header["row"] == rate_header["row"] == item["row"]):
                    if (coverage_refs := cell_refs(coverage_header)) and (rate_refs := cell_refs(rate_header)):
                        relevance_refs = (*coverage_refs, *rate_refs)
                        put("metric", text, "header-metric", (*source_ids, *relevance_refs), scope)
                if match := known_metric(text, entities, header=role == "header"):
                    metric, definition = match
                    entry = entities["metrics"].get(metric_key(text), {})
                    abbreviation = entry.get("defined_abbreviation")
                    rule = role + "-metric"
                    metric_refs = source_ids
                    if (role == "header" and abbreviation and caption["id"] in evidence
                            and re.fullmatch(rf"{re.escape(abbreviation)}\s*[↑↓]?", text, re.I)
                            and re.search(rf"\b(?:we report|we measure)\s+{re.escape(abbreviation)}\b", title, re.I)):
                        metric, rule = abbreviation, "caption-confirmed-header-abbreviation"
                        metric_refs = (*source_ids, caption["id"])
                    put("metric", metric, rule, metric_refs, scope, definition)
                    put("unit", explicit_unit(text), role + "-unit", source_ids, scope)
                if role == "header" and (relevance_refs or evaluation_header_metric(text)):
                    continue
                if entry := named(text, "datasets", entities):
                    put("dataset", entry["value"], role + "-dataset", source_ids, scope, entry["source"])
                model = named(text, "models", entities)
                bare_model = re.sub(r"\s+\([^()]+\)$", "", text)
                if role == "header" and (model or any(pattern.fullmatch(bare_model)
                                                       for pattern in (MODEL, MODEL_FAMILY, SHORT_MODEL))):
                    put("generator", model["value"] if model else text, "header-model-as-generator",
                        source_ids, scope, model["source"] if model else None)
                if entry := named(text, "methods", entities):
                    put("method", entry["value"], role + "-method", source_ids, scope, entry["source"])
                if role == "row":
                    for head in path_for(headers, item):
                        field_header = clean(head["text"])
                        chunking_method = path and entity_key(path[0]["text"]) in metric_aliases and re.fullmatch(
                            r"Chunking Methods?", field_header, re.I,
                        )
                        if (METHOD_HEADER.fullmatch(field_header) or chunking_method) and text.casefold() not in UNKNOWN_LABELS:
                            put("method", text, "explicit-method-column", (*source_ids, *cell_refs(head)), row_scope)
                            break
        if caption["id"] in evidence:
            caption_refs = (caption["id"],)
            if exact_match_summary and "metric" not in claims:
                put("metric", "EM", "caption-defined-exact-match-summary", caption_refs, table_scope)
                put("unit", "%", "caption-exact-match-percentages", caption_refs, table_scope)
            if ranked_score and "metric" not in claims:
                put("metric", ranked_score[0], "caption-defined-ranked-score", caption_refs, table_scope,
                    continuation)
            # An excluded row or column label does not authorize a caption-wide metric.
            excluded_label = any(metric_key(item["text"]) in entities["non_metrics"] for item in (*path, *labels))
            ancillary = explicit_caption and any(ANCILLARY_HEADER.search(clean(item["text"])) for item in path)
            caption_allowed = not winning and not recall and not accuracy_aliases
            if caption_metric and not excluded_label and not ancillary and caption_allowed:
                if accuracy_sources:
                    put("metric", "Answer accuracy", "caption-metric-with-linked-answer-definition",
                        (*caption_refs, *("article:" + ref for ref in accuracy_sources)), table_scope)
                else:
                    put("metric", caption_metric["value"], "caption-metric", caption_refs, table_scope,
                        caption_metric["source"])
            for field, entry in (("dataset", caption_dataset), ("generator", caption_model)):
                if entry:
                    put(field, entry["value"], "caption-" + field, caption_refs, table_scope, entry["source"])
            if caption_metric and not ancillary and caption_allowed:
                unit = explicit_unit(title)
                if explicit_caption:
                    selected = claims.get("metric")
                    same_metric = selected and metric_key(selected[1].value) == metric_key(caption_metric["value"])
                    unit = caption_unit if same_metric else None
                put("unit", unit, "caption-unit", caption_refs, table_scope)
        if referenced_metric and any(CUTOFF_LABEL.fullmatch(clean(item["text"]))
                                     or named(item["text"], "datasets", entities) for item in path):
            metric, locator = referenced_metric
            ref = "article:" + locator["id"]
            evidence[ref] = locator
            put("metric", metric, "table-reference-metric", (ref,), table_scope)
        if "metric" not in claims:
            continue
        for head in path:
            put("unit", explicit_unit(head["text"]), "header-unit", cell_refs(head), column_scope)
        left = next((item for item in labels if item["column"] == left_col), None)
        if left:
            text = clean(left["text"])
            if (text.casefold() not in UNKNOWN_LABELS and not SCALAR.fullmatch(text)
                    and not known_metric(text, entities, header=False) and not named(text, "datasets", entities)
                    and not named(text, "models", entities) and not MODEL.fullmatch(text)
                    and not MODEL_FAMILY.fullmatch(text)):
                put("method", text, "left-column-as-method-variant", cell_refs(left), row_scope)
        if caption_method and caption["id"] in evidence:
            put("method", caption_method["value"], "caption-method", (caption["id"],), table_scope,
                caption_method["source"])
        metric_claim = claims["metric"][1]
        if marker := K.search(metric_claim.value):
            put("metric_k", int(marker[1]), "metric-cutoff", metric_claim.evidence_ids, metric_claim.scope)
        retrieval = bool(re.match(r"^(?:mrr$|ndcg|hit@|recall@|precision@|map@)", metric_key(metric_claim.value)))
        if retrieval:
            put("task", "information retrieval", "task-from-retrieval-metric", metric_claim.evidence_ids, table_scope)
        elif caption["id"] in evidence:
            put("task", texts_for_caption(title), "caption-task", (caption["id"],), table_scope)
        if "task" not in claims and article_task:
            task, locator = article_task
            ref = "article:" + locator["id"]
            evidence[ref] = locator
            put("task", task, "article-task", (ref,), table_scope)
        source = Source(page, label, row, col, tuple(item["text"] for item in path),
                        tuple(evidence[refs[0]]["box"]), refs)
        for metric, value, raw_unit, numeric_rule in numbers(raw, metric_claim.value, title + " " + " ".join(source.headers)):
            if not Decimal(value).is_finite():
                continue
            final_claims = dict(claims)
            if raw_unit:
                final_claims["unit"] = ("cell-unit", Assertion("unit", raw_unit, row_scope, refs))
            if metric != metric_claim.value:
                final_claims["metric"] = ("explicit-standard-deviation-metric",
                                          Assertion("metric", metric, column_scope, (*metric_claim.evidence_ids, *refs)))
            observation = Observation("retrieval" if retrieval else "qa", value, source,
                                      ("generator", "task", "unit"))
            bound = assemble(observation, (claim for _, claim in final_claims.values()))
            fields = {field: {"value": option.value, "rule": final_claims[field][0],
                              "sources": [evidence[ref] for ref in option.evidence_ids]}
                      for field, option in bound.confirmed.items()}
            if "metric" not in fields:
                continue
            variant = level_variant if metric == "Diagnostic accuracy" else (
                clean(aspect["text"]) if aspect and metric == "Winning rate" else None
            )
            yield {"page": page, "table": label, "row": row, "column": col, "raw": raw, "value": value,
                   "fields": fields, "open": sorted(bound.open_fields),
                   **({"metric_variant": variant} if variant else {}),
                   "numeric_rule": numeric_rule, "number_sources": [evidence[ref] for ref in refs]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", type=Path)
    parser.add_argument("lexicon_gold", type=Path, help="Only metric and dataset names from other articles are used.")
    parser.add_argument("schema", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--pdf-dir", type=Path, help="Original PDFs for geometry of ruled paired comparisons.")
    args = parser.parse_args()
    input_paths = sorted(args.inputs.glob("D*.json"))
    if [path.stem for path in input_paths] != [f"D{i:03}" for i in range(1, 22)]:
        parser.error("Expected frozen inputs D001–D021.")
    args.output.mkdir(parents=True, exist_ok=True)
    schema = json.loads(args.schema.read_bytes())["tables"]
    metric_docs = sources_of_lexicon(load_csv(args.lexicon_gold / "results.csv"), ["metric_name"])
    dataset_docs = sources_of_lexicon(load_csv(args.lexicon_gold / "datasets.csv"), ["dataset_name"])
    bundle = {table: [] for table in schema}
    stats = {}
    with (args.output / "provenance.jsonl").open("w", encoding="utf-8") as provenance:
        for path in input_paths:
            record = json.loads(path.read_bytes())
            doc = record["doc"]
            entities = passport(record, metric_docs, dataset_docs)
            passport_dir = args.output / "passports"
            passport_dir.mkdir(exist_ok=True)
            write_json(passport_dir / f"{doc}.json", {"rules": VERSION, "excluded_gold_article": doc,
                       "sha256": record["sha256"], **entities})
            bundle["publications"].append({"publication_id": doc, "title": doc, "in_scope": "true"})
            indexes = {key: {} for key in ("configurations", "datasets", "tasks", "experiments")}

            def entity(table, prefix, key, values):
                if not any(key):
                    return ""
                if key not in indexes[table]:
                    identifier = f"{prefix}{len(indexes[table]) + 1}"
                    indexes[table][key] = identifier
                    bundle[table].append({"publication_id": doc, **values,
                                          {"C": "config_id", "DS": "dataset_id", "T": "task_id",
                                           "E": "experiment_id"}[prefix]: identifier})
                return indexes[table][key]

            started = len(bundle["results"])
            for table in sorted(record["tables"], key=lambda t: (t["page"], t["label"])):
                for row in table_records(table, entities, record["text"],
                                         pdf_path=args.pdf_dir / f"{doc}.pdf" if args.pdf_dir else None,
                                         pdf_sha=record["sha256"]):
                    fields = {key: field["value"] for key, field in row["fields"].items()}
                    method = fields.get("method", "")
                    model = fields.get("generator", "")
                    dataset = fields.get("dataset", "")
                    task = fields.get("task", "")
                    unit = fields.get("unit", "")
                    rid = f"R{len(bundle['results']) - started + 1}"
                    config = entity("configurations", "C", (method, model), {"rag_variant": method, "generator_model": model})
                    dataset_id = entity("datasets", "DS", (dataset,), {"dataset_name": dataset})
                    task_id = entity("tasks", "T", (task,), {"task_type": task})
                    exp = entity("experiments", "E", (row["page"], row["table"]), {"note": table["caption"]["text"]})
                    bundle["results"].append({
                        "publication_id": doc, "result_id": rid, "config_id": config, "dataset_id": dataset_id,
                        "experiment_id": exp, "task_id": task_id, "metric_name": fields["metric"],
                        "metric_variant": row.get("metric_variant", ""),
                        "metric_k": fields.get("metric_k", ""), "metric_value": row["value"], "metric_unit": unit,
                        "raw_value": row.get("raw_component", row["raw"]),
                        "normalized_value": row["value"], "normalized_unit": unit,
                        "result_origin": "primary", "source_page": row["page"], "source_table": "Table " + row["table"],
                        "source_fragment": row["raw"], "extraction_status": "extracted",
                        "note": f"{VERSION}; cell-{row['row']}-{row['column']}; partial draft",
                    })
                    provenance.write(json.dumps({"publication_id": doc, "result_id": rid, "rules": VERSION,
                                                 "pdf_sha256": record["sha256"], **row}, ensure_ascii=False) + "\n")
                    problems = [(f"open_{field}", f"Поле {field} не подтверждено.") for field in row["open"]]
                    problems += [(state["rule"], f"{field}={state['value']}; " +
                                  json.dumps(state["sources"], ensure_ascii=False))
                                 for field, state in row["fields"].items() if state["rule"] in GUESS_RULES]
                    for rule, text in problems:
                        bundle["issues"].append({"publication_id": doc, "issue_id": f"I{len(bundle['issues']) + 1}",
                                                "record_ids": rid, "check_name": rule, "description": text,
                                                "source_page": row["page"], "source_fragment": row["raw"]})
            stats[doc] = {"tables": len(record["tables"]), "unaddressed_tables": len(record["unresolved"]),
                          "reused_tables": sum("reused_from" in item for item in record["tables"]),
                          "rows": len(bundle["results"]) - started, "reader_status": record["status"],
                          "seconds": record["seconds"], "text_reused_from": record["text_reused_from"]}
            print(doc, stats[doc], flush=True)
    for table, rows in bundle.items():
        write_csv(args.output / f"{table}.csv", schema[table], rows)
    write_json(args.output / "manifest.json", {"rules": VERSION, "articles": stats})


if __name__ == "__main__":
    main()
