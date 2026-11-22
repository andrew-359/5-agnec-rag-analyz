import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from rag_mvp.inventory import inspect_pdf


@unittest.skipUnless(os.environ.get("RAG_DEV_CORPUS"), "Нужен комплект участника в RAG_DEV_CORPUS.")
class FigureLabelSearchTest(unittest.TestCase):
    def test_figure_2_is_found_by_its_label_inside_the_explicit_region(self):
        pdf = Path(os.environ["RAG_DEV_CORPUS"]) / "dev/D001.pdf"
        script = Path(__file__).resolve().parents[1] / "docs/research/scripts/search-figure-labels.py"
        with TemporaryDirectory() as directory:
            root = Path(directory)
            inspect_pdf(pdf, root / "map", discover_scans=False)
            inventory = json.loads((root / "map/inventory.json").read_text())
            command = [sys.executable, str(script), str(root / "map"), "--page", "3",
                       "--figure", "2", "--query", "Graph Pruning"]
            out = root / "comparison"
            execution = subprocess.run(
                [*command, "--box", "71", "57", "526", "286", "--out", str(out)],
                capture_output=True, text=True,
            )
            self.assertEqual(execution.returncode, 0, execution.stderr)
            baseline = json.loads((out / "caption.json").read_text())
            enriched = json.loads((out / "caption-and-labels.json").read_text())

            self.assertEqual(baseline["figure"], enriched["figure"])
            self.assertEqual(baseline["query"], enriched["query"])
            caption, = (item for item in inventory["candidates"]
                        if (item["kind"], item["page"], item["label"]) == ("figure", 3, "2"))
            self.assertEqual(enriched["figure"]["caption"], caption)
            self.assertEqual(enriched["source"]["sha256"], inventory["sha256"])
            self.assertTrue((out / enriched["source"]["pdf"]).is_file())
            self.assertEqual(baseline["matches"], [])
            label, = enriched["matches"]
            self.assertEqual((label["text"], label["page"], label["kind"]), ("Graph Pruning", 3, "figure-label"))
            # Independently measured with Poppler on PDF page 3, allowing font metrics to vary.
            for actual, expected in zip(label["box_pt"], (434.31, 184.60, 479.38, 192.98), strict=True):
                self.assertAlmostEqual(actual, expected, delta=1)
            self.assertTrue((out / enriched["preview"]).is_file())

            narrowed = root / "narrowed"
            execution = subprocess.run(
                [*command, "--box", "71", "57", "420", "286", "--out", str(narrowed)],
                capture_output=True, text=True,
            )
            self.assertEqual(execution.returncode, 0, execution.stderr)
            outside = json.loads((narrowed / "caption-and-labels.json").read_text())
            self.assertEqual(outside["matches"], [])
