import { readFile } from 'node:fs/promises'
import { join } from 'node:path'
import type { ArticleRun } from '../../shared/article.ts'
import { regionRunRequestSchema, type RegionTarget } from '../../shared/figure-run.ts'
import { Hono } from 'hono'
import { bodyLimit } from 'hono/body-limit'
import { exists, noStore, pdfHeaders, readJson, readTableArtifact, readTableRun, tableRunResponse } from './article-files.ts'
import { proseArtifactPath, reconciliationPath } from './article-publication-artifacts.ts'
import { readReconciliation } from './article-reconciliation.ts'
import type { openArticleRuns } from './article-run-store.ts'
import { explainReview, readReviewUsage, reviewEnabled, type ReviewConfig } from '../review-assistant.ts'
import { matchesReviewPdf, parseReviewArtifact } from '../review-case.ts'

const jsonHeaders = { ...noStore, 'Content-Type': 'application/json' }
const stageJsonFiles = new Map([
  ['article-text', 'article-text.json'],
  ['table-grid', 'table-grid.json'],
  ['figure', 'extracted.json'],
])

export function articleRunRoutes(
  store: string,
  runs: Pick<ReturnType<typeof openArticleRuns>, 'get' | 'result' | 'gridAttempt' | 'resumePublication'>,
  original: (articleId: string) => string,
  review: ReviewConfig = {},
  region?: {
    start(run: ArticleRun, target: RegionTarget): Promise<{ id: string } | { error: string; status: 404 | 409 | 422 | 503 }>
    apply(run: ArticleRun): boolean
  },
) {
  const app = new Hono()

  app.get('/:id', async c => {
    const run = runs.get(c.req.param('id'))
    if (!run) return c.json({ error: 'Прогон не найден.' }, 404)
    for (const figure of run.figures) {
      if (figure.status !== 'done' || !figure.remarkCount) continue
      const stage = runs.result(run.id, figure.id)
      if (!stage) continue
      try {
        const artifact = await readJson(join(stage.folder, 'extracted.json')) as {
          remarks: Array<{ kind: string; reason: string }>
        }
        figure.remarks = artifact.remarks.map(({ kind, reason }) => ({ kind, reason }))
      } catch {
        figure.error = 'Сохранённые замечания рисунка недоступны.'
      }
    }
    return c.json(run, 200, noStore)
  })

  app.get('/:id/source.pdf', async c => {
    const run = runs.get(c.req.param('id'))
    if (!run) return c.notFound()
    const filename = original(run.articleId)
    if (!(await exists(filename))) return c.notFound()
    const pdf = await readFile(filename)
    const sha = c.req.query('sha256')
    if ((sha !== undefined && run.pdfSha256 !== sha) || !matchesReviewPdf(pdf, run.pdfSha256)) return c.notFound()
    return c.body(pdf, 200, pdfHeaders)
  })

  app.get('/:id/prose-results.json', async c => {
    const run = runs.get(c.req.param('id'))
    if (!run) return c.notFound()
    const reportPath = reconciliationPath(store, run.id)
    if (run.reconciliationStatus === 'done') {
      const { report } = await readReconciliation(reportPath, run)
      const prose = report.artifacts.find(item => item.kind === 'prose')
      return prose ? c.json(prose.data, 200, noStore) : c.notFound()
    }
    const filename = proseArtifactPath(store, run.id)
    return run.publicationStatus && await exists(filename)
      ? c.body(await readFile(filename), 200, jsonHeaders)
      : c.notFound()
  })

  app.get('/:id/reconciliation.json', async c => {
    const run = runs.get(c.req.param('id'))
    if (!run || run.reconciliationStatus !== 'done') return c.notFound()
    const { report } = await readReconciliation(reconciliationPath(store, run.id), run)
    return c.json(report, 200, noStore)
  })

  app.post('/:id/region-runs', bodyLimit({ maxSize: 2048 }), async c => {
    const run = runs.get(c.req.param('id'))
    if (!run) return c.notFound()
    const request = regionRunRequestSchema.safeParse(await c.req.json().catch(() => null))
    if (!request.success) return c.json({ error: 'Укажите физическую страницу, рамку и SHA-256 PDF.' }, 400)
    const { pdfSha256, page, box, candidateId } = request.data
    if (pdfSha256 !== run.pdfSha256) return c.json({ error: 'Выбранная область относится к другой версии PDF.' }, 409)
    if (!region) return c.json({ error: 'Доставка прогона области недоступна.' }, 503)
    const result = await region.start(run, {
      stage: 'region', pdf_sha256: pdfSha256, parent_run_id: run.id, page, box, candidate_id: candidateId ?? null,
    })
    if ('error' in result) return c.json({ error: result.error }, result.status)
    return c.json({ id: result.id, status: 'queued' }, 202, { ...noStore, Location: `/api/article-runs/${result.id}` })
  })

  app.post('/:id/region/retry', async c => {
    const run = runs.get(c.req.param('id'))
    if (!run) return c.notFound()
    const stage = run.figures[0]
    const target = stage?.plan?.target
    if (target?.stage !== 'region' || (stage.status !== 'failed' && run.reconciliationStatus !== 'failed')) {
      return c.json({ error: 'Повтор доступен после технического сбоя прогона области.' }, 409)
    }
    if (!region) return c.json({ error: 'Доставка прогона области недоступна.' }, 503)
    const result = await region.start(run, { ...target, parent_run_id: run.id })
    if ('error' in result) return c.json({ error: result.error }, result.status)
    return c.json({ id: result.id, status: 'queued' }, 202, { ...noStore, Location: `/api/article-runs/${result.id}` })
  })

  app.post('/:id/region/apply', async c => {
    const id = c.req.param('id')
    const run = runs.get(id)
    if (!run) return c.notFound()
    if (run.figures[0]?.plan?.target.stage !== 'region' || run.reconciliationStatus !== 'done') {
      return c.json({ error: 'Дождитесь готового итога прогона области.' }, 409)
    }
    const { report } = await readReconciliation(reconciliationPath(store, id), run)
    if (!report.summary?.confirmed.length) return c.json({ error: 'Нет подтверждённых результатов для выпуска.' }, 409, noStore)
    if (!region) return c.json({ error: 'Выпуск версии недоступен.' }, 503)
    if (run.publication) return c.json({ id, status: 'done', publication: run.publication }, 200, noStore)
    if (!region.apply(run)) return c.json({ error: 'Не удалось поставить выпуск в очередь.' }, 409, noStore)
    return c.json({ id, status: 'queued' }, 202, noStore)
  })

  app.post('/:id/publication/retry', c => {
    const id = c.req.param('id')
    if (!runs.get(id)) return c.notFound()
    if (!runs.resumePublication(id)) {
      return c.json({ error: 'Для повтора нужен сохранённый итог и завершившаяся ошибкой публикация.' }, 409, noStore)
    }
    return c.json({ id, status: 'queued' }, 202, noStore)
  })

  app.get('/:id/stages/:stageId', async c => {
    const { id, stageId } = c.req.param()
    const stage = runs.result(id, stageId)
    if (stage?.kind !== 'table') return c.json({ error: 'Результат не найден.' }, 404)
    if (!review.reviewModel) return c.json(await readTableRun(stage.document, stage.folder), 200, noStore)
    const artifact = parseReviewArtifact(await readFile(join(stage.folder, 'run.json')))
    const response = tableRunResponse(artifact.run, stage.document)
    if (artifact.pdfSha256 === runs.get(id)?.pdfSha256 && reviewEnabled(review, artifact)) {
      response.reviewArtifactId = artifact.artifactId
    }
    return c.json(response, 200, noStore)
  })

  const reviewLocation = ({ id, stageId }: { id: string; stageId: string }) => {
    const stage = runs.result(id, stageId)
    const run = stage?.kind === 'table' ? runs.get(id) : null
    return run && stage ? { directory: stage.folder, pdfPath: original(run.articleId), expectedPdfSha256: run.pdfSha256 } : null
  }
  app.post('/:id/stages/:stageId/review', bodyLimit({ maxSize: 2048 }), c =>
    explainReview(c.req.raw, () => reviewLocation(c.req.param()), review))
  app.get('/:id/stages/:stageId/review', c => readReviewUsage(c.req.raw, () => reviewLocation(c.req.param()), review))

  app.get('/:id/stages/:stageId/attempts/:number/table-grid.json', async c => {
    const { id, stageId, number } = c.req.param()
    if (!/^[1-9]\d{0,5}$/.test(number)) return c.notFound()
    const folder = runs.gridAttempt(id, stageId, Number(number))
    const filename = folder && join(folder, 'table-grid.json')
    if (!filename || !(await exists(filename))) return c.notFound()
    return c.body(await readFile(filename), 200, jsonHeaders)
  })

  app.get('/:id/stages/:stageId/:file', async c => {
    const { id, stageId, file } = c.req.param()
    const stage = runs.result(id, stageId)
    if (!stage) return c.notFound()
    const readFileName = stageJsonFiles.get(stage.kind)
    if (file === readFileName || (stage.kind === 'table' && file === 'article-context-followup.json')) {
      const filename = join(stage.folder, file)
      if (!(await exists(filename))) return c.notFound()
      return c.body(await readFile(filename), 200, jsonHeaders)
    }
    const data = stage.kind === 'table' && await readTableArtifact(stage.folder, file)
    return data ? c.body(data.body, 200, data.headers) : c.notFound()
  })

  return app
}
