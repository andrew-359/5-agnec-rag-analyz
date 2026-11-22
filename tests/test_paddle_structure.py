import unittest
from pathlib import Path
from unittest.mock import patch

from rag_mvp.strategies.table_readers import TABLE_READERS
from rag_mvp.table_region import TableRegion


def read_fixture(*, missing_box=False, outside_region=False):
    region = TableRegion((0.10, 0.20, 0.90, 0.60), "confirmed", "confirmed", "Table II",
                         (0.10, 0.16, 0.90, 0.19), "Table II. Experimental accuracy.")
    rectangles = [[100, 200, 500, 400], [500, 200, 900, 400],
                  [100, 400, 500, 600], [500, 400, 900, 600]]
    if outside_region:
        rectangles[-1] = [500, 700, 900, 900]
    payload = {
        "versions": {},
        "table_res_list": [{
            "pred_html": ("<html><body><table><thead><tr><td>Method</td><td>Accuracy</td></tr></thead>"
                          "<tbody><tr><td>Candidate A</td><td>0.8109</td></tr></tbody></table></body></html>"),
            "cell_box_list": rectangles[:-1] if missing_box else rectangles,
            "table_ocr_pred": {
                "rec_texts": ["Method", "Accuracy", "Candidate A", "0.8109"],
                "rec_boxes": [[115, 245, 360, 300], [540, 245, 840, 300],
                              [115, 460, 360, 530], [570, 450, 770, 500]],
                "rec_scores": [0.98, 0.99, 0.99, 0.99],
            },
        }],
    }
    payload["table_blocks"] = [{"box": [100, 200, 900, 600], "html": payload["table_res_list"][0]["pred_html"]}]
    reader = TABLE_READERS["paddle-structure"]
    with patch("rag_mvp.strategies.paddle_structure.predict_page", return_value=(payload, (1000, 1000))):
        return reader.read(Path("paper.pdf"), 8, 2, None, region)


class PaddleStructureTest(unittest.TestCase):
    def test_registered_reader_preserves_ocr_value_and_pdf_address(self):
        read = read_fixture()
        cell = next(cell for cell in read.grid.cells if (cell.row, cell.column) == (1, 1))
        evidence = {item.id: item for item in read.evidence}

        self.assertEqual(cell.text, "0.8109")
        value = evidence[cell.evidence_ids[0]]
        self.assertEqual(value.page, 8)
        self.assertEqual(value.box, (0.5, 0.4, 0.9, 0.6))
        self.assertEqual(value.precision, "estimated_cell")
        self.assertEqual(read.collect().results, ())
        self.assertTrue(any(item.kind == "reading-defect" for item in read.collect().remarks))

    def test_mismatched_html_and_boxes_do_not_attach_a_cell_to_the_wrong_source(self):
        read = read_fixture(missing_box=True)

        self.assertIsNone(read.grid)
        self.assertEqual(read.collect().results, ())
        self.assertTrue(any(item.kind == "reading-defect" for item in read.remarks))

    def test_box_outside_region_keeps_reading_without_a_false_cell_reference(self):
        read = read_fixture(outside_region=True)
        cell = next(cell for cell in read.grid.cells if (cell.row, cell.column) == (1, 1))

        self.assertEqual(cell.text, "0.8109")
        self.assertEqual(cell.evidence_ids, ("table",))
        self.assertEqual(read.collect().results, ())
        self.assertTrue(any("границы таблицы" in item.reason for item in read.remarks))
        self.assertTrue(any(item.text == "0.8109" and item.box == (0.57, 0.45, 0.77, 0.50)
                            for item in read.evidence))
