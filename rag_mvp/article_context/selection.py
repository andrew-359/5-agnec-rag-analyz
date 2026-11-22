"""The saved, target-specific input to article condition binding."""

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import get_args

from ..core.domain import Evidence, TableTarget
from ..run import IncompatibleArtifact, TableRead
from ..table_region import TableRegion
from .benchmark_table import benchmark_fragments
from .context import Basis, BasisKind, ContextCandidate, ContextStatus, TableContext, find_table_context, sections
from .fixed_depth import FixedDepth, fixed_depths
from ..strategies.text_pdfplumber import ArticleFragments, parse_text_fragment, validate_sources


CONTEXT_ARTIFACT = "article-context.json"
VERSION = 1


@dataclass(frozen=True)
class PreparedTableContext:
    context: TableContext
    target: TableTarget
    region: TableRegion | None
    engine_version: str | None
    setup_fragments: tuple[Evidence, ...] = ()
    followup_fragments: tuple[Evidence, ...] = ()
    fixed_depths: tuple[FixedDepth, ...] = ()
    local_basis: tuple[Basis, ...] = ()
    benchmark_evidence: tuple[Evidence, ...] = ()

    def artifact(self) -> dict:
        return {
            "schema_version": VERSION,
            **asdict(self.context),
            "target": {**asdict(self.target), "region": asdict(self.region) if self.region else None},
            "engine_version": self.engine_version,
            "setup_fragments": tuple(map(asdict, self.setup_fragments)),
            "followup_fragments": tuple(map(asdict, self.followup_fragments)),
            "fixed_depths": tuple(map(asdict, self.fixed_depths)),
            "local_basis": tuple(map(asdict, self.local_basis)),
            **({"benchmark_evidence": tuple(map(asdict, self.benchmark_evidence))} if self.benchmark_evidence else {}),
        }


def prepare_table_context(
    document: ArticleFragments | None, sha256: str, read: TableRead, *, region: TableRegion | None = None,
) -> PreparedTableContext | None:
    if document is not None and document.sha256 != sha256:
        raise ValueError("SHA-256 общего текста не совпадает с версией PDF прогона.")
    if read.table is None:
        return None
    target = TableTarget(read.table.page, read.table.number)
    label = region.author_label if region else str(target.number)
    caption = next((item for item in read.evidence
                    if item.precision == "text_fragment" and item.text.strip()
                    and item.id == read.table.caption_evidence_id and item.page == target.page
                    and (region is None or (item.box == region.caption_box and item.text == region.caption_text))), None)
    local_basis = (Basis("caption", caption),) if caption else ()
    if document is None or label is None:
        context = TableContext(sha256, label or str(target.number), target.page, "not_found", (),
                               "Доступны только основания из сохранённой сетки таблицы.")
        return PreparedTableContext(context, target, region, None, local_basis=local_basis)

    map_caption = None
    if region and region.caption_text and region.caption_box:
        map_caption = Evidence(f"{sha256[:12]}-p{target.page}-map-table-{target.number}", target.page,
                               region.caption_text, region.caption_box, "text_fragment")
    context = find_table_context(document, label, target.page, located_caption=map_caption)
    benchmark_evidence = benchmark_fragments(read.table, read.grid.cells, document.fragments) if caption and read.grid else ()
    if context.status != "supported" or len(context.candidates) != 1:
        return PreparedTableContext(context, target, region, document.engine_version,
                                    local_basis=local_basis, benchmark_evidence=benchmark_evidence)

    selection, = context.candidates
    article_sections = sections(document.fragments)
    setup = {basis.source.id for basis in selection.basis if basis.kind == "setup_section"}
    linked = setup | {basis.source.id for basis in selection.basis if basis.kind == "section"}
    used = {fragment.id for fragment in selection.fragments}
    captions = [basis.source for basis in selection.basis if basis.kind == "caption"]
    bounds = region.box if region and region.bounds_status == "confirmed" else None
    return PreparedTableContext(
        context, target, region, document.engine_version,
        tuple(fragment for section in article_sections if section.heading.id in setup for fragment in section.fragments),
        tuple(fragment for section in article_sections if section.heading.id in linked
              for fragment in section.fragments if fragment.id not in used),
        fixed_depths(article_sections, document.fragments, captions[0], bounds) if len(captions) == 1 else (),
        benchmark_evidence=benchmark_evidence,
    )


