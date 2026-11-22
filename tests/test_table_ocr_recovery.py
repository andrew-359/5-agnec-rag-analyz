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


PRIMARY = "docling-ocr-tableformer"
RETRY = "paddle-structure"
REGION = {"box": [.1, .1, .9, .9], "bounds_status": "confirmed", "caption_status": "confirmed",
          "designation": "Table 1", "caption_box": [.1, .05, .9, .08],
          "caption_text": "Table 1: Multi-hop QA results on the HotpotQA dataset."}


def primary_grid(*, disagree=False):
    cells, readings, evidence, remarks = [], [], [], []
    evidence.append(Evidence("caption", 1, REGION["caption_text"], tuple(REGION["caption_box"]), "text_fragment"))

    def fragment(ref, value, left, top):
        token = Evidence(ref, 1, value, (left, top, left + .05, top + .016), "text_fragment")
        evidence.append(token)
        return token

    def grid_cell(row, column, token, role, *, top=None, bottom=None, text=None, precision="estimated_cell"):
        x0, y0, x1, y1 = token.box
        identifier = f"tableformer-{row}-{column}"
        value = token.text if text is None else text
        evidence.append(Evidence(identifier, 1, value, (x0, top if top is not None else y0, x1,
                                                         bottom if bottom is not None else y1), precision))
        cells.append(GridCell(row, column, value, role=role, evidence_ids=(identifier,)))

    for column, (x, label) in enumerate(zip((.14, .32, .50, .70), ("Method", "Generator", "F1", "EM"))):
        grid_cell(0, column, fragment(f"ocr-1-header-{column}", label, x, .2), "column_header")
    dataset = fragment("ocr-1-dataset", "HotpotQA", .60, .16)

    for row in range(1, 5):
        y = .22 + row / 10
        method = fragment(f"ocr-1-method-{row}", f"Method-{row}", .14, y)
        generator = fragment(f"ocr-1-generator-{row}", f"LLM-{row}", .32, y)
        grid_cell(row, 0, method, "row_header")
        if row != 2:
            grid_cell(row, 1, generator, "row_header")
        else:
            grid_cell(row, 1, generator, "row_header", bottom=y + .123, text="LLM-2 LLM-3")

        if row in (1, 4):
            for column, x, metric in ((2, .50, "F1"), (3, .70, "EM")):
                number = fragment(f"ocr-1-reading-{row}-{column}", f"0.{row}{column}", x, y)
                grid_cell(row, column, number, "data", precision="cell")
                readings.append(ReadCell(
                    row, column, method.text, (f"Generator: {generator.text}", "Dataset: HotpotQA", f"Metric: {metric}"),
                    number.text, number.box, verified_condition_sources={
                        "method": (method.id,), "generator": (generator.id,), "dataset": (dataset.id,),
                        "metric": (f"ocr-1-header-{column}",), "experiment": ("caption",),
                    },
                ))
        elif row == 2:
            if disagree:
                fragment("ocr-1-disagreement", "0.1234", .70, y)
            for column, x, value, top in ((2, .50, "0.51", y + .10), (3, .70, "07421", y)):
                template = Evidence("template", 1, value, (x, top, x + .05, top + .021), "estimated_cell")
                grid_cell(row, column, template, "data")
                remarks.append(Remark("OCR не подтвердил ячейку.", value, Source(1, 1, row, column, box=template.box),
                                      kind="reading-defect"))

    return TableRead(ReadTable(1, 1, REGION["caption_text"], "", tuple(readings), "caption"),
                     TableGrid("Table 1", 5, 4, caption_evidence_id="caption", cells=tuple(cells)),
                     tuple(evidence), tuple(remarks), raw_artifacts={"primary-raw.json": {"source": "primary"}})


def paddle_grid():
    tokens = (Evidence("paddle-ocr-1-wrong-row", 1, "0.51", (.50, .52, .55, .536), "text_fragment"),
              Evidence("paddle-ocr-1-right-row", 1, "0.7421", (.70, .42, .75, .436), "text_fragment"))
    return TableRead(grid=TableGrid("Table 1", 5, 4, unplaced_evidence_ids=tuple(t.id for t in tokens)),
                     evidence=tokens, raw_artifacts={"alternate-raw.json": {"source": "alternate"}})


def save_grid(path, sha256, strategy, read):
    path.write_text(json.dumps({
        "schema_version": 1, "sha256": sha256, "page": 1, "table": 1, "strategy": strategy,
        "region": REGION, "read": asdict(read),
    }))


class TableOcrRecoveryTest(TestCase):
    def test_guarded_retry_recovers_only_the_number_in_the_supported_row(self):
        with TemporaryDirectory() as directory:
            folder = Path(directory)
            pdf = folder / "source.pdf"
            with PdfDocument.new() as doc:
                doc.new_page(200, 300).close()
                doc.save(pdf)
            sha256 = hashlib.sha256(pdf.read_bytes()).hexdigest()
            primary, retry = folder / "primary.json", folder / "retry.json"
            save_grid(primary, sha256, PRIMARY, primary_grid())
            save_grid(retry, sha256, RETRY, paddle_grid())

            def extract(name, strategy, grid, *args):
                out = folder / name
                subprocess.run([sys.executable, "-m", "rag_mvp", "extract", str(pdf), "--page", "1", "--table", "1",
                                "--table-region", json.dumps(REGION), "--out", str(out), "--grid-input", str(grid),
                                "--strategy", strategy, *args], check=True, capture_output=True, text=True)
                return out, json.loads((out / "run.json").read_bytes())

            _, before = extract("before", PRIMARY, primary)
            out, after = extract("after", RETRY, retry, "--previous-grid-input", PRIMARY, str(primary))
            original = before["results"]
            self.assertEqual(after["results"][:len(original)], original)
            recovered, = after["results"][len(original):]
            self.assertEqual((recovered["method"], recovered["generator"], recovered["metric"], recovered["value"]),
                             ("Method-2", "LLM-2", "EM", "0.7421"))
            reference = recovered["source"]["evidence_ids"][0]
            token = next(e for e in after["evidence"] if e["id"] == reference)
            self.assertEqual((token["box"], token["strategy"]), ([.70, .42, .75, .436], RETRY))
            self.assertEqual(recovered["condition_sources"]["generator"], ["ocr-1-generator-2"])
            self.assertTrue(any(r["raw_text"] == "0.51" and r["kind"] == "reading-defect" for r in after["remarks"]))
            self.assertEqual(after["reading_retry"]["recovered"], 1)
            self.assertTrue((out / "first-primary-raw.json").is_file())
            self.assertTrue((out / "alternate-raw.json").is_file())

            disputed = folder / "disputed.json"
            save_grid(disputed, sha256, PRIMARY, primary_grid(disagree=True))
            _, conflict = extract("disputed", RETRY, retry, "--previous-grid-input", PRIMARY, str(disputed))
            self.assertEqual(conflict["results"], original)
            self.assertTrue(any(r["raw_text"] == "07421" and r["kind"] == "reading-defect" for r in conflict["remarks"]))
            self.assertTrue({"0.1234", "0.7421"} <= {e["text"] for e in conflict["evidence"]})
