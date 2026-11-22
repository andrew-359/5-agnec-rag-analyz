"""Add source-backed passport measurements to the offline submission bundle."""
from collections import Counter
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import re
import sys

from rag_mvp.article_context.captions import TABLE_LINE, text_for_matching
from rag_mvp.table_reading_retry import _overlaps


PROBE = Path(__file__).resolve().parents[1] / "experiments" / "entity-passport"
KEYS = {"results": "result_id", "issues": "issue_id", "configurations": "config_id",
        "datasets": "dataset_id", "tasks": "task_id", "experiments": "experiment_id"}
GUESSES = {"header-model-as-generator", "left-column-as-method-variant", "article-task",
           "caption-method", "caption-model", "caption-generator", "caption-dataset"}
METRIC_ROLE = re.compile(
    r"\b(?:accuracy|precision|recall|rate|ratio|score|efficiency|error|latency|time|throughput|"
    r"perplexity|similarity|consistency|faithfulness|factuality|coverage|relevance|agreement|"
    r"correlation|success|overlap|sensitivity|specificity|distance|pass@\d+)\b", re.I,
)
COMPARISON_AXIS = re.compile(r"^(?:[∆Δ]|diff(?:erence)?\.?\b|improve(?:ment)?\b|"
                             r"(?:relative|absolute) (?:gain|change|reduction)\b)", re.I)
CONFIG_AXIS = re.compile(
    r"(?:method|model|system|approach|retriever|baseline|variant|strategy|backbone|configuration|LLM|"
    r"retrieval model|model type)s?"
    r"(?:\s*/\s*(?:method|model|system|approach|retriever|baseline|variant|strategy|backbone)s?)?", re.I,
)
DOWNWARD_CONFIG_AXIS = re.compile(r"\b(?:method|model|retriever|system)s?\s*\(\s*↓\s*\)", re.I)
GENERATOR_AXIS = re.compile(r"(?:generators?|generator models?|answer (?:models?|generators?)|"
                            r"generation models?|generative models?)", re.I)
AGGREGATE_LABELS = {"all", "across", "average", "avg", "overall", "combined", "total", "macro", "micro"}
MULTI_DATASET_CAPTION = re.compile(
    r"\b(?:across|over|on|for)\s+(?:[\w-]+\s+){0,3}(?:datasets|benchmarks)\b|"
    r"\b(?:across|over|by)\s+(?:[\w-]+\s+){0,3}(?:domains|splits|subsets|categories)\b", re.I,
)
TRAINING_DATASET_LINK = re.compile(r"\b(?:train(?:ed|ing)?|fine[- ]?tun(?:e|ed|ing)|tuned)"
                                   r"(?:\s+[\w-]+){0,3}\s*$", re.I)


def filter_metric_roles(passport, known_names, metric_key, entity_key):
    """An expansion such as NQ = Natural Questions supplies a name, not a measurement role."""
    trusted = {metric_key(name) for name in known_names}
    other_roles = set().union(*(passport[role] for role in ("datasets", "models", "methods")))
    for key, entry in tuple(passport["metrics"].items()):
        display = entry["value"]
        known = metric_key(display) in trusted
        known_other_role = entity_key(display) in other_roles or entity_key(key) in other_roles
        if not known and (known_other_role or not METRIC_ROLE.search(display)):
            del passport["metrics"][key]


def blocked_cells(cells, stock):
    blocked = set()
    for source in stock:
        box = source.get("box")
        if not box:
            return None
        exact = {address for address, (_, witness) in cells.items()
                 if all(abs(a - b) <= 1e-6 for a, b in zip(box, witness["box"], strict=True))}
        if len(exact) == 1:
            blocked.update(exact)
            continue
        overlaps = {address for address, (_, witness) in cells.items() if _overlaps(box, witness["box"])}
        if not overlaps:
            return None
        blocked.update(overlaps)
    return blocked


CAPTION_REFERENCE = re.compile(r"^(?:shows?|reports?|presents?|compares?|gives?|lists?|provides?|"
                               r"extends?|studies|examines?|summari[sz]es?|illustrates?|analys[ez]es?)\b", re.I)


