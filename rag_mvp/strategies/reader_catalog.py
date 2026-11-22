"""Capabilities of the text and table readers used by queued article runs."""

import argparse
import json

from .article_text_readers import ARTICLE_TEXT_READERS
from .table_readers import TABLE_READERS


def catalogue(*, details: bool = False) -> dict:
    table_capabilities = {
        name: {"requires_crop": reader.requires_crop, "accepts_region": reader.accepts_region}
        for name, reader in TABLE_READERS.items()
    }
    if not details:
        return {"text": list(ARTICLE_TEXT_READERS), "table": table_capabilities}

    return {
        "text": [
            {"id": name, "description": reader.description}
            for name, reader in ARTICLE_TEXT_READERS.items()
        ],
        "table": [
            {"id": name, "description": reader.description, **table_capabilities[name]}
            for name, reader in TABLE_READERS.items()
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--details", action="store_true")
    print(json.dumps(catalogue(details=parser.parse_args().details), ensure_ascii=False))
