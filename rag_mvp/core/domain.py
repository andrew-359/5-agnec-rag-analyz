import re
from dataclasses import asdict, dataclass, field as dataclass_field, replace
from decimal import Decimal
from typing import Literal


Box = tuple[float, float, float, float]
NUMBER = r"[+-]?(?:\d+(?:\.\d+)?|\.\d+)"
MULTI_HOP_TASK = re.compile(r"\bmulti-hop (?:QA|question answering|questions and answers)\b", re.IGNORECASE)
LABELED_HEADER = re.compile(r"^(model|generator|dataset|metric)\s*:\s*(.*)$", re.IGNORECASE)
METRIC_HEADER = re.compile(r"EM|F1", re.IGNORECASE)
RETRIEVAL_METRIC = re.compile(r"(?:Hit|R|Recall|Precision)\s*@\s*(?P<k>[1-9][0-9]{0,14})", re.I)
SCORE_NAME = re.compile(r"EM|F1|Accuracy|Hallucination|Missing|Factuality", re.IGNORECASE)
METRIC_WITH_SCALE = re.compile(r"(?P<metric>Accuracy|ECE|OC@\d+(?:\.\d+)?)\s*\(%\)\s*[↑↓]", re.IGNORECASE)
DATASET_DESCRIPTION = re.compile(r"\b(?:datasets?|benchmarks?)\b", re.IGNORECASE)
QUESTION_DATASET = re.compile(
    r"\b(?:question[- ]answering|(?:MCQs?(?: and MAQs?)?|multiple[- ](?:choice|answer)|open[- ]ended) questions?"
    r"|multiple[- ]choice datasets?)\b",
    re.IGNORECASE,
)
ConditionField = Literal["generator", "dataset", "retrieval_top_k"]
ConditionValue = str | int
ConditionSources = dict[str, tuple[str, ...]]


@dataclass(frozen=True)
class Evidence:
    id: str
    page: int
    text: str
    box: Box | None
    precision: Literal["cell", "text_fragment", "estimated_cell", "table", "page", "figure", "plot_point"]


@dataclass(frozen=True)
class GridCell:
    row: int
    column: int
    text: str
    row_span: int = 1
    column_span: int = 1
    role: str | None = None
    evidence_ids: tuple[str, ...] = ()

    @property
    def id(self) -> str:
        return f"cell-{self.row}-{self.column}"


def _is_retrieval_top_k(value: object) -> bool:
    # Saved runs pass through JSON to JavaScript, where the count must stay exact.
    return type(value) is int and 0 <= value <= (1 << 53) - 1


@dataclass(frozen=True)
class TableTarget:
    page: int
    number: int


@dataclass(frozen=True)
class ConditionAssertion:
    field: ConditionField
    value: ConditionValue
    scope: str
    evidence_id: str
    target_table: TableTarget | None = None
    supporting_evidence_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.field in {"generator", "dataset"} and isinstance(self.value, str) and self.value.strip():
            return
        if self.field == "retrieval_top_k" and _is_retrieval_top_k(self.value):
            return
        raise ValueError(f"Недопустимое значение условия {self.field}: {self.value!r}")

    def applies_to(self, table: "ReadTable") -> bool:
        target = self.target_table
        return target is None or (target.page, target.number) == (table.page, table.number)


@dataclass(frozen=True)
class ConditionOption:
    value: ConditionValue
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class RowGroup:
    text: str
    evidence_id: str


@dataclass(frozen=True)
class ReadCell:
    row: int
    column: int
    method: str
    headers: tuple[str, ...]
    text: str
    box: Box | None = None
    is_baseline: bool | None = None
    row_group: RowGroup | None = None
    header_evidence_ids: tuple[str | None, ...] = ()
    baseline_evidence_ids: tuple[str, ...] = ()
    verified_condition_sources: ConditionSources = dataclass_field(default_factory=dict)


@dataclass(frozen=True)
class ReadTable:
    page: int
    number: int
    caption: str
    context: str
    cells: tuple[ReadCell, ...]
    caption_evidence_id: str | None = None