def adjacent_gap(table, caption):
    x0, y0, x1, y1 = table
    a0, b0, a1, b1 = caption
    if min(x1, a1) - max(x0, a0) < .6 * min(x1 - x0, a1 - a0):
        return None
    gaps = [abs(gap) for gap, outer in ((y0 - b1, b0 < y0), (b0 - y1, b1 > y1))
            if outer and -.028 <= gap <= .038]
    return min(gaps, default=None)


def nearby_caption(page, box, fragments, neighbors, label=None):
    if not box:
        return None
    found = []
    for item in fragments:
        if item["page"] != page or not item.get("box") or item.get("precision") != "text_fragment":
            continue
        marker = TABLE_LINE.fullmatch(text_for_matching(item["text"]))
        if not marker or (label and marker["label"].casefold() != label.casefold()):
            continue
        tail = marker["tail"].strip()
        title = tail.lstrip(":.—–- ")
        if (not title or CAPTION_REFERENCE.match(title)
                or (tail[0] not in ":.—–-" and not title[0].isupper())):
            continue
        gap = adjacent_gap(box, item["box"])
        if gap is None or any(
            other != box and (distance := adjacent_gap(other, item["box"])) is not None and distance <= gap + .008
            for other in neighbors
        ):
            continue
        found.append(item)
    return found[0] if len(found) == 1 else None


def table_input(data, known_metric=lambda _: None, fragments=(), neighbors=()):
    view = data if data.get("cells") else (data.get("reread_grid") or {})
    grid = view.get("grid") or {}
    cells = view.get("cells") or ()
    evidence = data.get("evidence") or ()
    if not cells:
        return None
    refs = {item["id"]: item for item in evidence}
    caption = refs.get(grid.get("caption_evidence_id"))
    page = data.get("page")
    if not page:
        return None
    caption_located = bool(caption and caption["page"] == page and caption["precision"] == "text_fragment"
                           and caption.get("box") and caption["text"].strip())
    marker = TABLE_LINE.fullmatch(text_for_matching(caption["text"])) if caption_located else None
    region = data.get("region") or {}
    label = region.get("author_label") or (marker["label"] if marker else None)
    recovered = None
    if not (caption_located and label):
        table_box = region.get("box") or next((refs[ref]["box"] for ref in grid.get("evidence_ids", ())
                                               if ref in refs and refs[ref]["precision"] == "table"), None)
        recovered = nearby_caption(page, table_box, fragments, neighbors, label)
        if recovered:
            caption = dict(recovered, id="passport-caption:" + recovered["id"])
            label = TABLE_LINE.fullmatch(text_for_matching(caption["text"]))["label"]
            caption_located = True
    header_only = not (caption_located and label)
    if header_only:
        if not data.get("table") or not any(
            cell["role"] == "column_header" and known_metric(cell["text"]) for cell in cells
        ):
            return None
        label = label or str(data["table"])
        caption = {"id": "", "text": "", "box": None}
    return {"page": page, "label": label, "caption": caption,
            "cells": cells, "evidence": evidence, "reader": data.get("strategy"), "header_only": header_only,
            "recovered_caption": recovered}


def positioned_cells(table, clean):
    refs = {item["id"]: item for item in table["evidence"]
            if item["page"] == table["page"] and item.get("box")
            and item["precision"] in {"cell", "estimated_cell"}}
    positions = {}
    duplicates = set()
    for cell in table["cells"]:
        address = cell["row"], cell["column"]
        witnesses = [refs[ref] for ref in cell["evidence_ids"] if ref in refs
                     and clean(refs[ref]["text"]) == clean(cell["text"])]
        exact = [item for item in witnesses if item["precision"] == "cell"]
        if exact:
            witnesses = exact
        if address in positions:
            duplicates.add(address)
        if len(witnesses) == 1:
            positions[address] = cell, witnesses[0]
    return {address: pair for address, pair in positions.items() if address not in duplicates}


