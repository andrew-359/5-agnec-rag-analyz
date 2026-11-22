"""Verify the direct prose-to-table link on the saved D011 extraction."""

import csv
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from unittest import TestCase, skipUnless


@skipUnless(os.getenv("RAG_D011_GRID") and os.getenv("RAG_D011_TEXT") and os.getenv("RAG_DEV_CORPUS"),
            "Нужны сохранённые входы таблицы D011 и комплект участника.")
class ScopedTableMetricCliTest(TestCase):
    def test_explicit_table_reference_binds_a_metric_without_borrowing_from_neighbor(self):
        grid = Path(os.environ["RAG_D011_GRID"])
        pdf = Path(os.environ["RAG_DEV_CORPUS"]) / "dev/D011.pdf"
        document = json.loads(Path(os.environ["RAG_D011_TEXT"]).read_text())
        direct = "1ba29927860a-p21-text-25"

        with TemporaryDirectory() as directory:
            folder = Path(directory)
            for with_reference in (True, False):
                with self.subTest(with_reference=with_reference):
                    text = folder / f"text-{with_reference}.json"
                    fragments = [item for item in document["fragments"] if with_reference or item["id"] != direct]
                    text.write_text(json.dumps({**document, "fragments": fragments}))
                    output = folder / f"output-{with_reference}"
                    completed = subprocess.run([
                        sys.executable, "-m", "rag_mvp", "extract", str(pdf), "--page", "21", "--table", "10",
                        "--grid-input", str(grid), "--article-text", str(text), "--out", str(output),
                    ], capture_output=True, text=True, timeout=30)
                    self.assertEqual(completed.returncode, 0, completed.stderr)
                    artifact = json.loads((output / "run.json").read_text())
                    with (output / "results.csv").open() as file:
                        rows = list(csv.DictReader(file))
                    if not with_reference:
                        self.assertEqual(rows, [])
                        self.assertTrue(any(item["raw_text"] == "1.00" for item in artifact["remarks"]))
                        continue

                    self.assertEqual(len(rows), 20)
                    self.assertEqual({json.loads(row["parameters"])["Injection Rate (%)"] for row in rows},
                                     {20, 40, 60, 80, 100})
                    target, = [item for item in artifact["results"]
                               if item["source"]["row"] == 1 and item["source"]["column"] == 1]
                    self.assertEqual((target["method"], target["metric"], target["raw_value"]), ("KMW", "WRR", "1.00"))
                    self.assertEqual(target["parameters"], {"Injection Rate (%)": 20})
                    self.assertEqual(target["source"]["page"], 21)
                    self.assertTrue(target["source"]["box"])
                    self.assertIn(direct, target["condition_sources"]["method"])
                    self.assertIn(direct, target["condition_sources"]["metric"])
                    self.assertTrue(any(row["method"] == "KMW" and row["metric"] == "WRR" for row in rows))
