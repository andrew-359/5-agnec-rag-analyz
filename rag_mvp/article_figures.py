"""Read supported vector plots and attach their results to an article figure."""

import argparse
from dataclasses import asdict
import hashlib
import json
import math
import os
from pathlib import Path
import re
from tempfile import TemporaryDirectory

import pdfplumber
import pypdfium2

from .article_context.figure_selection import (
    SENSITIVITY_CAPTION, PreparedFigureContext, continuous_text, load_figure_context, prepare_figure_context,
)
from .article_context.selection import CONTEXT_ARTIFACT
from .core.domain import FigureSource, Result
from .strategies.figure_readers import prepare_target, validate_selection
from .inventory import normalized_box
from .strategies.plot_extract import AXIS_EDGE_TOLERANCE, contains, extract, find_plot_frames, object_box
from .run import IncompatibleArtifact
from .run_output import write_json


PANEL_TITLE = re.compile(r"\(([a-z])\)\s+(.+)", re.I)
COUNT_AXIS = re.compile(r"Number of ([\w ]+)", re.I)
EXACT_MATCH_AXIS = re.compile(r"(?m)^Exact Match \(EM\)$", re.I)
EXACT_MATCH_NAME = re.compile(r"Exact Match(?:\s+\(EM\))?|EM", re.I)


def bounded_text(text, height, box, bounds=None):
    left, top, right, bottom = box
    if bounds is not None:
        left, top, right, bottom = (max(left, bounds[0]), max(top, bounds[1]),
                                    min(right, bounds[2]), min(bottom, bounds[3]))
        if left >= right or top >= bottom:
            return ""
    return text.get_text_bounded(left, height - bottom, right, height - top).strip()


def plot_evidence(identifier, page, text, box, precision):
    return {"id": identifier, "page": page, "text": text, "box": box, "precision": precision}


def digitize_panel(pdf, candidate, panel, method, x_axis):
    args = argparse.Namespace(
        pdf=pdf, page=candidate["page"], figure=candidate["label"], panel=panel, series=method,
        x_axis=x_axis, y_axis="Exact Match (EM)", strategy="vector-line",
        panel_box=None, plot_box=None, x_ticks=None, y_ticks=None, stroke_rgb=None,
    )
    return extract(args, None)


def has_single_trace(page, plot, points):
    left, top, right, bottom = plot

    def enclosed(objects):
        for item in objects:
            x0, y0, x1, y1 = box = object_box(item)
            if contains(plot, x0, y0) and contains(plot, x1, y1):
                yield item, box

    curves = list(enclosed(page.curves))
    if sum(curve["stroke"] and not curve["fill"] for curve, _ in curves) != 1:
        return False
    if any(enclosed(page.images)):
        return False
    for _, (x0, y0, x1, y1) in enclosed(page.lines):
        vertical_grid = x0 == x1 and (y0, y1) == (top, bottom)
        horizontal_grid = y0 == y1 and (x0, x1) == (left, right)
        if not (vertical_grid or horizontal_grid):
            return False
    # Any other visible object must be a small marker of the selected trace.
    markers = [(x, y) for x, y in (point["position_pt"] for point in points)]
    spacing = min((b[0] - a[0] for a, b in zip(markers, markers[1:])), default=0)
    decorated = set()
    shapes = [(curve, box) for curve, box in curves if curve["fill"]]
    shapes.extend((rect, box) for rect, box in enclosed(page.rects) if box != plot)
    for _, (x0, y0, x1, y1) in shapes:
        center = ((x0 + x1) / 2, (y0 + y1) / 2)
        if max(x1 - x0, y1 - y0) >= spacing / 2:
            return False
        position = next((point for point in markers if math.dist(point, center) <= AXIS_EDGE_TOLERANCE), None)
        if position is None or position in decorated:
            return False
        decorated.add(position)
    return True


