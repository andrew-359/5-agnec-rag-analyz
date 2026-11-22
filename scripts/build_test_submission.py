"""Replay completed test inputs and assemble one seven-CSV package offline."""
import argparse
from collections import Counter
from contextlib import nullcontext
from dataclasses import asdict
import csv
from hashlib import file_digest, sha256
import json
from pathlib import Path
import re
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from rag_mvp.article_context.selection import prepare_table_context
from rag_mvp.core.domain import Collection
from rag_mvp.reconciliation import report as reconcile
from rag_mvp.strategies.text_pdfplumber import load_article_fragments
from rag_mvp.table_extraction import _interpret_paddle_retry, interpret_grid
from rag_mvp.table_grid_artifact import load_table_grid
from rag_mvp.table_reading_retry import combine_readings
from rag_mvp.table_region import TableRegion
from scripts.submission_quotes import repair_citations


PUBLICATION_REVIEW = ROOT / ".scratch/test-submission-build/publication-review.json"
PUBLICATION_METADATA_FIELDS = ("title", "year", "DOI", "authors")
DOCS = tuple(f"P{i:03}" for i in range(1, 91))
REFERENCE_FIELDS = {"experiment_id": "experiments", "config_id": "configurations",
                    "dataset_id": "datasets", "task_id": "tasks", "duplicate_of": "results"}
TERMINAL = {"done", "failed", "skipped", "cancelled"}

# Keep this bridge thin. The existing TypeScript code owns publication semantics,
# including stable local IDs, partial rows, source links and CSV field mapping.
PUBLISH = """
import { readFile, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { buildPublication, columns, parsePublicationArtifact, publicationInputSchema }
  from './web/src/server/publication/publication-data.ts';
import { publishPartialTable } from './web/src/server/publication/partial-table.ts';
const [inputDir, outputDir, ...articles] = process.argv.slice(1);
for (const name of articles) {
  try {
    const packet = JSON.parse(await readFile(resolve(inputDir, name + '.json'), 'utf8'));
    const { artifacts, relationships, source, run_id: runId } = packet.report;
    const verified = artifacts.map(({id, kind, data}) => {
      const artifact = parsePublicationArtifact(data, id, source.sha256);
      return {id, runId, data: kind === 'table' ? publishPartialTable(artifact) : artifact};
    });
    const partialCandidates = verified.reduce((sum, item) => sum + item.data.data.results.filter(row => row.partial).length, 0);
    const hasResults = verified.some(item => item.data.data.results.length);
    const tables = hasResults ? buildPublication(publicationInputSchema.parse({
      publicationId: name, runId, pdfPath: packet.pdf, document: name + '.pdf',
      pdfSha256: source.sha256, publication: {title: packet.title},
      coverage: {partial: packet.partial, summary: 'Повтор по сохранённым артефактам'},
      relationships, artifacts: verified,
    }), 'version-' + runId, source.sha256).tables
      : Object.fromEntries(Object.keys(columns).map(table => [table, []]));
    const remarks = verified.reduce((sum, item) => sum + item.data.data.remarks.length, 0);
    const occupied = packet.passport ? Object.fromEntries(verified.filter(item => item.data.data.table != null)
      .map(({id, data: {data}}) => [id, [
        ...data.results.map(row => row.source),
        ...data.remarks.filter(row => row.kind === 'reading-conflict').map(row => row.source),
      ]])) : undefined;
    await writeFile(resolve(outputDir, name + '.json'), JSON.stringify({tables, partialCandidates, remarks, occupied}));
  } catch (error) {
    await writeFile(resolve(outputDir, name + '.json'), JSON.stringify({error: String(error)}));
  }
}
await writeFile(resolve(outputDir, 'columns.json'), JSON.stringify(columns));
"""


def load(path):
    return json.loads(path.read_bytes())


def write(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False) + "\n", encoding="utf-8")


