import unittest

from entities import passport
from extract import table_records


def fragment(identifier, text, page=8):
    return {"id": identifier, "text": text, "page": page, "box": [0.1, 0.2, 0.3, 0.4]}


def measurements(grid, caption, text=(), *, table_box=None, metric_docs=None, dataset_docs=None):
    cells = [
        {"id": identifier, "text": value, "row": row, "column": column, "row_span": 1,
         "column_span": span, "evidence_ids": [identifier], "role": role}
        for identifier, value, row, column, role, span in grid
    ]
    evidence = [fragment(cell["id"], cell["text"]) for cell in cells]
    if table_box:
        evidence.append({**fragment("table", ""), "precision": "table", "box": table_box})
    table = {"page": 8, "label": "4", "cells": cells, "caption": fragment("caption", caption),
             "evidence": evidence}
    lexicon = {"Context Recall": {"other"}} if metric_docs is None else metric_docs
    entities = passport({"doc": "fixture", "text": text}, lexicon, dataset_docs or {})
    return list(table_records(table, entities, text))


def measurement(header, caption, text=()):
    return measurements([
        ("method", "Method", 0, 0, "column_header", 1), ("header", header, 0, 1, "column_header", 1),
        ("variant", "Baseline", 1, 0, "data", 1), ("value", "37.9", 1, 1, "data", 1),
    ], caption, text)[0]


