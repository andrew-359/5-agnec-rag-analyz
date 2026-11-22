import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


def read(path):
    return json.loads(path.read_bytes())


@unittest.skipUnless(os.getenv("RAG_CONTEXT_REPLAY_INPUTS") and os.getenv("RAG_DEV_CORPUS"),
                     "Нужны сохранённое чтение D009 и комплект участника.")
class RecordReviewCliTest(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        inputs = Path(os.environ["RAG_CONTEXT_REPLAY_INPUTS"])
        self.grid = inputs / "d009-grid.json"
        self.text = inputs / "d009-text.json"
        self.pdf = Path(os.environ["RAG_DEV_CORPUS"]) / "dev/D009.pdf"
        first = self.directory / "d009"
        self.run_cli("rag_mvp", "extract", self.pdf, "--page", 5, "--table", 1,
                     "--grid-input", self.grid, "--article-text", self.text, "--out", first)
        self.context = first / "article-context.json"
        self.report = self.directory / "original-reconciliation.json"
        self.run_cli("rag_mvp.reconciliation", "--run-id", "d009", "--pdf-sha256", read(self.text)["sha256"],
                     "--table", "table-one", first / "run.json", "--out", self.report)

    def run_cli(self, module, *args, succeeds=True):
        completed = subprocess.run(
            [sys.executable, "-m", module, *map(str, args)],
            capture_output=True, text=True, timeout=20,
        )
        if succeeds:
            self.assertEqual(completed.returncode, 0, completed.stderr)
        return completed

    def review(self, remark, fragment, directory, *, succeeds=True, text=None, embedded=False):
        return self.run_cli(
            "rag_mvp.record_review", "--reconciliation", self.report, "--artifact", "table-one",
            "--remark", remark, "--grid-input", self.grid,
            *(() if embedded else ("--context-input", self.context)),
            "--article-text", text or self.text, "--fragment", fragment, "--out", directory,
            succeeds=succeeds,
        )

    def test_flare_keeps_its_open_generator_with_the_original_evidence_and_an_additional_fragment(self):
        before = self.report.read_bytes()
        original_context = self.context.read_bytes()
        original = read(self.report)["artifacts"][0]["data"]
        remark, = [row for row in original["remarks"] if row["field"] == "generator"
                   and row["conditions"].get("method") == "FLARE" and row["source"]["headers"] == ["HotpotQA", "EM"]]
        chosen = "8fd9ce01afa9-p6-text-13"
        output = self.directory / "review"
        self.review(remark["id"], chosen, output)
        new = read(output / "reconciliation.json")
        self.assertNotEqual(new["run_id"], "d009")
        self.assertEqual(new["review"]["base_run_id"], "d009")
        self.assertEqual(new["review"]["status"], "open")
        self.assertEqual(new["review"]["remark"]["raw_text"], "18.8")
        self.assertEqual(new["review"]["remark"]["source"]["page"], 5)
        self.assertEqual(new["review"]["remark"]["conditions"]["retrieval_top_k"], 3)
        sources = {item["id"]: item for item in new["review"]["evidence"]}
        self.assertIn("most baselines", sources["8fd9ce01afa9-p5-text-17"]["text"])
        self.assertIn("heuristic triggers", sources[chosen]["text"])
        self.assertEqual(new["artifacts"][0]["data"], original)
        revised = read(output / "article-context.json")
        self.assertIn(chosen, {item["id"] for item in revised["followup_fragments"]})
        self.assertEqual(before, self.report.read_bytes())
        self.assertEqual(original_context, self.context.read_bytes())

    def test_explicit_fragment_resolves_only_the_selected_cell(self):
        chosen = "8fd9ce01afa9-p5-text-17"
        # Keep the dataset explicit in the saved grid while withholding the generator citation.
        grid = read(self.grid)
        for cell in grid["read"]["table"]["cells"]:
            if cell["headers"] == ["HotpotQA", "EM"]:
                cell["headers"][0] = "Dataset: HotpotQA"
        self.grid = self.directory / "labeled-grid.json"
        self.grid.write_text(json.dumps(grid), encoding="utf-8")
        reduced = read(self.context)
        for candidate in reduced["candidates"]:
            candidate["fragments"] = [item for item in candidate["fragments"] if item["id"] != chosen]
            candidate["basis"] = [item for item in candidate["basis"] if item["source"]["id"] != chosen]
        for list_name in ("setup_fragments", "followup_fragments"):
            reduced[list_name] = [item for item in reduced[list_name] if item["id"] != chosen]
        self.context = self.directory / "before-review-context.json"
        self.context.write_text(json.dumps(reduced), encoding="utf-8")
        before = self.directory / "before-review"
        self.run_cli("rag_mvp", "extract", self.pdf, "--page", 5, "--table", 1, "--grid-input", self.grid,
                     "--context-input", self.context, "--out", before)
        self.run_cli("rag_mvp.reconciliation", "--run-id", "before-review", "--pdf-sha256", read(self.text)["sha256"],
                     "--table", "table-one", before / "run.json", "--out", self.report)
        original = read(self.report)["artifacts"][0]["data"]
        remark, = [row for row in original["remarks"] if row["field"] == "generator"
                   and row["conditions"].get("method") == "Self-RAG" and row["source"]["headers"] == ["Dataset: HotpotQA", "EM"]]

        output = self.directory / "review"
        self.review(remark["id"], chosen, output)
        new = read(output / "reconciliation.json")
        result_id = new["review"]["result_id"]
        self.assertEqual(new["review"]["status"], "confirmed")
        revised = new["artifacts"][0]["data"]
        result, = [row for row in revised["results"] if row["id"] == result_id]
        self.assertEqual((result["raw_value"], result["generator"]), ("18.2", "Llama-2-13B"))
        self.assertEqual(result["condition_sources"]["generator"], [chosen])
        self.assertEqual(result["source"], remark["source"])
        self.assertEqual(revised["results"][:-1], original["results"])
        self.assertEqual(revised["remarks"], [row for row in original["remarks"] if row["id"] != remark["id"]])

        # Another observation leaves the scope of this same value open.
        previous = read(self.report)
        sources = set(result["source"]["evidence_ids"])
        sources.update(ref for refs in result["condition_sources"].values() for ref in refs)
        previous["artifacts"].append({
            "id": "another-observation", "kind": "prose",
            "data": {"source": original["source"], "remarks": [],
                     "evidence": [item for item in revised["evidence"] if item["id"] in sources],
                     "results": [{**result, "id": "limited-scope", "protocol": None}]},
        })
        self.report.write_text(json.dumps(previous), encoding="utf-8")
        open_report = self.directory / "open-review"
        self.review(remark["id"], chosen, open_report)
        unresolved = read(open_report / "reconciliation.json")
        self.assertEqual(unresolved["review"]["status"], "open")
        self.assertIn("open", [item["status"] for item in unresolved["relationships"]])

    def test_verifies_the_pdf_version_and_reuses_the_context_embedded_in_the_report(self):
        original = read(self.report)
        original["contexts"] = [{"artifact_id": "table-one", "data": read(self.context)}]
        self.report.write_text(json.dumps(original), encoding="utf-8")
        remark = next(row for row in original["artifacts"][0]["data"]["remarks"]
                      if row["field"] == "generator" and row["conditions"].get("method") == "FLARE")
        chosen = "8fd9ce01afa9-p6-text-13"
        output = self.directory / "another-version"
        foreign_text = self.directory / "foreign-text.json"
        foreign_text.write_text(json.dumps({**read(self.text), "sha256": "0" * 64}), encoding="utf-8")
        rejected = self.review(remark["id"], chosen, output, succeeds=False, text=foreign_text, embedded=True)
        self.assertEqual(rejected.returncode, 1)
        self.assertIn("SHA-256", rejected.stderr)
        self.assertFalse(output.exists())

        self.review(remark["id"], chosen, output, embedded=True)
        updated = read(output / "reconciliation.json")
        self.assertEqual(updated["review"]["status"], "open")
        self.assertEqual(updated["contexts"], [{"artifact_id": "table-one", "data": read(output / "article-context.json")}])


if __name__ == "__main__":
    unittest.main()
