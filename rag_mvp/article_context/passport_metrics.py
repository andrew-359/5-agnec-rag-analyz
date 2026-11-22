"""Table-local metric definitions shared by the passport probe and release fallback."""
import re
import unicodedata


RANKED_SCORE = re.compile(r"\b(NDCG|Recall|Precision|Hit|MAP|MRR)@(\d+)\b", re.I)
FAILURE_RATE_LIST = re.compile(
    r"\b([A-Z][A-Z0-9]+)\s*,\s*([A-Z][A-Z0-9]+)\s*,?\s+and\s+([A-Z][A-Z0-9]+)\.?\s+denote\s+"
    r"unauthorized leakage,\s*stale citation,?\s+and\s+unsupported[- ]answer rates,\s*respectively\b", re.I,
)
FAILURE_RATE_UNIT = re.compile(r"\b(?:all )?values are failure rates\s*\(%\)", re.I)


def ranked_caption_metric(title):
    found = {match[0].casefold(): match for match in RANKED_SCORE.finditer(title)}
    if len(found) != 1:
        return None
    match, = found.values()
    if not re.search(rf"\b{re.escape(match[0])}\s+scores?\s+(?:for|across|on)\b", title, re.I):
        return None
    name = match[1].upper() if match[1].lower() in {"ndcg", "map", "mrr"} else match[1].capitalize()
    return f"{name}@{match[2]}", int(match[2])


def rate_code(text):
    return "".join(c for c in unicodedata.normalize("NFKC", text).casefold() if c.isalnum())


def declared_rate_names(title):
    declared = FAILURE_RATE_LIST.search(title)
    if declared is None:
        return {}
    codes = dict(zip(map(rate_code, declared.groups()),
                     ("Leakage rate", "Stale citation rate", "Unsupported answer rate"), strict=True))
    return codes if len(codes) == 3 else {}


def rate_columns(title, headers):
    codes = declared_rate_names(title)
    if not (codes and FAILURE_RATE_UNIT.search(title)):
        return {}
    scoped = {}
    for group in headers:
        if group["column_span"] != len(codes):
            continue
        children = [item for item in headers
                    if item["row"] == group["row"] + group["row_span"] and item["column_span"] == 1
                    and group["column"] <= item["column"] < group["column"] + group["column_span"]]
        if len(children) == len(codes) and {rate_code(item["text"]) for item in children} == codes.keys():
            scoped.update({item["id"]: (codes[rate_code(item["text"])], group) for item in children})
    return scoped
