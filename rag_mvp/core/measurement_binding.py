"""Assemble an observed measurement from the assertions that cover its cell."""

from dataclasses import dataclass
from typing import Iterable, Literal

from .domain import ConditionOption, ConditionValue, Source, TableTarget

MeasurementKind = Literal["retrieval", "qa"]
MeasurementField = Literal[
    "method", "dataset", "metric", "metric_k", "generator", "retrieval_top_k", "beam",
    "factor", "setting", "setting_default", "unit", "source_language",
]
REQUIRED_FIELDS: dict[MeasurementKind, tuple[MeasurementField, ...]] = {
    "retrieval": ("method", "dataset", "metric"),
    "qa": ("method", "dataset", "metric"),
}


@dataclass(frozen=True)
class Observation:
    kind: MeasurementKind
    raw_value: str
    source: Source
    additional_required: tuple[MeasurementField, ...] = ()


@dataclass(frozen=True)
class Scope:
    table: TableTarget
    row: int | None = None
    column: int | None = None
    row_span: int = 1

    def covers(self, source: Source) -> bool:
        return (
            (self.table.page, self.table.number) == (source.page, source.table)
            and (self.row is None or (source.row is not None and self.row <= source.row < self.row + self.row_span))
            and (self.column is None or self.column == source.column)
        )


@dataclass(frozen=True)
class Assertion:
    field: MeasurementField
    value: ConditionValue
    scope: Scope
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class Binding:
    observation: Observation
    confirmed: dict[MeasurementField, ConditionOption]
    open_fields: dict[MeasurementField, tuple[ConditionOption, ...]]


def assemble(observation: Observation, assertions: Iterable[Assertion]) -> Binding:
    by_field: dict[MeasurementField, dict[ConditionValue, set[tuple[str, ...]]]] = {
        field: {} for field in (*REQUIRED_FIELDS[observation.kind], *observation.additional_required)
    }
    for claim in assertions:
        if claim.evidence_ids and claim.scope.covers(observation.source):
            by_value = by_field.setdefault(claim.field, {})
            by_value.setdefault(claim.value, set()).add(claim.evidence_ids)

    confirmed = {}
    open_fields = {}
    for field, by_value in by_field.items():
        options = tuple(
            ConditionOption(value, tuple(dict.fromkeys(ref for claim in sorted(refs) for ref in claim)))
            for value, refs in sorted(by_value.items(), key=lambda item: (type(item[0]).__name__, item[0]))
        )
        if len(options) == 1:
            confirmed[field] = options[0]
        else:
            open_fields[field] = options
    return Binding(observation, confirmed, open_fields)
