import json
import unittest
from dataclasses import replace
from pathlib import Path

from rag_mvp.article_context import ArticleFragments
from rag_mvp.article_context.captions import table_caption_lines
from rag_mvp.article_context.context import find_table_context
from rag_mvp.run import Evidence


FIXTURES = Path(__file__).parent / "fixtures/article_context"


def read_fixture(name):
    data = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    data["pages"] = tuple(data["pages"])
    data["fragments"] = tuple(
        Evidence(**{**item, "box": tuple(item["box"]) if item["box"] is not None else None})
        for item in data["fragments"]
    )
    return ArticleFragments(**data)


class TableContextTest(unittest.TestCase):
    def test_bare_experimental_results_title_only_opens_a_block(self):
        caption = "Table 1 Main experimental results are presented below."
        self.assertEqual([(index, match["label"]) for index, match in table_caption_lines([caption])], [(0, "1")])
        self.assertEqual(list(table_caption_lines(["As described in the previous section,", caption])), [])
        self.assertEqual(list(table_caption_lines(["Table 1 presents the main experimental results."])), [])

    def test_russian_caption_after_grid_marks_and_reference_in_prose(self):
        document = read_fixture("d009-page-5.json")
        fragments = (
            Evidence("caption", 5, "↓ ↓\nТаблица 1 — Результаты", None, "page"),
            Evidence("reference", 5, "Описание заканчивается ссылкой на\nТаблица 1. Затем обсуждаем выводы.",
                     None, "page"),
        )
        result = find_table_context(replace(document, fragments=fragments), "Таблица 1", 5)
        self.assertEqual(result.table, "1")
        self.assertEqual(result.status, "not_found")
        self.assertIn("Подпись таблицы найдена", result.reason)
        self.assertEqual([(i, match["label"]) for i, match in table_caption_lines(
            fragments[0].text.splitlines()
        )], [(1, "1")])
        self.assertEqual(list(table_caption_lines(fragments[1].text.splitlines())), [])

    def test_nested_settings_connect_to_performance_comparison(self):
        document = read_fixture("d009-page-5.json")
        fragments = tuple(Evidence(str(i), 5, text, None, "page") for i, text in enumerate((
            "Table 2\nComparison of systems.",
            "5 Experiments",
            "5.1 Experiment Settings",
            "5.1.2 Implementation",
            "We used Llama-3 as the base model.",
            "5.2 Inference Performance Comparison\nTable 2 summarizes the main results.",
        )))
        result = find_table_context(replace(document, fragments=fragments), "2", 5)
        self.assertEqual(result.status, "supported")
        candidate, = result.candidates
        self.assertEqual(candidate.fragments, (fragments[4],))
        self.assertIn(fragments[2], [basis.source for basis in candidate.basis])

    def test_appendix_heading_alone_does_not_describe_conditions(self):
        document = read_fixture("d009-page-5.json")
        fragments = tuple(Evidence(str(i), 5, text, None, "page") for i, text in enumerate((
            "Table 2: Comparison of systems.",
            "E Experiment Settings",
            "E.3 Results\nTable 2 summarizes the main results.",
            "E.4 Implementation Details",
        )))
        result = find_table_context(replace(document, fragments=fragments), "2", 5)
        self.assertEqual(result.status, "not_found")

    def test_d009_returns_both_parts_and_traceable_basis(self):
        document = read_fixture("d009-page-5.json")
        appendix = Evidence("app", 19, "F Additional Evaluation\nWe supplement Table 1 with extra results. "
                            "Implementation Details. We use a separate protocol.", None, "page")
        document = replace(document, fragments=(*document.fragments, appendix))
        result = find_table_context(document, "Table 1", 5)
        self.assertEqual(result.status, "supported")
        self.assertEqual(result.sha256, document.sha256)
        candidate, = result.candidates
        self.assertEqual(len(candidate.fragments), 2)
        self.assertTrue(candidate.fragments[0].text.startswith("Implementation Details."))
        self.assertTrue(candidate.fragments[1].text.startswith("splits of HotpotQA"))
        by_kind = {item.kind: item.source for item in candidate.basis}
        self.assertTrue(by_kind["caption"].text.startswith("Table 1:"))
        self.assertIn("4.2 Main Results", by_kind["results_reference"].text)
        self.assertIn("4.1 Experimental Setup", by_kind["setup_section"].text)
        self.assertEqual(by_kind["experiment_section"].text, "4 Experiments")

    def test_d005_uses_the_local_experiment(self):
        document = read_fixture("d005-page-10.json")
        main = Evidence("main", 6, "5.1 Experimental Setup\nWe used Llama 3.1 as the base model.", None, "page")
        document = replace(document, fragments=(main, *document.fragments))
        result = find_table_context(document, "4", 10)
        self.assertEqual(result.status, "supported")
        candidate, = result.candidates
        self.assertIn("llama-3.3-70b-instruct", candidate.fragments[0].text)
        self.assertNotIn(main, candidate.fragments)

    def test_same_page_without_results_reference_is_ambiguous(self):
        document = read_fixture("d009-page-5.json")
        document = replace(document, fragments=tuple(
            item for item in document.fragments if not item.text.startswith("4.2 Main Results")
        ))
        result = find_table_context(document, "1", 5)
        self.assertEqual(result.status, "ambiguous")
        self.assertTrue(result.candidates)

    def test_bare_model_mention_does_not_select_context(self):
        document = read_fixture("d009-page-5.json")
        document = replace(document, fragments=(
            Evidence("caption", 5, "Table 1: Metrics", None, "page"),
            Evidence("heading", 5, "4.1 Experimental Setup", None, "page"),
            Evidence("mention", 5, "We mention Qwen in related work.", None, "page"),
        ))
        result = find_table_context(document, "1", 5)
        self.assertEqual(result.status, "not_found")
        self.assertEqual(result.candidates, ())

    def test_two_supported_sections_keep_both_candidates(self):
        document = read_fixture("d005-page-10.json")
        other = Evidence("other", 11, "B Alternative Settings\nResults are shown in Table 4. "
                         "We used another system as the generative model.", None, "page")
        document = replace(document, fragments=(*document.fragments, other))
        result = find_table_context(document, "4", 10)
        self.assertEqual(result.status, "ambiguous")
        self.assertEqual(len(result.candidates), 2)

    def test_repeated_caption_label_on_another_page_leaves_link_open(self):
        document = read_fixture("d009-page-5.json")
        repeated = Evidence("another-table", 20, "Table 1: Another experiment", None, "page")
        document = replace(document, pages=(5, 20), fragments=(*document.fragments, repeated))
        result = find_table_context(document, "1", 5)
        self.assertEqual(result.status, "ambiguous")
        self.assertIn("несколько подписей", result.reason)
        self.assertTrue(result.candidates)

    def test_complementary_paragraphs_in_one_setup_share_table_context(self):
        document = read_fixture("d009-page-5.json")
        index = next(i for i, fragment in enumerate(document.fragments)
                     if fragment.text.startswith("4.2 Main Results"))
        retrieval = Evidence("retrieval", 5, "We limit the number of retrieved passages.", None, "page")
        fragments = (*document.fragments[:index], retrieval, *document.fragments[index:])
        result = find_table_context(replace(document, fragments=fragments), "1", 5)
        self.assertEqual(result.status, "supported")
        candidate, = result.candidates
        self.assertEqual(len(candidate.fragments), 3)
        self.assertEqual(candidate.fragments[-1], retrieval)
        self.assertTrue(any(item.kind == "conditions" and item.source == retrieval for item in candidate.basis))

    def test_results_heading_can_follow_intermediate_sibling_but_not_new_setup(self):
        document = read_fixture("d009-page-5.json")
        result_index = next(i for i, fragment in enumerate(document.fragments)
                            if fragment.text.startswith("4.2 Main Results"))
        results = replace(document.fragments[result_index], text="4.3 Main Results\nTable 1 shows the main results.")
        for title, expected in (("4.2 Evaluation Metrics", "supported"),
                                ("4.2 Experimental Setup", "ambiguous")):
            with self.subTest(title=title):
                intermediate = Evidence("intermediate", 5, title, None, "page")
                fragments = (*document.fragments[:result_index], intermediate, results,
                             *document.fragments[result_index + 1:])
                result = find_table_context(replace(document, fragments=fragments), "1", 5)
                self.assertEqual(result.status, expected)
