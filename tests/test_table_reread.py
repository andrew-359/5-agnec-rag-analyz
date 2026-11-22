import hashlib
import json
from dataclasses import asdict
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from unittest import TestCase

from pypdfium2 import PdfDocument

from rag_mvp.core.domain import Evidence, GridCell, ReadCell, ReadTable, Remark, Source
from rag_mvp.run import TableGrid, TableRead


def save_grid(path, sha256, strategy, values, *, page=1, table=1, row_offset=0, with_defect=False,
              open_condition=False, region=None, top_k=None):
    cells, grid, evidence, remarks = [], [], [], []
    for index, (method, value) in enumerate(values):
        row = index + 1 + row_offset
        top = (index + 1) / 10
        box = (0.4, top, 0.5, top + 0.03)
        identifier = f"value-{row}"
        headers = ("Dataset: HotpotQA", "Metric: F1")
        if top_k is not None:
            headers = (f"Top-k={top_k}", "HotpotQA")
            header_id = f"top-k-{row}"
            evidence.append(Evidence(header_id, page, headers[0], (0.4, 0.04, 0.5, 0.07), "cell"))
            header_evidence = (header_id, None)
        else:
            header_evidence = ()
        if not open_condition and top_k is None:
            headers = ("Model: GPT-4", *headers)
        cells.append(ReadCell(row, 1, method, headers, value, box, header_evidence_ids=header_evidence))
        grid.append(GridCell(row, 1, value, evidence_ids=(identifier,)))
        evidence.append(Evidence(identifier, page, value, box, "cell"))
        if with_defect and method == "B":
            remarks.append(Remark("Контрольный дефект чтения сетки.", value, Source(page, table, row, 1, box=box),
                                  kind="reading-defect"))
    caption = f"Table {table}: Evaluation results for multi-hop QA"
    if top_k is not None:
        caption += ". Exact-match comparison with GPT-4, mean (SD)."
    caption_id = None
    if region and region.get("caption_status") == "confirmed":
        caption_id = "ocr-caption"
        evidence.append(Evidence(caption_id, page, region["caption_text"], tuple(region["caption_box"]), "text_fragment"))
    read = TableRead(
        table=ReadTable(page, table, caption, "", tuple(cells), caption_evidence_id=caption_id),
        grid=TableGrid(f"Table {table}", len(cells) + 1 + row_offset, 2, caption_evidence_id=caption_id, cells=tuple(grid)),
        evidence=tuple(evidence), remarks=tuple(remarks),
    )
    path.write_text(json.dumps({
        "schema_version": 1, "sha256": sha256, "page": page, "table": table, "strategy": strategy,
        **({"region": region} if region is not None else {}), "read": asdict(read),
    }))


