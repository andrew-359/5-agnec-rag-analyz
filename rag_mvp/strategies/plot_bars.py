"""Read exact labels on grouped PDF bars; geometry establishes their identity."""

from collections import Counter
from ctypes import addressof
from dataclasses import dataclass
from itertools import groupby
from math import dist, isclose
from typing import Callable

import pypdfium2


from ..core.domain import Box


Color = tuple[float, ...] | float | str
IsNumber = Callable[[str], object]
EDGE_TOLERANCE_PT = 0.5
# Transparent fills can differ from their opaque legend swatches.
LEGEND_COLOR_DISTANCE = 0.15


@dataclass(frozen=True)
class Label:
    text: str
    box: Box

    def center(self, axis: int) -> float:
        return (self.box[axis] + self.box[axis + 2]) / 2


@dataclass(frozen=True)
class Bar:
    box: Box
    color: Color


@dataclass(frozen=True)
class BarReading:
    value: Label
    series: Label
    category: Label
    category_index: int


@dataclass(frozen=True)
class BarPanel:
    readings: list[BarReading]
    unresolved: list[Box]
    metric: Label | None
    scale: list[float]


def pdf_labels(pdf, page_number: int, panel: Box) -> list[Label]:
    # Group by PDF text object, not extracted line order: rotated category names
    # and neighboring legend labels must stay separate.
    with pypdfium2.PdfDocument(pdf) as document:
        page = document[page_number - 1]
        text = page.get_textpage()
        height = page.get_height()
        labels = []
        for obj, indices in groupby(range(text.count_chars()), lambda i: _text_object(text, i)):
            if obj is None:
                continue
            first, *rest = indices
            last = rest[-1] if rest else first
            boxes = [text.get_charbox(i) for i in range(first, last + 1)]
            left, bottom, right, top = zip(*boxes, strict=True)
            box = (min(left), height - max(top), max(right), height - min(bottom))
            if _inside(box, panel):
                label = text.get_text_range(first, last - first + 1).strip()
                if label:
                    labels.append(Label(label, box))
        return labels


def _text_object(text, index):
    obj = text.get_textobj(index)
    return addressof(obj.raw.contents) if obj is not None else None


def _inside(box: Box, boundary: Box) -> bool:
    x0, y0, x1, y1 = box
    left, top, right, bottom = boundary
    return left <= x0 < x1 <= right and top <= y0 < y1 <= bottom


def filled_rectangles(rectangles) -> list[Bar]:
    return [Bar((rect["x0"], rect["top"], rect["x1"], rect["bottom"]), rect["non_stroking_color"])
            for rect in rectangles if rect["fill"] and rect["non_stroking_color"] is not None]


def _bar_direction(rectangles: list[Bar], frame: Box) -> tuple[int, list[Bar]]:
    left, top, right, bottom = frame
    # Clipping can hide the true baseline beyond the frame (e.g. a cropped axis).
    horizontal = [bar for bar in rectangles if
                  bar.box[0] <= left + EDGE_TOLERANCE_PT < bar.box[2] < right
                  and top <= bar.box[1] < bar.box[3] <= bottom]
    vertical = [bar for bar in rectangles if
                bar.box[3] >= bottom - EDGE_TOLERANCE_PT > bar.box[1] > top
                and left <= bar.box[0] < bar.box[2] <= right]
    if bool(horizontal) == bool(vertical):
        raise ValueError("no unambiguous baseline for grouped vector bars")
    return (1, horizontal) if horizontal else (0, vertical)


def _near_color(first: Color, second: Color) -> bool:
    if first == second:
        return True
    return (isinstance(first, tuple) and isinstance(second, tuple)
            and len(first) == len(second) == 3 and dist(first, second) <= LEGEND_COLOR_DISTANCE)


def _legend(
    rectangles: list[Bar], bars: list[Bar], labels: list[Label], panel: Box, is_number: IsNumber,
) -> dict[Color, Label]:
    legend: dict[Color, list[tuple[bool, Label]]] = {}
    colors = {bar.color for bar in bars}
    for swatch in rectangles:
        x0, y0, x1, y1 = swatch.box
        if swatch in bars or not _inside(swatch.box, panel):
            continue
        matched = ([swatch.color] if swatch.color in colors
                   else [color for color in colors if _near_color(swatch.color, color)])
        if len(matched) != 1:
            continue
        nearby = [label for label in labels if not is_number(label.text)
                  and x1 <= label.box[0] <= x1 + x1 - x0 and y0 <= label.center(1) <= y1]
        if len(nearby) == 1:
            color, = matched
            legend.setdefault(color, []).append((swatch.color == color, nearby[0]))
    unique = {}
    for color, entries in legend.items():
        exact = [label for same, label in entries if same]
        labels = exact or [label for _, label in entries]
        if len(labels) == 1:
            unique[color] = labels[0]
    counts = Counter(unique.values())
    return {color: label for color, label in unique.items() if counts[label] == 1}


