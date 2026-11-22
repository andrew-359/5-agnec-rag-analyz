"""Check the D001, D009 and D013 control cases against saved JSON artifacts."""

import argparse
import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path


RUNS = Path(__file__).resolve().parent
PDF_HASHES = {
    "D001": "7fcbb21b0bdf5bb222b347d0b5e8d6587aa8af5c5f855f62322d0d7ad5427d3c",
    "D009": "8fd9ce01afa9c483a4e7b60f4d69ff97e61df96e1611fd7bddb5a80c94714145",
    "D013": "a16d5237120a18d18488f4db3d7ae3fbc5ff628a90b794bd007451ae53472b7d",
}
MATCH = "СОВПАДЕНИЕ"
MISSING = "ПРОПУСК"
BINDING = "ОШИБКА ПРИВЯЗКИ"
READING = "ОШИБКА ЧТЕНИЯ"
OPEN = "ОТКРЫТОЕ УСЛОВИЕ"


@dataclass(frozen=True)
class TableCase:
    label: str
    paper: str
    address: tuple[int, int, int, int]
    headers: tuple[str, ...]
    value: str
    identity: dict
    conditions: dict


BASE = {"method": "ThreadRAG", "dataset": "HotpotQA", "metric": "EM"}
MODEL = "LLaMA3-8B-Instruct"
COMPONENTS = dict.fromkeys(("SN", "STG", "EDP"), True)
TABLE_CASES = (
    TableCase("D009 Search-R1 · R111", "D009", (5, 1, 9, 4), ("HotpotQA", "EM"), "37.9",
              {**BASE, "method": "Search-R1"}, {"retrieval_top_k": 3}),
    TableCase("D001 Table 1 · R235", "D001", (6, 1, 18, 1), (MODEL, "HotpotQA", "EM"), "0.422",
              {**BASE, "generator": MODEL}, {"unit": "fraction", "retrieval_top_k": 5}),
    TableCase("D001 Table 2 · R257", "D001", (7, 2, 2, 3), ("Datasets", "HotpotQA"), "0.422",
              {**BASE, "components": COMPONENTS}, {"unit": "fraction", "generator": None, "retrieval_top_k": None}),
)


def read_artifact(path, required_lists, required_objects):
    data = json.loads(path.read_text(encoding="utf-8-sig"), parse_float=Decimal)
    if not isinstance(data, dict):
        raise ValueError("ожидался объект JSON")
    for key in required_objects:
        if not isinstance(data.get(key), dict):
            raise ValueError(f"ожидался объект в поле {key}")
    for key in required_lists:
        rows = data.get(key)
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError(f"ожидался список объектов в поле {key}")
    return data


def as_object(value):
    return value if isinstance(value, dict) else {}


def number(value):
    try:
        parsed = Decimal(str(value))
    except InvalidOperation:
        return None
    return parsed if parsed.is_finite() else None


def valid_box(box):
    if not isinstance(box, list) or len(box) != 4:
        return False
    left, top, right, bottom = map(number, box)
    return (None not in (left, top, right, bottom)
            and 0 <= left < right <= 1 and 0 <= top < bottom <= 1)


def source_address(record):
    source = as_object(record.get("source"))
    return tuple(source.get(key) for key in ("page", "table", "row", "column"))


def check_value(value, expected, tolerance=Decimal(0)):
    actual = number(value)
    if value in (None, ""):
        return MISSING, f"число отсутствует, ожидалось {expected}"
    if actual is None or abs(actual - expected) > tolerance:
        return READING, f"число {value!r}, ожидалось {expected} (допуск {tolerance})"
    return None


def check_fields(record, expected):
    findings = []
    for key, target in expected.items():
        actual = record.get(key)
        if actual in (None, ""):
            if target is not None:
                findings.append((MISSING, f"поле {key} пусто, ожидалось {target!r}"))
        elif actual != target:
            wanted = "пустое поле" if target is None else repr(target)
            findings.append((READING if key == "unit" else BINDING,
                             f"{key}={actual!r}, ожидалось {wanted}"))
    return findings


def check_search_r1(run, record):
    generator = record.get("generator")
    fields = check_fields(record, {"generator": "Qwen2.5-7B-Instruct", "unit": "%"})
    if not generator:
        return fields
    refs = as_object(record.get("condition_sources")).get("generator")
    evidence = run.get("evidence")
    quotes = [
        str(item.get("text", "")).strip()
        for item in evidence if isinstance(item, dict) and item.get("id") in refs and item.get("page") == 5
    ] if isinstance(evidence, list) and isinstance(refs, list) else []
    # The generic quote about "most baselines" does not name Search-R1.
    # A less direct statement with both names needs review in the context of Table 1.
    if not any("Search-R1" in quote and str(generator) in quote for quote in quotes):
        fields.append((BINDING, "на странице 5 нет связанной цитаты, называющей Search-R1 и модель"))
    elif generator == "Qwen2.5-7B-Instruct":
        fields.append((OPEN, "проверить, подтверждает ли цитата генератор именно для Table 1"))
    return fields


