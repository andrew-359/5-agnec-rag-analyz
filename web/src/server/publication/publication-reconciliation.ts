import type { MeasurementRelationship } from '../../shared/reconciliation.ts'
import type { Dataset, Result } from '../../shared/schema.ts'
import type { ExportTables } from './publication-data.ts'

type RunRecord = { artifactId: string; sourceId: string }
const observationKey = (artifactId: string, sourceId: string) => JSON.stringify([artifactId, sourceId])

export function applyReconciliation(
  dataset: Dataset,
  tables: ExportTables,
  origins: Record<string, RunRecord>,
  relationships: MeasurementRelationship[],
) {
  const observations = new Map(dataset.results.map(result => {
    const { artifactId, sourceId } = origins[result.id]
    return [observationKey(artifactId, sourceId), result]
  }))
  const evidence = new Map(dataset.evidence.map(item => [item.id, item]))
  const csv = new Map(tables.results.map(row => [row.result_id, row]))
  const hidden = new Set<string>()
  const duplicates = new Map<string, string>()
  const locate = (member: MeasurementRelationship['members'][number]) => {
    const result = observations.get(observationKey(member.artifact_id, member.result_id))
    if (!result) throw new Error(`В выпуске отсутствует наблюдение ${member.result_id}.`)
    return result
  }

  for (const relation of relationships.filter(item => item.status === 'agreement')) {
    const [primary, ...copies] = relation.members.map(locate)
    for (const copy of copies) {
      duplicates.set(copy.id, primary.id)
      hidden.add(copy.id)
      const target = csv.get(primary.id)!
      Object.assign(csv.get(copy.id)!, {
        duplicate_of: primary.id,
        metric_value: target.metric_value, metric_unit: target.metric_unit,
        normalized_value: target.normalized_value, normalized_unit: target.normalized_unit,
      })
      primary.evidenceIds = [...new Set([...primary.evidenceIds, ...copy.evidenceIds])]
      primary.conditionEvidence ??= {}
      for (const [name, ids] of Object.entries(copy.conditionEvidence ?? {})) {
        primary.conditionEvidence[name] = [...new Set([...(primary.conditionEvidence[name] ?? []), ...ids])]
      }
    }
  }

  const resultIds = (rows: Result[]) => rows.map(row => row.id)
  for (const relation of relationships.filter(item => item.status !== 'agreement')) {
    const rows = relation.members.map(locate)
    const conflict = relation.status === 'conflict'
    if (conflict) for (const rowId of resultIds(rows)) hidden.add(rowId)
    const primary = evidence.get(rows[0].evidenceIds[0])!
    const id = `I${dataset.remarks.length + 1}`
    const reason = conflict ? 'Источники дают разные значения одной метрики.' : 'Недостаточно оснований, чтобы связать наблюдения.'
    const options = rows.map(row => ({
      value: row.rawValue, evidenceIds: row.evidenceIds, conditionEvidence: row.conditionEvidence,
      binding: [row.method, row.generator, row.dataset, row.metric].filter(Boolean), retrievalTopK: row.retrievalTopK,
    }))
    const rawText = rows.map(row => row.rawValue).join(' / ')
    dataset.remarks.push({
      id, runId: dataset.run.id, reason, rawText, options,
      evidenceIds: [...new Set(rows.flatMap(row => row.evidenceIds))], resultIds: resultIds(rows),
    })
    tables.issues.push({
      issue_id: id, record_ids: resultIds(rows).join(';'), check_name: `reconciliation-${relation.status}`,
      description: `${reason} (${rawText})`, source_page: primary.page, source_fragment: primary.text,
    })
  }

  dataset.results = dataset.results.filter(result => !hidden.has(result.id))
  for (const remark of dataset.remarks) {
    if (!remark.resultIds) continue
    const canonical = remark.resultIds.map(id => duplicates.get(id) ?? id)
    remark.resultIds = [...new Set(canonical.filter(id => !hidden.has(id)))]
  }
}
