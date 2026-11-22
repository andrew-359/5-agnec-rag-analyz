#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."

export PYTHONPATH=tests
exec "${RAG_PYTHON:-python3}" -m unittest -v \
  test_domain test_ablation test_article_context_search test_table_context_binding_run test_table_reread \
  test_ocr_tableformer_grid test_plot_extract.PlotCommandTest test_plot_extract.ScatterIdentityTest \
  test_chart2csv_pilot test_qwen_plot test_qwen_conditions_cli.QwenConditionsCliTest \
  test_unstructured_cli.UnstructuredCliTest test_cli.ReaderOptionsTest \
  test_prose_failure_counts.ProseFailureCountsTest.test_incomplete_categories_leave_a_source_anchored_remark \
  test_prose_failure_counts.ProseFailureCountsTest.test_table_repeat_conflict_and_distinct_scopes_are_visible_in_the_cli_artifact \
  test_reconciliation_cli test_saved_output_checks test_d001_load_baseline test_full_pdf_batch