def digest(path):
    with path.open("rb") as stream:
        return file_digest(stream, "sha256").hexdigest()


def read_publication_review(path, documents):
    # Bibliography was read and reviewed offline against this exact corpus.
    snapshot = path.read_bytes()
    review = json.loads(snapshot)
    entries = review["publications"]
    if review["version"] != 1 or set(entries) != set(DOCS):
        raise ValueError("Сверка публикаций должна содержать ровно P001–P090 по схеме 1.")
    for document in documents:
        name = document["article"]
        entry = entries[name]
        if entry["sha256"] != document["sha256"]:
            raise ValueError(f"{name}: сверка публикации относится к другой версии PDF.")
        scope = entry["scope"]
        if (type(scope["in_scope"]) is not bool or type(scope["page"]) is not int
                or scope["page"] < 1 or not scope["evidence"].strip()):
            raise ValueError(f"{name}: некорректная предметная сверка.")
        metadata = entry["metadata"]
        for field in PUBLICATION_METADATA_FIELDS:
            if field not in metadata:
                continue
            value = metadata[field]
            if not value["value"].strip() or not value["source"].strip():
                raise ValueError(f"{name}: нет значения или источника для {field}.")
        if "year" in metadata and not re.fullmatch(r"(?:19|20)\d{2}", metadata["year"]["value"]):
            raise ValueError(f"{name}: некорректный год публикации.")
        if "DOI" in metadata and not re.fullmatch(r"10\.\d{4,9}/\S+", metadata["DOI"]["value"]):
            raise ValueError(f"{name}: некорректный DOI публикации.")
    return entries, sha256(snapshot).hexdigest()


def attempt(roots, document, snapshot=None):
    name = document["article"]
    paths = {root / name / "01" for root in roots}
    pinned = []
    for entry in document.get("previous", ()):
        folder = Path(entry["run"]).resolve().parent
        if folder in paths:
            pinned.append((folder, entry.get("run_id")))
    order = [(roots[-1] / name / "01", None), *pinned]
    fallback = sorted(paths - {path for path, _ in order})
    if snapshot is not None:
        if not snapshot.get("input"):
            return None, []
        folder = Path(snapshot["input"]).resolve()
        if folder not in paths:
            raise ValueError(f"{name}: сохранённый путь вне заданных серий.")
        order, fallback = [(folder, snapshot["run_id"])], []
    errors = []
    ready = []
    for folder, expected in [*order, *((path, None) for path in fallback)]:
        run_path, saved_path = folder / "run.json", folder / "reconciliation.json"
        if not (run_path.is_file() and saved_path.is_file()):
            continue
        try:
            run, saved = load(run_path), load(saved_path)
            sha = document["sha256"]
            if (run["pdfSha256"] != sha or saved["source"]["sha256"] != sha
                    or saved["run_id"] != run["id"] or expected and expected != run["id"]):
                raise ValueError("Несовпадение SHA PDF или идентификатора прогона.")
            if run.get("reconciliationStatus") not in {None, "done"}:
                continue
            if any(stage.get("status") not in TERMINAL for stage in (*run["stages"], *run.get("figures", []))):
                continue
        except (OSError, ValueError, KeyError, TypeError) as error:
            errors.append(f"{folder}: {error}")
            continue
        selected = (folder, run, saved)
        if folder in {path for path, _ in order}:
            return selected, errors
        ready.append(selected)
    if len(ready) > 1:
        return None, [*errors, "Несколько завершённых попыток без приоритета в манифесте."]
    return (ready[0] if ready else None), errors


