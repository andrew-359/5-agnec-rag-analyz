"""Use the normal CLI to replay original benchmark grids and article text."""

import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from unittest import TestCase, skipUnless


@skipUnless(os.getenv("RAG_TEST_AUTO_BATCH") and os.getenv("RAG_DEV_CORPUS"),
            "Нужны сохранённые сетки test и комплект участника.")
class BenchmarkTableCliTest(TestCase):
    def replay(self, article, page, table, grid, article_text=None, change_grid=None, *, series=None):
        folder = Path(series or os.environ["RAG_TEST_AUTO_BATCH"]) / article / "01"
        pdf = Path(os.environ["RAG_DEV_CORPUS"]) / "test" / f"{article}.pdf"
        with TemporaryDirectory() as tmp:
            output = Path(tmp) / "result"
            source_text = folder / "article-text.json"
            if article_text is not None:
                changed = Path(tmp) / "article-text.json"
                changed.write_text(json.dumps(article_text(json.loads(source_text.read_text()))))
                source_text = changed
            grid_input = next(folder.glob(f"grid-{grid}*"))
            if change_grid:
                changed = Path(tmp) / "grid.json"
                changed.write_text(json.dumps(change_grid(json.loads(grid_input.read_text()))))
                grid_input = changed
            completed = subprocess.run([
                sys.executable, "-m", "rag_mvp", "extract", str(pdf), "--page", str(page),
                "--table", str(table), "--grid-input", str(grid_input),
                "--article-text", str(source_text), "--out", str(output),
            ], capture_output=True, text=True, timeout=45)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            return json.loads((output / "run.json").read_text())

    def record(self, run, method, dataset, metric, value):
        records = [row for row in run["results"] if (
            row["method"], row["dataset"], row["metric"], row["raw_value"],
        ) == (method, dataset, metric, value)]
        self.assertEqual(len(records), 1, (method, dataset, metric, value, len(run["results"])))
        row, = records
        evidence = {item["id"]: item for item in run["evidence"]}
        number, = row["source"]["evidence_ids"]
        self.assertEqual(evidence[number]["box"], row["source"]["box"])
        self.assertEqual(evidence[number]["text"], value)
        for field in ("dataset", "metric", "method", "task", "experiment"):
            for ref in row["condition_sources"][field]:
                self.assertTrue(evidence[ref]["box"], ref)
        return row

    def test_retrieval_metrics_and_caption_dataset(self):
        p002 = self.replay("P002", 8, 3, "17d79")
        p074 = self.replay("P074", 8, 5, "8766")
        self.assertEqual((len(p002["results"]), len(p074["results"])), (24, 9))
        self.assertEqual(sum(row["metric"] == "Hit@1" for row in p002["results"]), 12)
        self.assertEqual(sum(row["metric"] == "Hit@3" for row in p002["results"]), 12)
        self.assertEqual(len({row["source"]["cell_id"] for row in p002["results"]}), 24)
        for row in (
            self.record(p002, "BM25", "PubMedQA", "Hit@1", "0.835"),
            self.record(p074, "Random Select", "InfoSeek", "R@1", "53.5"),
        ):
            self.assertEqual(row["task"], "information retrieval")
            self.assertEqual(row["generator"], "")
            self.assertIsNone(row["retrieval_top_k"])
            self.assertEqual(row["metric_k"], 1)
            self.assertEqual(row["condition_sources"]["metric_k"], row["condition_sources"]["metric"])

    def test_explicit_dataset_in_three_real_captions(self):
        corpus_series = os.getenv("RAG_TEST_CORPUS_RESULTS")
        if not corpus_series:
            self.skipTest("Нужна сохранённая сетка P001.")
        cases = (
            ("P001", 7, 1, "8f43cf78", "0.702", "ORD-QA", "recall@15", corpus_series),
            ("P008", 3, 2, "7765d08e", "0.4961", "STARK-MAG", "Hit@5", None),
            ("P074", 7, 4, "574a016b", "57.2", "InfoSeek", "R@1", None),
        )
        for doc, page, table, grid, value, dataset, metric, series in cases:
            with self.subTest(doc=doc):
                run = self.replay(doc, page, table, grid, series=series)
                if doc == "P001":
                    self.assertFalse(run["results"])
                    target, = [item for item in run["remarks"] if item["raw_text"] == value]
                    self.assertEqual(target["field"], "method")
                    self.assertEqual(target["conditions"]["Model type"], "Our embedding model")
                    self.assertEqual((target["conditions"]["dataset"], target["conditions"]["metric"]),
                                     (dataset, metric))
                else:
                    target, = [item for item in run["results"] if item["raw_value"] == value]
                    self.assertEqual((target["dataset"], target["metric"]), (dataset, metric))
                evidence = {item["id"]: item for item in run["evidence"]}
                caption, definition = map(evidence.__getitem__, target["condition_sources"]["dataset"])
                self.assertEqual(caption["page"], page)
                self.assertIn(dataset.casefold(), caption["text"].casefold())
                self.assertIn(dataset, definition["text"])

                def remove_caption(artifact):
                    read = artifact["read"]
                    caption_id = read["table"]["caption_evidence_id"]
                    read["table"]["caption_evidence_id"] = None
                    read["grid"]["caption_evidence_id"] = None
                    read["grid"]["evidence_ids"] = [ref for ref in read["grid"]["evidence_ids"] if ref != caption_id]
                    read["evidence"] = [item for item in read["evidence"] if item["id"] != caption_id]
                    return artifact

                without = self.replay(doc, page, table, grid, series=series, change_grid=remove_caption)
                self.assertFalse(any(item["raw_value"] == value for item in without["results"]))
                self.assertTrue(any(item["raw_text"] == value and item["field"] == "dataset"
                                    for item in without["remarks"]))

    def test_same_dataset_is_not_a_named_caption_scope(self):
        def change(artifact):
            read = artifact["read"]
            caption = read["table"]["caption"].replace("in STARK-MAG", "on the same dataset")
            read["table"]["caption"] = caption
            next(item for item in read["evidence"] if item["id"] == "caption")["text"] = caption
            return artifact

        run = self.replay("P008", 3, 2, "7765d08e", change_grid=change)
        self.assertFalse(run["results"])
        self.assertTrue(any(item["field"] == "dataset" and item["raw_text"] == "0.4961"
                            for item in run["remarks"]))

    def test_multiple_caption_datasets_require_the_cell_group(self):
        def grouped_grid(artifact):
            read = artifact["read"]
            caption = read["table"]["caption"].replace("in STARK-MAG", "in STARK-MAG and STARK-PRIME")
            read["table"]["caption"] = caption
            next(item for item in read["evidence"] if item["id"] == "caption")["text"] = caption
            # Keep the recorded measurements and add one verified group above Hit@1 only.
            group_id = "test-dataset-header"
            label = "STARK-MAG"
            grid = read["grid"]
            grid["rows"] += 1
            for cell in grid["cells"]:
                cell["row"] += 1
            grid["cells"].append({"row": 0, "column": 1, "text": label, "role": "column_header",
                                  "evidence_ids": [group_id]})
            read["evidence"].append({"id": group_id, "page": artifact["page"], "text": label,
                                     "box": [0.74, 0.12, 0.77, 0.13], "precision": "cell"})
            for cell in read["table"]["cells"]:
                cell["row"] += 1
                if cell["column"] == 1:
                    cell["headers"].insert(0, label)
                    cell["header_evidence_ids"].insert(0, group_id)
            return artifact

        run = self.replay("P008", 3, 2, "7765d08e", change_grid=grouped_grid)
        result = self.record(run, "Text Retriever: VSS", "STARK-MAG", "Hit@1", "0.2908")
        self.assertIn("test-dataset-header", result["condition_sources"]["dataset"])
        self.assertTrue(any(item["raw_text"] == "0.4961" and item["field"] == "dataset"
                            for item in run["remarks"]))

    def test_p002_without_dataset_keeps_number_and_known_fields(self):
        original = self.replay("P002", 8, 3, "17d79")
        target = self.record(original, "BM25", "PubMedQA", "Hit@1", "0.835")
        dataset_refs = set(target["condition_sources"]["dataset"]) - {"caption"}

        def remove_dataset_description(article):
            article["fragments"] = [
                fragment for fragment in article["fragments"] if fragment["id"] not in dataset_refs
            ]
            return article

        run = self.replay("P002", 8, 3, "17d79", remove_dataset_description)
        self.assertEqual(run["results"], [])
        remarks = [item for item in run["remarks"] if item["source"] == target["source"]]
        remark, = remarks
        self.assertEqual(remark["field"], "dataset")
        self.assertEqual(remark["kind"], "ambiguous-binding")
        self.assertEqual(remark["raw_text"], "0.835")
        self.assertEqual(remark["conditions"], {"method": "BM25", "metric": "Hit@1", "metric_k": 1})
        for field in remark["conditions"]:
            self.assertEqual(remark["condition_sources"][field], target["condition_sources"][field])

    def test_retrieval_ablation_binds_factor_setting_and_default(self):
        run = self.replay("P002", 8, 5, "4ca94")
        results = run["results"]
        self.assertEqual(len(results), 16)
        self.assertEqual(len({item["source"]["cell_id"] for item in results}), 16)
        self.assertEqual({item["source"]["row"] for item in results}, {1, 2, 3, 4, 5, 6, 7, 11})
        evidence = {item["id"]: item for item in run["evidence"]}
        scores = (
            ("GAT", ("0.939", "0.968")),
            ("GCN", ("0.955", "0.976")),
            ("Graph Transformer", ("0.961", "0.987")),
        )
        for row, (setting, values) in enumerate(scores, start=1):
            for col, (k, value) in enumerate(zip((1, 3), values, strict=True), start=2):
                record, = (item for item in results if item["source"]["cell_id"] == f"cell-{row}-{col}")
                self.assertEqual((record["method"], record["dataset"], record["generator"]), ("LeSeGR", "PubMedQA", ""))
                self.assertEqual((record["factor"], record["setting"]), ("Graph Encoder", setting))
                self.assertEqual((record["metric"], record["metric_k"], record["raw_value"]), (f"Hit@{k}", k, value))
                self.assertEqual(record["source"]["evidence_ids"], [f"docling-{row}-{col}"])
                self.assertEqual(record["source"]["box"], evidence[f"docling-{row}-{col}"]["box"])
                self.assertEqual(record["condition_sources"]["method"], ["96c7e570f1ea-p8-text-18"])
                self.assertEqual(record["condition_sources"]["dataset"], ["caption"])
                self.assertEqual(record["condition_sources"]["factor"], ["docling-1-0", "docling-0-0"])
                self.assertEqual(record["condition_sources"]["setting"], [f"docling-{row}-1", "docling-0-1"])
                self.assertEqual(record["condition_sources"]["metric"], [f"docling-0-{col}"])
                self.assertEqual(record["condition_sources"]["metric_k"], [f"docling-0-{col}"])
                if row == 3:
                    self.assertIs(record["setting_default"], True)
                    self.assertEqual(evidence["docling-3-1"]["text"], "Graph Transformer ∗")
                    self.assertIn("caption", record["condition_sources"]["setting_default"])
                else:
                    self.assertNotIn("setting_default", record)

    def test_retrieval_ablation_does_not_extend_the_printed_span(self):
        def shorten_factor(grid):
            item, = (item for item in grid["read"]["grid"]["cells"] if item["evidence_ids"] == ["docling-1-0"])
            item["row_span"] = 1
            return grid

        baseline = self.replay("P002", 8, 5, "4ca94")
        run = self.replay("P002", 8, 5, "4ca94", change_grid=shorten_factor)
        self.assertEqual(len(run["results"]), 12)
        for record in baseline["results"]:
            source = record["source"]
            if source["row"] not in {2, 3}:
                current, = (item for item in run["results"] if item["source"]["cell_id"] == source["cell_id"])
                self.assertEqual({k: v for k, v in current.items() if k != "id"},
                                 {k: v for k, v in record.items() if k != "id"})
                continue
            remark, = (item for item in run["remarks"] if item["source"] == source)
            self.assertEqual(remark["field"], "factor")
            self.assertEqual(remark["raw_text"], record["raw_value"])
            for field in ("method", "dataset", "metric", "metric_k", "setting"):
                self.assertEqual(remark["conditions"][field], record[field])
                self.assertEqual(remark["condition_sources"][field], record["condition_sources"][field])

    def test_retrieval_ablation_uses_parameter_names_and_values_from_grid(self):
        def rename_factor_setting(grid):
            replacements = {"Graph Encoder": "Graph Attention Layer", "GAT": "Multi-head Attention"}
            read = grid["read"]
            for item in (*read["grid"]["cells"], *read["evidence"], *read["table"]["cells"]):
                if item["text"] in replacements:
                    item["text"] = replacements[item["text"]]
                if item.get("method") in replacements:
                    item["method"] = replacements[item["method"]]
            return grid

        run = self.replay("P002", 8, 5, "4ca94", change_grid=rename_factor_setting)
        self.assertEqual(len(run["results"]), 16)
        records = [item for item in run["results"] if item["factor"] == "Graph Attention Layer"]
        self.assertEqual(len(records), 6)
        self.assertEqual({item["source"]["row"] for item in records}, {1, 2, 3})
        self.assertEqual({item["setting"] for item in records}, {"Multi-head Attention", "GCN", "Graph Transformer"})
        self.assertEqual({item["source"]["row"] for item in records if item["setting"] == "Multi-head Attention"}, {1})

    def test_row_label_keeps_its_printed_role_in_open_retrieval(self):
        cases = (
            ("P074", 8, "9e833", "58.32", "Number of samples", "50,000", None),
            ("P008", 6, "733cc", "0.6019", "Base Model", "Claude 3 Haiku", "STARK-MAG"),
        )
        for article, page, grid, value, header, label, dataset in cases:
            with self.subTest(article=article):
                run = self.replay(article, page, 6, grid)
                self.assertEqual(run["results"], [])
                remarks = [item for item in run["remarks"] if item["raw_text"] == value]
                self.assertEqual({item["field"] for item in remarks}, {"method"} if dataset else {"method", "dataset"})
                evidence = {item["id"]: item for item in run["evidence"]}
                for item in remarks:
                    self.assertNotIn("method", item["conditions"])
                    self.assertNotIn("method", item["condition_sources"])
                    self.assertEqual(item["conditions"][header], label)
                    self.assertEqual(item["conditions"].get("dataset"), dataset)
                    if dataset:
                        self.assertIn(dataset, evidence[item["condition_sources"]["dataset"][0]]["text"])
                    refs = item["condition_sources"][header]
                    self.assertEqual(refs, ["docling-1-0", "docling-0-0"])
                    self.assertEqual([evidence[ref]["text"] for ref in refs], [label, header])
                    self.assertTrue(all(evidence[ref]["box"] and evidence[ref]["page"] == page for ref in refs))

    def test_dataset_metric_headers_and_row_conditions(self):
        p057 = self.replay("P057", 8, 4, "4407")
        for method, value, depth in (("GTR Passage", "41.9", 1), ("GTR Proposition", "36.6", 2)):
            row = self.record(p057, method, "NQ", "F1", value)
            self.assertEqual(row["task"], "single-hop question answering")
            self.assertEqual(row["generator"], "PALM2-M")
            self.assertEqual(row["retrieval_top_k"], depth)
            self.assertIn("caption", row["condition_sources"]["retrieval_top_k"])
            self.assertIn(f"docling-{row['source']['row']}-1", row["condition_sources"]["generator"])
        p002 = self.replay("P002", 7, 2, "84c14")
        self.assertEqual((len(p057["results"]), len(p002["results"])), (18, 48))
        self.record(p002, "BM25", "PapersWithCodeQA", "Accuracy", "0.689")

    def test_missing_dataset_evidence_keeps_the_cell_open(self):
        def remove_single_hop(dataset):
            for fragment in dataset["fragments"]:
                fragment["text"] = fragment["text"].replace("single-hop (NQ)", "single-hop")
            return dataset

        run = self.replay("P057", 8, 4, "4407", remove_single_hop)
        self.assertFalse(any(row["dataset"] == "NQ" for row in run["results"]))
        self.assertTrue(any(item["raw_text"] == "41.9" and item["kind"] == "ambiguous-binding"
                            for item in run["remarks"]))
        self.assertTrue(any(row["dataset"] == "Hotpot" for row in run["results"]))

    def test_beam_caption_generator_applies_to_each_row(self):
        run = self.replay("P057", 8, 5, "7fa5")
        self.assertEqual(len(run["results"]), 6)
        record = self.record(run, "Beam = 5", "NQ", "F1", "35.8")
        self.assertEqual(record["source"]["evidence_ids"], ["docling-2-1"])
        self.assertEqual(record["condition_sources"]["metric"], ["docling-0-1"])
        self.assertEqual(record["condition_sources"]["dataset"], ["caption"])
        self.assertEqual(record["condition_sources"]["generator"], [
            "caption", "c63a273f4539-p7-text-12", "c63a273f4539-p6-text-22",
        ])
        for beam, row in ((1, 1), (5, 2), (10, 3)):
            rows = [item for item in run["results"] if item["source"]["row"] == row]
            self.assertEqual(len(rows), 2)
            for item in rows:
                self.assertEqual(item["generator"], "PALM2-M")
                self.assertEqual(item["method"], f"Beam = {beam}")
                self.assertEqual(item["parameters"], {"beam": beam})
                self.assertEqual(item["condition_sources"]["beam"], [f"docling-{row}-0", "docling-0-0"])

    def test_beam_without_role_evidence_keeps_the_score_open(self):
        original = self.replay("P057", 8, 5, "7fa5")
        target = self.record(original, "Beam = 5", "NQ", "F1", "35.8")

        def remove_table_reference(article):
            article["fragments"] = [
                item for item in article["fragments"] if item["id"] != "c63a273f4539-p7-text-12"
            ]
            return article

        run = self.replay("P057", 8, 5, "7fa5", remove_table_reference)
        self.assertEqual(run["results"], [])
        remark, = (item for item in run["remarks"] if item["source"] == target["source"])
        self.assertEqual(remark["raw_text"], "35.8")
        self.assertEqual(remark["field"], "generator")
        self.assertEqual(remark["conditions"], {"method": "Beam = 5", "dataset": "NQ", "metric": "F1", "beam": 5})

    def test_generator_role_from_prose_with_renamed_header(self):
        def rename_header(grid):
            read = grid["read"]
            for item in (*read["grid"]["cells"], *read["evidence"]):
                if item["text"] == "Answerer":
                    item["text"] = "Component B"
            return grid

        role_source = "c63a273f4539-p7-text-2"
        run = self.replay("P057", 8, 4, "4407", change_grid=rename_header)
        row = self.record(run, "GTR Passage", "NQ", "F1", "41.9")
        self.assertEqual(row["source"]["evidence_ids"], ["docling-5-2"])
        self.assertEqual(row["generator"], "PALM2-M")
        self.assertEqual(set(row["condition_sources"]["generator"]), {"docling-5-1", role_source})
        evidence = {item["id"]: item for item in run["evidence"]}
        self.assertEqual(evidence[role_source]["page"], 7)
        self.assertTrue(all(r["method"] in {"GTR Passage", "GTR Proposition"}
                            for r in run["results"] if r["generator"]))
        iterative, = (item for item in run["remarks"] if item["raw_text"] == "34.4")
        self.assertEqual(iterative["field"], "generator")
        self.assertEqual(iterative["conditions"]["method"], "Iterative")
        self.assertFalse(any(r["raw_value"] == "41.4" for r in run["results"]))

        def remove_role_evidence(article):
            article["fragments"] = [item for item in article["fragments"] if item["id"] != role_source]
            return article

        run = self.replay("P057", 8, 4, "4407", remove_role_evidence, rename_header)
        self.assertFalse(any(r["raw_value"] == "41.9" for r in run["results"]))
        remark, = (item for item in run["remarks"] if item["raw_text"] == "41.9")
        self.assertEqual(remark["field"], "generator")
        self.assertEqual(remark["conditions"]["method"], "GTR Passage")
        self.assertEqual(remark["source"]["evidence_ids"], ["docling-5-2"])

    def test_unfamiliar_ablation_keeps_each_read_measurement_at_its_address(self):
        run = self.replay("P005", 8, 3, "aa430326")
        expected = {
            (13, 1): ("44.12", "Beam width b = 3"), (13, 5): ("98.83", "Beam width b = 3"),
            (14, 1): ("46.24", "Beam width b = 10"), (14, 5): ("98.42", "Beam width b = 10"),
            (15, 1): ("44.68", "λ = 0.05"), (15, 5): ("98.63", "λ = 0.05"),
            (16, 1): ("45.18", "λ = 0.20"), (16, 5): ("98.37", "λ = 0.20"),
        }
        evidence = {source["id"]: source for source in run["evidence"]}
        for (row, column), (number, variant) in expected.items():
            with self.subTest(row=row, column=column):
                remarks = [item for item in run["remarks"]
                           if (item["source"]["row"], item["source"]["column"]) == (row, column)]
                self.assertEqual(len(remarks), 1)
                remark, = remarks
                self.assertEqual(remark["field"], "method")
                self.assertNotIn("method", remark["conditions"])
                self.assertEqual(remark["raw_text"], number)
                self.assertEqual(remark["conditions"]["Variant"], variant)
                self.assertEqual(remark["condition_sources"]["Variant"], [f"docling-{row}-0", "docling-0-0"])
                path = ["Dragonball", "Rec. ↑"] if column == 1 else ["SCI-DOCS", "Pre. ↑"]
                self.assertEqual(remark["source"]["headers"], path)
                self.assertEqual(remark["conditions"]["header_path"], " → ".join(path))
                self.assertEqual(remark["condition_sources"]["header_path"],
                                 [f"docling-0-{1 if column == 1 else 4}", f"docling-1-{column}"])
                self.assertEqual((remark["source"]["page"], remark["source"]["table"]), (8, 3))
                ref, = remark["source"]["evidence_ids"]
                self.assertEqual(ref, f"docling-{row}-{column}")
                self.assertEqual(remark["source"]["box"], evidence[ref]["box"])

    def test_accuracy_from_caption_preserves_roles_and_units(self):
        for document, page, table, grid, row, cases in (
            ("P012", 5, 2, "423cd568", 3, (
                ("58.12", "RP", "NQ", "Llama3.1-8B-Instruct", "%", None),
                ("58.41", "RP", "NQ", "Qwen3-8B", "%", None),
            )),
            ("P062", 15, 7, "00830bfe", 2, (
                ("39.40", "Non-RAG", "BLEnD", "Llama-3.2-1B", None, "am"),
                ("49.01", "Non-RAG", "BLEnD", "Llama-3.2-1B", None, "ar"),
            )),
        ):
            with self.subTest(document=document):
                run = self.replay(document, page, table, grid)
                for column, (value, method, dataset, model, unit, language) in enumerate(cases, 1):
                    result, = (r for r in run["results"]
                               if (r["source"]["row"], r["source"]["column"]) == (row, column))
                    self.assertEqual((result["raw_value"], result["value"]), (value, value))
                    self.assertEqual((result["method"], result["dataset"], result["generator"], result["metric"]),
                                     (method, dataset, model, "Accuracy"))
                    self.assertEqual(result["unit"], unit)
                    self.assertEqual(result.get("source_language"), language)
                    self.assertEqual(result["source"]["evidence_ids"], [f"docling-{row}-{column}"])
                    evidence = {item["id"]: item for item in run["evidence"]}
                    self.assertEqual(result["source"]["box"], evidence[f"docling-{row}-{column}"]["box"])
                    self.assertIn("caption", result["condition_sources"]["metric"])
                    if language is not None:
                        self.assertNotIn("unit", result["condition_sources"])
                        self.assertIn("caption", result["condition_sources"]["generator"])
                        self.assertIn(f"docling-1-{column}", result["condition_sources"]["source_language"])
                    else:
                        self.assertIn("caption", result["condition_sources"]["unit"])
                        self.assertIn(f"docling-1-{column}", result["condition_sources"]["generator"])
                        self.assertIn("adb2be220e6f-p5-text-13", result["condition_sources"]["generator"])

    def replay_with_caption_edit(self, article, page, table, grid, caption_id, old, new):
        def change_grid(artifact):
            read = artifact["read"]
            target = read["table"]
            target["caption"] = target["caption"].replace(old, new, 1)
            for item in read["evidence"]:
                if item["id"] == target["caption_evidence_id"]:
                    item["text"] = item["text"].replace(old, new, 1)
            return artifact

        def change_text(article_text):
            for fragment in article_text["fragments"]:
                if fragment["id"] == caption_id:
                    fragment["text"] = fragment["text"].replace(old, new, 1)
            return article_text

        return self.replay(article, page, table, grid, article_text=change_text, change_grid=change_grid)

    def test_result_remains_addressed_without_caption_accuracy(self):
        for doc, page, table, grid, row, value, caption_id, old, new, known in (
            ("P012", 5, 2, "423cd568", 3, "58.12", "adb2be220e6f-p5-text-3",
             "RAG accuracy (%)", "RAG results (%)",
             {"method": "RP", "dataset": "NQ", "generator": "Llama3.1-8B-Instruct", "unit": "%"}),
            ("P062", 15, 7, "00830bfe", 2, "39.40", "504d4a03bbec-p15-text-15",
             "Accuracy on cultural QA", "Results on cultural QA",
             {"method": "Non-RAG", "dataset": "BLEnD", "generator": "Llama-3.2-1B", "source_language": "am"}),
        ):
            with self.subTest(doc=doc):
                run = self.replay_with_caption_edit(doc, page, table, grid, caption_id, old, new)
                def at_target(item):
                    return (item["source"]["row"], item["source"]["column"]) == (row, 1)
                self.assertFalse(any(at_target(item) for item in run["results"]))
                candidates = [item for item in run["remarks"] if at_target(item)]
                self.assertEqual(len(candidates), 1, candidates)
                remark, = candidates
                self.assertEqual((remark["raw_text"], remark["field"]), (value, "metric"))
                self.assertEqual(remark["conditions"], known)
                ref, = remark["source"]["evidence_ids"]
                self.assertEqual(ref, f"docling-{row}-1")
                self.assertEqual(remark["source"]["box"],
                                 next(item["box"] for item in run["evidence"] if item["id"] == ref))

    def test_caption_accuracy_does_not_inherit_a_percent_scale(self):
        run = self.replay_with_caption_edit("P012", 5, 2, "423cd568", "adb2be220e6f-p5-text-3",
                                            "RAG accuracy (%)", "RAG accuracy")
        result = self.record(run, "RP", "NQ", "Accuracy", "58.12")
        self.assertIsNone(result["unit"])
        self.assertNotIn("unit", result["condition_sources"])

    def test_duplicate_generator_caption_leaves_the_model_open(self):
        run = self.replay("P062", 15, 10, "0c388151")
        target = [item for item in run["remarks"] if (item["source"]["row"], item["source"]["column"]) == (2, 1)]
        self.assertEqual(len(target), 1, target)
        remark, = target
        self.assertEqual(remark["field"], "generator")
        self.assertNotIn("generator", remark["conditions"])
        self.assertEqual(remark["condition_sources"]["generator_caption"], ["caption", "504d4a03bbec-p15-text-31"])
