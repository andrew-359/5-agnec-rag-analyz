"""Evaluate one-to-one tabular matches against the fixed dev_gold subset."""
import argparse
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
import csv
from io import StringIO
import json
from pathlib import Path
import re
from zipfile import ZipFile

from entities import NUMBER, clean, entity_key, metric_key
from extract import load_csv, write_csv, write_json


def table_key(value):
    value = re.sub(r"^(?:table|tab\.?)[\s-]*", "", clean(value).casefold()).strip(". ")
    if re.fullmatch(r"[ivxlcdm]+", value):
        roman = dict(i=1, v=5, x=10, l=50, c=100, d=500, m=1000)
        digits = [roman[char] for char in value]
        return str(sum(-digit if i + 1 < len(digits) and digit < digits[i + 1] else digit
                       for i, digit in enumerate(digits)))
    return value


def numeric(text, *, leading=False):
    text = clean(text).replace("−", "-")
    match = re.match(NUMBER, text) if leading else re.fullmatch(NUMBER, text)
    if not match:
        return None
    try:
        value = Decimal(match[0].replace(",", ""))
    except InvalidOperation:
        return None
    return value if value.is_finite() else None


def number_keys(row):
    value = numeric(row["normalized_value"] or row["metric_value"])
    if value is None:
        value = numeric(row["raw_value"], leading=True)
    if value is None:
        return set()
    values = {value}
    unit = row["normalized_unit"] or row["metric_unit"]
    if unit == "%":
        values.add(value / 100)
    elif unit == "fraction":
        values.add(value * 100)
    return values


def tabular(rows):
    return [row for row in rows if row["source_table"] and not row["source_figure"]
            and row["extraction_status"] == "extracted"]


def attach_names(rows, configurations, datasets):
    methods = {(row["publication_id"], row["config_id"]): row["rag_variant"] or row["retrieval_method"]
               for row in configurations}
    names = {(row["publication_id"], row["dataset_id"]): row["dataset_name"] for row in datasets}
    return [{**row, "method_name": methods.get((row["publication_id"], row["config_id"]), ""),
             "dataset_name": names.get((row["publication_id"], row["dataset_id"]), "")}
            for row in rows]


def load_bundle(path):
    return attach_names(tabular(load_csv(path / "results.csv")),
                        load_csv(path / "configurations.csv"), load_csv(path / "datasets.csv"))


def load_stock(path, checksums):
    with ZipFile(path) as archive:
        def entry(name):
            found = [item for item in archive.namelist() if Path(item).name == name]
            if len(found) != 1:
                raise ValueError(f"Expected one {name} in {path}")
            return archive.read(found[0])

        def rows(name):
            return list(csv.DictReader(StringIO(entry(name).decode("utf-8-sig"))))

        manifest = json.loads(entry("manifest.json"))
        doc = manifest["submissionPublicationId"]
        if checksums.get(doc) != manifest["pdfSha256"]:
            raise ValueError(f"Stock PDF checksum differs: {path}")
        return doc, attach_names(tabular(rows("results.csv")), rows("configurations.csv"), rows("datasets.csv"))


def metric_identity(row):
    name = clean(row["metric_name"])
    aggregation = clean(row["aggregation"]).casefold().replace("_", " ")
    # Stock CSVs can carry the same statistic in aggregation that gold spells
    # out in the metric name. Keep distinct statistics out of the same bucket.
    suffix = re.search(r"\s+(sample SD|sample standard deviation|decrease|increase)$", name, re.I)
    statistic = suffix[1].casefold() if suffix else aggregation
    if suffix:
        name = name[:suffix.start()]
    if re.match(r"sample (?:sd|standard deviation)\b", statistic):
        statistic = "sample_sd"
    elif statistic not in {"decrease", "increase"}:
        statistic = ""
    return metric_key(name), statistic


def address(row):
    return (row["source_page"], table_key(row["source_table"]), metric_identity(row))


def method_key(text):
    return entity_key(text)


def dataset_key(text):
    key = entity_key(text)
    return {"2wiki": "2wikimultihopqa", "hotpot": "hotpotqa"}.get(key, key)


def condition(expected, predicted, key):
    field, normalize = key
    a, b = normalize(expected[field]), normalize(predicted[field])
    if not a:
        return "gold_open"
    if not b:
        return "open"
    return "correct" if a == b else "wrong"