def bind_panel(artifact, evidence, remark, candidate, experiment, panel_id, title, parameter, read):
    x_axis, y_axis = read["axes"]["x"], read["axes"]["y"]
    if not all(0 <= tick["value"] <= 1 for tick in y_axis["ticks"]):
        remark("indeterminate-axis", "Деления шкалы EM выходят за пределы шкалы долей.", panel_id)
        return
    # Round to a decimal step no finer than the geometric reading tolerance.
    tolerance = abs(y_axis["slope_per_pt"]) * AXIS_EDGE_TOLERANCE + y_axis["max_tick_residual"]
    places = math.floor(-math.log10(tolerance))
    resolution = 10.0 ** -places
    for point in read["points"]:
        x, y = point["x"], point["y"]
        ticks = [tick for tick in x_axis["ticks"]
                 if abs(tick["position_pt"] - point["position_pt"][0]) <= AXIS_EDGE_TOLERANCE]
        if len(ticks) != 1 or ticks[0]["value"] < 0 or not ticks[0]["value"].is_integer() or not 0 <= y <= 1:
            remark("indeterminate-point", f"Положение точки ({x:g}, {y:g}) не устанавливает параметр и значение EM.", panel_id)
            continue
        count = int(ticks[0]["value"])
        value = f"{round(y, places):.{max(places, 0)}f}"
        box = point["source"]["box"]
        point_id = f"{panel_id}-point-{point['index']}"
        address = FigureSource(candidate["page"], candidate["label"], title, box, (point_id,), (title, f"{parameter}={count}"))
        evidence[point_id] = plot_evidence(point_id, candidate["page"], title, box, "plot_point")
        result = Result(
            method=experiment.method, generator=experiment.generator, dataset=experiment.dataset, metric="EM",
            task=experiment.task, experiment=experiment.description, protocol=experiment.protocol, raw_value=f"≈{value}",
            value=value, unit="fraction", statistic=None, retrieval_top_k=None, source=address,
            condition_sources={
                "method": (experiment.context.id,), "generator": (experiment.context.id,),
                "dataset": (candidate["id"], experiment.context.id), "protocol": (experiment.setup.id,),
                parameter: (panel_id,),
            },
        )
        artifact["results"].append({
            **result.to_dict(), "id": point_id, "source_fragment": title,
            "parameters": {parameter: count},
            "digitization": {"method": "vector-line", "resolution": resolution, "series": experiment.method},
        })


def read_panels(pdf, candidate, experiment, artifact, evidence, remark, selected=None):
    page_number, figure = candidate["page"], candidate["label"]
    with pdfplumber.open(pdf) as document, pypdfium2.PdfDocument(pdf) as pdfium:
        page = document.pages[page_number - 1]
        if page.rotation:
            remark("unsupported-figure", "Автоматический выбор подписей осей не поддерживает повёрнутую страницу.")
            return
        if selected is None:
            try:
                frames = find_plot_frames(page, figure)
            except ValueError as error:
                remark("unsupported-figure", f"Рисунок не дал распознаваемую панель. {error}")
                return
            indexed_frames = list(enumerate(frames, 1))
        else:
            indexed_frames = [(selected["index"], selected["plot_box_pt"])]
        if not indexed_frames:
            remark("unsupported-figure", "По подписи рисунка не найдены векторные рамки с делениями.")
            return
        text = pdfium[page_number - 1].get_textpage()
        context = continuous_text(experiment.context.text)
        for index, (left, top, right, bottom) in indexed_frames:
            bounds = selected.get("panel_box_pt") if selected is not None else None
            title = bounded_text(text, page.height, (left - 10, top - 14, right + 8, top - 1), bounds)
            x_axis = bounded_text(text, page.height, (left - 8, bottom + 10, right + 8, bottom + 25), bounds)
            y_axis = bounded_text(text, page.height, (left - 32, top, left - 15, bottom), bounds).replace("\r", "")
            panel_id = f"{candidate['id']}-panel-{index}"
            region = bounds or (left - 32, top - 14, right + 8, bottom + 25)
            panel_box = normalized_box(dict(zip(("x0", "top", "x1", "bottom"), region)), page.width, page.height)
            evidence[panel_id] = plot_evidence(panel_id, page_number, title or candidate["text"], panel_box, "figure")
            panel, parameter = PANEL_TITLE.fullmatch(title), COUNT_AXIS.fullmatch(x_axis)
            if not (panel and ord(panel[1].casefold()) - ord("a") + 1 == index and parameter
                    and parameter[1].casefold() in panel[2].casefold()
                    and re.search(rf"\b{re.escape(parameter[1])}\b", context, re.I) and EXACT_MATCH_AXIS.search(y_axis)):
                reason = ("Уточните рамку, захватив заголовок панели и подписи обеих осей."
                          if bounds else "Подписи панели и осей не установили параметр серии и шкалу EM.")
                remark("indeterminate-axis", reason, panel_id)
                continue
            panel, parameter = panel[1].casefold(), parameter[1].casefold().replace(" ", "_")
            artifact["attempted_strategy"] = "vector-line"
            read = selected["read"] if selected is not None else digitize_panel(pdf, candidate, panel, experiment.method, x_axis)
            if selected is None:
                observe(artifact, read, candidate, panel)
            setting_panel = selected["panel"] if selected is not None else panel
            artifact["settings"]["panels"][setting_panel] = {
                "series": experiment.method, "axes": read.get("axes"), "region": read.get("region"), "engine": read["engine"],
            }
            if read["issues"]:
                remark("digitization-unavailable", " ".join(issue["message"] for issue in read["issues"]), panel_id)
            elif not has_single_trace(page, (left, top, right, bottom), read["points"]):
                reason = ("Уточните панель и захватите легенду, чтобы различить ряды данных."
                          if bounds else "Геометрия панели не подтверждает единственный ряд данных.")
                remark("ambiguous-series", reason, panel_id)
            else:
                bind_panel(artifact, evidence, remark, candidate, experiment, panel_id, title, parameter, read)


