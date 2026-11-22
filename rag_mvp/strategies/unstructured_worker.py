"""Run with the operator's isolated Python, as a script rather than a package module."""

import json
import os
import subprocess
import sys
from copy import deepcopy
from importlib.metadata import version
from pathlib import Path
from platform import python_version
from unittest.mock import patch


def main(pdf: Path, page: int, output: Path) -> None:
    pins = {"unstructured": "0.18.32", "unstructured-inference": "1.2.0"}
    for package, expected in pins.items():
        installed = version(package)
        if installed != expected:
            raise RuntimeError(f"Для перехвата геометрии нужен {package}=={expected}, установлен {installed}.")

    padding = 16
    table_psm = 6
    threads = min(4, os.cpu_count() or 1)
    if hasattr(os, "sched_getaffinity"):
        cpus = sorted(os.sched_getaffinity(0))[:threads]
        os.sched_setaffinity(0, cpus)
        threads = len(cpus)
    os.environ.update(EXTRACT_TABLE_AS_CELLS="true", TABLE_IMAGE_CROP_PAD=str(padding),
                      OMP_THREAD_LIMIT=str(threads), OMP_NUM_THREADS=str(threads),
                      MKL_NUM_THREADS=str(threads), OPENBLAS_NUM_THREADS=str(threads))

    import numpy as np
    import torch
    from unstructured.partition.pdf import partition_pdf
    from unstructured.partition.pdf_image import ocr
    from unstructured.partition.utils.constants import OCR_AGENT_TESSERACT
    from unstructured_inference.models.tables import UnstructuredTableTransformerModel

    torch.set_num_threads(threads)
    torch.set_num_interop_threads(1)
    tesseract = subprocess.check_output(["tesseract", "--version"], text=True).splitlines()[0]
    model = UnstructuredTableTransformerModel
    supplement = ocr.supplement_element_with_table_extraction
    predict = model.predict
    table_groups = []
    active = iter(())

    def supplement_with_crop(elements, image, tables_agent, ocr_agent, **kwargs):
        nonlocal active
        entries = []
        for index, label in enumerate(elements.element_class_ids):
            if elements.element_class_id_map[label] != "Table":
                continue
            bounds = elements.element_coords[index].tolist()
            left, top, right, bottom = bounds
            # Match Pillow's integer rounding when Unstructured slices the original raster.
            crop = [round(value) for value in (left - padding, top - padding, right + padding, bottom + padding)]
            entries.append({"page": page, "raster_size": image.size, "layout_bbox": bounds, "crop_bbox": crop})
        table_groups.extend(entries)
        active = iter(entries)

        read_hocr = ocr_agent.image_to_data_with_character_confidence_filter

        def read_table_hocr(*args, **kwargs):
            kwargs["config"] = f"{kwargs.get('config', '')} --psm {table_psm}"
            return read_hocr(*args, **kwargs)

        try:
            with patch.object(ocr_agent, "image_to_data_with_character_confidence_filter", read_table_hocr):
                return supplement(elements, image, tables_agent, ocr_agent, **kwargs)
        finally:
            active = iter(())

    def predict_with_geometry(self, crop_image, **kwargs):
        tokens = deepcopy(kwargs.get("ocr_tokens"))
        result = predict(self, crop_image, **kwargs)
        entry = next(active, None)
        if entry is not None and isinstance(result, list):
            left, top, right, bottom = entry["crop_bbox"]
            entry["ocr_tokens"] = tokens
            entry["raw_cells"] = deepcopy(result)
            if crop_image.size != (right - left, bottom - top):
                entry["crop_bbox"] = None
        return result

    options = {
        "strategy": "hi_res",
        "infer_table_structure": True,
        "starting_page_number": page,
        "languages": ["eng"],
        "hi_res_model_name": "yolox_quantized",
        "pdf_image_dpi": 350,
        "ocr_agent": OCR_AGENT_TESSERACT,
        "table_ocr_agent": OCR_AGENT_TESSERACT,
    }
    with (
        patch.object(ocr, "supplement_element_with_table_extraction", supplement_with_crop),
        patch.object(model, "predict", predict_with_geometry),
    ):
        elements = partition_pdf(filename=str(pdf), **options)

    def json_value(value):
        if isinstance(value, np.ndarray):
            return value.tolist()
        if isinstance(value, np.generic):
            return value.item()
        raise TypeError(f"Unexpected Unstructured value: {type(value).__name__}")

    packages = (*pins, "torch", "torchvision", "transformers", "onnxruntime", "pillow", "unstructured-pytesseract")
    artifacts = {
        "engine.json": {
            "settings": {**options, "EXTRACT_TABLE_AS_CELLS": True, "TABLE_IMAGE_CROP_PAD": padding,
                         "TABLE_TESSERACT_PSM": table_psm, "cpu_threads": threads, "hf_hub_offline": True},
            "versions": {"unstructured-python": python_version(), "tesseract": tesseract.removeprefix("tesseract "),
                         **{package: version(package) for package in packages}},
        },
        "elements.json": [element.to_dict() for element in elements],
        "table-transformer-cells.json": table_groups,
    }
    for name, value in artifacts.items():
        (output / name).write_text(json.dumps(value, ensure_ascii=False, default=json_value), encoding="utf-8")


if __name__ == "__main__":
    main(Path(sys.argv[1]), int(sys.argv[2]), Path(sys.argv[3]))
