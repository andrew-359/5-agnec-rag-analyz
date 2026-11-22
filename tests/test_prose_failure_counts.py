import json
import os
import subprocess
import sys
import unittest
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory

from rag_mvp.article_context import read_article_fragments


ROOT = Path(__file__).resolve().parents[1]
CORPUS = Path(os.environ.get("RAG_DEV_CORPUS", ROOT / "docs/context/RAG_ВИНИТИ_участникам_2026-11-20"))
D015 = CORPUS / "dev/D015.pdf"


class ProseFailureCountsTest(unittest.TestCase):
    def run_cli(self, pdf, saved, output, *options):
        command = [sys.executable, "-m", "rag_mvp.prose_results", str(pdf),
                   "--article-text", str(saved), "--failure-counts", "--out", str(output), *options]
        process = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(process.returncode, 0, process.stderr)
        return json.loads(output.read_text(encoding="utf-8"))

    def saved_passage(self, work, *paragraphs):
        pdf = work / "version.pdf"
        pdf.write_bytes(b"%PDF-1.7\n% Addressable saved-input CLI fixture")
        saved = work / "article-text.json"
        document = {
            "schema_version": 1, "document": pdf.name, "sha256": sha256(pdf.read_bytes()).hexdigest(),
            "pages": [1], "engine_version": "fixture",
            "fragments": [
                {"id": f"passage-{index}", "page": 1, "text": paragraph,
                 "precision": "text_fragment", "box": [.1, index * .1, .8, index * .1 + .08]}
                for index, paragraph in enumerate(paragraphs, 1)
            ],
        }
        saved.write_text(json.dumps(document), encoding="utf-8")
        return pdf, saved, document

    def test_incomplete_categories_leave_a_source_anchored_remark(self):
        with TemporaryDirectory() as directory:
            work = Path(directory)
            pdf, saved, _ = self.saved_passage(work, "Upon manual review of the 12 failed cases, "
                                              "we find: (i) 4 are correct; (ii) 8 are")
            artifact = self.run_cli(pdf, saved, work / "observations.json")

        self.assertEqual(artifact["results"], [])
        remark, = artifact["remarks"]
        self.assertEqual(remark["kind"], "ambiguous-binding")
        citation, = [item for item in artifact["evidence"] if item["id"] in remark["source"]["evidence_ids"]]
        self.assertIn("12 failed cases", citation["text"])

    @unittest.skipUnless(D015.is_file(), "Нужен исходный D015 из комплекта участника.")
    def test_d015_counts_reviewed_human_responses_and_cites_the_column_continuation(self):
        with TemporaryDirectory() as directory:
            work = Path(directory)
            if path := os.environ.get("RAG_D015_ARTICLE_TEXT"):
                saved = Path(path)
            else:
                saved = work / "article-text.json"
                text = read_article_fragments(D015, [15])
                saved.write_text(json.dumps(asdict(text)), encoding="utf-8")
            artifact = self.run_cli(D015, saved, work / "observations.json")
            document = json.loads(saved.read_text(encoding="utf-8"))

        rows = [row for row in artifact["results"] if row["metric"] == "failure count"]
        self.assertEqual([(row["value"], row["sample_size"], row["original_sample_size"]) for row in rows],
                         [("4", 30, 200), ("10", 30, 200), ("16", 30, 200)])
        self.assertIn("human answers are correct but incorrectly judged", rows[0]["metric_variant"])
        self.assertIn("genuinely incorrect, with the reference answers being valid", rows[1]["metric_variant"])
        self.assertIn("ambiguity or poor question quality", rows[2]["metric_variant"])

        available = {item["id"]: item for item in artifact["evidence"]}
        fragments = {item["id"]: item for item in document["fragments"]}
        for row in rows:
            count, = (available[ref] for ref in row["source"]["evidence_ids"])
            scopes = row["condition_sources"]
            category = [available[ref] for ref in scopes["metric_variant"]]
            sample, = (available[ref] for ref in scopes["sample_size"])
            original, = (available[ref] for ref in scopes["original_sample_size"])
            self.assertEqual(count["text"], row["value"])
            self.assertIn("30 failed cases", " ".join(sample["text"].split()))
            self.assertIn("200 questions", " ".join(original["text"].split()))
            for quote in [count, *category, sample, original]:
                fragment = fragments[quote["fragment_id"]]
                start, end = quote["text_range"]
                self.assertEqual(quote["text"], fragment["text"][start:end])
                self.assertEqual((quote["page"], quote["box"]), (15, fragment["box"]))
        self.assertGreater(available[rows[2]["condition_sources"]["metric_variant"][0]]["box"][0], .5)
        category = [available[ref] for ref in rows[1]["condition_sources"]["metric_variant"]]
        self.assertLess(category[0]["box"][0], .5)
        self.assertGreater(category[-1]["box"][0], .5)

    def table_results(self, document, rows):
        heading, sample = document["fragments"]
        sample_quote = "on the Example dataset. Specifically, we randomly sampled 12 incorrect prediction cases"

        def located(fragment, snippet, ref):
            start = fragment["text"].index(snippet)
            return {**fragment, "id": ref, "fragment_id": fragment["id"],
                    "text_range": [start, start + len(snippet)], "text": snippet}

        evidence = [
            located(heading, "A Failure analysis", "table-experiment"),
            located(sample, sample_quote, "table-sample"),
            located(sample, "other 12 incorrect prediction cases", "other-sample"),
        ]
        results = []
        for index, (value, category, changes) in enumerate(rows, 1):
            cell = {"id": f"number-{index}", "page": 2, "text": value,
                    "precision": "cell", "box": [.6, index * .1, .7, index * .1 + .05]}
            evidence.append(cell)
            results.append({
                "id": f"result-{index}", "metric": "failure count", "unit": "cases",
                "metric_variant": category, "experiment": "Failure analysis", "dataset": "Example", "method": None,
                "sample_size": 12, "raw_value": value, "value": value,
                "source": {"page": 2, "table": 1, "row": index, "column": 2,
                           "headers": [category, "Cases"], "box": cell["box"], "evidence_ids": [cell["id"]]},
                "condition_sources": {"experiment": ["table-experiment"], "sample_size": ["table-sample"]},
                **changes,
            })
        return {"source": {"sha256": document["sha256"]}, "page": 2, "table": 1,
                "results": results, "evidence": evidence}

    def reconciliation_fixture(self, work):
        pdf, saved, document = self.saved_passage(
            work, "A Failure analysis",
            "We report on the Example dataset. Specifically, we randomly sampled 12 incorrect prediction cases.\n"
            "• Retrieval Failure (4 cases): Missed documents.\n"
            "• Analysis Error (8 cases): Incorrect reasoning.\n"
            "A different analysis used other 12 incorrect prediction cases.",
        )
        table = work / "table-run.json"
        table.write_text(json.dumps(self.table_results(document, [
            ("4", "Retrieval Failure", {}),
            ("7", "Analysis Error", {}),
            ("4", "Retrieval Failure", {"sample_size": 40}),
            ("4", "Retrieval Failure", {"experiment": "Replicated experiment"}),
            ("4", "Retrieval Failure", {"condition_sources": {
                "experiment": ["table-experiment"], "sample_size": ["other-sample"],
            }}),
            ("4", "Retrieval Failure", {"condition_sources": {"experiment": ["table-experiment"]}}),
        ])), encoding="utf-8")
        return pdf, saved, table

    def test_table_repeat_conflict_and_distinct_scopes_are_visible_in_the_cli_artifact(self):
        with TemporaryDirectory() as directory:
            work = Path(directory)
            pdf, saved, table = self.reconciliation_fixture(work)
            artifact = self.run_cli(pdf, saved, work / "observations.json", "--table-run", str(table))

        rows = artifact["results"]
        prose = [row for row in rows if row["source"]["table"] is None]
        repeat, = (row for row in prose if row["metric_variant"] == "Retrieval Failure")
        cell, = repeat["additional_sources"]
        self.assertEqual((cell["page"], cell["table"], cell["row"]), (2, 1, 1))
        self.assertEqual(len(rows), 7)
        separate_4s = [row for row in rows if row["source"]["table"] == 1 and row["value"] == "4"]
        self.assertEqual([(row["experiment"], row["sample_size"]) for row in separate_4s],
                         [("Failure analysis", 40), ("Replicated experiment", 12),
                          ("Failure analysis", 12), ("Failure analysis", 12)])
        ambiguous = [remark["source"]["row"] for remark in artifact["remarks"]
                     if remark["kind"] == "ambiguous-binding"]
        self.assertEqual(ambiguous, [5, 6])
        conflict, = (remark for remark in artifact["remarks"] if remark["kind"] == "reading-conflict")
        self.assertIn("выборк", conflict["reason"])
        by_id = {row["id"]: row for row in rows}
        self.assertEqual([by_id[ref]["value"] for ref in conflict["result_ids"]], ["8", "7"])
        available = {item["id"]: item for item in artifact["evidence"]}
        self.assertEqual([available[ref]["text"] for ref in cell["evidence_ids"]], ["4"])
        for option in conflict["options"]:
            citation, = (available[ref] for ref in option["evidence_ids"])
            self.assertIn(option["value"], citation["text"])
