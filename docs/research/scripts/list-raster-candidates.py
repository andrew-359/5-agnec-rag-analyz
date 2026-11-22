"""Read-only candidate search; proximity to a table caption is NOT a classification.
Usage: python list-raster-candidates.py CORPUS_DIR --out candidates.json
Requires existing PyMuPDF. Does not perform OCR or extraction.
"""
import argparse
import json
import re
from pathlib import Path
import pymupdf

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('corpus', type=Path)
parser.add_argument('--out', required=True, type=Path)
args = parser.parse_args()
rows = []
scanned = []
for filename in sorted(args.corpus.glob('*.pdf')):
    with pymupdf.open(filename) as document:
        characters = sum(len(page.get_text()) for page in document)
        scanned.append({'pdf': filename.name, 'pages': len(document), 'characters': characters})
        if characters < 100:
            continue  # Entire scans are not a natural mixed-document control.
        for page_number, page in enumerate(document, 1):
            width, height = page.rect.width, page.rect.height
            words = page.get_text('words')
            for image in page.get_image_info():
                left, top, right, bottom = image['bbox']
                area = (right-left)*(bottom-top)/(width*height)
                if area < .008 or bottom-top < 25 or right-left < 100:
                    continue
                inside = [word for word in words if left <= (word[0]+word[2])/2 <= right
                          and top <= (word[1]+word[3])/2 <= bottom]
                if len(inside) >= 12:
                    continue
                nearby = page.get_textbox(pymupdf.Rect(max(0,left-15),max(0,top-50),min(width,right+15),min(height,bottom+65)))
                rows.append({'pdf':filename.name,'page':page_number,'box':[left/width,top/height,right/width,bottom/height],
                             'area_fraction':area,'words_inside':len(inside),'nearby_text':nearby,
                             'caption_candidates':re.findall(r'(?im)^.{0,20}(?:Table|TABLE)\s+[A-Z\dIVX][^\n]{0,160}',nearby)})
args.out.parent.mkdir(parents=True, exist_ok=True)
args.out.write_text(json.dumps({'documents':scanned,'candidates':rows},ensure_ascii=False,indent=2)+'\n')
print(f'{len(scanned)} PDFs, {len(rows)} candidate image occurrences; visual classification required')
