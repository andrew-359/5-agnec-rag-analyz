import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


@unittest.skipUnless(os.environ.get("RAG_DEV_CORPUS"), "Нужен комплект участника в RAG_DEV_CORPUS.")
class OcrTableFormerCliTest(unittest.TestCase):
    def test_p056_publishes_a_verified_value_and_preserves_glued_values_for_review(self):
        pdf = Path(os.environ["RAG_DEV_CORPUS"]) / "test" / "P056.pdf"
        region = {
            "box": [68 / 612, 102 / 792, 555 / 612, 330 / 792],
            "bounds_status": "confirmed",
            "caption_status": "confirmed",
            "designation": "Table II",
            "caption_box": [139 / 612, 73.5 / 792, 488 / 612, 101.5 / 792],
            "caption_text": (
                "TABLE II PERFORMANCE COMPARISON WITH SOTA METHODS "
                "ON THE ACADCHALL AND R-FLUE-FIQA DATASETS."
            ),
        }
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "run"
            process = subprocess.run([
                sys.executable, "-m", "rag_mvp", "extract", str(pdf), "--page", "8", "--table", "2",
                "--table-region", json.dumps(region), "--strategy", "docling-ocr-tableformer",
                "--out", str(out),
            ], capture_output=True, text=True)
            self.assertEqual(process.returncode, 0, process.stderr)
            run = json.loads((out / "run.json").read_text(encoding="utf-8"))

        evidence = {item["id"]: item for item in run["evidence"]}
        readings = [item for item in run["results"]
                    if (item["method"], item["generator"], item["dataset"], item["metric"])
                    == ("PolyRAG", "GPT-4", "AcadChall", "F1")]
        (reading,) = readings
        self.assertEqual(reading["value"], "0.8109")
        self.assertEqual(reading["source"]["page"], 8)
        (value_id,) = reading["source"]["evidence_ids"]
        self.assertEqual(evidence[value_id]["text"], "0.8109")
        self.assertEqual(
            [round(value, 3) for value in evidence[value_id]["box"]],
            [0.609, 0.386, 0.653, 0.405],
        )
        for field, label in [("method", "PolyRAG"), ("generator", "GPT-4"), ("dataset", "AcadChall"), ("metric", "F1")]:
            (ref,) = reading["condition_sources"][field]
            self.assertEqual(evidence[ref]["text"], label)

        glued = {"0.5032 0.5105", "0.5094 0.5791", "0.4651 0.5315"}
        self.assertTrue(glued.intersection(item["raw_text"] for item in run["remarks"]))
        self.assertFalse(glued.intersection(item["raw_value"] for item in run["results"]))
        self.assertTrue(all(result["source"]["box"] for result in run["results"]))


if __name__ == "__main__":
    unittest.main()
