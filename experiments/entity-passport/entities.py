"""Versioned text and grid heuristics for the aggressive probe."""
from collections import defaultdict
import re
import unicodedata

VERSION = "entity-passport-aggressive-v2"
COMMON_METRICS = (
    "EM", "F1", "Accuracy", "Recall", "Precision", "Hit Rate", "MRR", "MAP", "NDCG",
    "AUC", "BLEU", "ROUGE", "ROUGE-L", "METEOR", "BERTScore", "Latency", "Perplexity",
)
METRIC_ALIASES = {
    "acc": "accuracy", "accur": "accuracy", "exactmatch": "em", "emscores": "em",
    "emscore": "em", "f1score": "f1", "hitratio": "hitrate", "hitsrate": "hitrate",
    "rec": "recall", "pre": "precision", "meanreciprocalrank": "mrr", "ppl": "perplexity",
    "factualityscore": "factuality", "hallucination": "hallucinationrate",
    "missing": "missingresponserate", "unverified": "unverifiedresponserate",
}
NON_METRIC_LABELS = {
    "document", "documents", "qapair", "qapairs", "query", "queries", "sample", "samples",
    "largelanguagemodel", "largelanguagemodels", "llm", "llms",
}
METHODS = ("BM25", "BGE", "DPR", "Contriever", "ColBERT", "TF-IDF", "Dense Retrieval",
           "Hybrid Retrieval", "Naive RAG", "Vanilla RAG", "RAG", "No RAG", "Non-RAG", "Self-RAG")
MODEL = re.compile(
    r"(?<![\w-])(?:Llama|Qwen|GPT|Claude|Mistral|Ministral|DeepSeek|Gemma|Gemini|FLAN[- ]?T5|"
    r"Vicuna|Mixtral|Phi|PaLM|Yi)(?:[-. ][\w.-]+)*?"
    r"(?:[-. ](?:v?\d[\w.-]*|(?:Sonnet|Haiku|Opus|Flash|Pro|Turbo|Mini)(?!\w)))(?:[-.][\w.-]+)*",
    re.I,
)
MODEL_FAMILY = re.compile(r"(?:Llama|Qwen|GPT|Claude|Mistral|Ministral|DeepSeek|Gemma|"
                          r"Gemini|Vicuna|Mixtral|Phi|PaLM|Sonnet|Haiku|Opus|FLAN[- ]?T5(?:-X{1,2}L)?)", re.I)
SHORT_MODEL = re.compile(r"(?:[A-Z]{1,4}\d[\w.]*[-_]\d+(?:\.\d+)?[bB]|L\d+\.\d+[-_]\d+[bB])\b")
DEFINITION = re.compile(r"\b([A-Z][A-Za-z-]*(?:[ -][A-Za-z][A-Za-z-]*){0,4})\s+\(([A-Z][A-Za-z0-9-]{1,11})\)")
SECTION = re.compile(r"^(?:[\d.]+\s+)?(?:Datasets?|Benchmarks?|Evaluation(?: Datasets?)?)\.?\s*(?:\n|:|$)", re.I)
NAMED_DATASET = re.compile(
    r"\b([A-Za-z][\w.-]*(?:-[\w.-]+)?)\s+(?:datasets?|benchmarks?)\b|"
    r"\b(?:dataset|benchmark)(?:\s+(?:called|named))?\s*[:,]\s*([A-Za-z][\w.-]*)",
    re.I,
)
CANDIDATE_NAME = re.compile(r"\b(?:[A-Z]{2,}[A-Za-z0-9]*-[A-Za-z0-9-]+|[A-Z]+[a-z]+[A-Z][A-Za-z0-9]*)\b")
SKIP_ENTITY = {"the", "each", "our", "test", "training", "single", "standard", "same", "this",
               "development", "benchmark", "dataset", "table", "challenging", "different"}
SYSTEM = re.compile(r"\b(?:we (?:propose|introduce|present|develop)|(?:called|named|dubbed))\s+"
                    r"(?:the\s+)?([A-Z][\w-]{2,})")
BASELINES = re.compile(r"\bbaselines?\s*(?:include|are|:|,)?\s*([^.!?;]{3,155})", re.I)
DESCRIPTIVE = re.compile(
    r"^(?:dataset\s+|corpus\s+|data\s+|sample\s+)?statistics\b|"
    r"\b(?:statistics of (?:the |our )?(?:datasets?|corpus|samples?)|"
    r"hyper[- ]?parameter(?:s| settings)?|training (?:parameters|configuration)|"
    r"dataset (?:composition|statistics)|examples? of (?:prompts?|questions?|outputs?)|"
    r"prompt templates?|number of (?:questions|documents|samples) (?:by|per))\b", re.I,
)
NUMBER = r"[+−-]?(?:\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?|\.\d+)(?:[eE][+−-]?\d+)?"
SCALAR = re.compile(rf"^(?P<value>{NUMBER})\s*(?P<unit>%|×|ms|s)?\s*[∗*†‡★]*$", re.I)
COMPOUND = re.compile(rf"^(?P<value>{NUMBER})\s*(?P<unit>%|×)?\s*(?:\(\s*(?:±\s*)?"
                      rf"(?P<sd>{NUMBER})\s*\)|±\s*(?P<plus>{NUMBER}))\s*[∗*†‡★]*$")