def replay_tables(folder, run, saved, scratch):
    sha = run["pdfSha256"]
    article_text = folder / "article-text.json"
    text = load_article_fragments(article_text, sha, expected_run_id=run["id"]) if article_text.exists() else None
    inputs = {"run.json": digest(folder / "run.json"), "reconciliation.json": digest(folder / "reconciliation.json")}
    if text:
        inputs[article_text.name] = digest(article_text)
    retained = [item for item in saved["artifacts"] if item["kind"] != "table"]
    old_tables = {item["id"]: item["data"] for item in saved["artifacts"] if item["kind"] == "table"}
    artifacts = []
    incomplete = []
    ctx_path = scratch / "article-context.json"
    for stage in run["stages"]:
        stage_id = stage["id"]
        old = old_tables.pop(stage_id, {})
        grid_stage = stage.get("gridRead") or {}
        attempts = [a for a in grid_stage.get("attempts", []) if a.get("hasGridArtifact")]
        if not attempts:
            if old.get("results"):
                raise ValueError(f"У подтверждённых записей этапа {stage_id} нет сохранённой сетки.")
            if old:
                retained.append({"id": stage_id, "kind": "table", "data": old})
            incomplete.append({"stage": stage_id, "reason": "без сохранённой сетки"})
            continue

        def load_grid(selected):
            path = folder / f"grid-{grid_stage['id']}-{selected['number']}.json"
            meta = load(path)
            inputs[path.name] = digest(path)
            region = TableRegion.parse(meta["region"]) if "region" in meta else None
            grid = load_table_grid(path, sha, stage["page"], stage["table"], meta["strategy"], region=region)
            return grid, meta["strategy"], region

        grid, strategy, region = load_grid(attempts[-1])
        reading_retry = old.get("reading_retry")
        if reading_retry:
            if len(attempts) < 2:
                raise ValueError(f"Для повторного чтения этапа {stage_id} отсутствует исходная сетка.")
            prior, prior_strategy, prior_region = load_grid(attempts[-2])
            if (prior_strategy, strategy) == ("docling-ocr-tableformer", "paddle-structure"):
                selection = prepare_table_context(text, sha, prior, region=prior_region)
                prior, earlier, _, _ = _interpret_paddle_retry(prior, grid, None, None, selection, ctx_path, region)
                collected = Collection((), grid.remarks)
            else:
                prior, earlier, _ = interpret_grid(prior, None, None,
                    prepare_table_context(text, sha, prior, region=prior_region), ctx_path)
                grid, collected, _ = interpret_grid(grid, None, None,
                    prepare_table_context(text, sha, grid, region=region), ctx_path)
            _, data, _ = combine_readings((prior_strategy, prior, earlier), (strategy, grid, collected), stage["page"])
        else:
            selection = prepare_table_context(text, sha, grid, region=region)
            grid, collected, _ = interpret_grid(grid, None, None, selection, ctx_path)
            data = grid.serialize(collected, stage["page"])
        if not grid.grid or not grid.grid.cells or grid_stage.get("status") == "failed" or stage.get("status") == "failed":
            incomplete.append({"stage": stage_id, "reason": "ошибка или пустая сетка чтения"})
        payload = {**old, **data, "page": stage["page"], "table": stage["table"], "strategy": strategy,
                   "source": {"sha256": sha}}
        if "region" not in payload and region:
            payload["region"] = asdict(region)
        artifacts.append({"id": stage_id, "kind": "table", "data": payload})
    if old_tables:
        raise ValueError(f"Табличные артефакты не сопоставлены с этапами: {', '.join(old_tables)}")
    return reconcile(run["id"], sha, [*artifacts, *retained], None), inputs, incomplete, text


