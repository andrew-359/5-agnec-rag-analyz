import csv
import json
import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, skipUnless


@skipUnless(os.environ.get("RAG_DEV_CORPUS"), "Нужен комплект участника в RAG_DEV_CORPUS.")
class LocalConditionCliTest(TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.corpus = Path(os.environ["RAG_DEV_CORPUS"])

    def cli(self, *args):
        process = subprocess.run(
            [sys.executable, "-m", "rag_mvp", *map(str, args)], capture_output=True, text=True,
        )
        self.assertEqual(process.returncode, 0, process.stderr)

    def load_run(self, case, name, page, table):
        pdf = self.corpus / name
        arguments = ("extract", pdf, "--page", page, "--table", table)
        if root := os.environ.get("RAG_LOCAL_CONDITION_INPUT"):
            inputs = Path(root)
        else:
            inputs = self.directory
            self.cli(*arguments, "--grid-only", "--out", inputs / f"{case}-grid")
            self.cli("read-article-text", pdf, "--out", inputs / f"{case}-text")
        grid = inputs / f"{case}-grid" / "table-grid.json"
        article = inputs / f"{case}-text" / "article-text.json"
        out = self.directory / "run"
        self.cli(*arguments, "--grid-input", grid, "--article-text", article, "--out", out)
        run = json.loads((out / "run.json").read_text(encoding="utf-8"))
        with (out / "results.csv").open(encoding="utf-8", newline="") as stream:
            exported = list(csv.DictReader(stream))
        self.assertEqual(run["source"]["sha256"], json.loads(grid.read_text(encoding="utf-8"))["sha256"])
        return run, exported

    def test_d005_links_all_instruction_variants_to_the_local_appendix(self):
        run, exported = self.load_run("d005", "dev/D005.pdf", 10, 4)
        metrics = ("Accuracy", "Hallucination", "Missing", "Factuality")
        values = (
            ("69.9%", "18.4%", "11.7%", "51.5%"),
            ("63.2%", "15.5%", "21.4%", "47.7%"),
            ("69.2%", "18.7%", "12.1%", "50.5%"),
            ("69.3%", "19.6%", "11.1%", "49.7%"),
        )
        expected = {
            (row, column): (f"Prompt-{chr(ord('A') + row - 1)}", metrics[column - 1], raw)
            for row, values_in_row in enumerate(values, 1)
            for column, raw in enumerate(values_in_row, 1)
        }
        evidence = {item["id"]: item for item in run["evidence"]}
        results = {(item["source"]["row"], item["source"]["column"]): item for item in run["results"]}
        self.assertEqual(set(results), set(expected))
        self.assertCountEqual([(item["method"], item["metric"], item["raw_value"]) for item in exported], expected.values())
        for position, record in expected.items():
            with self.subTest(position=position):
                result = results[position]
                self.assertEqual((result["method"], result["metric"], result["raw_value"]), record)
                self.assertEqual(result["generator"].casefold(), "llama-3.3-70b-instruct")
                self.assertEqual(result["dataset"], "holdout set from the Web Pages (C) dataset")
                self.assertIsNone(result["retrieval_top_k"])
                self.assertIsNone(result["statistic"])
                source = result["source"]
                self.assertEqual((source["page"], source["table"], source["headers"]), (10, 4, [record[1]]))
                self.assertEqual(source["cell_id"], f"cell-{position[0]}-{position[1]}")
                cell, = [evidence[ref] for ref in source["evidence_ids"]]
                self.assertEqual((cell["page"], cell["precision"], cell["text"], cell["box"]),
                                 (10, "cell", record[2], source["box"]))
                self.assertTrue(cell["box"])
                for field, expected_text in (
                    ("generator", "llama-3.3-70b-instruct"), ("dataset", "holdout set from the Web Pages (C) dataset"),
                ):
                    sources = [evidence[ref] for ref in result["condition_sources"][field]]
                    self.assertTrue(any(
                        src["page"] == 10 and src["precision"] == "text_fragment" and src["box"]
                        and expected_text in " ".join(src["text"].split()) for src in sources
                    ))
                    self.assertTrue(all(src["page"] == 10 for src in sources))

        self.assertTrue(any("протокола оценки" in item["reason"] for item in run["remarks"]))

    def test_p003_links_metric_first_headers_to_datasets_and_a_model_group(self):
        run, exported = self.load_run("p003", "test/P003.pdf", 6, 1)
        controls = json.loads(
            (Path(__file__).resolve().parents[1] / "docs/research/controls/P003-table1-manual.json").read_text(encoding="utf-8")
        )
        self.assertEqual(run["source"]["sha256"], controls["pdf_sha256"])
        evidence = {item["id"]: item for item in run["evidence"]}
        by_cell = {(item["source"]["row"], item["source"]["column"]): item for item in run["results"]}
        for expected in controls["controls"]:
            with self.subTest(control=expected["case_id"]):
                old = expected["current_remark"]["source"]
                position = old["row"], old["column"]
                self.assertIn(position, by_cell)
                item = by_cell[position]
                for field in ("method", "generator", "dataset", "metric", "raw_value"):
                    self.assertEqual(item[field], expected[field])
                self.assertEqual(item["task"], "question answering")
                self.assertIsNone(item["statistic"])
                self.assertIsNone(item["retrieval_top_k"])
                self.assertIsNone(item["protocol"])
                source = item["source"]
                self.assertEqual((source["page"], source["table"], source["headers"]),
                                 (expected["page"], expected["table"], old["headers"]))
                self.assertEqual(source["cell_id"], old["cell_id"])
                for actual, recorded in zip(source["box"], old["box"], strict=True):
                    self.assertAlmostEqual(actual, recorded, delta=0.004)
                cell, = [evidence[ref] for ref in source["evidence_ids"]]
                self.assertEqual((cell["page"], cell["precision"], cell["text"], cell["box"]),
                                 (6, "cell", expected["raw_value"], source["box"]))
                model, = [evidence[ref] for ref in item["condition_sources"]["generator"]]
                self.assertEqual((model["page"], model["precision"], model["text"]), (6, "cell", expected["generator"]))
                self.assertTrue(model["box"])
                dataset_sources = [evidence[ref] for ref in item["condition_sources"]["dataset"]]
                self.assertTrue(any(src["precision"] == "cell" and src["text"] == expected["dataset"] and src["box"]
                                    for src in dataset_sources))
                self.assertTrue(any(src["page"] == 5 and src["precision"] == "text_fragment" and src["box"]
                                    and f"({expected['dataset']})" in src["text"] for src in dataset_sources))
                metric, = [evidence[ref] for ref in item["condition_sources"]["metric"]]
                self.assertEqual((metric["page"], metric["precision"], metric["text"]), (6, "cell", old["headers"][0]))
                self.assertTrue(metric["box"])
                self.assertTrue(any(all(row[field] == item[field] for field in ("method", "generator", "dataset", "metric", "raw_value"))
                                    for row in exported))

        self.assertTrue(any(item["kind"] == "ambiguous-binding" and item["raw_text"] == "1.98%"
                            and item["source"]["row"] != 3 for item in run["remarks"]))
