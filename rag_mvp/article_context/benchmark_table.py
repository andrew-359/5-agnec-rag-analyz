"""Bind benchmark measurements to the cell, its axes and the article's dataset description."""

from dataclasses import replace
from decimal import Decimal
import re
import unicodedata
from typing import Iterable

from ..core.domain import (
    Collection, ConditionAssertion, ConditionOption, Evidence, GridCell, NUMBER, RETRIEVAL_METRIC,
    ReadTable, Remark, Result, Source, TableTarget, match_condition, scope_rank,
)
from ..core.measurement_binding import Assertion, Observation, Scope, assemble
from .assertions import RETRIEVED_ANSWER_MODEL, retriever_in_variant
from .caption_accuracy import accuracy_claims, accuracy_fragments, caption_evaluation, repeated_generator_captions
from .caption_dataset import caption_dataset_names, dataset_claim as scoped_dataset_claim, dataset_definitions
from .captions import table_caption_line, text_for_matching
from .factor_setting import ablation_assertions, ablation_dataset, ablation_method_mentions, factor_setting_axes

ANSWER_METRICS = {"f1": "F1", "em": "EM", "acc": "Accuracy", "accuracy": "Accuracy", "autoais": "AutoAIS"}
DATASET_DECLARATION = re.compile(r"\b(?:datasets?|benchmarks?)\b", re.I)
HOP_DATASETS = re.compile(r"\b(single|multi)-hop(?:\s+(?:QA|question answering))?\s*\(([^()]+)\)", re.I)
RETRIEVAL_DATASET = re.compile(r"\(([\w-]+)\)\s*$|\b(?:for|on)\s+([\w-]+)\s*$", re.I)
ROW_DEPTH = re.compile(r"\bk\s*=\s*([1-9]\d{0,5})\s+for\s+([^,.;]+)", re.I)
QUESTION_ANSWERING = re.compile(r"\bquestion answering\b", re.I)
BEAM_CAPTION = re.compile(r":\s*Effect of Beam size on (?P<dataset>\S+) with (?P<model>\S+)\.", re.I)
METHOD_ROW_HEADERS = {"method", "methods", "retriever", "retrievers", "sample strategies"}


def metric_text(text: str) -> str:
    return unicodedata.normalize("NFKC", text).strip()


def retrieval_dataset(caption: str) -> str | None:
    title = table_caption_line(text_for_matching(caption))
    if title and re.search(r"\bretriev\w*\b", title["tail"], re.I):
        # The dataset is in the opening description, before the caption's commentary.
        match = RETRIEVAL_DATASET.search(title["tail"].lstrip(": .—–-").split(".", 1)[0].rstrip())
        if match:
            return match[1] or match[2]
    return None


def dataset_description(label: str, caption: str, text: str) -> str | None:
    if not label:
        return None
    for hop, names in HOP_DATASETS.findall(text):
        if label.casefold() in {name.strip().casefold() for name in names.split(",")}:
            return f"{hop.lower()}-hop question answering"
    # Match an explicitly declared benchmark token, including numbered variants.
    token = rf"(?<![\w-]){re.escape(label)}(?:-[0-9][\w-]*)?(?![\w-])"
    if not any(DATASET_DECLARATION.search(sentence) and re.search(token, sentence, re.I)
               for sentence in re.split(r"[.!?]\s+", text)):
        return None
    if retrieval_dataset(caption) == label:
        return "information retrieval"
    if QUESTION_ANSWERING.search(caption):
        return "question answering"
    return None


def beam_experiment(table: ReadTable, grid: tuple[GridCell, ...]) -> re.Match[str] | None:
    title = table_caption_line(text_for_matching(table.caption))
    if title and title["label"] == str(table.number) and any(
        cell.role == "column_header" and cell.text.strip().casefold() == "beam" for cell in grid
    ):
        return BEAM_CAPTION.fullmatch(title["tail"].strip())
    return None


def beam_role_fragments(table: ReadTable, model: str, fragments: tuple[Evidence, ...]) -> tuple[Evidence, ...]:
    reference = re.compile(rf"^Effect of Beam size Table {table.number} shows\b", re.I)
    model_name = re.compile(rf"\btuned versions of {re.escape(model)}\b", re.I)
    mentions = []
    setups = []
    for fragment in fragments:
        if fragment.page > table.page or fragment.precision != "text_fragment" or not fragment.box:
            continue
        text = text_for_matching(fragment.text)
        if reference.search(text):
            mentions.append(fragment)
        if model_name.search(text) and all(phrase in text.casefold() for phrase in (
            "unconstrained sequences or thoughts", "with constrained decoding", "we ablate these choices",
        )):
            setups.append(fragment)
    return (*mentions, *setups) if mentions and setups else ()


