import csv
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from rag_mvp.__main__ import write_csv
from tests.fixtures.table_context_binding.prepare_d009 import read_d009


class D009TableContextTest(TestCase):
    def test_saved_grid_and_assertions_produce_results_and_open_generator_with_sources(self):
        read, assertions = read_d009()
        collected = read.collect(assertions)
        run = read.serialize(collected, 5)
        evidence = {fragment["id"]: fragment for fragment in run["evidence"]}
        results = {result["method"]: result for result in run["results"]}
        expected = {
            "PRO-STEP": ("38.7", "Qwen2.5-7B-Instruct"),
            "Self-RAG": ("18.2", "Llama-2-13B"),
            "AutoRAG": ("14.8", "Llama-3-8B-Instruct"),
        }

        self.assertEqual(set(results), set(expected))
        for method, (value, generator) in expected.items():
            with self.subTest(method=method):
                result = results[method]
                self.assertEqual(
                    (result["dataset"], result["metric"], result["raw_value"], result["generator"], result["retrieval_top_k"]),
                    ("HotpotQA", "EM", value, generator, 3),
                )
                cell, = [evidence[ref] for ref in result["source"]["evidence_ids"]]
                self.assertEqual((cell["page"], cell["text"], cell["precision"]), (5, value, "cell"))
                quote, = [evidence[ref] for ref in result["condition_sources"]["generator"]]
                self.assertIn(generator, quote["text"])
                self.assertIn(method, quote["text"])
                self.assertEqual((quote["page"], quote["precision"]), (5, "text_fragment"))
                self.assertNotEqual(quote["box"], cell["box"])
                scope, = [evidence[ref] for ref in result["condition_sources"]["retrieval_top_k"]]
                self.assertIn("k = 3 across all methods", scope["text"])

        expected_open = {
            "Direct": "18.7",
            "Standard RAG": "31.6",
            "FLARE": "18.8",
            "IRCoT": "31.2",
            "Search-o1": "18.5",
            "Search-R1": "37.9",
            "ReasonRAG": "36.4",
            "StepSearch": "38.7",
        }
        open_generator = [remark for remark in run["remarks"] if remark.get("field") == "generator"]
        self.assertCountEqual([remark["conditions"]["method"] for remark in open_generator], expected_open)
        for remark in open_generator:
            method = remark["conditions"]["method"]
            with self.subTest(method=method):
                self.assertEqual(remark["raw_text"], expected_open[method])
                self.assertEqual(remark["conditions"]["retrieval_top_k"], 3)
                self.assertNotIn("generator", remark["conditions"])
                scope, = [evidence[ref] for ref in remark["condition_sources"]["retrieval_top_k"]]
                self.assertIn("k = 3 across all methods", scope["text"])
                cell, = [evidence[ref] for ref in remark["source"]["evidence_ids"]]
                self.assertEqual((cell["page"], cell["text"], cell["precision"]), (5, expected_open[method], "cell"))
                variant, = remark["options"]
                self.assertEqual(variant["value"], "Qwen2.5-7B-Instruct")
                quote, = [evidence[ref] for ref in variant["evidence_ids"]]
                self.assertIn("most baselines", quote["text"])
                self.assertEqual((quote["page"], quote["precision"]), (5, "text_fragment"))
                self.assertNotEqual(quote["box"], cell["box"])

        sources = [entry["source"]["cell_id"] for entry in [*results.values(), *open_generator]]
        self.assertEqual(len(set(sources)), 11)

        with TemporaryDirectory() as temp:
            csv_path = Path(temp) / "results.csv"
            write_csv(csv_path, collected.results)
            with csv_path.open(newline="", encoding="utf-8") as stream:
                rows = list(csv.DictReader(stream))
            self.assertCountEqual([row["method"] for row in rows], expected)
            self.assertTrue(all(row["retrieval_top_k"] == "3" for row in rows))
