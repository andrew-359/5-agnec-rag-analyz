"""Decide how saved observations of one measurement relate to each other."""

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from itertools import combinations


REQUIRED = ("experiment", "method", "dataset", "task", "metric")
CONDITIONS = (
    "generator", "protocol", "retrieval_top_k", "metric_group", "metric_variant", "metric_k",
    "statistic", "baseline_method", "sample_size", "original_sample_size", "cited_publication_id",
    "factor", "setting", "source_language",
)
IDENTITY_FIELDS = (*REQUIRED, *CONDITIONS, "result_origin", "unit")
SCORE_UNITS = {"fraction", "%"}
PERCENTAGE_POINT_UNITS = {"percentage_point", "percentage_points"}


def group_unit(unit: str | None) -> str | None:
    if unit in SCORE_UNITS:
        return "fraction"
    if unit in PERCENTAGE_POINT_UNITS:
        return "percentage_points"
    return unit


def normalized_value(row: dict) -> Decimal | None:
    if not row.get("unit"):
        return None
    try:
        value = Decimal(row["value"])
    except (InvalidOperation, TypeError):
        return None
    if not value.is_finite():
        return None
    if row.get("unit") == "%":
        sign, digits, exponent = value.as_tuple()
        return Decimal((sign, digits, exponent - 2))
    return value


def scope(row: dict) -> tuple:
    conditions = tuple(row.get(field) if row.get(field) != "" else None for field in CONDITIONS)
    origin = row.get("result_origin", "primary")
    configuration = tuple(tuple(sorted((row.get(field) or {}).items())) or None
                          for field in ("components", "parameters"))
    return (*conditions, origin, *configuration, group_unit(row.get("unit")))


def parse_measurement(row: dict) -> None:
    if any(type(row.get(field)) not in (int, str, type(None)) for field in IDENTITY_FIELDS):
        raise ValueError(f"Недопустимое условие измерения {row['id']}.")
    for field, allowed in (("components", (bool,)), ("parameters", (int, float))):
        values = row.get(field)
        if values is not None and (not isinstance(values, dict) or any(type(value) not in allowed for value in values.values())):
            raise ValueError(f"Недопустимые {field} измерения {row['id']}.")


@dataclass(frozen=True)
class Observation:
    artifact_id: str
    result_id: str
    value: Decimal | None
    identity: tuple
    conditions: tuple

    def reference(self) -> dict[str, str]:
        return {"artifact_id": self.artifact_id, "result_id": self.result_id}


def relate(status: str, observations: list[Observation]) -> dict:
    return {"status": status, "members": [observation.reference() for observation in observations]}


def parse_observations(artifacts: list[dict]) -> list[Observation]:
    observations = []
    for entry in artifacts:
        for row in entry["data"]["results"]:
            parse_measurement(row)
            identity = tuple(row.get(field) for field in REQUIRED)
            if all(identity):
                observations.append(Observation(entry["id"], row["id"], normalized_value(row), identity, scope(row)))
    return observations


def reconcile_measurements(observations: list[Observation]) -> list[dict]:
    """Only link matching scopes supported by observations from independent artifacts."""
    measurements = defaultdict(lambda: defaultdict(list))
    for item in observations:
        measurements[item.identity][item.conditions].append(item)

    relationships = []
    for scopes in measurements.values():
        for observations in scopes.values():
            if len({item.artifact_id for item in observations}) < 2:
                continue
            if len({item.artifact_id for item in observations}) != len(observations):
                relationships.append(relate("open", observations))
                continue
            values = {item.value for item in observations}
            status = "open" if None in values else "agreement" if len(values) == 1 else "conflict"
            relationships.append(relate(status, observations))

        for (left, first), (right, second) in combinations(scopes.items(), 2):
            if all(a == b or a is None or b is None for a, b in zip(left, right)):
                observations = [*first, *second]
                if len({item.artifact_id for item in observations}) > 1:
                    relationships.append(relate("open", observations))
    return relationships


def summarize_observations(artifacts: list[dict], relationships: list[dict]) -> dict:
    def key(reference: dict) -> tuple[str, str]:
        return reference["artifact_id"], reference["result_id"]

    conflicts = {key(member) for relation in relationships if relation["status"] == "conflict"
                 for member in relation["members"]}
    duplicates = {key(member) for relation in relationships if relation["status"] == "agreement"
                  for member in relation["members"][1:]}
    return {
        "confirmed": [
            {"artifact_id": entry["id"], "result_id": row["id"]}
            for entry in artifacts for row in entry["data"]["results"]
            if (entry["id"], row["id"]) not in conflicts | duplicates and row.get("value") is not None
        ],
        "stage_remarks": [
            {"artifact_id": entry["id"], "remark_id": row["id"]}
            for entry in artifacts for row in entry["data"]["remarks"]
        ],
        "disputed": [relation for relation in relationships if relation["status"] != "agreement"],
    }
