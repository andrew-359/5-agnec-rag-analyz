"""Digitize a selected linear-scale PDF panel and retain evidence for each point."""

import argparse
import hashlib
import json
import math
import re
import shlex
import sys
from collections.abc import Callable
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path

import pdfplumber
from pdfplumber.utils import cluster_objects

from .text_pdfplumber import page_text_blocks
from ..inventory import FIGURE_CAPTION, REFERENCE_AFTER_PERIOD, normalized_box
from .plot_qwen import STRATEGY as QWEN_VISION, read_qwen


NUMERIC_LABEL = re.compile(r"[+−-]?(?:\d+(?:\.\d+)?|\.\d+)")
AXIS_EDGE_TOLERANCE = 0.5  # PDF points, for matching short ticks to the supplied plot frame.
# Automatic crop around a framed bar plot: category labels, legend, and axis title.
BAR_PANEL_MARGINS = (30, 42, 5, 35)


def object_box(item):
    return item["x0"], item["top"], item["x1"], item["bottom"]


def contains(box, x, y):
    left, top, right, bottom = box
    return left <= x <= right and top <= y <= bottom


def tick_positions(lines, plot, axis):
    left, top, right, bottom = plot
    if axis == "x":
        ticks = {line["x0"] for line in lines
                 if line["height"] > 0.25 and line["width"] < 0.05
                 and left <= line["x0"] <= right
                 and abs(line["top"] - bottom) <= AXIS_EDGE_TOLERANCE
                 and bottom < line["bottom"] <= bottom + 5}
    else:
        ticks = {line["top"] for line in lines
                 if line["width"] > 0.25 and line["height"] < 0.05
                 and top <= line["top"] <= bottom + AXIS_EDGE_TOLERANCE
                 and abs(line["x1"] - left) <= AXIS_EDGE_TOLERANCE
                 and left - 5 <= line["x0"] < left}

    return sorted(ticks)


def find_plot_frames(page, figure, corrected_box=None):
    captions = []
    if corrected_box is None:
        for block in page_text_blocks(page):
            for line in block:
                match = FIGURE_CAPTION.match(line.text)
                if match and not (match[3] == "." and REFERENCE_AFTER_PERIOD.match(match[4])):
                    captions.append((line, match[2]))
        if sum(number == figure for _, number in captions) != 1:
            raise ValueError("a unique caption for the selected figure was not found")

    region = page if corrected_box is None else page.within_bbox(corrected_box)
    plots = []
    for rect in region.rects:
        if min(rect["width"], rect["height"]) < 25:
            continue
        plot = object_box(rect)
        if any(len(tick_positions(region.lines, plot, axis)) < 2 for axis in ("x", "y")):
            continue
        left, _, right, bottom = plot
        below = [(caption.top, number) for caption, number in captions
                 if caption.top > bottom and caption.left <= right and left <= caption.right]
        if corrected_box is not None or (below and min(below)[1] == figure):
            plots.append(plot)

    return [plot for row in cluster_objects(set(plots), lambda plot: plot[1], tolerance=2)
            for plot in sorted(row)]


def detect_panel(page, figure, panel, corrected_box=None):
    ordered = find_plot_frames(page, figure, corrected_box)
    if panel.isdecimal():
        index = int(panel)
    else:
        index = ord(panel.lower()) - ord("a") + 1 if re.fullmatch("[a-zA-Z]", panel) else 0
    if corrected_box is not None:
        if len(ordered) != 1:
            raise ValueError(f"corrected region must contain one frame, found {len(ordered)}")
        return corrected_box, ordered[0], 1
    if not 1 <= index <= len(ordered):
        raise ValueError(f"panel {panel!r} could not be selected from {len(ordered)} detected frames")
    plot = ordered[index - 1]
    left, top, right, bottom = plot
    panel_box = max(left - 32, 0), max(top - 16, 0), min(right + 3, page.width), min(bottom + 18, page.height)
    return panel_box, plot, len(ordered)


