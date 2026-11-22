import json
import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, skipUnless


@skipUnless(os.getenv("RAG_CONTEXT_REPLAY_INPUTS") and os.getenv("RAG_DEV_CORPUS"),
            "Нужны сохранённые сетки P003/D009 и комплект участника.")
class SavedContextCliTest(TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.inputs = Path(os.environ["RAG_CONTEXT_REPLAY_INPUTS"])
        self.corpus = Path(os.environ["RAG_DEV_CORPUS"])

    def extract(self, case, page, output, *options, error=None, grid=None):
        pdf = self.corpus / ("dev/D009.pdf" if case == "d009" else "test/P003.pdf")
        process = subprocess.run([
            sys.executable, "-m", "rag_mvp", "extract", str(pdf), "--page", str(page), "--table", "1",
            "--grid-input", str(grid or self.inputs / f"{case}-grid.json"), "--out", str(output), *map(str, options),
        ], capture_output=True, text=True, timeout=20)
        if error:
            self.assertEqual(process.returncode, 3, process.stderr)
            self.assertIn(error, process.stderr)
            self.assertFalse(output.exists())
        else:
            self.assertEqual(process.returncode, 0, process.stderr)

    def test_d009_reuses_the_context_of_an_open_binding(self):
        first = self.directory / "first"
        self.extract("d009", 5, first, "--article-text", self.inputs / "d009-text.json")
        context_path = first / "article-context.json"
        artifact = json.loads(context_path.read_bytes())
        self.assertEqual(artifact["schema_version"], 1)
        self.assertEqual(artifact["target"], {"page": 5, "number": 1, "region": None})
        candidate, = artifact["candidates"]
        self.assertIn("caption", {item["kind"] for item in candidate["basis"]})
        condition, = [item["source"] for item in candidate["basis"]
                      if item["kind"] == "conditions" and "most baselines" in item["source"]["text"]]
        self.assertEqual(condition["page"], 5)

        replay = self.directory / "replay"
        self.extract("d009", 5, replay, "--context-input", context_path)
        self.assertEqual((replay / "article-context.json").read_bytes(), context_path.read_bytes())
        before = json.loads((first / "run.json").read_bytes())
        after = json.loads((replay / "run.json").read_bytes())
        for key in ("results", "remarks", "evidence"):
            self.assertEqual(after[key], before[key])
        remark, = [item for item in after["remarks"] if item["conditions"].get("method") == "FLARE"
                   and item["field"] == "generator" and item["source"]["headers"] == ["HotpotQA", "EM"]]
        self.assertEqual(remark["source"]["page"], 5)
        self.assertIn(condition["id"], remark["options"][0]["evidence_ids"])

    def test_p003_saves_printed_row_and_definition_with_the_same_variant(self):
        first = self.directory / "first"
        self.extract("p003", 6, first, "--article-text", self.inputs / "p003-text.json")
        context_path = first / "article-context.json"
        artifact = json.loads(context_path.read_bytes())
        fixed_one, = [item for item in artifact["fixed_depths"] if item["depth"] == 1]
        self.assertEqual(fixed_one["scope"], "Fixed-1 RAG")
        self.assertIn("Fixed-1 RAG", fixed_one["declaration"]["text"])
        self.assertIn("retrieve the top-\nk", fixed_one["definition"]["text"])
        self.assertTrue(all(row["page"] == 6 and "Fixed-1 RAG" in row["text"] for row in fixed_one["rows"]))
        self.assertIn("74.65% 42.27%", fixed_one["rows"][0]["text"])

        replay = self.directory / "replay"
        self.extract("p003", 6, replay, "--context-input", context_path)
        self.assertEqual((replay / "article-context.json").read_bytes(), context_path.read_bytes())

        # A second reading has lost the Fixed-k labels in the grid headers.
        alternate = json.loads((self.inputs / "p003-grid.json").read_bytes())
        for cell in alternate["read"]["grid"]["cells"]:
            if cell["role"] == "row_header" and "Fixed-" in cell["text"]:
                cell["text"] = "Unreadable method"
        alternate_path = self.directory / "alternate-grid.json"
        alternate_path.write_text(json.dumps(alternate), encoding="utf-8")
        other = self.directory / "alternate"
        self.extract("p003", 6, other, "--article-text", self.inputs / "p003-text.json", grid=alternate_path)
        self.assertEqual((other / "article-context.json").read_bytes(), context_path.read_bytes())

    def test_rejects_a_context_for_another_pdf_or_target_or_schema(self):
        first = self.directory / "first"
        self.extract("d009", 5, first, "--article-text", self.inputs / "d009-text.json")
        artifact = json.loads((first / "article-context.json").read_bytes())
        for changes, message in (
            ({"schema_version": 2}, "версия формата"),
            ({"sha256": "0" * 64}, "SHA-256"),
            ({"target": {**artifact["target"], "number": 2}}, "другой таблице"),
        ):
            with self.subTest(changes=changes):
                incorrect = self.directory / "incorrect.json"
                incorrect.write_text(json.dumps({**artifact, **changes}), encoding="utf-8")
                self.extract("d009", 5, self.directory / "rejected", "--context-input", incorrect, error=message)
