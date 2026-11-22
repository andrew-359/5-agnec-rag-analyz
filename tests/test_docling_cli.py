import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from docling.datamodel.base_models import ConversionStatus, DoclingComponentType, ErrorItem
from docling_core.types.doc import BoundingBox, CoordOrigin, DoclingDocument, DocItemLabel, ProvenanceItem, Size, TableCell, TableData
from docling_core.types.doc.page import BoundingRectangle, TextCell
from pypdfium2 import PdfDocument

from rag_mvp.__main__ import extract
from rag_mvp.table_region import TableRegion


def bounds(left, top, right, bottom):
    return BoundingBox(l=left, t=top, r=right, b=bottom, coord_origin=CoordOrigin.TOPLEFT)


def provenance(box, text="", page=1):
    return ProvenanceItem(page_no=page, bbox=box, charspan=(0, len(text)))


def conversion(document, *, status=ConversionStatus.SUCCESS, error=None, parsed=None):
    errors = [ErrorItem(component_type=DoclingComponentType.MODEL, module_name="test-model", error_message=error)] if error else []
    pages = [SimpleNamespace(page_no=1, parsed_page=parsed)] if parsed is not None else []
    return SimpleNamespace(document=document, status=status, errors=errors, pages=pages, input=SimpleNamespace(page_count=2))


