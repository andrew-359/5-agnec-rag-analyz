"""Bind explicit conditions from the source article to one read table."""

import re
from dataclasses import asdict, replace

from .article_context import ArticleFragments
from .article_context.assertions import ConditionAssertion as ArticleAssertion
from .article_context.assertions import ConditionField as ArticleConditionField
from .article_context.assertions import RetrievedAnswerAssertion, extract_condition_assertions, retriever_in_variant
from .article_context.captions import text_for_matching
from .article_context.context import Basis, comparison_datasets
from .article_context.selection import CONTEXT_ARTIFACT, PreparedTableContext, prepare_table_context
from .core.domain import NUMBER, Box, Collection, ConditionAssertion, ConditionField, Remark, RowGroup, Source, TableTarget
from .run import ArticleContext, Evidence, TableRead
from .table_region import TableRegion


BASELINE_GROUPS = re.compile(r"\bbaselines\s+(?:categorized|grouped)\s+into\s+[^:.;]+\s+groups\s*:", re.I)


def listed_groups(declaration: str) -> list[str]:
    groups = []
    start = depth = 0
    for match in re.finditer(r"[(),;]|[.!?](?=\s|$)", declaration):
        symbol = match.group()
        if symbol == "(":
            depth += 1
        elif symbol == ")":
            depth -= 1
        elif depth == 0:
            groups.append(declaration[start:match.start()].strip())
            start = match.end()
            if symbol != ",":
                return groups
    if depth == 0:
        groups.append(declaration[start:].strip())
    return groups


def mark_baseline_rows(read: TableRead, sources: tuple[Evidence, ...]) -> tuple[TableRead, tuple[Evidence, ...]]:
    if read.table is None or read.grid is None:
        return read, ()
    declarations = [(fragment, match.string[match.end():]) for fragment in sources
                    if (match := BASELINE_GROUPS.search(text_for_matching(fragment.text)))]
    type_headers = [cell for cell in read.grid.cells
                    if cell.role == "column_header" and cell.column_span == 1 and cell.text.strip().casefold() == "type"]
    if len(declarations) != 1 or len(type_headers) != 1:
        return read, ()

    fragment, declaration = declarations[0]
    declared_groups = listed_groups(declaration)
    groups = [cell for cell in read.grid.cells if cell.role == "row_header" and cell.column == type_headers[0].column]
    baseline_rows: dict[int, tuple[str, ...]] = {}
    for group in groups:
        label = re.escape(text_for_matching(group.text))
        if label and any(re.match(rf"(?:and\s+)?{label}\s+(?:baselines?|RAG)\b", text, re.I) for text in declared_groups):
            for row in range(group.row, group.row + group.row_span):
                baseline_rows[row] = (fragment.id, *group.evidence_ids)
    if not baseline_rows:
        return read, ()
    cells = tuple(
        replace(cell, is_baseline=True, baseline_evidence_ids=baseline_rows[cell.row]) if cell.row in baseline_rows else cell
        for cell in read.table.cells
    )
    return replace(read, table=replace(read.table, cells=cells)), (fragment,)


EVALUATED_METHOD = re.compile(r"\bwe evaluate (?P<method>[A-Za-z][\w.-]*) on\b", re.I)
GLOBAL_RETRIEVAL = re.compile(r"\bwe retrieve top[- ](?P<k>[1-9]\d{0,8}) global con-?texts\b", re.I)


def scoped_retrieval(
    fragments: tuple[Evidence, ...], target: TableTarget,
) -> tuple[tuple[ConditionAssertion, ...], tuple[Evidence, ...]]:
    # The evaluation scope and numeric setting may be in different paragraphs
    # in the same setup section. A comparison with a named baseline does not set its top-k.
    scopes = [(match["method"], fragment) for fragment in fragments
              for match in EVALUATED_METHOD.finditer(text_for_matching(fragment.text))]
    if len({method.casefold() for method, _ in scopes}) != 1:
        return (), ()
    method, scope = scopes[0]
    assertions = []
    sources = {scope.id: scope}
    for fragment in fragments:
        text = text_for_matching(fragment.text)
        if not re.match(r"^Implementation Details[.:]", text, re.I):
            continue
        for match in GLOBAL_RETRIEVAL.finditer(text):
            sources[fragment.id] = fragment
            assertions.append(ConditionAssertion(
                "retrieval_top_k", int(match["k"]), method, fragment.id, target,
                () if fragment.id == scope.id else (scope.id,),
            ))
    return tuple(assertions), tuple(sources.values()) if assertions else ()


def condition_field(field: ArticleConditionField) -> ConditionField:
    return "generator" if field == "generator_model" else field