def observe(artifact, read, candidate, panel):
    artifact["observations"].extend({
        **point, "id": f"{candidate['id']}-panel-{panel}-observation-{point['index']}",
        "source": {**point["source"], "figure": candidate["label"], "precision": "plot_point"},
    } for point in read["points"])
    artifact["observations"].extend({
        **proposal, "id": f"{candidate['id']}-panel-{panel}-proposal-{index}",
        "source": {**proposal["source"], "figure": candidate["label"]},
    } for index, proposal in enumerate(read.get("proposals", []), 1))


def bind_labeled_bars(pdf, candidate, artifact, evidence, remark, read=None, panel="1"):
    page, figure, candidate_id = candidate["page"], candidate["label"], candidate["id"]
    args = argparse.Namespace(
        pdf=pdf, page=page, figure=figure, panel="1", series="", x_axis="", y_axis="",
        strategy="vector-bar", panel_box=None, plot_box=None, x_ticks=None, y_ticks=None, stroke_rgb=None,
    )
    if read is None:
        artifact["attempted_strategy"] = "vector-bar"
        read = extract(args, None)
        observe(artifact, read, candidate, "1")
    if not read.get("metric") or not EXACT_MATCH_NAME.fullmatch(read["metric"]["text"]):
        remark("unsupported-figure", "Автоматическое чтение столбцов требует подписанную шкалу Exact Match.")
        return
    artifact["strategy"] = args.strategy
    artifact["settings"]["panels"][panel] = {"region": read["region"], "engine": read["engine"]}
    if read["region"]["detected_panels"] != 1:
        remark("unsupported-figure", "Автоматическая публикация столбцов поддерживает одну панель.")
        return
    if len(set(read["scale"])) < 2 or not all(0 <= tick <= 1 for tick in read["scale"]):
        remark("scale-interpretation", "Деления графика не подтверждают шкалу Exact Match в долях.")
        return

    def attach(field, label, source, precision="figure"):
        box = source["box"]
        # Shared labels (legend entry, group name, axis title) have one source per box.
        identifier = f"{candidate_id}-{field}-" + "-".join(str(round(edge * 1_000_000)) for edge in box)
        evidence[identifier] = plot_evidence(identifier, page, label, box, precision)
        return identifier

    metric = read["metric"]
    metric_id = attach("metric", metric["text"], metric["source"])
    for point in read["points"]:
        raw = point["raw_value"]
        point_id = attach("bar", raw, point["source"], "plot_point")
        if not 0 <= point["y"] <= 1:
            remark("scale-interpretation", "Значение не укладывается в долю Exact Match.", point_id)
            continue
        method, dataset = point["series"], point["category"]
        series_id = attach("series", method, point["series_source"])
        group_id = attach("group", dataset, point["category_source"])
        address = FigureSource(page, figure, raw, point["source"]["box"], (point_id,), (method, dataset, "EM"))
        result = Result(
            method=method, generator="", dataset=dataset, metric="EM", task="", experiment=candidate["text"],
            protocol=None, raw_value=raw, value=raw.replace("−", "-"), unit="fraction", statistic=None,
            retrieval_top_k=None, source=address,
            condition_sources={"method": (series_id,), "dataset": (group_id,), "metric": (metric_id,)},
        )
        artifact["results"].append({**result.to_dict(), "id": point_id, "source_fragment": raw})
    for issue in read["issues"]:
        issue_id = attach("unresolved-bar", "", issue["source"])
        remark("ambiguous-binding", issue["message"], issue_id)


