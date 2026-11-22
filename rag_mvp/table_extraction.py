"""Read one table and assemble a saved extraction run."""

import hashlib
import shutil
from contextlib import closing
from dataclasses import asdict
from importlib.metadata import version
from pathlib import Path
from platform import python_version
from tempfile import TemporaryDirectory
from time import perf_counter

from .article_context.selection import CONTEXT_ARTIFACT, PreparedTableContext, load_table_context, prepare_table_context
from .core.domain import Box, Collection, TableTarget
from .run import ArticleContext, IncompatibleArtifact, TableRead, restrict_results
from .strategies.article_text_readers import read_article_fragments
from .run_output import write_csv, write_json
from .strategies.table_readers import DEFAULT_TABLE_READER, TABLE_READERS
from .table_region import TableRegion


def extract(
    pdf: Path, page: int, table_number: int, out: Path, strategy: str = DEFAULT_TABLE_READER, crop: Box | None = None,
    *, article_context: ArticleContext | None = None, with_article_context: bool = False,
    article_text: Path | None = None, grid_input: Path | None = None, grid_only: bool = False,
    context_input: Path | None = None,
    result_crop: Box | None = None, region: TableRegion | None = None,
    previous_grid_input: tuple[str, Path] | None = None,
) -> None:
    reader = TABLE_READERS.get(strategy)
    if reader is None:
        raise ValueError(f"Неизвестная стратегия чтения таблицы: {strategy}.")
    reader.validate_crop(crop, from_saved_grid=grid_input is not None)
    if region is not None and region.bounds_status != "confirmed":
        raise ValueError("Границы таблицы требуют проверки перед чтением.")
    if sum((with_article_context, article_context is not None, article_text is not None, context_input is not None)) > 1:
        raise ValueError("Выберите один источник контекста статьи.")
    if context_input is not None and grid_input is None:
        raise ValueError("Для повтора сохранённой подборки нужна сохранённая сетка таблицы.")
    if grid_only and (with_article_context or article_context is not None or article_text is not None or grid_input is not None):
        raise ValueError("Для чтения сетки нужен исходный PDF.")
    if previous_grid_input and (grid_input is None or previous_grid_input[0] == strategy):
        raise ValueError("Для дочитывания нужны сохранённые сетки двух разных стратегий.")

    from pypdfium2 import PdfDocument

    if out.exists():
        raise ValueError(f"Каталог прогона уже существует: {out}")
    if not pdf.is_file():
        raise ValueError(f"PDF не найден: {pdf}")
    out.parent.mkdir(parents=True, exist_ok=True)

    with TemporaryDirectory(prefix=".rag-run-", dir=out.parent) as temporary:
        run = Path(temporary)
        snapshot = run / "source.pdf"
        shutil.copyfile(pdf, snapshot)
        with snapshot.open("rb") as stream:
            sha256 = hashlib.file_digest(stream, "sha256").hexdigest()
        if article_context is not None and article_context.sha256 != sha256:
            raise ValueError("SHA-256 исходного PDF не совпадает с версией, из которой взяты цитаты.")
        article_text_metadata = None
        fragments = None
        if with_article_context or article_text is not None:
            from .strategies.text_pdfplumber import decode_article_fragments

            if article_text is None:
                fragments = read_article_fragments(snapshot)
            else:
                payload = article_text.read_bytes()
                try:
                    fragments = decode_article_fragments(payload, sha256)
                except (ValueError, KeyError, TypeError) as error:
                    raise IncompatibleArtifact(f"Несовместимый артефакт общего текста. {error}") from error
                article_text_metadata = {"sha256": sha256, "artifact_sha256": hashlib.sha256(payload).hexdigest(),
                                         "schema_version": fragments.schema_version}
        with PdfDocument(snapshot) as document:
            if page > len(document):
                raise ValueError(f"В PDF {len(document)} страниц, запрошена {page}.")
            result_region = None
            if result_crop is not None:
                width, height = document.get_page_size(page - 1)
                left, top, right, bottom = result_crop
                if not (0 <= left < right <= width and 0 <= top < bottom <= height):
                    raise ValueError(f"--result-crop должен задавать ненулевую область внутри страницы {page}.")
                result_region = (left / width, top / height, right / width, bottom / height)
            started = perf_counter()
            previous = None
            try:
                if grid_input is not None:
                    from .table_grid_artifact import load_table_grid

                    try:
                        read = load_table_grid(grid_input, sha256, page, table_number, strategy, region=region)
                        if previous_grid_input is not None:
                            previous_strategy, previous_path = previous_grid_input
                            previous = load_table_grid(previous_path, sha256, page, table_number, previous_strategy, region=region)
                    except (ValueError, KeyError, TypeError) as error:
                        raise IncompatibleArtifact(f"Несовместимая сетка таблицы. {error}") from error
                else:
                    read = reader.read(snapshot, page, table_number, crop, region)
            except ImportError as error:
                raise ValueError(f"Не удалось запустить {strategy}: {error}. Проверьте зависимости стратегии.") from error
            if grid_only:
                defects = list(dict.fromkeys(remark.reason for remark in read.collect().remarks
                                             if remark.kind == "reading-defect"))
                write_json(run / "table-grid.json", {
                    "schema_version": 1, "sha256": sha256, "page": page, "table": table_number,
                    "strategy": strategy, "read": asdict(read),
                    **({"reading_defects": defects} if defects else {}),
                    **({"region": asdict(region)} if region is not None else {}),
                })
                snapshot.unlink()
                run.rename(out)
                return
            if fragments is not None and fragments.pages != tuple(range(1, len(document) + 1)):
                raise IncompatibleArtifact("Сохранённый общий текст должен охватывать все страницы PDF.")
            paddle_recovery = previous is not None and (previous_strategy, strategy) == (
                "docling-ocr-tableformer", "paddle-structure",
            )
            selection = None
            if context_input is not None:
                selection = load_table_context(context_input, sha256, TableTarget(page, table_number), region, len(document))
            elif article_context is None and (fragments is not None or grid_input is not None):
                selection = prepare_table_context(
                    fragments, sha256, previous if paddle_recovery else read, region=region,
                )
            reading_retry = None
            if paddle_recovery:
                previous, earlier, reading_followup, recovered_count = _interpret_paddle_retry(
                    previous, read, article_context, result_region, selection, run / CONTEXT_ARTIFACT, region,
                )
                collected = Collection((), read.remarks)
                if result_region is not None:
                    collected = restrict_results(collected, result_region)
            else:
                read, collected, reading_followup = interpret_grid(
                    read, article_context, result_region, selection, run / CONTEXT_ARTIFACT,
                )
            results = collected.results
            raw_artifacts = read.raw_artifacts
            if previous is None:
                output = read.serialize(collected, page)
            else:
                from .table_reading_retry import combine_readings

                if not paddle_recovery:
                    previous, earlier, previous_followup = interpret_grid(
                        previous, article_context, result_region, selection, run / f"first-{CONTEXT_ARTIFACT}",
                    )
                results, output, reading_retry = combine_readings(
                    (previous_strategy, previous, earlier), (strategy, read, collected), page,
                )
                raw_artifacts = {**raw_artifacts, **{f"first-{name}": data for name, data in previous.raw_artifacts.items()}}
                if paddle_recovery:
                    reading_retry.update(recovered=recovered_count, preserved=len(results) - recovered_count)
                    if reading_followup:
                        raw_artifacts["article-context-followup.json"] = previous.raw_artifacts["article-context-followup.json"]
                elif reading_followup is None and previous_followup:
                    reading_followup = previous_followup
                    raw_artifacts["article-context-followup.json"] = previous.raw_artifacts["article-context-followup.json"]
            elapsed_ms = round((perf_counter() - started) * 1000)
            previews = {page: "page.png"}
            for fragment in output["evidence"]:
                number = fragment["page"]
                previews.setdefault(number, f"page-{number}.png")
            for number, filename in previews.items():
                with closing(document[number - 1]) as preview:
                    preview.render(scale=2).to_pil().save(run / filename)

        for name, data in raw_artifacts.items():
            if not (run / name).exists():
                write_json(run / name, data)
        write_json(run / "run.json", {
            "document": pdf.name,
            "page": page,
            "table": table_number,
            "strategy": strategy,
            **({"region": asdict(region)} if region is not None else {}),
            "versions": {"python": python_version(), "pypdfium2": version("pypdfium2"), **read.versions},
            "settings": {
                **read.settings,
                **({"article_context": True} if fragments is not None or (selection and selection.engine_version) else {}),
                **({"result_crop": result_crop} if result_crop is not None else {}),
            },
            **({"article_text": article_text_metadata} if article_text_metadata else {}),
            **({"reading_followup": reading_followup} if reading_followup else {}),
            **({"reading_retry": reading_retry} if reading_retry else {}),
            "elapsed_ms": elapsed_ms,
            "source": {"name": pdf.name, "sha256": sha256, "pdf": "source.pdf", "page": page,
                       "preview": previews[page], "previews": previews},
            "raw_artifacts": list(raw_artifacts),
            **output,
        })
        write_csv(run / "results.csv", results)
        for asset in (Path(__file__).parent / "viewer").iterdir():
            shutil.copyfile(asset, run / asset.name)
        run.rename(out)

    print(f"Результаты: {len(results)}. Замечания: {len(output['remarks'])}. Прогон: {out}")


