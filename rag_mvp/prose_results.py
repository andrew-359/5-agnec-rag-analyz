"""Extract reported values from the article text and retain their evidence."""

import argparse
import csv
import json
import re
import shutil
from collections import defaultdict
from collections.abc import Iterable
from contextlib import closing
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from hashlib import file_digest
from pathlib import Path
from tempfile import TemporaryDirectory

from .__main__ import write_json
from .article_context import load_article_fragments, read_article_fragments
from .article_context.captions import TABLE_LABEL, TABLE_WORD, table_caption_lines, text_for_matching
from .article_context.context import is_results_reference, sections
from .strategies.text_pdfplumber import ArticleFragments, validate_sources
from .failure_counts import add_failure_counts, reconcile_table_counts
from .run import Evidence


TABLE_REFERENCE = re.compile(rf"\b{TABLE_WORD}\s*(?:№\s*)?(?P<label>{TABLE_LABEL})(?!\w|[.\-]\w)", re.I)
REPORTED_GAIN = r"(?P<raw>(?P<number>\d+(?:\.\d+)?)\s*%)\s*(?P<metric>EM|F1)\b"
GAIN_COMPARISON = re.compile(
    rf"\b(?:outperforms?|surpasses?|exceeds?)\s+(?P<baseline>.+?)\s+by\s+"
    rf"(?:(?:up to|(?:an?\s+)?(?:substantial|significant|notable|absolute))\s+)?{REPORTED_GAIN}",
    re.I,
)
GAIN_NOUN = re.compile(rf"{REPORTED_GAIN}\s+(?:improvement|gain)\s+over\s+(?P<baseline>.+?)(?=\s+on\b|$)", re.I)
DATASET_SCOPE = re.compile(r"^\s+on\s+(?P<dataset>[^,();]+?)(?:\s*\((?P<generator>[^()]+)\)|[,;.]?$)", re.I)


@dataclass(frozen=True)
class Gain:
    raw: str
    printed: Decimal
    metric: str
    method: str | None
    baseline: str | None
    dataset: str | None
    generator: str | None


def identifier(value: str) -> str:
    return re.sub(r"[\s‐‑–-]+", "", value.casefold())


def named_entity(text: str, terms: set[tuple[str, str]], *, at_end: bool = False) -> str | None:
    found = set()
    for term, canonical in terms:
        name = r"[\s-]*".join(re.escape(part) for part in re.split(r"[\s-]+", term))
        suffix = r"$" if at_end else r"(?!\w)"
        if re.search(rf"(?<!\w){name}{suffix}", text, re.I):
            found.add(canonical)
    return next(iter(found)) if len(found) == 1 else None


def reported_gains(fragment: Evidence, methods: set[tuple[str, str]], datasets: set[tuple[str, str]]) -> Iterable[Gain]:
    sentences = re.split(r"(?<=[.!?])\s+(?=[A-Z])", text_for_matching(fragment.text))
    subject = named_entity(sentences[0], methods)
    for sentence in sentences:
        matches = (*GAIN_COMPARISON.finditer(sentence), *GAIN_NOUN.finditer(sentence))
        for match in sorted(matches, key=lambda item: item.start()):
            lead = sentence[:match.start()].strip()
            if match.re is GAIN_NOUN:
                lead = re.sub(r"\s+(?:achieves?|reports?|shows?)\s+(?:an?\s+)?$", "", lead + " ").strip()
            if re.search(r"\bit$", lead, re.I):
                method = subject
            else:
                method = named_entity(lead, methods, at_end=True)
                if named_entity(lead, methods) != method:
                    method = None
            subject = method
            scope = DATASET_SCOPE.match(sentence[match.end():])
            yield Gain(
                match["raw"], Decimal(match["number"]), match["metric"].upper(), method,
                named_entity(match["baseline"], methods),
                named_entity(scope["dataset"], datasets) if scope else None,
                scope["generator"] if scope else None,
            )


