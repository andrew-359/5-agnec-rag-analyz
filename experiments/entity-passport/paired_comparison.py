"""Read statistical comparisons whose table rules define centered row groups."""
from collections import defaultdict
from decimal import Decimal
from hashlib import file_digest
from itertools import pairwise
import re

from entities import NUMBER, clean, entity_key
from rag_mvp.core.domain import Source, TableTarget
from rag_mvp.core.measurement_binding import Assertion, Observation, Scope, assemble


RATE_HEADER = re.compile(r"(.+?)\s*%\s*\[95% CI\]", re.I)
INTERVAL = re.compile(rf"({NUMBER})\s*\[\s*({NUMBER})\s*,\s*({NUMBER})\s*\]")
SCALAR = re.compile(NUMBER)


def comparison_headers(cells, title):
    headers = sorted((c for c in cells if c.get("role") == "column_header"), key=lambda c: c["column"])
    if len(headers) != 7 or len({c["row"] for c in headers}) != 1 or len({c["column"] for c in headers}) != 7:
        return None
    names = [clean(c["text"]) for c in headers]
    baseline, target = map(RATE_HEADER.fullmatch, names[3:5])
    if not (names[:3] == ["Dataset", "Failure", "Model"] and baseline and target
            and re.fullmatch(r"[∆Δ]\s*pp", names[5]) and names[6].casefold() == "mcnemar p"
            and re.search(r"95% bootstrap confidence intervals\b", title, re.I)
            and re.search(rf"\breduction from {re.escape(baseline[1])} to {re.escape(target[1])}"
                          r" in percentage points\b", title, re.I)
            and re.search(r"\bpaired McNemar tests compare\b", title, re.I)):
        return None
    return headers, baseline[1], target[1]


def center(box):
    return (box[1] + box[3]) / 2


def value(text):
    return Decimal(text.replace("−", "-").replace(",", ""))


def parsed_statistics(cells):
    a, b = (INTERVAL.fullmatch(clean(c["text"])) for c in cells[:2])
    d, p = (clean(c["text"]) for c in cells[2:])
    if not (a and b and SCALAR.fullmatch(d) and SCALAR.fullmatch(p)):
        return None
    if not all(0 <= value(match[2]) <= value(match[1]) <= value(match[3]) <= 100 for match in (a, b)):
        return None
    delta = value(d)
    if not (0 <= value(p) <= 1 and -100 <= delta <= 100):
        return None
    if abs(value(a[1]) - value(b[1]) - delta) > Decimal(".015"):
        return None
    return a.groups(), b.groups(), (d,), (p,)


def band_label(y, rules, choices):
    band = next(((top, bottom) for top, bottom in pairwise(rules) if top < y < bottom), None)
    if band is None:
        return None
    labels = [item for item in choices if band[0] < center(item["box"]) < band[1]]
    if len(labels) == 1:
        return labels[0], band[1]
    return None


