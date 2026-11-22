"""Replay the latency benchmark of P069 from its saved grid and text."""

import csv
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from unittest import TestCase, skipUnless


@skipUnless(os.getenv("RAG_P069_GRID") and os.getenv("RAG_P069_TEXT") and os.getenv("RAG_DEV_CORPUS"),
            "Нужны сохранённые входы P069 и комплект участника.")
class LatencyBenchmarkCliTest(TestCase):
    def test_pairs_latency_and_accuracy_with_exact_sources(self):
        pdf = Path(os.environ["RAG_DEV_CORPUS"]) / "test/P069.pdf"
        text = Path(os.environ["RAG_P069_TEXT"])
        document = json.loads(text.read_text())
        direct = "091e446db458-p7-text-21"
        dataset = "091e446db458-p5-text-26"
        expected = {
            (2, 1): ("8B", "Latency", "0.81", "s"),
            (2, 2): ("8B", "Accuracy", "70.94", None),
            (3, 1): ("GRAD (8B+1B)", "Latency", "1.16", "s"),
            (3, 2): ("GRAD (8B+1B)", "Accuracy", "81.03", None),
            (4, 1): ("70B", "Latency", "5.75", "s"),
            (4, 2): ("70B", "Accuracy", "84.24", None),
        }

        with TemporaryDirectory() as directory:
            folder = Path(directory)
            without_reference = folder / "without-reference.json"
            fragments = [fragment for fragment in document["fragments"] if fragment["id"] != direct]
            self.assertEqual(len(fragments), len(document["fragments"]) - 1)
            without_reference.write_text(json.dumps({**document, "fragments": fragments}))
            without_definition = folder / "without-definition.json"
            without_definition.write_text(json.dumps({**document, "fragments": [
                fragment for fragment in document["fragments"] if fragment["id"] != dataset
            ]}))
            for source_text in (text, without_reference, without_definition):
                with self.subTest(source_text=source_text):
                    output = folder / f"run-{source_text.stem}"
                    completed = subprocess.run([
                        sys.executable, "-m", "rag_mvp", "extract", str(pdf), "--page", "7", "--table", "4",
                        "--grid-input", os.environ["RAG_P069_GRID"], "--article-text", str(source_text),
                        "--out", str(output),
                    ], capture_output=True, text=True, timeout=30)
                    self.assertEqual(completed.returncode, 0, completed.stderr)
                    artifact = json.loads((output / "run.json").read_text())
                    with (output / "results.csv").open() as file:
                        exported = list(csv.DictReader(file))
                    if source_text == without_reference:
                        self.assertEqual(exported, [])
                        self.assertTrue(any(remark["raw_text"] == "1.16" for remark in artifact["remarks"]))
                        continue

                    if source_text == without_definition:
                        self.assertEqual(exported, [])
                        remark, = [item for item in artifact["remarks"] if item["raw_text"] == "1.16"]
                        self.assertEqual(remark["reason"], "Не подтверждено, какой датасет обозначен HQA.")
                        self.assertEqual(remark["source"]["evidence_ids"], ["docling-3-1"])
                        self.assertEqual(remark["conditions"], {"method": "GRAD (8B+1B)", "metric": "Latency"})
                        self.assertEqual(remark["condition_sources"]["dataset"], ["docling-0-1"])
                        continue

                    self.assertEqual(len(exported), 6)
                    self.assertEqual(len(artifact["results"]), 6)
                    self.assertEqual(
                        {(row["source"]["row"], row["source"]["column"]): (
                            row["method"], row["metric"], row["raw_value"], row["unit"],
                        ) for row in artifact["results"]}, expected,
                    )
                    evidence = {fragment["id"]: fragment for fragment in artifact["evidence"]}
                    for row in artifact["results"]:
                        source = row["source"]
                        row_index, column = source["row"], source["column"]
                        value_id = f"docling-{row_index}-{column}"
                        self.assertEqual((source["page"], source["table"]), (7, 4))
                        self.assertEqual(source["evidence_ids"], [value_id])
                        self.assertEqual(source["box"], evidence[value_id]["box"])
                        self.assertEqual(evidence[value_id]["text"], row["raw_value"])
                        self.assertEqual(row["value"], row["raw_value"])
                        self.assertEqual((row["dataset"], row["task"], row["generator"]),
                                         ("HotpotQA", "multi-hop question answering", ""))
                        self.assertEqual(row["condition_sources"], {
                            "method": [f"docling-{row_index}-0"],
                            "dataset": ["docling-0-1", dataset],
                            "task": [dataset],
                            "metric": [f"docling-1-{column}"],
                            "experiment": ["caption", direct],
                        })
                        for refs in row["condition_sources"].values():
                            for ref in refs:
                                self.assertTrue(evidence[ref]["box"])
