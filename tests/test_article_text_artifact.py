import json
from dataclasses import asdict
from unittest import TestCase

from rag_mvp.strategies.text_pdfplumber import decode_article_fragments, parse_text_fragment


SHA = "a" * 64
BOX = [0.1, 0.2, 0.3, 0.4]


def fragment(number, text, page=1):
    return {"id": f"{SHA[:12]}-p{page}-text-{number}", "page": page, "text": text,
            "box": BOX, "precision": "text_fragment"}


def decode(fragments, **expectations):
    payload = {"schema_version": 1, "sha256": SHA, "pages": [1, 2], "document": "sample.pdf",
               "engine_version": "test", "run_id": "sample-run", "fragments": fragments}
    return decode_article_fragments(json.dumps(payload).encode(), **expectations)


class ArticleTextArtifactTest(TestCase):
    def test_preserves_text_and_sources_around_blank_fragments(self):
        before = fragment(1, "  First line\n")
        after = {**fragment(1, "Second line", page=2), "box": [0.2, 0.3, 0.4, 0.5]}
        result = decode([before, fragment(2, " \t\n"), fragment(3, ""), after],
                        expected_sha256=SHA, expected_run_id="sample-run")

        self.assertEqual(result.pages, (1, 2))
        # Compare every saved field, converting tuples back to their JSON representation.
        self.assertEqual(json.loads(json.dumps([asdict(f) for f in result.fragments])), [before, after])

    def test_invalid_fragments_still_reject_the_artifact(self):
        before = fragment(1, "First line")
        blank = fragment(2, " \n")
        for changes in (
            {"text": None},
            {"box": [0.1, 0.2, 0.1, 0.4]},
            {"precision": "page"},
            {"id": ""},
            {"id": before["id"]},
            {"id": "b" * 12 + "-p1-text-2"},
            {"id": SHA[:12] + "-p2-text-2"},
            {"page": 3},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                decode([before, {**blank, **changes}])

        with self.assertRaises(ValueError):
            decode([before, fragment(1, "", page=2), fragment(3, "Back on page one")])
        with self.assertRaises(ValueError):
            decode([before, blank, fragment(2, "Duplicate blank ID")])
        with self.assertRaises(ValueError):
            decode([blank], expected_sha256="b" * 64)

    def test_explicit_evidence_still_requires_text(self):
        with self.assertRaises(ValueError):
            parse_text_fragment(fragment(1, " \n"))
