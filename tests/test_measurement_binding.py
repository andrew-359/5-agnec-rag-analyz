from unittest import TestCase

from rag_mvp.core.domain import ConditionOption, Source, TableTarget
from rag_mvp.core.measurement_binding import Assertion, Observation, Scope, assemble


class MeasurementBindingTest(TestCase):
    def setUp(self):
        self.table = TableTarget(1, 2)
        self.source = Source(1, 2, 4, 1, ("MRR",), (0.1, 0.2, 0.3, 0.4), ("score",))
        self.observation = Observation("retrieval", ".61", self.source)
        self.claims = (
            Assertion("method", "BM25", Scope(self.table, row=4), ("method",)),
            Assertion("dataset", "Dataset", Scope(self.table), ("caption",)),
            Assertion("metric", "MRR", Scope(self.table, column=1), ("header",)),
        )

    def test_retrieval_without_cutoff_or_generator(self):
        binding = assemble(self.observation, self.claims)
        self.assertEqual(binding.observation, self.observation)
        self.assertEqual(binding.confirmed, {
            claim.field: ConditionOption(claim.value, claim.evidence_ids) for claim in self.claims
        })
        self.assertEqual(binding.open_fields, {})

    def test_conflict_preserves_sources_and_respects_scope(self):
        claims = (
            *self.claims,
            Assertion("dataset", "Other", Scope(self.table), ("description",)),
            Assertion("method", "Not this row", Scope(self.table, row=3), ("different-row",)),
            Assertion("metric", "Not this column", Scope(self.table, column=0), ("different-column",)),
            Assertion("dataset", "Not this table", Scope(TableTarget(1, 3)), ("different-table",)),
        )
        binding = assemble(self.observation, claims)
        self.assertEqual(assemble(self.observation, reversed(claims)), binding)
        self.assertEqual(set(binding.confirmed), {"method", "metric"})
        self.assertEqual(binding.open_fields, {"dataset": (
            ConditionOption("Dataset", ("caption",)), ConditionOption("Other", ("description",)),
        )})
