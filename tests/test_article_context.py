import json
import os
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path

from rag_mvp.article_context import read_article_fragments
from rag_mvp.inventory import inspect_pdf


FIXTURE = Path(__file__).parent / "fixtures/article_context/d009-implementation-details.json"


@unittest.skipUnless(os.environ.get("RAG_DEV_CORPUS"), "Нужен комплект участника в RAG_DEV_CORPUS.")
class ArticleFragmentTest(unittest.TestCase):
    def pdf(self, name):
        return Path(os.environ["RAG_DEV_CORPUS"]) / "dev" / name

    def test_d009_reads_both_parts_before_baselines(self):
        expected = json.loads(FIXTURE.read_text(encoding="utf-8"))
        document = read_article_fragments(self.pdf("D009.pdf"), [5])
        self.assertEqual(document.sha256, expected["sha256"])
        self.assertEqual(document.pages, (5,))
        fragments = document.fragments
        start = next(i for i, item in enumerate(fragments) if item.text.startswith("Implementation Details."))
        self.assertTrue(fragments[start + 2].text.startswith("Baselines."))
        for actual, reference in zip(fragments[start:start + 2], expected["fragments"], strict=True):
            self.assertIsNotNone(actual.box)
            values = asdict(actual)
            self.assertEqual(list(values.pop("box")), reference["box"])
            self.assertEqual(values, {key: val for key, val in reference.items() if key != "box"})
        self.assertEqual(document, read_article_fragments(self.pdf("D009.pdf"), [5, 5]))

    def test_d005_separates_offset_columns(self):
        document = read_article_fragments(self.pdf("D005.pdf"), [10])
        left = next(fragment for fragment in document.fragments if fragment.text.startswith("Table 4:"))
        right = next(fragment for fragment in document.fragments if fragment.text.startswith("Figure 5:"))
        self.assertIn("QA factuality metrics", left.text)
        self.assertIn("dynamic generation", right.text)
        self.assertNotIn("Figure 5:", left.text)
        self.assertLess(left.box[2], right.box[0])
        self.assertLess(document.fragments.index(left), document.fragments.index(right))

    def test_scan_has_no_text_fragments(self):
        document = read_article_fragments(self.pdf("D013.pdf"), [6])
        self.assertEqual(document.pages, (6,))
        self.assertEqual(document.fragments, ())

    def test_map_anchors_missing_tables_and_figure_in_two_columns(self):
        pdf = Path(os.environ["RAG_DEV_CORPUS"]) / "test/P002.pdf"
        with tempfile.TemporaryDirectory() as folder:
            result = inspect_pdf(pdf, Path(folder) / "map")
        tables = [item for item in result["candidates"] if item["kind"] == "table"]
        self.assertEqual([(item["page"], item["label"]) for item in tables],
                         [(6, "1"), (7, "2"), (8, "3"), (8, "4"), (8, "5")])
        self.assertTrue(all(item["box"] is not None for item in tables))
        figures = [(item["page"], item["label"]) for item in result["candidates"] if item["kind"] == "figure"]
        self.assertEqual(figures, [(2, "1"), (5, "2")])
