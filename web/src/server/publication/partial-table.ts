import { parsePublicationArtifact } from './publication-data.ts'

type Artifact = ReturnType<typeof parsePublicationArtifact>
type Row = Artifact['data']['results'][number]
type Remark = Artifact['data']['remarks'][number]

const scalar = /^[+−-]?(?:\d+(?:\.\d+)?|\.\d+)\s*%?$/u
const metricName = (name: string) => name.replace(/^recall(?=@)/i, 'Recall')
const address = (source: Row['source'], raw: string, statistic?: string | null) =>
  JSON.stringify([source.page, source.table, source.row, source.column, raw.trim(), statistic ?? null])

export function publishPartialTable(artifact: Artifact) {
  const { data, fragment } = artifact
  if (!data.table) return artifact
  const byCell = new Map<string, Remark[]>()
  for (const remark of data.remarks) {
    const { source, raw_text: raw } = remark
    if (remark.kind !== 'ambiguous-binding' || source.row == null || source.column == null
      || !source.box || !scalar.test(raw.trim())) continue
    const witness = source.evidence_ids[0] && fragment(source.evidence_ids[0])
    if (!witness || witness.precision !== 'cell' || witness.text.trim() !== raw.trim()) continue
    const key = address(source, raw, remark.statistic)
    const group = byCell.get(key) ?? []
    group.push(remark)
    byCell.set(key, group)
  }

  const results = [...data.results]
  const replacements = new Map<string, Remark>()
  const addedRemarks: Remark[] = []
  for (const [key, group] of byCell) {
    const tagged = group.filter(remark => typeof remark.conditions?.metric === 'string'
      && remark.conditions.metric.trim() && remark.condition_sources?.metric?.length)
    const metrics = new Set(tagged.map(remark => metricName(String(remark.conditions!.metric).trim())))
    if (metrics.size !== 1 || group.some(remark => remark.field === 'metric')) continue
    const [metric] = metrics
    if (data.results.some(row => address(row.source, row.raw_value, row.statistic) === key
      && metricName(row.metric).toLowerCase() === metric.toLowerCase())) continue
    const first = tagged[0]
    const keys = new Set(group.flatMap(remark => Object.keys(remark.conditions ?? {})))
    const conditions: Record<string, unknown> = {}
    const refs: Record<string, string[]> = {}
    for (const field of keys) {
      const supported = group.filter(remark => remark.condition_sources?.[field]?.length && remark.conditions?.[field] != null)
      if (new Set(supported.map(remark => remark.conditions![field])).size !== 1) continue
      conditions[field] = supported[0].conditions![field]
      refs[field] = [...new Set(supported.flatMap(remark => remark.condition_sources![field]))]
    }
    const text = (field: string) => typeof conditions[field] === 'string' ? conditions[field] as string : ''
    const integer = (field: string, min: number) => typeof conditions[field] === 'number'
      && Number.isInteger(conditions[field]) && conditions[field] >= min ? conditions[field] as number : null
    const raw = first.raw_text.trim()
    const unit = text('unit') || (raw.endsWith('%') ? '%' : null)
    const labels = Object.entries(conditions).filter(([name, value]) =>
      typeof value === 'string' && !['method', 'dataset', 'generator', 'experiment', 'task', 'protocol',
        'metric', 'header_path', 'unit', 'result_origin', 'cited_publication_id'].includes(name))
    const label = labels.map(([name, value]) => `${name}=${value}`).join(', ')
    const id = `partial-${first.id}`
    const resultOrigin = text('result_origin') === 'cited' ? 'cited' : 'primary'
    if (resultOrigin === 'cited' && !text('cited_publication_id')) continue
    results.push({
      id, partial: true, metric, source: first.source, raw_value: first.raw_text,
      value: unit ? raw.replace(/\s*%$/, '').replace('−', '-') : null, unit,
      method: text('method'), generator: text('generator'), dataset: text('dataset'), experiment: text('experiment'),
      task: text('task'), protocol: text('protocol') || null, statistic: first.statistic,
      metric_k: integer('metric_k', 1), retrieval_top_k: integer('retrieval_top_k', 0), condition_sources: refs,
      result_origin: resultOrigin, cited_publication_id: text('cited_publication_id') || undefined,
      note: ['Проверка условий', label].filter(Boolean).join(' · '),
    })
    const openFields = new Set(['method', 'dataset', 'experiment', 'task', 'protocol'].filter(field => !text(field)))
    if (!unit) openFields.add('unit')
    for (const remark of group) {
      if (remark.field) openFields.delete(remark.field)
      const reason = remark.field === 'method' && label ? `${remark.reason} Подпись строки: ${label}.` : remark.reason
      replacements.set(remark.id, { ...remark, kind: 'partial-measurement', reason,
        result_ids: [...new Set([...remark.result_ids, id])] })
    }
    for (const field of openFields) addedRemarks.push({
      id: `${id}-${field}`, kind: 'partial-measurement', field, reason: `Для числа не установлено поле «${field}».`,
      source: first.source, raw_text: first.raw_text, result_ids: [id],
    })
  }
  if (results.length === data.results.length) return artifact
  const remarks = [...data.remarks.map(remark => replacements.get(remark.id) ?? remark), ...addedRemarks]
  return parsePublicationArtifact({ ...data, results, remarks }, artifact.id, artifact.pdfSha256)
}