@dataclass(frozen=True)
class Source:
    page: int
    table: int | None
    row: int | None = None
    column: int | None = None
    headers: tuple[str, ...] = ()
    box: Box | None = None
    evidence_ids: tuple[str, ...] = ()
    figure: str | None = None


@dataclass(frozen=True)
class FigureSource:
    page: int
    figure: str
    fragment: str
    box: Box
    evidence_ids: tuple[str, ...]
    headers: tuple[str, ...]


@dataclass(frozen=True)
class Result:
    method: str
    generator: str
    dataset: str
    metric: str
    task: str
    experiment: str
    protocol: str | None
    raw_value: str
    value: str
    unit: str | None
    statistic: str | None
    retrieval_top_k: int | None
    source: Source | FigureSource
    condition_sources: ConditionSources = dataclass_field(default_factory=dict)
    components: dict[str, bool] = dataclass_field(default_factory=dict)
    parameters: dict[str, float] = dataclass_field(default_factory=dict)

    metric_k: int | None = None
    factor: str | None = None
    setting: str | None = None
    setting_default: bool | None = None
    source_language: str | None = None

    def to_dict(self) -> dict:
        fields = asdict(self)
        for name in ("metric_k", "factor", "setting", "setting_default", "source_language"):
            if fields[name] is None:
                fields.pop(name)
        return fields


@dataclass(frozen=True)
class Remark:
    reason: str
    raw_text: str
    source: Source
    field: ConditionField | Literal["method", "metric", "metric_k", "beam", "factor", "setting", "setting_default", "unit", "source_language"] | None = None
    conditions: dict[str, ConditionValue] = dataclass_field(default_factory=dict)
    condition_sources: ConditionSources = dataclass_field(default_factory=dict)
    options: tuple[ConditionOption, ...] = ()
    kind: Literal["ambiguous-binding", "scale-interpretation", "reading-defect"] | None = None
    result_indices: tuple[int, ...] = ()
    statistic: str | None = None


@dataclass(frozen=True)
class Collection:
    results: tuple[Result, ...]
    remarks: tuple[Remark, ...]


@dataclass(frozen=True)
class ColumnConditions:
    generator: str | None
    dataset: str
    metric: str
    retrieval_top_k: int | None = None
    condition_sources: ConditionSources = dataclass_field(default_factory=dict)


def generator_group_dataset(caption: str) -> str | None:
    match = re.match(
        r"^Table\s+\d+\s*:\s*(\S+)\s+answer generation performance\s+benchmarked by different LLM generators\b",
        caption.strip(), re.IGNORECASE,
    )
    return match[1] if match else None


def caption_has_answer_f1_sem(caption: str) -> bool:
    return bool(re.search(
        r"\banswer F1\s*\(and its standard error (?:of (?:the )?)?mean\)",
        " ".join(caption.split()), re.IGNORECASE,
    ))


def labeled_column(cell: ReadCell) -> ColumnConditions | None:
    if len(cell.headers) not in {2, 3}:
        return None
    fields: dict[str, str] = {}
    sources: ConditionSources = {}
    for index, header in enumerate(cell.headers):
        header = header.strip()
        label = LABELED_HEADER.fullmatch(header)
        if label:
            role, value = label[1].lower(), label[2].strip()
            if role == "model":
                role = "generator"
        elif METRIC_HEADER.fullmatch(header):
            role, value = "metric", header
        else:
            return None
        if not value or role in fields:
            return None
        fields[role] = value
        if verified := cell.verified_condition_sources.get(role):
            sources[role] = verified
        elif role == "generator" and index < len(cell.header_evidence_ids) and cell.header_evidence_ids[index]:
            sources[role] = (cell.header_evidence_ids[index],)
    if "dataset" in fields and "metric" in fields:
        return ColumnConditions(fields.get("generator"), fields["dataset"], fields["metric"], condition_sources=sources)
    return None


def cited_names(text: str) -> set[str]:
    return set(re.findall(r"(?<!\w)([\w-]+)\s*\([^()]*\b(?:19|20)\d\d\)", text))


