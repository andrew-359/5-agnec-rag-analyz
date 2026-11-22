"""Detect table areas with Docling layout and RapidOCR."""
import json
import os
from importlib.metadata import version

from .table_region_discovery import table_region_candidates


def ocr_backend():
    backend = os.environ.get('RAG_OCR_BACKEND', 'torch')
    if backend not in {'torch', 'onnxruntime'}:
        raise ValueError('RAG_OCR_BACKEND должен быть torch или onnxruntime.')
    return backend


def discover_tables(pdf, pages, digest, folder, *, ocr=True):
    import torch
    from docling.datamodel.base_models import ConversionStatus
    from docling.datamodel.pipeline_options import OcrMode, RapidOcrOptions
    from .strategies.parser import converter_for, docling_options, docling_versions

    engine = 'ocr' if ocr else 'layout'
    settings = {}
    versions = docling_versions(*(['rapidocr'] if ocr else []))
    if ocr:
        backend = ocr_backend()
        if backend == 'onnxruntime':
            versions['onnxruntime'] = version('onnxruntime')
        settings['ocr_options'] = RapidOcrOptions(backend=backend, lang=['en'], mode=OcrMode.FULL_PAGE, scale=2)
    options = docling_options(do_ocr=ocr, do_table_structure=False, **settings)
    torch.set_num_threads(options.accelerator_options.num_threads)
    converter = converter_for(options)
    groups = []
    for page in sorted(set(pages)):
        if groups and groups[-1][-1] == page - 1:
            groups[-1].append(page)
        else:
            groups.append([page])
    candidates, warnings = [], []
    for group in groups:
        try:
            result = converter.convert(pdf, page_range=(group[0], group[-1]), raises_on_error=False)
            name = f'discovery-{engine}-{group[0]}-{group[-1]}.json'
            (folder / name).write_text(json.dumps({
                'sha256': digest, 'pages': group, 'versions': versions,
                'pipeline': options.model_dump(mode='json'), 'status': result.status.value,
                'document': result.document.export_to_dict(),
                'errors': [str(error) for error in result.errors],
            }, ensure_ascii=False, indent=2), encoding='utf-8')
            if result.status in {ConversionStatus.SUCCESS, ConversionStatus.PARTIAL_SUCCESS}:
                candidates.extend(table_region_candidates(result.document, set(group), digest, ocr=ocr))
            if result.status != ConversionStatus.SUCCESS:
                warnings.extend({'page': page, 'reason': f'Поиск областей таблиц завершён не полностью: {result.status.value}. См. {name}.'}
                                for page in group)
        except Exception as error:
            warnings.extend({'page': page, 'reason': f'Поиск областей таблиц недоступен: {type(error).__name__}: {str(error)[:300]}'}
                            for page in group)
    return candidates, warnings
