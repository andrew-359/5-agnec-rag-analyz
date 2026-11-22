import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SNAPSHOTS = ROOT / "runs/snapshots"


def snapshot(article):
    return json.loads((SNAPSHOTS / article / "run.json").read_text(encoding="utf-8"))


def cell(page, table, row, column, headers):
    return {
        "page": page, "table": table, "row": row, "column": column,
        "headers": headers, "cell_id": f"cell-{row}-{column}",
        "box": [0.2, 0.3, 0.25, 0.35],
    }


class SavedOutputChecksTest(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        main = snapshot("D001")
        self.main_result = next(row for row in main["results"] if row["id"] == "result-234")
        self.main_result["retrieval_top_k"] = 5
        ablation = {
            "document": "D001.pdf", "page": 7, "table": 2,
            "source": {**main["source"], "page": 7},
            "results": [{
                "method": "ThreadRAG", "dataset": "HotpotQA", "metric": "EM",
                "value": "0.422", "unit": "fraction",
                "experiment": "Table 2: Ablation study",
                "source": cell(7, 2, 2, 3, ["Datasets", "HotpotQA"]),
                "components": {"SN": True, "STG": True, "EDP": True},
            }],
            "remarks": [],
        }
        search_result = {
            "method": "Search-R1", "dataset": "HotpotQA", "metric": "EM",
            "value": "37.9", "unit": "%", "retrieval_top_k": 3,
            "generator": "Qwen2.5-7B-Instruct",
            "experiment": "Table 1: Comparison of different RAG methods",
            "source": cell(5, 1, 9, 4, ["HotpotQA", "EM"]),
            "condition_sources": {"generator": ["search-r1-model"]},
        }
        search = {
            "document": "D009.pdf", "page": 5, "table": 1,
            "source": {
                "name": "D009.pdf", "page": 5,
                "sha256": "8fd9ce01afa9c483a4e7b60f4d69ff97e61df96e1611fd7bddb5a80c94714145",
            },
            "results": [search_result],
            "remarks": [],
            "evidence": [{
                "id": "search-r1-model", "page": 5, "precision": "text_fragment",
                "text": "Search-R1 uses Qwen2.5-7B-Instruct.",
            }],
        }
        plot = {
            "pdf": "D013.pdf", "sha256": snapshot("D013")["source"]["sha256"],
            "settings": {"figure": "2", "panel": "a", "series": "PPO", "page": 9,
                         "y_axis": "EM", "x_axis": "step"},
            "source": {"page": 9, "box": [0.1, 0.1, 0.6, 0.6]},
            "points": [
                {"x": step, "y": value, "source": {"page": 9, "box": [0.2, 0.3, 0.21, 0.31]}}
                for step, value in [(50, 0.19986), (175, 0.318)]
            ],
            "issues": [],
        }
        self.artifacts = {"d009": search, "d001-table1": main, "d001-table2": ablation, "d013": plot}

    def run_checker(self):
        command = [sys.executable, str(ROOT / "runs/check_saved_outputs.py")]
        for name, artifact in self.artifacts.items():
            path = self.directory / f"{name}.json"
            if artifact is not None:
                path.write_text(artifact if isinstance(artifact, str) else json.dumps(artifact), encoding="utf-8")
            command.extend((f"--{name}", str(path)))
        return subprocess.run(command, text=True, capture_output=True, check=False)

    def test_matching_controls_and_generator_citation_for_review(self):
        result = self.run_checker()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.count("СОВПАДЕНИЕ"), 4, result.stdout)
        self.assertIn("проверить, подтверждает ли цитата", result.stdout)

    def test_equal_number_with_wrong_source_is_a_binding_error(self):
        self.main_result["source"] = self.artifacts["d001-table2"]["results"][0]["source"]
        result = self.run_checker()
        self.assertEqual(result.returncode, 1)
        self.assertIn("ОШИБКА ПРИВЯЗКИ", result.stdout)

    def test_ambiguous_generator_is_reported_as_open(self):
        search = self.artifacts["d009"]
        row = search["results"].pop()
        search["remarks"] = [{
            "source": row["source"], "raw_text": "37.9", "field": "generator",
            "conditions": {"method": "Search-R1", "dataset": "HotpotQA", "metric": "EM", "retrieval_top_k": 3},
            "options": [{"value": "Qwen2.5-7B-Instruct", "evidence_ids": ["search-r1-model"]}],
        }]
        search["evidence"][0]["text"] = "Qwen2.5-7B-Instruct is the primary backbone for PRO-STEP and most baselines."
        result = self.run_checker()
        self.assertEqual(result.returncode, 0)
        self.assertIn("ОТКРЫТОЕ УСЛОВИЕ", result.stdout)
        self.assertNotIn("ОШИБКА ПРИВЯЗКИ", result.stdout)

    def test_generic_quote_cannot_confirm_generator(self):
        self.artifacts["d009"]["evidence"][0]["text"] = "Qwen2.5-7B-Instruct is used for most baselines."
        result = self.run_checker()
        self.assertEqual(result.returncode, 1)
        self.assertIn("ОШИБКА ПРИВЯЗКИ", result.stdout)
        self.assertNotIn("ОТКРЫТОЕ УСЛОВИЕ", result.stdout)

    def test_missing_artifact_and_unread_point_are_reported_separately(self):
        self.artifacts["d001-table2"] = None
        plot = self.artifacts["d013"]
        plot["points"].pop()
        plot["issues"] = [{"message": "x=175: expected one dark trace, found 2", "source": plot["source"]}]
        result = self.run_checker()
        self.assertEqual(result.returncode, 1)
        self.assertIn("АРТЕФАКТ НЕ НАЙДЕН", result.stdout)
        self.assertIn("ПРОПУСК", result.stdout)
        self.assertIn("expected one dark trace", result.stdout)

    def test_wrong_series_and_broken_json_are_reported_separately(self):
        self.artifacts["d013"]["settings"]["series"] = "OAS"
        self.artifacts["d009"] = "{"
        result = self.run_checker()
        self.assertEqual(result.returncode, 1)
        self.assertIn("ОШИБКА ПРИВЯЗКИ", result.stdout)
        self.assertIn("ОШИБКА АРТЕФАКТА", result.stdout)

    def test_value_outside_plot_tolerance_is_a_reading_error(self):
        self.artifacts["d013"]["points"][0]["y"] = 0.19899
        result = self.run_checker()
        self.assertEqual(result.returncode, 1)
        self.assertIn("ОШИБКА ЧТЕНИЯ", result.stdout)
        self.assertNotIn("ОШИБКА ПРИВЯЗКИ", result.stdout)
