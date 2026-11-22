import unittest

from paired_comparison import band_label, comparison_headers, parsed_statistics


class PairedComparisonTests(unittest.TestCase):
    def test_requires_a_positioned_comparison_schema_and_matching_definition(self):
        names = ("Dataset", "Failure", "Model", "Baseline % [95% CI]", "Candidate % [95% CI]",
                 "∆ pp", "McNemar p")
        headers = [{"row": 0, "column": column, "text": name, "role": "column_header"}
                   for column, name in enumerate(names)]
        title = ("Failure rates are reported with 95% bootstrap confidence intervals. "
                 "∆ pp denotes the absolute reduction from Baseline to Candidate in percentage points. "
                 "Paired McNemar tests compare both methods on the same examples.")
        self.assertEqual(comparison_headers(headers, title), (headers, "Baseline", "Candidate"))
        self.assertIsNone(comparison_headers(headers, title.replace("95% bootstrap", "90% bootstrap")))
        self.assertIsNone(comparison_headers(headers, title.replace("from Baseline to Candidate",
                                                                    "from Candidate to Baseline")))

    def test_preserves_constituents_and_validates_relationships(self):
        words = ["37.50 [32.75, 42.25]", "0.00 [0.00, 0.00]", "37.50", "1.40e-45"]
        self.assertEqual(parsed_statistics([{"text": word} for word in words]),
                         (("37.50", "32.75", "42.25"), ("0.00", "0.00", "0.00"), ("37.50",), ("1.40e-45",)))
        for column, invalid in [(0, "37.50 [42.25, 32.75]"), (2, "32.75"), (3, "1.25")]:
            with self.subTest(column=column):
                cells = [{"text": word} for word in words]
                cells[column] = {"text": invalid}
                self.assertIsNone(parsed_statistics(cells))

    def test_label_applies_to_its_geometric_band_only(self):
        first = {"text": "Leak", "box": [.2, .26, .3, .28]}
        second = {"text": "Stale", "box": [.2, .61, .3, .63]}
        labels = [first, second]
        rules = [.1, .4, .8]
        self.assertEqual(band_label(.15, rules, labels), (first, .4))
        self.assertEqual(band_label(.79, rules, labels), (second, .8))
        self.assertIsNone(band_label(.4, rules, labels))
        self.assertIsNone(band_label(.25, rules, [*labels, {**first, "text": "Unsup."}]))