def axis_calibration(lines, words, plot, axis, supplied, fallback):
    left, _, _, bottom = plot
    ticks = tick_positions(lines, plot, axis)
    label_source = "manual" if supplied is not None else "pdf_text"
    if supplied is not None:
        if len(ticks) != len(supplied):
            raise ValueError(f"{axis}: found {len(ticks)} ticks, supplied {len(supplied)} labels")
        anchors = [{"position_pt": pos, "value": value} for pos, value in zip(ticks, supplied)]
    else:
        if axis == "x":
            labels = [((word["x0"] + word["x1"]) / 2, word)
                      for word in words if bottom <= word["top"] <= bottom + 11]
        else:
            labels = [((word["top"] + word["bottom"]) / 2, word)
                      for word in words if left - 15 <= word["x1"] <= left]
        anchors = []
        for pos in ticks:
            matches = [word for center, word in labels
                       if abs(center - pos) <= 2 and NUMERIC_LABEL.fullmatch(word["text"])]
            if len(matches) == 1:
                word = matches[0]
                anchors.append({"position_pt": pos, "value": float(word["text"].replace("−", "-")),
                                "label_box_pt": object_box(word)})
        if len(anchors) != len(ticks):
            anchors = fallback(axis, ticks)
            label_source = "pdfium_text"

    if len(anchors) < 2:
        raise ValueError(f"{axis}: at least two labeled ticks are required")
    start, end = anchors[0], anchors[-1]
    slope = (end["value"] - start["value"]) / (end["position_pt"] - start["position_pt"])
    if not math.isfinite(slope) or slope == 0:
        raise ValueError(f"{axis}: invalid linear scale")
    intercept = start["value"] - slope * start["position_pt"]
    residual = max(abs(slope * tick["position_pt"] + intercept - tick["value"]) for tick in anchors)
    if residual > abs(slope) * AXIS_EDGE_TOLERANCE:
        raise ValueError(f"{axis}: tick labels disagree with a linear scale")
    return {"label_source": label_source,
            "scale": "linear", "ticks": anchors, "slope_per_pt": slope, "intercept": intercept,
            "max_tick_residual": residual}


def pdfium_ticks(path, page_number, page, plot, axis, ticks, panel=None):
    import pypdfium2

    if page.rotation:
        raise ValueError(f"{axis}: PDFium label fallback requires an unrotated page")
    left, _, _, bottom = plot
    anchors = []
    with pypdfium2.PdfDocument(path) as document:
        text_page = document[page_number - 1].get_textpage()
        for position in ticks:
            if axis == "x":
                box = (position - 5, bottom + 1, position + 5, bottom + 12)
            else:
                box = (left - 25, position - 4.5, left - 1, position + 4.5)
            x0, y0, x1, y1 = box
            if panel is not None:
                x0, y0, x1, y1 = max(x0, panel[0]), max(y0, panel[1]), min(x1, panel[2]), min(y1, panel[3])
                if x0 >= x1 or y0 >= y1:
                    raise ValueError(f"{axis}: label for the tick is outside the selected panel")
                box = (x0, y0, x1, y1)
            text = text_page.get_text_bounded(x0, page.height - y1, x1, page.height - y0).strip()
            if not NUMERIC_LABEL.fullmatch(text):
                raise ValueError(f"{axis}: tick at {position:.3f} pt has no unambiguous numeric label")
            anchors.append({"position_pt": position, "value": float(text.replace("−", "-")),
                            "label_search_box_pt": box})
    return anchors


def line_points(curves, plot, color):
    paths = []
    for curve in curves:
        if not curve["stroke"] or curve["fill"]:
            continue
        if color is not None and (not isinstance(curve["stroking_color"], tuple)
                                  or len(curve["stroking_color"]) != 3
                                  or any(abs(a - b / 255) > 0.001
                                         for a, b in zip(curve["stroking_color"], color))):
            continue
        path = curve["path"]
        if (len(path) >= 3 and path[0][0] == "m" and all(op == "l" for op, *_ in path[1:])
                and all(contains(plot, *point) for _, point in path)):
            points = [point for _, point in path]
            if all(a[0] < b[0] for a, b in zip(points, points[1:])):
                paths.append(points)
    if len(paths) != 1:
        raise ValueError(f"expected one matching data polyline, found {len(paths)}")
    return [(x, y, (x - 2, y - 2, x + 2, y + 2)) for x, y in paths[0]]


def scatter_points(curves, plot, marker_size):
    points = []
    for curve in curves:
        if not curve["fill"] or tuple(op for op, *_ in curve["path"]) != ("m",) + ("c",) * 8 + ("h",):
            continue
        left, top, right, bottom = box = object_box(curve)
        if abs(right - left - marker_size) > 0.1 or abs(bottom - top - marker_size) > 0.1:
            continue
        x, y = (left + right) / 2, (top + bottom) / 2
        if contains(plot, x, y):
            points.append((x, y, box))
    if not points:
        raise ValueError("no matching round vector markers found")
    return points


