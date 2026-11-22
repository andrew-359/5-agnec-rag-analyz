import argparse
import json
from dataclasses import asdict
from pathlib import Path

from . import read_article_fragments


def main() -> None:
    parser = argparse.ArgumentParser(description="Извлечь текстовые фрагменты цифрового PDF.")
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--page", action="append", type=int, help="PDF-страница с 1. Можно повторять.")
    arguments = parser.parse_args()
    try:
        document = read_article_fragments(arguments.pdf, arguments.page)
    except (OSError, ValueError) as error:
        parser.exit(1, f"{error}\n")
    print(json.dumps(asdict(document), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
