"""Collect table areas and independent caption evidence from a Docling layout."""

from dataclasses import asdict, dataclass, replace
from hashlib import sha256
import re
from statistics import median

from .article_context.captions import TABLE_LINE, table_caption_lines
from .core.domain import Box
from .table_read_features import read_features
from .table_region import TableRegion, area, overlap, shared_area


@dataclass(frozen=True)
class RegionToken:
    text: str
    box: Box


@dataclass(frozen=True)
class Caption:
    reference: str
    text: str
    box: Box
    label: str
    designation: str


def _located_box(item, document, page: int) -> Box | None:
    from .strategies.parser import normalized_box

    locations = [location for location in item.prov if location.page_no == page]
    if len(locations) != 1:
        return None
    box = normalized_box(locations[0].bbox, document.pages[page].size)
    if box is None:
        return None
    left, top, right, bottom = (round(coordinate, 6) for coordinate in box)
    return (left, top, right, bottom) if left < right and top < bottom else None


def table_tokens(table, document, page: int, box: Box) -> list[RegionToken]:
    from docling_core.types.doc import TextItem

    stack = list(reversed(table.children))
    captions = {item.cref for item in table.captions}
    tokens = []
    while stack:
        reference = stack.pop()
        if reference.cref in captions:
            continue
        item = reference.resolve(document)
        stack.extend(reversed(item.children))
        if not isinstance(item, TextItem) or not item.text.strip():
            continue
        bounds = _located_box(item, document, page)
        if bounds and overlap(bounds, box) >= area(bounds) / 2:
            tokens.append(RegionToken(item.text.strip(), bounds))
    return tokens


def has_rows_and_columns(tokens: list[RegionToken]) -> bool:
    if len(tokens) < 4:
        return False
    height = median(token.box[3] - token.box[1] for token in tokens)
    rows: list[list[RegionToken]] = []
    row_origin = -1.0
    y = lambda token: (token.box[1] + token.box[3]) / 2
    for token in sorted(tokens, key=y):
        if y(token) - row_origin > height * .7:
            rows.append([])
            row_origin = y(token)
        rows[-1].append(token)

    # Match left, centre or right edges across rows. Numeric columns are often right aligned.
    def aligned(first: RegionToken, second: RegionToken) -> bool:
        left, _, right, _ = first.box
        start, _, end, _ = second.box
        return min(abs(left - start), abs(right - end), abs(left + right - start - end) / 2) <= height

    for upper, lower in zip(rows, rows[1:]):
        remaining = sorted(lower, key=lambda token: token.box[0])
        matched = []
        for first in sorted(upper, key=lambda token: token.box[0]):
            second = next((item for item in remaining if aligned(first, item)), None)
            if second is not None:
                matched.append(first)
                remaining.remove(second)
        if len(matched) >= 2 and (matched[-1].box[0] - matched[0].box[2]) >= height:
            return True
    return False


def _captions(document, page: int) -> list[Caption]:
    from docling_core.types.doc import DocItemLabel

    captions = []
    for item in document.texts:
        if not any(location.page_no == page for location in item.prov):
            continue
        text = item.text.strip()
        matches = list(table_caption_lines(text.splitlines()))
        match = matches[0][1] if len(matches) == 1 and matches[0][0] == 0 else None
        if match is None:
            # OCR often reads small-caps caption titles without a separator.
            match = TABLE_LINE.match(text.splitlines()[0]) if text else None
            if match is None or not (item.label == DocItemLabel.CAPTION or text.isupper()):
                continue
        if item.label != DocItemLabel.CAPTION and match['tail'] and re.match(
            r"\s+(?:shows?|presents?|reports?|provides?|summarizes?)\b", match['tail'], re.IGNORECASE,
        ):
            continue
        box = _located_box(item, document, page)
        if box:
            word, label = match['word'], match['label']
            captions.append(Caption(item.self_ref, text, box, label, f"{word.capitalize()} {label}"))
    return captions