def validated(tables, columns):
    keys = {"publications": "publication_id", "configurations": "config_id", "datasets": "dataset_id",
            "tasks": "task_id", "experiments": "experiment_id", "results": "result_id", "issues": "issue_id"}
    index = {}
    for table, key in keys.items():
        seen = set()
        for row in tables[table]:
            rest = set(row) - set(columns[table])
            pair = (row.get("publication_id"), row.get(key))
            if rest or not all(pair) or pair in seen:
                raise ValueError(f"{table}: лишние поля, пустой или повторный ключ {pair}: {sorted(rest)}")
            seen.add(pair)
        index[table] = seen
    excluded = {row["publication_id"] for row in tables["publications"] if row["in_scope"] == "false"}
    for table, rows in tables.items():
        for row in rows:
            doc = row["publication_id"]
            if (doc, doc) not in index["publications"]:
                raise ValueError(f"{table}: публикация {doc} отсутствует.")
            if table != "publications" and doc in excluded:
                raise ValueError(f"{table}: запись исключённой статьи {doc}.")
            if table == "results":
                refs = ((name, row.get(field)) for field, name in REFERENCE_FIELDS.items())
            elif table == "issues":
                refs = (("results", item.strip()) for item in row.get("record_ids", "").split(";"))
            else:
                continue
            for name, ref in refs:
                if ref and (doc, ref) not in index[name]:
                    raise ValueError(f"{table}: битая ссылка {doc}/{ref} в {name}.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf-dir", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--runs", type=Path, action="append", required=True,
                        help="Две прежние серии, затем каталог текущих results.")
    parser.add_argument("--mode", choices=("rehearsal", "final"), default="rehearsal")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--schema", type=Path, help="Схема участника. По умолчанию рядом с test/.")
    parser.add_argument("--publication-review", type=Path, default=PUBLICATION_REVIEW,
                        help="Предметная и библиографическая сверка для версии каждого PDF.")
    parser.add_argument("--expect-revision", help="Точная ревизия установленного экстрактора.")
    parser.add_argument("--input-snapshot", type=Path, help="build-report.json первого слоя с выбранными входами.")
    parser.add_argument("--passport-layer", action="store_true", help="Добавить второй слой извлечения по сохранённым сеткам.")
    parser.add_argument("--passport-dev-names", type=Path, help="Источник только словарных имён метрик и датасетов.")
    args = parser.parse_args()
    out = args.out.resolve()
    if out.exists():
        parser.error("Выходной каталог уже существует.")
    roots = [path.resolve() for path in args.runs]
    if len(roots) != 3 or len(set(roots)) != 3 or not all(path.is_dir() for path in roots):
        parser.error("Укажите три разных существующих каталога --runs.")
    docs = load(args.manifest)["documents"]
    progress_path = roots[-1].parent / "progress.json"
    finished = load(progress_path).get("finished", []) if progress_path.is_file() else []
    worker_revisions = {item.get("run_id"): item.get("revision") for item in finished}
    if len(docs) != len(DOCS) or {d["article"] for d in docs} != set(DOCS):
        parser.error("Манифест должен содержать ровно P001–P090.")
    review, review_sha = read_publication_review(args.publication_review, docs)
    pdf_dir = args.pdf_dir.resolve()
    if {p.stem for p in pdf_dir.glob("*.pdf")} != set(DOCS):
        parser.error("В test/ должны лежать ровно P001.pdf–P090.pdf.")
    schema = load(args.schema or pdf_dir.parent / "submission_template/schema.json")["tables"]
    snapshot = load(args.input_snapshot)["articles"] if args.input_snapshot else None
    if args.passport_dev_names and not args.passport_layer:
        parser.error("--passport-dev-names требует --passport-layer.")
    passport = None
    if args.passport_layer:
        from submission_passport import SubmissionPassport

        passport = SubmissionPassport(args.passport_dev_names)
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    tracked = ["rag_mvp", "web/src/server/publication", "scripts/build_test_submission.py", "scripts/submission_quotes.py"]
    if passport:
        tracked += ["scripts/submission_passport.py", "experiments/entity-passport"]
    changes = subprocess.check_output(["git", "status", "--porcelain", "--", *tracked], cwd=ROOT, text=True).splitlines()
    if args.expect_revision and (args.expect_revision != revision or changes):
        parser.error(f"Экстрактор не соответствует чистой закреплённой ревизии: {revision}")
    started = perf_counter()
    out.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".test-submission-", dir=out.parent) as temp:
        scratch = Path(temp)
        packets, bundles, final = (scratch / name for name in ("packets", "published", "bundle"))
        for path in (packets, bundles, final):
            path.mkdir()
        selected = {}
        stats = {}
        publications = {}
        for doc in sorted(docs, key=lambda item: item["article"]):
            name = doc["article"]
            pdf = pdf_dir / f"{name}.pdf"
            if digest(pdf) != doc["sha256"]:
                raise ValueError(f"{name}: PDF не совпадает с манифестом.")
            entry = review[name]
            scope, metadata = entry["scope"], entry["metadata"]
            publications[name] = {"publication_id": name, "in_scope": str(scope["in_scope"]).lower(),
                                  **{field: metadata[field]["value"] for field in PUBLICATION_METADATA_FIELDS
                                     if field in metadata}}
            missing = [field for field in PUBLICATION_METADATA_FIELDS if field not in metadata]
            stats[name] = {"pdf_sha256": doc["sha256"], "scope": scope, "metadata": metadata,
                           "missing_metadata": missing, "diagnostics": []}
            if entry.get("metadata_notes"):
                stats[name]["metadata_notes"] = entry["metadata_notes"]
            if not scope["in_scope"]:
                publications[name]["exclusion_reason"] = scope["evidence"]
                stats[name]["status"] = "excluded"
                continue
            picked, reasons = attempt(roots, doc, snapshot[name] if snapshot is not None else None)
            if snapshot is not None and snapshot[name].get("input") and not picked:
                raise ValueError(f"{name}: зафиксированный вход недоступен. {reasons}")
            stats[name].update(status="selected" if picked else "error" if reasons else "pending",
                               diagnostics=reasons)
            if picked:
                selected[name] = (pdf, picked)
                run_id = picked[1]["id"]
                if worker_revisions.get(run_id):
                    stats[name]["worker_revision"] = worker_revisions[run_id]
                else:
                    prior = next((entry for entry in doc.get("previous", ()) if entry.get("run_id") == run_id), {})
                    if prior.get("recorded_checkout_sha"):
                        stats[name]["recorded_checkout_revision"] = prior["recorded_checkout_sha"]
                        stats[name]["worker_sha_verified"] = bool(prior.get("worker_sha_verified"))
        selected_seconds = perf_counter() - started

        for name, (pdf, (folder, run, saved)) in selected.items():
            row = stats[name]
            row.update(input=str(folder), run_id=run["id"])
            try:
                reconciled, hashes, incomplete, text = replay_tables(folder, run, saved, scratch)
                if snapshot is not None and hashes != snapshot[name].get("inputs"):
                    raise ValueError(f"{name}: SHA входов отличаются от снимка.")
                row.update(inputs=hashes, incomplete_tables=incomplete, table_artifacts=sum(
                    item["kind"] == "table" for item in reconciled["artifacts"]))
                write(packets / f"{name}.json", {"report": reconciled, "pdf": str(pdf),
                                                 "title": publications[name].get("title") or name,
                                                 "partial": bool(incomplete),
                                                 **({"passport": True, "article_text": [asdict(x) for x in text.fragments]
                                                     if text else []} if passport else {})})
            except (OSError, ValueError, KeyError, TypeError) as error:
                if snapshot is not None:
                    raise
                row["status"] = "error"
                row["diagnostics"].append(str(error))
        replayed_seconds = perf_counter() - started
        names = sorted(path.stem for path in packets.glob("P*.json"))
        subprocess.run(["node", "--input-type=module", "-e", PUBLISH, str(packets), str(bundles), *names],
                       cwd=ROOT, check=True)
        columns = {key: fields.split(",") for key, fields in load(bundles / "columns.json").items()}
        if columns != schema:
            raise ValueError("Колонки штатного выпуска не совпадают со схемой участника.")
        combined = {table: [] for table in schema}
        passport_seconds = 0
        audit_context = (final / "passport-audit.jsonl").open("w", encoding="utf-8") if passport else nullcontext()
        with audit_context as audit:
            for name in DOCS:
                combined["publications"].append(publications[name])
                row = stats[name]
                row["rows"] = {table: int(table == "publications") for table in schema}
                if name not in names:
                    continue
                packet = load(bundles / f"{name}.json")
                if "error" in packet:
                    row["status"] = "error"
                    row["diagnostics"].append(packet["error"])
                    continue
                if passport:
                    start_passport = perf_counter()
                    payload = load(packets / f"{name}.json")
                    row["stock_rows"] = len(packet["tables"]["results"])
                    row["passport"] = passport.add(name, Path(payload["pdf"]), row["pdf_sha256"],
                                                    payload["article_text"], payload["report"]["artifacts"],
                                                    packet["occupied"], packet["tables"], audit)
                    passport_seconds += perf_counter() - start_passport
                for key, values in packet["tables"].items():
                    if key == "publications":
                        continue
                    if any(value.get("publication_id", name) != name for value in values):
                        raise ValueError(f"{name}: публикация вернула строки другой статьи.")
                    # The ZIP writer normally attaches the submission ID while producing CSV.
                    combined[key].extend({**value, "publication_id": name} for value in values)
                    row["rows"][key] = len(values)
                row["status"] = "published" if row["rows"]["results"] else "processed_empty"
                row["partial_candidates"] = packet["partialCandidates"]
                row["remarks_before_publication"] = packet["remarks"]
                if row["status"] == "processed_empty":
                    row["diagnostics"].append("После повтора и проверки частичного выпуска нет подходящих записей.")

        quote_typography = repair_citations(combined, pdf_dir)
        validated(combined, columns)
        for table, rows in combined.items():
            with (final / f"{table}.csv").open("w", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=columns[table], lineterminator="\n")
                writer.writeheader()
                writer.writerows(rows)
        counts = Counter(row["status"] for row in stats.values())
        ready = counts["published"] + counts["processed_empty"]
        complete = ready + counts["excluded"] == len(DOCS) if args.mode == "final" else ready >= 51
        summary = {"mode": args.mode, "complete_inputs": complete, "extractor": "interpret_grid",
                   "revision": revision, "uncommitted_extractor_paths": changes,
                   "publication_review": {"path": str(args.publication_review.resolve()),
                                          "sha256": review_sha},
                   "quote_typography": quote_typography,
                   "missing_metadata": {field: [name for name, row in stats.items() if field in row["missing_metadata"]]
                                        for field in PUBLICATION_METADATA_FIELDS},
                   "counts": dict(counts), "csv_rows": {key: len(rows) for key, rows in combined.items()},
                   "seconds": {"selection": round(selected_seconds, 3),
                               "replay": round(replayed_seconds - selected_seconds, 3),
                               "publication_and_csv": round(perf_counter() - started - replayed_seconds, 3)},
                   "articles": stats}
        if passport:
            summary["passport"] = {**passport.manifest, "seconds": round(passport_seconds, 3),
                                   "builder_sha256": digest(Path(__file__)),
                                   "adapter_sha256": digest(Path(__file__).with_name("submission_passport.py")),
                                   "stock_rows": sum(row.get("stock_rows", 0) for row in stats.values()),
                                   "added_rows": sum(row.get("passport", {}).get("added", 0) for row in stats.values())}
        write(final / "build-report.json", summary)
        final.rename(out)
        print(json.dumps({key: value for key, value in summary.items() if key != "articles"}, ensure_ascii=False))
        return 0 if summary["complete_inputs"] else 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError, TypeError, subprocess.CalledProcessError) as error:
        print(f"Ошибка сборки: {error}", file=sys.stderr)
        sys.exit(1)