def explicit_selection(raw_plan, expected_target):
    try:
        plan = json.loads(raw_plan) if isinstance(raw_plan, str) else raw_plan
    except (ValueError, TypeError) as error:
        raise ValueError(f"Invalid figure plan JSON. {error}") from error
    if not isinstance(plan, dict) or set(plan) != {"target", "selection"}:
        raise ValueError("Figure plan requires target and selection.")
    if plan["target"] != expected_target:
        raise IncompatibleArtifact("Адрес рисунка не совпадает с сохранённой картой статьи.")
    if plan["selection"] == {"mode": "auto"}:
        return None
    return validate_selection(plan["selection"])


def read_explicit(pdf, candidate, target, selection, experiment, artifact, evidence, remark, private_artifacts):
    strategy, parameters = selection
    artifact.update(strategy=strategy, requested_strategy=strategy)
    args = argparse.Namespace(
        pdf=pdf, page=candidate["page"], figure=candidate["label"], strategy=strategy,
        panel="1", series="", x_axis="", y_axis="", panel_box=None, plot_box=None,
        x_ticks=None, y_ticks=None, stroke_rgb=None, marker_size=None, raster_config=None,
    )
    vars(args).update(parameters)
    artifact["settings"]["parameters"] = parameters
    region = target["region"]
    panels = region["panels"]
    if strategy == "raster-line":
        with pdfplumber.open(pdf) as document:
            page = document.pages[candidate["page"] - 1]
            left, top, right, bottom = region["box"]
            corners = parameters["raster_config"]["corners_pt"]
            within = all(left - 1e-6 <= x / page.width <= right + 1e-6
                         and top - 1e-6 <= y / page.height <= bottom + 1e-6 for x, y in corners)
        if not within:
            artifact.update(status="not_applicable", reason="Raster frame escapes the frozen figure region.")
            remark("unsupported-figure", artifact["reason"])
            return
        # JSON Schema integer semantics also permit integral JSON numbers such as 80.0.
        args.raster_config = {**args.raster_config, "max_gray": int(args.raster_config["max_gray"])}
    else:
        index = int(args.panel) if args.panel.isdecimal() else ord(args.panel.lower()) - ord("a") + 1
        if not 1 <= index <= len(panels):
            artifact.update(status="not_applicable", reason=f"Panel {args.panel!r} has no frozen vector frame.")
            remark("unsupported-figure", artifact["reason"])
            return
        frozen = panels[index - 1]
        args.panel_box = frozen["panel_box_pt"]
        args.plot_box = frozen["plot_box_pt"]
        args.frozen_panel_count = len(panels)
    artifact["attempted_strategy"] = strategy
    try:
        read = extract(args, None, private_artifacts=private_artifacts)
    except (ImportError, ConnectionError) as error:
        artifact.update(status="unavailable", reason=str(error))
        remark("reader-unavailable", str(error))
        return
    artifact.update(status=read["status"], reason=read.get("reason"))
    artifact["reader_issues"] = read["issues"]
    artifact["settings"]["panels"][args.panel] = {
        "region": read.get("region"), "engine": read["engine"], "axes": read.get("axes"),
    }
    observe(artifact, read, candidate, args.panel)
    address = candidate["id"]
    if "model_attempt" in read:
        artifact["model_attempt"] = read["model_attempt"]
        crop = read["source"]
        address = f"{candidate['id']}-panel-{args.panel}-crop"
        evidence[address] = plot_evidence(address, crop["page"], candidate["text"], crop["box"], crop["precision"])
    if read["status"] != "succeeded":
        artifact["reason"] = read.get("reason") or " ".join(issue["message"] for issue in read["issues"]) or "Reader returned no observations."
        kind = {
            "not_applicable": "unsupported-figure", "unavailable": "reader-unavailable",
            "timed_out": "reader-timeout", "failed": "figure-reader-issue",
        }.get(read["status"], "no-observations")
        remark(kind, artifact["reason"], address)
    elif read.get("proposals"):
        remark("unreviewed-figure-proposal", "Наблюдения модели требуют проверки по области исходного PDF.", address)
    elif strategy == "vector-bar":
        bind_labeled_bars(pdf, candidate, artifact, evidence, remark, read=read, panel=args.panel)
    elif strategy == "vector-line" and args.x_ticks is None and args.y_ticks is None:
        if experiment is None:
            remark("ambiguous-binding", "Подпись и текст не установили единственный эксперимент и серию рисунка.")
        else:
            evidence.update({fragment.id: asdict(fragment) for fragment in (experiment.context, experiment.setup)})
            read_panels(pdf, candidate, experiment, artifact, evidence, remark, selected={
                "index": index, "plot_box_pt": args.plot_box, "panel": args.panel, "read": read,
            })
    else:
        remark("ambiguous-binding", "Наблюдения ридера сохранены; условия эксперимента требуют подтверждения.")