def table_scopes(fragments: tuple[Evidence, ...]) -> dict[str, tuple[str, dict[str, Evidence]]]:
    scopes = {}
    for section in sections(fragments):
        citations: dict[str, Evidence] = {}
        for fragment in section.fragments:
            text = text_for_matching(fragment.text)
            for match in TABLE_REFERENCE.finditer(text):
                label = match["label"]
                reference = re.compile(rf"\b{TABLE_WORD}\s*(?:№\s*)?{re.escape(label)}(?!\w|[.\-]\w)", re.I)
                if is_results_reference(text, reference):
                    citations[label.casefold()] = fragment
            scopes[fragment.id] = (section.key, citations.copy())
    return scopes


def score(row: dict) -> Decimal | None:
    if row.get("unit") not in {"fraction", "%"} or row.get("statistic") is not None:
        return None
    try:
        value = Decimal(row["value"])
    except (InvalidOperation, KeyError, TypeError):
        return None
    if row["unit"] == "%":
        value /= 100
    return value if value.is_finite() and 0 <= value <= 1 else None


def comparable_pairs(gain: Gain, table: dict) -> Iterable[tuple[dict, dict]]:
    candidates = [
        row for row in table["results"]
        if row.get("experiment") and row.get("task") and row["method"] in {gain.method, gain.baseline}
        and row["dataset"] == gain.dataset and row["metric"].upper() == gain.metric
        and row.get("generator") and identifier(row["generator"]) == identifier(gain.generator)
        and (row["source"]["page"], row["source"].get("table")) == (table["page"], table["table"])
        and score(row) is not None
    ]
    for target in candidates:
        if target["method"] != gain.method:
            continue
        for baseline in candidates:
            if baseline["method"] == gain.baseline and all(
                target.get(field) == baseline.get(field) for field in ("experiment", "task", "protocol")
            ):
                yield target, baseline


def table_terms(tables: Iterable[dict]) -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
    methods = set()
    datasets = set()
    for table in tables:
        for row in table["results"]:
            methods.add((row["method"], row["method"]))
            dataset = row["dataset"]
            datasets.add((dataset, dataset))
            headers = row["source"].get("headers", [])
            if len(headers) >= 2 and identifier(headers[-1]) == identifier(row["metric"]):
                if identifier(headers[-2]) and identifier(dataset).startswith(identifier(headers[-2])):
                    datasets.add((headers[-2], dataset))


    return methods, datasets