def comparison_records(table, cells, evidence, layout, codes, pdf_path, pdf_sha):
    import pdfplumber

    headers, baseline, target = layout
    caption = table["caption"]
    page_no = table["page"]
    table_boxes = [item["box"] for item in evidence.values() if item.get("precision") == "table"]
    if len(table_boxes) != 1:
        return
    table_box, = table_boxes
    by_column = [header["column"] for header in headers]

    def anchor(cell):
        refs = [evidence[ref] for ref in cell["evidence_ids"] if ref in evidence]
        return refs[0] if len(refs) == 1 else None

    header_boxes = [anchor(header) for header in headers]
    if not all(header_boxes):
        return
    x_failure, x_model, x_test = (header_boxes[i]["box"][0] for i in (1, 2, 6))
    rows = defaultdict(dict)
    for cell in cells:
        if cell["row"] > headers[0]["row"] and (ref := anchor(cell)):
            rows[cell["row"]][cell["column"]] = (cell, ref)
    labels = [[row[col][1] for row in rows.values() if col in row] for col in by_column[:2]]
    with pdf_path.open("rb") as stream:
        if file_digest(stream, "sha256").hexdigest() != pdf_sha:
            raise ValueError(f"PDF checksum differs: {pdf_path}")
    with pdfplumber.open(pdf_path) as pdf:
        page = pdf.pages[page_no - 1]
        edges = [(e["x0"] / page.width, e["top"] / page.height, e["x1"] / page.width)
                 for e in page.horizontal_edges]
        words = [{"text": w["text"], "box": [w["x0"] / page.width, w["top"] / page.height,
                                           w["x1"] / page.width, w["bottom"] / page.height]}
                 for w in page.extract_words(y_tolerance=2)]
    edges = [(x, y) for x, y, right in edges
             if x >= table_box[0] - .005 and table_box[1] - .005 <= y <= table_box[3] + .005
             and right >= x_test and x < x_model]
    dataset_rules = sorted({y for x, y in edges if x < x_failure})
    failure_rules = sorted({y for _, y in edges})
    numeric_rows = [tuple(row[col] for col in by_column[3:]) for _, row in sorted(rows.items())
                    if all(col in row for col in by_column[3:])]
    if not numeric_rows:
        return
    x_value = min(parts[0][1]["box"][0] for parts in numeric_rows)
    model_words = [w for w in words if x_model - .005 <= w["box"][0] < x_value - .002]
    for index, parts in enumerate(numeric_rows):
        row_cells, sources = zip(*parts)
        top, bottom = sources[0]["box"][1::2]
        y = center(sources[0]["box"])
        selected = [band_label(y, rules, choices)
                    for rules, choices in zip((dataset_rules, failure_rules), labels, strict=True)]
        if not all(selected) or not (stats := parsed_statistics(row_cells)):
            continue
        (dataset, _), (failure, band_end) = selected
        if not (metric := codes.get(entity_key(failure["text"]))):
            continue
        if any(abs(center(source["box"]) - y) > .003 for source in sources):
            continue
        next_top = numeric_rows[index + 1][0][1]["box"][1] if index + 1 < len(numeric_rows) else band_end
        matching = [w for w in model_words if top <= center(w["box"]) < min(next_top, band_end)]
        matching.sort(key=lambda w: (center(w["box"]), w["box"][0]))
        model_name = "".join(w["text"] for w in matching)
        model = None
        if (len(matching) == 1 or (len(matching) == 2 and matching[0]["text"].endswith("-")
                                   and center(matching[1]["box"]) > bottom)):
            if re.fullmatch(r"[\w.-]+", model_name):
                model = {"id": f"pdf-model-{row_cells[0]['row']}", "page": page_no, "text": model_name,
                         "precision": "text_fragment",
                         "box": [min(w["box"][0] for w in matching), min(w["box"][1] for w in matching),
                                 max(w["box"][2] for w in matching), max(w["box"][3] for w in matching)]}
        variants = ((metric, metric + " 95% CI lower", metric + " 95% CI upper"),
                    (metric, metric + " 95% CI lower", metric + " 95% CI upper"),
                    (metric + " reduction vs " + baseline.removesuffix(" RAG"),),
                    (metric + " paired McNemar p",))
        for i, (cell, source, tokens, names) in enumerate(zip(row_cells, sources, stats, variants, strict=True)):
            header = header_boxes[i + 3]
            unit = ("%", "%", "percentage_points", "probability")[i]
            method = baseline if i == 0 else target
            method_header = header_boxes[3 if i == 0 else 4]
            scope = Scope(TableTarget(page_no, table["label"]), row=cell["row"], column=cell["column"])
            locator = Source(page_no, table["label"], cell["row"], cell["column"],
                             (header["text"],), tuple(source["box"]), (source["id"],))
            for token, name in zip(tokens, names, strict=True):
                claims = {"metric": (name, (failure, header, caption)), "unit": (unit, (header, caption)),
                          "method": (method, (method_header, caption)), "dataset": (dataset["text"], (dataset,))}
                if model:
                    claims["generator"] = (model_name, (model, header_boxes[2]))
                bound = assemble(Observation("qa", token, locator, ("generator",)),
                                 (Assertion(field, field_value, scope, tuple(e["id"] for e in refs))
                                  for field, (field_value, refs) in claims.items()))
                fields = {field: {"value": option.value, "rule": "ruled-paired-comparison",
                                  "sources": claims[field][1]} for field, option in bound.confirmed.items()}
                yield {"page": page_no, "table": table["label"], "row": cell["row"], "column": cell["column"],
                       "raw": cell["text"], "raw_component": token,
                       "value": token.replace("−", "-").replace(",", ""), "fields": fields,
                       "open": sorted(bound.open_fields), "numeric_rule": "paired-comparison:" + name,
                       "number_sources": [source]}
