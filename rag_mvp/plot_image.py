"""Render a bounded PNG crop for figure readers."""

from base64 import b64encode
from io import BytesIO


IMAGE_EDGE_PX = 1100


def render_chart_crop(page, box):
    width, height = box[2] - box[0], box[3] - box[1]
    # pdfplumber renders the page before cropping; bound both raster sizes.
    scale = min(220 / 72, IMAGE_EDGE_PX / max(width, height), 3200 / max(page.width, page.height))
    image = page.crop(box).to_image(resolution=72 * scale, antialias=True).original
    image.thumbnail((IMAGE_EDGE_PX, IMAGE_EDGE_PX))
    with BytesIO() as png:
        image.save(png, format="PNG")
        return b64encode(png.getvalue()).decode("ascii"), image.size
