"""Compare table policies on the complete dev_gold corpus and require the P020 control."""
import argparse
from collections import Counter
import csv
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re
import unicodedata

POLICIES = ("baseline", "updated")
DEV = {f"D{index:03}" for index in range(1, 22)}

def csv_rows(path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def compact(value):
    return "".join(unicodedata.normalize("NFKC", str(value or "")).casefold().split())


def designation(value):
    cleaned = compact(value)
    roman = {key: str(n) for n, key in enumerate(
        ("i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x"), start=1)}
    suffix = cleaned.removeprefix("table")
    return roman.get(suffix, suffix)


def printed(value):
    text = compact(value).replace("−", "-").replace(",", "").rstrip("†‡*")
    try:
        number = Decimal(text)
        return str(number.normalize()) if number.is_finite() else text
    except InvalidOperation:
        return text


def measurement(article, row):
    metric = compact(row["metric_name"])
    cutoff = row.get("metric_k") or ""
    if match := re.fullmatch(r"(.+?)@(\d+)", metric):
        metric, inline = match.groups()
        cutoff = cutoff or inline
    return (article, str(row["source_page"]), designation(row["source_table"]),
            metric, str(cutoff), printed(row["raw_value"]))


def exact_context(method, dataset, generator):
    return tuple(compact(v) for v in (method, dataset, generator))


def count_matches(gold, predictions):
    matches = sum((gold & predictions).values())
    n_gold, n_predictions = gold.total(), predictions.total()
    return {"matches": matches, "predictions": n_predictions, "gold": n_gold,
            "precision": matches / n_predictions if n_predictions else None,
            "recall": matches / n_gold if n_gold else None}


def article_index(rows):
    index = {}
    for row in rows:
        article = row["article"]
        if article in index:
            raise ValueError(f"Повтор статьи: {article}")
        index[article] = row
    return index


def paired(baseline, updated):
    before, after = article_index(baseline), article_index(updated)
    if before.keys() != after.keys():
        raise ValueError(f"Разный набор входов. Только до: {sorted(before.keys() - after.keys())}. "
                         f"Только после: {sorted(after.keys() - before.keys())}.")
    rows = []
    for article, left in before.items():
        right = after[article]
        if left["inputs"] != right["inputs"]:
            raise ValueError(f"{article}: SHA сохранённого PDF, текста или сеток изменился.")
        read_tables = [table for table in left["read_tables"] if table in right["read_tables"]]
        rows.append({"article": article, "baseline": left["results"], "updated": right["results"],
                     "read_tables": read_tables})
    return rows


def p020_control(comparison):
    row = next((r for r in comparison if r["article"] == "P020"), None)
    if row is None or [5, 1] not in row.get("read_tables", []):
        raise ValueError("P020, стр. 5 Table 1: нужен подтверждённый повтор непустой сетки.")
    return {mode: sum(str(r["source_page"]) == "5" and designation(r["source_table"]) == "1"
                      for r in row[mode]) for mode in POLICIES}


def score(comparison, gold_dir):
    index = article_index(comparison)
    corpus = set(index) - {"P020"}
    if corpus != DEV:
        raise ValueError(f"Вход покрывает {len(DEV & corpus)}/21 dev. "
                         f"Отсутствуют: {', '.join(sorted(DEV - corpus)) or '—'}. "
                         f"Другие ID: {', '.join(sorted(corpus - DEV)) or '—'}.")
    gold_ids = {r["publication_id"] for r in csv_rows(gold_dir / "publications.csv")}
    if gold_ids != DEV:
        raise ValueError("Ожидался полный эталон dev_gold с D001–D021.")
    control = p020_control(comparison)
    comparison = [index[article] for article in sorted(DEV)]
    configs = {(r["publication_id"], r["config_id"]): r for r in csv_rows(gold_dir / "configurations.csv")}
    datasets = {(r["publication_id"], r["dataset_id"]): r for r in csv_rows(gold_dir / "datasets.csv")}
    gold = [r for r in csv_rows(gold_dir / "results.csv") if r["extraction_status"] == "extracted" and r["source_table"]]
    expected = Counter(measurement(r["publication_id"], r) for r in gold)
    context_expected = Counter()
    for row in gold:
        article = row["publication_id"]
        config = configs.get((article, row["config_id"]), {})
        dataset = datasets.get((article, row["dataset_id"]), {})
        context = exact_context(config.get("rag_variant"), dataset.get("dataset_name"), config.get("generator_model"))
        context_expected[measurement(article, row) + context] += 1
    metrics = {}
    totals = {}
    for mode in POLICIES:
        observed = Counter()
        context_observed = Counter()
        for article in comparison:
            for row in article[mode]:
                if not row.get("source_table"):
                    continue
                key = measurement(article["article"], row)
                observed[key] += 1
                context_observed[key + exact_context(row.get("method"), row.get("dataset"), row.get("generator"))] += 1
        totals[mode] = observed
        metrics[mode] = {"measurements": count_matches(expected, observed),
                         "with_exact_method_dataset_generator": count_matches(context_expected, context_observed)}
    return {"article_ids": sorted(DEV), "p020_false_results": control, "policies": metrics,
            "per_article": {article: {mode: count_matches(
                Counter({key: count for key, count in expected.items() if key[0] == article}),
                Counter({key: count for key, count in totals[mode].items() if key[0] == article}),
            ) for mode in POLICIES} for article in sorted(DEV)},
            "new_table_predictions": (totals["updated"] - totals["baseline"]).total()}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("comparison", type=Path)
    parser.add_argument("dev_gold", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--updated", type=Path, help="Pair two prediction files written by eval_batch.py")
    parser.add_argument("--control", type=Path, help="Paired P020 input when not included in the dev files")
    args = parser.parse_args()
    try:
        comparison = json.loads(args.comparison.read_bytes())
        if args.updated:
            comparison = paired(comparison, json.loads(args.updated.read_bytes()))
        if args.control:
            comparison += json.loads(args.control.read_bytes())
        metrics = score(comparison, args.dev_gold)
    except (OSError, TypeError, KeyError, ValueError) as error:
        parser.exit(2, f"{error}\n")
    args.output.write_text(json.dumps(metrics, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps(metrics, ensure_ascii=False))
    if any(metrics["p020_false_results"].values()):
        parser.exit(1, "P020: найдены ложные измерения.\n")
