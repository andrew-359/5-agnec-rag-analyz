"""Render the QA results as a GitHub Actions job summary."""

import json
import os
from pathlib import Path
import re

BUILD = Path("qa/java/build")
STATUS = {
    "success": ("🟢 Прошёл", "Прошёл", "ok"),
    "failure": ("🔴 Сбой", "Сбой", "bad"),
    "skipped": ("⚪ Пропущен", "Пропущен", "idle"),
    "cancelled": ("⚪ Отменён", "Отменён", "idle"),
}
UNKNOWN = ("⚪ Нет статуса", "Нет статуса", "idle")


def read(path):
    return path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""


def browser_results():
    try:
        stats = json.loads(read(BUILD / "allure-report/widgets/summary.json"))["statistic"]
        return stats["total"], stats["passed"], stats["failed"] + stats["broken"], stats["skipped"]
    except (ValueError, KeyError, TypeError):
        return None


def backend_results(log):
    counters = dict(re.findall(r"(?m)^(?:ℹ|#) (tests|pass|fail|cancelled|skipped|todo) (\d+)$", log))
    if not {"tests", "pass", "fail"} <= counters.keys():
        return None
    value = {name: int(number) for name, number in counters.items()}
    return value["tests"], value["pass"], value["fail"] + value.get("cancelled", 0), value.get("skipped", 0) + value.get("todo", 0)


def worker_results(log):
    total = re.search(r"(?m)^Ran (\d+) tests? in ", log)
    outcome = re.search(r"(?m)^(?:OK|FAILED)(?: \(([^)]*)\))?$", log)
    if not total or not outcome:
        return None
    count = int(total[1])
    counters = {name: int(number) for name, number in re.findall(r"([a-z]+(?: [a-z]+)*)=(\d+)", outcome[1] or "")}
    failed = sum(counters.get(name, 0) for name in ("failures", "errors", "unexpected successes"))
    skipped = counters.get("skipped", 0) + counters.get("expected failures", 0)
    return count, count - failed - skipped, failed, skipped


def main():
    suites = [
        ("selenium", "Selenium", "Браузер", browser_results()),
        ("backend", "Node / API", "Загрузка, API, SQLite и ZIP", backend_results(read(BUILD / "ci/backend.log"))),
        ("workers", "Python", "Извлечение, OCR и проверка данных", worker_results(read(BUILD / "ci/workers.log"))),
    ]
    job_status = STATUS.get(os.getenv("QA_JOB_STATUS"), UNKNOWN)
    artifact = os.getenv("ARTIFACT_URL")
    lines = [
        "# RAG · QA", "",
        f"**{job_status[0]}** · Коммит `{os.environ['GITHUB_SHA'][:7]}` · Прогон №{os.environ['GITHUB_RUN_NUMBER']}",
        "",
    ]
    reported = [counts for _, _, _, counts in suites if counts is not None]
    if reported:
        total, passed, failed, skipped = map(sum, zip(*reported))
        lines += [f"**{passed} из {total} проверок прошли** · Сбои {failed} · Пропуски {skipped}", ""]
        if len(reported) < len(suites):
            lines += [f"Доступны результаты {len(reported)} из {len(suites)} наборов.", ""]
    if os.getenv("FAIL_PDF_SOURCE") == "true":
        lines += ["> **Диагностический режим.** Ошибка PDF включена для проверки вложений Allure.", ""]

    lines += ["## Пайплайн", "", "```mermaid", "flowchart LR", '    prepare["Подготовка"]']
    previous = "prepare"
    for key, label, _, counts in suites:
        _, state, style = STATUS.get(os.getenv(f"{key.upper()}_STATUS"), UNKNOWN)
        detail = f"{counts[1]}/{counts[0]} прошли" if counts and style == "ok" else state
        lines.append(f'    {previous} --> {key}["{label}<br/>{detail}"]:::{style}')
        previous = key
    artifact_label = "Allure и логи" if artifact else "Артефакт недоступен"
    artifact_style = "ok" if artifact else "idle"
    lines += [
        f'    {previous} --> report["{artifact_label}"]:::{artifact_style}',
        "    classDef ok fill:#dcfce7,stroke:#16a34a,color:#14532d",
        "    classDef bad fill:#fee2e2,stroke:#dc2626,color:#7f1d1d",
        "    classDef idle fill:#f1f5f9,stroke:#94a3b8,color:#334155",
        "```", "", "## Результаты", "",
        "| Набор | Что проверяет | Статус | Всего | Прошло | Сбои | Пропуски |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: |",
    ]
    for key, label, scope, counts in suites:
        icon, _, _ = STATUS.get(os.getenv(f"{key.upper()}_STATUS"), UNKNOWN)
        values = " | ".join(map(str, counts)) if counts is not None else "— | — | — | —"
        lines.append(f"| **{label}** | {scope} | {icon} | {values} |")
    lines += ["", "## Отчёт и логи", ""]
    if artifact:
        lines += [
            f"**[Скачать Allure и логи]({artifact})**",
            "",
        ]
        if os.getenv("PORTABLE_STATUS") == "success":
            if (BUILD / "open-in-browser/browser/index.html").is_file():
                lines.append("- `open-in-browser/browser/index.html` — текущий Selenium-прогон. Откройте файл в браузере и перейдите в **Behaviors**.")
            if (BUILD / "open-in-browser/load/index.html").is_file():
                lines.append("- `open-in-browser/load/index.html` — отчёт по сохранённому замеру D001 от 22 ноября 2026. Откройте **Behaviors** или **Suites**.")
            lines.append("")
        lines.append("В архиве также доступны обычный отчёт Selenium, JUnit XML и логи Node и Python.")
    else:
        lines.append("Артефакт недоступен. Подробности в логах шагов запуска.")
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as summary:
        summary.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
