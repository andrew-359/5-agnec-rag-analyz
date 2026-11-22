import { Hono } from 'hono'
import { bodyLimit } from 'hono/body-limit'
import { readFile } from 'node:fs/promises'
import { z } from 'zod'
import { resultRefSchema, scopeProofSchema } from '../../shared/comparison.ts'
import type { ResultRef, ScopeProof, ComparisonSide, ComparisonCandidate, ComparisonRecommendation } from '../../shared/comparison.ts'
import type { PublicationStore } from '../publication/publication-store.ts'
import { compareResults, tableColumn } from './comparison-engine.ts'

type PublishedData = NonNullable<Awaited<ReturnType<PublicationStore['read']>>>

function toSide(data: PublishedData, ref: ResultRef, result: PublishedData['results'][number]): ComparisonSide {
  return { ref, result, title: data.publication.title, pdfSha256: data.pdf.sha256, pdfUrl: data.pdf.url,
    evidence: data.evidence.filter(item => result.evidenceIds.includes(item.id)) }
}

function compareWithProofs(a: ComparisonSide, b: ComparisonSide, proofs: ScopeProof[]) {
  const validProofs = proofs.filter(({ source }) => [a, b].some(side =>
    side.ref.publicationId === source.publicationId && side.ref.versionId === source.versionId &&
    side.evidence.some(item => item.id === source.evidenceId)))
  return compareResults(a, b, validProofs)
}