def model_name_for_role(name: str) -> str:
    return re.sub(r"[-.\s]+", "", name.lower()).removesuffix("instruct")


def column_conditions(
    cell: ReadCell, caption_generator: str | None, *, has_dataset_context: bool,
    benchmark_names: set[str], generator_names: set[str], caption_evidence_id: str | None,
) -> ColumnConditions | None:
    headers = cell.headers
    if not all(headers):
        return None
    if any(LABELED_HEADER.fullmatch(header.strip()) for header in headers):
        return labeled_column(cell)
    if len(headers) == 3:
        generator, dataset, metric = headers
        if (METRIC_HEADER.fullmatch(metric) and model_name_for_role(generator) in generator_names
                and sum(name.lower().startswith(dataset.lower()) for name in benchmark_names) == 1):
            sources: ConditionSources = {}
            if cell.header_evidence_ids and cell.header_evidence_ids[0]:
                sources["generator"] = (cell.header_evidence_ids[0],)
            return ColumnConditions(generator, dataset, metric, condition_sources=sources)
    if len(headers) == 2:
        header = headers[0].strip()
        if (cell.row_group and cell.verified_condition_sources.get("dataset")
                and cell.verified_condition_sources.get("metric") and (metric := METRIC_WITH_SCALE.fullmatch(header))):
            return ColumnConditions(cell.row_group.text, headers[1], metric["metric"], condition_sources={
                "generator": (cell.row_group.evidence_id,),
                "dataset": cell.verified_condition_sources["dataset"],
                "metric": cell.verified_condition_sources["metric"],
            })
        top_k = re.match(r"Top\s*-\s*k\s*=", header, re.IGNORECASE)
        if top_k:
            raw_value = header[top_k.end():].strip()
            if not caption_generator or not raw_value.isdecimal():
                return None
            try:
                value = int(raw_value)
            except ValueError:
                return None
            if _is_retrieval_top_k(value):
                sources = {}
                if caption_evidence_id:
                    sources["generator"] = (caption_evidence_id,)
                if cell.header_evidence_ids and cell.header_evidence_ids[0]:
                    sources["retrieval_top_k"] = (cell.header_evidence_ids[0],)
                return ColumnConditions(caption_generator, headers[1], "EM", value, sources)
            return None
        if has_dataset_context and METRIC_HEADER.fullmatch(headers[1]):
            return ColumnConditions(None, headers[0], headers[1])
    return None


def scope_rank(scope: str, cell: ReadCell) -> int:
    """Separate confirmed coverage from possible baseline coverage."""
    text = " ".join(scope.casefold().split()).removeprefix("for ")
    method = " ".join(cell.method.casefold().split())
    if text in {method, f"{method} and most baselines", "all methods", "across all methods"}:
        return 2
    if cell.is_baseline and (text == "most baselines" or text.endswith(" and most baselines")):
        return 1
    return 0


def match_condition(
    assertions: tuple[ConditionAssertion, ...], field: ConditionField, cell: ReadCell,
    from_table: ConditionOption | None,
) -> tuple[ConditionOption | None, tuple[ConditionOption, ...]]:
    ranked = [(scope_rank(item.scope, cell), item) for item in assertions if item.field == field]
    rank = max((priority for priority, _ in ranked), default=0)
    if rank == 0 or (rank == 1 and from_table is not None):
        return from_table, ()

    by_value: dict[ConditionValue, list[str]] = {}
    if from_table is not None:
        by_value[from_table.value] = list(from_table.evidence_ids)
    for priority, item in ranked:
        if priority == rank:
            evidence_ids = by_value.setdefault(item.value, [])
            evidence_ids.extend((item.evidence_id, *item.supporting_evidence_ids))
            if rank == 1:
                evidence_ids.extend(cell.baseline_evidence_ids)
    options = tuple(ConditionOption(value, tuple(dict.fromkeys(ids))) for value, ids in by_value.items())
    if rank > 1 and len(options) == 1:
        return options[0], ()
    return None, options


