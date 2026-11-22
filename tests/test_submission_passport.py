import json
import unittest
from io import StringIO
from pathlib import Path

from scripts.submission_passport import KEYS, SubmissionPassport, blocked_cells, filter_metric_roles


def artifact_for(entries, *, stage="table", precision="cell", caption=None):
    cells, evidence = [], [caption] if caption else []
    for row, col, raw in entries:
        ref = f"{row}-{col}"
        box = [.1 + .2 * col, .1 + .2 * row, .2 + .2 * col, .2 + .2 * row]
        evidence.append({"id": ref, "page": 3, "precision": precision, "text": raw, "box": box})
        cells.append({"row": row, "column": col, "row_span": 1, "column_span": 1,
                      "text": raw, "role": "column_header" if not row else "data", "evidence_ids": [ref]})
    return {"kind": "table", "id": stage, "data": {"page": 3, "table": 1,
            "grid": {"caption_evidence_id": caption["id"] if caption else None}, "cells": cells, "evidence": evidence}}


class SubmissionPassportTests(unittest.TestCase):
    def test_keeps_stock_cell_when_other_reader_renumbered_it(self):
        first = {"id": "a", "box": [.1, .1, .2, .2]}
        second = {"id": "b", "box": [.2, .1, .3, .2]}
        cells = {(3, 1): ({"text": ".422"}, first), (3, 2): ({"text": ".422"}, second)}
        stock = [{"row": 9, "column": 2, "box": first["box"], "evidence_ids": ["reread-a"]}]
        self.assertEqual(blocked_cells(cells, stock), {(3, 1)})

    def test_unlocatable_stock_sources_block_the_table(self):
        cells = {(3, 1): ({"text": ".422"}, {"id": "a", "box": [.1, .1, .2, .2]})}
        self.assertIsNone(blocked_cells(cells, [{"row": 3, "column": 1, "box": None, "evidence_ids": []}]))

    def test_recognized_names_still_need_a_measurement_role(self):
        passport = {"datasets": {"nq": {}}, "models": {}, "methods": {"rag": {}},
                    "metrics": {"nq": {"value": "Natural Questions"},
                                "rag": {"value": "Retrieval Augmented Generation"},
                                "f1": {"value": "F1"},
                                "ie": {"value": "Information Efficiency"}}}
        filter_metric_roles(passport, ("F1",), str.casefold, str.casefold)
        self.assertEqual(set(passport["metrics"]), {"f1", "ie"})

    def test_adds_an_unoccupied_equal_number_with_its_own_issue(self):
        caption = {"id": "caption", "page": 3, "precision": "text_fragment",
                   "text": "Table I: Accuracy achieved by each system.", "box": [.1, .05, .8, .08]}
        entries = [(0, 0, "Method"), (0, 1, "Accuracy"), (0, 2, "Accuracy"),
                   (1, 0, "Baseline"), (1, 1, ".422"), (1, 2, ".422")]
        artifact = artifact_for(entries, caption=caption)
        stock = {"table": [{"row": 1, "column": 1, "box": artifact["data"]["evidence"][-2]["box"]}]}
        tables = {name: [] for name in KEYS}
        baseline = {"result_id": "R1", "raw_value": ".422"}
        tables["results"].append(baseline.copy())
        counts = SubmissionPassport(None).add("P001", Path("unused.pdf"), "a" * 64, [],
                                               [artifact], stock, tables, StringIO())
        old, added = tables["results"]
        self.assertEqual(old, baseline)
        self.assertEqual(added["source_table"], "Table I")
        self.assertEqual(added["source_fragment"], ".422")
        self.assertIn("column 2", added["note"])
        self.assertEqual(tables["issues"][0]["record_ids"], added["result_id"])
        self.assertEqual(counts["stock_or_reading_conflict"], 1)

    def test_records_estimated_frame_and_uses_header_without_a_positioned_caption(self):
        caption = {"id": "caption", "page": 3, "precision": "text_fragment",
                   "text": "Table 1: Unknown frame.", "box": None}
        artifact = artifact_for([(0, 0, "Method"), (0, 1, "F1"), (1, 0, "Baseline"), (1, 1, ".422")],
                                precision="estimated_cell", caption=caption)
        tables = {name: [] for name in KEYS}
        audit = StringIO()
        counts = SubmissionPassport(None).add("P001", Path("unused.pdf"), "a" * 64, [],
                                               [artifact], {}, tables, audit)
        result, = tables["results"]
        self.assertEqual(result["source_table"], "Table 1")
        self.assertEqual(result["metric_name"], "F1")
        self.assertIn("Оценочная рамка OCR", result["note"])
        self.assertIn("подпись не подтверждена", result["note"])
        self.assertIn("Рамка OCR оценочная", tables["issues"][0]["description"])
        self.assertIn('"precision": "estimated_cell"', audit.getvalue())
        self.assertEqual(counts["added_estimated_cell"], 1)

    def test_captionless_counters_produce_no_measurements(self):
        artifact = artifact_for([(0, 0, "Dataset"), (0, 1, "Documents"), (1, 0, "A"), (1, 1, "42")])
        tables = {name: [] for name in KEYS}
        SubmissionPassport(None).add("P020", Path("unused.pdf"), "a" * 64, [],
                                     [artifact], {}, tables, StringIO())
        self.assertEqual(tables["results"], [])

    def test_saved_ocr_token_refines_box_only_on_unique_exact_match(self):
        entries = [(0, 0, "Method"), (0, 1, "F1"), (1, 0, "Baseline"), (1, 1, ".422")]
        token = {"id": "ocr-3-8", "page": 3, "precision": "text_fragment",
                 "text": ".422", "box": [.32, .33, .36, .35]}
        cases = (([token], True), ([{**token, "text": ".423"}], False),
                 ([token, {**token, "id": "ocr-3-9", "text": ".5"}], False),
                 ([{**token, "page": 4}], False))
        for tokens, refined in cases:
            with self.subTest(tokens=tokens):
                artifact = artifact_for(entries, precision="estimated_cell")
                cell_box = artifact["data"]["evidence"][-1]["box"]
                artifact["data"]["evidence"].extend(tokens)
                tables = {name: [] for name in KEYS}
                audit = StringIO()
                counts = SubmissionPassport(None).add("P001", Path("unused.pdf"), "a" * 64, [],
                                                       [artifact], {}, tables, audit)
                record = json.loads(audit.getvalue())
                self.assertEqual(record["witness"]["box"], cell_box)
                self.assertEqual(record["source_box"], token["box"] if refined else cell_box)
                self.assertEqual(counts.get("added_ocr_token", 0), int(refined))
                self.assertEqual(tables["results"][0]["raw_value"], ".422")

    def test_recovers_unique_adjacent_caption_without_colon(self):
        entries = [(0, 0, "Method"), (0, 1, "Overall"), (1, 0, "Baseline"), (1, 1, ".422")]
        caption = {"id": "nearby", "page": 3, "precision": "text_fragment",
                   "text": "Table 4 Accuracy (%)", "box": [.1, .06, .6, .09]}
        cases = (([caption], True), ([{**caption, "text": "Table 4 Shows Accuracy (%)"}], False),
                 ([{**caption, "text": "Table 4. Shows Accuracy (%)"}], False),
                 ([caption, {**caption, "id": "another", "text": "Table 5 Accuracy (%)"}], False),
                 ([{**caption, "box": [.1, .65, .6, .69]}], False))
        for fragments, recovered in cases:
            with self.subTest(fragments=fragments):
                artifact = artifact_for(entries)
                artifact["data"]["region"] = {"box": [.1, .1, .6, .4]}
                tables = {name: [] for name in KEYS}
                audit = StringIO()
                counts = SubmissionPassport(None).add("P001", Path("unused.pdf"), "a" * 64, fragments,
                                                       [artifact], {}, tables, audit)
                self.assertEqual(counts.get("added_recovered_caption", 0), int(recovered))
                if recovered:
                    result, = tables["results"]
                    self.assertEqual((result["source_table"], result["metric_name"]), ("Table 4", "Accuracy"))
                    metric = json.loads(audit.getvalue())["measurement"]["fields"]["metric"]
                    self.assertEqual(metric["sources"][0]["box"], caption["box"])
                else:
                    self.assertEqual(tables["results"], [])

    def test_composite_metric_header_works_without_caption(self):
        artifact = artifact_for([(0, 0, "Method"), (0, 1, "Mini-Corpus (str-em)"),
                                 (1, 0, "Baseline"), (1, 1, "41.6")])
        tables = {name: [] for name in KEYS}
        SubmissionPassport(None).add("P001", Path("unused.pdf"), "a" * 64, [],
                                     [artifact], {}, tables, StringIO())
        result, = tables["results"]
        self.assertEqual((result["metric_name"], result["raw_value"]), ("STR-EM", "41.6"))
        dataset, = tables["datasets"]
        self.assertEqual(dataset["dataset_name"], "Mini-Corpus")

    def test_spanned_dataset_and_explicit_model_axis_publish_config_and_raw_units(self):
        entries = [(0, 0, "Model"), (0, 1, "NQ"), (1, 1, "F1"), (1, 2, "EM"),
                   (2, 0, "Reader A"), (2, 1, "54.2%"), (2, 2, ".33")]
        artifact = artifact_for(entries)
        model_axis, dataset, f1, em, *_ = artifact["data"]["cells"]
        model_axis["row_span"] = 2
        dataset["column_span"] = 2
        for head in (f1, em):
            head["role"] = "column_header"
        tables = {name: [] for name in KEYS}
        passport = SubmissionPassport(None)
        passport.datasets = {"NQ": {"another-doc"}}
        audit = StringIO()
        passport.add("P001", Path("unused.pdf"), "a" * 64, [], [artifact], {}, tables, audit)
        self.assertEqual([dataset["dataset_name"] for dataset in tables["datasets"]], ["NQ"])
        self.assertEqual([config["rag_variant"] for config in tables["configurations"]], ["Reader A"])
        self.assertEqual([r["raw_unit"] for r in tables["results"]], ["%", ""])
        self.assertTrue(all(r["dataset_id"] and r["config_id"] for r in tables["results"]))
        condition_sources = json.loads(audit.getvalue().splitlines()[0])["condition_sources"]
        self.assertEqual([s["text"] for s in condition_sources["method"]], ["Reader A", "Model"])
        self.assertEqual([s["text"] for s in condition_sources["dataset"]], ["NQ"])

    def test_caption_dataset_needs_explicit_scope_and_no_other_dataset_in_grid(self):
        passport = SubmissionPassport(None)
        passport.datasets = {"NQ": {"another-doc"}, "HotpotQA": {"another-doc"}}
        for title, first_header, expected in (
            ("Table 1: Evaluation on NQ.", "Method", ["NQ"]),
            ("Table 1: Evaluation on NQ and HotpotQA.", "Method", []),
            ("Table 1: Evaluation on NQ.", "HotpotQA", []),
            ("Table 1: Results across multiple benchmarks. Trained on NQ.", "Method", []),
            ("Table 1: Comparison of models trained on NQ.", "Method", []),
        ):
            with self.subTest(title=title, first_header=first_header):
                caption = {"id": "caption", "page": 3, "precision": "text_fragment",
                           "text": title, "box": [.1, .05, .6, .08]}
                artifact = artifact_for([(0, 0, first_header), (0, 1, "F1"),
                                         (1, 0, "Baseline"), (1, 1, ".422")], caption=caption)
                tables = {name: [] for name in KEYS}
                passport.add("P001", Path("unused.pdf"), "a" * 64, [], [artifact], {}, tables, StringIO())
                self.assertEqual([dataset["dataset_name"] for dataset in tables["datasets"]], expected)

    def test_model_in_header_needs_a_generator_role(self):
        for label, expected in (("Generator", ["GPT-4o-mini"]), ("Judge", [])):
            with self.subTest(label=label):
                artifact = artifact_for([(0, 0, "Variant"), (0, 1, label), (1, 1, "GPT-4o-mini"),
                                         (2, 1, "F1"), (3, 0, "Base"), (3, 1, ".8")])
                for head in artifact["data"]["cells"][2:4]:
                    head["role"] = "column_header"
                tables = {name: [] for name in KEYS}
                SubmissionPassport(None).add("P001", Path("unused.pdf"), "a" * 64, [],
                                             [artifact], {}, tables, StringIO())
                self.assertEqual([r.get("generator_model") for r in tables["configurations"]
                                  if r.get("generator_model")], expected)

    def test_downward_method_axis_uses_only_the_spanning_row_label(self):
        artifact = artifact_for([(0, 0, "Method(↓)/Metric(→)"), (0, 1, "Latency"),
                                 (1, 0, "Reader A"), (1, 1, "500 ms"), (2, 1, "0.7 s")])
        artifact["data"]["cells"][2]["row_span"] = 2
        tables = {name: [] for name in KEYS}
        SubmissionPassport(None).add("P001", Path("unused.pdf"), "a" * 64, [], [artifact], {}, tables, StringIO())
        self.assertEqual([r["rag_variant"] for r in tables["configurations"]], ["Reader A"])
        self.assertEqual([r["raw_unit"] for r in tables["results"]], ["ms", "s"])
        self.assertEqual(len({r["config_id"] for r in tables["results"]}), 1)

    def test_caption_can_confirm_header_models_role(self):
        caption = {"id": "caption", "page": 3, "precision": "text_fragment",
                   "text": "Table 1: Evaluation with GPT-4o-mini as the generator.", "box": [.1, .05, .6, .08]}
        artifact = artifact_for([(0, 0, "Method"), (0, 1, "GPT-4o-mini"), (1, 1, "F1"),
                                 (2, 0, "Base"), (2, 1, ".8")], caption=caption)
        artifact["data"]["cells"][2]["role"] = "column_header"
        tables = {name: [] for name in KEYS}
        audit = StringIO()
        SubmissionPassport(None).add("P001", Path("unused.pdf"), "a" * 64, [], [artifact], {}, tables, audit)
        config, = tables["configurations"]
        self.assertEqual(config["generator_model"], "GPT-4o-mini")
        self.assertEqual([source["id"] for source in json.loads(audit.getvalue())["condition_sources"]["generator"]],
                         ["0-1", "caption"])

    def test_selects_largest_grid_and_protects_stock_from_discarded_grid(self):
        small = artifact_for([(0, 0, "Method"), (0, 1, "F1"), (1, 0, "A"), (1, 1, ".1")], stage="small")
        large = artifact_for([(0, 0, "Method"), (0, 1, "F1"), (1, 0, "A"), (1, 1, ".1"),
                              (2, 0, "B"), (2, 1, ".2")], stage="large")
        stock = {"small": [{"box": small["data"]["evidence"][-1]["box"]}]}
        tables = {name: [] for name in KEYS}
        counts = SubmissionPassport(None).add("P001", Path("unused.pdf"), "a" * 64, [],
                                               [small, large], stock, tables, StringIO())
        result, = tables["results"]
        self.assertEqual(result["raw_value"], ".2")
        self.assertEqual(counts["stock_or_reading_conflict"], 1)
