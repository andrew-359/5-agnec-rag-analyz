"""Replay saved table grids and rank recurring open bindings by corpus coverage."""
import argparse
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter

# Support "python scripts/eval_batch.py" directly from a checkout.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rag_mvp.article_context.selection import prepare_table_context
from rag_mvp.strategies.text_pdfplumber import load_article_fragments
from rag_mvp.table_extraction import interpret_grid
from rag_mvp.table_grid_artifact import load_table_grid
from rag_mvp.table_region import TableRegion


def read_json(path):
    return json.loads(path.read_bytes())


def pdf_path(folder, pdf_dir):
    name = folder.parent.name + ".pdf"
    if pdf_dir is not None:
        return pdf_dir / name
    manifest = folder.parent / "manifest.json"
    if manifest.is_file():
        key = read_json(manifest).get("key")
        payload = json.loads(key) if isinstance(key, str) else key
        if payload and payload.get("input"):
            path = Path(payload["input"])
            return path if path.is_absolute() else folder.parent / path
    for path in (folder / "source.pdf", folder.parent / name):
        if path.is_file():
            return path
    return None


def header_at(grid, row, column):
    """Use the marked header cells above this position, including spanning cells."""
    headers = sorted({
        (cell.row, cell.column, cell.text.strip())
        for cell in grid.cells
        if cell.role == "column_header" and cell.text.strip()
        and cell.row < row and cell.column <= column < cell.column + cell.column_span
    }) if grid else []
    return len({level for level, _, _ in headers}) or None, [label for _, _, label in headers]


def group_remarks(groups, remarks, header_grid, doc, address, pdf):
    for remark in remarks:
        # Scale explanations and reading defects are reported separately from open bindings.
        if remark["kind"] != "ambiguous-binding":
            continue
        source = remark["source"]
        row, column = source["row"], source["column"]
        if row is None or column is None:
            continue
        depth, headers = header_at(header_grid, row, column)
        key = (remark["field"], depth)
        group = groups[key]
        group["cells"].add((*address, row, column))
        raw = " ".join(remark["raw_text"].split())
        if doc not in group["examples"] and re.match(r"^[+−–-]?(?:\d|\.\d)", raw):
            group["examples"][doc] = {
                "article": doc, "value": raw, "page": address[1], "table": address[2],
                "row": row, "column": column, "cell": f"cell-{row}-{column}",
                "source": f"{pdf.as_uri()}#page={address[1]}" if pdf else None,
                "box": source["box"], "headers": headers, "reason": remark["reason"],
            }


def prediction(row, page, table, region):
    return {
        "source_page": page,
        "source_table": region.designation if region and region.designation else f"Table {table}",
        "metric_name": row["metric"],
        "metric_k": row.get("metric_k"),
        "raw_value": row["raw_value"],
        "method": row.get("method"),
        "dataset": row.get("dataset"),
        "generator": row.get("generator"),
    }


