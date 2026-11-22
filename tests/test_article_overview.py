import json
import os
from pathlib import Path
import subprocess
import sys
import unittest


@unittest.skipUnless(os.environ.get("RAG_DEV_CORPUS"), "Нужен комплект участника в RAG_DEV_CORPUS.")
class ArticleOverviewTest(unittest.TestCase):
    def overview(self, article):
        pdf = Path(os.environ["RAG_DEV_CORPUS"]) / "dev" / f"{article}.pdf"
        result = subprocess.run(
            [sys.executable, "-m", "rag_mvp.article_overview", str(pdf)],
            capture_output=True, text=True, check=True,
        )
        return json.loads(result.stdout)

    def test_d008_reads_authors_text_without_adjacent_figure(self):
        abstract = self.overview("D008")["abstract"]
        self.assertEqual(abstract["page"], 1)
        self.assertIn("hallucinated and factual tokens diverge", abstract["text"])
        self.assertIn("this reflects an imbalance", abstract["text"])
        self.assertNotIn("Usain", abstract["text"])

    def test_d001_keeps_title_and_viewer_addresses(self):
        overview = self.overview("D001")
        self.assertIn("Graph Topology Meets Energy-Driven Pathfinding", overview["title"]["text"])
        self.assertEqual(overview["abstract"]["page"], 1)
        self.assertEqual(overview["conclusion"]["page"], 9)
        self.assertIsNotNone(overview["abstract"]["box"])
        self.assertIsNotNone(overview["conclusion"]["box"])

    def test_scan_reports_missing_text(self):
        overview = self.overview("D013")
        self.assertFalse(overview["has_text"])
        self.assertIsNone(overview["abstract"])
        self.assertIsNone(overview["conclusion"])
