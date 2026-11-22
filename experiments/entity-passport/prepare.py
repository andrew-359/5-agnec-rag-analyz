"""Read and cache positioned table grids without consulting dev_gold."""
import argparse
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict
from hashlib import file_digest
import json
import os
from pathlib import Path
import re
import sys
from time import perf_counter

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))

from rag_mvp.article_context.captions import TABLE_LINE, table_caption_line, text_for_matching
from rag_mvp.strategies.text_pdfplumber import load_article_fragments, read_article_fragments


def read(path):
    return json.loads(path.read_bytes())


def write(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def digest(path):
    with path.open("rb") as stream:
        return file_digest(stream, "sha256").hexdigest()


def label_of(text, relaxed=False):
    text = text_for_matching(text)
    caption = table_caption_line(text)
    if not caption and relaxed:
        candidate = TABLE_LINE.match(text)
        if candidate and candidate["tail"].lstrip()[:1].isupper():
            caption = candidate
    return caption["label"] if caption else None


def freeze_table(table, document, page, caption, label):
    from rag_mvp.strategies.parser import item_evidence, normalized_box

    size = document.pages[page].size
    table_source = asdict(item_evidence(table, document, page, "table", caption["text"]))
    cells = []
    evidence = [table_source, caption]
    for cell in table.data.table_cells:
        row, column = cell.start_row_offset_idx, cell.start_col_offset_idx
        identifier = f"cell-{row}-{column}"
        box = normalized_box(cell.bbox, size)
        role = "column_header" if cell.column_header else "row_header" if cell.row_header else "data"
        cells.append(dict(id=identifier, row=row, column=column, row_span=cell.row_span,
                          column_span=cell.col_span, role=role, text=cell.text.strip(),
                          evidence_ids=[identifier] if box else []))
        if box:
            evidence.append(dict(id=identifier, page=page, box=box, text=cell.text.strip(), precision="cell"))
    return {"page": page, "label": label, "caption": caption,
            "cells": cells, "evidence": evidence, "reader": "docling-tableformer"}


def saved_table(path):
    data = read(path)
    if "read" in data:
        body = data["read"]
    else:
        body = data
    grid = body.get("grid")
    if not grid:
        return None
    cells = grid.get("cells", body.get("cells", ()))
    if not any(cell.get("text", "").strip() for cell in cells):
        return None
    caption = next((item for item in body["evidence"] if item["id"] == grid.get("caption_evidence_id")), None)
    candidate = label_of(caption["text"]) if caption else None
    if candidate is None:
        return None
    return {"page": data["page"], "label": candidate, "caption": caption, "cells": cells,
            "evidence": body["evidence"], "reader": data.get("strategy"), "reused_from": str(path)}


def saved_index(cache, ids):
    grids = {sha: {} for sha in ids}
    text = {}
    for pattern in ("grid-*.json", "table-grid.json", "run.json", "article-text*.json"):
        for artifact in sorted(cache.rglob(pattern)):
            try:
                data = read(artifact)
                if pattern.startswith("article-text"):
                    filename = artifact.name
                    is_reader_output = (filename == "article-text.json" or
                                        re.fullmatch(r"article-text-with-ocr-page-\d+\.json", filename)
                                        and "RapidOCR" in data.get("engine_version", ""))
                    if is_reader_output and data.get("sha256") in ids:
                        load_article_fragments(artifact, data["sha256"])
                        old = text.get(data["sha256"])
                        if old is None or old.name != "article-text.json" and filename == "article-text.json":
                            text[data["sha256"]] = artifact
                    continue
                sha = data.get("sha256")
                if pattern == "run.json":
                    if not data.get("cells") or data["document"].removesuffix(".pdf") not in ids.values():
                        continue
                    pdf = artifact.with_name("source.pdf")
                    if not pdf.is_file():
                        continue
                    sha = digest(pdf)
                if sha not in ids:
                    continue
                table = saved_table(artifact)
            except (ValueError, TypeError, KeyError, OSError):
                continue
            if table:
                key = (table["page"], table["label"].casefold())
                # Prefer an actual saved grid. Otherwise keep the widest positioned grid.
                current = grids[sha].get(key)
                if current is None or (pattern == "run.json" and "run.json" in current["reused_from"]
                                       and len(table["cells"]) > len(current["cells"])):
                    grids[sha][key] = table
    return grids, text


def read_document(pdf, output, cached, text_path, relaxed_captions):
    from docling.datamodel.pipeline_options import OcrMode, RapidOcrOptions
    from rag_mvp.strategies.parser import converter_for, docling_options, table_evidence, item_evidence, docling_versions

    started = perf_counter()
    native = read_article_fragments(pdf)
    native_pages = {item.page for item in native.fragments}
    use_ocr = len(native_pages) < len(native.pages) / 2
    options = {"do_ocr": use_ocr, "do_table_structure": True}
    if use_ocr:
        options["ocr_options"] = RapidOcrOptions(
            backend="onnxruntime", lang=["en"], mode=OcrMode.FULL_PAGE, scale=2,
        )
    result = converter_for(docling_options(**options)).convert(pdf, raises_on_error=False)
    document = result.document
    text = read(text_path)["fragments"] if text_path else list(map(asdict, native.fragments))
    text_pages = {item["page"] for item in text}
    for index, item in enumerate(document.texts, 1):
        for location in item.prov[:1]:
            if location.page_no not in text_pages and item.text.strip():
                text.append(asdict(item_evidence(item, document, location.page_no, f"ocr-{index}", item.text.strip())))
    text_by_page = {}
    for item in text:
        if item["box"] and label_of(item["text"], relaxed_captions):
            text_by_page.setdefault(item["page"], []).append(item)

    tables = dict(cached)
    unresolved = []
    for table in document.tables:
        if not table.prov:
            continue
        page = table.prov[0].page_no
        _, located = table_evidence(table, document, page, 0)
        caption = asdict(located)
        if not label_of(caption["text"], relaxed_captions) or not caption["box"]:
            # Docling sometimes detaches captions. Use the nearest uniquely scoped PDF caption.
            box = item_evidence(table, document, page, "table", "").box
            candidates = []
            if box:
                for item in text_by_page.get(page, ()):
                    x, y, x1, y1 = item["box"]
                    overlap = min(x1, box[2]) - max(x, box[0])
                    distance = max(box[1] - y1, y - box[3], 0)
                    numbered_heading = not TABLE_LINE.match(text_for_matching(item["text"]))["tail"].strip()
                    if overlap > 0 and (distance < .045 or relaxed_captions and numbered_heading and y1 <= box[1] and distance < .22):
                        candidates.append((distance, item))
            candidates.sort(key=lambda pair: pair[0])
            if candidates and (len(candidates) == 1 or candidates[1][0] - candidates[0][0] > .008):
                caption = {**candidates[0][1], "id": "caption"}
        label = label_of(caption["text"], relaxed_captions) if caption["box"] else None
        if label is None:
            unresolved.append({"page": page, "caption": caption["text"], "cells": len(table.data.table_cells)})
            continue
        key = (page, label.casefold())
        if key not in tables:
            tables[key] = freeze_table(table, document, page, caption, label)
    report = {"pdf": str(pdf), "sha256": native.sha256, "doc": pdf.stem, "pages": native.pages,
              "text": text, "tables": list(tables.values()), "unresolved": unresolved,
              "status": result.status.value, "errors": [item.error_message for item in result.errors],
              "ocr": use_ocr, "versions": docling_versions(),
              "text_reused_from": str(text_path) if text_path else None,
              "seconds": round(perf_counter() - started, 2)}
    write(output / f"{pdf.stem}.json", report)
    return pdf.stem, report["status"], len(tables), len(unresolved), report["seconds"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dev", type=Path)
    parser.add_argument("cache", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--jobs", type=int, default=3)
    parser.add_argument("--article", action="append", help="Refresh these frozen inputs from the same PDF sources.")
    parser.add_argument("--relaxed-captions", action="store_true",
                        help="Allow a detached standalone table number farther above a grid.")
    args = parser.parse_args()
    pdfs = sorted(args.dev.resolve().glob("D*.pdf"))
    if len(pdfs) != 21:
        parser.error("Expected the 21 dev PDFs.")
    args.output.mkdir(parents=True, exist_ok=True)
    ids = {digest(pdf): pdf.stem for pdf in pdfs}
    grids, texts = saved_index(args.cache.resolve(), ids)
    print("Cached grids and texts:", sum(map(len, grids.values())), len(texts), flush=True)
    with ProcessPoolExecutor(max_workers=args.jobs) as workers:
        from concurrent.futures import as_completed
        jobs = {workers.submit(read_document, pdf, args.output, grids[sha], texts.get(sha), args.relaxed_captions): doc
                for sha, doc in ids.items() for pdf in (args.dev.resolve() / f"{doc}.pdf",)
                if (doc in args.article if args.article else not (args.output / f"{doc}.json").exists())}
        for future in as_completed(jobs):
            print(*future.result(), flush=True)


if __name__ == "__main__":
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    main()
