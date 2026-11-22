"""Exchange an adapter's saved grid and source references with the binding stage."""

import json
from pathlib import Path

from .core.domain import LABELED_HEADER, ConditionOption, Evidence, GridCell, ReadCell, ReadTable, Remark, RowGroup, Source
from .run import TableGrid, TableRead
from .table_region import TableRegion


def load_table_grid(path: Path, sha256: str, page: int, table: int, strategy: str,
                    *, region: TableRegion | None = None) -> TableRead:
    data = json.loads(path.read_bytes())
    if data["schema_version"] != 1:
        raise ValueError("Неподдерживаемый формат сохранённой сетки таблицы.")
    if data["sha256"] != sha256:
        raise ValueError("SHA-256 сетки не совпадает с версией PDF прогона.")
    if (data["page"], data["table"], data["strategy"]) != (page, table, strategy):
        raise ValueError("Адрес или стратегия сохранённой сетки не совпадают с планом прогона.")
    saved_region = TableRegion.parse(data["region"]) if "region" in data else None
    if saved_region != region:
        raise ValueError("Область сохранённой сетки не совпадает с планом прогона.")

    saved = data["read"]
    table_data = saved["table"]
    if table_data is not None:
        table_data["cells"] = tuple(
            read_cell(item, strategy, table_data.get("caption_evidence_id"))
            for item in table_data["cells"]
        )
        table_data = ReadTable(**table_data)
        if (table_data.page, table_data.number) != (page, table):
            raise ValueError("Сохранённая сетка содержит другой адрес таблицы.")

    grid = saved["grid"]
    if grid is not None:
        grid = TableGrid(**{
            **grid, "cells": tuple(GridCell(**{**item, "evidence_ids": tuple(item["evidence_ids"])})
                                   for item in grid["cells"]),
            "evidence_ids": tuple(grid["evidence_ids"]), "unplaced_evidence_ids": tuple(grid["unplaced_evidence_ids"]),
        })
    evidence = tuple(Evidence(**{**item, "box": box(item)}) for item in saved["evidence"])
    references = set(saved["context_evidence_ids"])
    if table_data is not None:
        references.update(ref for cell in table_data.cells for ref in cell.header_evidence_ids if ref is not None)
        references.update(ref for cell in table_data.cells for ref in cell.baseline_evidence_ids)
        references.update(ref for cell in table_data.cells for refs in cell.verified_condition_sources.values() for ref in refs)
    if grid is not None:
        references.update((*grid.evidence_ids, *grid.unplaced_evidence_ids))
        references.update(ref for cell in grid.cells for ref in cell.evidence_ids)
        if grid.caption_evidence_id is not None:
            references.add(grid.caption_evidence_id)
    if references.difference(item.id for item in evidence):
        raise ValueError("Сохранённая сетка ссылается на отсутствующие источники.")
    return TableRead(**{
        **saved, "table": table_data, "grid": grid, "evidence": evidence,
        "remarks": tuple(read_remark(item) for item in saved["remarks"]),
        "context_evidence_ids": tuple(saved["context_evidence_ids"]),
    })


def box(item: dict):
    return tuple(item["box"]) if item["box"] is not None else None


def read_cell(item: dict, strategy: str, caption_id: str | None) -> ReadCell:
    fields = item.copy()
    method = fields.pop("method_evidence_id", None)
    verified = fields.get("verified_condition_sources", {})
    if not isinstance(verified, dict) or any(not isinstance(refs, list) or not all(isinstance(ref, str) for ref in refs)
                                            for refs in verified.values()):
        raise ValueError("Некорректные ссылки на проверенные условия ячейки.")
    verified = {field: tuple(refs) for field, refs in verified.items()}
    if method and strategy == "docling-ocr-tableformer" and "verified_condition_sources" not in fields:
        # Older OCR artifacts stored positioned references in the labeled header path.
        verified["method"] = (method,)
        if caption_id:
            verified["experiment"] = (caption_id,)
        for header, reference in zip(fields["headers"], fields["header_evidence_ids"]):
            if reference and (match := LABELED_HEADER.fullmatch(header.strip())):
                role = match[1].lower()
                verified["generator" if role == "model" else role] = (reference,)
    return ReadCell(**{**fields, "headers": tuple(fields["headers"]), "box": box(fields),
                       "row_group": RowGroup(**fields["row_group"]) if fields["row_group"] is not None else None,
                       "header_evidence_ids": tuple(fields["header_evidence_ids"]),
                       "baseline_evidence_ids": tuple(fields["baseline_evidence_ids"]),
                       "verified_condition_sources": verified})


def read_remark(item: dict) -> Remark:
    source = item["source"]
    return Remark(**{
        **item, "source": Source(**{**source, "box": box(source), "headers": tuple(source["headers"]),
                                     "evidence_ids": tuple(source["evidence_ids"])}),
        "condition_sources": {key: tuple(value) for key, value in item["condition_sources"].items()},
        "options": tuple(ConditionOption(option["value"], tuple(option["evidence_ids"])) for option in item["options"]),
        "result_indices": tuple(item.get("result_indices", ())),
    })
