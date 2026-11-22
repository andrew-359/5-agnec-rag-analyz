import unittest
from dataclasses import replace

from rag_mvp.strategies.ocr_tableformer import PositionedCell, read_grid
from rag_mvp.run import Evidence


class OcrTableFormerGridTest(unittest.TestCase):
    def test_loss_of_a_dataset_header_keeps_its_values_and_preserves_another_group(self):
        cells = []
        tokens = []

        def positioned(text, row, column, left, top, right, bottom, *, header=False, column_span=1):
            box = (left, top, right, bottom)
            cells.append(PositionedCell(row, column, 1, column_span, text, header, box))
            tokens.append(Evidence(f"token-{row}-{column}", 4, text, box, "text_fragment"))

        positioned("Method", 0, 0, .08, .10, .16, .12, header=True)
        positioned("Generator", 0, 1, .20, .10, .28, .12, header=True)
        positioned("Atlas", 0, 2, .39, .10, .47, .12, header=True, column_span=2)
        positioned("Beacon", 0, 4, .71, .10, .79, .12, header=True, column_span=2)
        for index, (name, left) in enumerate(zip(("Precision", "F1", "Precision", "F1"), (.32, .48, .64, .80)), 2):
            positioned(name, 1, index, left, .13, left + .06, .15, header=True)
        for row, top in enumerate((.18, .24, .30), 2):
            positioned(f"System {row}", row, 0, .08, top, .16, top + .02)
            positioned("Example-7B", row, 1, .20, top, .28, top + .02)
            for column, left in enumerate((.32, .48, .64, .80), 2):
                positioned(f"0.{row}{column}", row, column, left, top, left + .06, top + .02)
        caption = Evidence("caption", 4, "Table V. Comparison on the Atlas and Beacon datasets.",
                           (.1, .06, .8, .08), "text_fragment")

        _, verified, _, _ = read_grid(tuple(cells), tuple(tokens), 4, 5, caption)
        self.assertEqual(len(verified), 12)

        malformed = tuple(replace(token, text="A..as") if token.text == "Atlas" else token for token in tokens)
        _, remaining, _, remarks = read_grid(tuple(cells), malformed, 4, 5, caption)
        self.assertEqual({(item.column, item.text) for item in remaining},
                         {(4, "0.24"), (5, "0.25"), (4, "0.34"), (5, "0.35"), (4, "0.44"), (5, "0.45")})
        self.assertEqual({(item.source.column, item.raw_text, item.kind) for item in remarks},
                         {(2, "0.22", "reading-defect"), (3, "0.23", "reading-defect"),
                          (2, "0.32", "reading-defect"), (3, "0.33", "reading-defect"),
                          (2, "0.42", "reading-defect"), (3, "0.43", "reading-defect")})