def bind_local_variants(
    assertions: tuple[ArticleAssertion, ...], evidence: tuple[Evidence, ...], basis: tuple[Basis, ...],
    methods: set[str], target: TableTarget,
) -> tuple[tuple[ConditionAssertion, ...], tuple[Evidence, ...]]:
    links = tuple(item.source for item in basis if item.kind in {"section", "results_reference"})
    has_local_scope = any(item.kind == "section" for item in basis) and any(
        item.kind == "results_reference" for item in basis
    )
    quotes = {fragment.id: fragment for fragment in evidence}
    bound = []
    for item in assertions:
        field = condition_field(item.field)
        if isinstance(item, RetrievedAnswerAssertion):
            bound.extend(
                ConditionAssertion(field, item.value, method, item.evidence_id, target)
                for method in sorted(methods) if retriever_in_variant(item.scope, method)
            )
        elif not re.fullmatch(r"In (?:these|this) experiments?", item.scope, re.I):
            bound.append(ConditionAssertion(field, item.value, item.scope, item.evidence_id))
        elif has_local_scope:
            text = text_for_matching(quotes[item.evidence_id].text)
            support = tuple(fragment.id for fragment in links if fragment.id != item.evidence_id)
            bound.extend(
                ConditionAssertion(field, item.value, method, item.evidence_id, target, support)
                for method in sorted(methods) if re.search(rf"\({re.escape(method)}\)", text, re.I)
            )
    used = {ref for item in bound for ref in item.supporting_evidence_ids}
    return tuple(bound), tuple(fragment for fragment in links if fragment.id in used)


def mark_model_group_rows(read: TableRead, definition: Evidence, caption: Evidence) -> TableRead:
    if read.table is None or read.grid is None:
        return read
    names = comparison_datasets(caption.text)
    headers = [cell for cell in read.grid.cells
               if cell.role == "column_header" and cell.row == 0 and cell.column_span == 1]
    model_columns = [cell.column for cell in headers if cell.text.strip().casefold() == "model"]
    method_columns = [cell.column for cell in headers if cell.text.strip().casefold() == "method"]
    if len(model_columns) != 1 or len(method_columns) != 1 or model_columns == method_columns:
        return read
    model_column, = model_columns
    data_start = max(model_column, method_columns[0])
    evidence = {fragment.id: fragment for fragment in read.evidence if fragment.precision == "cell" and fragment.box}
    models = [cell for cell in read.grid.cells if cell.role != "column_header" and cell.column == model_column
              and cell.column_span == 1 and cell.text.strip()
              and len(cell.evidence_ids) == 1 and cell.evidence_ids[0] in evidence]
    cells = []
    for cell in read.table.cells:
        if len(cell.headers) != 2 or len(cell.header_evidence_ids) != 2 or cell.column <= data_start:
            cells.append(cell)
            continue
        dataset = cell.headers[1]
        metric_ref, dataset_ref = cell.header_evidence_ids
        groups = [model for model in models if model.row <= cell.row < model.row + model.row_span]
        if len(groups) != 1 or dataset not in names or metric_ref not in evidence or dataset_ref not in evidence:
            cells.append(cell)
            continue
        group, = groups
        cells.append(replace(
            cell, row_group=RowGroup(group.text.strip(), group.evidence_ids[0]),
            verified_condition_sources={
                **cell.verified_condition_sources,
                "dataset": (dataset_ref, definition.id, caption.id),
                "metric": (metric_ref,),
                "experiment": (caption.id,),
            },
        ))
    return replace(read, table=replace(read.table, cells=tuple(cells)))


def bind_article_context(document: ArticleFragments, sha256: str, read: TableRead,
                         *, region: TableRegion | None = None) -> tuple[TableRead, ArticleContext | None]:
    selection = prepare_table_context(document, sha256, read, region=region)
    return apply_table_context(read, selection)


