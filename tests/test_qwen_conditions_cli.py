from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Event
from unittest import TestCase, skipUnless

from test_qwen_plot import OBSERVATION, blank_pdf, ollama_reply


SHA = "0" * 64
CAPTION = "Table 1. Results on the evaluation set."
SETUP = "We evaluate Method A and Method B."
DETAILS = "Method A retrieves exactly two passages."
PROPOSAL = {
    "field": "retrieval_top_k",
    "value": 2,
    "scope": "Method A",
    "citations": [
        {"fragment_id": "detail", "quote": DETAILS},
        {"fragment_id": "scope", "quote": SETUP},
    ],
}


class QwenConditionsCliTest(TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.source = self.root / "article-text.json"
        self.source.write_text(json.dumps({
            "document": "fixture.pdf", "sha256": SHA, "pages": [1, 2],
            "schema_version": 1, "engine_version": "fixture",
            "fragments": [
                {"id": ident, "text": value, "page": page, "precision": "text_fragment",
                 "box": [0.1, 0.2, 0.3, 0.4]}
                for ident, value, page in (("scope", SETUP, 1), ("detail", DETAILS, 1), ("caption", CAPTION, 2))
            ],
        }))

    def cli(self, env, out="attempt", extra=(), *, source=None, fragment_args=None):
        command = [
            sys.executable, "-m", "rag_mvp", "propose-table-conditions",
            "--article-text", str(source or self.source), "--page", "2", "--table", "1",
            "--caption", "caption",
            *(fragment_args if fragment_args is not None else ("--fragment", "scope", "--fragment", "detail")),
            "--out", str(self.root / out), *extra,
        ]
        return subprocess.run(command, env=env, capture_output=True, text=True, timeout=10)

    def attempt(self, out="attempt"):
        return json.loads((self.root / out / "attempt.json").read_text())

    def test_saves_an_open_proposal_with_both_literal_sources_and_call_metadata(self):
        with ollama_reply(json.dumps({"proposals": [PROPOSAL]})) as (env, requests, _):
            process = self.cli(env)
        self.assertEqual(process.returncode, 0, process.stderr)
        artifact = self.attempt()
        self.assertEqual(artifact["status"], "succeeded")
        proposal, = artifact["proposals"]
        self.assertEqual({key: proposal[key] for key in ("field", "value", "scope")},
                         {"field": "retrieval_top_k", "value": 2, "scope": "Method A"})
        self.assertEqual(proposal["applicability"], "open")
        self.assertEqual([(item["fragment_id"], item["quote"], item["status"]) for item in proposal["citations"]],
                         [("detail", DETAILS, "verified"), ("scope", SETUP, "verified")])
        request, = requests
        self.assertEqual(request[0], "/api/chat")
        sent = request[1]
        self.assertEqual(artifact["model_attempt"]["reported_model"], "qwen-fixture")
        self.assertEqual(artifact["model_attempt"]["options"], sent["options"])
        encoded = json.dumps(sent["messages"], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        self.assertEqual(artifact["model_attempt"]["prompt"], {
            "revision": "qwen-text-conditions-v1", "sha256": hashlib.sha256(encoded).hexdigest(),
        })
        self.assertEqual(artifact["input"]["pdf_sha256"], SHA)
        self.assertEqual({item["id"] for item in artifact["input"]["fragments"]}, {"scope", "detail", "caption"})
        raw = self.root / "attempt" / artifact["model_attempt"]["raw_response"]
        self.assertEqual(raw.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(json.loads(raw.read_bytes())["message"]["content"])["proposals"], [PROPOSAL])

    def test_distinguishes_invalid_fields_values_citations_empty_and_transport_failure(self):
        variants = [
            {**PROPOSAL, "field": "dataset"},
            {**PROPOSAL, "value": True},
            {**PROPOSAL, "citations": [{"fragment_id": "missing", "quote": DETAILS}]},
            {**PROPOSAL, "citations": [{"fragment_id": "detail", "quote": SETUP}]},
        ]
        with ollama_reply(json.dumps({"proposals": variants})) as (env, _, _):
            self.assertEqual(self.cli(env).returncode, 0)
        attempt = self.attempt()
        self.assertEqual(attempt["status"], "rejected_proposals")
        field, value, missing, wrong = attempt["proposals"]
        self.assertEqual(field["status"], "unsupported_field")
        self.assertEqual(value["status"], "invalid_value")
        self.assertEqual(missing["citations"][0]["status"], "unknown_fragment")
        self.assertEqual(wrong["citations"][0]["status"], "quote_missing")

        replies = (
            ('{"proposals":[]}', "no_proposals"),
            ('{"proposals":[{"scope":"\\ud800"}]}', "failed"),
            ("broken", "failed"),
        )
        for index, (content, expected) in enumerate(replies):
            with self.subTest(status=expected), ollama_reply(content) as (env, _, _):
                out = f"reply-{index}"
                self.assertEqual(self.cli(env, out=out).returncode, 0)
                attempt = self.attempt(out)
                self.assertEqual(attempt["status"], expected)
                self.assertTrue((self.root / out / attempt["model_attempt"]["raw_response"]).is_file())

        with socket.socket() as unused_port:
            unused_port.bind(("127.0.0.1", 0))
            env = {**os.environ, "RAG_OLLAMA_URL": f"http://127.0.0.1:{unused_port.getsockname()[1]}",
                   "RAG_QWEN_TEXT_MODEL": "fixture", "RAG_QWEN_TIMEOUT_SECONDS": "1"}
            self.assertEqual(self.cli(env, out="unavailable").returncode, 0)
        attempt = self.attempt("unavailable")
        self.assertEqual(attempt["status"], "unavailable")
        self.assertIn("Ollama", attempt["reason"])
        self.assertNotIn("raw_response", attempt["model_attempt"])

    def test_compares_against_a_local_control_without_sending_the_control_to_ollama(self):
        control = self.root / "control.json"
        marker = "local-only-control"
        control.write_text(json.dumps({
            "schema_version": 1, "name": marker, "pdf_sha256": SHA, "page": 2, "table": 1,
            "conditions": [{
                "field": "retrieval_top_k", "value": 2, "scope": "Method A",
                "required_citations": PROPOSAL["citations"],
            }],
        }))
        wrong_scope = {**PROPOSAL, "scope": "Method B"}
        wrong_value = {**PROPOSAL, "value": 3}
        missing_support = {**PROPOSAL, "citations": PROPOSAL["citations"][:1]}
        variants = [PROPOSAL, wrong_scope, wrong_value, missing_support]
        with ollama_reply(json.dumps({"proposals": variants})) as (env, requests, _):
            process = self.cli(env, extra=("--control", str(control)))
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertNotIn(marker, requests[0][1]["messages"][0]["content"])
        artifact = self.attempt()
        self.assertEqual(artifact["baseline"]["assertions"], [])
        self.assertEqual([result["status"] for result in artifact["comparison"]["proposal_checks"]],
                         ["matches_control", "scope_mismatch", "value_mismatch", "insufficient_citations"])
        self.assertTrue(all(item["applicability"] == "open" for item in artifact["proposals"]))

    def test_text_waits_for_chart_while_an_ordinary_reader_completes(self):
        chart_requested, release_chart = Event(), Event()

        def gate(request):
            if any(message.get("images") for message in request["messages"]):
                chart_requested.set()
                release_chart.wait(6)

        pdf = self.root / "chart.pdf"
        blank_pdf(pdf)
        body = json.dumps({"observations": [OBSERVATION], "proposals": [PROPOSAL]})
        with ollama_reply(body, timeout="8", before_reply=gate) as (env, requests, completed):
            with ThreadPoolExecutor(2) as pool:
                chart = pool.submit(subprocess.run, [
                    sys.executable, "-m", "rag_mvp.plot_extract", str(pdf), "--strategy", "qwen-vision",
                    "--page", "1", "--figure", "1", "--panel", "a", "--series", "all",
                    "--x-axis", "x", "--y-axis", "y", "--panel-box", "10", "20", "90", "100", "--out", "-",
                ], env=env, capture_output=True, text=True, timeout=10)
                try:
                    self.assertTrue(chart_requested.wait(5))
                    started = Event()

                    def start_text():
                        started.set()
                        return self.cli(env)

                    text = pool.submit(start_text)
                    self.assertTrue(started.wait(2))
                    ordinary = subprocess.run([
                        sys.executable, "-m", "rag_mvp", "read-article-text", str(pdf),
                        "--out", str(self.root / "ordinary"),
                    ], env=env, capture_output=True, text=True, timeout=5)
                    self.assertEqual(ordinary.returncode, 0, ordinary.stderr)
                    self.assertTrue((self.root / "ordinary/article-text.json").is_file())
                    self.assertFalse(chart.done())
                    self.assertFalse(text.done())
                    self.assertEqual(len(requests), 1)
                finally:
                    release_chart.set()
                chart_result, text_result = chart.result(), text.result()
        self.assertEqual(chart_result.returncode, 0, chart_result.stderr)
        self.assertEqual(text_result.returncode, 0, text_result.stderr)
        self.assertEqual(json.loads(chart_result.stdout)["status"], "succeeded")
        self.assertEqual(self.attempt()["status"], "succeeded")
        self.assertEqual(len(requests), 2)
        self.assertGreaterEqual(requests[1][2], completed[0])


@skipUnless(os.getenv("RAG_QWEN_CONTROL_INPUTS"), "Требуются сохранённые фрагменты и сетки P003/D005/D009.")
class QwenSavedControlsTest(TestCase):
    def test_compare_saved_p003_d005_d009(self):
        with TemporaryDirectory() as temporary:
            out = Path(os.getenv("RAG_QWEN_CONTROL_OUTPUT", temporary))
            for case in ("p003", "d005", "d009"):
                with self.subTest(case=case):
                    self.compare_case(case, out)

    def compare_case(self, case, out):
        inputs = Path(os.environ["RAG_QWEN_CONTROL_INPUTS"])
        source, grid = (inputs / f"{case}-{name}.json" for name in ("text", "grid"))
        initial_grid = grid.read_bytes()
        control = Path(__file__).parent / "fixtures" / "qwen_text_conditions" / f"{case}.json"
        plan = json.loads(control.read_bytes())
        expected, = plan["conditions"]
        digest = plan["pdf_sha256"][:12]

        def ref(suffix):
            return f"{digest}-{suffix}"

        def cite(suffix, quote):
            return {"fragment_id": ref(suffix), "quote": quote}

        positive = {key: expected[key] for key in ("field", "value", "scope")}
        positive["citations"] = expected["required_citations"]
        if case == "p003":
            caption, *sources = (
                "p6-text-1", "p5-text-24", "p5-text-26", "p5-text-27", "p6-text-2",
                "p6-text-4", "p6-text-12",
            )
            variants = [
                positive,
                {**positive, "scope": "URGen"},
                {**positive, "value": 5, "citations": [cite("p6-text-12", "top K = 5 candidates")]},
                {**positive, "value": 0.8, "citations": [cite("p6-text-1", "OC@0.8")]},
                {**positive, "citations": positive["citations"][:1]},
            ]
            checks = ["matches_control", "scope_mismatch", "value_mismatch", "invalid_value", "insufficient_citations"]
        elif case == "d005":
            caption, *sources = ("p10-text-5", "p6-text-8", "p10-text-1", "p10-text-2", "p10-text-3")
            variants = [
                positive,
                {**positive, "value": "Llama-3.1-70b-instruct",
                 "citations": [cite("p6-text-8", "We used Llama-3.1-70b-instruct")]},
            ]
            checks = ["matches_control", "value_mismatch"]
        else:
            caption, *sources = ("p5-text-1", "p5-text-17", "p5-text-18")
            variants = [positive, {**positive, "scope": "FLARE"}]
            checks = ["matches_control", "scope_mismatch"]

        with ollama_reply(json.dumps({"proposals": variants})) as (env, requests, _):
            result = subprocess.run([
                sys.executable, "-m", "rag_mvp", "propose-table-conditions",
                "--article-text", str(source), "--page", str(plan["page"]), "--table", str(plan["table"]),
                "--caption", ref(caption), *[arg for suffix in sources for arg in ("--fragment", ref(suffix))],
                "--grid-input", str(grid), "--control", str(control), "--out", str(out / case),
            ], env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        artifact = json.loads((out / case / "attempt.json").read_bytes())
        comparison = artifact["comparison"]
        self.assertEqual([check["status"] for check in comparison["proposal_checks"]], checks)
        self.assertEqual(comparison["conditions"][0]["matching_proposals"], [0])
        self.assertEqual(comparison["conditions"][0]["baseline_match"], case != "p003")
        self.assertEqual(artifact["baseline"]["grid"]["sha256"], hashlib.sha256(initial_grid).hexdigest())
        self.assertEqual(grid.read_bytes(), initial_grid)
        prompt = requests[0][1]["messages"][0]["content"]
        sent = json.loads(prompt.rsplit("\n\n", 1)[-1])
        self.assertEqual({item["id"] for item in sent["fragments"]}, {ref(part) for part in (caption, *sources)})
        self.assertNotIn(plan["name"], prompt)
