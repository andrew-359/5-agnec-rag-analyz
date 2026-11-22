import csv
from importlib.util import find_spec
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from rag_mvp.strategies.plot_extract import scatter_points


class PlotCommandTest(unittest.TestCase):
    def test_help_lists_required_parameters_for_each_strategy(self):
        process = subprocess.run(
            [sys.executable, "-m", "rag_mvp.plot_extract", "--help"],
            capture_output=True, text=True, check=True,
        )
        self.assertRegex(process.stdout, r"(?m)^\s*vector-line\s+Reads vertices from a vector polyline")
        self.assertRegex(process.stdout, r"(?m)^\s*vector-scatter\s+Requires a positive --marker-size")
        self.assertRegex(process.stdout, r"(?m)^\s*raster-line\s+Requires --raster-config")

    def test_required_and_incompatible_strategy_options_are_reported_before_reading_pdf(self):
        raster_error = "--raster-config is required for raster-line and only applies to that strategy"
        marker_error = "vector-scatter requires a positive --marker-size"
        cases = (
            ("vector-scatter", [], marker_error),
            ("vector-scatter", ["--marker-size", "inf"], marker_error),
            ("raster-line", [], raster_error),
            ("vector-line", ["--raster-config", "scale.json"], raster_error),
            ("raster-line", ["--raster-config", "scale.json", "--panel-box", "0", "0", "1", "1"],
             "raster frame is set by --raster-config"),
        )
        for strategy, options, message in cases:
            with self.subTest(strategy=strategy, options=options):
                process = subprocess.run(
                    [sys.executable, "-m", "rag_mvp.plot_extract", "missing.pdf",
                     "--page", "8", "--figure", "3", "--panel", "a", "--series", "pilot",
                     "--x-axis", "x", "--y-axis", "y", "--strategy", strategy, "--out", "-", *options],
                    capture_output=True, text=True,
                )
                self.assertEqual(process.returncode, 2)
                self.assertEqual(process.stdout, "")
                self.assertTrue(process.stderr.rstrip().endswith(f"error: {message}"))


class ScatterIdentityTest(unittest.TestCase):
    def test_coincident_markers_remain_separate(self):
        marker = {"fill": True, "path": [("m",)] + [("c",)] * 8 + [("h",)],
                  "x0": 10, "top": 10, "x1": 12, "bottom": 12}
        points = scatter_points([marker, marker.copy()], (0, 0, 20, 20), 2)
        self.assertEqual(points, [(11, 11, (10, 10, 12, 12))] * 2)