def aligned_ocr_tokens(table, cells, clean, scalar):
    page = table["page"]
    tokens = [item for item in table["evidence"] if item["page"] == page and item.get("box")
              and item["id"].startswith(f"ocr-{page}-") and item["precision"] == "text_fragment"
              and scalar.fullmatch(clean(item["text"]))]
    if not tokens:
        return {}
    estimated_boxes = [witness["box"] for _, witness in cells.values() if witness["precision"] == "estimated_cell"]
    result = {}
    for address, (cell, witness) in cells.items():
        if witness["precision"] != "estimated_cell" or cell["row_span"] != 1 or cell["column_span"] != 1:
            continue
        x0, y0, x1, y1 = witness["box"]
        inside = []
        for item in tokens:
            a0, b0, a1, b1 = item["box"]
            x, y = (a0 + a1) / 2, (b0 + b1) / 2
            if x0 <= x <= x1 and y0 <= y <= y1:
                inside.append((item, x, y))
        if len(inside) != 1:
            continue
        (token, x, y), = inside
        if clean(token["text"]) == clean(cell["text"]) and sum(
            a0 <= x <= a1 and b0 <= y <= b1 for a0, b0, a1, b1 in estimated_boxes
        ) == 1:
            result[address] = token
    return result


def scoped_dataset(claim, address, path, headers, cells, caption_dataset, named_dataset, known_metric):
    row, _ = address
    if claim["rule"] == "header-dataset":
        refs = {source["id"] for source in claim["sources"]}
        scoped = [head for head in path if refs.intersection(head["evidence_ids"])]
        named = {name for head in path if (name := named_dataset(head["text"]))}
        left = [label for head in headers if head["text"].strip().casefold() == "dataset"
                and head["column_span"] == 1 and (label := cells.get((row, head["column"])))]
        named.update(name for label, _ in left if (name := named_dataset(label["text"])))
        if not scoped:
            return False
        if path[0] in scoped and path[0]["column_span"] == 1:
            # A printed leaf can identify a subdataset of the parent benchmark.
            return True
        if named != {claim["value"]}:
            return False
        # An unknown child label may identify a subdataset, even if it is absent from the passport.
        for head in path:
            if head in scoped:
                return True
            text = head["text"]
            if named_dataset(text) == claim["value"] or known_metric(text):
                continue
            return False
        return False
    if claim["rule"] == "row-dataset":
        refs = {source["id"] for source in claim["sources"]}
        return any(header["text"].strip().casefold() == "dataset" and header["column_span"] == 1
                   and (label := cells.get((row, header["column"]))) and label[1]["id"] in refs
                   for header in headers)
    if claim["rule"] == "caption-dataset":
        return caption_dataset == claim["value"] and not any(
            head["text"].strip().casefold() in AGGREGATE_LABELS for head in path
        )
    return True


def single_caption_dataset(caption, cells, datasets, named_dataset, clean):
    title = clean(caption["text"])
    if not caption.get("box") or MULTI_DATASET_CAPTION.search(title):
        return None
    mentioned = set()
    linked = set()
    for entry in datasets.values():
        name = entry["value"]
        if name.casefold() in AGGREGATE_LABELS:
            continue
        token = rf"(?<![\w-]){re.escape(name)}(?![\w-])"
        # Short literal names such as NQ are case-sensitive.
        flags = re.I if len(name) > 3 else 0
        if not re.search(token, title, flags):
            continue
        mentioned.add(name)
        links = re.finditer(rf"(?i:\b(?:on|using|dataset\s*[:=]|benchmark\s*[:=])\s+(?:the\s+)?)"
                            rf"{token}|{token}\s+(?i:dataset|benchmark)\b", title, flags)
        if any(not TRAINING_DATASET_LINK.search(title[:link.start()]) for link in links):
            linked.add(name)
    if len(mentioned) != 1 or mentioned != linked:
        return None
    (name,) = linked
    observed = {dataset for cell in cells if (dataset := named_dataset(cell["text"]))}
    return name if observed <= {name} else None


def caption_names_generator(caption, name):
    if not caption.get("box"):
        return False
    model = rf"(?<![\w-]){re.escape(name)}(?![\w-])"
    role = r"(?:generator|generative model|answer model|answer generator)"
    return bool(re.search(rf"{model}\s+(?:as|for)\s+(?:the |an? )?{role}\b|"
                          rf"\b{role}\s*[:=]\s*{model}", text_for_matching(caption["text"]), re.I))


