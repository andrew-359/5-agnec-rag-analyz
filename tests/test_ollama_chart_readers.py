"""Reader calls against native chart replies served by a fixture Ollama."""

import base64
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import pdfplumber

from rag_mvp.plot_ollama_charts import read_granite_chart, read_paddle_chart
from test_qwen_plot import blank_pdf, ollama_reply


class OllamaChartReaderTest(unittest.TestCase):
    def read(self, reader, content, *, metric=""):
        output = {"points": [], "issues": []}
        box = (10, 20, 90, 100)
        with TemporaryDirectory() as folder, ollama_reply(content) as (env, requests, _):
            path = Path(folder) / "chart.pdf"
            blank_pdf(path)
            with pdfplumber.open(path) as document, patch.dict("os.environ", env):
                reader(document.pages[0], SimpleNamespace(panel_box=box, y_axis=metric), output,
                       lambda region: {"page": 1, "box_pt": region, "url": f"{path.as_uri()}#page=1"})
        (endpoint, request, _), = requests
        self.assertEqual(endpoint, "/api/chat")
        self.assertTrue(base64.b64decode(request["messages"][-1]["images"][0]).startswith(b"\x89PNG"))
        return output, request

    def test_granite_csv_retains_labels_decimal_spelling_and_pdf_source(self):
        content = '```csv\nDataset,"Method A, tuned",Method B\nSet X,0.570,\n```'
        output, request = self.read(read_granite_chart, content, metric="Exact Match")

        self.assertEqual(request["model"], "hf.co/ibm-granite/granite-vision-4.1-4b-GGUF:Q4_K_M")
        self.assertEqual(request["messages"][-1]["content"], "<chart2csv>")
        self.assertNotIn("format", request)
        self.assertEqual(output["status"], "succeeded")
        self.assertEqual(output["points"], [])
        proposal, = output["proposals"]
        self.assertEqual(proposal, {
            "series": "Method A, tuned", "category": "Set X", "metric": "Exact Match",
            "value": "0.570", "status": "unreviewed", "approximate": True, "source": output["source"],
        })
        self.assertEqual(proposal["source"]["precision"], "figure")
        self.assertEqual(proposal["source"]["box_pt"], (10, 20, 90, 100))
        self.assertTrue(proposal["source"]["url"].endswith("chart.pdf#page=1"))
        raw = json.loads(output["private_artifacts"]["ollama-response.txt"])
        self.assertEqual(raw["message"]["content"], content)

    def test_paddle_reads_pipe_delimited_steps_and_leaves_unknown_metric_open(self):
        content = "Step | PPO | BM25\n0 | 0.000 | N/A\n2 | 0.1480 | 0.170"
        output, request = self.read(read_paddle_chart, content)

        self.assertEqual(request["model"], "hf.co/PaddlePaddle/PaddleOCR-VL-1.6-GGUF:latest")
        self.assertEqual(request["messages"][-1]["content"], "Chart Recognition:")
        self.assertEqual(output["status"], "succeeded")
        self.assertEqual([
            (proposal["category"], proposal["series"], proposal["value"], proposal["metric"], proposal["status"])
            for proposal in output["proposals"]
        ], [
            ("0", "PPO", "0.000", None, "unreviewed"),
            ("2", "PPO", "0.1480", None, "unreviewed"),
            ("2", "BM25", "0.170", None, "unreviewed"),
        ])

    def test_explicit_method_header_keeps_methods_as_series(self):
        content = "Method,Set X,Set Y\nMethod A,0.570,0.600"
        output, _ = self.read(read_granite_chart, content)

        self.assertEqual([
            (proposal["category"], proposal["series"], proposal["value"])
            for proposal in output["proposals"]
        ], [("Set X", "Method A", "0.570"), ("Set Y", "Method A", "0.600")])