def benchmark_fragments(table: ReadTable, grid: tuple[GridCell, ...],
                        fragments: tuple[Evidence, ...]) -> tuple[Evidence, ...]:
    caption = table.caption
    answerer = any(item.role == "column_header" and item.text.strip().casefold() == "answerer" for item in grid)
    retriever_axis = next((item for item in grid if item.role == "column_header"
                           and item.text.strip().casefold() == "retriever"), None)
    labels = {cell.headers[0] for cell in table.cells if len(cell.headers) == 2
              and metric_text(cell.headers[-1]).casefold() in ANSWER_METRICS
              and (answerer or retriever_axis or QUESTION_ANSWERING.search(caption))}
    retrieval = retrieval_dataset(caption)
    if retrieval and any(len(cell.headers) == 1 and RETRIEVAL_METRIC.fullmatch(metric_text(cell.headers[0]))
                         for cell in table.cells):
        labels.add(retrieval)
    row_values: dict[int, set[str]] = {}
    if labels and retriever_axis and not answerer:
        for item in grid:
            if item.row > retriever_axis.row and item.column > retriever_axis.column and item.role == "data":
                row_values.setdefault(item.row, set()).add(item.text.strip().casefold())
    methods = [(item.text, row_values[item.row]) for item in grid if retriever_axis
               and item.column == retriever_axis.column and item.row in row_values]
    beam = beam_experiment(table, grid)
    matches = list(accuracy_fragments(table, fragments))
    if not retrieval and any(cell.headers and RETRIEVAL_METRIC.fullmatch(metric_text(cell.headers[-1]))
                             for cell in table.cells):
        matches.extend(source for _, source in dataset_definitions(
            caption_dataset_names(caption, table.number), fragments,
        ).values())
    if beam:
        matches.extend(beam_role_fragments(table, beam["model"], fragments))
    if factor_setting_axes(grid) and ablation_dataset(table):
        matches.extend(fragment for fragment, _ in ablation_method_mentions(table, fragments))
    for fragment in fragments:
        if fragment.precision != "text_fragment" or not fragment.box or fragment.page > table.page:
            continue
        content = text_for_matching(fragment.text)
        if any(dataset_description(label, caption, content) for label in labels) or (
            methods and any(retriever_in_variant(match["scope"], method) and match["model"].casefold() in values
                            for match in RETRIEVED_ANSWER_MODEL.finditer(content) for method, values in methods)
        ):
            matches.append(fragment)
    return tuple(dict.fromkeys(matches))


def benchmark_descriptions(label: str, caption: str, context: tuple[Evidence, ...]) -> dict[str, list[str]]:
    descriptions: dict[str, list[str]] = {}
    for fragment in context:
        if task := dataset_description(label, caption, text_for_matching(fragment.text)):
            descriptions.setdefault(task, []).append(fragment.id)
    return descriptions


def retrieval_assertions(
    observation: Observation, metric: str, metric_refs: tuple[str, ...],
    method: ConditionOption | None = None, dataset: ConditionOption | None = None,
    generator: ConditionOption | None = None, depths: tuple[ConditionOption, ...] = (),
) -> Iterable[Assertion]:
    source = observation.source
    target = TableTarget(source.page, source.table)
    column = Scope(target, column=source.column)
    row = Scope(target, row=source.row)
    yield Assertion("metric", metric, column, metric_refs)
    if match := RETRIEVAL_METRIC.fullmatch(metric):
        yield Assertion("metric_k", int(match["k"]), column, metric_refs)
    if dataset:
        yield Assertion("dataset", dataset.value, Scope(target), dataset.evidence_ids)
    if method:
        yield Assertion("method", method.value, row, method.evidence_ids)
    if generator:
        yield Assertion("generator", generator.value, row, generator.evidence_ids)
    for depth in depths:
        yield Assertion("retrieval_top_k", depth.value, row, depth.evidence_ids)