def read_vector(page, args, output, evidence, find_points):
    if not page.chars and page.images and not (page.lines or page.curves):
        raise ValueError("image-only page: raster chart extraction is required")
    if args.plot_box is None:
        panel_box, plot, count = detect_panel(page, args.figure, args.panel, args.panel_box)
        output["region"] = {
            "selection": "automatic" if args.panel_box is None else "corrected",
            "detected_panels": count,
        }
    else:
        panel_box, plot = args.panel_box, args.plot_box
        output["region"] = {"selection": "manual"}
        if hasattr(args, "frozen_panel_count"):
            output["region"]["detected_panels"] = args.frozen_panel_count
    left, top, right, bottom = panel_box
    x0, y0, x1, y1 = plot
    if not (0 <= left <= x0 < x1 <= right <= page.width
            and 0 <= top <= y0 < y1 <= bottom <= page.height):
        raise ValueError("plot box must fit in the panel, and panel must fit on the PDF page")
    output["source"] = evidence(panel_box)
    output["region"]["plot_box_pt"] = plot
    panel = page.within_bbox(panel_box)

    def fallback(axis, ticks):
        panel_limit = panel_box if getattr(args, "restrict_tick_labels", False) else None
        anchors = pdfium_ticks(args.pdf, args.page, page, plot, axis, ticks, panel=panel_limit)
        output["engine"]["text_fallback"] = {"name": "pypdfium2", "version": version("pypdfium2")}
        return anchors

    words = panel.extract_words() if args.x_ticks is None or args.y_ticks is None else []
    x_axis = axis_calibration(panel.lines, words, plot, "x", args.x_ticks, fallback)
    y_axis = axis_calibration(panel.lines, words, plot, "y", args.y_ticks, fallback)
    output["axes"] = {"x": {"name": args.x_axis, **x_axis}, "y": {"name": args.y_axis, **y_axis}}
    for index, (x, y, box) in enumerate(find_points(panel.curves, plot), 1):
        output["points"].append({
            "index": index, "position_pt": [x, y], "approximate": True,
            "x": round(x_axis["slope_per_pt"] * x + x_axis["intercept"], 8),
            "y": round(y_axis["slope_per_pt"] * y + y_axis["intercept"], 8),
            "source": evidence(box),
        })


def read_vector_line(page, args, output, evidence):
    read_vector(page, args, output, evidence, lambda curves, plot: line_points(curves, plot, args.stroke_rgb))


def read_vector_scatter(page, args, output, evidence):
    read_vector(page, args, output, evidence, lambda curves, plot: scatter_points(curves, plot, args.marker_size))


def read_vector_bar(page, args, output, evidence):
    from .plot_bars import filled_rectangles, pdf_labels, read_labeled_bars

    if page.rotation:
        raise ValueError("rotated PDF pages are not supported for grouped bars")
    if args.plot_box is None:
        panel, frame, count = detect_panel(page, args.figure, args.panel, args.panel_box)
        selection = "automatic" if args.panel_box is None else "corrected"
        if args.panel_box is None:
            left, top, right, bottom = frame
            margin_left, margin_top, margin_right, margin_bottom = BAR_PANEL_MARGINS
            panel = (max(left - margin_left, 0), max(top - margin_top, 0),
                     min(right + margin_right, page.width), min(bottom + margin_bottom, page.height))
    else:
        panel, frame, count, selection = args.panel_box, args.plot_box, getattr(args, "frozen_panel_count", 1), "manual"
    left, top, right, bottom = panel
    if not (0 <= left <= frame[0] < frame[2] <= right <= page.width
            and 0 <= top <= frame[1] < frame[3] <= bottom <= page.height):
        raise ValueError("plot box must fit in the panel, and panel must fit on the PDF page")
    output["source"] = evidence(panel)
    output["region"] = {"selection": selection, "detected_panels": count, "plot_box_pt": frame}
    output["engine"]["text"] = {"name": "pypdfium2", "version": version("pypdfium2")}

    read = read_labeled_bars(
        filled_rectangles(page.rects), pdf_labels(args.pdf, args.page, panel), panel, frame,
        (tick_positions(page.lines, frame, "x"), tick_positions(page.lines, frame, "y")),
        NUMERIC_LABEL.fullmatch,
    )
    output["metric"] = {"text": read.metric.text, "source": evidence(read.metric.box)} if read.metric else None
    output["scale"] = read.scale
    output["issues"] = [
        {"message": "Не подтверждены подпись значения, группа, серия или метрика столбца.", "source": evidence(box)}
        for box in read.unresolved
    ]
    output["points"] = [{
        "index": index, "x": bar.category_index, "y": float(bar.value.text.replace("−", "-")),
        "approximate": False, "raw_value": bar.value.text,
        "position_pt": [bar.value.center(0), bar.value.center(1)],
        "series": bar.series.text, "category": bar.category.text,
        "source": evidence(bar.value.box),
        "series_source": evidence(bar.series.box), "category_source": evidence(bar.category.box),
    } for index, bar in enumerate(read.readings, 1)]