def _attached_caption(box: Box, captions: list[Caption], neighboring: list[Box], references: set[str]):
    def distance(bounds: Box, caption: Caption, linked: bool = False) -> float | None:
        left, top, right, bottom = bounds
        start, upper, end, lower = caption.box
        width = min(right - left, end - start)
        if min(right, end) - max(left, start) < width / 2:
            return None
        gap = max(top - lower, upper - bottom)
        tolerance = -.012 if linked else -.004
        return max(0, gap) if tolerance <= gap <= .06 else None

    explicit = [caption for caption in captions if caption.reference in references and distance(box, caption, True) is not None]
    if len(explicit) == 1:
        return "confirmed", explicit[0]
    if explicit:
        return "needs_review", None

    matches = []
    for caption in captions:
        gap = distance(box, caption)
        if gap is None:
            continue
        competing = [distance(other, caption) for other in neighboring if other != box]
        if any(value is not None and value < gap for value in competing):
            continue
        matches.append((caption, any(value is not None and abs(value - gap) < .008 for value in competing)))
    if not matches:
        return "missing", None
    if len({caption.designation.casefold() for caption, _ in matches}) > 1 or any(ambiguous for _, ambiguous in matches):
        return "needs_review", None
    return "confirmed", min((caption for caption, _ in matches), key=lambda caption: distance(box, caption))


def _with_caption_title(caption: Caption, document, page: int, table: Box) -> Caption:
    from docling_core.types.doc import DocItemLabel

    if caption.text.casefold() != caption.designation.casefold():
        return caption
    marker = caption.box
    title_candidates = []
    for item in document.texts:
        text = item.text.strip()
        if (item.self_ref == caption.reference or item.label not in {
                DocItemLabel.CAPTION, DocItemLabel.PAGE_HEADER, DocItemLabel.SECTION_HEADER}
                or not text.isupper() or len(text.split()) < 3 or TABLE_LINE.match(text.splitlines()[0])):
            continue
        bounds = _located_box(item, document, page)
        if bounds is None:
            continue
        if (bounds[1] >= marker[1] - .008 and bounds[3] >= marker[3] + .004
                and 0 <= table[1] - bounds[3] <= .035
                and abs((bounds[0] + bounds[2]) - (marker[0] + marker[2])) <= .07):
            title_candidates.append((text, bounds))
    if len(title_candidates) != 1:
        return caption
    title, bounds = title_candidates[0]
    combined = (min(marker[0], bounds[0]), min(marker[1], bounds[1]),
                max(marker[2], bounds[2]), max(marker[3], bounds[3]))
    return replace(caption, text=f"{caption.text}\n{title}", box=combined)


def table_region_candidates(document, pages, digest, *, ocr: bool = True) -> list[dict]:
    candidates = []
    for page in sorted(pages.intersection(document.pages)):
        proposals = {}
        references: dict[Box, set[str]] = {}
        positioned: dict[Box, list[RegionToken]] = {}
        for table in document.tables:
            box = _located_box(table, document, page)
            if box is None:
                continue
            tokens = table_tokens(table, document, page, box)
            aligned = (table.data.num_rows > 1 and table.data.num_cols > 1 or has_rows_and_columns(tokens))
            proposals[box] = proposals.get(box, False) or aligned
            if box not in positioned or len(tokens) > len(positioned[box]):
                positioned[box] = tokens
            references.setdefault(box, set()).update(ref.cref for ref in table.captions)
        captions = _captions(document, page)
        clusters: list[list[Box]] = []
        for box in sorted(proposals):
            # A substantially overlapping alternative may be a different interpretation of the same area.
            matching = [cluster for cluster in clusters if any(
                overlap(box, other) >= .8 * min(area(box), area(other)) for other in cluster)]
            for cluster in matching:
                clusters.remove(cluster)
            clusters.append([box, *(item for cluster in matching for item in cluster)])

        primary = [min(cluster, key=lambda box: (not proposals[box], box)) for cluster in clusters]
        for box, cluster in zip(primary, clusters, strict=True):
            alternatives = tuple(sorted(other for other in cluster if shared_area(box, other) < .8))
            bounds_status = "confirmed" if proposals[box] and not alternatives else "needs_review"
            linked = {ref for other in cluster for ref in references[other]}
            caption_status, caption = _attached_caption(box, captions, primary, linked)
            if caption:
                caption = _with_caption_title(caption, document, page, box)
            region = TableRegion(box, bounds_status, caption_status, caption.designation if caption else None,
                                 caption.box if caption else None, caption.text if caption else None, alternatives)
            identity = sha256(repr(box).encode()).hexdigest()[:12]
            descriptor = asdict(region)
            if not alternatives:
                descriptor.pop("alternative_boxes")
            candidates.append({
                "id": f"{digest[:12]}-p{page}-region-{identity}",
                "kind": "table", "page": page, "label": caption.label if caption else None,
                "text": caption.text if caption else "", "box": box, "region": descriptor,
                "table_read_features": read_features(box, positioned[box], ocr=ocr),
                **({"discovery": "ocr"} if ocr else {}),
            })
    return candidates