def collect_results(
    table: ReadTable, assertions: tuple[ConditionAssertion, ...] = (), *,
    grid: tuple[GridCell, ...] = (), evidence: tuple[Evidence, ...] = (),
    context_evidence: tuple[Evidence, ...] = (),
) -> Collection:
    """Interpret the read cells using the article's headers and evaluation context."""
    ablation = bool(grid and re.search(r"\bablation study\b", table.caption, re.I))
    if ablation:
        components = ablation_headers(grid, "ablation setting")
        datasets = ablation_headers(grid, "datasets")
        if components and datasets and not {item.column for item in components}.intersection(
            item.column for item in datasets
        ):
            return collect_ablation(table, grid, evidence, context_evidence, components, datasets)

    assertions = tuple(item for item in assertions if item.applies_to(table))
    paragraphs = re.sub(r"(?<=\w)-[ \t]*\n\s*(?=\w)", "", table.context).splitlines()
    context = " ".join(table.context.split())
    text = f"{table.caption} {context}"
    dataset_names = cited_names(" ".join(paragraphs))
    benchmark_names = cited_names(" ".join(p for p in paragraphs if DATASET_DESCRIPTION.search(p)))
    generator_names = set()
    for paragraph in paragraphs:
        models = re.search(r"\bwe use (.+?) with [^.]+ for embeddings\b", paragraph, re.IGNORECASE)
        if models:
            generator_names.update(
                model_name_for_role(name)
                for name in re.findall(r"([\w.-]+)\s*\([^()]*\b(?:19|20)\d\d\)", models[1])
            )
    protocol = next((paragraph for paragraph in paragraphs if re.search(r"\bFor evaluation,", paragraph, re.IGNORECASE)), None)
    paired_statistics = re.search(
        r"\bmean\s*\((sample\s+)?(?:SD|standard deviation)\)", table.caption, re.IGNORECASE
    )
    sd_statistic = "sample_sd" if paired_statistics and paired_statistics[1] else "sd"
    answer_f1_sem = caption_has_answer_f1_sem(table.caption)
    if protocol is None and paired_statistics and re.search(r"\bover [\w\s]+? runs\b", table.caption, re.IGNORECASE):
        protocol = table.caption
    protocol_sources: tuple[str, ...] = ()
    if protocol:
        if protocol == table.caption and table.caption_evidence_id:
            protocol_sources = (table.caption_evidence_id,)
        else:
            for fragment in context_evidence:
                fragment_text = re.sub(r"(?<=\w)-[ \t]*\n\s*(?=\w)", "", fragment.text)
                if protocol in fragment_text.splitlines():
                    protocol_sources = (fragment.id,)
                    break
    baseline_scope = next((fragment.id for fragment in context_evidence
                           if re.match(r"\s*Baselines\.\s*We evaluate\b[^.]+\bagainst\b", fragment.text, re.I)), None)
    caption_generator = None
    if paired_statistics and re.search(r"\bexact[- ]match\b", table.caption, re.IGNORECASE):
        model = re.search(r"\bcomparison with (\S+)", table.caption, re.IGNORECASE)
        caption_generator = model[1].rstrip(".,;") if model else None
    percent_scale = re.search(r"\(\s*%\s*\)|\bpercent(?:age)?s?\b", table.caption, re.IGNORECASE)
    task = ""
    if MULTI_HOP_TASK.search(text):
        task = "multi-hop question answering"
        if re.search(r"\bopen-domain multi-hop\b", text, re.IGNORECASE):
            task = f"open-domain {task}"
    elif re.search(r"\bopen-domain (?:QA|question answering)\b", text, re.IGNORECASE):
        task = "open-domain question answering"
    grouped_dataset = generator_group_dataset(table.caption)
    has_dataset_context = bool(assertions and DATASET_DESCRIPTION.search(table.caption))
    experiment = (
        table.caption if grouped_dataset or re.search(r"\b(?:results?|evaluation|comparison)\b", table.caption, re.IGNORECASE)
        else ""
    )

    results: list[Result] = []
    remarks: list[Remark] = []
    for cell in table.cells:
        source = Source(
            page=table.page,
            table=table.number,
            row=cell.row,
            column=cell.column,
            headers=cell.headers,
            box=cell.box,
        )
        if ablation and not cell.verified_condition_sources.get("method") and ablation_measurement(cell.text):
            remarks.append(open_ablation_method(cell, source, grid))
            continue
        condition_sources: ConditionSources = {}
        uses_group_conditions = grouped_dataset is not None and len(cell.headers) == 1
        if uses_group_conditions:
            metric, = cell.headers
            group = cell.row_group
            conditions = None
            if group and group.text.strip() and metric.upper() in {"EM", "F1", "KP", "KR", "KF1"}:
                condition_sources["generator"] = (group.evidence_id,)
                if table.caption_evidence_id:
                    condition_sources["dataset"] = (table.caption_evidence_id,)
                conditions = ColumnConditions(group.text.strip(), grouped_dataset, metric,
                                              condition_sources=condition_sources)
        else:
            conditions = column_conditions(
                cell, caption_generator, has_dataset_context=has_dataset_context,
                benchmark_names=benchmark_names, generator_names=generator_names,
                caption_evidence_id=table.caption_evidence_id,
            )
        result_task = task
        result_experiment = experiment
        if conditions is None and len(cell.headers) == 1 and SCORE_NAME.fullmatch(cell.headers[0]):
            dataset, options = match_condition(assertions, "dataset", cell, None)
            metric, = cell.headers
            if options:
                remarks.append(Remark(
                    "Не удалось однозначно установить набор данных.", cell.text, source, "dataset",
                    {"method": cell.method, "metric": metric}, options=options, kind="ambiguous-binding",
                ))
                continue
            if dataset:
                assert isinstance(dataset.value, str)
                sources = {"dataset": dataset.evidence_ids}
                if cell.header_evidence_ids and cell.header_evidence_ids[0]:
                    sources["metric"] = (cell.header_evidence_ids[0],)
                if table.caption_evidence_id:
                    sources["experiment"] = (table.caption_evidence_id,)
                if not result_task and re.search(r"\bRAG QA\b", table.caption, re.IGNORECASE):
                    result_task = "question answering"
                    if table.caption_evidence_id:
                        sources["task"] = (table.caption_evidence_id,)
                result_experiment = table.caption
                conditions = ColumnConditions(None, dataset.value, metric, condition_sources=sources)
        task_evidence = ()
        if not result_task and conditions and cell.verified_condition_sources.get("dataset"):
            dataset_name = re.compile(rf"(?<![\w-]){re.escape(conditions.dataset)}(?![\w-])", re.IGNORECASE)
            task_evidence = tuple(
                item.id for item in context_evidence
                if DATASET_DESCRIPTION.search(item.text) and dataset_name.search(item.text)
                and QUESTION_DATASET.search(" ".join(item.text.split()))
            )
            if task_evidence:
                result_task = "question answering"
        if not (cell.method and result_experiment and result_task and conditions):
            remarks.append(Remark(
                "Не удалось однозначно связать число с условиями.", cell.text, source, kind="ambiguous-binding",
            ))
            continue

        generator, dataset, metric = conditions.generator, conditions.dataset, conditions.metric
        condition_sources = conditions.condition_sources.copy()
        for field in ("method", "experiment"):
            if verified := cell.verified_condition_sources.get(field):
                condition_sources[field] = verified
        if task_evidence:
            condition_sources["task"] = task_evidence
        if not uses_group_conditions and not any(LABELED_HEADER.fullmatch(header.strip()) for header in cell.headers):
            matching_names = {name for name in dataset_names if name.lower().startswith(dataset.lower())}
            if len(matching_names) == 1:
                dataset = next(iter(matching_names))

        known: dict[str, ConditionValue] = {"method": cell.method, "dataset": dataset, "metric": metric}
        unresolved: dict[ConditionField, tuple[ConditionOption, ...]] = {}
        for field, value in (("generator", generator), ("retrieval_top_k", conditions.retrieval_top_k)):
            references = condition_sources.pop(field, ())
            from_table = ConditionOption(value, references) if value is not None else None
            match, options = match_condition(assertions, field, cell, from_table)
            if match:
                known[field] = match.value
                if match.evidence_ids:
                    condition_sources[field] = match.evidence_ids
            elif field == "generator" or options:
                unresolved[field] = options

        if unresolved:
            for field, options in unresolved.items():
                label = "генератор" if field == "generator" else "retrieval top-k"
                remarks.append(Remark(
                    f"Не удалось однозначно установить {label} для метода {cell.method}.",
                    cell.text, source, field, known, condition_sources, options, kind="ambiguous-binding",
                ))
            continue

        if protocol_sources:
            condition_sources["protocol"] = protocol_sources
        if baseline_scope and re.match(r"Table\s+\d+:\s*Main (?:experimental )?results\b", table.caption, re.I):
            condition_sources["comparison_scope"] = (baseline_scope,)
        resolved_generator = known["generator"]
        resolved_top_k = known.get("retrieval_top_k")
        assert isinstance(resolved_generator, str)
        assert resolved_top_k is None or isinstance(resolved_top_k, int)
        readings: tuple[tuple[str | None, str], ...] = ((None, cell.text),)
        error_statistic = sd_statistic if paired_statistics else None
        if answer_f1_sem and uses_group_conditions and metric.upper() == "F1":
            error_statistic = "sem"
        if error_statistic:
            mean, _, tail = cell.text.partition("(")
            tail = tail.strip()
            error = tail[:-1].strip() if tail.endswith(")") else ""
            if error_statistic == "sem":
                error = error.removeprefix("±").strip()
            readings = (("mean", mean), (error_statistic, error))

        for statistic, raw in readings:
            is_uncertainty = statistic in {"sd", "sample_sd", "sem"}
            value_match = re.fullmatch(rf"({NUMBER})\s*(%?)", raw.strip())
            if not value_match or (statistic == "sem" and value_match[1].startswith("-")):
                label = ("SEM" if statistic == "sem" else "SD") if is_uncertainty else "значение метрики"
                missing_value = raw.strip().casefold() in {"", "-", "—", "–", "n/a", "na"}
                defect = is_uncertainty or not missing_value
                remarks.append(Remark(f"Не удалось прочитать {label}.", cell.text, source,
                                      kind="reading-defect" if defect else None, statistic=statistic))
                continue

            number = Decimal(value_match[1])
            unit = None
            if value_match[2] or percent_scale:
                unit = "percentage_point" if is_uncertainty else "%"
            elif uses_group_conditions and 0 <= number <= 1:
                unit = "fraction"
            elif (
                metric.upper() in {"EM", "F1"} and protocol and re.search(rf"\b{re.escape(metric)}\b", protocol, re.IGNORECASE)
                and re.match(r"^0?\.", value_match[1])
            ):
                unit = "fraction"
            results.append(Result(
                method=cell.method,
                generator=resolved_generator,
                dataset=dataset,
                metric=metric,
                task=result_task,
                experiment=result_experiment,
                protocol=protocol,
                raw_value=raw.strip(),
                value=str(number),
                unit=unit,
                statistic=statistic,
                retrieval_top_k=resolved_top_k,
                source=source,
                condition_sources=condition_sources,
            ))

    if results and protocol is None:
        remarks.append(Remark("Подробности протокола оценки не извлечены.", table.caption, Source(table.page, table.number)))
    return Collection(tuple(results), tuple(remarks))


