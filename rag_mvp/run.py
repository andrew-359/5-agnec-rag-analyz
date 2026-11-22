from collections import Counter
from dataclasses import asdict, dataclass, field, replace
from typing import Any

from .core.domain import Box, Collection, ConditionAssertion, Evidence, GridCell, ReadTable, Remark, Result, Source, collect_results


class IncompatibleArtifact(ValueError):
    """An immutable input cannot be used for this plan."""


@dataclass(frozen=True)
class ArticleContext:
    sha256: str
    assertions: tuple[ConditionAssertion, ...]
    evidence: tuple[Evidence, ...]


@dataclass(frozen=True)
class TableGrid:
    label: str
    rows: int
    columns: int
    evidence_ids: tuple[str, ...] = ()
    caption_evidence_id: str | None = None
    cells: tuple[GridCell, ...] = ()
    unplaced_evidence_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class TableRead:
    table: ReadTable | None = None
    grid: TableGrid | None = None
    evidence: tuple[Evidence, ...] = ()
    remarks: tuple[Remark, ...] = ()
    versions: dict[str, str] = field(default_factory=dict)
    settings: dict[str, Any] = field(default_factory=dict)
    raw_artifacts: dict[str, Any] = field(default_factory=dict)
    context_evidence_ids: tuple[str, ...] = ()

    def with_article_evidence(self, fragments: tuple[Evidence, ...]) -> "TableRead":
        return replace(
            self,
            evidence=(*self.evidence, *fragments),
            context_evidence_ids=(*self.context_evidence_ids, *(fragment.id for fragment in fragments)),
        )

    def collect(
        self, assertions: tuple[ConditionAssertion, ...] = (), *, result_crop: Box | None = None,
    ) -> Collection:
        ids = Counter(fragment.id for fragment in self.evidence)
        if self.table:
            ids[f"page-{self.table.page}"] += 1
            assertions = tuple(item for item in assertions if item.applies_to(self.table))
        duplicates = sorted(ref for ref, count in ids.items() if count > 1)
        if duplicates:
            raise ValueError(f"Повторяются идентификаторы источников: {', '.join(duplicates)}")
        if assertions:
            available = {
                fragment.id for fragment in self.evidence
                if fragment.precision in {"page", "text_fragment"} and fragment.text.strip()
            }
            missing = sorted({ref for item in assertions for ref in (item.evidence_id, *item.supporting_evidence_ids)} - available)
            if missing:
                raise ValueError(f"Источники утверждений отсутствуют в прогоне: {', '.join(missing)}")
        interpreted = Collection((), ())
        if self.table:
            from .article_context.benchmark_latency import recover_latency_benchmark
            from .article_context.benchmark_table import recover_benchmark_measurements
            from .article_context.watermark_table import recover_watermark_measurements
            from .numeric_header_observations import numeric_header_remarks
            from .passport_fallback import passport_remarks

            context_ids = set(self.context_evidence_ids)
            context = tuple(fragment for fragment in self.evidence if fragment.id in context_ids)
            grid = self.grid.cells if self.grid else ()
            retrieval = recover_benchmark_measurements(
                self.table, grid, self.evidence, context, interpreted, self.remarks, assertions, retrieval_only=True,
            )
            claimed = {(item.source.row, item.source.column) for item in (*retrieval.results, *retrieval.remarks)}
            remaining_table = self.table
            if claimed:
                remaining_table = replace(self.table, cells=tuple(
                    cell for cell in self.table.cells if (cell.row, cell.column) not in claimed
                ))
            interpreted = collect_results(
                remaining_table, assertions, grid=grid, evidence=self.evidence, context_evidence=context,
            )
            interpreted = recover_watermark_measurements(remaining_table, grid, self.evidence, context, interpreted, self.remarks)
            interpreted = recover_latency_benchmark(remaining_table, grid, self.evidence, context, interpreted, self.remarks)
            interpreted = recover_benchmark_measurements(
                remaining_table, grid, self.evidence, context, interpreted, self.remarks, assertions,
            )
            interpreted = Collection(
                (*interpreted.results, *retrieval.results),
                (*interpreted.remarks, *retrieval.remarks, *numeric_header_remarks(self)),
            )
            interpreted = Collection(interpreted.results, passport_remarks(self, interpreted))
        if result_crop is not None:
            interpreted = restrict_results(interpreted, result_crop)
        return Collection(interpreted.results, self.remarks + interpreted.remarks)

    def serialize(self, collected: Collection, page: int, *, strategy: str | None = None,
                  source_prefix: str = "") -> dict[str, Any]:
        cells = self.grid.cells if self.grid else ()
        by_position = {(cell.row, cell.column): cell for cell in cells}
        page_id = f"page-{page}"
        fragments = (Evidence(page_id, page, "", None, "page"), *self.evidence)
        page_ids = {fragment.id for fragment in fragments if fragment.id == f"page-{fragment.page}"}

        def source_id(ref):
            return ref if ref is None or ref in page_ids else source_prefix + ref

        def source_ids(refs):
            return tuple(map(source_id, refs))

        def record_fields(item):
            fields = item.to_dict() if isinstance(item, Result) else asdict(item)
            if not fields.get("parameters"):
                fields.pop("parameters", None)
            fields["condition_sources"] = {field: source_ids(refs) for field, refs in item.condition_sources.items()}
            return fields

        def linked_source(source: Source) -> dict[str, Any]:
            cell = by_position.get((source.row, source.column))
            table_refs = self.grid.evidence_ids if self.grid else ()
            if cell is None and self.grid and self.grid.caption_evidence_id:
                table_refs = (self.grid.caption_evidence_id, *table_refs)
            evidence_ids = source.evidence_ids or (cell.evidence_ids if cell else ()) or table_refs or (page_id,)
            return {**asdict(source), "cell_id": source_id(cell.id) if cell else None,
                    "evidence_ids": source_ids(evidence_ids)}

        def linked_remark(item: Remark, index: int) -> dict[str, Any]:
            fields = record_fields(item)
            fields["options"] = tuple({**option, "evidence_ids": source_ids(option["evidence_ids"])}
                                      for option in fields["options"])
            indices = fields.pop("result_indices")
            if indices:
                fields["result_ids"] = [f"result-{position}" for position in indices]
            return {**fields, "id": f"remark-{index}", "source": linked_source(item.source)}

        table = None
        if self.grid:
            table = {
                "label": self.grid.label,
                "rows": self.grid.rows,
                "columns": self.grid.columns,
                "evidence_ids": source_ids(self.grid.evidence_ids),
                "caption_evidence_id": source_id(self.grid.caption_evidence_id),
                "unplaced_evidence_ids": source_ids(self.grid.unplaced_evidence_ids),
            }
        return {
            "grid": table,
            "cells": [{"id": source_id(cell.id), **asdict(cell), "evidence_ids": source_ids(cell.evidence_ids)}
                      for cell in cells],
            "evidence": [{**asdict(fragment), "id": source_id(fragment.id),
                          **({"strategy": strategy} if strategy is not None and fragment.id not in page_ids else {})}
                         for fragment in fragments],
            "context_evidence_ids": source_ids(self.context_evidence_ids),
            "results": [
                {**record_fields(item), "id": f"result-{index}", "source": linked_source(item.source)}
                for index, item in enumerate(collected.results)
            ],
            "remarks": [linked_remark(item, index) for index, item in enumerate(collected.remarks)],
        }


def restrict_results(collection: Collection, region: Box) -> Collection:
    left, top, right, bottom = region

    def contains_center(source: Source) -> bool:
        if source.box is None:
            return False
        x0, y0, x1, y1 = source.box
        return left <= (x0 + x1) / 2 <= right and top <= (y0 + y1) / 2 <= bottom

    results = []
    indices = {}
    for index, result in enumerate(collection.results):
        if contains_center(result.source):
            indices[index] = len(results)
            results.append(result)
    remarks = tuple(
        replace(remark, result_indices=tuple(indices[index] for index in remark.result_indices if index in indices))
        for remark in collection.remarks if remark.source.row is None or contains_center(remark.source)
    )
    return Collection(tuple(results), remarks)
