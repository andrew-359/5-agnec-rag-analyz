"""Translate the two saved overview excerpts with a local OPUS-MT model."""

import argparse
from html import unescape
import json
import re
import sys
from pathlib import Path


SECTIONS = ("abstract", "conclusion")
MAX_CHARS = 8000
MAX_TOKENS = 240
RAG_TERM = re.compile(r"\bretrieval[-\s]+augmented\s+generation(?:\s*\(RAG\))?", re.I)


def segments(text, tokenizer):
    """Pack sentences; split a long sentence at word boundaries."""
    current = ""
    for sentence in re.split(r"(?<=[.!?])\s+", text.strip()):
        pieces = [sentence] if len(tokenizer.encode(sentence)) <= MAX_TOKENS else sentence.split()
        for piece in pieces:
            if len(tokenizer.encode(piece)) > MAX_TOKENS:
                raise ValueError("An overview token exceeds the input limit")
            combined = f"{current} {piece}" if current else piece
            if len(tokenizer.encode(combined)) > MAX_TOKENS:
                yield current
                current = piece
            else:
                current = combined
    if current:
        yield current


def translate(model_dir, excerpts):
    import ctranslate2
    import sentencepiece

    if set(excerpts) != set(SECTIONS) or any(
        text is not None and (not isinstance(text, str) or not 0 < len(text.strip()) <= MAX_CHARS)
        for text in excerpts.values()
    ):
        raise ValueError("Expected bounded overview excerpts")
    source = sentencepiece.SentencePieceProcessor(model_file=str(model_dir / "source.spm"))
    target = sentencepiece.SentencePieceProcessor(model_file=str(model_dir / "target.spm"))
    translator = ctranslate2.Translator(str(model_dir), device="cpu", intra_threads=2, compute_type="int8")
    owners = []
    batch = []
    for section in SECTIONS:
        excerpt = excerpts[section]
        if excerpt is None:
            continue
        # This older model mistranslates the expanded name of the project's core term.
        for text in segments(RAG_TERM.sub("RAG", excerpt), source):
            owners.append(section)
            batch.append([*source.encode(text, out_type=str), "</s>"])
    converted = translator.translate_batch(
        batch, beam_size=4, max_batch_size=4, max_decoding_length=512, return_end_token=True,
    )
    output = {section: [] for section in SECTIONS}
    for section, result in zip(owners, converted, strict=True):
        tokens = result.hypotheses[0]
        if not tokens or tokens[-1] != "</s>":
            raise ValueError("Incomplete overview translation")
        text = unescape(target.decode(tokens[:-1])).strip()
        if not text:
            raise ValueError("Empty overview translation")
        output[section].append(text)
    return {section: " ".join(parts) if parts else None for section, parts in output.items()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model_dir", type=Path)
    args = parser.parse_args()
    try:
        result = translate(args.model_dir, json.load(sys.stdin))
    except (ImportError, OSError, RuntimeError, ValueError) as error:
        parser.exit(1, f"{error}\n")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