ABLATION_MARKS = {"✓": True, "✔": True, "": False, "✗": False, "✘": False, "×": False, "—": False}
ABLATION_SCORE = re.compile(rf"\s*({NUMBER})(?:\s*\(\s*(↓\s*({NUMBER})\s*%)\s*\))?\s*")


def ablation_headers(grid: tuple[GridCell, ...], label: str) -> tuple[GridCell, ...]:
    groups = [cell for cell in grid if cell.role == "column_header" and cell.text.strip().casefold() == label]
    if len(groups) != 1:
        return ()
    group, = groups
    headers = sorted(
        (cell for cell in grid if cell.role == "column_header" and cell.row == group.row + group.row_span
         and group.column <= cell.column < group.column + group.column_span
         and cell.column_span == cell.row_span == 1 and cell.text.strip()),
        key=lambda cell: cell.column,
    )
    if len(headers) == group.column_span == len({cell.text.strip().casefold() for cell in headers}) and (
        [cell.column for cell in headers] == list(range(group.column, group.column + group.column_span))
    ):
        return tuple(headers)
    return ()


def ablation_measurement(text: str) -> bool:
    return bool(ABLATION_SCORE.fullmatch(text) or re.fullmatch(
        rf"\s*{NUMBER}\s*%?(?:\s*(?:±\s*{NUMBER}|\(\s*±?\s*{NUMBER}\s*\)))?\s*", text,
    ))