def matching(before, after):
    gold_by_key = defaultdict(list)
    for j, row in enumerate(before):
        for number in number_keys(row):
            gold_by_key[(*address(row), number)].append(j)
    # Compare bare numbers at their reported scale. A cross-scale match requires
    # the factor to be declared on both sides, so a missing unit cannot rescue it.
    edges = {}
    for i, row in enumerate(after):
        value = numeric(row["normalized_value"] or row["metric_value"])
        if value is None:
            edges[i] = []
            continue
        candidates = gold_by_key[(*address(row), value)]
        unit = row["normalized_unit"] or row["metric_unit"]
        same = []
        scaled = []
        for j in candidates:
            gold = before[j]
            gold_value = numeric(gold["normalized_value"] or gold["metric_value"])
            gold_unit = gold["normalized_unit"] or gold["metric_unit"]
            if value == gold_value or gold_value is None:
                same.append(j)
            elif {unit, gold_unit} == {"%", "fraction"}:
                scaled.append(j)
        edges[i] = same + scaled

    claimed = {}

    def augment(i, visited):
        for j in edges[i]:
            if j in visited:
                continue
            visited.add(j)
            if j not in claimed or augment(claimed[j], visited):
                claimed[j] = i
                return True
        return False

    for i in sorted(edges, key=lambda index: (len(edges[index]), index)):
        augment(i, set())
    neighbors = Counter(j for group in edges.values() for j in group)
    ambiguous = {i for i, group in edges.items() if len(group) > 1 or any(neighbors[j] > 1 for j in group)}
    return claimed, edges, ambiguous


def ratio(num, denom):
    return round(num / denom, 6) if denom else ""


def evaluate(gold, predicted, grids):
    matches, edges, ambiguous = matching(gold, predicted)
    matched_output = set(matches.values())
    problems = Counter()
    examples = defaultdict(list)
    methods = Counter()
    datasets = Counter()
    unique_methods = Counter()
    unique_datasets = Counter()

    def problem(reason, row):
        problems[reason] += 1
        if len(examples[reason]) < 5:
            examples[reason].append({key: row[key] for key in ("result_id", "source_table", "source_page",
                                                             "metric_name", "raw_value", "source_fragment")})

    pred_locations = {address(row)[:2] for row in predicted}
    gold_addresses = {address(row) for row in gold}
    gold_locations = {key[:2] for key in gold_addresses}
    pred_addresses = {address(row) for row in predicted}
    for j, row in enumerate(gold):
        key = address(row)
        if j not in matches:
            if key[:2] not in grids:
                problem("FN_table_grid_missing", row)
            elif key[:2] not in pred_locations:
                problem("FN_table_without_predictions", row)
            elif key not in pred_addresses:
                problem("FN_metric_missing", row)
            else:
                problem("FN_value_or_occurrence_missing", row)
            continue
        i = matches[j]
        other = predicted[i]
        method = condition(row, other, ("method_name", method_key))
        dataset = condition(row, other, ("dataset_name", dataset_key))
        methods[method] += 1
        datasets[dataset] += 1
        if i not in ambiguous:
            unique_methods[method] += 1
            unique_datasets[dataset] += 1

    for i, row in enumerate(predicted):
        if i in matched_output:
            continue
        key = address(row)
        if edges[i]:
            problem("FP_extra_occurrence", row)
        elif key[:2] not in gold_locations:
            problem("FP_table_outside_extracted_gold", row)
        elif key not in gold_addresses:
            problem("FP_unmatched_metric", row)
        else:
            problem("FP_unmatched_value", row)
    selected_gold = {i: j for j, i in matches.items()}
    labeled = ("result_id", "method_name", "dataset_name", "source_fragment")
    ambiguity = [{"prediction": predicted[i]["result_id"],
                  "candidates": [gold[j]["result_id"] for j in edges[i]],
                  "selected": gold[selected_gold[i]]["result_id"] if i in selected_gold else None,
                  "prediction_fields": {key: predicted[i][key] for key in labeled},
                  "candidate_fields": [{key: gold[j][key] for key in labeled} for j in edges[i]],
                  "table": predicted[i]["source_table"], "page": predicted[i]["source_page"],
                  "metric": predicted[i]["metric_name"], "value": predicted[i]["metric_value"]}
                 for i in sorted(ambiguous)]
    return {"gold": len(gold), "predicted": len(predicted), "tp": len(matches),
            "fp": len(predicted) - len(matches), "fn": len(gold) - len(matches),
            "ambiguous_predictions": len(ambiguity),
            **{f"method_{k}": methods[k] for k in ("correct", "open", "wrong", "gold_open")},
            **{f"dataset_{k}": datasets[k] for k in ("correct", "open", "wrong", "gold_open")},
            **{f"unique_method_{k}": unique_methods[k] for k in ("correct", "open", "wrong", "gold_open")},
            **{f"unique_dataset_{k}": unique_datasets[k] for k in ("correct", "open", "wrong", "gold_open")},
            "unambiguous_tp": len(matched_output - ambiguous)}, {
        "ambiguities": ambiguity, "errors": dict(problems), "examples": examples,
        "matched_gold_ids": [gold[j]["result_id"] for j in sorted(matches)],
        "unambiguous_gold_ids": [gold[j]["result_id"] for j, i in sorted(matches.items()) if i not in ambiguous],
    }


