#!/usr/bin/env python3
"""Run the browser acceptance against the saved, manually checked releases."""

import argparse
from contextlib import closing, contextmanager
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
RELEASES = {
    "d001": (
        "f92a0763-9f83-4b13-97ed-e889fc996be1", "5a984233-a235-4755-b770-da2008bc4718",
        "publication-7fcbb21b0bdf5bb222b347d0b5e8d6587aa8af5c5f855f62322d0d7ad5427d3c",
        "version-f0821d2bc81ccd04145d39d751c8ebbf74fdf28f",
    ),
    "d013": (
        "a857566f-6288-4e3e-a34b-b37a6a8cc71f", "5ba0283f-2909-4fbd-bb82-b5064bdb22a5",
        "publication-a16d5237120a18d18488f4db3d7ae3fbc5ff628a90b794bd007451ae53472b7d",
        "version-3d39a3d655488ddd399f9681f9d815bac184f264",
    ),
    "p056": (
        "f8194610-5893-43ee-8761-88b87f4700a2", "e335e9b2-a19d-4e58-8fc6-b669f18bb93c",
        "publication-21a95a6a98731f0041daebb34b48c256d1154e4c823efdd71454b987889f76d7",
        "version-8729df18831767db56e1125ecdeb6545e91da355",
    ),
}


@contextmanager
def server(command, env, log, ready):
    with log.open("w", encoding="utf-8") as output:
        process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=output, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                match = re.search(ready, log.read_text(encoding="utf-8"))
                if match:
                    yield match[1]
                    return
                if process.poll() is not None:
                    break
                time.sleep(0.1)
            raise RuntimeError(f"Стенд не запустился. Журнал: {log}")
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("suite", choices=RELEASES)
    parser.add_argument("saved_run", type=Path, help="Каталог с articles/ и publications/ проверенного выпуска")
    parser.add_argument("--newer-d001", type=Path, help="Каталог более свежей версии для проверки архивной ссылки D001")
    args = parser.parse_args()
    if args.newer_d001 and args.suite != "d001":
        parser.error("--newer-d001 применим только к d001")
    saved = args.saved_run.resolve()
    article_id, run_id, publication_id, version_id = RELEASES[args.suite]
    output = ROOT / "qa/java/build" / args.suite
    articles = saved / "articles"
    database = articles / "article-runs.sqlite"
    publications = saved / "publications"
    version = publications / publication_id / version_id
    for path in (database, articles / article_id / "map/inventory.json", version / "dataset.json", version / "export.zip"):
        if not path.is_file():
            parser.error(f"Нет файла контрольного выпуска: {path}")
    newer = args.newer_d001.resolve() if args.newer_d001 else None
    if newer:
        archived_info = json.loads((version / "bundle/manifest.json").read_text(encoding="utf-8"))
        newer_info = json.loads((newer / "bundle/manifest.json").read_text(encoding="utf-8"))
        if (newer_info["publicationId"] != publication_id or newer_info["versionId"] != newer.name
                or newer_info["pdfSha256"] != archived_info["pdfSha256"]
                or newer_info["publishedAt"] <= archived_info["publishedAt"]
                or newer_info["versionId"] == version_id):
            parser.error("Нужна более свежая версия той же публикации D001 и того же PDF")
    output.mkdir(parents=True, exist_ok=True)
    gradle = [
        str(ROOT / "qa/java/gradlew"), "-p", "qa/java", f"-PqaOutput=build/{args.suite}",
        f"-PqaLabAssets={ROOT / 'web/dist/client'}",
    ]
    subprocess.run([*gradle, "buildLab"], cwd=ROOT, check=True)
    with tempfile.TemporaryDirectory(prefix=f"rag-qa-{args.suite}-") as directory:
        isolated = Path(directory)
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as source:
            with closing(sqlite3.connect(isolated / database.name)) as target:
                source.backup(target)
        shutil.copytree(articles / article_id, isolated / article_id)
        if newer:
            publications = isolated / "publications"
            shutil.copytree(version, publications / publication_id / version_id)
            shutil.copytree(newer, publications / publication_id / newer.name)
        environment = {
            **os.environ,
            "PORT": "0",
            "RAG_HOST": "127.0.0.1",
            "RAG_ARTICLE_STORE": str(isolated),
            "RAG_PUBLICATION_STORE": str(publications),
            "RAG_AMQP_URL": "",
        }
        with server(["node", "web/src/server/index.ts"], environment, output / "hono.log",
                    r"RAG web: (http://127\.0\.0\.1:\d+)") as api:
            print(f"QA React и Hono {api}", flush=True)
            archive_args = [f"-PqaLiveNewer={newer.name}"] if newer else []
            return subprocess.run([
                *gradle, "test", "-x", "buildLab", f"-PqaLive={api}", f"-PqaLiveSuite={args.suite}",
                f"-PqaLiveArticle={article_id}", f"-PqaLiveRun={run_id}", f"-PqaLiveVersion={version_id}",
                f"-PqaLivePublication={publication_id}", f"-PqaLiveZip={version / 'export.zip'}", *archive_args,
            ], cwd=ROOT, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