class MetricSourcesTests(unittest.TestCase):
    def test_failure_count_is_a_positioned_total_with_supported_components(self):
        grid = [
            ("tester", "Test method", 0, 1, "column_header", 3),
            ("mut", "Model", 1, 0, "column_header", 1),
            ("ai", "AI", 1, 1, "column_header", 1), ("web", "Internet", 1, 2, "column_header", 1),
            ("total", "Total failures", 1, 3, "column_header", 1),
            ("model", "Model A", 2, 0, "row_header", 1),
            ("a", "307", 2, 1, "data", 1), ("b", "654", 2, 2, "data", 1), ("n", "961", 2, 3, "data", 1),
        ]
        caption = "Table 4: System under test failures"
        row, = measurements(grid, caption)
        self.assertEqual((row["column"], row["value"], row["fields"]["metric"]["value"]), (3, "961", "Total failures"))
        self.assertEqual(row["fields"]["method"]["value"], "Test method")
        self.assertEqual([s["id"] for s in row["number_sources"]], ["n"])
        self.assertEqual(measurements([*grid[:-1], ("n", "962", 2, 3, "data", 1)], caption), [])
        self.assertEqual(measurements(grid, "Table 4: Corpus composition."), [])

    def test_accuracy_in_linked_paragraph_applies_to_named_benchmark_columns(self):
        grid = [
            ("axis", "Method", 0, 0, "column_header", 1), ("benchmark", "MiniQA", 0, 1, "column_header", 1),
            ("time", "Time", 0, 2, "column_header", 1), ("method", "Base", 1, 0, "row_header", 1),
            ("score", "77.5", 1, 1, "data", 1), ("runtime", "5.6", 1, 2, "data", 1),
        ]
        caption = "Table 4: Performance comparison on MiniQA."
        source = fragment("report", "We compare how the prompt affects the accuracy. The results are reported in Table 4.")
        rows = measurements(grid, caption, [source], dataset_docs={"MiniQA": {"other"}})
        self.assertEqual([(r["column"], r["fields"]["metric"]["value"]) for r in rows], [(1, "Accuracy")])
        self.assertEqual([s["id"] for s in rows[0]["fields"]["metric"]["sources"]], ["benchmark", "report"])
        wrong = {**source, "text": source["text"].replace("Table 4", "Table 5")}
        self.assertEqual(measurements(grid, caption, [wrong], dataset_docs={"MiniQA": {"other"}}), [])

    def test_accuracy_levels_keep_separate_granularity_and_skip_model_size(self):
        grid = [
            ("method", "Model", 0, 0, "column_header", 1), ("size", "Size", 0, 1, "column_header", 1),
            ("l1", "𝐿1", 0, 2, "column_header", 1), ("l2", "𝐿2", 0, 3, "column_header", 1),
            ("l3", "𝐿3", 0, 4, "column_header", 1), ("model", "Base", 1, 0, "row_header", 1),
            ("size_value", "1.5", 1, 1, "data", 1), ("a", "78", 1, 2, "data", 1),
            ("b", "75", 1, 3, "data", 1), ("c", "73", 1, 4, "data", 1),
        ]
        caption = "Table 4: Performance on different models."
        source = fragment("report", "The results in Table 4 demonstrate the method's performance. "
                          "The results demonstrate higher diagnostic accuracy across 𝐿1, 𝐿2, and 𝐿3.")
        rows = measurements(grid, caption, [source])
        self.assertEqual([(r["column"], r["fields"]["metric"]["value"], r["metric_variant"]) for r in rows],
                         [(2, "Diagnostic accuracy", "L1"), (3, "Diagnostic accuracy", "L2"),
                          (4, "Diagnostic accuracy", "L3")])
    def test_declared_recall_cutoff_only_applies_to_retrieval_score_columns(self):
        grid = [
            ("label", "Label", 0, 0, "column_header", 1),
            ("dense", "Dense", 0, 1, "column_header", 1),
            ("best", "Best recall", 0, 2, "column_header", 1),
            ("weight", "Best strategy", 0, 3, "column_header", 1),
            ("method", "Base", 1, 0, "data", 1),
            ("m1", ".7", 1, 1, "data", 1), ("m2", ".8", 1, 2, "data", 1), ("m3", ".5", 1, 3, "data", 1),
        ]
        rows = measurements(grid, "Table 4: Recall accuracy is measured with Recall@5.")
        self.assertEqual([(row["column"], row["fields"]["metric"]["value"], row["fields"]["metric_k"]["value"])
                          for row in rows], [(1, "Recall@5", 5), (2, "Recall@5", 5)])
        self.assertEqual([item["id"] for item in rows[0]["fields"]["metric"]["sources"]], ["dense", "caption"])

    def test_caption_accuracy_aliases_follow_leaf_columns(self):
        grid = [
            ("group", "Model A", 0, 1, "column_header", 3),
            ("bbx", "bbx", 1, 1, "column_header", 1), ("ans", "ans", 1, 2, "column_header", 1),
            ("size", "Size", 1, 3, "column_header", 1),
            ("method", "Base", 2, 0, "data", 1),
            ("m1", "1.5", 2, 1, "data", 1), ("m2", "60.4", 2, 2, "data", 1), ("m3", "12", 2, 3, "data", 1),
        ]
        rows = measurements(grid, "Table 4: Bounding box accuracy (bbx) and answer accuracy (ans).")
        self.assertEqual([(r["column"], r["fields"]["metric"]["value"]) for r in rows],
                         [(1, "Bounding box accuracy"), (2, "Answer accuracy")])
        self.assertEqual([item["id"] for item in rows[1]["fields"]["metric"]["sources"]], ["ans", "caption"])
        self.assertNotIn("unit", rows[1]["fields"])

    def test_composite_header_scopes_the_printed_dataset_and_metric(self):
        row = measurement("Mini-Corpus (str-em)", "Table 4: Benchmark results.")
        for field, value in (("dataset", "Mini-Corpus"), ("metric", "STR-EM")):
            self.assertEqual(row["fields"][field]["value"], value)
            self.assertEqual([item["id"] for item in row["fields"][field]["sources"]], ["header"])
        aggregate = measurement("Overall (acc)", "Table 4: Benchmark results.")
        self.assertEqual(aggregate["fields"]["metric"]["value"], "Accuracy")
        self.assertNotIn("dataset", aggregate["fields"])

    def test_winning_rate_keeps_aspect_and_excludes_unrelated_rows(self):
        grid = [
            ("aspect", "Aspect", 0, 0, "column_header", 1),
            ("group", "Domain", 0, 1, "column_header", 1),
            ("diversity", "Diversity", 1, 0, "data", 1), ("m1", "57.9", 1, 1, "data", 1),
            ("samples", "Samples", 2, 0, "data", 1), ("m2", "500", 2, 1, "data", 1),
        ]
        row, = measurements(grid, "Table 4: Main results: winning rates (%) across datasets.")
        self.assertEqual((row["fields"]["metric"]["value"], row["metric_variant"]), ("Winning rate", "Diversity"))
        self.assertEqual([item["id"] for item in row["fields"]["metric"]["sources"]],
                         ["diversity", "group", "caption"])

    def test_caption_indicators_keep_caption_as_witness(self):
        cases = (
            ("ACC (%)", "Accuracy", "%"), ("Accuracy (%)", "Accuracy", "%"),
            ("winning rates (%)", "Winning rate", "%"), ("refusal rate", "Refusal rate", None),
            ("refusal rate (%)", "Refusal rate", "%"), ("compliance rate (%)", "Compliance rate", "%"),
            ("wall-clock runtime. Runtime is reported in rounded minutes", "Wall-clock runtime", "minutes"),
        )
        for text, metric, unit in cases:
            with self.subTest(text=text):
                fields = measurement("Overall", "Table 4: We report " + text + ".")["fields"]
                self.assertEqual(fields["metric"]["value"], metric)
                self.assertEqual([s["id"] for s in fields["metric"]["sources"]], ["caption"])
                self.assertEqual(fields.get("unit", {}).get("value"), unit)

    def test_embedded_and_prefixed_metrics_keep_the_printed_header(self):
        cases = (
            ("D-Acc", "D-Acc"), ("C-Pre", "C-Pre"), ("D-Rec", "D-Rec"), ("C-F1", "C-F1"),
            ("Accuracy @ 0.85", "Accuracy@0.85"), ("@0.95 Accuracy", "Accuracy@0.95"),
            ("Best recall", "Best recall"), ("Cosine Sim Avg", "Cosine similarity"),
            ("Cosine Similarity", "Cosine similarity"), ("BLEU-4", "BLEU-4"),
            ("Average ROUGE-2 score", "ROUGE-2"), ("ROUGE Prompts", "ROUGE Prompts"),
            ("ROUGE Contexts", "ROUGE Contexts"),
            ("Overall F1", "F1"), ("Document Recall @ 5", "Recall@5"),
        )
        for header, metric in cases:
            with self.subTest(header=header):
                fields = measurement(header, "Table 4: Experimental results.")["fields"]
                self.assertEqual(fields["metric"]["value"], metric)
                self.assertEqual([s["id"] for s in fields["metric"]["sources"]], ["header"])
                if metric.startswith("Accuracy@"):
                    self.assertNotIn("metric_k", fields)

    def test_named_evaluation_headers_keep_their_own_scale(self):
        cases = (
            ("IN-F1", "IN-F1", None), ("Coverage (%)", "Coverage", "%"),
            ("Correctness (1: worst, 10: best)", "Correctness", None),
            ("Semantic Similarity (smaller is better)", "Semantic Similarity", None),
            ("EngWinRate", "EngWinRate", None), ("Textual RAG Score", "Textual RAG Score", None),
        )
        for header, metric, unit in cases:
            with self.subTest(header=header):
                fields = measurement(header, "Table 4: Evaluation results.")["fields"]
                self.assertEqual(fields["metric"]["value"], metric)
                self.assertEqual([item["id"] for item in fields["metric"]["sources"]], ["header"])
                self.assertEqual(fields.get("unit", {}).get("value"), unit)

    def test_generic_score_and_correlation_require_their_own_caption(self):
        def grid(header):
            return [
                ("method", "Method", 0, 0, "column_header", 1), ("header", header, 0, 1, "column_header", 1),
                ("variant", "Baseline", 1, 0, "data", 1), ("number", "37.9", 1, 1, "data", 1),
            ]

        for header in ("Score", "Interest", "SamplingRate", "Documents", "QA pairs"):
            self.assertEqual(measurements(grid(header), "Table 4: Experimental results."), [])
        score = measurement("Score", "Table 4: Overall performance scores for each method out of 200 points.")["fields"]
        self.assertEqual(score["metric"]["value"], "Overall performance score")
        self.assertEqual(score["unit"]["value"], "points")
        self.assertEqual([item["id"] for item in score["unit"]["sources"]], ["caption"])
        self.assertEqual([item["id"] for item in score["metric"]["sources"]], ["header", "caption"])
        correlation = measurement("Interest", "Table 4: Correlation between LLM scores and hu-\nman scores.")["fields"]
        self.assertEqual(correlation["metric"]["value"], "Interest correlation")
        self.assertEqual([item["id"] for item in correlation["metric"]["sources"]], ["header", "caption"])
        self.assertNotIn("unit", correlation)

    def test_relevance_requires_positioned_evaluation_headers_at_its_level(self):
        grid = [
            ("method", "Model", 0, 0, "column_header", 1),
            ("relevance", "Relevance", 0, 1, "column_header", 1),
            ("coverage", "Coverage", 0, 2, "column_header", 1),
            ("rate", "UnsafeRate", 0, 3, "column_header", 1),
            ("variant", "Baseline", 1, 0, "data", 1),
            ("value", "0.69", 1, 1, "data", 1),
        ]
        caption = "Table 4: Offline evaluation."
        self.assertEqual(measurements([cell for cell in grid if cell[0] != "rate"], caption), [])
        metric = measurements(grid, caption)[0]["fields"]["metric"]
        self.assertEqual(metric["value"], "Relevance")
        self.assertEqual([source["id"] for source in metric["sources"]], ["relevance", "coverage", "rate"])

    def test_ordered_caption_aliases_bind_only_their_columns(self):
        grid = [
            ("method", "Method", 0, 0, "column_header", 1),
            ("h1", "B-1", 0, 1, "column_header", 1), ("h2", "ROU.", 0, 2, "column_header", 1),
            ("variant", "Baseline", 1, 0, "data", 1),
            ("v1", "0.5022", 1, 1, "data", 1), ("v2", "0.5654", 1, 2, "data", 1),
        ]
        title = "Table 4 Main experimental results. B-1 and ROU. represent BLEU-1 and ROUGE-L"
        self.assertEqual(measurements(grid, title + "."), [])
        self.assertEqual(measurements(grid, title + " and METEOR, respectively."), [])
        rows = measurements(grid, title + ", respectively.")
        self.assertEqual(len(rows), 2)
        for row, name, header in zip(rows, ("BLEU-1", "ROUGE-L"), ("h1", "h2"), strict=True):
            metric = row["fields"]["metric"]
            self.assertEqual(metric["value"], name)
            self.assertEqual([item["id"] for item in metric["sources"]], [header, "caption"])

    def test_table_reference_requires_one_explicit_metric_in_its_own_sentence(self):
        grid = [
            ("method", "Method", 0, 0, "column_header", 1),
            ("k5", "@5", 0, 1, "column_header", 1),
            ("k10", "@10", 0, 2, "column_header", 1),
            ("variant", "Baseline", 1, 0, "data", 1),
            ("value5", "37.9", 1, 1, "data", 1),
            ("value10", "41.4", 1, 2, "data", 1),
        ]
        caption = "Table 4: Results across settings."
        examples = (
            "We report Accuracy in Table 40.",
            "Table 4 reports Accuracy and F1.",
            "We compute Accuracy. Results are reported in Table 4.",
            "Table 4 reports metrics. Table 5 shows Accuracy.",
            "Table 4 shows Accuracy. We report F1 in Table 4.",
        )
        for text in examples:
            with self.subTest(text=text):
                self.assertEqual(measurements(grid, caption, [fragment("body", text)]), [])
        rows = measurements(grid, caption, [fragment("body", "We report Accuracy in Table 4.")])
        self.assertEqual(len(rows), 2)
        for row in rows:
            metric = row["fields"]["metric"]
            self.assertEqual((metric["value"], metric["rule"]), ("Accuracy", "table-reference-metric"))
            self.assertEqual([s["id"] for s in metric["sources"]], ["body"])
            self.assertNotIn("metric_k", row["fields"])

    def test_one_caption_rate_skips_an_ancillary_time_column(self):
        grid = [
            ("rate", "Overall", 0, 1, "column_header", 1),
            ("time", "Time-C", 0, 2, "column_header", 1),
            ("variant", "Baseline", 1, 0, "data", 1),
            ("rate_value", "58.5", 1, 1, "data", 1),
            ("time_value", "1.2", 1, 2, "data", 1),
        ]
        rows = measurements(grid, "Table 4: Winning rates (%). Time-C (hours) denotes build time.")
        self.assertEqual([(r["column"], r["fields"]["metric"]["value"]) for r in rows], [(1, "Winning rate")])
        self.assertEqual(measurements(grid, "Table 4: Statistics for the corpora. Winning rates (%)."), [])

    def test_counters_models_and_methods_are_not_measurement_labels(self):
        grid = [
            ("method", "Method", 0, 0, "column_header", 1), ("header", "", 0, 1, "column_header", 1),
            ("variant", "Baseline", 1, 0, "data", 1), ("value", "37.9", 1, 1, "data", 1),
        ]
        counts = ("Documents", "QA pairs", "Queries", "Samples")
        terminology = [fragment("llm", "Large Language Model (LLM)"),
                       fragment("kmw", "Knowledge-Infused Multi-Bit Watermarking (KMW)"),
                       fragment("wdr", "Watermark Detection Rate (WDR)")]
        lexicon = {name: {"other"} for name in (*counts, "Large Language Model", "Watermarking")}
        for term in (*counts, "LLM", "KMW", "Watermarking", "Large Language Model"):
            with self.subTest(header=term):
                grid[1] = ("header", term, 0, 1, "column_header", 1)
                self.assertEqual(measurements(grid, "Table 4: Comparison.", terminology, metric_docs=lexicon), [])
        grid[1] = ("header", "Score by group", 0, 1, "column_header", 1)
        self.assertEqual(measurements(
            grid, "Table 4: Each model uses the same number of QA pairs.", terminology, metric_docs=lexicon,
        ), [])
        grid[1] = ("header", "WDR", 0, 1, "column_header", 1)
        row, = measurements(grid, "Table 4: Comparison.", terminology, metric_docs=lexicon)
        self.assertEqual(row["fields"]["metric"]["value"], "Watermark Detection Rate")
        self.assertEqual([item["id"] for item in row["fields"]["metric"]["sources"]], ["header", "wdr"])

    def test_filtered_label_does_not_unlock_a_generic_caption_metric(self):
        text = [fragment("domain", "Domain categories include Legal Documents (Law).")]
        lexicon = {"Documents": {"other"}}
        grid = [
            ("method", "Method", 0, 0, "column_header", 1), ("header", "Law", 0, 1, "column_header", 1),
            ("variant", "Baseline", 1, 0, "data", 1), ("value", "37.9", 1, 1, "data", 1),
        ]
        caption = "Table 4: Accuracy (%) on the benchmark."
        self.assertEqual(measurements(grid, caption, text, metric_docs=lexicon), [])
        grid[1] = ("header", "Domain", 0, 1, "column_header", 1)
        self.assertEqual(measurements(
            grid, caption + " Domain categories include Legal Documents (Law).", text, metric_docs=lexicon,
        ), [])
        row, = measurements(grid, caption, text, metric_docs=lexicon)
        self.assertEqual(row["fields"]["metric"]["value"], "Accuracy")

    def test_reported_abbreviation_keeps_header_caption_and_definition(self):
        text = [fragment("definition", "Context Recall (ConR)")]
        row = measurement("ConR ↑", "Table 4: We report ConR (↑) in all groups.", text)
        self.assertEqual(row["fields"]["metric"]["value"], "ConR")
        self.assertEqual([item["id"] for item in row["fields"]["metric"]["sources"]],
                         ["header", "caption", "definition"])
        self.assertEqual(row["number_sources"], [fragment("value", "37.9")])
        row = measurement("ConR ↑", "Table 4: Group comparison.", text)
        self.assertEqual(row["fields"]["metric"]["value"], "Context Recall")

    def test_answer_accuracy_requires_a_definition_and_link_to_this_table(self):
        caption = "Table 4: Complete evaluation results. Values represent accuracy scores."
        definition = fragment("definition", "We used accuracy—based on whether the response "
                              "matched the correct answer—to evaluate the system.")
        link = fragment("report", "The evaluation results are shown in Tab. 4.")
        row = measurement("Overall (561)", caption, [definition, link])
        self.assertEqual(row["fields"]["metric"]["value"], "Answer accuracy")
        self.assertEqual([item["id"] for item in row["fields"]["metric"]["sources"]],
                         ["caption", "definition", "report"])
        self.assertNotIn("unit", row["fields"])

        for text in ([definition], [link], [definition, {**link, "text": "Results are shown in Table 40."}],
                     [definition, {**link, "page": 9}], [definition, {**definition, "id": "other"}, link]):
            with self.subTest(text=text):
                row = measurement("Overall (561)", caption, text)
                self.assertEqual(row["fields"]["metric"]["value"], "Accuracy")

        row = measurement("Recall@10", caption, [definition, link])
        self.assertEqual(row["fields"]["metric"]["value"], "Recall@10")
        self.assertEqual(row["fields"]["metric_k"]["value"], 10)

    def test_two_scores_in_same_header_group_keep_their_own_cells(self):
        caption = ("Table 4: Accuracy (%) and GPT-Score (%) on the benchmark. "
                   "Acc is calculated by the verification method. Score is verified by an LLM.")
        text = [fragment("protocol", "We report official rule-based accuracy (Acc).", page=7)]
        grid = [
            ("group", "Overall", 0, 1, "column_header", 2),
            ("acc", "Acc", 1, 1, "column_header", 1), ("score", "Score", 1, 2, "column_header", 1),
            ("method", "Baseline", 2, 0, "data", 1),
            ("first", "37.9", 2, 1, "data", 1), ("second", "37.9", 2, 2, "data", 1),
        ]
        rows = measurements(grid, caption, text)
        self.assertEqual([(row["column"], row["fields"]["metric"]["value"]) for row in rows],
                         [(1, "Rule-based accuracy"), (2, "LLM Score")])
        self.assertEqual([[item["id"] for item in row["fields"]["metric"]["sources"]] for row in rows],
                         [["acc", "caption", "protocol"], ["score", "caption"]])
        self.assertEqual([row["fields"]["unit"]["value"] for row in rows], ["%", "%"])
        fallback = measurements(grid, caption, [{**text[0], "page": 4}])
        self.assertEqual([row["fields"]["metric"]["value"] for row in fallback], ["Accuracy", "Accuracy"])

    def test_declared_row_metrics_stay_in_their_column_blocks(self):
        text = [
            fragment("experiment", "We evaluate Watermark Recovery Rate (WRR) with Large Language Model (LLM)."),
            fragment("details", "E.2 Metrics Details\nWatermark Recovery Rate (WRR): recovered bits.\n"
                                "Knowledge Correct Rate (KCR): correct items."),
            fragment("other", "E.3 Implementation\nLarge Language Model (LLM): the system tested."),
        ]
        header = "column_header"
        grid = [
            ("dataset", "Dataset", 0, 0, header, 1), ("validity", "Validity Metrics", 0, 1, header, 1),
            ("llm", "LLM", 0, 2, header, 2), ("quality", "Quality Metrics", 0, 4, header, 1),
            ("scheme", "Scheme", 0, 5, header, 1), ("wrr", "WRR", 1, 1, "data", 1),
            ("valid-a", "1.00", 1, 2, "data", 1), ("valid-b", "0.52", 1, 3, "data", 1),
            ("kcr", "KCR↑", 1, 4, "data", 1), ("quality-a", "0.89", 1, 5, "data", 1),
            ("stray", "LLM", 2, 1, "data", 1), ("unbound", "0.60", 2, 2, "data", 1),
        ]
        rows = measurements(grid, "Table 4: Results across datasets and schemes.", text)
        self.assertEqual([(row["column"], row["fields"]["metric"]["value"]) for row in rows],
                         [(2, "WRR"), (3, "WRR"), (5, "KCR")])
        self.assertEqual([item["id"] for item in rows[0]["fields"]["metric"]["sources"]],
                         ["wrr", "validity", "llm", "details"])

    def test_exact_match_summary_keeps_pair_statistics_and_one_cell(self):
        grid = [
            ("method", "Method", 0, 0, "column_header", 1),
            ("dataset", "Dataset A", 0, 1, "column_header", 1),
            ("label", "Baseline", 1, 0, "data", 1),
            ("pair", "24.5 (.29)", 1, 1, "data", 1),
        ]
        caption = "Table 4: Exact-match percentages are reported as mean (sample SD) over two runs."
        rows = measurements(grid, caption)
        self.assertEqual([(row["fields"]["metric"]["value"], row["value"]) for row in rows],
                         [("EM", "24.5"), ("EM sample SD", ".29")])
        self.assertEqual([row["fields"]["unit"]["value"] for row in rows], ["%", "%"])
        self.assertEqual([row["number_sources"] for row in rows], [[fragment("pair", "24.5 (.29)")]] * 2)
        self.assertEqual(measurements(grid, "Table 4: Parenthetical statistics."), [])

    def test_ranked_score_uses_only_the_same_tables_caption(self):
        grid = [
            ("method", "Method", 0, 0, "column_header", 1),
            ("dataset", "Dataset A", 0, 1, "column_header", 1),
            ("label", "Baseline", 1, 0, "data", 1),
            ("value", ".4554", 1, 1, "data", 1),
        ]
        marker = fragment("marker", "Table 4")
        text = "NDCG@5 scores for retrieval across datasets."
        continuation = {**fragment("continuation", text), "box": [.1, .405, .9, .45]}
        box = [.1, .46, .9, .8]
        row, = measurements(grid, "Table 4", [marker, continuation], table_box=box)
        self.assertEqual(row["fields"]["metric"]["value"], "NDCG@5")
        self.assertEqual(row["fields"]["metric_k"]["value"], 5)
        self.assertEqual([item["id"] for item in row["fields"]["metric"]["sources"]],
                         ["caption", "continuation"])
        row, = measurements(grid, "Table 4: " + text)
        self.assertEqual(row["fields"]["metric"]["value"], "NDCG@5")
        for other in ({**continuation, "page": 9},
                      {**continuation, "box": [.2, .405, .9, .45]},
                      {**continuation, "box": [.1, .405, .9, .47]},
                      {**continuation, "text": "Table 5: " + text},
                      {**continuation, "text": text + " Recall@10 scores for ranking."}):
            with self.subTest(other=other):
                self.assertEqual(measurements(grid, "Table 4", [marker, other], table_box=box), [])

    def test_percent_rate_names_follow_declared_column_groups(self):
        caption = ("Table 4: All values are failure rates (%). "
                   "LEAK, STALE, and UNSUP. denote unauthorized leakage, stale citation, and "
                   "unsupported-answer rates, respectively.")
        header = "column_header"
        grid = [
            ("model", "Qwen-Max", 0, 1, header, 3),
            ("leak", "Leak", 1, 1, header, 1), ("stale", "Stale", 1, 2, header, 1),
            ("unsup", "Unsup.", 1, 3, header, 1),
            ("method", "Baseline", 2, 0, "data", 1),
            ("first", "37.50", 2, 1, "data", 1), ("second", "0.00", 2, 2, "data", 1),
            ("third", "0.00", 2, 3, "data", 1),
        ]
        rows = measurements(grid, caption)
        self.assertEqual([(row["fields"]["metric"]["value"], row["value"]) for row in rows],
                         [("Leakage rate", "37.50"), ("Stale citation rate", "0.00"),
                          ("Unsupported answer rate", "0.00")])
        self.assertEqual([row["fields"]["unit"]["value"] for row in rows], ["%"] * 3)
        self.assertEqual([item["id"] for item in rows[2]["fields"]["metric"]["sources"]],
                         ["unsup", "model", "caption"])
        self.assertEqual(measurements(grid[1:], caption), [])
        compound_grid = [*grid[:-1], ("third", "0.00–2.00", 2, 3, "data", 1)]
        self.assertEqual(len(measurements(compound_grid, caption)), 2)


if __name__ == "__main__":
    unittest.main()
