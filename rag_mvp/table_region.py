"""Table coordinates in fractions of the original PDF page, starting at top left."""

from dataclasses import dataclass
import json
from math import isfinite

from .article_context.captions import table_caption_line
from .core.domain import Box


def _box(value) -> Box:
    if not isinstance(value, (list, tuple)) or len(value) != 4 or not all(
        isinstance(coordinate, (int, float)) and not isinstance(coordinate, bool) and isfinite(coordinate)
        for coordinate in value
    ):
        raise ValueError("Область таблицы должна содержать четыре конечные координаты.")
    left, top, right, bottom = value
    if not (0 <= left < right <= 1 and 0 <= top < bottom <= 1):
        raise ValueError("Область таблицы должна находиться внутри страницы PDF.")
    return left, top, right, bottom


@dataclass(frozen=True)
class TableRegion:
    box: Box
    bounds_status: str
    caption_status: str
    designation: str | None
    caption_box: Box | None
    caption_text: str | None
    alternative_boxes: tuple[Box, ...] = ()

    @classmethod
    def parse(cls, payload: dict) -> "TableRegion":
        if not isinstance(payload, dict):
            raise ValueError("Описание области таблицы должно быть объектом.")
        if payload.get("bounds_status") not in ("confirmed", "needs_review"):
            raise ValueError("Неизвестный статус границ области.")
        if payload.get("caption_status") not in ("confirmed", "missing", "needs_review"):
            raise ValueError("Неизвестный статус подписи области.")
        designation, caption_text = payload.get("designation"), payload.get("caption_text")
        if any(value is not None and (not isinstance(value, str) or not value.strip())
               for value in (designation, caption_text)):
            raise ValueError("Обозначение и подпись таблицы должны быть текстом.")
        caption_box = payload.get("caption_box")
        alternatives = payload.get("alternative_boxes", ())
        if not isinstance(alternatives, (list, tuple)):
            raise ValueError("Альтернативные границы должны быть списком областей.")
        return cls(_box(payload.get("box")), payload["bounds_status"], payload["caption_status"],
                   designation, _box(caption_box) if caption_box is not None else None, caption_text,
                   tuple(_box(bounds) for bounds in alternatives))

    @classmethod
    def from_json(cls, value: str) -> "TableRegion":
        return cls.parse(json.loads(value))

    @property
    def author_label(self) -> str | None:
        if self.caption_status == "confirmed" and self.designation:
            match = table_caption_line(self.designation)
            return match["label"] if match else None
        return None


def overlap(first: Box, second: Box) -> float:
    return max(0, min(first[2], second[2]) - max(first[0], second[0])) * max(
        0, min(first[3], second[3]) - max(first[1], second[1]))


def area(box: Box) -> float:
    return (box[2] - box[0]) * (box[3] - box[1])


def shared_area(first: Box, second: Box) -> float:
    intersection = overlap(first, second)
    return intersection / (area(first) + area(second) - intersection)
