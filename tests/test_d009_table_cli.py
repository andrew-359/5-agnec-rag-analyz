import csv
import hashlib
import json
import os
import re
import subprocess
import sys
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, skipUnless

from rag_mvp.__main__ import extract
from rag_mvp.run import ArticleContext
from tests.fixtures.table_context_binding.prepare_d009 import FIXTURE, read_d009


def checked_article_context() -> ArticleContext:
    checked, assertions = read_d009()
    ids = {item.evidence_id for item in assertions}
    return ArticleContext(
        sha256=json.loads(FIXTURE.read_text(encoding="utf-8"))["sha256"],
        assertions=assertions,
        evidence=tuple(fragment for fragment in checked.evidence if fragment.id in ids),
    )


@skipUnless(os.environ.get("RAG_DEV_CORPUS"), "Нужен комплект участника в RAG_DEV_CORPUS.")
class D009TableCliTest(TestCase):
    def setUp(self):
        self.pdf = Path(os.environ["RAG_DEV_CORPUS"]) / "dev" / "D009.pdf"

    def test_docling_reads_methods_and_hotpotqa_em_from_pdf(self):
        from pypdfium2 import PdfDocument

        from rag_mvp.strategies.parser import read_table

        read = read_table(self.pdf, 5, 1)
        self.assertIsNotNone(read.table)
        expected, _ = read_d009()
        actual = [cell for cell in read.table.cells if cell.headers == ("HotpotQA", "EM")]
        self.assertEqual(
            [(cell.method, cell.text) for cell in actual],
            [(cell.method, cell.text) for cell in expected.table.cells],
        )
        self.assertEqual(len(actual), 11)
        self.assertEqual({cell.column for cell in actual}, {4})
        with PdfDocument(self.pdf) as pdf:
            page = pdf[4]
            width, height = page.get_size()
            with closing(page.get_textpage()) as text:
                for cell in actual:
                    self.assertIsNotNone(cell.box)
                    x0, y0, x1, y1 = cell.box
                    self.assertEqual(
                        text.get_text_bounded(x0 * width, (1 - y1) * height, x1 * width, (1 - y0) * height).strip(),
                        cell.text,
                    )

    def test_cli_binds_actual_cells_to_article_citations_from_the_same_pdf(self):
        with TemporaryDirectory() as directory:
            out = Path(directory) / "run"
            process = subprocess.run(
                [sys.executable, "-m", "rag_mvp", "extract", str(self.pdf), "--page", "5", "--table", "1",
                 "--article-context", "--out", str(out)],
                capture_output=True, text=True,
            )
            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertTrue((out / "docling.json").is_file())
            self.assertTrue((out / "page.png").is_file())
            run = json.loads((out / "run.json").read_text(encoding="utf-8"))
            context = json.loads((out / "article-context.json").read_text(encoding="utf-8"))
            with self.pdf.open("rb") as source:
                sha256 = hashlib.file_digest(source, "sha256").hexdigest()
            self.assertEqual(run["source"]["sha256"], sha256)
            self.assertEqual((context["sha256"], context["status"]), (sha256, "supported"))
            self.assertTrue(run["settings"]["article_context"])
            with (out / "results.csv").open(encoding="utf-8", newline="") as stream:
                exported = [row for row in csv.DictReader(stream) if (row["dataset"], row["metric"]) == ("HotpotQA", "EM")]

        self.assertEqual(len(exported), 3)
        self.assertEqual({(row["method"], row["value"], row["generator"]) for row in exported}, {
            ("Self-RAG", "18.2", "Llama-2-13B"),
            ("AutoRAG", "14.8", "Llama-3-8B-Instruct"),
            ("PRO-STEP", "38.7", "Qwen2.5-7B-Instruct"),
        })
        self.assertTrue(all(row["header_path"] == "HotpotQA → EM" for row in exported))
        self.assertEqual((run["grid"]["label"], run["grid"]["rows"], run["grid"]["columns"]), ("Table 1", 13, 14))
        evidence = {fragment["id"]: fragment for fragment in run["evidence"]}
        (baseline_source,) = [evidence[ref] for ref in run["context_evidence_ids"] if ref.startswith(sha256[:12])
                              and "baselines categorized into three groups" in " ".join(evidence[ref]["text"].split())]
        self.assertEqual((baseline_source["page"], baseline_source["precision"]), (5, "text_fragment"))
        results = {result["method"]: result for result in run["results"]
                   if (result["dataset"], result["metric"]) == ("HotpotQA", "EM")}
        open_generator = {item["conditions"]["method"]: item for item in run["remarks"]
                          if item["field"] == "generator" and item["source"]["headers"] == ["HotpotQA", "EM"]}
        self.assertEqual({method: item["raw_text"] for method, item in open_generator.items()}, {
            "Direct": "18.7", "Standard RAG": "31.6", "FLARE": "18.8", "IRCoT": "31.2",
            "Search-o1": "18.5", "Search-R1": "37.9", "ReasonRAG": "36.4", "StepSearch": "38.7",
        })
        cells = {}
        for method, item in (results | open_generator).items():
            with self.subTest(method=method):
                (ref,) = item["source"]["evidence_ids"]
                fragment = evidence[ref]
                self.assertEqual((fragment["page"], fragment["precision"]), (5, "cell"))
                self.assertEqual(fragment["text"], item["raw_value"] if method in results else item["raw_text"])
                self.assertIsNotNone(fragment["box"])
                self.assertEqual(item["source"]["column"], 4)
                cells[method] = fragment["box"]
                conditions = item if method in results else item["conditions"]
                self.assertEqual(conditions["retrieval_top_k"], 3)
                (top_k,) = item["condition_sources"]["retrieval_top_k"]
                self.assertIn("k = 3 across all methods", evidence[top_k]["text"])
                if method in results:
                    (quote,) = item["condition_sources"]["generator"]
                    text = re.sub(r"-\s*\n\s*", "-", evidence[quote]["text"])
                    self.assertIn(method, text)
                    self.assertIn(item["generator"], text)
                else:
                    (option,) = item["options"]
                    self.assertEqual(option["value"], "Qwen2.5-7B-Instruct")
                    quote, membership, row_group = option["evidence_ids"]
                    self.assertIn("most baselines", evidence[quote]["text"])
                    self.assertEqual(evidence[membership], baseline_source)
                    self.assertEqual(evidence[row_group]["precision"], "cell")
                    self.assertIn(evidence[row_group]["text"], {"Zero-shot", "Advanced", "RL-based"})
                self.assertEqual((evidence[quote]["page"], evidence[quote]["precision"]), (5, "text_fragment"))

        self.assertEqual(len({tuple(box) for box in cells.values()}), 11)
        self.assertNotEqual(cells["PRO-STEP"], cells["StepSearch"])

    def test_cli_rejects_article_citations_from_a_different_pdf(self):
        with TemporaryDirectory() as directory:
            out = Path(directory) / "run"
            context = replace(checked_article_context(), sha256="0" * 64)
            with self.assertRaisesRegex(ValueError, "SHA-256"):
                extract(self.pdf, 5, 1, out, article_context=context)
            self.assertFalse(out.exists())
