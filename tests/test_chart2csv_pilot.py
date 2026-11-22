import csv
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


PILOT = (
    Path(__file__).resolve().parents[1]
    / "scripts/chart2csv_pilot.py"
)


class Chart2CsvPilotTests(unittest.TestCase):
    def test_compare_preserves_model_labels_and_reports_mismatches(self):
        with tempfile.TemporaryDirectory() as folder:
            run = Path(folder)
            gold = run / "dev_gold"
            gold.mkdir()
            inputs = {
                "configurations": [
                    ("publication_id", "config_id", "rag_variant"),
                    ("D001", "C79", "ThreadRAG representation: KG-Triplets"),
                    ("D001", "C80", "ThreadRAG representation: KG-Texts"),
                ],
                "datasets": [
                    ("publication_id", "dataset_id", "dataset_name"),
                    ("D001", "DS1", "HotpotQA"),
                    ("D001", "DS2", "2WikiMultihopQA"),
                ],
                "results": [
                    ("publication_id", "result_id", "source_figure", "metric_name",
                     "config_id", "dataset_id", "normalized_value"),
                    ("D001", "R354", "Figure 5", "EM", "C79", "DS1", "0.556"),
                    ("D001", "R355", "Figure 5", "EM", "C79", "DS2", "0.554"),
                    ("D001", "R357", "Figure 5", "EM", "C80", "DS1", "0.558"),
                    ("D001", "R358", "Figure 5", "EM", "C80", "DS2", "0.566"),
                ],
            }
            for name, rows in inputs.items():
                with (gold / f"{name}.csv").open("w", newline="") as stream:
                    csv.writer(stream).writerows(rows)

            source = {"page": 8, "bbox": [305, 209, 524, 403], "precision": "figure_region"}
            (run / "attempt.json").write_text(json.dumps({"source": source}))
            (run / "raw-output.txt").write_text(
                "```csv\nDataset,KG-Triplets,KG-Texts,Mystery\n"
                "HotpotQA,0.556,0.566,0.1\n2Wiki,0.500,,\n```\n"
            )
            subprocess.run(
                [sys.executable, str(PILOT), "compare", str(run), "--gold", str(gold)],
                check=True,
            )
            normalized = json.loads((run / "matched-values.json").read_text())
            score = json.loads((run / "comparison.json").read_text())
            self.assertEqual(normalized[0]["source"], source)
            wiki_record, = [record for record in normalized if record["raw_dataset"] == "2Wiki"]
            self.assertEqual(wiki_record["dataset"], "2WikiMultihopQA")
            self.assertEqual(score["counts"], {
                "expected": 4, "matched": 1, "missing": 1, "extra": 1,
                "value_errors": 1, "binding_errors": 1, "ambiguous": 0,
            })
            self.assertEqual(score["binding_errors"][0]["value_would_match"], ["R358"])


if __name__ == "__main__":
    unittest.main()