def read_operator_region(pdf, inventory, shared_text, candidate_id, plan, digest, context_output):
    target = plan["target"]
    box, page_number = target.get("box"), target.get("page")
    if (set(plan) != {"target", "selection"} or plan["selection"] != {"mode": "auto"}
            or set(target) != {"stage", "box", "page", "pdf_sha256", "parent_run_id", "candidate_id"}
            or target["pdf_sha256"] != digest or type(page_number) is not int
            or not 1 <= page_number <= inventory["page_count"] or not isinstance(box, list) or len(box) != 4
            or any(type(edge) not in (float, int) for edge in box)
            or not (0 <= box[0] < box[2] <= 1 and 0 <= box[1] < box[3] <= 1)
            or (target["candidate_id"] is not None and target["candidate_id"] != candidate_id)):
        raise IncompatibleArtifact("Некорректная рамка или версия PDF в плане области.")
    with pdfplumber.open(pdf) as document:
        if page_number > len(document.pages):
            raise IncompatibleArtifact("Физическая страница отсутствует в PDF.")
        page = document.pages[page_number - 1]
        bounds = tuple(edge * scale for edge, scale in zip(box, (page.width, page.height) * 2))
        frames = find_plot_frames(page, None, bounds) if not page.rotation else []
        caption_frames = []
        if len(frames) == 1:
            for item in inventory["candidates"]:
                if (item["kind"] != "figure" or item["page"] != page_number or not item["label"]
                        or (target["candidate_id"] is not None and item["id"] != target["candidate_id"])):
                    continue
                try:
                    addressed = find_plot_frames(page, item["label"])
                except ValueError:
                    continue
                if frames[0] in addressed:
                    caption_frames.append((item, addressed.index(frames[0]) + 1))
        rotation = page.rotation

    caption, index = caption_frames[0] if len(caption_frames) == 1 else (None, 1)
    figure = caption["label"] if caption else None
    candidate = {"id": candidate_id, "page": page_number, "label": figure,
                 "text": caption["text"] if caption else "Рамка оператора", "box": caption["box"] if caption else box}
    evidence = {candidate_id: plot_evidence(candidate_id, page_number, candidate["text"], candidate["box"], "figure")}
    area_id = candidate_id + "-operator-area"
    evidence[area_id] = plot_evidence(area_id, page_number, "Рамка оператора", box, "figure")
    artifact = {
        "source": {"sha256": digest}, "strategy": "vector-line", "target": target,
        "settings": {"candidate": candidate_id, "panels": {}}, "results": [], "remarks": [], "observations": [],
        "requested_strategy": "auto", "attempted_strategy": None, "status": "no_observations", "reason": None,
    }

    def remark(kind, reason, address=area_id):
        source = evidence[address]
        artifact["remarks"].append({
            "id": f"figure-issue-{len(artifact['remarks']) + 1}", "kind": kind,
            "reason": reason, "raw_text": source["text"],
            "source": {"page": page_number, "figure": figure, "fragment": source["text"],
                       "box": source["box"], "evidence_ids": [address]},
        })

    if rotation:
        remark("unsupported-figure", "Чтение графика на повёрнутой странице требует уточнения.")
    elif not frames:
        remark("missing-panel", "В рамке не найдена панель с делениями. Захватите график и обе оси целиком.")
    elif len(frames) > 1:
        remark("multiple-panels", "В рамку попали несколько панелей. Обведите одну панель с её осями.")
    else:
        panel = chr(ord("a") + index - 1) if index <= 26 else str(index)
        args = argparse.Namespace(
            pdf=pdf, page=page_number, figure=figure, strategy="vector-line",
            panel=panel, series="", x_axis="", y_axis="", panel_box=bounds, plot_box=frames[0],
            x_ticks=None, y_ticks=None, stroke_rgb=None, restrict_tick_labels=True,
        )
        artifact["attempted_strategy"] = args.strategy
        read = extract(args, None)
        observe(artifact, read, candidate, panel)
        artifact["settings"]["panels"][panel] = {
            "engine": read["engine"], "axes": read.get("axes"), "region": read.get("region"),
        }
        experiment = None
        if caption:
            prepared = (prepare_figure_context(shared_text, digest, inventory["page_count"], candidate) if shared_text
                        else PreparedFigureContext(digest, candidate, None))
            if context_output is not None:
                write_json(context_output, prepared.artifact())
            experiment = prepared.experiment
        if not read["points"]:
            detail = " ".join(issue["message"] for issue in read["issues"])
            remark("missing-observations", f"В рамке не прочитаны точки. Проверьте деления, оси и легенду. {detail}".strip())
        elif experiment is None:
            remark("ambiguous-binding", "Подпись и текст не установили эксперимент, датасет и серию точек.")
        else:
            evidence.update({fragment.id: asdict(fragment) for fragment in (experiment.context, experiment.setup)})
            read_panels(pdf, candidate, experiment, artifact, evidence, remark, selected={
                "index": index, "plot_box_pt": frames[0], "panel_box_pt": bounds, "panel": panel, "read": read,
            })
        confirmed_boxes = {tuple(row["source"]["box"]) for row in artifact["results"]}
        for point in artifact["observations"]:
            point_box = point["source"]["box"]
            if tuple(point_box) in confirmed_boxes:
                continue
            point_id = point["id"]
            number = f"≈{point['y']:.3f}"
            evidence[point_id] = plot_evidence(point_id, page_number, number, point_box, "plot_point")
            remark("unbound-figure-observation",
                   f"Прочитано {number}. Привязка к условиям эксперимента требует подтверждения.", point_id)

    artifact["status"] = "succeeded" if artifact["observations"] else "no_observations"
    if not artifact["observations"]:
        artifact["reason"] = artifact["remarks"][0]["reason"]
    artifact["binding"] = {"results": artifact["results"], "remarks": artifact["remarks"]}
    return {**artifact, "evidence": list(evidence.values())}


