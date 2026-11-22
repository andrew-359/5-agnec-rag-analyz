"""Real PDF → shared result → immutable submission package acceptance checks."""
import csv
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from rag_mvp.plot_publish import publish, reviewed_result


@unittest.skipUnless(os.environ.get("RAG_DEV_CORPUS"), "Нужен комплект в RAG_DEV_CORPUS.")
class PlotPublicationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.kit = Path(os.environ["RAG_DEV_CORPUS"])
        cls.pdf = cls.kit / "dev/D001.pdf"
        cls.template = cls.kit / "submission_template"
        cls.context_path = Path(".scratch/plot-publication/d001-figure3-context.json")
        cls.context = json.loads(cls.context_path.read_text())
        cls.temporary = TemporaryDirectory()
        cls.plot_path = Path(cls.temporary.name) / "plot.json"
        subprocess.run([
            sys.executable, "-m", "rag_mvp.plot_extract", str(cls.pdf), "--page", "8",
            "--figure", "3", "--panel", "a", "--series", "HotpotQA",
            "--strategy", "vector-line", "--x-axis", "anchors", "--y-axis", "EM",
            "--out", str(cls.plot_path),
        ], capture_output=True, text=True, check=True)
        cls.plot = json.loads(cls.plot_path.read_text())

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def test_result_export_and_source_stay_in_one_version(self):
        with TemporaryDirectory() as out:
            folder = publish(self.plot_path, self.context_path, self.pdf, self.template, out)
            run = json.loads((folder / "run.json").read_text())
            result = run["results"][0]
            with (folder / "results.csv").open(newline="") as stream:
                row = next(csv.DictReader(stream))
            self.assertEqual(result["value"], row["metric_value"])
            self.assertAlmostEqual(float(row["metric_value"]), .528, delta=.001)
            self.assertEqual(result["raw_value"], row["raw_value"])
            self.assertTrue(row["raw_value"].startswith("~"))
            self.assertEqual(result["parameter"], {"name": "Number of Anchors", "value": 1.0})
            self.assertIsNone(result["retrieval_top_k"])
            self.assertEqual(result["generator"], "")
            self.assertEqual(result["source"]["page"], 8)
            self.assertIsNone(result["source"]["table"])
            self.assertEqual(result["source"]["figure"], row["source_figure"])
            self.assertEqual(len(list(folder.glob("*.csv"))), 7)
            self.assertFalse(run["coverage"]["whole_publication"])
            check = json.loads((folder / "check-report.json").read_text())
            self.assertEqual(check["errors"], [])
            manifest = json.loads((folder / "manifest.json").read_text())
            self.assertEqual(run["version_id"], manifest["version_id"])
            self.assertEqual(run["pdf"]["sha256"], manifest["pdf_sha256"])
            self.assertEqual(run["evidence"]["plot-point"]["url"],
                             f'/published/{run["version_id"]}/source.pdf#page=8')
            for name, expected in manifest["files"].items():
                self.assertEqual(hashlib.sha256((folder / name).read_bytes()).hexdigest(), expected)
            html = (folder / "index.html").read_text()
            self.assertIn('href="#point-1"', html)
            self.assertIn('href="source.pdf#page=8"', html)
            self.assertIn('href="results.csv"', html)
            with self.assertRaisesRegex(ValueError, "already exists"):
                publish(self.plot_path, self.context_path, self.pdf, self.template, out)

    def test_unreviewed_or_untraceable_observations_are_refused(self):
        for updates in ({"reviewed": False}, {"sha256": "wrong"}, {"point_index": 999},
                        {"absolute_tolerance": 0}, {"unit": "unknown"}):
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                reviewed_result(self.plot, {**self.context, **updates}, self.pdf)
        evidence = {**self.context["field_evidence"], "method": [{"page": 1, "text": "fabricated-method"}]}
        with self.assertRaisesRegex(ValueError, "Unverified quote"):
            reviewed_result(self.plot, {**self.context, "field_evidence": evidence}, self.pdf)

    def test_checker_failure_leaves_no_published_or_partial_version(self):
        with TemporaryDirectory() as out, patch("rag_mvp.plot_publish.subprocess.run") as checked:
            checked.return_value = subprocess.CompletedProcess([], 1, "invalid linkage", "")
            with self.assertRaisesRegex(ValueError, "Submission check failed"):
                publish(self.plot_path, self.context_path, self.pdf, self.template, out)
            self.assertEqual(list(Path(out).iterdir()), [])
