import type { ArticleTableTarget, TableReadFeatures } from '../../shared/article.ts'

export function planRegionReading(features: TableReadFeatures): Pick<ArticleTableTarget, 'strategy' | 'strategyReason' | 'fallback'> {
  if (features.text_layer === 'usable') return {
    strategy: 'docling-tableformer',
    strategyReason: 'В области таблицы есть пригодный текстовый слой PDF.',
  }
  if (features.header === 'method-top-k') return {
    strategy: 'docling-rapidocr',
    strategyReason: `В области требуется OCR. Распознаны Method и группы Top-k${features.header_labels?.length
      ? ` (${features.header_labels.join(', ')})` : ''}.`,
    fallback: {
      strategy: 'docling-ocr-tableformer',
      reason: 'OCR + TableFormer может восстановить сетку по той же области при дефекте специализированного чтения.',
    },
  }
  return {
    strategy: 'docling-ocr-tableformer',
    strategyReason: 'В области отсутствует пригодный текстовый слой PDF. OCR не подтвердил шапку с Method и группами Top-k.',
    ...(process.env.RAG_PADDLE_PYTHON && {
      fallback: {
        strategy: 'paddle-structure',
        reason: 'PaddleOCR может дочитать одиночное значение при подтверждённых опорах строки и столбца.',
      },
    }),
  }
}
