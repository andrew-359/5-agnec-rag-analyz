"""Available readers for a table in the original PDF."""

from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from ..core.domain import Box
    from ..run import TableRead
    from ..table_region import TableRegion


@dataclass(frozen=True)
class TableReader:
    module: str
    description: str
    requires_crop: bool = False
    accepts_region: bool = False

    def validate_crop(self, crop: Box | None, *, from_saved_grid: bool) -> None:
        if self.requires_crop and crop is None and not from_saved_grid:
            raise ValueError("Для pdfplumber укажите --crop X0 TOP X1 BOTTOM.")
        if not self.requires_crop and crop is not None:
            raise ValueError("--crop доступен для стратегии pdfplumber.")

    def read(self, pdf: Path, page: int, table_number: int, crop: Box | None, region: TableRegion | None = None) -> TableRead:
        read_table = import_module(self.module, __package__).read_table
        arguments = {}
        if crop is not None:
            arguments["crop"] = crop
        if region is not None:
            if not self.accepts_region:
                raise ValueError("Выбранный ридер не поддерживает адрес области таблицы.")
            arguments["region"] = region
        return read_table(pdf, page, table_number, **arguments)


DEFAULT_TABLE_READER = "docling-tableformer"
TABLE_READERS = {
    DEFAULT_TABLE_READER: TableReader(".parser", "Сетка TableFormer с текстовым слоем PDF.", accepts_region=True),
    "docling-rapidocr": TableReader(".rapidocr", "Сетка по OCR-координатам и шапке с группами top-k.", accepts_region=True),
    "docling-ocr-tableformer": TableReader(".ocr_tableformer", "Сетка TableFormer с OCR.", accepts_region=True),
    "paddle-structure": TableReader(".paddle_structure", "Экспериментальная OCR-сетка Paddle PP-StructureV3.", accepts_region=True),
    "pdfplumber": TableReader(".pdfplumber", "Сетка по расположению цифрового текста.", requires_crop=True),
    "unstructured": TableReader(".unstructured", "Сетка таблицы через Unstructured."),
}