def interpret_grid(
    read: TableRead, context: ArticleContext | None, result_region: Box | None,
    selection: PreparedTableContext | None, context_output: Path,
) -> tuple[TableRead, Collection, dict | None]:
    from .article_table_binding import apply_table_context, continue_article_context

    if selection is not None:
        write_json(context_output, selection.artifact())
        read, context = apply_table_context(read, selection)
    assertions = ()
    if context is not None:
        existing = {item.id: item for item in read.evidence}
        if any(item.id in existing and existing[item.id] != item for item in context.evidence):
            raise IncompatibleArtifact("Одинаковый ID у разных источников сетки и контекста.")
        read = read.with_article_evidence(tuple(item for item in context.evidence if item.id not in existing))
        assertions = context.assertions
    collected = read.collect(assertions, result_crop=result_region)
    if selection is not None and context is not None:
        return continue_article_context(selection, read, context, collected, result_region)
    return read, collected, None


def _interpret_paddle_retry(
    primary: TableRead, retry: TableRead, context: ArticleContext | None, result_region: Box | None,
    selection: PreparedTableContext | None, context_output: Path, region: TableRegion | None,
) -> tuple[TableRead, Collection, dict | None, int]:
    from .table_ocr_recovery import recover_cells

    interpreted, original, followup = interpret_grid(primary, context, result_region, selection, context_output)
    recovered, positions = recover_cells(primary, retry) if region else (primary, set())
    if positions:
        candidate, collected, candidate_followup = interpret_grid(
            recovered, context, result_region, selection, context_output,
        )
        # The retry may add source-backed cells but must preserve prior interpretations.
        unchanged = tuple(result for result in collected.results if (result.source.row, result.source.column) not in positions)
        original_open = tuple(remark for remark in original.remarks
                              if (remark.source.row, remark.source.column) not in positions)
        retained_open = tuple(remark for remark in collected.remarks
                              if (remark.source.row, remark.source.column) not in positions)
        if unchanged == original.results and retained_open == original_open:
            return candidate, collected, candidate_followup, len(collected.results) - len(original.results)
    return interpreted, original, followup, 0