def open_ablation_method(cell: ReadCell, source: Source, grid: tuple[GridCell, ...]) -> Remark:
    conditions: dict[str, ConditionValue] = {}
    refs: ConditionSources = {}
    if cell.headers:
        conditions["header_path"] = " → ".join(cell.headers)
        if header_refs := tuple(ref for ref in cell.header_evidence_ids if ref):
            refs["header_path"] = header_refs

    labels = [item for item in grid if item.role == "row_header" and item.row == cell.row
              and item.row_span == 1 and item.column + item.column_span <= cell.column
              and item.text.strip() and item.evidence_ids]
    if len(labels) > 1 and cell.method:
        labels = [item for item in labels if item.text == cell.method]
    if len(labels) == 1:
        label, = labels
        headers = [item for item in grid if item.role == "column_header" and item.text.strip()
                   and item.row + item.row_span <= label.row
                   and item.column <= label.column < item.column + item.column_span]
        header = headers[0] if len(headers) == 1 else None
        field = header.text if header and header.text.casefold() not in {"method", "header_path"} else "variant"
        conditions[field] = label.text
        refs[field] = (*label.evidence_ids, *(header.evidence_ids if header else ()))
        reason = f"Не подтверждено, к какому методу относится вариант «{label.text}»."
    else:
        reason = "Не подтверждено, к какому методу относится это измерение."
    return Remark(reason, cell.text, source, "method", conditions, refs, kind="ambiguous-binding")


