import { publicationErrorMessage } from './article-publication-artifacts.ts'
import { ensureReconciliation } from './article-reconciliation.ts'
import type { ArticleReleaseWork } from './article-release-store.ts'
import type { openArticleRuns } from './article-run-store.ts'
import type { PublicationStore } from '../publication/publication-store.ts'
import { publishPartialTable } from '../publication/partial-table.ts'

export function startArticlePublication(
  runs: ArticleReleaseWork & Pick<ReturnType<typeof openArticleRuns>, 'get' | 'result'>,
  options: {
    store: string
    sourcePdf(articleId: string): string
    publications?: Pick<PublicationStore, 'forRun' | 'publish'>
    pythonTask(args: string[], timeout: number): Promise<Buffer>
    shutdown: AbortSignal
  },
) {
  const { store, sourcePdf, publications, pythonTask, shutdown } = options

  async function publish(runId: string, token: string) {
    const run = runs.get(runId)
    if (!run) throw new Error('Прогон не найден.')
    const region = run.figures[0]?.plan?.target
    const coveragePlan = run.coverage
    if (!coveragePlan && region?.stage !== 'region') throw new Error('План прогона не найден.')
    if (!publications) throw new Error('Хранилище опубликованных статей недоступно.')
    const publicationId = 'publication-' + run.pdfSha256
    const existing = await publications.forRun(publicationId, runId)
    if (existing) {
      if (existing.pdfSha256 !== run.pdfSha256 || existing.runId !== runId) {
        throw new Error('Опубликованная версия относится к другому PDF или прогону.')
      }
      return { version: existing }
    }
    const pdfPath = sourcePdf(run.articleId)
    const { artifacts, report } = await ensureReconciliation(store, runs, run, token, pythonTask)
    if (region?.stage === 'region') {
      if (!report.summary?.confirmed.length) return { version: null, diagnostic: 'Нет подтверждённых результатов области.' }
      if (!runs.renewRelease(runId, token)) throw new Error('Истёк срок выполнения публикации.')
      return { version: await publications.publish({
        publicationId, runId, pdfPath, pdfSha256: run.pdfSha256, document: run.document,
        coverage: {
          partial: true, summary: `Область на странице ${region.page}. Подтверждённых результатов: ${report.summary.confirmed.length}.`,
          region, rejectedArtifacts: report.rejectedArtifacts,
        },
        artifacts: artifacts.map(({ id, artifact }) => ({ id, runId, data: artifact })),
        relationships: report.relationships,
      }) }
    }
    if (!coveragePlan) throw new Error('План полного прогона не найден.')
    const { rejectedArtifacts, proseError } = report
    const rejectedById = new Map(rejectedArtifacts.map(item => [item.id, item.reason]))
    const tableArtifacts = artifacts.filter(item => item.kind === 'table')
    const figureArtifacts = artifacts.filter(item => item.kind === 'figure')
    const proseResult = artifacts.find(item => item.kind === 'prose')?.artifact
    const { figures } = run
    const detectedFigures = figures.length + coveragePlan.deferredFigures.length
    const counts = (entries: typeof artifacts) => entries.reduce((sum, item) => sum + item.artifact.data.results.length, 0)
    const tableResults = counts(tableArtifacts)
    const figureResults = counts(figureArtifacts)
    const proseResults = proseResult?.data.results.length ?? 0
    const publicationArtifacts = artifacts.map(({ id, kind, artifact }) =>
      ({ id, runId, data: kind === 'table' ? publishPartialTable(artifact) : artifact }))
    const publishedTables = new Map(publicationArtifacts.filter(item => item.data.data.table != null)
      .map(item => [item.id, item.data.data.results]))
    const partialResults = [...publishedTables.values()].reduce((sum, rows) =>
      sum + rows.filter(row => row.partial).length, 0)
    if (tableResults + proseResults + figureResults + partialResults === 0) {
      const diagnostics = rejectedArtifacts.map(({ id, file, reason }) => `${id}/${file}: ${reason}`)
      if (proseError) diagnostics.push('Текст: ' + proseError)
      return { version: null, diagnostic: diagnostics.join('; ') || undefined }
    }

    const figureRemarks = figureArtifacts.reduce((sum, item) => sum + item.artifact.data.remarks.length, 0)
    const tableReadingIssues = tableArtifacts.some(item => item.artifact.data.remarks
      .some(remark => remark.kind === 'reading-conflict' || remark.kind === 'reading-defect'))
    const textStatus = proseResult ? 'done' : 'failed'
    const partial = Boolean(partialResults || coveragePlan.failedStages || coveragePlan.excludedCandidates.length
      || coveragePlan.discoveryWarnings.length || coveragePlan.discoveryLimitations.length
      || coveragePlan.ambiguousBindings || tableReadingIssues || textStatus !== 'done' || proseResult?.data.remarks.length
      || detectedFigures > figureArtifacts.length || figureRemarks || rejectedArtifacts.length
      || report.relationships.some(item => item.status !== 'agreement'))
    const summary = [
      `Таблицы: ${tableArtifacts.length} из ${coveragePlan.plannedStages} этапов, ${tableResults} полных, ${partialResults} с открытыми полями`,
      `результаты из текста: ${proseResults}`,
      `рисунки: ${figureArtifacts.length} из ${detectedFigures} этапов, ${figureResults} результатов`,
    ].join(', ') + '.' + (rejectedArtifacts.length ? ` Отклонено артефактов: ${rejectedArtifacts.length}.` : '')
    const coverage = {
      ...coveragePlan, partial, summary, rejectedArtifacts,
      tables: {
        results: tableResults, partialResults,
        missing: run.stages.filter(stage => stage.status === 'failed' || !publishedTables.get(stage.id)?.length || rejectedById.has(stage.id))
          .map(({ id, page, table, error, emptyResultReason }) => ({ page, table, reason: rejectedById.get(id) ?? error ?? emptyResultReason ?? null })),
      },
      prose: { status: textStatus, results: proseResults, remarks: proseResult?.data.remarks.length ?? 0, error: proseError },
      figures: {
        detected: detectedFigures, completed: figureArtifacts.length, results: figureResults, remarks: figureRemarks,
        stages: figures.map(({ id, page, label, status, resultCount, error }) => ({
          page, label, status: rejectedById.has(id) ? 'failed' : status,
          results: rejectedById.has(id) ? 0 : resultCount ?? 0, error: rejectedById.get(id) ?? error,
          remarks: figureArtifacts.find(artifact => artifact.id === id)?.artifact.data.remarks.map(({ kind, reason }) => ({ kind, reason })) ?? [],
        })),
      },
    }
    if (!runs.renewRelease(runId, token)) throw new Error('Истёк срок выполнения публикации.')
    return { version: await publications.publish({
      publicationId, runId, pdfPath, pdfSha256: run.pdfSha256, document: run.document, coverage,
      artifacts: publicationArtifacts,
      relationships: report.relationships,
    }) }
  }

  let inFlight = false
  async function tick() {
    if (shutdown.aborted || inFlight) return
    const claim = runs.claimRelease()
    if (!claim) return
    inFlight = true
    const { runId, token } = claim
    const heartbeat = setInterval(() => {
      try {
        runs.renewRelease(runId, token)
      } catch (error) {
        console.error('Состояние публикации:', publicationErrorMessage(error))
      }
    }, 4_000).unref()
    try {
      if (claim.publish) {
        const { version, diagnostic } = await publish(runId, token)
        runs.finishRelease(runId, token, version, diagnostic)
      } else {
        const run = runs.get(runId)
        if (!run) throw new Error('Прогон не найден.')
        await ensureReconciliation(store, runs, run, token, pythonTask)
        runs.finishRelease(runId, token, null, undefined, true)
      }
    } catch (error) {
      const detail = publicationErrorMessage(error)
      runs.retryRelease(runId, token, detail.slice(0, 1_000))
      console.error('Публикация прогона:', detail)
    } finally {
      clearInterval(heartbeat)
      inFlight = false
    }
  }

  const dispatch = () => {
    void tick().catch(error => console.error('Результат публикации:', publicationErrorMessage(error)))
  }
  const timer = setInterval(dispatch, 750).unref()
  shutdown.addEventListener('abort', () => clearInterval(timer), { once: true })
  dispatch()
}
