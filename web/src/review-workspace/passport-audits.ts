type PassportAudit = {
  publicationId: string
  versionId: string
  pdfSha256: string
  method: 'Ручная сверка' | 'Сверка с dev_gold'
  scope: string
  referenceLabel: string
  referenceCount: number
  publishedCount: number
  matchedCount: number
  criteria: string
  control: { resultId: string; evidenceId: string; page: number; label: string }
}

const d001Sha = '7fcbb21b0bdf5bb222b347d0b5e8d6587aa8af5c5f855f62322d0d7ad5427d3c'
const d001Versions = [
  { versionId: 'version-4a207d1d8548d8f504edbbe930d7b2f9aaecea28', pointResult: 'R280', pointEvidence: 'S342' },
  { versionId: 'version-408d65f1d974e27991dbff50563f6a67521ad819', pointResult: 'R292', pointEvidence: 'S363' },
]
const d001Audits = d001Versions.flatMap(({ versionId, pointResult, pointEvidence }): PassportAudit[] => {
  const identity = { publicationId: 'publication-' + d001Sha, pdfSha256: d001Sha, versionId, method: 'Сверка с dev_gold' as const }
  return [{
    ...identity,
    scope: 'D001 · Table 1 · стр. 6',
    referenceLabel: 'Числовые ячейки в эталоне',
    referenceCount: 252,
    publishedCount: 252,
    matchedCount: 252,
    criteria: 'Сверены значение, метод, генератор, датасет и метрика. Рамка показана для одной контрольной ячейки.',
    control: { resultId: 'R235', evidenceId: 'S240', page: 6, label: 'ThreadRAG · LLaMA3-8B-Instruct · HotpotQA · EM = 0.422' },
  }, {
    ...identity,
    scope: 'D001 · Figure 3(a) · стр. 8',
    referenceLabel: 'Точки в контрольной выборке',
    referenceCount: 1,
    publishedCount: 1,
    matchedCount: 1,
    criteria: 'Проверена одна точка из dev_gold. Значение приблизительное, абсолютный допуск 0.001.',
    control: { resultId: pointResult, evidenceId: pointEvidence, page: 8, label: 'HotpotQA · anchors = 1 · EM ≈ 0.528' },
  }]
})

export const passportAudits: PassportAudit[] = [{
  publicationId: 'publication-b12f654576b9c58f058492c908f8f8633d0c5a89c1a3404e02661986e751f32e',
  versionId: 'version-f8ce20d8eefa953ce2bae9339896efbabc565e82',
  pdfSha256: 'b12f654576b9c58f058492c908f8f8633d0c5a89c1a3404e02661986e751f32e',
  method: 'Ручная сверка',
  scope: 'P001 · Table 1 · стр. 7',
  referenceLabel: 'Числовые ячейки в PDF',
  referenceCount: 12,
  publishedCount: 12,
  matchedCount: 12,
  criteria: 'По адресам ячеек сверены исходные числа, метрики Recall@k и датасет ORD-QA. У всех 12 строк метод остаётся открытым.',
  control: { resultId: 'R11', evidenceId: 'S21', page: 7, label: 'ORD-QA · Recall@15 = 0.702 · метод открыт' },
}, {
  publicationId: 'publication-051dcfd4eafd604460ba3b18a172c07a08726fd26e1eca4c4eeebbfc31a19995',
  versionId: 'version-c7fa7ba0b5ff48c64ab6e8945885fef2b7c6caaf',
  pdfSha256: '051dcfd4eafd604460ba3b18a172c07a08726fd26e1eca4c4eeebbfc31a19995',
  method: 'Ручная сверка',
  scope: 'P008 · Table 2 · стр. 3',
  referenceLabel: 'Числовые ячейки в PDF',
  referenceCount: 6,
  publishedCount: 6,
  matchedCount: 6,
  criteria: 'По адресам ячеек сверены исходные числа, методы, метрики Hit@k и датасет STARK-MAG. Все 6 строк опубликованы как полные.',
  control: { resultId: 'R2', evidenceId: 'S7', page: 3, label: 'Text Retriever: VSS · STARK-MAG · Hit@5 = 0.4961' },
}, {
  publicationId: 'publication-21a95a6a98731f0041daebb34b48c256d1154e4c823efdd71454b987889f76d7',
  versionId: 'version-8729df18831767db56e1125ecdeb6545e91da355',
  pdfSha256: '21a95a6a98731f0041daebb34b48c256d1154e4c823efdd71454b987889f76d7',
  method: 'Ручная сверка',
  scope: 'P056 · Table II · стр. 8',
  referenceLabel: 'Числовые ячейки в эталоне',
  referenceCount: 155,
  publishedCount: 57,
  matchedCount: 57,
  criteria: 'Сверены значение, метод, генератор, датасет и метрика. Рамка проверена у контрольной записи.',
  control: { resultId: 'R26', evidenceId: 'S60', page: 8, label: 'PolyRAG · GPT-4 · AcadChall · F1 = 0.8109' },
}, ...d001Audits]