export function createComparisonApi({ publications, proofFile }: { publications: Pick<PublicationStore, 'read' | 'listLatest'>; proofFile?: string }) {
  const app = new Hono()
  async function readProofs(): Promise<ScopeProof[]> {
    if (!proofFile) return []
    try {
      return z.object({ proofs: z.array(scopeProofSchema) }).parse(JSON.parse(await readFile(proofFile, 'utf8'))).proofs
    } catch { return [] }
  }

  app.use('*', bodyLimit({ maxSize: 8192, onError: c => c.json({ error: 'Слишком большой запрос.' }, 413) }))
  app.post('/recommendations', async c => {
    const address = resultRefSchema.omit({ resultId: true }).safeParse(await c.req.json().catch(() => null))
    if (!address.success) return c.json({ error: 'Укажите опубликованную версию статьи.' }, 400)
    try {
      const { publicationId, versionId } = address.data
      const [data, proofs] = await Promise.all([publications.read(publicationId, versionId), readProofs()])
      if (!data) return c.json({ error: 'Опубликованная версия не найдена.' }, 404)
      const rows = new Map(data.results.map(row => [row.id, row]))
      const recommendations: ComparisonRecommendation[] = []
      const seen = new Set<string>()
      function recommend(a: ComparisonSide, b: ComparisonSide, proofs: ScopeProof[]) {
        if (a.result.method === b.result.method) return
        const key = JSON.stringify([a.result.id, b.result.id].sort())
        if (seen.has(key)) return
        const comparison = compareWithProofs(a, b, proofs)
        if (comparison.status !== 'comparable' || comparison.delta === undefined || !comparison.unit) return
        seen.add(key)
        recommendations.push({ a: a.ref, b: b.ref, methods: [a.result.method, b.result.method],
          dataset: a.result.dataset, metric: a.result.metric, delta: comparison.delta, unit: comparison.unit })
      }
      for (const proof of proofs) {
        if (![proof.a, proof.b].every(ref => ref.publicationId === publicationId && ref.versionId === versionId)) continue
        const a = rows.get(proof.a.resultId), b = rows.get(proof.b.resultId)
        if (a && b) recommend(toSide(data, proof.a, a), toSide(data, proof.b, b), [proof])
      }
      const groups = new Map<string, ComparisonSide[]>()
      for (const row of data.results) {
        if (!row.conditionEvidence?.protocol?.length || !row.experimentId || row.value === null) continue
        const side = toSide(data, { publicationId, versionId, resultId: row.id }, row)
        const cell = tableColumn(side)
        if (!cell) continue
        const key = JSON.stringify([cell.page, cell.table, cell.column, row.experimentId, row.dataset, row.task, row.metric])
        const group = groups.get(key) ?? []
        for (const previous of group) {
          if (recommendations.length >= 8) break
          recommend(previous, side, [])
        }
        group.push(side)
        groups.set(key, group)
        if (recommendations.length >= 8) break
      }
      return c.json({ recommendations }, 200, { 'Cache-Control': 'no-store' })
    } catch { return c.json({ error: 'Не удалось подобрать подтверждённые сравнения.' }, 503) }
  })
  app.post('/candidates', async c => {
    let ref: ResultRef
    let scope: 'article' | 'corpus'
    try {
      const request = resultRefSchema.extend({ scope: z.enum(['article', 'corpus']).default('article') }).parse(await c.req.json())
      const { scope: requestedScope, ...reference } = request
      ref = reference
      scope = requestedScope
    } catch { return c.json({ error: 'Укажите адрес записи и допустимую область подбора.' }, 400) }
    try {
      const data = await publications.read(ref.publicationId, ref.versionId)
      const result = data?.results.find(item => item.id === ref.resultId)
      if (!data || !result) return c.json({ error: 'Запись не найдена в закреплённой опубликованной версии.' }, 404)
      const a = toSide(data, ref, result)
      const [proofs, datasets] = await Promise.all([
        readProofs(), scope === 'corpus' ? publications.listLatest(ref.publicationId) : [data],
      ])
      const candidates: ComparisonCandidate[] = []
      for (const dataset of datasets) {
        const version = scope === 'article' ? ref : dataset.version
        if (!version || (scope === 'corpus' && version.publicationId === ref.publicationId)) continue
        for (const item of dataset.results) {
          if ((scope === 'article' && item.id === result.id) || item.value === null || !result.dataset || !result.metric ||
              item.dataset !== result.dataset || item.metric !== result.metric) continue
          const b = toSide(dataset, { publicationId: version.publicationId, versionId: version.versionId, resultId: item.id }, item)
          const { status, reasons } = compareWithProofs(a, b, proofs)
          if (status === 'different') continue
          candidates.push({ ref: b.ref, title: b.title, method: item.method, generator: item.generator, rawValue: item.rawValue,
            status, reasons, sameExperiment: scope === 'article' && !!result.experimentId && result.experimentId === item.experimentId })
        }
      }
      candidates.sort((left, right) => Number(right.status === 'comparable') - Number(left.status === 'comparable') ||
        Number(right.sameExperiment) - Number(left.sameExperiment))
      return c.json({ candidates: candidates.slice(0, 8) }, 200, { 'Cache-Control': 'no-store' })
    } catch { return c.json({ error: 'Не удалось прочитать опубликованные данные. Повторите запрос.' }, 503) }
  })
  app.post('/', async c => {
    let refs
    try { refs = z.object({ a: resultRefSchema, b: resultRefSchema }).strict().parse(await c.req.json()) }
    catch { return c.json({ error: 'Укажите две записи с publicationId, versionId и resultId.' }, 400) }
    async function side(ref: ResultRef): Promise<ComparisonSide | null> {
      const data = await publications.read(ref.publicationId, ref.versionId)
      const result = data?.results.find(item => item.id === ref.resultId)
      if (!data || !result) return null
      return toSide(data, ref, result)
    }
    try {
      const [a,b] = await Promise.all([side(refs.a), side(refs.b)])
      if (!a || !b) return c.json({ error: 'Запись не найдена в закреплённой опубликованной версии.' }, 404)
      return c.json(compareWithProofs(a, b, await readProofs()), 200, { 'Cache-Control': 'no-store' })
    } catch { return c.json({ error: 'Не удалось прочитать опубликованные данные. Повторите запрос.' }, 503) }
  })
  return app
}
