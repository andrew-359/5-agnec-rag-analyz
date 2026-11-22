import argparse
from dataclasses import asdict
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from math import isfinite
from pathlib import Path
from tempfile import TemporaryDirectory

from .strategies.article_text_readers import ARTICLE_TEXT_READERS, DEFAULT_ARTICLE_TEXT_READER, read_article_fragments
from .run import IncompatibleArtifact
from .run_output import write_csv as write_csv
from .run_output import write_json as write_json
from .table_extraction import extract
from .strategies.table_readers import DEFAULT_TABLE_READER, TABLE_READERS
from .table_region import TableRegion


def save_article_text(pdf: Path, out: Path, *, strategy: str = DEFAULT_ARTICLE_TEXT_READER) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".rag-text-", dir=out.parent) as temporary:
        folder = Path(temporary)
        write_json(folder / "article-text.json", asdict(read_article_fragments(pdf, strategy=strategy)))
        folder.rename(out)


def serve(run: Path, port: int) -> None:
    if not (run / "run.json").is_file() or not (run / "index.html").is_file():
        raise ValueError(f"Не найден сохранённый прогон: {run}")
    handler = partial(SimpleHTTPRequestHandler, directory=str(run))
    with ThreadingHTTPServer(("127.0.0.1", port), handler) as server:
        print(f"Откройте http://127.0.0.1:{server.server_port}/", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


def positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("Ожидается целое число от 1.")
    return number


def crop_coordinate(value: str) -> float:
    coordinate = float(value)
    if not isfinite(coordinate) or coordinate < 0:
        raise argparse.ArgumentTypeError("Ожидается конечная неотрицательная координата.")
    return coordinate


def main() -> None:
    parser = argparse.ArgumentParser(description="Извлечение результатов из таблицы научного PDF.")
    commands = parser.add_subparsers(dest="command", required=True)
    inventory = commands.add_parser("inspect", help="Построить карту всей статьи")
    inventory.add_argument("pdf", type=Path)
    inventory.add_argument("--out", type=Path, required=True, help="Новый каталог обзора")
    text = commands.add_parser("read-article-text", help="Сохранить общий текст PDF")
    text.add_argument("pdf", type=Path)
    text.add_argument("--out", type=Path, required=True)
    text.add_argument("--strategy", choices=ARTICLE_TEXT_READERS, default=DEFAULT_ARTICLE_TEXT_READER,
                      help=f"Стратегия чтения (по умолчанию {DEFAULT_ARTICLE_TEXT_READER})")
    figure = commands.add_parser("read-article-figure", help="Прочитать рисунок по карте статьи")
    figure.add_argument("pdf", type=Path)
    figure.add_argument("--inventory", type=Path, required=True)
    figure_context = figure.add_mutually_exclusive_group()
    figure_context.add_argument("--article-text", type=Path, help="Сохранённый общий текст PDF")
    figure_context.add_argument("--context-input", type=Path, help="Повторно использовать сохранённую подборку")
    figure.add_argument("--candidate", required=True)
    figure.add_argument("--figure-plan", help="JSON с адресом рисунка и явным выбором ридера")
    figure.add_argument("--out", type=Path, required=True)
    conditions = commands.add_parser("propose-table-conditions", help="Предложить условия таблицы по сохранённому тексту")
    conditions.add_argument("--article-text", type=Path, required=True)
    conditions.add_argument("--page", type=positive_int, required=True)
    conditions.add_argument("--table", type=positive_int, required=True)
    conditions.add_argument("--caption", required=True, help="ID фрагмента подписи")
    conditions.add_argument("--fragment", action="append", required=True, help="ID фрагмента основания")
    conditions.add_argument("--grid-input", type=Path, help="Неизменная сохранённая сетка для штатной привязки")
    conditions.add_argument("--control", type=Path, help="Ручной контроль, используемый только при сверке")
    conditions.add_argument("--out", type=Path, required=True, help="Новый каталог попытки")
    extraction = commands.add_parser("extract", help="Сохранить прогон и CSV")
    extraction.add_argument("pdf", type=Path)
    extraction.add_argument("--page", type=positive_int, required=True, help="Номер страницы PDF с 1")
    extraction.add_argument("--table", type=positive_int, required=True, help="Номер таблицы по подписи")
    extraction.add_argument("--table-region", help="JSON-адрес области таблицы в долях исходной страницы")
    extraction.add_argument("--out", type=Path, required=True, help="Новый каталог для прогона")
    extraction.add_argument("--strategy", choices=TABLE_READERS, default=DEFAULT_TABLE_READER,
                            help=f"Стратегия чтения (по умолчанию {DEFAULT_TABLE_READER})")
    crop_readers = ", ".join(name for name, reader in TABLE_READERS.items() if reader.requires_crop)
    extraction.add_argument("--crop", nargs=4, type=crop_coordinate, metavar=("X0", "TOP", "X1", "BOTTOM"),
                            help=f"Обязательная область для {crop_readers} при чтении PDF, в пунктах от верхнего левого угла страницы")
    extraction.add_argument("--result-crop", nargs=4, type=crop_coordinate, metavar=("X0", "TOP", "X1", "BOTTOM"),
                            help="Отобрать ячейки результатов по центру внутри области в пунктах PDF")
    context_input = extraction.add_mutually_exclusive_group()
    context_input.add_argument("--article-context", action="store_true",
                               help="Искать в тексте PDF условия выбранной таблицы")
    context_input.add_argument("--article-text", type=Path,
                               help="Применить условия из сохранённого общего текста PDF")
    context_input.add_argument("--context-input", type=Path,
                               help="Повторить привязку по сохранённой подборке (с --grid-input)")
    grid = extraction.add_mutually_exclusive_group()
    grid.add_argument("--grid-only", action="store_true", help="Сохранить прочитанную сетку")
    grid.add_argument("--grid-input", type=Path, help="Использовать сохранённую сетку")
    extraction.add_argument("--previous-grid-input", nargs=2, metavar=("STRATEGY", "FILE"),
                            help="Учесть сохранённую сетку предыдущей стратегии")
    viewer = commands.add_parser("serve", help="Открыть сохранённый прогон в браузере")
    viewer.add_argument("run", type=Path)
    viewer.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    try:
        if args.command == "inspect":
            from .inventory import inspect_pdf

            result = inspect_pdf(args.pdf, args.out)
            print(f"Страниц: {result['page_count']}. Кандидаты: {result['summary']}. Обзор: {args.out}")
        elif args.command == "read-article-text":
            save_article_text(args.pdf, args.out, strategy=args.strategy)
        elif args.command == "read-article-figure":
            from .article_figures import save_article_figure

            save_article_figure(args.pdf, args.inventory, args.article_text, args.candidate, args.out,
                                figure_plan=args.figure_plan, context_input=args.context_input)
        elif args.command == "propose-table-conditions":
            from .qwen_conditions import propose

            propose(args)
        elif args.command == "extract":
            region = TableRegion.from_json(args.table_region) if args.table_region is not None else None
            extract(args.pdf, args.page, args.table, args.out, args.strategy, args.crop,
                    with_article_context=args.article_context, article_text=args.article_text,
                    context_input=args.context_input,
                    grid_input=args.grid_input, grid_only=args.grid_only, result_crop=args.result_crop, region=region,
                    previous_grid_input=(args.previous_grid_input[0], Path(args.previous_grid_input[1]))
                    if args.previous_grid_input else None)
        else:
            serve(args.run, args.port)
    except IncompatibleArtifact as error:
        parser.exit(3, f"{error}\n")
    except (ValueError, OSError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
