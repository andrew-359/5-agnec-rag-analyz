import unittest

from scripts.submission_quotes import pdf_spelling


class SubmissionQuoteTests(unittest.TestCase):
    def test_restores_the_same_mark_without_the_extractor_space(self):
        cases = (("76.17 †", "76.17†"), ("0.403 *", "0.403*"),
                 ("Graph Transformer ∗", "Graph Transformer∗"))
        for extracted, printed in cases:
            with self.subTest(extracted=extracted):
                self.assertEqual(pdf_spelling(extracted, [f"before {printed} after"], str.strip), printed)

    def test_restores_the_printed_minus_without_changing_its_meaning(self):
        self.assertEqual(pdf_spelling("-5.5", ["before −5.5 after"], str.strip), "−5.5")
        self.assertEqual(pdf_spelling("78.2 (-4.5)", ["78.2 (−4.5)"], str.strip), "78.2 (−4.5)")
        self.assertEqual(pdf_spelling("-5.5", ["5.5 and −5.51"], str.strip), "-5.5")

    def test_does_not_replace_mark_or_match_a_larger_number(self):
        self.assertEqual(pdf_spelling("5.5 †", ["15.5† and −5.5†"], str.strip), "5.5 †")
        self.assertEqual(pdf_spelling("20.0 *", ["20.0†"], str.strip), "20.0 *")

    def test_leaves_existing_or_ambiguous_spellings_alone(self):
        self.assertEqual(pdf_spelling("A * B", ["A*B", "A * B"], str.strip), "A * B")
        self.assertEqual(pdf_spelling("A * B", ["A *B", "A* B"], str.strip), "A * B")