def _axis_labels(labels: list[Label], frame: Box, category_axis: int) -> tuple[list[Label], list[Label]]:
    left, top, right, bottom = frame
    below = [label for label in labels if label.box[1] >= bottom and left <= label.center(0) <= right]
    beside = [label for label in labels if label.box[2] <= left and top <= label.center(1) <= bottom]
    return (below, beside) if category_axis == 0 else (beside, below)


def _category_label(labels: list[Label], axis: int) -> Label | None:
    if not labels:
        return None
    ordered = sorted(labels, key=lambda label: label.box[1])
    for previous, current in zip(ordered, ordered[1:]):
        # Extra lines must form one continuous label at the tick, not an axis title
        # or nearby prose. Vertical (rotated) tick labels are single PDF objects.
        gap = current.box[1] - previous.box[3]
        height = previous.box[3] - previous.box[1]
        if axis == 1 or not 0 <= gap <= height:
            return None
    left, top, right, bottom = zip(*(label.box for label in ordered), strict=True)
    return Label(" ".join(label.text for label in ordered), (min(left), min(top), max(right), max(bottom)))


def _at_tip(label: Label, bar: Bar, category_axis: int) -> bool:
    left, top, right, bottom = label.box
    center = label.center(category_axis)
    if not bar.box[category_axis] <= center <= bar.box[category_axis + 2]:
        return False
    gap = left - bar.box[2] if category_axis == 1 else bar.box[1] - bottom
    return -EDGE_TOLERANCE_PT <= gap <= bottom - top


def _group_for(bar: Bar, ticks: list[float], anchors: dict[float, Label], axis: int) -> tuple[int, Label] | None:
    center = (bar.box[axis] + bar.box[axis + 2]) / 2
    ranked = sorted((abs(center - tick), index, tick) for index, tick in enumerate(ticks, 1))
    if not ranked or (len(ranked) > 1 and isclose(ranked[0][0], ranked[1][0], abs_tol=EDGE_TOLERANCE_PT)):
        return None
    _, index, tick = ranked[0]
    return (index, anchors[tick]) if tick in anchors else None


def read_labeled_bars(
    rectangles: list[Bar], labels: list[Label], panel: Box, frame: Box,
    ticks: tuple[list[float], list[float]], is_number: IsNumber,
) -> BarPanel:
    axis, bars = _bar_direction(rectangles, frame)
    legend = _legend(rectangles, bars, labels, panel, is_number)
    categories, metric_labels = _axis_labels(labels, frame, axis)
    names = [label for label in categories if not is_number(label.text)]
    category_ticks = ticks[axis]
    candidates = {tick: [label for label in names if label.box[axis] <= tick <= label.box[axis + 2]]
                  for tick in category_ticks}
    uses = Counter(label for group in candidates.values() for label in group)
    anchors = {}
    for tick, candidates_at_tick in candidates.items():
        unique = [label for label in candidates_at_tick if uses[label] == 1]
        if label := _category_label(unique, axis):
            anchors[tick] = label
    metrics = [label for label in metric_labels if not is_number(label.text)]
    metric = metrics[0] if len(metrics) == 1 else None
    numbers = [label for label in labels if is_number(label.text)]
    matches = [(bar, [label for label in numbers if _at_tip(label, bar, axis)]) for bar in bars]
    number_uses = Counter(label for _, found in matches for label in found)
    groups = [_group_for(bar, category_ticks, anchors, axis) for bar in bars]
    series_uses = Counter((group, bar.color) for bar, group in zip(bars, groups, strict=True))
    readings, unresolved = [], []
    for (bar, values), group in zip(matches, groups, strict=True):
        series = legend.get(bar.color)
        number = values[0] if len(values) == 1 and number_uses[values[0]] == 1 else None
        if not all((group, series, number, metric)) or series_uses[group, bar.color] != 1:
            issue_box = number.box if number else (
                max(bar.box[0], frame[0]), max(bar.box[1], frame[1]),
                min(bar.box[2], frame[2]), min(bar.box[3], frame[3]),
            )
            unresolved.append(issue_box)
            continue
        index, category = group
        readings.append(BarReading(number, series, category, index))
    scale = [float(label.text.replace("−", "-"))
             for label in metric_labels if is_number(label.text)]
    return BarPanel(readings, unresolved, metric, scale)
