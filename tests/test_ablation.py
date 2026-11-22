import unittest

from rag_mvp.core.domain import ReadTable
from rag_mvp.run import Evidence, GridCell, TableGrid, TableRead


class AblationRunTest(unittest.TestCase):
    def test_result_region_retains_two_scores_and_the_verified_decrease(self):
        grid = (
            GridCell(0, 0, "Ablation Setting", column_span=2, role="column_header"),
            GridCell(0, 2, "Datasets", column_span=2, role="column_header"),
            GridCell(1, 0, "Ret", role="column_header", evidence_ids=("ret",)),
            GridCell(1, 1, "Rank", role="column_header", evidence_ids=("rank",)),
            GridCell(1, 2, "Corpus A", role="column_header", evidence_ids=("corpus-a",)),
            GridCell(1, 3, "Corpus B", role="column_header"),
            GridCell(2, 0, "✓", row_span=2, evidence_ids=("ret-on",)),
            GridCell(2, 1, "✓", evidence_ids=("rank-on",)),
            GridCell(2, 2, ".680", evidence_ids=("full",)),
            GridCell(2, 3, ".650", evidence_ids=("other",)),
            GridCell(3, 2, ".520 (↓ 16.0%)", evidence_ids=("reduced",)),
        )
        evidence = (
            Evidence("ret", 2, "Ret", None, "cell"),
            Evidence("rank", 2, "Rank", None, "cell"),
            Evidence("corpus-a", 2, "Corpus A", None, "cell"),
            Evidence("ret-on", 2, "✓", (.2, .32, .25, .37), "cell"),
            Evidence("rank-on", 2, "✓", None, "cell"),
            Evidence("full", 2, ".680", (.4, .3, .5, .4), "cell"),
            Evidence("other", 2, ".650", (.6, .3, .7, .4), "cell"),
            Evidence("reduced", 2, ".520 (↓ 16.0%)", (.4, .4, .5, .5), "cell"),
            Evidence("study", 2, "For multi-hop QA, we validate the components of OurModel.", None, "text_fragment"),
            Evidence("scores", 2, "As shown in Table 4, the EM score drops.", None, "text_fragment"),
        )
        read = TableRead(
            table=ReadTable(2, 4, "Table 4: Ablation study of two components.", "", ()),
            grid=TableGrid("Table 4", 4, 4, cells=grid),
            evidence=evidence,
            context_evidence_ids=("study", "scores"),
        )

        run = read.serialize(read.collect(result_crop=(.35, .25, .55, .55)), 2)

        self.assertEqual([
            (row["method"], row["metric"], row["generator"], row["components"], row["value"], row["unit"])
            for row in run["results"]
        ], [
            ("OurModel", "EM", "", {"Ret": True, "Rank": True}, "0.680", "fraction"),
            ("OurModel", "EM", "", {"Ret": False, "Rank": False}, "0.520", "fraction"),
            ("OurModel", "EM", "", {"Ret": False, "Rank": False}, "16.0", "percentage_points"),
        ])
        full, reduced, decrease = run["results"]
        scale, = (remark for remark in run["remarks"] if remark["kind"] == "scale-interpretation")
        self.assertEqual(decrease["raw_value"], "↓ 16.0%")
        self.assertEqual(decrease["statistic"], "decrease")
        self.assertEqual(scale["result_ids"], [decrease["id"]])
        self.assertEqual(scale["source"], decrease["source"])
        self.assertEqual(decrease["source"], reduced["source"])
        self.assertNotEqual(full["source"], reduced["source"])
