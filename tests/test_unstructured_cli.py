import csv
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from decimal import Decimal, InvalidOperation
from pathlib import Path
from unittest.mock import patch

from pypdfium2 import PdfDocument

from rag_mvp.__main__ import extract as save_run


def extract(pdf, out, page=11, table=1, **options):
    return subprocess.run([
        sys.executable, "-m", "rag_mvp", "extract", str(pdf), "--page", str(page), "--table", str(table),
        "--strategy", "unstructured", "--out", str(out),
    ], **options)


class UnstructuredCliTest(unittest.TestCase):
    def test_duplicate_table_captions_leave_the_selection_open(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.pdf"
            with PdfDocument.new() as pdf:
                pdf.new_page(100, 100).close()
                pdf.save(source)
            out = Path(directory) / "run"
            elements = [{"type": kind, "text": text, "metadata": {"page_number": 1}}
                        for text in ("First", "Second")
                        for kind, text in (("Table", text), ("FigureCaption", f"Table 1. {text}"))]
            raw = {"elements.json": elements, "table-transformer-cells.json": []}
            with patch("rag_mvp.strategies.unstructured.worker_output", return_value=(raw, {"versions": {}, "settings": {}})):
                save_run(source, 1, 1, out, "unstructured")
            run = json.loads((out / "run.json").read_text(encoding="utf-8"))
            self.assertIsNone(run["grid"])
            self.assertEqual(run["results"], [])
            (remark,) = run["remarks"]
            self.assertIn("неоднозначно", remark["reason"])
            self.assertIn("Table 1", remark["reason"])
            self.assertTrue((out / run["source"]["preview"]).is_file())
            self.assertEqual(json.loads((out / "elements.json").read_text(encoding="utf-8")), elements)

    def test_missing_isolated_environment_explains_how_to_run(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.pdf"
            with PdfDocument.new() as pdf:
                pdf.new_page(100, 100).close()
                pdf.save(source)

            out = Path(directory) / "run"
            env = os.environ.copy()
            env.pop("RAG_UNSTRUCTURED_PYTHON", None)
            result = extract(source, out, page=1, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn("RAG_UNSTRUCTURED_PYTHON", result.stderr)
            self.assertFalse(out.exists())

            unavailable = Path(directory) / "missing-venv" / "bin" / "python"
            env["RAG_UNSTRUCTURED_PYTHON"] = str(unavailable)
            result = extract(source, out, page=1, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn(str(unavailable), result.stderr)
            self.assertFalse(out.exists())


@unittest.skipUnless(os.environ.get("RAG_DEV_CORPUS") and os.environ.get("RAG_UNSTRUCTURED_PYTHON"),
                     "Нужны комплект участника и изолированное окружение Unstructured.")
class D021Test(unittest.TestCase):
    def test_missing_caption_keeps_the_requested_page_and_raw_output(self):
        source = Path(os.environ["RAG_DEV_CORPUS"]) / "dev" / "D021.pdf"
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "run"
            extract(source, out, table=2, check=True)
            run = json.loads((out / "run.json").read_text(encoding="utf-8"))
            self.assertIsNone(run["grid"])
            self.assertEqual(run["cells"], [])
            self.assertEqual(run["results"], [])
            self.assertEqual(run["remarks"][0]["source"]["evidence_ids"], ["page-11"])
            self.assertIn("Table 2", run["remarks"][0]["reason"])
            self.assertTrue((out / run["source"]["preview"]).is_file())
            raw = json.loads((out / "elements.json").read_text(encoding="utf-8"))
            self.assertTrue(any(element["type"] == "Table" for element in raw))

    def test_run_keeps_grid_and_highlight_links_without_results(self):
        corpus = Path(os.environ["RAG_DEV_CORPUS"])
        source = corpus / "dev" / "D021.pdf"
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "run"
            extract(source, out, check=True)
            run = json.loads((out / "run.json").read_text(encoding="utf-8"))

            self.assertEqual(run["strategy"], "unstructured")
            self.assertEqual(run["versions"]["unstructured"], "0.18.32")
            self.assertEqual(run["settings"]["TABLE_TESSERACT_PSM"], 6)
            self.assertEqual(run["source"]["page"], 11)
            self.assertEqual(run["source"]["sha256"], hashlib.sha256(source.read_bytes()).hexdigest())
            self.assertTrue((out / run["source"]["pdf"]).is_file())
            self.assertTrue((out / run["source"]["preview"]).is_file())
            self.assertTrue((out / "index.html").is_file())
            self.assertEqual(set(run["raw_artifacts"]), {"elements.json", "table-transformer-cells.json"})
            for artifact in run["raw_artifacts"]:
                self.assertTrue((out / artifact).is_file())
            self.assertEqual(run["results"], [])
            with (out / "results.csv").open(newline="", encoding="utf-8") as stream:
                self.assertEqual(list(csv.DictReader(stream)), [])

            grid = run["grid"]
            self.assertEqual((grid["label"], grid["rows"], grid["columns"]), ("Table 1", 5, 4))
            cells = {(cell["row"], cell["column"]): cell for cell in run["cells"]}
            self.assertEqual(len(cells), 20)
            self.assertEqual(cells[3, 0]["text"], "SQuAD")
            self.assertEqual(cells[1, 0]["text"], "")
            evidence = {fragment["id"]: fragment for fragment in run["evidence"]}
            caption = evidence[grid["caption_evidence_id"]]
            self.assertTrue(caption["text"].startswith("Table 1."))
            context, = (evidence[id] for id in run["context_evidence_ids"])
            self.assertIn("ROC-AUC", context["text"])
            self.assertEqual(context["precision"], "text_fragment")
            self.assertLess(context["box"][3], evidence[grid["evidence_ids"][0]]["box"][1])

            with (corpus / "dev_gold" / "results.csv").open(newline="", encoding="utf-8") as stream:
                expected = {(row["dataset_id"], row["config_id"]): Decimal(row["raw_value"])
                            for row in csv.DictReader(stream)
                            if (row["publication_id"], row["source_page"], row["source_table"], row["metric_name"])
                            == ("D021", "11", "Table 1", "ROC-AUC")}
            # Manually read values and centers on the 1700 × 2200 PDF render at 200 dpi.
            manual = [
                ("0.86", 782, 1463), ("0.65", 935, 1457), ("0.63", 1087, 1447),
                ("0.92", 783, 1490), ("0.83", 936, 1486), ("0.65", 1088, 1477),
                ("0.84", 783, 1520), ("0.80", 937, 1512), ("0.78", 1090, 1505),
            ]
            wrong = {}
            for index, (value, x, y) in enumerate(manual):
                row, column = divmod(index, 3)
                dataset = ("TRUE", "SQuAD-G", "PubmedQA")[row]
                config = ("C01", "C03", "C04")[column]
                self.assertEqual(Decimal(value), expected[dataset, config])
                cell = cells[row + 2, column + 1]
                try:
                    matches = Decimal(cell["text"]) == Decimal(value)
                except InvalidOperation:
                    matches = False
                if not matches:
                    wrong[cell["id"]] = cell["text"]

                fragment = evidence[cell["evidence_ids"][0]]
                self.assertEqual((fragment["page"], fragment["precision"]), (11, "estimated_cell"))
                left, top, right, bottom = fragment["box"]
                self.assertTrue(left <= x / 1700 <= right and top <= y / 2200 <= bottom, cell)

            self.assertEqual(wrong, {"cell-2-3": "ies", "cell-4-3": "0.78 angsmith faithfuln"})
            for column in (1, 2, 3):
                nearby = [remark for remark in run["remarks"] if remark["source"]["column"] == column
                          and remark["source"]["row"] in (0, 1)]
                self.assertTrue(nearby, column)
                for remark in nearby:
                    header = cells[remark["source"]["row"], column]
                    self.assertEqual(remark["raw_text"], header["text"])
                    self.assertEqual(remark["source"]["evidence_ids"], header["evidence_ids"])
                    self.assertEqual(evidence[header["evidence_ids"][0]]["precision"], "estimated_cell")
            for cell_id, text in wrong.items():
                remark = next(remark for remark in run["remarks"] if remark["source"]["cell_id"] == cell_id)
                self.assertEqual(remark["raw_text"], text)
                self.assertEqual(remark["source"]["evidence_ids"], next(
                    cell["evidence_ids"] for cell in cells.values() if cell["id"] == cell_id
                ))