def ablation_scale(
    results: list[Result], decreases: dict[int, tuple[str, Decimal]], remarks: list[Remark],
) -> Collection:
    full_scores: dict[str, list[Result]] = {}
    for index, result in enumerate(results):
        if index not in decreases and all(result.components.values()):
            full_scores.setdefault(result.dataset, []).append(result)

    readings = []
    for index, result in enumerate(results):
        readings.append(result)
        if index not in decreases:
            continue
        raw, printed = decreases[index]
        baseline = full_scores.get(result.dataset, [])
        difference = (Decimal(baseline[0].value) - Decimal(result.value)) * 100 if len(baseline) == 1 else None
        if all(result.components.values()) or difference is None or difference <= 0 or difference != printed:
            remarks.append(Remark("Шкала снижения не подтверждена по полной конфигурации.", raw, result.source))
            continue
        conditions = {**result.condition_sources, "baseline": baseline[0].source.evidence_ids}
        decrease = replace(result, raw_value=raw, value=str(printed), unit="percentage_points",
                           statistic="decrease", condition_sources=conditions)
        remarks.append(Remark(
            f"Снижение {result.metric} составляет {printed} п.п. по разности {baseline[0].value} и {result.value}.",
            raw, result.source, kind="scale-interpretation", result_indices=(len(readings),),
        ))
        readings.append(decrease)
    return Collection(tuple(readings), tuple(remarks))


