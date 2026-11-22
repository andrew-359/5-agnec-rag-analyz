import csv
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from collections import Counter
from decimal import Decimal
from pathlib import Path


class ReaderOptionsTest(unittest.TestCase):
    def test_cli_describes_pdfplumber_crop_as_required_for_pdf_reading(self):
        help_text = subprocess.check_output([sys.executable, "-m", "rag_mvp", "extract", "--help"], text=True)
        choices = re.search(r"--strategy\s+\{([^}]+)\}", help_text)
        self.assertIsNotNone(choices)
        self.assertIn("pdfplumber", choices[1].split(","))
        self.assertIn("Обязательная область для pdfplumber при чтении PDF", " ".join(help_text.split()))

    def test_article_text_cli_offers_native_text_and_ocr_profiles(self):
        help_text = subprocess.check_output(
            [sys.executable, "-m", "rag_mvp", "read-article-text", "--help"], text=True,
        )
        self.assertIn("--strategy {pdfplumber,pdfplumber-ocr}", help_text)
        self.assertIn("по умолчанию pdfplumber", help_text)


@unittest.skipUnless(os.environ.get("RAG_DEV_CORPUS"), "Нужен комплект участника в RAG_DEV_CORPUS.")
class ExtractTest(unittest.TestCase):
    def test_detached_caption_cannot_reassign_an_already_captioned_grid(self):
        from docling_core.types.doc import (
            BoundingBox, CoordOrigin, DocItemLabel, DoclingDocument, ProvenanceItem, Size, TableData,
        )
        from rag_mvp.strategies.parser import find_grouped_table

        document = DoclingDocument(name="Example")
        document.add_page(page_no=1, size=Size(width=100, height=100))

        def region(top, bottom):
            return ProvenanceItem(
                page_no=1, bbox=BoundingBox(l=20, t=top, r=80, b=bottom, coord_origin=CoordOrigin.TOPLEFT),
                charspan=(0, 0),
            )

        original = document.add_text(label=DocItemLabel.CAPTION, text="Table 1: Actual caption", prov=region(10, 13))
        document.add_table(data=TableData(), caption=original, prov=region(20, 40))
        document.add_text(
            label=DocItemLabel.CAPTION,
            text="Table 2: HotpotQA answer generation performance benchmarked by different LLM generators",
            prov=region(49, 55),
        )

        self.assertIsNone(find_grouped_table(document, 1, 2))

    def saved_run(self, document, table, *options, page=6):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        out = Path(directory.name) / "run"
        pdf = Path(os.environ["RAG_DEV_CORPUS"]) / "dev" / f"{document}.pdf"
        subprocess.run([
            sys.executable, "-m", "rag_mvp", "extract", str(pdf), "--page", str(page), "--table", str(table),
            "--out", str(out), *options,
        ], check=True)
        return out, json.loads((out / "run.json").read_text(encoding="utf-8"))

    def gold(self, document, table, page=6):
        directory = Path(os.environ["RAG_DEV_CORPUS"]) / "dev_gold"
        configurations = {row["config_id"]: row for row in csv_rows(directory / "configurations.csv")
                          if row["publication_id"] == document}
        datasets = {row["dataset_id"]: row["dataset_name"] for row in csv_rows(directory / "datasets.csv")
                    if row["publication_id"] == document}
        return [(row, configurations[row["config_id"]], datasets[row["dataset_id"]])
                for row in csv_rows(directory / "results.csv")
                if (row["publication_id"], row["source_page"], row["source_table"]) == (document, str(page), f"Table {table}")]

    def test_pdfplumber_run_links_latency_values_to_their_source_cells(self):
        from pypdfium2 import PdfDocument

        out, run = self.saved_run("D008", 4, "--strategy", "pdfplumber", "--crop", "75", "70", "285", "144", page=14)
        self.assertEqual((run["strategy"], run["source"]["page"]), ("pdfplumber", 14))
        self.assertEqual(run["settings"]["crop"], [75, 70, 285, 144])
        self.assertEqual(run["settings"]["coordinate_system"], "PDF points, top-left, x0/top/x1/bottom")
        self.assertEqual(run["settings"]["table_settings"], {
            "vertical_strategy": "text", "horizontal_strategy": "text", "min_words_vertical": 2,
        })
        self.assertIn("pdfplumber", run["versions"])
        self.assertTrue((out / run["source"]["preview"]).is_file())
        self.assertIn("pdfplumber.json", run["raw_artifacts"])
        raw = json.loads((out / "pdfplumber.json").read_text(encoding="utf-8"))
        self.assertIn(["", "", ""], raw["tables"][0]["rows"])
        self.assertEqual((run["grid"]["label"], run["grid"]["rows"], run["grid"]["columns"]), ("Table 4", 6, 3))
        self.assertEqual(run["results"], [])
        self.assertEqual(csv_rows(out / "results.csv"), [])
        self.assertTrue(any("генератор" in item["reason"] and "датасет" in item["reason"] for item in run["remarks"]))

        evidence = {item["id"]: item for item in run["evidence"]}
        caption = evidence[run["grid"]["caption_evidence_id"]]
        self.assertIn("Inference latency under different prompt", caption["text"])
        cells = {(cell["row"], cell["column"]): cell for cell in run["cells"]}
        self.assertEqual(cells[0, 0]["text"].replace(" ", ""), "PromptLength(tokens)")
        self.assertEqual([cells[row, 0]["text"] for row in range(1, 6)], ["100", "259", "341", "1142", "2600"])
        methods = {"Baseline": "Vanilla-RAG", "CoDA": "CoDA"}
        self.assertEqual([cells[0, column]["text"].replace(" ", "") for column in (1, 2)], ["Baseline(s)", "CoDA(s)"])
        expected = Counter((int(row["metric_variant"].split()[2]), config["rag_variant"], row["metric_name"],
                            Decimal(row["raw_value"]), row["raw_unit"], Decimal(row["normalized_value"]))
                           for row, config, _ in self.gold("D008", 4, page=14))
        self.assertEqual(expected.total(), 10)
        actual = Counter()
        boxes = set()
        with PdfDocument(out / run["source"]["pdf"]) as document:
            page = document[13]
            page_width, page_height = page.get_size()
            text_page = page.get_textpage()
            for row in range(1, 6):
                for column in (1, 2):
                    cell = cells[row, column]
                    raw_value = Decimal(cell["text"])
                    method, seconds = cells[0, column]["text"].replace(" ", "").removesuffix(")").split("(")
                    actual[int(cells[row, 0]["text"]), methods[method], "Latency", raw_value,
                           seconds, raw_value * 1000] += 1
                    (fragment_id,) = cell["evidence_ids"]
                    fragment = evidence[fragment_id]
                    self.assertEqual((fragment["page"], fragment["precision"]), (14, "cell"))
                    left, top, right, bottom = fragment["box"]
                    boxes.add(tuple(fragment["box"]))
                    self.assertEqual(text_page.get_text_bounded(left * page_width, (1 - bottom) * page_height,
                                                                right * page_width, (1 - top) * page_height).strip(), cell["text"])
        self.assertEqual(actual, expected)
        self.assertEqual(len(boxes), 10)

    def test_pdfplumber_empty_crop_saves_page_and_raw_output(self):
        out, run = self.saved_run("D008", 4, "--strategy", "pdfplumber", "--crop", "0", "0", "25", "25", page=14)
        self.assertIsNone(run["grid"])
        (remark,) = run["remarks"]
        self.assertIn("найдено 0", remark["reason"])
        self.assertEqual(remark["source"]["evidence_ids"], ["page-14"])
        self.assertTrue((out / run["source"]["preview"]).is_file())
        raw = json.loads((out / "pdfplumber.json").read_text(encoding="utf-8"))
        self.assertEqual(raw["tables"], [])

    def test_pdfplumber_reports_the_caption_found_at_a_mismatched_crop(self):
        out, run = self.saved_run("D008", 3, "--strategy", "pdfplumber", "--crop", "75", "70", "285", "144", page=14)
        self.assertEqual(run["table"], 3)
        self.assertIsNone(run["grid"]["caption_evidence_id"])
        self.assertIn("Table 4", run["grid"]["label"])
        self.assertEqual(run["results"], [])
        self.assertTrue((out / run["source"]["preview"]).is_file())
        evidence = {item["id"]: item for item in run["evidence"]}
        remark = next(item for item in run["remarks"] if all(label in item["reason"] for label in ("Table 3", "Table 4")))
        caption = evidence[remark["source"]["evidence_ids"][0]]
        self.assertEqual((caption["page"], caption["precision"]), (14, "text_fragment"))
        self.assertIn("Inference latency", caption["text"])
        self.assertIsNotNone(caption["box"])
        raw = json.loads((out / "pdfplumber.json").read_text(encoding="utf-8"))
        self.assertEqual(raw["caption"], caption["text"])

    def test_rapidocr_run_links_both_statistics_to_the_same_fragment(self):
        out, run = self.saved_run("D013", 2, "--strategy", "docling-rapidocr")
        self.assertEqual(run["strategy"], "docling-rapidocr")
        self.assertEqual((run["grid"]["rows"], run["grid"]["columns"]), (10, 10))
        self.assertEqual(Counter(cell["role"] for cell in run["cells"]),
                         {"row_header": 8, "column_header": 13, "data": 72})
        self.assertEqual(run["remarks"], [])
        self.assertTrue((out / run["source"]["preview"]).is_file())
        for artifact in run["raw_artifacts"]:
            self.assertTrue((out / artifact).is_file())

        statistic = {"EM": "mean", "EM sample SD": "sample_sd"}
        expected = Counter((config["rag_variant"], int(config["top_k"]), dataset,
                            statistic[row["metric_name"]], Decimal(row["raw_value"]))
                           for row, config, dataset in self.gold("D013", 2))
        self.assertEqual(expected.total(), 144)
        for results in (run["results"], csv_rows(out / "results.csv")):
            self.assertEqual(Counter((row["method"], int(row["retrieval_top_k"]), row["dataset"], row["statistic"],
                                      Decimal(row["value"])) for row in results), expected)

        selected = [row for row in run["results"] if
                    (row["method"], row["retrieval_top_k"], row["dataset"]) == ("OAS", 5, "HotpotQA")]
        self.assertEqual({row["statistic"]: Decimal(row["value"]) for row in selected},
                         {"mean": Decimal("24.5"), "sample_sd": Decimal(".29")})
        self.assertEqual(len({row["id"] for row in selected}), 2)
        self.assertEqual(selected[0]["source"], selected[1]["source"])
        self.assertEqual(selected[0]["task"], "open-domain question answering")
        (fragment_id,) = selected[0]["source"]["evidence_ids"]
        evidence = {item["id"]: item for item in run["evidence"]}
        fragment = evidence[fragment_id]
        self.assertEqual((fragment["page"], fragment["text"], fragment["precision"]),
                         (6, "24.5 (.29)", "text_fragment"))
        self.assertEqual([round(value, 4) for value in fragment["box"]], [0.7955, 0.4945, 0.8467, 0.5091])
        caption_id = run["grid"]["caption_evidence_id"]
        for row in run["results"]:
            sources = row["condition_sources"]
            self.assertEqual(sources["generator"], [caption_id])
            self.assertIn(row["generator"], evidence[caption_id]["text"])
            (top_k_id,) = sources["retrieval_top_k"]
            self.assertEqual(evidence[top_k_id]["text"], row["source"]["headers"][0])
            self.assertIsNotNone(evidence[top_k_id]["box"])
        self.assertTrue((out / run["source"]["previews"]["7"]).is_file())
        self.assertTrue(any("open-domain QA" in evidence[identifier]["text"] and evidence[identifier]["page"] == 7
                            for identifier in run["context_evidence_ids"]))
        context = [evidence[identifier] for identifier in run["context_evidence_ids"]]
        self.assertTrue(any("NQ, TriviaQA, and HotpotQA" in item["text"] and item["page"] == 7 for item in context))
        self.assertTrue(any("Table 2 reports mean" in item["text"] and item["page"] == 7 for item in context))

    def test_default_tableformer_run_preserves_threadrag_values_and_distinct_cells(self):
        out, run = self.saved_run("D001", 1)
        self.assertEqual((run["strategy"], run["table"]), ("docling-tableformer", 1))
        expected = Counter((config["generator_model"], dataset, row["metric_name"], Decimal(row["raw_value"]))
                           for row, config, dataset in self.gold("D001", 1) if config["rag_variant"] == "ThreadRAG")
        self.assertEqual(expected.total(), 18)
        for results in (run["results"], csv_rows(out / "results.csv")):
            actual = Counter((row["generator"], row["dataset"], row["metric"], Decimal(row["value"]))
                             for row in results if row["method"] == "ThreadRAG")
            self.assertEqual(actual, expected)
        same_number = [row["source"] for row in run["results"]
                       if row["method"] == "ThreadRAG" and row["raw_value"] == ".586"]
        self.assertEqual({tuple(source["headers"]) for source in same_number},
                         {("Qwen3.5 plus", "HotpotQA", "EM"), ("GPT-4o mini", "2Wiki", "EM")})
        self.assertEqual(len({tuple(source["box"]) for source in same_number}), 2)
        self.assertEqual(len({source["cell_id"] for source in same_number}), 2)
        evidence = {item["id"]: item for item in run["evidence"]}
        for row in run["results"]:
            (model_id,) = row["condition_sources"]["generator"]
            self.assertEqual(evidence[model_id]["text"].strip(), row["generator"])
            self.assertIsNotNone(evidence[model_id]["box"])

    def test_saved_article_text_sets_retrieval_k_only_for_the_evaluated_method(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        pdf = Path(os.environ["RAG_DEV_CORPUS"]) / "dev" / "D001.pdf"
        text = Path(directory.name) / "article-text.json"
        with text.open("w", encoding="utf-8") as output:
            subprocess.run([sys.executable, "-m", "rag_mvp.article_context", str(pdf)], stdout=output, check=True)

        _, run = self.saved_run("D001", 1, "--article-text", str(text))
        self.assertEqual(len(run["results"]), 252)
        threadrag = [row for row in run["results"] if row["method"] == "ThreadRAG"]
        self.assertEqual(len(threadrag), 18)
        self.assertTrue(all(row["retrieval_top_k"] is None for row in run["results"] if row["method"] != "ThreadRAG"))
        fragments = {item["id"]: item for item in run["evidence"]}
        for row in threadrag:
            self.assertEqual(row["retrieval_top_k"], 5)
            quote, scope = (fragments[ref] for ref in row["condition_sources"]["retrieval_top_k"])
            self.assertEqual((quote["page"], scope["page"]), (6, 6))
            self.assertIn("We retrieve top-5 global", quote["text"])
            self.assertIn("We evaluate ThreadRAG", scope["text"])
            self.assertNotIn(quote["id"], row["source"]["evidence_ids"])

        incompatible = json.loads(text.read_text(encoding="utf-8"))
        incompatible["sha256"] = "0" * 64
        text.write_text(json.dumps(incompatible), encoding="utf-8")
        rejected = subprocess.run([
            sys.executable, "-m", "rag_mvp", "extract", str(pdf), "--page", "6", "--table", "1",
            "--out", str(Path(directory.name) / "rejected"), "--article-text", str(text),
        ], capture_output=True, text=True)
        self.assertEqual(rejected.returncode, 3)
        self.assertIn("SHA-256 общего текста не совпадает с версией PDF прогона", rejected.stderr)

    def test_d014_run_links_numbers_to_rows_generator_groups_and_caption(self):
        out, run = self.saved_run("D014", 2, page=4)

        metrics = {
            "Knowledge precision": ("KP", None),
            "Knowledge recall": ("KR", None),
            "Knowledge F1": ("KF1", None),
            "EM": ("EM", None),
            "Answer F1": ("F1", "mean"),
            "Answer F1 SEM": ("F1", "sem"),
        }
        # Match normalized model names in the gold set to the printed row-group labels.
        models = {"LLaMA3.1-8B": "LLaMA 3.1 8B", "Mistral7B-Instruct": "Mistral 7B Instruct"}
        expected = Counter(
            (config["rag_variant"].split(": ", 1)[1].capitalize(),
             models.get(config["generator_model"], config["generator_model"]),
             dataset, *metrics[row["metric_name"]], Decimal(row["raw_value"]), row["metric_unit"])
            for row, config, dataset in self.gold("D014", 2, page=4)
        )
        self.assertEqual(expected.total(), 54)
        for results in (run["results"], csv_rows(out / "results.csv")):
            actual = Counter(
                (row["method"], row["generator"], row["dataset"], row["metric"], row["statistic"] or None,
                 Decimal(row["value"]), row["unit"])
                for row in results
            )
            self.assertEqual(actual, expected)

        selected = [r for r in run["results"] if r["method"] == "No knowledge" and r["metric"] == "EM"]
        self.assertEqual(
            [(r["generator"], r["dataset"], r["value"], r["task"], r["source"]["row"]) for r in selected],
            [
                ("GPT-4o-mini", "HotpotQA", "0.330", "multi-hop question answering", 2),
                ("LLaMA 3.1 8B", "HotpotQA", "0.200", "multi-hop question answering", 6),
                ("Mistral 7B Instruct", "HotpotQA", "0.208", "multi-hop question answering", 10),
            ],
        )
        evidence = {fragment["id"]: fragment for fragment in run["evidence"]}
        caption_id = run["grid"]["caption_evidence_id"]
        self.assertTrue(evidence[caption_id]["text"].startswith("Table 2: HotpotQA answer generation"))
        self.assertIsNotNone(evidence[caption_id]["box"])
        number_ids, group_ids = set(), set()
        for result in selected:
            (number_id,) = result["source"]["evidence_ids"]
            (group_id,) = result["condition_sources"]["generator"]
            self.assertEqual(result["condition_sources"]["dataset"], [caption_id])
            for ref, value in ((number_id, result["value"]), (group_id, result["generator"])):
                self.assertEqual((evidence[ref]["page"], evidence[ref]["text"]), (4, value))
                self.assertIsNotNone(evidence[ref]["box"])
            number_ids.add(number_id)
            group_ids.add(group_id)
        self.assertEqual(len(number_ids | group_ids | {caption_id}), 7)

        pairs = {}
        for result in run["results"]:
            if result["metric"] == "F1":
                pairs.setdefault((result["method"], result["generator"]), []).append(result)
        self.assertEqual(len(pairs), 9)
        for pair in pairs.values():
            mean, sem = pair
            self.assertEqual((mean["statistic"], sem["statistic"]), ("mean", "sem"))
            self.assertNotEqual(mean["id"], sem["id"])
            self.assertEqual(mean["source"], sem["source"])
            self.assertEqual(mean["condition_sources"], sem["condition_sources"])
            (fragment_id,) = sem["source"]["evidence_ids"]
            self.assertIn("±", evidence[fragment_id]["text"])

    def test_missing_table_saves_page_and_reason(self):
        out, run = self.saved_run("D001", 99)
        self.assertIsNone(run["grid"])
        self.assertEqual(run["results"], [])
        self.assertEqual(csv_rows(out / "results.csv"), [])
        (remark,) = run["remarks"]
        self.assertIn("Table 99", remark["reason"])
        self.assertEqual(remark["source"]["evidence_ids"], ["page-6"])
        self.assertTrue((out / run["source"]["preview"]).is_file())


def csv_rows(path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))