def read_raster_line(page, args, output, evidence):
    from .plot_raster import read_line

    config = (args.raster_config if isinstance(args.raster_config, dict)
              else json.loads(args.raster_config.read_text(encoding="utf-8")))
    output["settings"]["raster"] = config
    observations, axes, transform, issues = read_line(page, config)
    xs, ys = zip(*transform["corners_pt"])
    output["source"] = evidence((min(xs), min(ys), max(xs), max(ys)))
    output["region"] = {"selection": "manual", **transform}
    output["axes"] = {"x": {"name": args.x_axis, **axes["x"]}, "y": {"name": args.y_axis, **axes["y"]}}
    for index, point in enumerate(observations, 1):
        source = evidence(point.pop("box_pt"))
        output["points"].append({"index": index, "approximate": True, **point, "source": source})
    for issue in issues:
        box = issue.pop("box_pt", None)
        output["issues"].append({**issue, "source": evidence(box) if box else output["source"]})


@dataclass(frozen=True)
class PlotReader:
    read: Callable
    help: str
    parameters_schema: dict
    requires_marker_size: bool = False
    requires_raster_config: bool = False

    def validate(self, args):
        if self.requires_raster_config != (getattr(args, "raster_config", None) is not None):
            raise ValueError("--raster-config is required for raster-line and only applies to that strategy")
        if args.plot_box is not None and args.panel_box is None:
            raise ValueError("--plot-box requires --panel-box")
        if self.requires_raster_config and args.panel_box is not None:
            raise ValueError("raster frame is set by --raster-config")
        if args.stroke_rgb is not None and any(not 0 <= c <= 255 for c in args.stroke_rgb):
            raise ValueError("--stroke-rgb channels must be in 0..255")
        if any(not math.isfinite(tick) for ticks in (args.x_ticks, args.y_ticks) for tick in ticks or []):
            raise ValueError("manual tick labels must be finite")
        if self.requires_marker_size and not (args.marker_size and 0 < args.marker_size < math.inf):
            raise ValueError("vector-scatter requires a positive --marker-size")


def figure_parameters(properties=None, required=(), *, labels=True):
    return {
        "type": "object", "additionalProperties": False,
        "properties": {
            "panel": {"type": "string", "pattern": "^(?:[1-9][0-9]*|[a-zA-Z])$"},
            **({"series": {"type": "string"}, "x_axis": {"type": "string"}, "y_axis": {"type": "string"}}
               if labels else {}),
            **(properties or {}),
        },
        "required": list(required),
    }


NUMBER_LIST = {"type": "array", "items": {"type": "number"}, "minItems": 2}
VECTOR_PARAMETERS = {"x_ticks": NUMBER_LIST, "y_ticks": NUMBER_LIST}
RASTER_CONFIG_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "corners_pt": {"type": "array", "minItems": 4, "maxItems": 4,
                       "items": {"type": "array", "minItems": 2, "maxItems": 2, "items": {"type": "number"}}},
        "x_ticks": {"type": "array", "minItems": 2,
                    "items": {"type": "array", "minItems": 3, "maxItems": 3, "items": {"type": "number"}}},
        "y_ticks": {"type": "array", "minItems": 2,
                    "items": {"type": "array", "minItems": 3, "maxItems": 3, "items": {"type": "number"}}},
        "sample_x": {"type": "array", "minItems": 1, "items": {"type": "number"}},
        "max_gray": {"type": "integer", "minimum": 1, "maximum": 255},
    },
    "required": ["corners_pt", "x_ticks", "y_ticks", "sample_x", "max_gray"],
}


