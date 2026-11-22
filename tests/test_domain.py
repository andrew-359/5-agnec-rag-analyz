import unittest
from dataclasses import replace

from rag_mvp.core.domain import ConditionAssertion, ReadCell, ReadTable, RowGroup, TableTarget, collect_results
from rag_mvp.run import Evidence, TableRead


class CollectResultsTest(unittest.TestCase):
    def test_task_context_uses_the_verified_dataset_source_independently_of_the_method(self):
        table = replace(labeled_table(("Dataset: HotpotQA", "Model: GPT-4o mini", "EM")),
                        caption="Table 1: Comparison on the HotpotQA dataset.", context="")
        description = Evidence("dataset-task", 2, "HotpotQA is a dataset for question answering.", None, "page")
        self.assertEqual(collect_results(table, context_evidence=(description,)).results, ())

        cell = replace(table.cells[0], verified_condition_sources={"dataset": ("header-dataset",)})
        result, = collect_results(replace(table, cells=(cell,)), context_evidence=(description,)).results
        self.assertEqual(result.task, "question answering")
        self.assertEqual(result.condition_sources["dataset"], ("header-dataset",))
        self.assertEqual(result.condition_sources["task"], ("dataset-task",))

    def test_explicit_header_roles_follow_labels_and_keep_generator_source(self):
        table = labeled_table(("Dataset: HotpotQA", "Model: GPT-4o mini", "EM"))
        table = replace(table, context="For evaluation, multi-hop QA uses HotpotQAPlus (Author, 2026).")

        collected = collect_results(table)

        result, = collected.results
        self.assertEqual((result.dataset, result.generator, result.metric, result.value),
                         ("HotpotQA", "GPT-4o mini", "EM", "0.422"))
        self.assertEqual(result.condition_sources, {"generator": ("header-model",)})
        self.assertEqual((result.source.row, result.source.column), (3, 2))
        self.assertEqual(collected.remarks, ())

    def test_header_merges_agreeing_evidence_and_keeps_confirmed_conflicts_open(self):
        table = labeled_table(("Dataset: HotpotQA", "Model: GPT-4o mini", "EM"))
        table = replace(table, cells=(replace(table.cells[0], is_baseline=True),))
        agreement = ConditionAssertion("generator", "GPT-4o mini", "Self-RAG", "agreement")
        possible = ConditionAssertion("generator", "Model B", "most baselines", "possible")
        conflict = ConditionAssertion("generator", "Model B", "all methods", "conflict")

        result, = collect_results(table, (agreement, possible)).results
        self.assertEqual(result.condition_sources["generator"], ("header-model", "agreement"))
        self.assertEqual(collect_results(table, (possible,)).results[0].generator, "GPT-4o mini")

        collected = collect_results(table, (agreement, conflict))
        self.assertEqual(collected.results, ())
        remark, = collected.remarks
        self.assertEqual((remark.field, remark.source, remark.raw_text), ("generator", result.source, ".422"))
        self.assertEqual(remark.condition_sources, {})
        self.assertEqual([(option.value, option.evidence_ids) for option in remark.options], [
            ("GPT-4o mini", ("header-model", "agreement")), ("Model B", ("conflict",)),
        ])

    def test_quote_only_applies_to_the_table_it_targets(self):
        table = labeled_table(("Dataset: HotpotQA", "Model: GPT-4o mini", "EM"))
        quote = ConditionAssertion("generator", "Model B", "Self-RAG", "other-quote",
                                   target_table=TableTarget(8, 1))

        local = collect_results(table, (quote,))
        same_table = collect_results(replace(table, page=8), (quote,))

        self.assertEqual([r.generator for r in local.results], ["GPT-4o mini"])
        self.assertEqual(local.remarks, ())
        self.assertEqual(same_table.results, ())
        self.assertEqual(same_table.remarks[0].options[1].evidence_ids, ("other-quote",))
        self.assertEqual(TableRead(table=table, evidence=(
            Evidence("header-model", 5, "Model: GPT-4o mini", None, "cell"),
        )).collect((quote,)), local)

    def test_three_header_levels_require_evidence_for_generator_dataset_and_metric(self):
        table = labeled_table(("Training", "Test", "EM"))
        collected = collect_results(table)

        self.assertEqual(collected.results, ())
        remark, = collected.remarks
        self.assertEqual((remark.raw_text, remark.source.row, remark.source.column), (".422", 3, 2))

    def test_unlabeled_two_level_header_requires_benchmarks_and_a_metric(self):
        table = labeled_table(("HotpotQA", "EM"))
        assertion = ConditionAssertion("generator", "Model B", "Self-RAG", "quote")

        self.assertEqual([r.generator for r in collect_results(table, (assertion,)).results], ["Model B"])
        for invalid_table in (
            replace(table, caption="Table 1: Results on multi-hop QA."),
            labeled_table(("HotpotQA", "Test")),
        ):
            with self.subTest(caption=invalid_table.caption, headers=invalid_table.cells[0].headers):
                collected = collect_results(invalid_table, (assertion,))
                self.assertEqual(collected.results, ())
                self.assertEqual(len(collected.remarks), 1)

        labeled = labeled_table(("Dataset: HotpotQA", "Metric: EM"))
        self.assertEqual(collect_results(labeled, (assertion,)).results[0].condition_sources, {"generator": ("quote",)})

    def test_d009_keeps_a_generator_open_when_only_most_baselines_are_named(self):
        table = ReadTable(
            page=5,
            number=1,
            caption="Table 1: Comparison of different RAG methods across 5 benchmarks using EM and F1 metrics.",
            context="For multi-hop QA, we use HotpotQA (Yang et al., 2018).",
            cells=(
                ReadCell(12, 4, "PRO-STEP", ("HotpotQA", "EM"), "38.7", is_baseline=False),
                ReadCell(5, 4, "Self-RAG", ("HotpotQA", "EM"), "18.2", is_baseline=True),
                ReadCell(4, 4, "FLARE", ("HotpotQA", "EM"), "18.8", is_baseline=True),
            ),
        )
        assertions = (
            ConditionAssertion("generator", "Qwen2.5-7B-Instruct", "PRO-STEP and most baselines", "qwen"),
            ConditionAssertion("generator", "Llama-2-13B", "Self-RAG", "self-rag"),
            ConditionAssertion("retrieval_top_k", 3, "across all methods", "retrieval"),
        )

        collected = collect_results(table, assertions)

        self.assertEqual([(row.method, row.generator, row.value, row.retrieval_top_k) for row in collected.results], [
            ("PRO-STEP", "Qwen2.5-7B-Instruct", "38.7", 3),
            ("Self-RAG", "Llama-2-13B", "18.2", 3),
        ])
        self.assertEqual(collected.results[1].condition_sources, {"generator": ("self-rag",), "retrieval_top_k": ("retrieval",)})
        remark, = (item for item in collected.remarks if item.field == "generator")
        self.assertEqual((remark.raw_text, remark.source.row, remark.source.column), ("18.8", 4, 4))
        self.assertEqual(remark.conditions["retrieval_top_k"], 3)
        self.assertEqual(remark.condition_sources["retrieval_top_k"], ("retrieval",))
        self.assertEqual([(option.value, option.evidence_ids) for option in remark.options], [("Qwen2.5-7B-Instruct", ("qwen",))])

    def test_same_number_keeps_both_header_paths(self):
        table = ReadTable(
            page=6,
            number=1,
            caption="Table 1: Main results of multi-hop QA datasets.",
            context="Datasets & Metrics. We evaluate on open-domain multi-hop QA benchmarks: "
            "HotpotQA (Yang et al., 2018) and 2WikiMultihopQA (Ho et al., 2020). "
            "For evaluation, we report Exact Match (EM) and token-level F1.\n"
            "We use Qwen-3.5-Plus (Yang et al., 2025) and GPT-4o-mini (OpenAI et al., 2024) "
            "with nomic-embed-text for embeddings.",
            cells=(
                ReadCell(18, 7, "ThreadRAG", ("Qwen3.5 plus", "HotpotQA", "EM"), ".586"),
                ReadCell(18, 15, "ThreadRAG", ("GPT-4o mini", "2Wiki", "EM"), ".586"),
            ),
        )

        protocol = table.context.splitlines()[0]
        fragment = Evidence("evaluation-protocol", 6, protocol, (20, 30, 200, 80), "text_fragment")
        results = collect_results(table, context_evidence=(fragment,)).results
        self.assertTrue(all(row.condition_sources.get("protocol") == (fragment.id,) for row in results))

        self.assertEqual(
            [(r.generator, r.dataset, r.metric, r.value, r.source.column) for r in results],
            [
                ("Qwen3.5 plus", "HotpotQA", "EM", "0.586", 7),
                ("GPT-4o mini", "2WikiMultihopQA", "EM", "0.586", 15),
            ],
        )

    def test_run_rejects_a_condition_without_its_source_fragment(self):
        assertion = ConditionAssertion("generator", "Example model", "Ours (OAS)", "missing-quote")
        with self.assertRaisesRegex(ValueError, "missing-quote"):
            TableRead(table=d013_table("24.5 (.29)")).collect((assertion,))

    def test_run_rejects_ambiguous_source_ids(self):
        assertion = ConditionAssertion("generator", "Model A", "Ours (OAS)", "quote")
        for source_id in ("quote", "page-6"):
            with self.subTest(source_id=source_id), self.assertRaisesRegex(ValueError, source_id):
                table = TableRead(
                    table=d013_table("24.5 (.29)"),
                    evidence=(
                        Evidence("quote", 6, "Model A is used.", None, "page"),
                        Evidence(source_id, 6, "Another fragment.", None, "page"),
                    ),
                )
                table.collect((assertion,))

    def test_conflicting_conditions_keep_distinct_values_and_all_citations(self):
        table = ReadTable(
            5, 1, "Comparison of methods across benchmarks", "Multi-hop QA.",
            (ReadCell(4, 2, "Example RAG", ("HotpotQA", "EM"), "18.8"),),
        )
        assertions = (
            ConditionAssertion("generator", "Model A", "Example RAG", "first"),
            ConditionAssertion("generator", "Model A", "Example RAG", "second"),
            ConditionAssertion("generator", "Other model", "Example RAG 2", "other"),
        )
        for scope in ("Example RAG", "all methods"):
            with self.subTest(scope=scope):
                conflicting = ConditionAssertion("generator", "Model B", scope, "third")
                collected = collect_results(table, (*assertions, conflicting))

                self.assertEqual(collected.results, ())
                remark, = collected.remarks
                self.assertEqual(remark.field, "generator")
                self.assertEqual([(option.value, option.evidence_ids) for option in remark.options], [
                    ("Model A", ("first", "second")), ("Model B", ("third",)),
                ])

    def test_unreadable_sd_preserves_mean_and_refers_to_same_cell(self):
        collected = collect_results(d013_table("24.5 (?)"))

        self.assertEqual([(r.value, r.unit, r.statistic) for r in collected.results], [("24.5", "%", "mean")])
        result = collected.results[0]
        self.assertEqual((result.generator, result.dataset, result.metric, result.retrieval_top_k),
                         ("Qwen2.5-3B-Instruct", "HotpotQA", "EM", 5))
        self.assertEqual(len(collected.remarks), 1)
        self.assertIn("SD", collected.remarks[0].reason)
        self.assertEqual(collected.remarks[0].source, result.source)

    def test_two_statistics_can_share_one_cell(self):
        collected = collect_results(d013_table("24.5 (.29)"))

        self.assertEqual([(r.value, r.unit, r.statistic) for r in collected.results], [
            ("24.5", "%", "mean"), ("0.29", "percentage_point", "sample_sd"),
        ])
        self.assertEqual(collected.results[0].source, collected.results[1].source)
        self.assertEqual(collected.remarks, ())

    def test_zero_mean_and_sample_sd_are_confirmed_results(self):
        collected = collect_results(d013_table("0 (0)"))

        self.assertEqual([(r.value, r.raw_value, r.statistic) for r in collected.results], [
            ("0", "0", "mean"), ("0", "0", "sample_sd"),
        ])
        self.assertEqual(collected.remarks, ())

    def test_retrieval_top_k_preserves_explicit_zero_from_header_and_quote(self):
        table = d013_table("24.5 (.29)")
        cell, = table.cells
        with_header = replace(table, cells=(replace(cell, headers=("Top-k = 0", "HotpotQA")),))
        without_top_k = replace(
            table, cells=(replace(cell, headers=("Model: Qwen2.5-3B-Instruct", "Dataset: HotpotQA", "EM")),),
        )

        from_header = collect_results(with_header)
        unknown = collect_results(without_top_k)
        assertion = ConditionAssertion("retrieval_top_k", 0, "Ours (OAS)", "no-retrieval")
        from_quote = collect_results(without_top_k, (assertion,))

        self.assertEqual([r.retrieval_top_k for r in from_header.results], [0, 0])
        self.assertEqual([r.retrieval_top_k for r in unknown.results], [None, None])
        self.assertEqual([r.retrieval_top_k for r in from_quote.results], [0, 0])
        self.assertEqual([r.condition_sources for r in from_quote.results], [
            {"retrieval_top_k": ("no-retrieval",)},
            {"retrieval_top_k": ("no-retrieval",)},
        ])

    def test_invalid_retrieval_top_k_is_rejected_in_quotes_and_headers(self):
        for value in (-1, 1.5, True, 2**53):
            with self.subTest(quote=value), self.assertRaisesRegex(ValueError, "retrieval_top_k"):
                ConditionAssertion("retrieval_top_k", value, "Example RAG", "quote")

        table = d013_table("24.5 (.29)")
        cell, = table.cells
        generator = ConditionAssertion("generator", "Qwen2.5-3B-Instruct", "Ours (OAS)", "generator")
        for value in ("-1", "1.5", "True", str(2**53), ""):
            with self.subTest(header=value):
                headers = (f"Top-k = {value}", "HotpotQA")
                with_invalid_header = replace(table, cells=(replace(cell, headers=headers),))

                collected = collect_results(with_invalid_header, (generator,))

                self.assertEqual(collected.results, ())
                remark, = collected.remarks
                self.assertEqual(remark.raw_text, cell.text)
                self.assertEqual(remark.source.headers, headers)

    def test_top_k_header_conflict_keeps_caption_and_both_top_k_sources(self):
        table = d013_table("24.5 (.29)")
        cell = replace(table.cells[0], headers=("Top-k = 0", "HotpotQA"), header_evidence_ids=("top-k", None))
        table = replace(table, cells=(cell,), caption_evidence_id="caption")
        quote = ConditionAssertion("retrieval_top_k", 3, "Ours (OAS)", "quote")

        collected = collect_results(table, (quote,))

        self.assertEqual(collected.results, ())
        remark, = collected.remarks
        self.assertEqual(remark.field, "retrieval_top_k")
        self.assertEqual(remark.condition_sources, {"generator": ("caption",)})
        self.assertEqual([(option.value, option.evidence_ids) for option in remark.options], [
            (0, ("top-k",)), (3, ("quote",)),
        ])

    def test_d014_binds_repeated_methods_to_their_groups_and_caption(self):
        collected = collect_results(d014_table())

        self.assertEqual([
            (r.method, r.generator, r.dataset, r.metric, r.task, r.value, r.retrieval_top_k, r.source.row)
            for r in collected.results
        ], [
            ("No knowledge", "GPT-4o-mini", "HotpotQA", "EM", "multi-hop question answering", "0.330", None, 2),
            ("No knowledge", "LLaMA 3.1 8B", "HotpotQA", "EM", "multi-hop question answering", "0.200", None, 6),
            ("No knowledge", "Mistral 7B Instruct", "HotpotQA", "EM", "multi-hop question answering", "0.208", None, 10),
        ])
        self.assertEqual([r.condition_sources for r in collected.results], [
            {"generator": ("group-gpt",), "dataset": ("caption",)},
            {"generator": ("group-llama",), "dataset": ("caption",)},
            {"generator": ("group-mistral",), "dataset": ("caption",)},
        ])

    def test_d014_does_not_borrow_a_group_for_an_unbound_row(self):
        table = d014_table()
        first, second, third = table.cells
        table = replace(table, cells=(first, replace(second, row_group=None), third))

        collected = collect_results(table)

        self.assertEqual([r.generator for r in collected.results], ["GPT-4o-mini", "Mistral 7B Instruct"])
        unbound, = (remark for remark in collected.remarks if remark.source.row == second.row)
        self.assertEqual((unbound.raw_text, unbound.source.column), ("0.200", 4))

    def test_d014_reads_knowledge_quality_metrics_with_their_group(self):
        table = d014_table()
        cells = tuple(
            ReadCell(3, column, "Full knowledge", (metric,), raw, row_group=RowGroup("GPT-4o-mini", "group-gpt"))
            for column, (metric, raw) in enumerate((("KP", "0.065"), ("KR", "1"), ("KF1", "0.120")), start=1)
        )

        collected = collect_results(replace(table, cells=cells))

        self.assertEqual([
            (r.metric, r.value, r.unit, r.statistic, r.generator, r.dataset, r.source.column)
            for r in collected.results
        ], [
            ("KP", "0.065", "fraction", None, "GPT-4o-mini", "HotpotQA", 1),
            ("KR", "1", "fraction", None, "GPT-4o-mini", "HotpotQA", 2),
            ("KF1", "0.120", "fraction", None, "GPT-4o-mini", "HotpotQA", 3),
        ])
        self.assertTrue(all(
            r.condition_sources == {"generator": ("group-gpt",), "dataset": ("caption",)}
            for r in collected.results
        ))

    def test_d014_f1_and_sem_are_distinct_results_from_the_same_cell(self):
        table = d014_table()
        em = table.cells[0]
        f1 = replace(em, column=5, headers=("F1",), text="0.437 (± .020)")
        table = replace(
            table, cells=(em, f1),
            caption=f"{table.caption} We measure answer exact match (EM) and answer F1 (and its standard error mean).",
        )

        collected = collect_results(table)

        self.assertEqual([(r.metric, r.statistic, r.raw_value, r.value, r.unit) for r in collected.results], [
            ("EM", None, "0.330", "0.330", "fraction"),
            ("F1", "mean", "0.437", "0.437", "fraction"),
            ("F1", "sem", ".020", "0.020", "fraction"),
        ])
        mean, sem = collected.results[1:]
        self.assertEqual(mean.source, sem.source)
        self.assertEqual(sem.source.column, 5)
        self.assertEqual(sem.condition_sources, {"generator": ("group-gpt",), "dataset": ("caption",)})

    def test_d014_unreadable_or_negative_sem_preserves_mean_and_cell(self):
        table = d014_table()
        base = replace(table.cells[0], column=5, headers=("F1",))
        caption = f"{table.caption} We measure answer F1 (and its standard error mean)."
        for error in ("?", "-.020"):
            with self.subTest(error=error):
                cell = replace(base, text=f"0.437 (± {error})")
                collected = collect_results(replace(table, cells=(cell,), caption=caption))

                mean, = collected.results
                self.assertEqual((mean.statistic, mean.value), ("mean", "0.437"))
                remark, = (remark for remark in collected.remarks if remark.source == mean.source)
                self.assertEqual((remark.reason, remark.raw_text), ("Не удалось прочитать SEM.", cell.text))

    def test_readable_number_with_incomplete_header_path_needs_review(self):
        table = ReadTable(
            page=6,
            number=1,
            caption="Table 1: Main results of multi-hop QA datasets.",
            context="",
            cells=(ReadCell(18, 7, "ThreadRAG", ("HotpotQA", "EM"), ".586"),),
        )

        collected = collect_results(table)

        self.assertEqual(collected.results, ())
        self.assertEqual(len(collected.remarks), 1)
        self.assertEqual(collected.remarks[0].raw_text, ".586")
        self.assertEqual(collected.remarks[0].source.column, 7)

    def test_known_experiment_keeps_result_with_missing_protocol(self):
        table = ReadTable(
            page=6,
            number=1,
            caption="Table 1: Main results of multi-hop QA datasets.",
            context="Datasets: HotpotQA (Yang et al., 2018).\n"
            "We use Qwen-3.5-Plus (Yang et al., 2025) with nomic-embed-text for embeddings.",
            cells=(ReadCell(18, 7, "ThreadRAG", ("Qwen3.5 plus", "HotpotQA", "EM"), ".586"),),
        )

        collected = collect_results(table)

        self.assertEqual(len(collected.results), 1)
        self.assertIsNone(collected.results[0].protocol)
        self.assertIsNone(collected.results[0].unit)
        self.assertEqual(collected.remarks[0].source.table, 1)