class TableRereadTest(TestCase):
    def test_confirmed_ocr_caption_connects_only_conditions_referenced_in_article_text(self):
        with TemporaryDirectory() as directory:
            folder = Path(directory)
            pdf = folder / "mixed.pdf"
            with PdfDocument.new() as document:
                for _ in range(2):
                    document.new_page(200, 300).close()
                document.save(pdf)
            sha256 = hashlib.sha256(pdf.read_bytes()).hexdigest()
            grid, article_text = (folder / name for name in ("grid.json", "article-text.json"))
            region = {
                "box": [0.1, 0.08, 0.9, 0.35], "bounds_status": "confirmed", "caption_status": "confirmed",
                "designation": "Table II", "caption_box": [0.1, 0.03, 0.9, 0.07],
                "caption_text": "Table II: Results for HotpotQA with Other-Model as the generator.",
            }
            strategy = "docling-ocr-tableformer"
            save_grid(grid, sha256, strategy, (("PolyRAG", "0.8109"),), page=2, table=2, region=region, open_condition=True)
            conditions = "Implementation Details. We used GPT-4 as the generator for PolyRAG."
            for linked in (True, False):
                prose = [Evidence("heading", 1, "4 Experiments", None, "page"),
                         Evidence("conditions", 1, conditions, None, "page")]
                if linked:
                    prose.append(Evidence("results-reference", 1, "Results are reported in Table II.", None, "page"))
                article_text.write_text(json.dumps({
                    "schema_version": 1, "document": pdf.name, "sha256": sha256, "pages": [1, 2],
                    "fragments": [asdict(fragment) for fragment in prose], "engine_version": "fixture",
                }))
                run = folder / f"run-{linked}"
                execution = subprocess.run([
                    sys.executable, "-m", "rag_mvp", "extract", str(pdf), "--page", "2", "--table", "2",
                    "--strategy", strategy, "--grid-input", str(grid), "--table-region", json.dumps(region),
                    "--article-text", str(article_text), "--out", str(run),
                ], capture_output=True, text=True)
                self.assertEqual(execution.returncode, 0, execution.stderr)
                saved = json.loads((run / "run.json").read_text())
                context = json.loads((run / "article-context.json").read_text())
                if linked:
                    self.assertEqual(context["status"], "supported")
                    (result,) = saved["results"]
                    self.assertEqual((result["generator"], result["value"]), ("GPT-4", "0.8109"))
                    self.assertEqual(result["condition_sources"]["generator"], ["conditions"])
                    (candidate,) = context["candidates"]
                    (caption,) = (item["source"] for item in candidate["basis"] if item["kind"] == "caption")
                    self.assertEqual(caption["page"], 2)
                    self.assertEqual(caption["box"], region["caption_box"])
                    self.assertEqual(caption["text"], region["caption_text"])
                else:
                    self.assertEqual(context["status"], "not_found")
                    self.assertEqual(saved["results"], [])

    def test_equal_numbers_under_different_top_k_retains_both_conditions_and_sources(self):
        with TemporaryDirectory() as directory:
            folder = Path(directory)
            pdf = folder / "source.pdf"
            with PdfDocument.new() as document:
                document.new_page(200, 300).close()
                document.save(pdf)
            sha256 = hashlib.sha256(pdf.read_bytes()).hexdigest()
            first, second, run = (folder / name for name in ("first.json", "second.json", "run"))
            save_grid(first, sha256, "docling-tableformer", (("A", "0.422"),), top_k=0)
            save_grid(second, sha256, "docling-rapidocr", (("A", "0.422"),), top_k=5)
            execution = subprocess.run([
                sys.executable, "-m", "rag_mvp", "extract", str(pdf), "--page", "1", "--table", "1",
                "--strategy", "docling-rapidocr", "--grid-input", str(second),
                "--previous-grid-input", "docling-tableformer", str(first), "--out", str(run),
            ], capture_output=True, text=True)
            self.assertEqual(execution.returncode, 0, execution.stderr)
            saved = json.loads((run / "run.json").read_text())
            self.assertEqual(saved["results"], [])
            (conflict,) = (item for item in saved["remarks"] if item["kind"] == "reading-conflict")
            evidence = {fragment["id"]: fragment for fragment in saved["evidence"]}
            for option, expected in zip(conflict["options"], (0, 5), strict=True):
                self.assertEqual(option["value"], "0.422")
                self.assertEqual(option["retrieval_top_k"], expected)
                (condition_id,) = option["condition_sources"]["retrieval_top_k"]
                self.assertEqual(evidence[condition_id]["text"], f"Top-k={expected}")

    def test_saved_ocr_grid_reports_missing_article_text_without_disputing_the_caption(self):
        with TemporaryDirectory() as directory:
            folder = Path(directory)
            pdf = folder / "scan.pdf"
            with PdfDocument.new() as document:
                document.new_page(200, 300).close()
                document.save(pdf)
            sha256 = hashlib.sha256(pdf.read_bytes()).hexdigest()
            grid, article_text, run = (folder / name for name in ("grid.json", "article-text.json", "run"))
            region = {
                "box": [0.1, 0.08, 0.9, 0.35], "bounds_status": "confirmed", "caption_status": "confirmed",
                "designation": "Table II", "caption_box": [0.1, 0.03, 0.9, 0.07],
                "caption_text": "TABLE II. Evaluation results for multi-hop QA",
            }
            strategy = "docling-ocr-tableformer"
            save_grid(grid, sha256, strategy, (("PolyRAG", "0.8109"),), table=2, region=region)
            article_text.write_text(json.dumps({
                "schema_version": 1, "document": pdf.name, "sha256": sha256, "pages": [1],
                "fragments": [], "engine_version": "fixture",
            }))
            execution = subprocess.run([
                sys.executable, "-m", "rag_mvp", "extract", str(pdf), "--page", "1", "--table", "2",
                "--strategy", strategy, "--grid-input", str(grid), "--table-region", json.dumps(region),
                "--article-text", str(article_text), "--out", str(run),
            ], capture_output=True, text=True)
            self.assertEqual(execution.returncode, 0, execution.stderr)
            saved = json.loads((run / "run.json").read_text())
            self.assertEqual(saved["region"]["caption_status"], "confirmed")
            (result,) = saved["results"]
            self.assertEqual((result["method"], result["value"]), ("PolyRAG", "0.8109"))
            context = json.loads((run / "article-context.json").read_text())
            self.assertEqual(context["status"], "not_found")
            self.assertIn("В доступном тексте статьи не найдены условия, связанные с этой таблицей.", context["reason"])
            self.assertIn("Подпись подтверждена отдельно по области таблицы.", context["reason"])
            self.assertIn(context["reason"], [remark["reason"] for remark in saved["remarks"]])

            without_text = folder / "without-text"
            command = [
                sys.executable, "-m", "rag_mvp", "extract", str(pdf), "--page", "1", "--table", "2",
                "--strategy", strategy, "--grid-input", str(grid), "--table-region", json.dumps(region),
            ]
            execution = subprocess.run([*command, "--out", str(without_text)], capture_output=True, text=True)
            self.assertEqual(execution.returncode, 0, execution.stderr)
            local_path = without_text / "article-context.json"
            local = json.loads(local_path.read_bytes())
            self.assertIsNone(local["engine_version"])
            self.assertEqual(local["local_basis"][0]["source"]["id"], "ocr-caption")
            self.assertEqual(json.loads((without_text / "run.json").read_bytes())["results"], saved["results"])

            replay = folder / "replay"
            execution = subprocess.run([*command, "--context-input", str(local_path), "--out", str(replay)],
                                       capture_output=True, text=True)
            self.assertEqual(execution.returncode, 0, execution.stderr)
            self.assertEqual((replay / "article-context.json").read_bytes(), local_path.read_bytes())
            self.assertEqual(json.loads((replay / "run.json").read_bytes())["results"], saved["results"])

    def test_saved_retry_preserves_independent_values_and_holds_a_conflict_with_both_sources(self):
        with TemporaryDirectory() as directory:
            folder = Path(directory)
            pdf = folder / "source.pdf"
            with PdfDocument.new() as document:
                document.new_page(200, 300).close()
                document.save(pdf)
            sha256 = hashlib.sha256(pdf.read_bytes()).hexdigest()
            first, second, run = (folder / name for name in ("first.json", "second.json", "run"))
            region = {"box": [0.1, 0.08, 0.9, 0.35], "bounds_status": "confirmed", "caption_status": "missing"}
            save_grid(first, sha256, "docling-tableformer",
                      (("A", "0.42"), ("B", "0.44 0.45"), ("C", "0.63")), region=region)
            save_grid(second, sha256, "docling-rapidocr",
                      (("A", "—"), ("B", "0.44"), ("C", "0.64")), row_offset=1, region=region)
            command = [
                sys.executable, "-m", "rag_mvp", "extract", str(pdf), "--page", "1", "--table", "1",
                "--strategy", "docling-rapidocr", "--grid-input", str(second), "--table-region", json.dumps(region),
                "--previous-grid-input", "docling-tableformer", str(first), "--out",
            ]
            execution = subprocess.run([*command, str(run)], capture_output=True, text=True)
            self.assertEqual(execution.returncode, 0, execution.stderr)
            saved = json.loads((run / "run.json").read_text())
            evidence = {fragment["id"]: fragment for fragment in saved["evidence"]}
            rows = {result["method"]: result for result in saved["results"]}
            self.assertEqual({method: row["value"] for method, row in rows.items()}, {"A": "0.42", "B": "0.44"})
            for method, strategy in (("A", "docling-tableformer"), ("B", "docling-rapidocr")):
                source = rows[method]["source"]
                self.assertEqual(evidence[source["evidence_ids"][0]]["text"], rows[method]["raw_value"])
                self.assertEqual(evidence[source["evidence_ids"][0]]["box"], source["box"])
                self.assertEqual(evidence[source["evidence_ids"][0]]["strategy"], strategy)

            conflict, = (remark for remark in saved["remarks"] if remark["kind"] == "reading-conflict")
            self.assertEqual([option["value"] for option in conflict["options"]], ["0.63", "0.64"])
            self.assertEqual([evidence[option["evidence_ids"][0]]["text"] for option in conflict["options"]], ["0.63", "0.64"])
            self.assertEqual(saved["reading_retry"]["recovered"], 1)
            self.assertEqual(saved["reading_retry"]["conflicts"], 1)
            self.assertNotIn("C,", (run / "results.csv").read_text())

            original = json.loads(first.read_text())
            for index, (mutation, error) in enumerate((
                ({"region": {**region, "box": [0.2, 0.08, 0.9, 0.35]}}, "Область сохранённой сетки"),
                ({"sha256": "0" * 64}, "SHA-256 сетки"),
            )):
                with self.subTest(error=error):
                    first.write_text(json.dumps({**original, **mutation}))
                    rejected = folder / f"rejected-{index}"
                    execution = subprocess.run([*command, str(rejected)], capture_output=True, text=True)
                    self.assertEqual(execution.returncode, 3, execution.stderr)
                    self.assertIn(error, execution.stderr)
                    self.assertFalse(rejected.exists())
