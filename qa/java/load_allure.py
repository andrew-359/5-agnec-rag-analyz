"""Convert the saved D001 baseline metrics to Allure results without copying corpus data."""

import argparse
import json
from pathlib import Path
import uuid

LABELS = {"control": "Одиночный запуск", "pair-first": "Пара · первый запуск", "pair-second": "Пара · второй запуск"}
METRICS = {
    "accepted_status": "Статус приёма",
    "last_status": "Итоговый статус",
    "first_attempt_wait_seconds": "До первой попытки, с",
    "duration_seconds": "До публикации, с",
}
OUTCOMES = {"published", "failed", "empty", "timeout", "verification_error", "launch_error", "not_started"}
READ_ROUTES = {"library": "Библиотека", "run": "Карточка прогона", "results": "Результаты выпуска"}


def write_results(baseline: dict, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    runs = {item["label"]: item for item in baseline["runs"]}
    overlap = baseline.get("pair", {}).get("overlap_confirmed") is True
    accepted = baseline.get("acceptance_passed") is True
    measurement = baseline.get("measurement")

    for label, title in LABELS.items():
        item = runs.get(label, {})
        outcome = item.get("outcome")
        if outcome not in OUTCOMES:
            outcome = "incomplete"
        failures = [] if outcome == "published" else [f"Итог запуска: {outcome}"]
        if label == "pair-second" and not overlap:
            failures.append("Пересечение запусков пары не подтверждено.")
        if label == "pair-second" and not accepted:
            failures.append("Проверка трёх независимых выпусков не пройдена.")
        parameters = [
            {"name": title, "value": str(item[key])}
            for key, title in METRICS.items()
            if isinstance(item.get(key), (int, float)) or item.get(key) in ("queued", "running", "done", "failed", "empty")
        ]
        sent, received = item.get("sent_at"), item.get("accepted_at")
        if isinstance(sent, (int, float)) and isinstance(received, (int, float)) and received >= sent:
            parameters.append({"name": "HTTP-приём, с", "value": f"{received - sent:.3f}"})
        if label == "pair-second":
            parameters.append({"name": "Пересечение подтверждено", "value": "да" if overlap else "нет"})
        full_name = f"rag.load.d001.{label}"
        test_id = uuid.uuid5(uuid.NAMESPACE_URL, full_name).hex
        scenario = "Одиночный запуск" if label == "control" else "Пересекающиеся запуски"
        result = {
            "uuid": test_id, "historyId": test_id, "name": title, "fullName": full_name,
            "status": "failed" if failures else "passed", "stage": "finished",
            "parameters": parameters,
            "labels": [
                {"name": "epic", "value": "Нагрузочные проверки"},
                {"name": "feature", "value": "Публикация D001"},
                {"name": "story", "value": scenario},
                {"name": "parentSuite", "value": "Нагрузочные проверки"},
                {"name": "suite", "value": scenario},
            ],
        }
        if isinstance(measurement, str):
            result["description"] = measurement
        if failures:
            result["statusDetails"] = {"message": "\n".join(failures)}
        if isinstance(item.get("sent_at"), (int, float)) and isinstance(item.get("last_observed_at"), (int, float)):
            result["start"] = int(item["sent_at"] * 1000)
            result["stop"] = int(item["last_observed_at"] * 1000)
        (target / f"{test_id}-result.json").write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")


def write_read_results(measurement: dict, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    modes = measurement["modes"]
    expected = measurement["rate"] * measurement["seconds"]
    for route, title in {**READ_ROUTES, "integrity": "Целостность закреплённой версии и условия замера"}.items():
        failures = []
        parameters = []
        if route == "integrity":
            failures.extend(measurement["errors"])
            if not measurement.get("release_stable"):
                failures.append("Не подтверждена сохранность закреплённого PDF и ZIP.")
            busy = modes.get("busy", {})
            parameters = [
                {"name": "Версия", "value": measurement["pinned_version"]},
                {"name": "Циклы/с по плану", "value": str(measurement["rate"])},
                {"name": "Окно, с", "value": str(measurement["seconds"])},
                {"name": "Отсчёты с активным воркером", "value": f"{busy.get('active_samples', 0)}/{busy.get('worker_samples', 0)}"},
            ]
            if measurement.get("publication_recovery"):
                parameters.append({"name": "Повтор публикации", "value": measurement["publication_recovery"]})
        else:
            p95 = {}
            for mode, caption in (("idle", "Воркер свободен"), ("busy", "Идёт разбор")):
                row = modes.get(mode, {}).get("rows", {}).get(route)
                if not row:
                    failures.append(f"{caption}: метрики отсутствуют.")
                    continue
                if row["errors"] != 0 or not expected <= row["requests"] <= expected + 1:
                    failures.append(f"{caption}: {row['requests']} запросов при плане {expected}, ошибки {row['errors']}.")
                for key, unit in (("p50_ms", "p50, мс"), ("p95_ms", "p95, мс"),
                                  ("requests_per_second", "запросы/с"), ("requests", "запросы"), ("errors", "ошибки")):
                    value = row[key]
                    parameters.append({"name": f"{caption} · {unit}",
                                       "value": f"{value:.2f}" if isinstance(value, float) else str(value)})
                p95[mode] = row["p95_ms"]
            if p95.get("idle") is not None and p95.get("busy") is not None:
                parameters.append({"name": "Изменение p95, мс", "value": f"{p95['busy'] - p95['idle']:+.2f}"})
        full_name = f"rag.load.d001.read.{route}"
        test_id = uuid.uuid5(uuid.NAMESPACE_URL, full_name).hex
        result = {
            "uuid": test_id, "historyId": test_id, "name": title, "fullName": full_name, "stage": "finished",
            "description": measurement["measurement"], "status": "failed" if failures else "passed",
            "labels": [{"name": "epic", "value": "Нагрузочные проверки"},
                       {"name": "feature", "value": "Чтение на фоне разбора"},
                       {"name": "suite", "value": "Чтение на фоне разбора"}],
            "parameters": parameters,
        }
        if failures:
            result["statusDetails"] = {"message": "\n".join(failures)}
        (target / f"{test_id}-result.json").write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("out", type=Path)
    parser.add_argument("--read-load", action="store_true")
    args = parser.parse_args()
    convert = write_read_results if args.read_load else write_results
    convert(json.loads(args.baseline.read_text(encoding="utf-8")), args.out)


if __name__ == "__main__":
    main()