def labeled_table(headers: tuple[str, ...]) -> ReadTable:
    return ReadTable(
        5, 1, "Table 1: Results on multi-hop QA benchmarks.", "For evaluation, we report Exact Match (EM).",
        (ReadCell(3, 2, "Self-RAG", headers, ".422",
                  header_evidence_ids=("header-dataset", "header-model", "header-metric")),),
    )


def d014_table() -> ReadTable:
    return ReadTable(
        page=4,
        number=2,
        caption="Table 2: HotpotQA answer generation performance benchmarked by different LLM generators.",
        context="HotpotQA is a question-answering dataset derived from Wikipedia knowledge. "
        "The multi-hop questions and answers are first directly derived from gold knowledge graphs.",
        cells=(
            ReadCell(2, 4, "No knowledge", ("EM",), "0.330", row_group=RowGroup("GPT-4o-mini", "group-gpt")),
            ReadCell(6, 4, "No knowledge", ("EM",), "0.200", row_group=RowGroup("LLaMA 3.1 8B", "group-llama")),
            ReadCell(10, 4, "No knowledge", ("EM",), "0.208", row_group=RowGroup("Mistral 7B Instruct", "group-mistral")),
        ),
        caption_evidence_id="caption",
    )


def d013_table(value: str) -> ReadTable:
    return ReadTable(
        page=6,
        number=2,
        caption="Table 2: Main OAS comparison with Qwen2.5-3B-Instruct. Exact-match percentages are "
        "reported as mean (sample SD) over two runs (n = 2).",
        context="The experiments cover open-domain question answering benchmarks.",
        cells=(ReadCell(9, 9, "Ours (OAS)", ("Top-k = 5", "HotpotQA"), value),),
    )


if __name__ == "__main__":
    unittest.main()