class SubmissionPassport:
    def __init__(self, lexicon):
        # Reuse the probe's implementation with its existing sibling imports.
        sys.path.insert(0, str(PROBE))
        try:
            import entities
            import extract
            from score_dev_gold import table_key
        finally:
            sys.path.pop(0)
        self.entities, self.extract = entities, extract
        self.table_key = table_key
        self.metrics, self.datasets = {}, {}
        self.manifest = {"profile": "local-pdf", "rules": entities.VERSION,
                         "sources": {path.name: sha256(path.read_bytes()).hexdigest()
                                     for path in PROBE.glob("*.py") if not path.name.startswith("test_")}}
        if lexicon:
            # Retain only names and their corpus owners. Gold observations never reach the binder.
            for file, field, attr in (("results.csv", "metric_name", "metrics"),
                                      ("datasets.csv", "dataset_name", "datasets")):
                names = entities.sources_of_lexicon(extract.load_csv(lexicon / file), (field,))
                setattr(self, attr, names)
            names = [{name: sorted(owners) for name, owners in sorted(vocab.items())}
                     for vocab in (self.metrics, self.datasets)]
            self.manifest.update(profile="dev-names-only",
                                 lexicon_sha256=sha256(json.dumps(names, sort_keys=True).encode()).hexdigest())

    def add(self, name, pdf, sha, fragments, artifacts, stock, tables, audit):
        entities = self.entities.passport({"doc": name, "text": fragments}, self.metrics, self.datasets)
        vocabulary = (*self.entities.COMMON_METRICS, *(metric for metric, owners in self.metrics.items()
                                                       if owners - {name}))
        filter_metric_roles(entities, vocabulary, self.entities.metric_key, self.entities.entity_key)
        counts = Counter()
        candidates = {}
        occupied = {}
        regions = {}
        for item in artifacts:
            region = item["data"].get("region") or {}
            if item["kind"] == "table" and region.get("box"):
                regions.setdefault(item["data"]["page"], []).append(region["box"])
        for item in artifacts:
            if item["kind"] != "table":
                continue
            stage = item["id"]
            table = table_input(
                item["data"],
                lambda text: (self.entities.known_metric(text, entities)
                              or self.extract.dataset_metric_header(text, entities)),
                fragments, regions.get(item["data"].get("page"), ()),
            )
            if table is None:
                counts["unaddressed_or_duplicate_table"] += 1
                continue
            address = table["page"], self.table_key(table["label"])
            occupied.setdefault(address, []).extend(stock.get(stage, ()))
            previous = candidates.get(address)
            if previous:
                counts["unaddressed_or_duplicate_table"] += 1
                old = previous[1]
                if (len(old["cells"]), not old["header_only"]) >= (len(table["cells"]), not table["header_only"]):
                    continue
            candidates[address] = stage, table
        used = {key: {row[field] for row in tables[key]} for key, field in KEYS.items()}
        indexes = {key: {} for key in ("configurations", "datasets", "tasks", "experiments")}

        def append(key, fields):
            n = len(tables[key]) + 1
            while (identifier := f"B3-{KEYS[key]}-{n}") in used[key]:
                n += 1
            used[key].add(identifier)
            tables[key].append({KEYS[key]: identifier, **fields})
            return identifier

        def entity(key, fields):
            fields = {field: value for field, value in fields.items() if value}
            if not fields:
                return ""
            identity = tuple(fields.items())
            if identity not in indexes[key]:
                indexes[key][identity] = append(key, fields)
            return indexes[key][identity]

        for table_address, (stage, table) in candidates.items():
            cells = positioned_cells(table, self.entities.clean)
            ocr_tokens = aligned_ocr_tokens(table, cells, self.entities.clean, self.entities.SCALAR)
            headers = self.extract.headers_for(table["cells"], entities)
            left = min(cell["column"] for cell in table["cells"])
            left_labels = {}
            for (r, col), pair in cells.items():
                if col == left and pair[0] not in headers:
                    for covered in range(r, r + pair[0]["row_span"]):
                        left_labels.setdefault(covered, []).append(pair)

            def named_dataset(text):
                entry = self.extract.named(text, "datasets", entities)
                if entry and entry["value"].casefold() not in AGGREGATE_LABELS:
                    return entry["value"]
                return None

            caption_dataset = single_caption_dataset(table["caption"], table["cells"],
                                                     entities["datasets"], named_dataset, self.entities.clean)
            model_sources = {item["id"]: item for item in table["evidence"]
                             if item["page"] == table["page"] and item.get("box")}
            comparison_rows = {cell["row"] for cell in table["cells"]
                               if cell["role"] == "row_header" and COMPARISON_AXIS.search(cell["text"].strip())}
            comparison_columns = {column for header in headers if COMPARISON_AXIS.search(header["text"].strip())
                                  for column in range(header["column"], header["column"] + header["column_span"])}
            blocked = blocked_cells(cells, occupied[table_address])
            if blocked is None:
                counts["unmapped_stock_table"] += 1
                continue
            seen = set()
            for row in self.extract.table_records(table, entities, fragments, pdf_path=pdf, pdf_sha=sha):
                address = row["row"], row["column"]
                info = {"article": name, "pdf_sha256": sha, "stage": stage, "page": table["page"],
                        "table": table["label"], "row": address[0], "column": address[1]}
                metric = row["fields"]["metric"]
                source = cells.get(address)
                component = (*address, row["numeric_rule"], metric["value"])
                if address in blocked:
                    reason = "stock_or_reading_conflict"
                elif not source or source[1] not in row["number_sources"] or source[0]["text"] != row["raw"]:
                    reason = "missing_cell_witness"
                elif (not metric["sources"] or not all(item.get("box") and item["text"].strip() for item in metric["sources"])
                      or table["header_only"] and metric["rule"] != "header-metric"):
                    reason = "missing_metric_witness"
                elif ((address[0] in comparison_rows or address[1] in comparison_columns)
                      and not row["numeric_rule"].startswith("paired-comparison:")):
                    reason = "comparison_without_typed_statistic"
                elif component in seen:
                    reason = "duplicate_component"
                else:
                    reason = None
                if reason:
                    counts[reason] += 1
                    audit.write(json.dumps({**info, "reason": reason}, ensure_ascii=False) + "\n")
                    continue
                _, witness = source
                token = ocr_tokens.get(address)
                seen.add(component)
                values = {}
                open_fields = set(row["open"])
                hypotheses = {}
                path = self.extract.path_for(headers, source[0])
                proposed = dict(row["fields"])
                if "dataset" not in proposed and caption_dataset:
                    proposed["dataset"] = {"value": caption_dataset, "rule": "caption-dataset",
                                           "sources": [table["caption"]]}
                labels = left_labels.get(address[0], ())
                if len(labels) == 1:
                    (label_cell,) = labels
                    label_text = self.entities.clean(label_cell[0]["text"])
                    axes = [model_sources[ref] for head in self.extract.path_for(headers, label_cell[0])
                            if (CONFIG_AXIS.fullmatch(self.entities.clean(head["text"]).strip(" :↑↓→"))
                                or DOWNWARD_CONFIG_AXIS.search(head["text"]))
                            for ref in head["evidence_ids"] if ref in model_sources]
                    method = proposed.get("method")
                    if (axes and (not method or method["rule"] in GUESSES)
                            and label_text.casefold() not in AGGREGATE_LABELS | self.extract.UNKNOWN_LABELS
                            and not self.entities.SCALAR.fullmatch(label_text)
                            and not named_dataset(label_text) and not self.entities.known_metric(label_text, entities)):
                        proposed["method"] = {"value": label_text, "rule": "explicit-configuration-axis",
                                              "sources": [label_cell[1], *axes]}
                confirmed = {}
                for field, claim in proposed.items():
                    if field == "dataset":
                        accept = scoped_dataset(
                            claim, address, path, headers, cells, caption_dataset, named_dataset,
                            lambda text: self.entities.known_metric(text, entities),
                        )
                    elif claim["rule"] == "header-model-as-generator":
                        # A model name can also label a retriever or judge. Require an explicit role.
                        refs = {item["id"] for item in claim["sources"]}
                        axes = [model_sources[ref] for head in path
                                if GENERATOR_AXIS.fullmatch(self.entities.clean(head["text"]).strip(" :↑↓→"))
                                for ref in head["evidence_ids"] if ref in model_sources]
                        models = {self.entities.entity_key(head["text"]) for head in path
                                  if self.extract.named(head["text"], "models", entities)
                                  or self.entities.MODEL.fullmatch(self.entities.clean(head["text"]))}
                        caption_confirms = caption_names_generator(table["caption"], claim["value"])
                        header_confirms = axes and len(models) == 1 and any(
                            refs.intersection(head["evidence_ids"]) for head in path
                        )
                        accept = bool(header_confirms or caption_confirms)
                        if header_confirms:
                            claim = {**claim, "sources": [*claim["sources"], *axes]}
                        elif caption_confirms and table["caption"] not in claim["sources"]:
                            claim = {**claim, "sources": [*claim["sources"], table["caption"]]}
                    else:
                        accept = claim["rule"] not in GUESSES
                    if not accept or not claim["sources"]:
                        hypotheses[field] = claim
                        open_fields.add(field)
                    else:
                        values[field] = claim["value"]
                        confirmed[field] = claim
                        open_fields.discard(field)
                reported = row.get("raw_component", row["raw"] if row["numeric_rule"] == "scalar" else row["value"])
                raw = self.entities.clean(reported)
                printed = self.entities.SCALAR.fullmatch(raw) or self.entities.DELTA.fullmatch(raw)
                raw_unit = (printed["unit"] or "") if printed else ""
                if row["numeric_rule"] == "first-number-of-reported-pair":
                    raw = self.entities.clean(row["raw"])
                    if (pair := self.entities.COMPOUND.fullmatch(raw)) and pair["unit"]:
                        raw_unit = pair["unit"]
                        reported = raw[:pair.end("unit")]
                unit = values.get("unit", "")
                number = Decimal(row["value"])
                if (unit == "%" and not 0 <= number <= 100) or (unit == "fraction" and not 0 <= number <= 1):
                    unit = ""
                    open_fields.add("unit")
                label = "Table " + table["label"]
                notes = [f"B3 · {witness['id']} · row {address[0]}, column {address[1]}"]
                if witness["precision"] == "estimated_cell":
                    notes.append("Привязка к сохранённому OCR-токену" if token else "Оценочная рамка OCR")
                    counts["added_estimated_cell"] += 1
                    if token:
                        counts["added_ocr_token"] += 1
                if table["recovered_caption"]:
                    counts["added_recovered_caption"] += 1
                if table["header_only"]:
                    notes.append("Метрика из шапки, подпись не подтверждена")
                    counts["added_header_only"] += 1
                rid = append("results", {
                    "experiment_id": entity("experiments", {"note": table["caption"]["text"]}),
                    "config_id": entity("configurations", {"rag_variant": values.get("method"),
                                                           "generator_model": values.get("generator")}),
                    "task_id": entity("tasks", {"task_type": values.get("task")}),
                    "dataset_id": entity("datasets", {"dataset_name": values.get("dataset")}),
                    "metric_name": metric["value"], "metric_k": values.get("metric_k", ""),
                    "metric_variant": row.get("metric_variant", ""),
                    "metric_value": row["value"], "metric_unit": unit, "raw_value": reported, "raw_unit": raw_unit,
                    "normalized_value": row["value"] if unit else "", "normalized_unit": unit,
                    "result_origin": "primary", "extraction_status": "extracted", "source_page": table["page"],
                    "source_table": label, "source_fragment": witness["text"],
                    "note": " · ".join(notes),
                })
                description = "Извлечено по сохранённой сетке вторым слоем entity-passport."
                if witness["precision"] == "estimated_cell":
                    if token:
                        description += " Текст числа совпал с единственным сохранённым OCR-токеном в ячейке."
                    else:
                        description += " Рамка OCR оценочная, проверьте число и положение ячейки по PDF."
                if table["header_only"]:
                    description += " Метрика из шапки. Подпись таблицы не подтверждена."
                if open_fields:
                    description += " Требуют проверки поля " + ", ".join(sorted(open_fields)) + "."
                if hypotheses:
                    description += " Гипотезы " + ", ".join(f"{k}={v['value']}" for k, v in sorted(hypotheses.items())) + "."
                append("issues", {"record_ids": rid, "check_name": "B3 паспорт таблицы",
                                  "description": description, "source_page": table["page"],
                                  "source_fragment": witness["text"]})
                counts["added"] += 1
                audit.write(json.dumps({**info, "result_id": rid, "witness": witness, "measurement": row,
                                        "source_box": (token or witness)["box"],
                                        **({"ocr_token": token} if token else {}),
                                        **({"recovered_caption": table["recovered_caption"]}
                                           if table["recovered_caption"] else {}),
                                        "header_only": table["header_only"],
                                        "condition_sources": {field: confirmed[field]["sources"]
                                                              for field in ("dataset", "method", "generator")
                                                              if field in confirmed},
                                        "published_fields": values, "open_fields": sorted(open_fields)},
                                       ensure_ascii=False) + "\n")
        return dict(counts)
