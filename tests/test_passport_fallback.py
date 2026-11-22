import unittest
from dataclasses import replace

from rag_mvp.core.domain import Collection, ConditionOption, Evidence, GridCell, ReadTable, Remark, Source
from rag_mvp.passport_fallback import passport_remarks
from rag_mvp.run import TableGrid, TableRead


def read_with_caption(caption, headers, values):
    box = (.1, .2, .3, .4)
    cells = tuple(GridCell(row, col, text, column_span=span, role=role, evidence_ids=(f"e-{row}-{col}",))
                  for row, col, text, span, role in (*headers, *values))
    evidence = (Evidence("caption", 3, caption, box, "text_fragment"),
                *(Evidence(cell.evidence_ids[0], 3, cell.text, box, "cell") for cell in cells))
    return TableRead(ReadTable(3, 1, caption, "", (), "caption"),
                     TableGrid("1", 3, 4, caption_evidence_id="caption", cells=cells), evidence=evidence)


def open_remark(row, column, raw, **kwargs):
    return Remark("Открытая привязка.", raw,
                  Source(3, 1, row, column, box=(.1, .2, .3, .4), evidence_ids=(f"e-{row}-{column}",)),
                  kind="ambiguous-binding", **kwargs)


class PassportFallbackTests(unittest.TestCase):
    def test_collect_and_serialize_a_source_backed_partial_rate(self):
        header = "column_header"
        caption = ("Table 1: All values are failure rates (%). LEAK, STALE and UNSUP. denote unauthorized "
                   "leakage, stale citation, and unsupported-answer rates, respectively.")
        read = read_with_caption(caption, (
            (0, 1, "Qwen-Max", 3, header), (1, 1, "LEAK", 1, header),
            (1, 2, "STALE", 1, header), (1, 3, "UNSUP.", 1, header),
        ), ((2, 1, "37.50", 1, "data"), (2, 2, "0.0–2.0", 1, "data")))
        original = open_remark(2, 1, "37.50", field="metric")
        original = replace(original, source=replace(original.source, evidence_ids=()))
        read_remarks = passport_remarks(read, Collection((), (original,)))
        result = read.serialize(Collection((), read_remarks), 3)
        self.assertEqual(result["results"], [])
        remark, = result["remarks"]
        self.assertEqual(remark["kind"], "ambiguous-binding")
        self.assertEqual(remark["conditions"], {"metric": "Leakage rate", "unit": "%", "generator": "Qwen-Max"})
        self.assertEqual(remark["condition_sources"]["generator"], ("e-0-1",))
        self.assertIsNone(remark["field"])
        self.assertEqual(remark["condition_sources"]["metric"], ("e-1-1", "e-0-1", "caption"))
        self.assertEqual(remark["source"]["evidence_ids"], ("e-2-1",))
        self.assertEqual(read_remarks[0].source, original.source)
        self.assertEqual(passport_remarks(read, Collection((), ())), ())

    def test_ranked_caption_stays_out_of_conflicting_or_damaged_cells(self):
        read = read_with_caption("Table 1: NDCG@5 scores for retrievers.", (
            (0, 0, "Method", 1, "column_header"), (0, 1, "Dataset", 1, "column_header"),
        ), ((1, 1, ".4554", 1, "data"), (2, 1, ".33", 1, "data")))
        blocked = open_remark(1, 1, ".4554", field="metric", options=(ConditionOption("MRR", ("caption",)),))
        damaged = open_remark(2, 1, ".33")
        read = replace(read, evidence=(*read.evidence[:-1], replace(read.evidence[-1], precision="estimated_cell")))
        self.assertEqual(passport_remarks(read, Collection((), (blocked, damaged))), (blocked, damaged))
        p020 = read_with_caption("Table 1: Statistics for the cross-domain RAG datasets.", (
            (0, 0, "Dataset", 1, "column_header"), (0, 1, "Mean length", 1, "column_header"),
        ), ((1, 1, "75", 1, "data"),))
        statistics = open_remark(1, 1, "75")
        self.assertEqual(passport_remarks(p020, Collection((), (statistics,))), (statistics,))

    def test_adds_scoped_conditions_without_overwriting_remarks(self):
        header = "column_header"
        read = read_with_caption("Table 1: NDCG@5 scores for retrievers.", (
            (0, 0, "Method", 1, header), (0, 1, "Dataset", 1, header),
            (0, 2, "Generator: Qwen-Max", 1, header),
        ), ((1, 0, "BM25", 1, "row_header"), (1, 1, "NQ", 1, "data"), (1, 2, ".4554", 1, "data")))
        missing = open_remark(1, 2, ".4554", field="dataset")
        remark, = passport_remarks(read, Collection((), (missing,)))
        self.assertEqual(remark.conditions, {
            "metric": "NDCG@5", "metric_k": 5, "method": "BM25", "dataset": "NQ", "generator": "Qwen-Max",
        })
        self.assertEqual(remark.condition_sources["dataset"], ("e-1-1", "e-0-1"))
        self.assertEqual(remark.condition_sources["method"], ("e-1-0", "e-0-0"))
        self.assertEqual(remark.condition_sources["generator"], ("e-0-2",))
        self.assertIsNone(remark.field)
        disputed = replace(missing, options=(ConditionOption("HotpotQA", ("caption",)),))
        wrong_box = replace(missing, source=replace(missing.source, box=(.2, .3, .4, .5)))
        result, preserved = passport_remarks(read, Collection((), (disputed, wrong_box)))
        self.assertNotIn("dataset", result.conditions)
        self.assertEqual(result.options, disputed.options)
        self.assertEqual(preserved, wrong_box)