def _parse_basis(data: dict) -> Basis:
    kind = data["kind"]
    if kind not in get_args(BasisKind):
        raise ValueError("Неизвестное основание связи с таблицей.")
    return Basis(kind, parse_text_fragment(data["source"]))


def _parse_depth(data: dict) -> FixedDepth:
    depth, scope = data["depth"], data["scope"]
    rows = tuple(map(parse_text_fragment, data["rows"]))
    if type(depth) is not int or not 0 < depth < 10**9 or not isinstance(scope, str) or not scope.strip() or not rows:
        raise ValueError("Некорректная область глубины поиска.")
    return FixedDepth(scope, depth, parse_text_fragment(data["definition"]),
                      parse_text_fragment(data["declaration"]), rows)


def _parse_context(data: dict, sha256: str, label: str, page: int) -> TableContext:
    if data["sha256"] != sha256:
        raise ValueError("SHA-256 подборки не совпадает с версией PDF прогона.")
    status, reason = data["status"], data["reason"]
    if (not isinstance(reason, str) or not reason.strip()
            or status not in get_args(ContextStatus)):
        raise ValueError("Некорректный статус подборки.")
    candidates = tuple(
        ContextCandidate(tuple(map(parse_text_fragment, item["fragments"])), tuple(map(_parse_basis, item["basis"])))
        for item in data["candidates"]
    )
    expected_count = {"supported": (1,), "ambiguous": range(1, len(candidates) + 1), "not_found": (0,)}
    if data["table"] != label or type(data["page"]) is not int or data["page"] != page:
        raise ValueError("Подборка относится к другой таблице.")
    if len(candidates) not in expected_count[status]:
        raise ValueError("Число вариантов противоречит статусу подборки.")
    return TableContext(sha256, label, page, status, candidates, reason)


def load_table_context(
    path: Path, sha256: str, target: TableTarget, region: TableRegion | None, pages: int,
) -> PreparedTableContext:
    try:
        data = json.loads(path.read_bytes())
        if type(data["schema_version"]) is not int or data["schema_version"] != VERSION:
            raise ValueError("Неизвестная версия формата подборки.")
        address = data["target"]
        saved_region = TableRegion.parse(address["region"]) if address["region"] is not None else None
        if (type(address["page"]) is not int or type(address["number"]) is not int
                or TableTarget(address["page"], address["number"]) != target or saved_region != region):
            raise ValueError("Подборка относится к другой таблице.")
        engine = data["engine_version"]
        if engine is not None and (not isinstance(engine, str) or not engine):
            raise ValueError("Не указана версия чтения общего текста.")
        label = (region.author_label if region else None) or str(target.number)
        context = _parse_context(data, sha256, label, target.page)
        setup = tuple(map(parse_text_fragment, data["setup_fragments"]))
        followup = tuple(map(parse_text_fragment, data["followup_fragments"]))
        depths = tuple(map(_parse_depth, data["fixed_depths"]))
        local_basis = tuple(map(_parse_basis, data["local_basis"]))
        benchmark_evidence = tuple(map(parse_text_fragment, data.get("benchmark_evidence", ())))
        if engine is None and (context.status != "not_found" or setup or followup or depths or benchmark_evidence):
            raise ValueError("У подборки из сетки не может быть связей с общим текстом.")
        sources = [fragment for item in context.candidates
                   for fragment in (*item.fragments, *(basis.source for basis in item.basis))]
        sources.extend(basis.source for basis in local_basis)
        sources.extend((*setup, *followup, *benchmark_evidence))
        sources.extend(fragment for item in depths for fragment in (item.declaration, item.definition, *item.rows))
        validate_sources(sha256, sources)
        if any(item.page > pages for item in sources):
            raise ValueError("Страница фрагмента находится за пределами PDF.")
        return PreparedTableContext(context, target, saved_region, engine, setup, followup, depths, local_basis, benchmark_evidence)
    except (IndexError, KeyError, OSError, TypeError, ValueError) as error:
        raise IncompatibleArtifact(f"Несовместимая подборка контекста. {error}") from error