def replay(source, pdf_dir, predictions=None):
    started = perf_counter()
    counts = Counter()
    remark_kinds = Counter()
    without_address = Counter()
    groups = defaultdict(lambda: {"cells": set(), "examples": {}})
    articles = []
    read_errors = []
    empty_grids = []

    with TemporaryDirectory(prefix="rag-eval-") as temporary:
        context_output = Path(temporary) / "article-context.json"
        for folder in sorted(source.glob("*/01")):
            if not (folder / "reconciliation.json").is_file():
                continue
            run = read_json(folder / "run.json")
            sha = run["pdfSha256"]
            doc = folder.parent.name
            text = load_article_fragments(folder / "article-text.json", sha)
            pdf = pdf_path(folder, pdf_dir)
            article = Counter()
            captured = None
            if predictions is not None:
                captured = {"article": doc, "results": [], "read_tables": [], "inputs": {
                    "pdf": sha,
                    "article-text.json": hashlib.sha256((folder / "article-text.json").read_bytes()).hexdigest(),
                }}
            for stage in run["stages"]:
                page, table = stage["page"], stage["table"]
                address = (doc, page, table)
                location = {"article": doc, "page": page, "table": table}
                grid_stage = stage.get("gridRead") or {}
                attempts = [item for item in grid_stage.get("attempts", []) if item.get("hasGridArtifact")]
                if grid_stage.get("status") == "failed":
                    article["recorded_read_errors"] += 1
                    read_errors.append({**location, "reason": grid_stage.get("error") or "Неизвестная ошибка чтения"})
                if not attempts:
                    article["missing_grids"] += 1
                    continue
                path = folder / f"grid-{grid_stage['id']}-{attempts[-1]['number']}.json"
                try:
                    if captured is not None:
                        captured["inputs"][path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
                    artifact = read_json(path)
                    region = TableRegion.parse(artifact["region"]) if "region" in artifact else None
                    if (artifact["page"], artifact["table"]) != (page, table):
                        raise ValueError("Адрес сетки не совпадает с этапом прогона.")
                    read = load_table_grid(path, sha, page, table, artifact["strategy"], region=region)
                except (OSError, ValueError, KeyError, TypeError) as error:
                    article["artifact_errors"] += 1
                    read_errors.append({**location, "reason": str(error), "artifact": str(path)})
                    continue
                article["grids"] += 1
                header_grid = read.grid
                if not (header_grid and any(cell.text.strip() for cell in header_grid.cells)):
                    article["empty_grids"] += 1
                    empty_grids.append(location)
                selected = prepare_table_context(text, sha, read, region=region)
                read, collected, _ = interpret_grid(read, None, None, selected, context_output)
                data = read.serialize(collected, page)
                if captured is not None:
                    if header_grid and any(cell.text.strip() for cell in header_grid.cells):
                        captured["read_tables"].append([page, table])
                    captured["results"].extend(prediction(row, page, table, region) for row in data["results"])
                result_count = len(data["results"])
                article["results"] += result_count
                article["tables_without_results"] += not result_count
                remark_kinds.update(remark["kind"] or "general" for remark in data["remarks"])
                without_address.update(
                    remark["kind"] or "general" for remark in data["remarks"]
                    if remark["source"]["row"] is None or remark["source"]["column"] is None
                )
                group_remarks(groups, data["remarks"], header_grid, doc, address, pdf)
            articles.append({"id": doc, **article})
            if captured is not None:
                predictions.append(captured)
            counts.update(article)

    if not articles:
        raise ValueError(f"Нет завершённых сохранённых прогонов */01 в {source}")
    top = []
    for (field, depth), group in groups.items():
        cells = group["cells"]
        top.append({
            "field": field, "header_depth": depth,
            "articles": len({cell[0] for cell in cells}),
            "tables": len({cell[:3] for cell in cells}),
            "cells": len(cells),
            "examples": list(group["examples"].values())[:2],
        })
    top.sort(key=lambda group: (-group["articles"], -group["cells"], group["field"] or "", group["header_depth"] or 0))
    return {
        "articles": len(articles), "counts": dict(counts), "remark_kinds": dict(remark_kinds),
        "remarks_without_address": dict(without_address), "empty_grids": empty_grids,
        "read_errors": read_errors, "per_article": articles, "top": top[:10],
        "seconds": round(perf_counter() - started, 2),
    }


def print_report(report):
    counts = report["counts"]
    print(f"Статьи {report['articles']} · сетки {counts.get('grids', 0)} · "
          f"подтверждённые записи {counts.get('results', 0)} · "
          f"таблицы без записей {counts.get('tables_without_results', 0)}")
    print(f"Пустые сетки {counts.get('empty_grids', 0)} · "
          f"ошибки чтения сетки в сохранённом прогоне {counts.get('recorded_read_errors', 0)} · "
          f"без артефакта {counts.get('missing_grids', 0)} · "
          f"ошибки загрузки артефакта {counts.get('artifact_errors', 0)}")
    print(f"Замечания без адреса {sum(report['remarks_without_address'].values())}: "
          f"{report['remarks_without_address']}")
    print(f"Замечания по видам: {report['remark_kinds']}")
    print("\nОткрытые привязки · статьи / таблицы / уникальные ячейки")
    for rank, group in enumerate(report["top"], 1):
        field = f"открыт {group['field']}" if group["field"] else "открытое поле неизвестно"
        depth = group["header_depth"]
        shape = f"уровней шапки над ячейкой {depth}" if depth else "форма шапки неизвестна"
        print(f"{rank:>2}. {field}, {shape} — {group['articles']} / {group['tables']} / {group['cells']}")
        for sample in group["examples"]:
            print(f"    {sample['article']} · {sample['value']} · стр. {sample['page']} · "
                  f"Table {sample['table']} · {sample['cell']} · {sample['source'] or 'PDF не указан'}")
            print(f"      Шапка: {' → '.join(sample['headers']) or 'не установлена'}. {sample['reason']}")
        if len(group["examples"]) < min(2, group["articles"]):
            print("    Других статей с числовым примером в этой группе нет.")
    print(f"\nВремя повтора {report['seconds']:.2f} с")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("series", type=Path, help="Каталог сохранённой серии")
    parser.add_argument("--pdf-dir", type=Path, help="Папка с оригинальными PDF для ссылок")
    parser.add_argument("--json", type=Path, help="Также сохранить полный отчёт в JSON")
    parser.add_argument("--predictions", type=Path, help="Сохранить табличные записи и SHA входов для score-dev.py")
    args = parser.parse_args()
    if not args.series.is_dir():
        parser.error(f"Каталог не найден: {args.series}")
    predictions = [] if args.predictions else None
    report = replay(args.series.resolve(), args.pdf_dir.resolve() if args.pdf_dir else None, predictions)
    print_report(report)
    if args.json:
        args.json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.predictions:
        args.predictions.write_text(json.dumps(predictions, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