def rates(row):
    for field, num, denom in (("precision", "tp", "predicted"), ("recall", "tp", "gold")):
        row[field] = ratio(row[num], row[denom])
    for prefix in ("method", "dataset", "unique_method", "unique_dataset"):
        row[prefix + "_accuracy"] = ratio(row[prefix + "_correct"],
                                          sum(row[prefix + "_" + k] for k in ("correct", "wrong", "open")))
    return row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("predictions", type=Path)
    parser.add_argument("gold", type=Path)
    parser.add_argument("inputs", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--stock", type=Path, action="append", default=[])
    args = parser.parse_args()
    rows = defaultdict(list)
    gold = defaultdict(list)
    for base, target in ((args.predictions, rows), (args.gold, gold)):
        for row in load_bundle(base):
            target[row["publication_id"]].append(row)
    if sum(map(len, gold.values())) != 5908 or len(gold) != 21:
        raise ValueError("Unexpected gold population; expected 21 articles and 5,908 extracted table records.")
    prepared = {item["doc"]: item for path in args.inputs.glob("D*.json")
                for item in (json.loads(path.read_bytes()),)}
    if set(prepared) != set(gold):
        raise ValueError("Freeze all 21 articles before scoring.")
    stock = dict(load_stock(path, {doc: item["sha256"] for doc, item in prepared.items()}) for path in args.stock)
    args.output.mkdir(parents=True, exist_ok=True)
    stats = []
    all_errors = Counter()
    details = {}
    for doc in sorted(gold):
        grids = {(str(item["page"]), table_key(item["label"])) for item in prepared[doc]["tables"]}
        article, detail = evaluate(gold[doc], rows[doc], grids)
        entry = {"article": doc, **rates(article)}
        all_errors.update(detail["errors"])
        details[doc] = detail
        if doc in stock:
            stock_counts, stock_details = evaluate(gold[doc], stock[doc], grids)
            a, b = set(detail["matched_gold_ids"]), set(stock_details["matched_gold_ids"])
            stable_a = set(detail["unambiguous_gold_ids"])
            stable_b = set(stock_details["unambiguous_gold_ids"])
            detail["stock"] = {"counts": rates(stock_counts), "wins": sorted(a - b), "losses": sorted(b - a),
                               "unambiguous_wins": sorted(stable_a - b), "unambiguous_losses": sorted(stable_b - a)}
            entry.update(**{f"stock_{k}": stock_counts[k] for k in ("predicted", "tp", "fp", "precision", "recall")},
                         stock_gold_wins=len(a-b), stock_gold_losses=len(b-a),
                         stock_unambiguous_wins=len(stable_a-b), stock_unambiguous_losses=len(stable_b-a))
        stats.append(entry)

    numeric = Counter()
    for entry in stats:
        numeric.update({k: v for k, v in entry.items() if type(v) is int and not k.startswith("stock_")})
    total = rates(dict(numeric))
    fields = list(dict.fromkeys(key for row in stats for key in row))
    write_csv(args.output / "per-article.csv", fields, stats)
    write_json(args.output / "summary.json", {"total": total, "errors": all_errors.most_common(),
               "stock_archives": list(map(str, args.stock))})
    write_json(args.output / "details.json", details)
    print(total)
    print("Top error groups:", all_errors.most_common(3))


if __name__ == "__main__":
    main()
