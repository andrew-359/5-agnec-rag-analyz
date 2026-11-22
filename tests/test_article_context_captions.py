import os
import unittest
from dataclasses import replace
from pathlib import Path

from rag_mvp.article_context import read_article_fragments
from rag_mvp.article_context.assertions import extract_table_assertions
from rag_mvp.article_context.context import find_table_context


@unittest.skipUnless(os.environ.get("RAG_DEV_CORPUS"), "Нужен комплект участника в RAG_DEV_CORPUS.")
class CaptionConditionsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.document = read_article_fragments(Path(os.environ["RAG_DEV_CORPUS"]) / "dev/D007.pdf")

    def test_caption_and_self_rag_exception_have_separate_sources(self):
        for table, page, expected_model, exception_model in (
            ("2", 7, "FLAN-T5-XL (3B)", "LLaMA2 (7B)"),
            ("7", 15, "FLAN-T5-XXL (11B)", "LLaMA2 (13B)"),
            ("8", 15, "GPT-3.5 (Turbo)", "LLaMA2 (13B)"),
        ):
            with self.subTest(table=table):
                context = find_table_context(self.document, table, page)
                self.assertEqual(context.status, "supported")
                candidate, = context.candidates
                result = extract_table_assertions(context.sha256, table, page, candidate.fragments, candidate.basis)
                default, exception = result.assertions
                self.assertEqual((default.scope_kind, default.value, default.excluded_methods),
                                 ("table_default", expected_model, ("Self-RAG",)))
                self.assertEqual((exception.scope_kind, exception.scope, exception.value),
                                 ("method_exception", "Self-RAG", exception_model))
                sources = {item.id: item for item in result.evidence}
                self.assertEqual(sources[default.evidence_id].page, page)
                self.assertEqual(sources[exception.evidence_id].page, 6)
                self.assertIn(exception.evidence_id, default.supporting_evidence_ids)
                for source_id in (*default.supporting_evidence_ids, *exception.supporting_evidence_ids):
                    self.assertIn(source_id, sources)
                if table != "2":
                    anchor, = exception.supporting_evidence_ids
                    self.assertEqual(sources[anchor].page, 7)

    def test_others_needs_the_explicit_comparison_anchor(self):
        document = replace(self.document, fragments=tuple(
            item for item in self.document.fragments if not item.text.startswith("Table 2:")
        ))
        context = find_table_context(document, "8", 15)
        self.assertEqual(context.status, "supported")
        candidate, = context.candidates
        result = extract_table_assertions(context.sha256, "8", 15, candidate.fragments, candidate.basis)
        default, = result.assertions
        self.assertEqual(default.scope_kind, "table_default")
        self.assertEqual(default.supporting_evidence_ids, ())

    def test_mixed_pdf_evidence_is_rejected(self):
        context = find_table_context(self.document, "7", 15)
        candidate, = context.candidates
        changed = tuple(
            replace(item, source=replace(item.source, id="0" * 12 + item.source.id[12:]))
            if item.kind == "peer_caption" else item
            for item in candidate.basis
        )
        with self.assertRaisesRegex(ValueError, "другой версии PDF"):
            extract_table_assertions(context.sha256, "7", 15, candidate.fragments, changed)