DELTA = re.compile(rf"^(?P<value>{NUMBER})\s*(?P<unit>%|×)?\s*\(\s*[↑↓]\s*{NUMBER}\s*%\s*\)$")
EXPLICIT_UNIT = re.compile(r"\(\s*(%|ms|s|seconds?|tokens?|usd|×)\s*\)|%|\bmilliseconds\b", re.I)
K = re.compile(r"@(\d+)(?![\d.])\b")


CAPTION_METRICS = re.compile(
    r"(?<![\w-])(?:(?P<accuracy>ACC(?:uracy)?)\s*\(\s*%\s*\)|"
    r"(?P<winning>winning rates?)\s*\(\s*%\s*\)|"
    r"(?P<refusal>refusal rate)s?(?:\s*(?P<refusal_percent>\(\s*%\s*\)))?|(?P<compliance>compliance rate)s?\s*\(\s*%\s*\)|"
    r"(?P<runtime>wall[- ]clock runtime))(?![\w-])",
    re.I,
)
EXPLICIT_HEADER = re.compile(
    r"[A-Za-z]+[-–](?:Acc|Pre|Rec|F1)|Accuracy\s*@\s*0\.\d+|@\s*0\.\d+\s+Accuracy|"
    r"Best recall|Cosine Sim(?:ilarity)?(?:\s+Avg)?|BLEU-AVG",
    re.I,
)
COMPACT_RATE = re.compile(r"UnsafeRate|EngWinRate|CatalogUngrdRate|CtxUngrdRate", re.I)
EVALUATION_HEADER = re.compile(
    r"Coverage|(?:Textual|Visual) RAG Score|Item[-– ]wise Score|"
    r"Semantic Similarity(?:\s*\((?:smaller|larger|higher|lower) is better\))?",
    re.I,
)
CORRECTNESS_SCALE = re.compile(
    r"(Correctness)\s*\([+-]?\d+(?:\.\d+)?:\s*worst,?\s*[+-]?\d+(?:\.\d+)?:\s*best\)", re.I,
)
HEADER_METRIC_NAMES = re.compile(
    r"(?<![\w-])(?:ROUGE(?:\s*[-–]\s*[12L])?|BLEU(?:\s*[-–]\s*[1-4])?|"
    r"Recall(?:\s*@\s*\d+)?|F1)(?![\w-]|\.\d)", re.I,
)
CAPTION_NAMES = {
    "accuracy": ("Accuracy", "%"), "winning": ("Winning rate", "%"),
    "compliance": ("Compliance rate", "%"),
    "runtime": ("Wall-clock runtime", None),
}


def clean(text):
    return " ".join(unicodedata.normalize("NFKC", text or "").split())


def entity_key(text):
    return "".join(char for char in clean(text).casefold() if char.isalnum())


def metric_key(text):
    text = clean(text).casefold().strip()
    text = re.sub(r"^r\s*(?=@)", "recall", text)
    text = re.sub(r"^hits\s*(?=@)", "hit", text)
    text = re.sub(r"\(\s*(?:%|ms|s|seconds?|tokens?|usd|×)\s*\)", "", text)
    text = "".join(char for char in text if char.isalnum() or char in "@.")
    text = text.strip(".")
    base, separator, cutoff = text.partition("@")
    return METRIC_ALIASES.get(base, base) + separator + cutoff


def token_in(value, text):
    return bool(re.search(rf"(?<![\w-]){re.escape(value)}(?![\w-])", text, re.I))


def sources_of_lexicon(rows, fields):
    counts = defaultdict(set)
    for row in rows:
        for field in fields:
            if name := clean(row.get(field, "")):
                counts[name].add(row["publication_id"])
    return counts