class DoclingRunTest(unittest.TestCase):
    def save(self, *conversions, strategy="docling-rapidocr"):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        source = Path(directory.name) / "input.pdf"
        with PdfDocument.new() as pdf:
            for _ in range(2):
                pdf.new_page(100, 100).close()
            pdf.save(source)
        out = Path(directory.name) / "run"
        with patch("rag_mvp.strategies.parser.DocumentConverter.convert", side_effect=conversions):
            extract(source, 1, 2, out, strategy)
        return out, json.loads((out / "run.json").read_text(encoding="utf-8"))

    def table(self, *caption_texts):
        document = DoclingDocument(name="scan")
        document.add_page(1, Size(width=100, height=100))
        table = document.add_table(TableData(), prov=provenance(bounds(1, 15, 99, 90)))
        for index, text in enumerate(caption_texts):
            prov = provenance(bounds(5, 91 + 3 * index, 90 + 3 * index, 93 + 3 * index), text)
            caption = document.add_text(DocItemLabel.CAPTION, text, prov=prov, parent=table)
            table.captions.append(caption.get_ref())
        return document

    def scan(self, *caption, shifted=False, **status):
        positions = [
            (10, 20, "Method"), (60, 20, "Top-k = 5"), (40, 22, "DataA"), (80, 22, "DataB"),
            (10, 40, "MethodA"), (10, 60, "MethodB"), (10, 80, "MethodC"),
            (40, 40, "15.5 (.15)"), (40, 60, "16.5 (.16)"), (40, 80, "18.5 (.18)"),
            (80, 40, "20.5 (.25)"), (80, 66 if shifted else 60, "24.5 (.29)"), (80, 80, "28.5 (.28)"),
        ]
        cells = [TextCell(rect=BoundingRectangle.from_bounding_box(bounds(x - 2, y - 2, x + 2, y + 2)),
                          text=text, orig=text, from_ocr=True) for x, y, text in positions]
        parsed = SimpleNamespace(textline_cells=cells, model_dump=lambda **options: [item.model_dump(**options) for item in cells])
        return conversion(self.table(*caption), parsed=parsed, **status)

    def test_grid_retains_a_number_whose_row_is_uncertain(self):
        _, run = self.save(self.scan("Table 2: Open-domain QA comparison with ModelA. Exact-match percentages as "
                                     "mean (sample SD) over two runs.", shifted=True))
        self.assertEqual(len(run["results"]), 10)
        evidence = {item["id"]: item for item in run["evidence"]}
        (fragment_id,) = run["grid"]["unplaced_evidence_ids"]
        self.assertEqual(evidence[fragment_id]["text"], "24.5 (.29)")
        self.assertEqual(evidence[fragment_id]["precision"], "text_fragment")
        remark = next(remark for remark in run["remarks"] if remark["raw_text"] == "24.5 (.29)")
        self.assertEqual(remark["source"]["evidence_ids"], [fragment_id])
        self.assertIsNone(remark["source"]["row"])

    def test_failed_docling_conversion_keeps_page_and_diagnostics(self):
        failure = conversion(DoclingDocument(name="scan"), status=ConversionStatus.FAILURE, error="Layout model failed")
        for strategy in ("docling-tableformer", "docling-rapidocr"):
            with self.subTest(strategy=strategy):
                out, run = self.save(failure, strategy=strategy)
                self.assertIsNone(run["grid"])
                self.assertEqual(run["results"], [])
                (remark,) = run["remarks"]
                self.assertIn("Docling", remark["reason"])
                self.assertEqual(remark["raw_text"], "Layout model failed")
                self.assertEqual(remark["source"]["evidence_ids"], ["page-1"])
                self.assertTrue((out / run["source"]["preview"]).is_file())
                raw = json.loads((out / "docling-diagnostics.json").read_text(encoding="utf-8"))
                self.assertEqual((raw["status"], raw["errors"][0]["error_message"]), ("failure", "Layout model failed"))

    def test_partial_rapidocr_context_diagnostics_open_the_context_page(self):
        setup = self.scan("Table 2: Main comparison with ModelA. Exact-match percentages as mean (sample SD).")
        context = conversion(DoclingDocument(name="context"), status=ConversionStatus.PARTIAL_SUCCESS, error="Text failed")
        out, run = self.save(setup, context)
        self.assertEqual(len(run["cells"]), 13)
        self.assertEqual(run["results"], [])
        remark = next(remark for remark in run["remarks"] if "Docling" in remark["reason"])
        self.assertEqual((remark["source"]["page"], remark["raw_text"]), (2, "Text failed"))
        self.assertTrue((out / run["source"]["previews"]["2"]).is_file())
        evidence = {item["id"]: item for item in run["evidence"]}
        fragment = evidence[remark["source"]["evidence_ids"][0]]
        self.assertEqual((fragment["page"], fragment["precision"]), (2, "page"))
        raw = json.loads((out / "docling-context-diagnostics.json").read_text(encoding="utf-8"))
        self.assertEqual(raw["status"], "partial_success")

    def test_multi_part_caption_link_covers_each_fragment(self):
        document = self.table("Table 2. Comparison. ", "Results on two test sets.")
        _, run = self.save(conversion(document), strategy="docling-tableformer")
        caption = next(item for item in run["evidence"] if item["id"] == run["grid"]["caption_evidence_id"])
        self.assertEqual(caption["text"], "Table 2. Comparison. Results on two test sets.")
        self.assertEqual(caption["box"], [0.05, 0.91, 0.93, 0.96])

    def test_page_cache_reuses_conversion_for_table_number_and_region(self):
        document = self.table("Table 2. First table.")
        document.add_table(TableData(), prov=provenance(bounds(10, 5, 70, 12)))
        region = TableRegion((.10, .05, .70, .12), "confirmed", "missing", None, None, None)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "input.pdf"
            with PdfDocument.new() as pdf:
                pdf.new_page(100, 100).close()
                pdf.save(source)
            with patch.dict(os.environ, {"RAG_DOCLING_PAGE_CACHE": str(root / "cache")}), patch(
                "rag_mvp.strategies.parser.DocumentConverter.convert", return_value=conversion(document),
            ) as convert:
                extract(source, 1, 2, root / "first", "docling-tableformer", grid_only=True)
                extract(source, 1, 3, root / "second", "docling-tableformer", region=region, grid_only=True)
                convert.assert_called_once()
            first = json.loads((root / "first/table-grid.json").read_text())["read"]
            second = json.loads((root / "second/table-grid.json").read_text())["read"]
            self.assertEqual(first["table"]["caption"], "Table 2. First table.")
            self.assertEqual(second["table"]["number"], 3)
            self.assertEqual(second["evidence"][0]["box"], list(region.box))
            self.assertEqual(first["raw_artifacts"], second["raw_artifacts"])

    def test_page_cache_recovers_corruption_without_caching_partial_result(self):
        document = self.table("Table 2. Standalone table.")
        responses = [conversion(document), conversion(document, status=ConversionStatus.PARTIAL_SUCCESS),
                     conversion(document)]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "input.pdf"
            cache = root / "cache"
            with PdfDocument.new() as pdf:
                pdf.new_page(100, 100).close()
                pdf.save(source)
            with patch.dict(os.environ, {"RAG_DOCLING_PAGE_CACHE": str(cache)}), patch(
                "rag_mvp.strategies.parser.DocumentConverter.convert", side_effect=responses,
            ) as convert:
                extract(source, 1, 2, root / "original", "docling-tableformer", grid_only=True)
                (entry,) = cache.glob("*.json")
                entry.write_text("{broken", encoding="utf-8")
                extract(source, 1, 2, root / "partial", "docling-tableformer", grid_only=True)
                extract(source, 1, 2, root / "recovered", "docling-tableformer", grid_only=True)
                extract(source, 1, 2, root / "cached", "docling-tableformer", grid_only=True)
                self.assertEqual(convert.call_count, 3)
            first = (root / "original/table-grid.json").read_bytes()
            self.assertEqual((root / "recovered/table-grid.json").read_bytes(), first)
            self.assertEqual((root / "cached/table-grid.json").read_bytes(), first)
            partial = json.loads((root / "partial/table-grid.json").read_text())
            self.assertEqual(partial["read"]["raw_artifacts"]["docling-diagnostics.json"]["status"], "partial_success")

    def test_merged_scores_marked_as_headers_remain_addressable(self):
        document = self.table("Table 2. Comparison.")
        rows = (
            ((1, 2, "Model A"), (3, 2, "Model B")),
            ((0, 1, "Baseline"), (1, 2, "0.31"), (3, 2, "0.26")),
            ((0, 1, "Budget"), (1, 1, "1"), (2, 1, "5"), (3, 1, "1"), (4, 1, "5")),
            ((0, 1, "System"), (1, 1, "0.5"), (2, 1, "0.8"), (3, 1, "0.6"), (4, 1, "0.9")),
            ((0, 1, "Cutoff"), (1, 2, "k=5"), (3, 2, "k=5")),
        )
        document.tables[0].data = TableData(num_rows=len(rows), num_cols=5, table_cells=[
            TableCell(
                text=text, bbox=bounds(2+column*18, 18+row*10, 2+(column+span)*18, 24+row*10),
                row_span=1, col_span=span, start_row_offset_idx=row, end_row_offset_idx=row+1,
                start_col_offset_idx=column, end_col_offset_idx=column+span, column_header=True,
            )
            for row, cells in enumerate(rows) for column, span, text in cells
        ])
        _, run = self.save(conversion(document), strategy="docling-tableformer")
        self.assertEqual(run["results"], [])
        addressed = [remark for remark in run["remarks"] if remark["source"]["cell_id"]]
        self.assertEqual({remark["raw_text"] for remark in addressed}, {"0.31", "0.26"})
        remark, = (item for item in addressed if item["raw_text"] == "0.31")
        source = remark["source"]
        self.assertEqual((source["page"], source["table"], source["row"], source["column"]), (1, 2, 1, 1))
        self.assertEqual(source["evidence_ids"], ["docling-1-1"])
        value = next(item for item in run["cells"] if item["id"] == source["cell_id"])
        self.assertEqual(value["role"], "column_header")
        numeric_headers = [item for item in run["cells"] if item["text"] in ("1", "5", "k=5")]
        self.assertEqual(len(numeric_headers), 6)
        self.assertTrue(all(item["role"] == "column_header" for item in numeric_headers))
        evidence = next(item for item in run["evidence"] if item["id"] == "docling-1-1")
        self.assertEqual(source["box"], evidence["box"])
        self.assertEqual(remark["conditions"], {
            "reader_role": "column_header", "row_label": "Baseline", "column_label": "Model A",
        })
        self.assertEqual(remark["condition_sources"]["row_label"], ["docling-1-0"])
        self.assertEqual(remark["condition_sources"]["column_label"], ["docling-0-1"])

    def test_disconnected_task_mention_leaves_table_numbers_unconfirmed(self):
        setup = self.scan("Table 2: Main comparison with ModelA. Exact-match percentages as mean (sample SD).")
        context = DoclingDocument(name="context")
        context.add_page(2, Size(width=100, height=100))
        paragraphs = ["Table 2 reports other benchmark comparisons.", "Future work will study open-domain QA."]
        for index, text in enumerate(paragraphs):
            context.add_text(DocItemLabel.TEXT, text, prov=provenance(bounds(5, 10 + 10 * index, 90, 15 + 10 * index), text, 2))
        _, run = self.save(setup, conversion(context))
        self.assertEqual(len(run["cells"]), 13)
        self.assertEqual(run["results"], [])
        self.assertEqual(run["context_evidence_ids"], [])
        self.assertTrue(any(remark["raw_text"] == "24.5 (.29)" for remark in run["remarks"]))