def bind_measurement(
    observation: Observation, assertions: Iterable[Assertion], caption: Evidence | None,
    task: str, task_refs: tuple[str, ...], *, row_label: tuple[str, ConditionOption] | None = None,
) -> Collection:
    binding = assemble(observation, assertions)
    values = {field: option.value for field, option in binding.confirmed.items()}
    sources = {field: option.evidence_ids for field, option in binding.confirmed.items()}
    if caption:
        sources["experiment"] = (caption.id,)
    if task_refs:
        sources["task"] = task_refs

    if binding.open_fields:
        if row_label:
            header, label = row_label
            if header in sources or header in binding.open_fields:
                header = f"{header} (подпись строки)"
            values[header] = label.value
            sources[header] = label.evidence_ids
        measurement = "результата поиска" if observation.kind == "retrieval" else "метрики"
        return Collection((), tuple(
            Remark(
                f"Не удалось однозначно установить поле «{field}» для {measurement}.",
                observation.raw_value, observation.source, field, values, sources, options,
                kind="ambiguous-binding",
            )
            for field, options in binding.open_fields.items()
        ))

    return Collection((Result(
        method=str(values["method"]), generator=str(values.get("generator", "")),
        dataset=str(values["dataset"]), metric=str(values["metric"]), task=task,
        experiment=caption.text if caption else "", protocol=None, raw_value=observation.raw_value,
        value=str(Decimal(observation.raw_value)), unit=values.get("unit"), statistic=None,
        retrieval_top_k=values.get("retrieval_top_k"), metric_k=values.get("metric_k"),
        source=observation.source, condition_sources=sources,
        parameters={"beam": values["beam"]} if "beam" in values else {},
        factor=values.get("factor"), setting=values.get("setting"),
        setting_default=True if values.get("setting_default") == "default" else None,
        source_language=values.get("source_language"),
    ),), ())