def passport(record, metric_docs, dataset_docs):
    doc = record["doc"]
    output = {kind: {} for kind in ("metrics", "non_metrics", "datasets", "models", "methods")}

    def add(kind, display, source=None, alias=None):
        key = (metric_key if kind == "metrics" else entity_key)(alias or display)
        if kind == "metrics":
            name = metric_key(display)
            # Check the expanded name before admitting its acronym as a metric.
            if name in NON_METRIC_LABELS or name.endswith("watermarking"):
                kind = "non_metrics"
        if key:
            previous = output[kind].get(key)
            if previous is None or previous["source"] is None and source is not None:
                output[kind][key] = {"value": display, "source": source}

    corpus_metrics = sorted(name for name, owners in metric_docs.items() if owners - {doc})
    corpus_datasets = sorted(name for name, owners in dataset_docs.items() if owners - {doc})
    for metric in (*COMMON_METRICS, *corpus_metrics):
        add("metrics", metric)
    for dataset in corpus_datasets:
        add("datasets", dataset)
    for method in METHODS:
        add("methods", method)
    # Whole-article aliases are selected from the PDF only. The lexicon discloses no
    # associations between held-out values, labels, tables or publications.
    for position, source in enumerate(record["text"]):
        if not source.get("box"):
            continue
        text = clean(source["text"])
        for match in MODEL.finditer(text):
            model = match[0].strip(". ")
            add("models", model, source)
            suffix = text[match.end():]
            if alias := re.match(r"\s*\((?:denoted as\s+)?([\w.-]+)\)", suffix, re.I):
                if SHORT_MODEL.fullmatch(alias[1]) or any(char.isdigit() for char in alias[1]):
                    add("models", model, source, alias[1])
        for alias in SHORT_MODEL.findall(text):
            add("models", alias, source)
        for first, second in NAMED_DATASET.findall(text):
            name = first or second
            if name.casefold() not in SKIP_ENTITY and (any(c.isupper() for c in name) or "-" in name):
                add("datasets", name, source)
        if SECTION.match(source["text"]):
            for name in CANDIDATE_NAME.findall(text):
                add("datasets", name, source)
        for long, short in DEFINITION.findall(text):
            words = long.split()
            full = None
            for index in range(len(words)):
                key = metric_key(" ".join(words[index:]))
                full = output["metrics"].get(key) or output["non_metrics"].get(key)
                if full:
                    break
            acronym = "".join(word[0] for word in words).upper()
            if full or acronym == short:
                add("metrics", full["value"] if full else long, source, short)
                entry = output["metrics"].get(metric_key(short))
                if full and entry and entry["source"] is source and metric_key(short) != metric_key(full["value"]):
                    entry["defined_abbreviation"] = short
        for method in SYSTEM.findall(text):
            add("methods", method, source)
        if position < 2:
            for name in re.findall(r"\b[\w-]*RAG[\w-]*\b", text):
                add("methods", name, source)
        for segment in BASELINES.findall(text):
            for name in re.split(r"\s*,\s*|\s+and\s+", segment):
                name = re.sub(r"\s*\([^)]*\)\s*$", "", name).strip()
                if re.fullmatch(r"[A-Z][\w-]{2,}", name):
                    add("methods", name, source)
    return output


def caption_indicators(text):
    for match in CAPTION_METRICS.finditer(text):
        if match["refusal"]:
            yield "Refusal rate", "%" if match["refusal_percent"] else None
        else:
            yield CAPTION_NAMES[match.lastgroup]


def evaluation_header_metric(text):
    label = EXPLICIT_UNIT.sub("", clean(text)).strip(" ↑↓*†‡")
    if match := CORRECTNESS_SCALE.fullmatch(label):
        return match[1]
    if EVALUATION_HEADER.fullmatch(label) or COMPACT_RATE.fullmatch(label):
        return label.partition("(")[0].rstrip()
    return None


def decorated_header_metric(text):
    if evaluation := evaluation_header_metric(text):
        return evaluation
    label = EXPLICIT_UNIT.sub("", clean(text)).strip(" ↑↓*†‡")
    if EXPLICIT_HEADER.fullmatch(label):
        if threshold := re.search(r"@\s*(0\.\d+)", label):
            return "Accuracy@" + threshold[1]
        if label.casefold().startswith("cosine sim"):
            return "Cosine similarity"
        return label.replace("–", "-")
    matches = list(HEADER_METRIC_NAMES.finditer(label))
    if len(matches) == 1 and not re.search(
        r"\b(?:EM|Precision|Accuracy|latency|std|SD|variance|difference|improvement)\b", label, re.I
    ):
        token = re.sub(r"\s+", "", matches[0][0]).replace("–", "-")
        # Overlap-based counters can be labelled with the scored object.
        if token.upper().startswith(("ROUGE", "BLEU")) and re.search(r"\b(?:prompts?|contexts?)\b", label, re.I):
            return label
        if token.casefold().startswith("recall"):
            return "Recall" + token[6:]
        return token.upper()
    return None


def known_metric(text, entities, *, header=True):
    key = metric_key(text)
    if key in entities["metrics"]:
        entry = entities["metrics"][key]
        return entry["value"], entry["source"]
    if match := re.fullmatch(r"(hit|recall|precision|ndcg|map|mrr)@(\d+)", key):
        name = {"hit": "Hit", "recall": "Recall", "precision": "Precision",
                "ndcg": "NDCG", "map": "MAP", "mrr": "MRR"}[match[1]]
        return f"{name}@{match[2]}", None
    if header and key not in entities["non_metrics"] and (explicit := decorated_header_metric(text)):
        return explicit, None
    return None


def explicit_unit(text):
    if match := EXPLICIT_UNIT.search(text):
        return (match[1] or match[0]).casefold()
    return None
