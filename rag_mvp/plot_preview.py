"""Create a self-contained, local report over the original PDF page."""

from base64 import b64encode
from html import escape
from io import BytesIO
from pathlib import Path

import pdfplumber


def write_preview(result, destination):
    page_number = result["source"]["page"]
    buffer = BytesIO()
    with pdfplumber.open(result["pdf"]) as document:
        page = document.pages[page_number - 1]
        page.to_image(resolution=145).original.save(buffer, format="PNG")
    encoded = b64encode(buffer.getvalue()).decode("ascii")
    settings = result["settings"]
    title = escape(f"{Path(result['pdf']).name} · Fig. {settings['figure']}({settings['panel']})")
    pdf_url = escape(result["source"]["url"], quote=True)
    overlays = []

    def link_to_source(anchor, label, source, kind):
        if source["box"] is None:
            return f"<li>{escape(label)}</li>"
        left, top, right, bottom = source["box"]
        overlays.append(
            f'<span id="{anchor}" class="overlay {kind}" aria-label="{escape(label, quote=True)}" '
            f'style="left:{left:.4%};top:{top:.4%};width:{right-left:.4%};height:{bottom-top:.4%}"></span>'
        )
        return f'<li><a href="#{anchor}">{escape(label)}</a></li>'

    points = "".join(
        link_to_source(f'point-{point["index"]}', f'Точка {point["index"]} · x={point["x"]:.8g} · y≈{point["y"]:.6g}',
                       point["source"], "point")
        for point in result["points"]
    )
    issues = "".join(
        link_to_source(f"issue-{index}", issue["message"], issue["source"], "issue")
        for index, issue in enumerate(result["issues"], 1)
    )
    issue_section = f"<h2>Замечания</h2><ul>{issues}</ul>" if issues else ""
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(f'''<!doctype html>
<html lang="ru">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="data:,">
<title>{title}</title>
<style>
body {{ font: 16px system-ui, sans-serif; color: #19242c; margin: 24px; background: #eef2f5; }}
main {{ display: grid; grid-template-columns: minmax(16rem, 1fr) minmax(30rem, 3fr); gap: 24px; align-items: start; }}
nav {{ position: sticky; top: 24px; max-height: calc(100vh - 48px); overflow: auto; }}
a {{ color: #174875; }}
figure {{ position: relative; margin: 0; background: white; }}
img {{ display: block; width: 100%; }}
.overlay {{ position: absolute; visibility: hidden; }}
.overlay:target {{ visibility: visible; scroll-margin: 40vh; }}
.point {{ border-radius: 50%; outline: 3px solid #b91c1c; background: #ef44445c; }}
.issue {{ outline: 3px dashed #a65d05; background: #ffba463f; }}
li {{ margin: .4em 0; }}
@media (max-width: 760px) {{
  body {{ margin: 12px; }}
  main {{ grid-template-columns: minmax(0, 1fr); }}
  nav {{ position: static; max-height: none; }}
}}
</style>
<h1>{title}</h1>
<main>
<nav aria-label="Результаты"><a href="{pdf_url}">Открыть оригинал, страница {page_number}</a>
<h2>Точки</h2><ul>{points}</ul>{issue_section}</nav>
<figure><img alt="Оригинальная PDF-страница {page_number}" src="data:image/png;base64,{encoded}">
{''.join(overlays)}
</figure>
</main>
</html>
''', encoding="utf-8")
