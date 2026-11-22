"""Compare caption search with text-layer search inside a marked figure region."""

import argparse
from hashlib import file_digest
from importlib.metadata import version
import json
from math import isfinite
from os.path import relpath
from pathlib import Path
from platform import python_version
import re
import sys

import pdfplumber


def write_attempt(path, attempt):
    path.write_text(json.dumps(attempt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def compare(args):
    pdf = args.article_map / "source.pdf"
    inventory_path = args.article_map / "inventory.json"
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    with pdf.open("rb") as stream:
        sha256 = file_digest(stream, "sha256").hexdigest()
    if sha256 != inventory["sha256"]:
        raise ValueError("The article map and source PDF have different SHA-256 identifiers.")

    captions = [item for item in inventory["candidates"] if item["kind"] == "figure"
                and item["page"] == args.page and item["label"] == args.figure]
    if len(captions) != 1:
        raise ValueError("Expected one figure caption at the selected address in the article map.")
    caption = captions[0]
    words = args.query.split()
    if not words:
        raise ValueError("The search query must contain text.")
    phrase = r"[ \t]+".join(re.escape(word) for word in words)
    pattern = re.compile(rf"(?<!\w){phrase}(?!\w)", re.IGNORECASE)
    region = tuple(args.box)
    left, top, right, bottom = region
    if not all(map(isfinite, region)) or not (left < right and top < bottom):
        raise ValueError("Expected a nonempty figure box in PDF points.")

    caption_matches = []
    if match := pattern.search(caption["text"]):
        caption_matches = [{"text": match[0], "page": args.page, "kind": "caption", "box": caption["box"]}]

    text_settings = {"x_tolerance": 3, "y_tolerance": 3, "layout": False}
    dpi = 144
    attempt = {
        "schema_version": 1,
        "source": {"document": inventory["document"], "sha256": sha256,
                   "pdf": relpath(pdf, args.out), "inventory": relpath(inventory_path, args.out)},
        "figure": {"page": args.page, "label": args.figure, "box_pt": region, "caption": caption},
        "query": args.query,
        "command": [sys.executable, *sys.argv],
        "versions": {"python": python_version(), **{name: version(name) for name in
                     ("pdfplumber", "pdfminer.six", "pypdfium2", "Pillow")}},
        "settings": {"pattern": pattern.pattern, "ignore_case": True,
                     "coordinates": "left, top, right, bottom from the page's upper-left corner",
                     "text_layer": text_settings, "preview_dpi": dpi},
    }
    with pdfplumber.open(pdf) as document:
        page = document.pages[args.page - 1]
        figure = page.within_bbox(region)
        args.out.mkdir(parents=True)
        write_attempt(args.out / "caption.json", {**attempt, "mode": "caption", "matches": caption_matches})
        labels = [
            {"text": item["text"], "page": args.page, "kind": "figure-label",
             "box_pt": [item["x0"], item["top"], item["x1"], item["bottom"]]}
            for item in figure.search(pattern, return_chars=False, return_groups=False, **text_settings)
        ]
        preview = page.crop(region).to_image(resolution=dpi, force_mediabox=True)
        for label in labels:
            preview.draw_rect(tuple(label["box_pt"]), fill=(255, 196, 0, 45), stroke=(196, 30, 30, 255), stroke_width=2)
        preview.save(args.out / "figure.png", quantize=False)
        write_attempt(args.out / "caption-and-labels.json", {
            **attempt, "mode": "caption-and-labels", "matches": caption_matches + labels,
            "figure_text": figure.extract_text(**text_settings), "preview": "figure.png",
        })


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("article_map", type=Path, help="Directory made by rag_mvp inspect")
    parser.add_argument("--page", type=int, required=True, help="One-based PDF page number")
    parser.add_argument("--figure", required=True, help="Figure label from the article map")
    parser.add_argument("--box", type=float, nargs=4, required=True, metavar=("LEFT", "TOP", "RIGHT", "BOTTOM"),
                        help="Figure region in PDF points, excluding the caption and body text")
    parser.add_argument("--query", required=True)
    parser.add_argument("--out", type=Path, required=True, help="New directory for the two attempts and preview")
    args = parser.parse_args()
    try:
        compare(args)
    except (ValueError, OSError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