PLOT_READERS = {
    QWEN_VISION: PlotReader(read_qwen, "Sends a figure crop to the worker-local Ollama.",
                           figure_parameters(labels=False)),
    "vector-line": PlotReader(read_vector_line, "Reads vertices from a vector polyline.", figure_parameters({
        **VECTOR_PARAMETERS,
        "stroke_rgb": {"type": "array", "minItems": 3, "maxItems": 3,
                       "items": {"type": "integer", "minimum": 0, "maximum": 255}},
    })),
    "vector-bar": PlotReader(read_vector_bar, "Reads exact labels on grouped vector bars.", figure_parameters(labels=False)),
    "vector-scatter": PlotReader(
        read_vector_scatter, "Requires a positive --marker-size in PDF points.",
        figure_parameters({**VECTOR_PARAMETERS, "marker_size": {"type": "number", "exclusiveMinimum": 0}},
                          required=("marker_size",)), requires_marker_size=True,
    ),
    "raster-line": PlotReader(
        read_raster_line, "Requires --raster-config with frame, scale and samples.",
        figure_parameters({"raster_config": RASTER_CONFIG_SCHEMA}, required=("raster_config",)),
        requires_raster_config=True,
    ),
}


def extract(args, command, *, private_artifacts=None):
    reader = PLOT_READERS[args.strategy]
    reader.validate(args)
    settings = {key: value for key, value in vars(args).items() if key not in {"pdf", "out", "raster_config", "preview"}}
    with args.pdf.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    output = {
        "version": 1, "command": command,
        "pdf": str(args.pdf.resolve()), "sha256": digest,
        "engine": {"name": "pdfplumber", "version": version("pdfplumber")},
        "settings": settings, "points": [], "issues": [],
    }
    with pdfplumber.open(args.pdf) as document:
        if not 1 <= args.page <= len(document.pages):
            raise ValueError("PDF page out of range")
        page = document.pages[args.page - 1]

        def evidence(box):
            item = dict(zip(("x0", "top", "x1", "bottom"), box))
            return {"page": args.page, "box_pt": box, "box": normalized_box(item, page.width, page.height),
                    "url": f"{args.pdf.resolve().as_uri()}#page={args.page}"}

        output["source"] = evidence(args.panel_box or (0, 0, page.width, page.height))
        try:
            reader.read(page, args, output, evidence)
        except ValueError as error:
            output["status"] = "not_applicable"
            output["reason"] = str(error)
            output["issues"].append({"message": str(error), "source": output["source"]})
    private = output.pop("private_artifacts", {})
    if private_artifacts is not None:
        private_artifacts.update(private)
    output.setdefault("status", "succeeded" if output["points"] else "no_observations")
    return output


def main():
    requirements = "\n".join(f"  {name:<16} {reader.help}" for name, reader in PLOT_READERS.items())
    args_parser = argparse.ArgumentParser(
        description=__doc__, epilog=f"Strategies:\n{requirements}",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    args_parser.add_argument("pdf", type=Path)
    args_parser.add_argument("--page", type=int, required=True, help="1-based PDF page")
    args_parser.add_argument("--figure", required=True)
    args_parser.add_argument("--panel", required=True)
    args_parser.add_argument("--series", required=True)
    args_parser.add_argument("--panel-box", type=float, nargs=4,
                             help="Panel with tick labels, four PDF points from top left")
    args_parser.add_argument("--plot-box", type=float, nargs=4,
                             help="Data frame, four PDF points from top left")
    args_parser.add_argument("--x-axis", required=True)
    args_parser.add_argument("--y-axis", required=True)
    args_parser.add_argument("--x-ticks", type=float, nargs="+", help="Manual values, ordered left to right")
    args_parser.add_argument("--y-ticks", type=float, nargs="+", help="Manual values, ordered top to bottom")
    args_parser.add_argument("--strategy", choices=PLOT_READERS, required=True)
    args_parser.add_argument("--stroke-rgb", type=int, nargs=3,
                             metavar=("R", "G", "B"))
    args_parser.add_argument("--marker-size", type=float, help="Round marker diameter in PDF points")
    args_parser.add_argument("--raster-config", type=Path, help="Raster quadrilateral, scale landmarks and samples in JSON")
    args_parser.add_argument("--preview", type=Path, help="Write a local HTML report with highlightable PDF points")
    args_parser.add_argument("--out", type=Path, required=True)
    args = args_parser.parse_args()
    try:
        result = extract(args, shlex.join(["python", "-m", "rag_mvp.plot_extract", *sys.argv[1:]]))
    except (OSError, ValueError) as error:
        args_parser.error(str(error))
    payload = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.out == Path("-"):
        sys.stdout.write(payload)
    else:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(payload, encoding="utf-8")
    if args.preview:
        from ..plot_preview import write_preview

        write_preview(result, args.preview)
    return 2 if result["issues"] else 0