def apply_table_context(
    read: TableRead, selection: PreparedTableContext | None,
) -> tuple[TableRead, ArticleContext | None]:
    table = read.table
    if selection is None or table is None:
        return read, None
    context = selection.context
    read = replace(read, raw_artifacts={**read.raw_artifacts, CONTEXT_ARTIFACT: selection.artifact()})
    if selection.engine_version is None:
        return read, None
    read = replace(read, versions={**read.versions, "pdfplumber": selection.engine_version})
    benchmark = extract_condition_assertions(context.sha256, selection.benchmark_evidence)
    methods = {cell.method for cell in table.cells if cell.method}
    benchmark_assertions, _ = bind_local_variants(
        tuple(item for item in benchmark.assertions if isinstance(item, RetrievedAnswerAssertion)),
        benchmark.evidence, (), methods, selection.target,
    )
    if context.status != "supported" or len(context.candidates) != 1:
        remark = Remark(context.reason, "", Source(table.page, table.number))
        possible_fragments = {fragment.id: fragment for fragment in selection.benchmark_evidence}
        possible_fragments.update((fragment.id, fragment) for candidate in context.candidates
                                  for fragment in candidate.fragments)
        return replace(read, remarks=(*read.remarks, remark)), ArticleContext(
            context.sha256, benchmark_assertions, tuple(possible_fragments.values()),
        )

    candidate = context.candidates[0]
    extracted = extract_condition_assertions(context.sha256, candidate.fragments)
    assertions, supporting_evidence = bind_local_variants(
        extracted.assertions, extracted.evidence, candidate.basis,
        methods, selection.target,
    )
    read, baseline_evidence = mark_baseline_rows(read, selection.setup_fragments)
    retrieval_assertions, retrieval_evidence = scoped_retrieval(selection.setup_fragments, selection.target)
    fixed_assertions = tuple(ConditionAssertion(
        "retrieval_top_k", item.depth, item.scope, item.definition.id, selection.target,
        (item.declaration.id, *(row.id for row in item.rows)),
    ) for item in selection.fixed_depths)
    fixed_evidence = tuple(source for item in selection.fixed_depths
                           for source in (item.definition, item.declaration, *item.rows))
    evidence = {fragment.id: fragment for fragment in (
        *extracted.evidence, *supporting_evidence, *baseline_evidence, *retrieval_evidence, *fixed_evidence,
        *selection.benchmark_evidence,
    )}
    definitions = [item.source for item in candidate.basis if item.kind == "dataset_definition"]
    captions = [item.source for item in candidate.basis if item.kind == "caption"]
    if any(item.kind in {"metric_definition", "latency_reference"} for item in candidate.basis):
        evidence.update((fragment.id, fragment) for fragment in candidate.fragments)
    if len(definitions) == len(captions) == 1:
        definition, = definitions
        caption, = captions
        read = mark_model_group_rows(read, definition, caption)
        evidence.update((fragment.id, fragment) for fragment in (definition, caption))
    return read, ArticleContext(
        extracted.sha256, tuple(dict.fromkeys((*assertions, *benchmark_assertions, *retrieval_assertions, *fixed_assertions))), tuple(evidence.values()),
    )


READABLE_NUMBER = re.compile(rf"\s*{NUMBER}\s*%?(?:\s*\(\s*±?\s*{NUMBER}\s*\))?\s*")


def continue_article_context(
    selection: PreparedTableContext, read: TableRead, context: ArticleContext,
    collected: Collection, result_crop: Box | None,
) -> tuple[TableRead, Collection, dict | None]:
    def open_numbers(collection: Collection) -> list[Remark]:
        defective_cells = {(item.source.row, item.source.column) for item in collection.remarks
                           if item.kind == "reading-defect" and item.source.row is not None}
        return [item for item in collection.remarks
                if item.kind == "ambiguous-binding" and READABLE_NUMBER.fullmatch(item.raw_text)
                and (item.source.row, item.source.column) not in defective_cells]

    opened = open_numbers(collected)
    if not opened:
        return read, collected, None
    requested = {(item.field, method.casefold()) for item in opened
                 if isinstance(method := item.conditions.get("method"), str) and item.field}
    fragments = ()
    assertions = []
    evidence_ids = set()
    if requested and read.table is not None and selection.context.status == "supported":
        fragments = selection.followup_fragments
        extracted = extract_condition_assertions(selection.context.sha256, fragments)
        for item in extracted.assertions:
            field = condition_field(item.field)
            if (field, item.scope.casefold()) in requested:
                assertions.append(ConditionAssertion(
                    field, item.value, item.scope, item.evidence_id,
                    TableTarget(read.table.page, read.table.number),
                ))
        if assertions:
            evidence_ids = {item.evidence_id for item in assertions}
            existing = {item.id for item in read.evidence}
            evidence = tuple(item for item in extracted.evidence if item.id in evidence_ids and item.id not in existing)
            read = read.with_article_evidence(evidence)
            collected = read.collect((*context.assertions, *assertions), result_crop=result_crop)
    remaining = {(item.source.row, item.source.column, item.field) for item in open_numbers(collected)}
    action = {
        "sha256": selection.context.sha256,
        "reason": "У читаемых чисел осталось открытое условие эксперимента.",
        "checked_fragments": len(fragments),
        "opened": len(opened),
        "remaining": sum((item.source.row, item.source.column, item.field) in remaining for item in opened),
    }
    read = replace(read, raw_artifacts={
        **read.raw_artifacts,
        "article-context-followup.json": {
            **action,
            "scope": selection.context.reason,
            "requested": sorted(requested),
            "assertions": [asdict(item) for item in assertions],
            "evidence": [asdict(item) for item in read.evidence if item.id in evidence_ids],
        },
    })
    return read, collected, action
