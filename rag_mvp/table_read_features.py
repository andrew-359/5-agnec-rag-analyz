"""Record the text layer and the OCR header shape of a detected table region."""

import re
from typing import TYPE_CHECKING

from .core.domain import Box

if TYPE_CHECKING:
    from .table_region_discovery import RegionToken


TOP_K = re.compile(r"Top\s*-\s*k\s*=\s*(\d+)", re.IGNORECASE)


def read_features(box: Box, tokens: list["RegionToken"], *, ocr: bool) -> dict:
    if not ocr:
        return {"text_layer": "usable", "header": "unconfirmed"}

    cutoff = box[1] + 0.4 * (box[3] - box[1])
    header = [token for token in tokens if (token.box[1] + token.box[3]) / 2 <= cutoff]
    method = [token for token in header if token.text.casefold() == "method"]
    if len(method) == 1:
        name = method[0]
        groups = [
            (token, match[1]) for token in header if (match := TOP_K.fullmatch(token.text))
            and token.box[0] > name.box[2]
            and min(token.box[3], name.box[3]) > max(token.box[1], name.box[1])
        ]
        if len(groups) >= 2 and len({value for _, value in groups}) == len(groups):
            labels = [token.text for token, _ in sorted(groups, key=lambda item: item[0].box[0])]
            return {"text_layer": "ocr-required", "header": "method-top-k", "header_labels": [name.text, *labels]}

    return {"text_layer": "ocr-required", "header": "other" if header else "unconfirmed"}