def read_figure(
    pdf: Path, inventory: dict, shared_text: Path | None, candidate_id: str, figure_plan=None, *,
    context_input: Path | None = None, context_output: Path | None = None, private_artifacts=None,
) -> dict:
    with pdf.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if inventory.get("schema_version") != 1 or inventory.get("sha256") != digest:
        raise IncompatibleArtifact("Карта статьи относится к другой версии PDF или имеет неизвестный формат.")
    if figure_plan is not None:
        plan = json.loads(figure_plan) if isinstance(figure_plan, str) else figure_plan
        if isinstance(plan, dict) and isinstance(plan.get("target"), dict) and plan["target"].get("stage") == "region":
            if context_input is not None:
                raise ValueError("Для рамки оператора используйте общий текст статьи.")
            return read_operator_region(pdf, inventory, shared_text, candidate_id, plan, digest, context_output)
    matches = [item for item in inventory["candidates"] if item["kind"] == "figure" and item["id"] == candidate_id]
    if len(matches) != 1:
        raise IncompatibleArtifact(f"На карте нет единственного рисунка с адресом {candidate_id}.")
    candidate = matches[0]
    target = prepare_target(pdf, inventory, candidate_id) if figure_plan is not None else None
    selection = explicit_selection(figure_plan, target) if figure_plan is not None else None
    figure, page, caption = candidate["label"], candidate["page"], candidate["text"]
    pages = inventory["page_count"]
    if shared_text is not None and context_input is not None:
        raise ValueError("Выберите один источник контекста рисунка.")
    if context_input is not None:
        prepared = load_figure_context(context_input, digest, pages, candidate)
    elif shared_text is not None:
        prepared = prepare_figure_context(shared_text, digest, pages, candidate)
    else:
        prepared = PreparedFigureContext(digest, candidate, None)
    if context_output is not None:
        write_json(context_output, prepared.artifact())
    experiment = prepared.experiment

    evidence = {candidate_id: plot_evidence(candidate_id, page, caption, candidate["box"], "figure")}
    artifact = {
        "source": {"sha256": digest}, "strategy": "vector-line", "settings": {"candidate": candidate_id, "panels": {}},
        "results": [], "remarks": [],
        "requested_strategy": "auto", "attempted_strategy": None, "status": "no_observations",
        "reason": None, "observations": [],
    }

    def remark(kind, reason, address=candidate_id):
        source = evidence[address]
        artifact["remarks"].append({
            "id": f"figure-issue-{len(artifact['remarks']) + 1}", "kind": kind,
            "reason": reason, "raw_text": source["text"],
            "source": {"page": page, "figure": figure, "fragment": source["text"],
                       "box": source["box"], "evidence_ids": [address]},
        })

    if selection is not None:
        artifact["target"] = target
        read_explicit(pdf, candidate, target, selection, experiment, artifact, evidence, remark, private_artifacts)
    elif not SENSITIVITY_CAPTION.search(caption):
        artifact["strategy"] = "figure-auto"
        bind_labeled_bars(pdf, candidate, artifact, evidence, remark)
    else:
        if experiment is None:
            remark("ambiguous-binding", "Подпись и текст не установили единственный эксперимент и серию рисунка.")
        else:
            evidence.update({fragment.id: asdict(fragment) for fragment in (experiment.context, experiment.setup)})
            read_panels(pdf, candidate, experiment, artifact, evidence, remark)
    if selection is None:
        artifact["status"] = "succeeded" if artifact["observations"] else "no_observations"
        if not artifact["observations"]:
            artifact["reason"] = " ".join(item["reason"] for item in artifact["remarks"]) or "Reader returned no observations."
    artifact["binding"] = {"results": artifact["results"], "remarks": artifact["remarks"]}
    return {**artifact, "evidence": list(evidence.values())}


