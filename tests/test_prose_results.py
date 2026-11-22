import json
import os
import subprocess
import sys
import unittest
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory

from rag_mvp.article_context import load_article_fragments, read_article_fragments
from rag_mvp.prose_results import build_prose_artifact


ROOT = Path(__file__).resolve().parents[1]
CORPUS = Path(os.environ.get("RAG_DEV_CORPUS", ROOT / "docs/context/RAG_ВИНИТИ_участникам_2026-11-20"))
PDF = CORPUS / "dev/D001.pdf"
TABLE_RUN = Path(os.environ.get("RAG_D001_TABLE_RUN", ROOT / "runs/snapshots/D001/run.json"))


@unittest.skipUnless(PDF.is_file(), "Нужен исходный D001 из комплекта участника.")
class ArticleProseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if path := os.environ.get("RAG_D001_ARTICLE_TEXT"):
            cls.fragments = load_article_fragments(Path(path))
        else:
            cls.fragments = read_article_fragments(PDF)
        cls.table = json.loads(TABLE_RUN.read_text(encoding="utf-8"))

    def test_d001_prose_gain_keeps_the_paragraph_and_comparable_table_cells(self):
        artifact = build_prose_artifact(self.fragments, [self.table])

        gain, = [row for row in artifact["results"] if row["raw_value"] == "14.4%"]
        self.assertEqual(("ThreadRAG", "IRCoT", "Qwen3.5 plus", "HotpotQA", "EM"), (
            gain["method"], gain["baseline_method"], gain["generator"], gain["dataset"], gain["metric"],
        ))
        self.assertEqual(("14.4", "percentage_points"), (gain["value"], gain["unit"]))
        self.assertTrue(gain["experiment"].startswith("Table 1: Main results"))
        evidence = {item["id"]: item for item in artifact["evidence"]}
        source = gain["source"]
        self.assertEqual((7, None, "4.2"), (source["page"], source["table"], source["section"]))
        paragraph = evidence[source["evidence_ids"][0]]
        self.assertEqual(("text_fragment", source["box"]), (paragraph["precision"], paragraph["box"]))
        self.assertIsNotNone(paragraph["box"])
        self.assertIn("IRCoT by a substantial 14.4% EM", paragraph["text"])
        self.assertEqual(
            [(6, ".586"), (6, ".442")],
            [(evidence[ref]["page"], evidence[ref]["text"]) for ref in gain["condition_sources"]["gain_scale"]],
        )

    def test_appendix_g_counts_from_saved_text_have_category_and_sample_citations(self):
        with TemporaryDirectory() as directory:
            saved = Path(directory) / "article-text.json"
            saved.write_text(json.dumps(asdict(self.fragments)), encoding="utf-8")
            output = Path(directory) / "prose.json"
            command = [sys.executable, "-m", "rag_mvp.prose_results", str(PDF),
                       "--article-text", str(saved), "--out", str(output)]
            baseline = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(baseline.returncode, 0, baseline.stderr)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["results"], [])
            pilot = subprocess.run([*command, "--failure-counts"], cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(pilot.returncode, 0, pilot.stderr)
            artifact = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(artifact["source"]["sha256"], self.fragments.sha256)
        self.assertEqual([(row["metric_variant"], row["value"]) for row in artifact["results"]], [
            ("Retrieval Failure", "149"), ("LLM Analysis Error", "42"), ("Pathfinding Error", "9"),
        ])
        evidence = {item["id"]: item for item in artifact["evidence"]}
        fragments = {fragment.id: fragment for fragment in self.fragments.fragments}
        sample_ids = set()
        for row in artifact["results"]:
            self.assertEqual((row["metric"], row["unit"], row["dataset"], row["sample_size"]),
                             ("failure count", "cases", "HotpotQA", 200))
            self.assertEqual((row["source"]["page"], row["source"]["section"]), (16, "G"))
            category_id, = row["source"]["evidence_ids"]
            sample_id, = row["condition_sources"]["sample_size"]
            self.assertNotEqual(category_id, sample_id)
            sample_ids.add(sample_id)
            category, sample = evidence[category_id], evidence[sample_id]
            self.assertIn(row["metric_variant"], category["text"])
            self.assertIn(f'{row["value"]} cases', category["text"])
            self.assertIn("HotpotQA dataset", sample["text"])
            self.assertIn("200 incorrect prediction cases", " ".join(sample["text"].split()))
            for quote in (category, sample):
                fragment = fragments[quote["fragment_id"]]
                start, end = quote["text_range"]
                self.assertEqual(quote["text"], fragment.text[start:end])
                self.assertEqual((quote["page"], quote["box"]), (16, list(fragment.box)))
        self.assertEqual(len(sample_ids), 1)

    def test_missing_or_ambiguous_table_binding_keeps_the_reading_with_its_paragraph(self):
        matching_baseline, = [
            row for row in self.table["results"]
            if (row["method"], row["generator"], row["dataset"], row["metric"])
            == ("IRCoT", "Qwen3.5 plus", "HotpotQA", "EM")
        ]
        for rows in (
            [row for row in self.table["results"] if row is not matching_baseline],
            [*self.table["results"], {**matching_baseline, "value": "0.443"}],
        ):
            with self.subTest(row_count=len(rows)):
                artifact = build_prose_artifact(self.fragments, [{**self.table, "results": rows}])

                self.assertFalse(any(row["raw_value"] == "14.4%" for row in artifact["results"]))
                remark, = [
                    item for item in artifact["remarks"]
                    if item["raw_text"] == "14.4%" and item["source"]["page"] == 7
                ]
                self.assertEqual("ambiguous-binding", remark["kind"])
                evidence = {item["id"]: item for item in artifact["evidence"]}
                source = evidence[remark["source"]["evidence_ids"][0]]
                self.assertIn("IRCoT by a substantial 14.4% EM", source["text"])
