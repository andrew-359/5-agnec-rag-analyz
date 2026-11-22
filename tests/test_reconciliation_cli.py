import json
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/reconciliation"
# D001 observations were reduced from a saved load-baseline run (71769951-c4fc-434d-8a43-a5c4ba2f0891).
# The synthetic second observation in the comparison test isolates the relationship rules.


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


class ReconciliationCliTest(unittest.TestCase):
    def invoke(self, output, *inputs):
        sha256 = read(FIXTURES / "d001-text.json")["sha256"]
        return subprocess.run(
            [sys.executable, "-m", "rag_mvp.reconciliation", "--run-id", "d001", "--pdf-sha256", sha256,
             "--out", str(output), *map(str, inputs)],
            cwd=ROOT, capture_output=True, text=True,
        )

    def test_d001_from_saved_artifacts_preserves_gain_sources_and_distinct_experiments(self):
        with TemporaryDirectory() as directory:
            output = Path(directory) / "reconciliation.json"
            completed = self.invoke(
                output, "--table", "main-table", FIXTURES / "d001-table-1.json",
                "--table", "ablation", FIXTURES / "d001-table-2.json",
                "--article-text", FIXTURES / "d001-text.json",
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            report = read(output)
        artifacts = {item["id"]: item["data"] for item in report["artifacts"]}
        prose = artifacts["prose-results"]
        gain, = [row for row in prose["results"] if row["raw_value"] == "14.4%"]
        self.assertEqual((gain["value"], gain["unit"]), ("14.4", "percentage_points"))
        evidence = {source["id"]: source for source in prose["evidence"]}
        paragraph, = gain["source"]["evidence_ids"]
        target, baseline = gain["condition_sources"]["gain_scale"]
        self.assertEqual(evidence[paragraph]["page"], 7)
        self.assertIn("IRCoT by a substantial 14.4% EM", evidence[paragraph]["text"])
        self.assertEqual([evidence[id_]["text"] for id_ in (target, baseline)], [".586", ".442"])
        values = [(item["id"], row["value"]) for item in report["artifacts"]
                  for row in item["data"]["results"] if row["raw_value"] == ".422"]
        self.assertEqual(values, [("main-table", "0.422"), ("ablation", "0.422")])
        self.assertEqual(report["relationships"], [])

    def test_same_measurement_keeps_both_observations_and_compares_normalized_values(self):
        table = read(FIXTURES / "d001-table-1.json")
        row = next(row for row in table["results"] if row["raw_value"] == ".422")
        for value, expected in (("42.2", "agreement"), ("42.3", "conflict"), (None, "open")):
            with self.subTest(value=value), TemporaryDirectory() as directory:
                output = Path(directory) / "reconciliation.json"
                prose = Path(directory) / "prose.json"
                paragraph = {"id": "paragraph", "page": 7, "text": f"EM {value}%", "precision": "text_fragment"}
                observation = {**row, "id": "prose-em", "raw_value": f"{value}%" if value else "unreadable",
                               "unit": "%", "value": value, "condition_sources": {},
                               "source": {"page": 7, "evidence_ids": [paragraph["id"]]}}
                prose.write_text(json.dumps({"source": table["source"], "evidence": [paragraph],
                                              "results": [observation], "remarks": []}), encoding="utf-8")
                completed = self.invoke(output, "--table", "main-table", FIXTURES / "d001-table-1.json",
                                        "--prose", "narrative", prose)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                report = read(output)
                relation, = report["relationships"]
                self.assertEqual(relation["status"], expected)
                self.assertEqual(relation["members"], [
                    {"artifact_id": "main-table", "result_id": row["id"]},
                    {"artifact_id": "narrative", "result_id": "prose-em"},
                ])
                if expected == "conflict":
                    self.assertTrue(all(member not in report["summary"]["confirmed"] for member in relation["members"]))
                    self.assertEqual(report["summary"]["disputed"], [relation])
                if expected == "agreement":
                    self.assertIn(relation["members"][0], report["summary"]["confirmed"])
                    self.assertNotIn(relation["members"][1], report["summary"]["confirmed"])
                actual = report["artifacts"][1]["data"]["results"][0]
                self.assertEqual(actual["raw_value"], observation["raw_value"])
                self.assertEqual(actual["source"]["evidence_ids"], ["paragraph"])

    def test_source_language_keeps_two_measurements_in_separate_scopes(self):
        artifact = read(FIXTURES / "d001-table-1.json")
        source = next(row for row in artifact["results"] if row["raw_value"] == ".422")
        with TemporaryDirectory() as directory:
            folder = Path(directory)
            first, second = folder / "first.json", folder / "second.json"
            output = folder / "reconciliation.json"
            for path, language in ((first, "am"), (second, "ar")):
                row = {**source, "source_language": language}
                path.write_text(json.dumps({**artifact, "results": [row], "remarks": []}))
            completed = self.invoke(output, "--table", "first", first, "--table", "second", second)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            report = read(output)
            self.assertEqual(report["relationships"], [])
            self.assertEqual(len(report["summary"]["confirmed"]), 2)

    def test_rejects_an_artifact_with_an_unaddressable_observation(self):
        table = read(FIXTURES / "d001-table-1.json")
        original = table["results"][0]
        original["source"]["evidence_ids"] = ["missing"]
        with TemporaryDirectory() as directory:
            output = Path(directory) / "reconciliation.json"
            path = Path(directory) / "unaddressable.json"
            path.write_text(json.dumps(table), encoding="utf-8")
            completed = self.invoke(output, "--table", "main-table", path)
            self.assertNotEqual(completed.returncode, 0, completed.stdout)
            self.assertIn("missing", completed.stderr)
            self.assertFalse(output.exists())

    def test_rejects_a_different_queue_run_on_the_same_pdf(self):
        with TemporaryDirectory() as directory:
            output = Path(directory) / "reconciliation.json"
            path = Path(directory) / "runs/other-run/table/attempts/one/run.json"
            path.parent.mkdir(parents=True)
            path.write_bytes((FIXTURES / "d001-table-1.json").read_bytes())
            completed = self.invoke(output, "--table", "main-table", path)
            self.assertNotEqual(completed.returncode, 0, completed.stdout)
            self.assertIn("другом прогоне", completed.stderr)
            self.assertFalse(output.exists())

    def test_rejects_a_non_scalar_parameter_before_grouping_measurements(self):
        table = read(FIXTURES / "d001-table-1.json")
        table["results"][0]["parameters"] = {"temperature": [0.2]}
        with TemporaryDirectory() as directory:
            output = Path(directory) / "reconciliation.json"
            path = Path(directory) / "incorrect.json"
            path.write_text(json.dumps(table), encoding="utf-8")
            completed = self.invoke(output, "--table", "main-table", path)
            self.assertNotEqual(completed.returncode, 0, completed.stdout)
            self.assertIn("parameters", completed.stderr)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