def recover_benchmark_measurements(
    table: ReadTable, grid: tuple[GridCell, ...], evidence: tuple[Evidence, ...],
    context: tuple[Evidence, ...], collected: Collection, reader_remarks: tuple[Remark, ...],
    assertions: tuple[ConditionAssertion, ...], *, retrieval_only: bool = False,
) -> Collection:
    if not grid:
        return collected
    beam = beam_experiment(table, grid) if not retrieval_only else None
    evaluation = caption_evaluation(table.caption, table.number) if not retrieval_only else None
    generator_peers = repeated_generator_captions(table, context) if evaluation and evaluation.generator else ()
    if not retrieval_only and not (beam or context or evaluation):
        return collected
    legacy_dataset = retrieval_dataset(table.caption)
    named_datasets = caption_dataset_names(table.caption, table.number) if retrieval_only and not legacy_dataset else ()
    cells = tuple(
        cell for cell in table.cells
        if cell.headers and (len(cell.headers) == 1 or len(named_datasets) > 1)
        and RETRIEVAL_METRIC.fullmatch(metric_text(cell.headers[-1]))
    ) if retrieval_only else tuple(cell for cell in table.cells if len(cell.headers) == (1 if beam else 2))
    if not cells:
        return collected
    positions = {(item.row, item.column): item for item in grid}
    if len(positions) != len(grid):
        return collected
    sources = {item.id: item for item in evidence if item.box and item.page == table.page}
    caption = sources.get(table.caption_evidence_id)
    if (not caption or caption.precision != "text_fragment" or caption.text != table.caption
            or not table_caption_line(text_for_matching(caption.text))):
        caption = None
    if not caption and not retrieval_only:
        return collected

    def cell_evidence(item: GridCell | None) -> Evidence | None:
        if not item or len(item.evidence_ids) != 1:
            return None
        source = sources.get(item.evidence_ids[0])
        if source and source.precision == "cell" and source.text == item.text:
            return source
        return None

    headers = [item for item in grid if item.role == "column_header" and cell_evidence(item)]
    method_labels = {"beam"} if beam else {"method", "retriever"}
    method_axes = [item for item in headers if item.text.strip().casefold() in method_labels]
    answer_axes = [item for item in headers if item.text.strip().casefold() == "answerer"]
    if len(method_axes) > 1 or len(answer_axes) > 1:
        return collected
    method_axis = next(iter(method_axes), None)
    answer_axis = next(iter(answer_axes), None)
    needs_prose_generator = (
        answer_axis is None and method_axis is not None and method_axis.text.strip().casefold() == "retriever"
    )
    by_header_id = {item.evidence_ids[0]: item for item in headers}
    caption_dataset = legacy_dataset if caption else None
    named_definitions = dataset_definitions(named_datasets, context) if caption else {}
    beam_role = beam_role_fragments(table, beam["model"], context) if beam else ()
    ablation = ablation_dataset(table) if retrieval_only and caption else None
    ablation_axes = factor_setting_axes(headers) if ablation else None
    ablation_claims = tuple(ablation_assertions(
        table, ablation_axes, (cell for cell in grid if cell_evidence(cell)), context, caption, ablation,
    )) if ablation_axes else ()
    occupied = {(item.source.row, item.source.column) for item in collected.results}
    occupied.update((item.source.row, item.source.column) for item in (*reader_remarks, *collected.remarks)
                    if item.kind == "reading-defect")
    depth_by_variant: dict[str, set[int]] = {}
    printed_variants = {
        item.text.strip().casefold() for item in grid
        if (method_axis and item.column == method_axis.column and item.row > method_axis.row)
        or (method_axis is None and item.role == "row_header")
    }
    caption_depths = ROW_DEPTH.findall(text_for_matching(caption.text)) if caption else ()
    for depth, variant in caption_depths:
        name = variant.strip().casefold()
        # Resolve a prose plural only when the grid lacks that exact variant.
        scope = name if name in printed_variants else name.removesuffix("s")
        depth_by_variant.setdefault(scope, set()).add(int(depth))
    row_labels: dict[int, list[GridCell]] = {}
    for item in grid:
        if item.role == "row_header" and item.row_span == item.column_span == 1 and item.text.strip():
            row_labels.setdefault(item.row, []).append(item)

    # Resolve each header independently. In a mixed table the same score column
    # name may occur under multiple datasets with different tasks.
    descriptions: dict[str, dict[str, list[str]]] = {}
    results = []
    binding_remarks = []
    for cell in cells:
        position = (cell.row, cell.column)
        if position in occupied or not cell.box or not re.fullmatch(NUMBER, cell.text):
            continue
        path = tuple(by_header_id.get(ref) for ref in cell.header_evidence_ids)
        if not path or any(item is None for item in path) or tuple(item.text for item in path) != cell.headers:
            continue
        if any(not (item.row < cell.row and item.column <= cell.column < item.column + item.column_span)
               for item in path):
            continue
        metric = metric_text(cell.headers[-1])
        value_cell = positions.get(position)
        number = cell_evidence(value_cell)
        if (not number or value_cell.role != "data" or value_cell.column_span != 1 or value_cell.row_span != 1
                or number.box != cell.box or number.text != cell.text):
            continue
        source = Source(table.page, table.number, *position, cell.headers, cell.box, (number.id,))
        if ablation_axes and ablation_axes[1].column < cell.column:
            observation = Observation("retrieval", number.text, source, ("factor", "setting"))
            metric_claims = retrieval_assertions(observation, metric, path[-1].evidence_ids)
            bound = bind_measurement(
                observation, (*ablation_claims, *metric_claims), caption, "information retrieval",
                path[-1].evidence_ids,
            )
            results.extend(bound.results)
            binding_remarks.extend(bound.remarks)
            continue

        row_label = None
        method_cell = positions.get((cell.row, method_axis.column)) if method_axis else None
        if method_axis is None:
            labels = row_labels.get(cell.row, ())
            if len(labels) == 1 and labels[0].column < cell.column and cell_evidence(labels[0]):
                label, = labels
                label_headers = [
                    header for header in headers
                    if header.row + header.row_span <= label.row
                    and header.column <= label.column < header.column + header.column_span
                ]
                if len(label_headers) == 1:
                    header, = label_headers
                    if header.text.strip().casefold() in METHOD_ROW_HEADERS:
                        method_cell = label
                    else:
                        row_label = (
                            header.text, ConditionOption(label.text, label.evidence_ids + header.evidence_ids),
                        )
        method = cell_evidence(method_cell)
        if (not method or method_cell.row_span != 1 or method_cell.column_span != 1
                or method_cell.column >= cell.column or method.text.strip() in {"", "-", "—", "–"}):
            method = None

        if retrieval_only:
            observation = Observation("retrieval", number.text, source)
            dataset = caption_dataset
            if dataset and caption:
                if dataset not in descriptions:
                    descriptions[dataset] = benchmark_descriptions(dataset, caption.text, context)
                tasks = descriptions[dataset]
                task_refs = tuple(tasks["information retrieval"]) if set(tasks) == {"information retrieval"} else ()
            else:
                task_refs = ()
            legacy_claim = ConditionOption(dataset, tuple(dict.fromkeys((caption.id, *task_refs)))) if task_refs else None
            named_claim = scoped_dataset_claim(observation, caption, named_datasets, named_definitions, path) if caption else None
            method_claim = ConditionOption(method.text, (method.id, *method_axis.evidence_ids) if method_axis
                                           else (method.id,)) if method else None
            generator_claim = None
            if answer_axis and answer_axis.column < cell.column:
                candidate = positions.get((cell.row, answer_axis.column))
                generator = cell_evidence(candidate)
                if generator and candidate.row_span == candidate.column_span == 1 and generator.text.strip() not in {"", "-", "—", "–"}:
                    generator_claim = ConditionOption(generator.text, (generator.id, *answer_axis.evidence_ids))

            depths = ()
            if method:
                printed = sorted(depth_by_variant.get(method.text.casefold().strip(), ()))
                if len(printed) > 1:
                    depths = tuple(ConditionOption(depth, (caption.id, method.id)) for depth in printed)
                else:
                    explicit = ConditionOption(printed[0], (caption.id, method.id)) if printed else None
                    match, options = match_condition(assertions, "retrieval_top_k", replace(cell, method=method.text), explicit)
                    depths = (match,) if match else options
            claims = tuple(retrieval_assertions(observation, metric, path[-1].evidence_ids,
                                                method_claim, legacy_claim, generator_claim, depths))
            if named_claim:
                claims = (*claims, named_claim)
            task_sources = tuple(dict.fromkeys((caption.id, *task_refs))) if caption else path[-1].evidence_ids
            bound = bind_measurement(
                observation, claims, caption, "information retrieval", task_sources, row_label=row_label,
            )
            results.extend(bound.results)
            binding_remarks.extend(bound.remarks)
            continue

        if evaluation and caption and len(path) == 2:
            if evaluation.model_datasets and row_label and row_label[0].strip().casefold() == "evidence":
                label = row_label[1]
                method_claim = ConditionOption(metric_text(str(label.value)), label.evidence_ids)
            else:
                method_claim = ConditionOption(method.text, (method.id, *method_axis.evidence_ids)
                                               if method_axis else (method.id,)) if method else None
            required = ("generator",) if evaluation.model_datasets else ("generator", "source_language")
            observation = Observation("qa", number.text, source, required)
            claims = accuracy_claims(observation, evaluation, caption, path, method_claim, context,
                                     generator_peers=generator_peers)
            bound = bind_measurement(observation, claims, caption, "question answering", (caption.id,),
                                     row_label=row_label if not method_claim else None)
            results.extend(bound.results)
            for remark in bound.remarks:
                if remark.field == "generator" and generator_peers:
                    remark = replace(
                        remark, reason="Генератор открыт: та же подпись модели относится к нескольким таблицам.",
                        condition_sources={**remark.condition_sources,
                                           "generator_caption": (caption.id, *(f.id for f in generator_peers))},
                    )
                binding_remarks.append(remark)
            continue

        if beam:
            metric = ANSWER_METRICS.get(metric.casefold())
            if not metric or not method or not method_axis or not method.text.strip().isdecimal():
                continue
            observation = Observation("qa", number.text, source, ("generator", "beam"))
            target = TableTarget(table.page, table.number)
            row = Scope(target, row=cell.row)
            beam_size = int(method.text.strip())
            method_refs = (method.id, *method_axis.evidence_ids)
            claims = [
                Assertion("method", f"{method_axis.text} = {beam_size}", row, method_refs),
                Assertion("beam", beam_size, row, method_refs),
                Assertion("dataset", beam["dataset"], Scope(target), (caption.id,)),
                Assertion("metric", metric, Scope(target, column=cell.column), path[-1].evidence_ids),
            ]
            if beam_role:
                claims.append(Assertion(
                    "generator", beam["model"], Scope(target), (caption.id, *(fragment.id for fragment in beam_role)),
                ))
            dataset = beam["dataset"]
            if dataset not in descriptions:
                descriptions[dataset] = benchmark_descriptions(dataset, caption.text, context)
            tasks = descriptions[dataset]
            task, refs = next(iter(tasks.items())) if len(tasks) == 1 else ("question answering", [])
            task_sources = tuple(refs) if refs else tuple(fragment.id for fragment in beam_role)
            bound = bind_measurement(observation, claims, caption, task, task_sources)
            results.extend(bound.results)
            binding_remarks.extend(bound.remarks)
            continue

        if not method or not (answer_axis or needs_prose_generator or QUESTION_ANSWERING.search(caption.text)):
            continue
        metric = ANSWER_METRICS.get(metric.casefold())
        if not metric:
            continue
        dataset, dataset_refs = cell.headers[0], path[0].evidence_ids
        if dataset not in descriptions:
            descriptions[dataset] = benchmark_descriptions(dataset, caption.text, context)
        if len(descriptions[dataset]) != 1:
            continue
        (task, task_refs), = descriptions[dataset].items()
        if task == "information retrieval":
            continue
        generator = None
        if answer_axis:
            generator_cell = positions.get((cell.row, answer_axis.column))
            generator = cell_evidence(generator_cell)
            if (not generator or generator_cell.row_span != 1 or generator_cell.column_span != 1
                    or answer_axis.column >= cell.column or generator.text.strip() in {"", "-", "—", "–"}):
                continue
        depths = depth_by_variant.get(method.text.casefold().strip(), ())
        if len(depths) > 1:
            continue
        depth = next(iter(depths), None)
        target = TableTarget(table.page, table.number)
        row = Scope(target, row=cell.row)
        column = Scope(target, column=cell.column)
        method_refs = (method.id, *method_axis.evidence_ids) if method_axis else (method.id,)
        claims = [
            Assertion("method", method.text, row, method_refs),
            Assertion("dataset", dataset, column, tuple(dict.fromkeys((*dataset_refs, *task_refs)))),
            Assertion("metric", metric, column, path[-1].evidence_ids),
        ]
        scope = replace(cell, method=method.text)
        generator_value = ConditionOption(generator.text, (generator.id, *answer_axis.evidence_ids)) if generator else None
        depth_value = ConditionOption(depth, (caption.id, method.id)) if depth is not None else None
        generator_match, generator_options = match_condition(assertions, "generator", scope, generator_value)
        depth_match, depth_options = match_condition(assertions, "retrieval_top_k", scope, depth_value)
        if needs_prose_generator and generator_match:
            matching_cells = [
                fragment.id for column_index in range(method_axis.column + 1, min(item.column for item in path))
                if (item := positions.get((cell.row, column_index))) is not None
                and item.role == "data" and item.row_span == item.column_span == 1
                and item.text.strip().casefold() == str(generator_match.value).casefold()
                and (fragment := cell_evidence(item)) is not None
            ]
            if len(matching_cells) == 1:
                generator_match = replace(generator_match, evidence_ids=(*matching_cells, *generator_match.evidence_ids))
            else:
                generator_match = None

        # A possible baseline match is weaker than an assertion about this row.
        if generator_match:
            generator_options = (generator_match,)
        elif not any(item.field == "generator" and scope_rank(item.scope, scope) == 2 for item in assertions):
            generator_options = ()
        if depth_match:
            depth_options = (depth_match,)
        elif depth_options and not any(
            item.field == "retrieval_top_k" and scope_rank(item.scope, scope) == 2 for item in assertions
        ):
            continue
        for option in generator_options:
            claims.append(Assertion("generator", option.value, row, option.evidence_ids))
        for option in depth_options:
            claims.append(Assertion("retrieval_top_k", option.value, row, option.evidence_ids))

        expected = ("generator",) if answer_axis or needs_prose_generator else ()
        observation = Observation("qa", number.text, source, expected)
        task_sources = tuple(task_refs) if "-hop" in task else tuple(dict.fromkeys((caption.id, *task_refs)))
        bound = bind_measurement(observation, claims, caption, task, task_sources)
        results.extend(bound.results)
        binding_remarks.extend(bound.remarks)

    handled = {(item.source.row, item.source.column) for item in (*results, *binding_remarks)}
    return replace(collected, results=(*collected.results, *results), remarks=tuple(
        item for item in collected.remarks
        if item.kind != "ambiguous-binding" or (item.source.row, item.source.column) not in handled
    ) + tuple(binding_remarks))