def build_prose_artifact(fragments: ArticleFragments, table_runs: Iterable[dict]) -> dict:
    """Use persisted table readings from the same PDF to confirm textual score gains."""
    validate_sources(fragments.sha256, fragments.fragments)
    tables = [run for run in table_runs if run.get("source", {}).get("sha256") == fragments.sha256]
    article_terms = table_terms(tables)
    captions = defaultdict(set)
    for fragment in fragments.fragments:
        for _, match in table_caption_lines(fragment.text.splitlines()):
            captions[match["label"].casefold()].add((fragment.id, fragment.page))
    scopes = table_scopes(fragments.fragments)
    evidence = {}
    results = []
    remarks = []
    for fragment in fragments.fragments:
        section, citations = scopes.get(fragment.id, (None, {}))
        cited_tables = []
        for index, table in enumerate(tables, 1):
            label = str(table.get("table", "")).casefold()
            citation = citations.get(label)
            locations = captions[label]
            if citation and len(locations) == 1 and next(iter(locations))[1] == table.get("page"):
                cited_tables.append((index, table, citation))
        terms = table_terms(table for _, table, _ in cited_tables) if cited_tables else article_terms
        for index, gain in enumerate(reported_gains(fragment, *terms), 1):
            observation = f"{fragment.id}-gain-{index}"
            source = {
                "page": fragment.page, "table": None, "box": fragment.box,
                "evidence_ids": [fragment.id],
            }
            if section:
                source["section"] = section
            evidence[fragment.id] = asdict(fragment)
            pairs = []
            has_conditions = all((gain.method, gain.baseline, gain.dataset, gain.generator)) and gain.method != gain.baseline
            if has_conditions:
                for run_index, table, citation in cited_tables:
                    pairs.extend((run_index, table, citation, target, baseline)
                                 for target, baseline in comparable_pairs(gain, table))
            if len(pairs) != 1:
                if not has_conditions:
                    reason = "В абзаце недостаточно сведений о методах, наборе данных или генераторе."
                else:
                    reason = ("Найдено несколько сопоставимых пар в таблицах этого раздела." if pairs else
                              "В таблицах, связанных с разделом, не найдена сопоставимая пара значений.")
                remarks.append({"id": observation, "raw_text": gain.raw, "reason": reason,
                                "kind": "ambiguous-binding", "source": source})
                continue

            run_index, table, citation, target, baseline = pairs[0]
            table_evidence = {item["id"]: item for item in table["evidence"]}
            cell_refs = []
            for row in (target, baseline):
                cell_refs.append(next((table_evidence[ref] for ref in row["source"].get("evidence_ids", ())
                                       if ref in table_evidence and table_evidence[ref]["page"] == row["source"]["page"]
                                       and table_evidence[ref]["precision"] in {"cell", "estimated_cell"}), None))
            if any(item is None for item in cell_refs):
                remarks.append({
                    "id": observation, "raw_text": gain.raw, "kind": "ambiguous-binding", "source": source,
                    "reason": "У сопоставимых значений отсутствует ссылка на исходную ячейку.",
                })
                continue

            evidence[citation.id] = asdict(citation)
            scale_ids = []
            for cell in cell_refs:
                ref = f"table-run-{run_index}-{cell['id']}"
                evidence[ref] = {**cell, "id": ref, "table": table["table"]}
                scale_ids.append(ref)
            target_score, baseline_score = score(target), score(baseline)
            difference = (target_score - baseline_score) * 100
            conditions = {"experiment": [citation.id], "gain_scale": scale_ids}
            tolerance = Decimal("0.5").scaleb(gain.printed.as_tuple().exponent)
            if difference <= 0 or gain.printed <= 0 or abs(difference - gain.printed) > tolerance:
                remarks.append({
                    "id": observation, "raw_text": gain.raw, "kind": "scale-interpretation", "source": source,
                    "condition_sources": conditions,
                    "reason": "Разница сопоставимых значений не подтверждает указанную величину в процентных пунктах.",
                })
                continue

            conditions.update({field: [fragment.id] for field in ("method", "baseline", "generator", "dataset", "metric")})
            for original in target.get("condition_sources", {}).get("retrieval_top_k", ()):
                if original in table_evidence:
                    ref = f"table-run-{run_index}-{original}"
                    evidence[ref] = {**table_evidence[original], "id": ref}
                    conditions.setdefault("retrieval_top_k", []).append(ref)
            results.append({
                "id": observation, "method": target["method"], "baseline_method": baseline["method"],
                "generator": target["generator"], "dataset": target["dataset"], "metric": target["metric"],
                "task": target["task"], "experiment": target["experiment"], "protocol": target.get("protocol"),
                "retrieval_top_k": target.get("retrieval_top_k"),
                "raw_value": gain.raw, "raw_unit": "%", "value": str(gain.printed), "unit": "percentage_points",
                "statistic": "increase", "metric_group": "comparative change",
                "metric_variant": f"improvement over {baseline['method']}", "baseline_value": str(baseline_score),
                "source": source, "source_fragment": fragment.text,
                "condition_sources": conditions,
                "note": (f"Прирост относительно {baseline['method']}. "
                         f"({target_score} − {baseline_score}) × 100 = {difference.normalize():f} п.п."),
            })
    return {"source": {"sha256": fragments.sha256}, "results": results, "remarks": remarks,
            "evidence": list(evidence.values())}