@unittest.skipUnless(os.environ.get("RAG_DEV_CORPUS"), "Нужен комплект участника в RAG_DEV_CORPUS.")
class PlotPilotTest(unittest.TestCase):
    kit = Path(os.environ.get("RAG_DEV_CORPUS", "."))

    def run_plot(self, path, page, figure, panel, strategy, *options, expected_exit=0):
        with TemporaryDirectory() as folder:
            output = Path(folder) / "plot.json"
            process = subprocess.run(
                [sys.executable, "-m", "rag_mvp.plot_extract", str(self.kit / path),
                 "--page", str(page), "--figure", str(figure), "--panel", str(panel),
                 "--series", "pilot", "--x-axis", "x", "--y-axis", "y",
                 "--strategy", strategy, "--out", str(output), *options],
                capture_output=True, text=True,
            )
            self.assertEqual(process.returncode, expected_exit, process.stderr)
            return json.loads(output.read_text())

    def test_line_from_automatically_detected_panel(self):
        result = self.run_plot("dev/D001.pdf", 8, 3, "a", "vector-line")
        self.assertEqual(result["region"]["detected_panels"], 4)
        self.assertEqual(len(result["points"]), 7)
        self.assertEqual(result["axes"]["y"]["label_source"], "pdfium_text")
        point = result["points"][0]
        self.assertAlmostEqual(point["x"], 1)
        self.assertLessEqual(abs(point["y"] - 0.528), 0.001)
        self.assertEqual(point["source"]["page"], 8)
        self.assertTrue(point["source"]["url"].endswith("D001.pdf#page=8"))
        self.assertEqual(len(point["source"]["box"]), 4)
        self.assertTrue(point["approximate"])

    def test_labeled_bars_keep_the_exact_value_group_series_and_pdf_label(self):
        result = self.run_plot("dev/D001.pdf", 8, 5, "1", "vector-bar",
                               "--panel-box", "305", "209", "524", "403")
        expected = {
            "KG-Triplets": ("0.556", "0.554", "0.310"),
            "KG-Texts": ("0.558", "0.566", "0.316"),
            "KG-Paths": ("0.560", "0.564", "0.304"),
            "STG-Paths (Ours)": ("0.574", "0.588", "0.328"),
        }
        self.assertEqual(result["issues"], [])
        self.assertEqual(len(result["points"]), 12)
        self.assertEqual(
            {(point["series"], point["category"], point["raw_value"]) for point in result["points"]},
            {(series, category, value) for series, values in expected.items()
             for category, value in zip(("HotpotQA", "2Wiki", "MuSiQue"), values, strict=True)},
        )
        point = next(point for point in result["points"] if point["raw_value"] == "0.588")
        self.assertEqual(result["metric"]["text"], "Exact Match")
        self.assertFalse(point["approximate"])
        self.assertAlmostEqual(point["y"], 0.588)
        self.assertEqual(point["source"]["page"], 8)
        left, top, right, bottom = point["source"]["box_pt"]
        self.assertTrue(489 < left < 497 and 315 < top < 322 and 510 < right < 520 and 320 < bottom < 328)

    def test_vertical_bars_match_an_inset_legend_and_multiline_category(self):
        result = self.run_plot("dev/D006.pdf", 15, 5, "1", "vector-bar")
        self.assertEqual(result["issues"], [])
        self.assertEqual(len(result["points"]), 12)
        self.assertEqual(result["metric"]["text"], "Improvement (%)")
        controls = {(point["series"], point["category"]): point["raw_value"] for point in result["points"]}
        self.assertEqual(controls["InternLM7B", "RankGPT Reranker"], "24.5")
        self.assertEqual(controls["Llama8B", "RankT5"], "0.1")

    def test_cropped_legend_keeps_only_bars_with_an_identifiable_series(self):
        result = self.run_plot("dev/D001.pdf", 8, 5, "1", "vector-bar",
                               "--panel-box", "305", "223", "524", "403", expected_exit=2)
        self.assertEqual(len(result["points"]), 6)
        self.assertEqual({point["series"] for point in result["points"]}, {"KG-Texts", "STG-Paths (Ours)"})
        self.assertEqual(len(result["issues"]), 6)
        self.assertTrue(all(issue["source"]["box"] for issue in result["issues"]))

    def test_scatter_preserves_order_and_matches_both_gold_axes(self):
        result = self.run_plot("dev/D014.pdf", 16, 10, "1", "vector-scatter", "--marker-size", "1.494")
        self.assertEqual(result["region"]["detected_panels"], 6)
        with (self.kit / "dev_gold/results.csv").open(newline="", encoding="utf-8-sig") as stream:
            gold = {(int(match[1]), row["metric_name"]): float(row["metric_value"])
                    for row in csv.DictReader(stream) if row["publication_id"] == "D014"
                    and row["source_figure"] == "Figure 10"
                    and (match := re.search(r"panel1,entry(\d+)", row["note"]))}
        self.assertEqual(len(result["points"]), 231)
        self.assertEqual(len(gold), 462)
        for point in result["points"]:
            with self.subTest(index=point["index"]):
                for axis, metric in (("x", "Knowledge precision"), ("y", "Answer F1")):
                    self.assertLessEqual(abs(point[axis] - gold[point["index"], metric]), 0.0001)
                x, y = point["position_pt"]
                left, top, right, bottom = point["source"]["box_pt"]
                self.assertTrue(left < x < right and top < y < bottom)

    def test_corrected_panel_still_detects_frame(self):
        corrected = self.run_plot("dev/D001.pdf", 8, 3, "a", "vector-line",
                                  "--panel-box", "70", "70", "192", "176")
        self.assertEqual(corrected["region"]["selection"], "corrected")
        self.assertEqual(corrected["source"]["box_pt"], [70, 70, 192, 176])
        self.assertAlmostEqual(corrected["points"][0]["y"], 0.528)

    @unittest.skipUnless(find_spec("cv2"), "Нужен OpenCV из окружения RapidOCR.")
    def test_raster_values_and_unresolved_sample(self):
        config = Path(__file__).resolve().parents[1] / ".scratch/plot-extraction/d013-raster-config.json"
        result = self.run_plot("dev/D013.pdf", 9, 2, "a", "raster-line", "--raster-config", str(config),
                               expected_exit=2)
        self.assertEqual(len(result["points"]), 9)
        with (self.kit / "dev_gold/results.csv").open(newline="", encoding="utf-8-sig") as stream:
            gold = {int(row["metric_variant"].removeprefix("step")): float(row["metric_value"]) / 100
                    for row in csv.DictReader(stream)
                    if row["publication_id"] == "D013" and row["source_figure"] == "Figure 2"
                    and row["config_id"] == "C26" and row["metric_name"] == "EM"}
        control = next(point for point in result["points"] if point["x"] == 50)
        self.assertLessEqual(abs(control["y"] - gold[50]), 0.001)
        self.assertEqual(control["source"]["page"], 9)
        left, top, right, bottom = control["source"]["box"]
        self.assertTrue(0 < left < right < 1 and 0 < top < bottom < 1)
        issue, = result["issues"]
        self.assertEqual(issue["code"], "trace_not_unique")
        self.assertEqual(issue["sample_x"], 175)
        self.assertEqual(issue["source"]["page"], 9)
        issue_left, _, issue_right, _ = issue["source"]["box"]
        panel_left, _, panel_right, _ = result["source"]["box"]
        self.assertTrue(panel_left < issue_left < issue_right < panel_right)
        self.assertLess(issue_right - issue_left, 0.02)
        self.assertEqual(len(result["region"]["rectified_to_pdf_pt"]), 3)

    def test_scan_reports_review_with_page_source(self):
        result = self.run_plot("test/P013.pdf", 8, 5, "1", "vector-line", expected_exit=2)
        self.assertEqual(result["points"], [])
        self.assertIn("raster chart extraction", result["issues"][0]["message"])
        self.assertEqual(result["issues"][0]["source"]["page"], 8)
        self.assertEqual(result["issues"][0]["source"]["box"], [0, 0, 1, 1])


if __name__ == "__main__":
    unittest.main()