def save_article_figure(pdf, inventory_path, shared_text, candidate, output, figure_plan=None, *, context_input=None):
    if output.exists():
        raise ValueError(f"Каталог результата уже существует: {output}")
    inventory = json.loads(inventory_path.read_bytes())
    private_artifacts = {}
    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".rag-figure-", dir=output.parent) as temporary:
        directory = Path(temporary)
        artifact = read_figure(pdf, inventory, shared_text, candidate, figure_plan=figure_plan,
                               context_input=context_input, context_output=directory / CONTEXT_ARTIFACT,
                               private_artifacts=private_artifacts)
        write_json(directory / "extracted.json", artifact)
        for name, contents in private_artifacts.items():
            with (directory / name).open("xb") as private:
                os.fchmod(private.fileno(), 0o600)
                private.write(contents)
        directory.rename(output)


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("pdf", type=Path)
    cli.add_argument("--inventory", type=Path, required=True)
    context = cli.add_mutually_exclusive_group()
    context.add_argument("--article-text", type=Path)
    context.add_argument("--context-input", type=Path)
    cli.add_argument("--candidate", required=True)
    cli.add_argument("--figure-plan")
    cli.add_argument("--out", type=Path, required=True)
    args = cli.parse_args()
    try:
        save_article_figure(args.pdf, args.inventory, args.article_text, args.candidate, args.out,
                            figure_plan=args.figure_plan, context_input=args.context_input)
    except IncompatibleArtifact as error:
        cli.exit(3, f"{error}\n")
    except (ValueError, OSError) as error:
        cli.error(str(error))


if __name__ == "__main__":
    main()