def save_prose_run(pdf: Path, table_runs: Iterable[dict], out: Path) -> dict:
    """Prepare the artifact and its printable source pages for the saved-run viewer."""
    from pypdfium2 import PdfDocument

    if out.exists():
        raise ValueError(f"Каталог прогона уже существует: {out}")
    out.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".rag-prose-", dir=out.parent) as temporary:
        run = Path(temporary)
        snapshot = run / "source.pdf"
        shutil.copyfile(pdf, snapshot)
        fragments = read_article_fragments(snapshot)
        artifact = build_prose_artifact(fragments, table_runs)

        observations = [*artifact["results"], *artifact["remarks"]]
        page = observations[0]["source"]["page"] if observations else 1
        pages = {page, *(item["page"] for item in artifact["evidence"])}
        previews = {number: "page.png" if number == page else f"page-{number}.png" for number in sorted(pages)}
        with PdfDocument(snapshot) as document:
            for number, filename in previews.items():
                with closing(document[number - 1]) as preview:
                    preview.render(scale=2).to_pil().save(run / filename)
        artifact["source"].update(name=pdf.name, pdf=snapshot.name, page=page,
                                  preview=previews[page], previews=previews)
        artifact.update(document=pdf.name, page=page, table=None, strategy="prose-comparison",
                        versions={"pdfplumber": fragments.engine_version})
        write_json(run / "run.json", artifact)

        columns = ("method", "baseline_method", "generator", "dataset", "task", "experiment", "metric",
                   "raw_value", "raw_unit", "value", "unit", "source_section", "page", "source_fragment", "note")
        with (run / "results.csv").open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, columns, extrasaction="ignore")
            writer.writeheader()
            for row in artifact["results"]:
                writer.writerow({**row, "page": row["source"]["page"], "source_section": row["source"].get("section")})
        for asset in Path(__file__).with_name("viewer").iterdir():
            shutil.copyfile(asset, run / asset.name)
        run.rename(out)
    return artifact


def main() -> None:
    parser = argparse.ArgumentParser(description="Извлечь числовые результаты из текста статьи.")
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--table-run", type=Path, action="append", default=[])
    parser.add_argument("--article-text", type=Path, help="Использовать сохранённый текст и записать только JSON.")
    parser.add_argument("--failure-counts", action="store_true",
                        help="Дополнить JSON наблюдениями о количестве разобранных ошибок.")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.failure_counts and not args.article_text:
        parser.error("--failure-counts requires --article-text")
    try:
        tables = [json.loads(path.read_text(encoding="utf-8")) for path in args.table_run]
        if args.article_text:
            with args.pdf.open("rb") as pdf:
                sha256 = file_digest(pdf, "sha256").hexdigest()
            fragments = load_article_fragments(args.article_text, sha256)
            comparison_tables = tables
            if args.failure_counts:
                comparison_tables = [{**run, "results": [row for row in run["results"]
                                                         if row.get("metric") != "failure count"]} for run in tables]
            artifact = build_prose_artifact(fragments, comparison_tables)
            artifact.update(strategy="prose-comparison", versions={"pdfplumber": fragments.engine_version})
            if args.failure_counts:
                add_failure_counts(artifact, fragments)
                reconcile_table_counts(artifact, tables)
                artifact["settings"] = {"failure_counts": True}
            write_json(args.out, artifact)
        else:
            if not args.table_run:
                parser.error("--table-run is required for the standalone viewer")
            artifact = save_prose_run(args.pdf, tables, args.out)
    except (ImportError, OSError, ValueError) as error:
        parser.exit(1, f"{error}\n")
    print(f"Результаты: {len(artifact['results'])}. Замечания: {len(artifact['remarks'])}. Прогон: {args.out}")


if __name__ == "__main__":
    main()
