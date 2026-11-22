"""Run PP-StructureV3 in the optional Paddle environment on a local raster."""

import json
from importlib.metadata import version
from pathlib import Path
import sys


OPTIONS = {
    "device": "cpu",
    "cpu_threads": 1,
    "enable_mkldnn": False,
    "lang": "en",
    "ocr_version": "PP-OCRv5",
    "use_doc_orientation_classify": False,
    "use_doc_unwarping": False,
    "use_textline_orientation": False,
    "use_region_detection": False,
    "use_seal_recognition": False,
    "use_formula_recognition": False,
    "use_chart_recognition": False,
}
# The e2e path uses one structure prediction for both HTML order and cell boxes.
TABLE_OPTIONS = {
    "use_table_orientation_classify": False,
    "use_e2e_wired_table_rec_model": True,
    "use_e2e_wireless_table_rec_model": True,
}


def main(image: Path, output: Path) -> None:
    from paddleocr import PPStructureV3

    if not image.is_file():
        raise ValueError("PaddleOCR ожидает локальный растровый файл.")
    pipeline = PPStructureV3(**OPTIONS)
    page, = pipeline.predict(str(image), **TABLE_OPTIONS)
    result = page.json["res"]
    payload = {
        "table_res_list": result["table_res_list"],
        "table_blocks": [
            {"html": block["block_content"], "box": block["block_bbox"]}
            for block in result["parsing_res_list"] if block["block_label"] == "table"
        ],
        "versions": {name: version(name) for name in ("paddleocr", "paddlepaddle", "paddlex")},
        "options": {**OPTIONS, **TABLE_OPTIONS},
    }
    output.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main(*map(Path, sys.argv[1:]))
