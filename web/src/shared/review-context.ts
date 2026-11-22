import type { Dataset, Result } from './schema.ts'

type ReviewSelection = { remarkId: string; resultId?: string; evidenceId?: string; sourceMode?: string }
export function reviewContext(data: Pick<Dataset, 'remarks' | 'results' | 'evidence'>, selection: ReviewSelection) {
  const remark = data.remarks.find(item => item.id === selection.remarkId)
  if (!remark) return { error: 'Замечание не найдено в выбранной версии.' } as const
  const linked = data.results.filter(item => remark.resultIds?.includes(item.id))
  const result = selection.resultId ? linked.find(item => item.id === selection.resultId) : linked.length === 1 ? linked[0] : undefined
  if (selection.resultId && !result) return { error: 'Результат не связан с выбранным замечанием.' } as const
  const condition = selection.sourceMode === 'condition'
  if (selection.sourceMode && !['remark', 'condition'].includes(selection.sourceMode)) return { error: 'Неизвестный режим источника.' } as const
  if (condition && !result) return { error: 'Для источника условия сначала выберите связанный результат.' } as const
  const ids = condition ? result!.evidenceIds : remark.evidenceIds
  const evidenceId = selection.evidenceId ?? ids[0]
  const evidence = data.evidence.find(item => item.id === evidenceId)
  if (evidenceId && (!ids.includes(evidenceId) || !evidence)) return { error: 'Источник не принадлежит выбранному случаю.' } as const
  return { remark, linked, result, evidence, condition, error: undefined,
    request: evidence ? { evidenceId: evidence.id, resultId: condition ? result!.id : undefined, remarkId: condition ? undefined : remark.id } : undefined }
}

export function resultConditions(result: Result) {
  const fields = [
    { key: 'method', label: 'Метод', value: result.method },
    { key: 'dataset', label: 'Датасет', value: result.dataset },
    { key: 'generator', label: 'Генератор', value: result.generator },
    { key: 'retrieval_top_k', label: 'Retrieval top-k', value: result.retrievalTopK },
    { key: 'protocol', label: 'Протокол', value: result.protocol },
    { key: 'statistic', label: 'Статистика', value: result.statistic },
    ...Object.entries(result.parameters ?? {}).map(([key, value]) => ({ key, label: key, value })),
  ]
  return fields.map(field => ({ ...field, evidenceIds: (result.conditionEvidence?.[field.key] ?? []).filter(id => result.evidenceIds.includes(id)) }))
}
