import json
from importlib.util import find_spec
from io import BytesIO
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch


@unittest.skipUnless(os.environ.get('RAG_TEST_SCAN_DISCOVERY') == '1', 'Нужен RAG_TEST_SCAN_DISCOVERY=1 и комплект участника.')
class ScanDiscoveryAcceptanceTest(unittest.TestCase):
    def test_full_scan_finds_table_caption_without_a_supplied_page_or_crop(self):
        kit = Path(os.environ['RAG_DEV_CORPUS'])
        with TemporaryDirectory() as folder:
            out = Path(folder) / 'map'
            process = subprocess.run([sys.executable, '-m', 'rag_mvp', 'inspect', str(kit / 'dev/D013.pdf'),
                                      '--out', str(out)], capture_output=True, text=True, timeout=600)
            self.assertEqual(process.returncode, 0, process.stderr[-1500:])
            inventory = json.loads((out / 'inventory.json').read_text())
            candidates = [item for item in inventory['candidates'] if item['kind'] == 'table'
                          and item['page'] == 6 and item['label'] == '2']
            self.assertEqual(len(candidates), 1, inventory['summary'])
            self.assertEqual(candidates[0]['discovery'], 'ocr')
            self.assertEqual(candidates[0]['region']['designation'], 'Table 2')
            self.assertEqual(candidates[0]['region']['bounds_status'], 'confirmed')
            self.assertIsNotNone(candidates[0]['box'])
            self.assertTrue(list(out.glob('discovery-ocr-*.json')))

    def test_full_scan_finds_roman_label_and_the_table_body(self):
        pdf = Path(os.environ['RAG_DEV_CORPUS']) / 'test/P056.pdf'
        with TemporaryDirectory() as folder:
            out = Path(folder) / 'map'
            process = subprocess.run([sys.executable, '-m', 'rag_mvp', 'inspect', str(pdf), '--out', str(out)],
                                     capture_output=True, text=True, timeout=600)
            self.assertEqual(process.returncode, 0, process.stderr[-1500:])
            inventory = json.loads((out / 'inventory.json').read_text())
            candidates = [item for item in inventory['candidates'] if item['kind'] == 'table'
                          and item['page'] == 8 and item['label'] == 'II']
            self.assertEqual(len(candidates), 1, inventory['summary'])
            region = candidates[0]['region']
            self.assertEqual(region['designation'], 'Table II')
            self.assertEqual(region['caption_status'], 'confirmed')
            self.assertEqual(region['bounds_status'], 'confirmed')
            self.assertGreater(region['box'][3] - region['box'][1], .25)
            self.assertLessEqual(region['caption_box'][3], region['box'][1])


class RegionDiscoveryTest(unittest.TestCase):
    @unittest.skipUnless(find_spec('pymupdf'), 'Для подготовки смешанного PDF нужен PyMuPDF.')
    def test_mixed_pages_send_a_substantial_uncaptioned_image_to_region_discovery(self):
        import pymupdf
        from PIL import Image
        from rag_mvp.inventory import inspect_pdf

        anonymous = {
            'id': 'anonymous', 'page': 2, 'kind': 'table', 'label': None, 'text': 'Область таблицы',
            'box': [0.5, 0.1, 0.85, 0.2], 'discovery': 'ocr',
            'region': {'bounds_status': 'confirmed', 'caption_status': 'missing', 'caption_box': None},
        }
        with TemporaryDirectory() as folder:
            pdf = Path(folder) / 'mixed.pdf'
            with BytesIO() as image, pymupdf.open() as document:
                Image.new('RGB', (20, 20), 'white').save(image, format='PNG')
                native_text = 'This is the native text layer of a page with an embedded image.'
                for width in (70, 210):
                    page = document.new_page(width=600, height=800)
                    page.insert_text((40, 300), native_text + '\n' + native_text)
                    page.insert_image(pymupdf.Rect(300, 80, 300 + width, 160),
                                      stream=image.getvalue(), keep_proportion=False)
                document.save(pdf)
            with patch('rag_mvp.ocr_discovery.discover_tables', return_value=([anonymous], [])):
                inventory = inspect_pdf(pdf, Path(folder) / 'map')

        self.assertTrue(all(page['text_chars'] >= 80 for page in inventory['pages']))
        self.assertEqual(inventory['settings']['ocr_discovery']['pages'], [2])
        (table,) = (candidate for candidate in inventory['candidates'] if candidate['kind'] == 'table')
        self.assertEqual((table['page'], table['region']['caption_status']), (2, 'missing'))

    def test_aligned_table_survives_a_missing_caption_and_exposes_conflicting_bounds(self):
        from docling_core.types.doc import BoundingBox, CoordOrigin, DocItemLabel, DoclingDocument, ProvenanceItem, Size, TableData
        from rag_mvp.ocr_discovery import table_region_candidates

        doc = DoclingDocument(name='scan')
        doc.add_page(page_no=1, size=Size(width=100, height=200))

        def provenance(left, top, right, bottom):
            return ProvenanceItem(page_no=1, bbox=BoundingBox(l=left, t=top, r=right, b=bottom,
                                  coord_origin=CoordOrigin.TOPLEFT), charspan=(0, 1))

        small = (10, 20, 90, 62)
        broad = (10, 20, 90, 115)
        grid = TableData(num_rows=1, num_cols=1)
        table = doc.add_table(data=grid, prov=provenance(*small))
        group = doc.add_group(parent=table)
        for top in (24, 37, 51):
            for left in (12, 47, 82):
                doc.add_text(label=DocItemLabel.TEXT, text='1', prov=provenance(left, top, left + 4, top + 7), parent=group)
        doc.add_text(label=DocItemLabel.TEXT, text='Table 3 reports results in another section.',
                     prov=provenance(10, 5, 90, 10))

        [anonymous] = table_region_candidates(doc, {1}, 'a' * 64)
        self.assertEqual(anonymous['region']['caption_status'], 'missing')
        self.assertEqual(anonymous['region']['bounds_status'], 'confirmed')
        self.assertEqual(anonymous['box'], (.1, .1, .9, .31))

        doc.add_table(data=grid, prov=provenance(*broad))
        [disputed] = table_region_candidates(doc, {1}, 'a' * 64)
        self.assertEqual(disputed['region']['bounds_status'], 'needs_review')
        self.assertEqual(set((disputed['region']['box'], *disputed['region']['alternative_boxes'])),
                         {(.1, .1, .9, .31), (.1, .1, .9, .575)})