def check_table(run, case):
    source = as_object(run.get("source"))
    if source.get("name") != f"{case.paper}.pdf" or source.get("sha256") != PDF_HASHES[case.paper]:
        return [(BINDING, f"ожидался источник {case.paper}.pdf с SHA-256 из контрольного прогона")]

    results = [row for row in run["results"] if row.get("statistic") in (None, "")]
    remarks = [row for row in run["remarks"] if number(row.get("raw_text")) is not None
               or row.get("field") == "generator"]
    exact = [row for row in results if source_address(row) == case.address]
    notes = [row for row in remarks if source_address(row) == case.address]

    def is_candidate(row):
        fields = as_object(row.get("conditions")) if "raw_text" in row else row
        return all(fields.get(key) == value for key, value in case.identity.items())

    if not exact and not notes:
        wanted = ", ".join(map(str, case.address))
        other = [source_address(row) for row in results + remarks if is_candidate(row)]
        if other:
            return [(BINDING, f"запись найдена по адресу {other}, ожидалось ({wanted})")]
        return [(MISSING, f"нет результата или числового замечания в ячейке ({wanted})")]
    if len(exact) > 1:
        return [(BINDING, "несколько основных результатов ссылаются на одну контрольную ячейку")]

    record = exact[0] if exact else next((note for note in notes if note.get("field") == "generator"), notes[0])
    fields = record if exact else as_object(record.get("conditions"))
    source = as_object(record.get("source"))
    findings = check_fields(fields, {**case.identity, **case.conditions})
    if source.get("headers") != list(case.headers):
        findings.append((BINDING, f"шапка ячейки {source.get('headers')!r}, ожидалось {list(case.headers)!r}"))
    if not valid_box(source.get("box")):
        findings.append((BINDING, "нет корректной области ячейки"))
    value_finding = check_value(record.get("value" if exact else "raw_text"), Decimal(case.value))
    if value_finding:
        findings.append(value_finding)
    if exact:
        prefix = f"Table {case.address[1]}:"
        if not str(record.get("experiment", "")).startswith(prefix):
            findings.append((BINDING, f"эксперимент {record.get('experiment')!r}, ожидалось начало {prefix!r}"))
        if case.paper == "D009":
            findings.extend(check_search_r1(run, record))
    elif case.paper == "D009" and record.get("field") == "generator" and not fields.get("generator"):
        if value_finding is None:
            findings.append((OPEN, "число прочитано, есть замечание по генератору"))
    else:
        findings.append((MISSING, "число есть в замечании, подтверждённая запись отсутствует"))
    return findings or [(MATCH, f"{case.value}, источник и условия совпали")]


def check_plot(run, step, expected):
    settings = as_object(run.get("settings"))
    source = as_object(run.get("source"))
    if Path(str(run.get("pdf", ""))).name != "D013.pdf" or run.get("sha256") != PDF_HASHES["D013"]:
        return [(BINDING, "ожидался источник D013.pdf с SHA-256 из контрольного прогона")]
    findings = check_fields({**settings, "figure": str(settings.get("figure", ""))},
                            {"figure": "2", "panel": "a", "series": "PPO",
                             "page": 9, "x_axis": "step", "y_axis": "EM"})
    if source.get("page") != 9:
        findings.append((BINDING, "источник панели должен вести на страницу 9"))
    if findings:
        return findings

    points = [point for point in run["points"] if number(point.get("x")) == step]
    if not points:
        detail = next((issue["message"] for issue in run["issues"]
                       if isinstance(issue.get("message"), str)
                       and re.match(rf"^x={step}(?:\.0+)?:", issue["message"])), "")
        return [(MISSING, f"точка отсутствует{f' ({detail})' if detail else ''}")]
    if len(points) > 1:
        return [(BINDING, "несколько точек для одного шага PPO")]
    point = points[0]
    point_source = as_object(point.get("source"))
    point_box = point_source.get("box")
    panel_box = source.get("box")
    if (point_source.get("page") != 9 or not valid_box(point_box)
            or not valid_box(panel_box) or point_box == panel_box):
        return [(BINDING, "ожидалась область самой точки на странице 9")]
    left, top, right, bottom = map(number, point_box)
    panel_left, panel_top, panel_right, panel_bottom = map(number, panel_box)
    if not (panel_left <= left < right <= panel_right and panel_top <= top < bottom <= panel_bottom):
        return [(BINDING, "область точки выходит за пределы панели")]
    if mismatch := check_value(point.get("y"), expected, Decimal("0.001")):
        return [mismatch]
    return [(MATCH, f"{point['y']} (эталон {expected}, допуск 0.001)")]


def main():
    parser = argparse.ArgumentParser(
        description="Сверить сохранённые JSON для D001, D009 и D013.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--d009", type=Path, default=RUNS / "D009-table-cli/run.json", help="JSON Table 1 D009")
    parser.add_argument("--d001-table1", type=Path, default=RUNS / "snapshots/D001/run.json", help="JSON Table 1 D001")
    parser.add_argument("--d001-table2", type=Path, default=RUNS / "D001-ablation/run.json", help="JSON Table 2 D001")
    parser.add_argument("--d013", type=Path, default=RUNS / "plot-pilot/D013.json", help="JSON графика Figure 2(a) D013")
    args = parser.parse_args()

    artifacts = list(zip(TABLE_CASES, (args.d009, args.d001_table1, args.d001_table2)))
    artifacts.append((None, args.d013))
    ok = True
    for case, path in artifacts:
        label = case.label if case else "D013 Figure 2(a) · PPO"
        print(f"{label} — {path}")
        try:
            if case:
                run = read_artifact(path, ("results", "remarks"), ("source",))
                findings = check_table(run, case)
            else:
                run = read_artifact(path, ("points", "issues"), ("source", "settings"))
                findings = [(kind, f"шаг {step} · {message}")
                            for step, target in ((50, "0.200"), (175, "0.318"))
                            for kind, message in check_plot(run, step, Decimal(target))]
        except FileNotFoundError:
            findings = [("АРТЕФАКТ НЕ НАЙДЕН", "файл по указанному пути отсутствует")]
        except (OSError, ValueError) as error:
            findings = [("ОШИБКА АРТЕФАКТА", str(error))]

        for kind, message in findings:
            print(f"  [{kind}] {message}")
            ok &= kind in (MATCH, OPEN)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