def collect_ablation(
    table: ReadTable, grid: tuple[GridCell, ...], evidence: tuple[Evidence, ...],
    context: tuple[Evidence, ...], component_headers: tuple[GridCell, ...], dataset_headers: tuple[GridCell, ...],
) -> Collection:

    table_reference = re.compile(rf"\bTable\s+{table.number}(?!\w|[.\-]\w)", re.I)
    scoped_metrics: dict[str, list[str]] = {}
    for fragment in context:
        if table_reference.search(fragment.text):
            for match in re.finditer(r"\b(EM|F1)\s+scores?\b", fragment.text, re.I):
                scoped_metrics.setdefault(match[1].upper(), []).append(fragment.id)
    methods: dict[str, list[str]] = {}
    task_refs = []
    for fragment in context:
        if MULTI_HOP_TASK.search(fragment.text):
            task_refs.append(fragment.id)
        for match in re.finditer(r"\bcomponents of\s+([\w-]+(?:\.[\w-]+)*)", fragment.text, re.I):
            methods.setdefault(match[1], []).append(fragment.id)

    by_id = {fragment.id: fragment for fragment in evidence}
    def cell_box(cell: GridCell) -> Box | None:
        return next((by_id[ref].box for ref in cell.evidence_ids
                     if ref in by_id and by_id[ref].precision == "cell" and by_id[ref].box), None)

    markers = {
        header.column: [cell for cell in grid if cell.row > header.row
                        and cell.column <= header.column < cell.column + cell.column_span]
        for header in component_headers
    }
    results = []
    remarks = []
    decreases = {}
    for cell in sorted(grid, key=lambda item: (item.row, item.column)):
        header = next((item for item in dataset_headers if item.column == cell.column and item.row < cell.row), None)
        if header is None or not cell.text.strip() or cell.role == "column_header":
            continue
        source = Source(table.page, table.number, cell.row, cell.column, ("Datasets", header.text.strip()),
                        cell_box(cell), cell.evidence_ids)
        components = {}
        refs = []
        for component in component_headers:
            covering = [marker for marker in markers[component.column]
                        if marker.row <= cell.row < marker.row + marker.row_span]
            # Table readers may extend a single printed check mark through empty rows.
            # Its text box establishes which score row actually contains the mark.
            spanned = [(marker, cell_box(marker)) for marker in covering if marker.row_span > 1]
            if any(marker.column_span != 1 for marker in covering) or (
                spanned and (source.box is None or any(box is None for _, box in spanned))
            ):
                break
            in_row = [marker for marker in covering if marker.row_span == 1]
            in_row.extend(marker for marker, box in spanned
                          if source.box[1] <= (box[1] + box[3]) / 2 <= source.box[3])
            if len(in_row) > 1:
                break
            marker = next(iter(in_row), None)
            mark = marker.text.strip() if marker is not None else ""
            if mark not in ABLATION_MARKS:
                break
            components[component.text.strip()] = ABLATION_MARKS[mark]
            refs.extend(component.evidence_ids)
            if marker:
                refs.extend(marker.evidence_ids)
        score = ABLATION_SCORE.fullmatch(cell.text)
        if (not score or not 0 <= Decimal(score[1]) <= 1 or cell.row_span != 1 or cell.column_span != 1
                or len(components) != len(component_headers) or len(methods) != 1 or len(scoped_metrics) != 1 or not task_refs):
            remarks.append(Remark("Не удалось однозначно связать число с условиями абляции.", cell.text, source,
                                  kind="ambiguous-binding"))
            continue
        method, = methods
        metric, = scoped_metrics
        condition_sources = {
            "method": tuple(methods[method]), "metric": tuple(dict.fromkeys(scoped_metrics[metric])),
            "task": tuple(task_refs), "dataset": header.evidence_ids, "components": tuple(dict.fromkeys(refs)),
        }
        if score[2] is not None:
            decreases[len(results)] = (score[2], Decimal(score[3]))
        results.append(Result(method, "", header.text.strip(), metric, "multi-hop question answering", table.caption,
                              None, score[1], str(Decimal(score[1])), "fraction", None, None, source,
                              condition_sources, components))
    return ablation_scale(results, decreases, remarks)
