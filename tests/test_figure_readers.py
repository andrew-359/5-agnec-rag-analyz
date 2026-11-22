import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest


class FigureReaderCatalogueTest(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("RAG_DEV_CORPUS"), "Нужен комплект участника в RAG_DEV_CORPUS.")
    def test_prepare_target_freezes_plot_and_panel_geometry_beyond_the_caption(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            pdf = Path(os.environ["RAG_DEV_CORPUS"]) / "dev/D001.pdf"
            inspected = subprocess.run(
                [sys.executable, "-m", "rag_mvp", "inspect", str(pdf), "--out", str(root / "map")],
                capture_output=True, text=True,
            )
            self.assertEqual(inspected.returncode, 0, inspected.stderr)
            inventory = json.loads((root / "map/inventory.json").read_bytes())
            candidate = next(item for item in inventory["candidates"] if item["kind"] == "figure" and item["label"] == "5")
            prepared = subprocess.run(
                [sys.executable, "-m", "rag_mvp.figure_readers", "--prepare-target", str(pdf),
                 "--inventory", str(root / "map/inventory.json"), "--candidate", candidate["id"]],
                capture_output=True, text=True,
            )
            self.assertEqual(prepared.returncode, 0, prepared.stderr)
            target = json.loads(prepared.stdout)

        self.assertEqual((target["stage"], target["candidate_id"], target["page"], target["figure"]),
                         ("figure", candidate["id"], 8, "5"))
        self.assertEqual(target["box"], candidate["box"])
        self.assertEqual(target["region"]["precision"], "figure")
        self.assertNotEqual(target["region"]["box"], target["box"])
        left, top, right, bottom = target["region"]["box"]
        self.assertTrue(left < 0.84 < right and top < 0.38 < bottom)
        panel, = target["region"]["panels"]
        self.assertEqual(panel["panel"], "1")
        x0, y0, x1, y1 = panel["panel_box_pt"]
        fx0, fy0, fx1, fy1 = panel["plot_box_pt"]
        self.assertTrue(x0 < fx0 < fx1 < x1 and y0 < fy0 < fy1 < y1)

    def test_catalogue_describes_the_registered_figure_readers_and_required_parameters(self):
        command = subprocess.run(
            [sys.executable, "-m", "rag_mvp.figure_readers"], capture_output=True, text=True,
        )
        self.assertEqual(command.returncode, 0, command.stderr)
        readers = {reader["id"]: reader for reader in json.loads(command.stdout)["readers"]}
        self.assertTrue({"vector-line", "vector-bar", "vector-scatter", "raster-line"} <= readers.keys())
        for reader in readers.values():
            self.assertEqual((reader["stage"], reader["input"]), ("figure", "figure-region"))
            self.assertTrue(reader["description"])
            self.assertFalse(reader["parameters_schema"]["additionalProperties"])
        scatter = readers["vector-scatter"]["parameters_schema"]
        self.assertIn("marker_size", scatter["required"])
        self.assertEqual(scatter["properties"]["marker_size"], {"type": "number", "exclusiveMinimum": 0})
        raster = readers["raster-line"]["parameters_schema"]
        self.assertEqual(raster["required"], ["raster_config"])
        self.assertEqual(raster["properties"]["raster_config"]["type"], "object")


if __name__ == "__main__":
    unittest.main()
