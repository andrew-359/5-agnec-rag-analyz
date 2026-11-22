import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from rag_mvp.__main__ import save_article_text
from rag_mvp.inventory import inspect_pdf


@unittest.skipUnless(os.environ.get("RAG_DEV_CORPUS"), "Нужен комплект участника в RAG_DEV_CORPUS.")
class ArticleFigureTest(unittest.TestCase):
    def test_operator_region_reads_d001_without_a_map_panel_and_preserves_unbound_points(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            pdf = Path(os.environ["RAG_DEV_CORPUS"]) / "dev/D001.pdf"
            inventory = inspect_pdf(pdf, root / "map")
            save_article_text(pdf, root / "text")
            figure = next(item for item in inventory["candidates"] if item["kind"] == "figure" and item["label"] == "3")
            target = {
                "stage": "region", "page": 8, "box": [70 / 595.276, 70 / 841.89, 192 / 595.276, 176 / 841.89],
                "pdf_sha256": inventory["sha256"], "parent_run_id": "original-run", "candidate_id": figure["id"],
            }
            for label, box, have_text in (
                ("complete", target["box"], True),
                ("open", target["box"], False),
                ("wide", [0.11, 0.08, 0.52, 0.22], True),
            ):
                plan = {"target": {**target, "box": box}, "selection": {"mode": "auto"}}
                command = [
                    sys.executable, "-m", "rag_mvp", "read-article-figure", str(pdf),
                    "--inventory", str(root / "map/inventory.json"),
                    "--candidate", figure["id"], "--figure-plan", json.dumps(plan), "--out", str(root / label),
                ]
                if have_text:
                    command.extend(["--article-text", str(root / "text/article-text.json")])
                process = subprocess.run(command, capture_output=True, text=True)
                self.assertEqual(process.returncode, 0, process.stderr)
            complete, unresolved, wide = [
                json.loads((root / label / "extracted.json").read_bytes())
                for label in ("complete", "open", "wide")
            ]

        self.assertEqual(complete["target"], target)
        result = next(row for row in complete["results"] if row["parameters"] == {"anchors": 1})
        self.assertEqual((result["dataset"], result["metric"], result["source"]["page"]), ("HotpotQA", "EM", 8))
        self.assertAlmostEqual(float(result["value"]), 0.528, delta=0.001)
        self.assertFalse(unresolved["results"])
        self.assertTrue(any(row["kind"] == "unbound-figure-observation" and row["source"]["box"]
                            for row in unresolved["remarks"]))
        self.assertFalse(wide["results"])
        self.assertEqual(wide["remarks"][0]["kind"], "multiple-panels")
        self.assertEqual(wide["remarks"][0]["source"]["box"], [0.11, 0.08, 0.52, 0.22])

    def test_explicit_line_panel_keeps_the_same_confirmation_rules_as_auto(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            pdf = Path(os.environ["RAG_DEV_CORPUS"]) / "dev/D001.pdf"
            inventory = inspect_pdf(pdf, root / "map")
            save_article_text(pdf, root / "text")
            figure = next(item for item in inventory["candidates"] if item["kind"] == "figure" and item["label"] == "3")
            auto = subprocess.run([
                sys.executable, "-m", "rag_mvp", "read-article-figure", str(pdf),
                "--inventory", str(root / "map/inventory.json"), "--article-text", str(root / "text/article-text.json"),
                "--candidate", figure["id"], "--out", str(root / "auto"),
            ], capture_output=True, text=True)
            self.assertEqual(auto.returncode, 0, auto.stderr)
            context_path = root / "auto/article-context.json"
            context = json.loads(context_path.read_bytes())
            self.assertEqual(context["sha256"], inventory["sha256"])
            self.assertEqual(context["target"]["candidate"], figure["id"])
            self.assertEqual({item["kind"] for item in context["basis"]}, {"caption", "results_reference", "setup_section"})
            replay = subprocess.run([
                sys.executable, "-m", "rag_mvp", "read-article-figure", str(pdf),
                "--inventory", str(root / "map/inventory.json"), "--context-input", str(context_path),
                "--candidate", figure["id"], "--out", str(root / "replay"),
            ], capture_output=True, text=True)
            self.assertEqual(replay.returncode, 0, replay.stderr)
            self.assertEqual((root / "replay/article-context.json").read_bytes(), context_path.read_bytes())
            self.assertEqual((root / "replay/extracted.json").read_bytes(), (root / "auto/extracted.json").read_bytes())
            selected = self.explicit_command(root, pdf, figure, "vector-line", {"panel": "a"})
            self.assertEqual(selected.returncode, 0, selected.stderr)
            original = json.loads((root / "auto/extracted.json").read_bytes())
            explicit = json.loads((root / "figure/extracted.json").read_bytes())

        expected = [row for row in original["results"] if "anchors" in row.get("parameters", {})]
        self.assertEqual(len(expected), 7)
        self.assertEqual(explicit["results"], expected)
        self.assertEqual(len(explicit["observations"]), 7)
        self.assertEqual(explicit["attempted_strategy"], "vector-line")
        self.assertEqual(explicit["settings"]["panels"]["a"]["region"]["detected_panels"], 4)

    def prepared_target(self, root, pdf, figure):
        command = subprocess.run([
            sys.executable, "-m", "rag_mvp.figure_readers", "--prepare-target", str(pdf),
            "--inventory", str(root / "map/inventory.json"), "--candidate", figure["id"],
        ], capture_output=True, text=True)
        self.assertEqual(command.returncode, 0, command.stderr)
        return json.loads(command.stdout)

    def test_explicit_reader_uses_frozen_region_and_rejects_modified_geometry(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            pdf = Path(os.environ["RAG_DEV_CORPUS"]) / "dev/D001.pdf"
            inventory = inspect_pdf(pdf, root / "map")
            save_article_text(pdf, root / "text")
            figure = next(item for item in inventory["candidates"] if item["kind"] == "figure" and item["label"] == "5")
            target = self.prepared_target(root, pdf, figure)
            command = self.explicit_command(root, pdf, figure, "vector-bar", {}, target=target)
            self.assertEqual(command.returncode, 0, command.stderr)
            artifact = json.loads((root / "figure/extracted.json").read_bytes())
            actual = artifact["settings"]["panels"]["1"]["region"]
            self.assertEqual(actual["selection"], "manual")
            self.assertEqual(actual["plot_box_pt"], target["region"]["panels"][0]["plot_box_pt"])
            self.assertEqual(actual["detected_panels"], 1)
            self.assertEqual(len(artifact["results"]), 12)
            target["region"]["panels"][0]["plot_box_pt"][0] += 1
            tampered = self.explicit_command(root, pdf, figure, "vector-bar", {}, output="tampered", target=target)
            self.assertEqual(tampered.returncode, 3)
            self.assertFalse((root / "tampered").exists())

    def test_frozen_candidate_plan_with_auto_selection_preserves_legacy_output(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            pdf = Path(os.environ["RAG_DEV_CORPUS"]) / "dev/D001.pdf"
            inventory = inspect_pdf(pdf, root / "map")
            save_article_text(pdf, root / "text")
            figure = next(item for item in inventory["candidates"] if item["kind"] == "figure" and item["label"] == "5")
            command = [
                sys.executable, "-m", "rag_mvp", "read-article-figure", str(pdf),
                "--inventory", str(root / "map/inventory.json"),
                "--article-text", str(root / "text/article-text.json"), "--candidate", figure["id"],
            ]
            legacy = subprocess.run([*command, "--out", str(root / "legacy")], capture_output=True, text=True)
            self.assertEqual(legacy.returncode, 0, legacy.stderr)
            plan = {"target": self.prepared_target(root, pdf, figure), "selection": {"mode": "auto"}}
            planned = subprocess.run([*command, "--figure-plan", json.dumps(plan), "--out", str(root / "planned")],
                                     capture_output=True, text=True)
            self.assertEqual(planned.returncode, 0, planned.stderr)
            original = json.loads((root / "legacy/extracted.json").read_bytes())
            actual = json.loads((root / "planned/extracted.json").read_bytes())

        self.assertEqual(actual["requested_strategy"], "auto")
        for field in ("results", "remarks", "evidence", "observations", "binding"):
            self.assertEqual(actual[field], original[field], field)

    def explicit_command(self, root, pdf, figure, strategy, parameters, output="figure", target=None):
        plan = {
            "target": target or self.prepared_target(root, pdf, figure),
            "selection": {"mode": "explicit", "strategy_id": strategy, "parameters": parameters},
        }
        return subprocess.run([
            sys.executable, "-m", "rag_mvp", "read-article-figure", str(pdf),
            "--inventory", str(root / "map/inventory.json"),
            "--article-text", str(root / "text/article-text.json"),
            "--candidate", figure["id"], "--figure-plan", json.dumps(plan), "--out", str(root / output),
        ], capture_output=True, text=True)

    def test_explicit_line_reader_cannot_be_replaced_by_working_bar_reader(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            pdf = Path(os.environ["RAG_DEV_CORPUS"]) / "dev/D001.pdf"
            inventory = inspect_pdf(pdf, root / "map")
            save_article_text(pdf, root / "text")
            figure = next(item for item in inventory["candidates"] if item["kind"] == "figure" and item["label"] == "5")
            command = self.explicit_command(root, pdf, figure, "vector-line", {})
            self.assertEqual(command.returncode, 0, command.stderr)
            artifact = json.loads((root / "figure/extracted.json").read_bytes())

        self.assertEqual((artifact["requested_strategy"], artifact["attempted_strategy"], artifact["strategy"]),
                         ("vector-line", "vector-line", "vector-line"))
        self.assertEqual(artifact["status"], "not_applicable")
        self.assertTrue(artifact["reason"])
        self.assertEqual(artifact["observations"], [])
        self.assertEqual(artifact["binding"]["results"], [])

    def test_worker_cli_rejects_invalid_reader_parameters_and_changed_candidate_snapshot(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            pdf = Path(os.environ["RAG_DEV_CORPUS"]) / "dev/D001.pdf"
            inventory = inspect_pdf(pdf, root / "map")
            save_article_text(pdf, root / "text")
            figure = next(item for item in inventory["candidates"] if item["kind"] == "figure" and item["label"] == "5")
            cases = [
                ("unknown", {}, "Unknown figure reader"),
                ("vector-bar", {"typo": 1}, "Additional properties"),
                ("vector-scatter", {}, "marker_size"),
                ("vector-scatter", {"marker_size": "2"}, "not of type"),
                ("vector-scatter", {"marker_size": 0}, "minimum"),
                ("vector-scatter", {"marker_size": float("inf")}, "finite"),
                ("vector-scatter", {"marker_size": float("nan")}, "finite"),
                ("raster-line", {"raster_config": "/etc/passwd"}, "not of type"),
            ]
            for strategy, parameters, message in cases:
                with self.subTest(strategy=strategy, parameters=parameters):
                    command = self.explicit_command(root, pdf, figure, strategy, parameters)
                    self.assertNotEqual(command.returncode, 0)
                    self.assertIn(message, command.stderr)
                    self.assertFalse((root / "figure").exists())
            changed = {"stage": "figure", "candidate_id": figure["id"], "page": 7,
                       "figure": figure["label"], "box": figure["box"]}
            command = self.explicit_command(root, pdf, figure, "vector-bar", {}, target=changed)
            self.assertEqual(command.returncode, 3)
            self.assertIn("Адрес рисунка", command.stderr)
            self.assertFalse((root / "figure").exists())
            changed_map = json.loads((root / "map/inventory.json").read_bytes())
            frozen = self.prepared_target(root, pdf, figure)
            changed_figure = next(item for item in changed_map["candidates"] if item["id"] == figure["id"])
            changed_figure["box"][0] += 0.001
            (root / "map/inventory.json").write_text(json.dumps(changed_map), encoding="utf-8")
            command = self.explicit_command(root, pdf, figure, "vector-bar", {}, target=frozen)
            self.assertEqual(command.returncode, 3)
            self.assertIn("Адрес рисунка", command.stderr)
            self.assertFalse((root / "figure").exists())

    def test_bar_observations_remain_available_when_metric_does_not_allow_confirmation(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            pdf = Path(os.environ["RAG_DEV_CORPUS"]) / "dev/D006.pdf"
            inventory = inspect_pdf(pdf, root / "map")
            save_article_text(pdf, root / "text")
            figure = next(item for item in inventory["candidates"] if item["kind"] == "figure" and item["label"] == "5")
            command = self.explicit_command(root, pdf, figure, "vector-bar", {})
            self.assertEqual(command.returncode, 0, command.stderr)
            artifact = json.loads((root / "figure/extracted.json").read_bytes())

        self.assertEqual(artifact["status"], "succeeded")
        self.assertEqual(len(artifact["observations"]), 12)
        self.assertEqual(artifact["binding"]["results"], [])
        self.assertIn("Exact Match", artifact["binding"]["remarks"][0]["reason"])
        control = next(point for point in artifact["observations"]
                       if (point["series"], point["category"]) == ("InternLM7B", "RankGPT Reranker"))
        self.assertEqual(control["raw_value"], "24.5")
        self.assertEqual((control["source"]["page"], control["source"]["figure"]), (15, "5"))

    def test_explicit_vector_bar_keeps_twelve_exact_observations_separate_from_binding(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            pdf = Path(os.environ["RAG_DEV_CORPUS"]) / "dev/D001.pdf"
            inventory = inspect_pdf(pdf, root / "map")
            save_article_text(pdf, root / "text")
            figure = next(item for item in inventory["candidates"] if item["kind"] == "figure" and item["label"] == "5")
            plan = {
                "target": self.prepared_target(root, pdf, figure),
                "selection": {"mode": "explicit", "strategy_id": "vector-bar", "parameters": {}},
            }
            execution = subprocess.run([
                sys.executable, "-m", "rag_mvp", "read-article-figure", str(pdf),
                "--inventory", str(root / "map/inventory.json"),
                "--article-text", str(root / "text/article-text.json"),
                "--candidate", figure["id"], "--figure-plan", json.dumps(plan), "--out", str(root / "figure"),
            ], capture_output=True, text=True)
            self.assertEqual(execution.returncode, 0, execution.stderr)
            artifact = json.loads((root / "figure/extracted.json").read_bytes())

        self.assertEqual((artifact["requested_strategy"], artifact["attempted_strategy"], artifact["status"]),
                         ("vector-bar", "vector-bar", "succeeded"))
        expected = {
            "KG-Triplets": ("0.556", "0.554", "0.310"),
            "KG-Texts": ("0.558", "0.566", "0.316"),
            "KG-Paths": ("0.560", "0.564", "0.304"),
            "STG-Paths (Ours)": ("0.574", "0.588", "0.328"),
        }
        self.assertEqual(
            {(point["series"], point["category"], point["raw_value"]) for point in artifact["observations"]},
            {(series, category, value) for series, values in expected.items()
             for category, value in zip(("HotpotQA", "2Wiki", "MuSiQue"), values, strict=True)},
        )
        self.assertEqual(len(artifact["results"]), 12)
        self.assertEqual(artifact["binding"]["results"], artifact["results"])
        point = next(point for point in artifact["observations"] if point["raw_value"] == "0.588")
        result = next(result for result in artifact["results"] if result["value"] == "0.588")
        self.assertEqual((point["source"]["page"], point["source"]["figure"]), (8, "5"))
        self.assertEqual(point["source"]["box"], result["source"]["box"])
        self.assertFalse(point["approximate"])

    def test_article_stage_reads_grouped_bar_labels_with_sources_for_value_and_context(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            pdf = Path(os.environ["RAG_DEV_CORPUS"]) / "dev/D001.pdf"
            inventory = inspect_pdf(pdf, root / "map")
            save_article_text(pdf, root / "text")
            figure = next(item for item in inventory["candidates"] if item["kind"] == "figure" and item["label"] == "5")
            execution = subprocess.run([
                sys.executable, "-m", "rag_mvp", "read-article-figure", str(pdf),
                "--inventory", str(root / "map/inventory.json"),
                "--article-text", str(root / "text/article-text.json"),
                "--candidate", figure["id"], "--out", str(root / "figure"),
            ], capture_output=True, text=True)
            self.assertEqual(execution.returncode, 0, execution.stderr)
            artifact = json.loads((root / "figure/extracted.json").read_bytes())

        self.assertEqual(artifact["strategy"], "vector-bar")
        self.assertEqual(len(artifact["results"]), 12)
        self.assertEqual(artifact["remarks"], [])
        result = next(row for row in artifact["results"] if row["value"] == "0.588")
        self.assertEqual((result["method"], result["dataset"], result["metric"], result["unit"]),
                         ("STG-Paths (Ours)", "2Wiki", "EM", "fraction"))
        self.assertFalse(result["generator"])
        self.assertEqual(result["raw_value"], "0.588")
        source = result["source"]
        self.assertEqual((source["page"], source["figure"]), (8, "5"))
        evidence = {item["id"]: item for item in artifact["evidence"]}
        value = evidence[source["evidence_ids"][0]]
        self.assertEqual((value["text"], value["box"]), ("0.588", source["box"]))
        self.assertEqual(
            {field: [evidence[item]["text"] for item in refs]
             for field, refs in result["condition_sources"].items()},
            {"method": ["STG-Paths (Ours)"], "dataset": ["2Wiki"], "metric": ["Exact Match"]},
        )

    def test_digitizes_labeled_sensitivity_panel_from_article_map_and_shared_text(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            pdf = root / "article.pdf"
            shutil.copyfile(Path(os.environ["RAG_DEV_CORPUS"]) / "dev/D001.pdf", pdf)
            inventory = inspect_pdf(pdf, root / "map")
            save_article_text(pdf, root / "text")
            figure = next(item for item in inventory["candidates"] if item["kind"] == "figure" and item["label"] == "3")
            execution = subprocess.run([
                sys.executable, "-m", "rag_mvp.article_figures", str(pdf),
                "--inventory", str(root / "map/inventory.json"),
                "--article-text", str(root / "text/article-text.json"),
                "--candidate", figure["id"], "--out", str(root / "figure"),
            ], capture_output=True, text=True)
            self.assertEqual(execution.returncode, 0, execution.stderr)
            artifact = json.loads((root / "figure/extracted.json").read_bytes())

        self.assertEqual(artifact["source"]["sha256"], inventory["sha256"])
        result = next(row for row in artifact["results"] if row["parameters"] == {"anchors": 1})
        self.assertEqual((result["method"], result["generator"], result["dataset"], result["metric"]),
                         ("ThreadRAG", "GPT-4o-mini", "HotpotQA", "EM"))
        self.assertIn("Hyperparameter", result["experiment"])
        self.assertLessEqual(abs(float(result["value"]) - 0.528), 0.001)
        self.assertEqual(result["unit"], "fraction")
        self.assertEqual(result["digitization"], {"method": "vector-line", "resolution": 0.001, "series": "ThreadRAG"})
        source = result["source"]
        self.assertEqual((source["page"], source["figure"], source["fragment"]), (8, "3", "(a) Top-k Anchors"))
        self.assertNotIn("table", source)
        self.assertTrue(all(abs(actual - expected) < 0.001 for actual, expected in
                            zip(source["box"], (0.178807, 0.154002, 0.185527, 0.158753), strict=True)))
        evidence = {item["id"]: item for item in artifact["evidence"]}
        self.assertEqual(evidence[source["evidence_ids"][0]]["precision"], "plot_point")
        self.assertIn("indeterminate-axis", {row["kind"] for row in artifact["remarks"]})


if __name__ == "__main__":
    unittest.main()
