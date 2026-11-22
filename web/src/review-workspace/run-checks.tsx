import { useState } from 'react'
import type { Article, ArticleRun } from '../shared/article'
import type { reconciliationSchema } from '../shared/reconciliation'
import type { parsePublicationArtifact } from '../server/publication/publication-data'
import type { z } from 'zod'
import type { Source } from './pdf'
import { labUrl, post, runPath } from './api'

type Artifact = ReturnType<typeof parsePublicationArtifact>['data']
export type Report = z.infer<typeof reconciliationSchema>
type Locator = Omit<Source, 'url' | 'trigger'> & { id: string }
type Check = { id: string; title: string; detail?: string; source?: Locator; kind?: 'result'; otherSources?: Locator[] }

export function runChecks(run: ArticleRun, article: Article, report?: Report) {
  const region = run.figures[0]?.plan?.target
  const checks: Check[] = []
  const area: Locator | undefined = region?.stage === 'region'
    ? { id: 'operator-region', page: region.page, box: region.box, candidateId: region.candidate_id ?? undefined, locatorText: 'Рамка оператора' }
    : undefined
  function candidateSource(id: string, page: number): Locator {
    const candidate = run.pdfSha256 === article.sha256 ? article.candidates.find(item => item.id === id) : undefined
    const plot = run.figures.find(figure => figure.candidateId === id)?.plan?.target
    const figureBox = plot?.stage === 'figure' && plot.region.precision === 'figure' ? plot.region.box : null
    return { id, page, box: figureBox ?? candidate?.region?.box ?? candidate?.box ?? null,
      locatorText: candidate?.text || undefined, candidateId: candidate?.kind === 'figure' ? id : undefined }
  }
  for (const entry of report?.artifacts ?? []) {
    const data = entry.data as Artifact
    const figure = run.figures.find(item => item.id === entry.id)
    const table = run.stages.find(item => item.id === entry.id)
    const stage = figure ?? table
    const fallback = area ?? (stage && candidateSource(stage.candidateId, stage.page))
    const evidence = data.evidence ?? []
    const located = (row: Artifact['remarks'][number] | Artifact['results'][number], id: string) => {
      const sources = (row.source.evidence_ids ?? []).map(ref => evidence.find(item => item.id === ref))
      const precise = sources.find(item => item?.box)
      const page = precise?.page ?? row.source.page
      return {
        id, page, box: precise?.box ?? row.source.box ?? (fallback?.page === page ? fallback.box : null),
        locatorText: precise?.text || row.source.fragment || fallback?.locatorText,
        candidateId: fallback?.page === page ? fallback.candidateId : undefined,
      }
    }
    for (const remark of data.remarks) {
      const id = `${entry.id}:${remark.id}`
      checks.push({ id, title: remark.reason, detail: remark.raw_text, source: located(remark, id) })
    }
    const confirmed = new Set(report?.summary?.confirmed.filter(item => item.artifact_id === entry.id).map(item => item.result_id))
    if (area) for (const result of data.results.filter(row => confirmed.has(row.id))) {
      const id = `${entry.id}:${result.id}`
      const fields = Object.entries(result.parameters ?? {}).map(([name, value]) => `${name}=${value}`)
      const references = new Set(Object.values(result.condition_sources ?? {}).flat())
      checks.push({
        id, kind: 'result', title: [result.dataset, result.metric, result.raw_value, ...fields].filter(Boolean).join(' · '),
        detail: [result.method, result.generator].filter(Boolean).join(' · '), source: located(result, id),
        otherSources: evidence.filter(item => references.has(item.id)).map(item => ({
          id: item.id, page: item.page, box: item.box ?? null, locatorText: item.text,
        })),
      })
    }
  }
  for (const figure of run.figures) {
    if (figure.status !== 'failed' && (figure.status !== 'done' || (figure.resultCount ?? 0) > 0
        || report?.artifacts.some(item => item.id === figure.id && (item.data as Artifact).remarks.length))) continue
    checks.push({ id: figure.id, title: figure.error || figure.remarks?.[0]?.reason || 'Рисунок не дал подтверждённых результатов.',
      source: area ?? candidateSource(figure.candidateId, figure.page) })
  }
  for (const stage of run.stages) {
    const reason = stage.error || stage.emptyResultReason
    if (reason && !report?.artifacts.some(item => item.id === stage.id && (item.data as Artifact).remarks.length)) {
      checks.push({ id: stage.id, title: reason, source: candidateSource(stage.candidateId, stage.page) })
    }
  }
  for (const candidate of [...(run.coverage?.excludedCandidates ?? []), ...(run.coverage?.deferredFigures ?? [])]) {
    const reasons = {
      'duplicate-address': 'Подпись повторяет адрес другого кандидата.',
      'unsupported-address': 'Карта не содержит поддерживаемого адреса.',
      'prompt-template': 'Подпись относится к шаблону промпта.',
      'table-bounds-review': 'Границы таблицы требуют проверки.',
      'figure-reading-deferred': 'Рисунок не входил в план прогона.',
    }
    checks.push({ id: candidate.candidateId, title: reasons[candidate.reason], detail: candidate.text,
      source: candidateSource(candidate.candidateId, candidate.page) })
  }
  for (const [index, rejected] of (report?.rejectedArtifacts ?? []).entries()) {
    const figure = run.figures.find(item => item.id === rejected.id)
    const stage = run.stages.find(item => item.id === rejected.id)
    const candidate = figure ?? stage
    checks.push({ id: `rejected-${index}`, title: rejected.reason,
      source: area ?? (candidate && candidateSource(candidate.candidateId, candidate.page)) })
  }
  for (const [index, warning] of (run.coverage?.discoveryWarnings ?? []).entries()) {
    checks.push({ id: `warning-${index}`, title: warning.reason, source: { id: `warning-${index}`, page: warning.page, box: null } })
  }
  for (const [index, limitation] of (run.coverage?.discoveryLimitations ?? []).entries()) {
    checks.push({ id: `limitation-${index}`, title: limitation })
  }
  const proseError = report?.proseError || run.articleText?.error
  if (!area && proseError) checks.push({ id: 'prose-error', title: proseError })
  if (run.publicationNote) checks.push({ id: 'release-note', title: run.publicationNote })
  return { area, checks }
}


export function RunChecks({ run, article, checks, area, confirmed, loading, selectedId, select, preview, open, refresh }: {
  run: ArticleRun; article: Article; checks: Check[]; area?: Locator; confirmed: number; loading: boolean;
  selectedId: string | null; select: (id: string | null, source?: Locator, trigger?: HTMLElement) => void;
  preview: (source: Locator, trigger: HTMLElement) => void; open: (id: string) => void; refresh: () => Promise<unknown>;
}) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const current = checks.find(check => check.id === selectedId)
  const remarks = checks.filter(check => check.kind !== 'result')
  const isPending = (status?: string) => status === 'queued' || status === 'running'
  const releasing = isPending(run.publicationStatus)
  const working = [run.reconciliationStatus, run.articleText?.status,
    ...run.stages.map(stage => stage.status), ...run.figures.map(figure => figure.status)].some(isPending)
  const failed = run.figures[0]?.status === 'failed' || run.reconciliationStatus === 'failed'
  const target = run.figures[0]?.plan?.target

  async function act(action: 'retry' | 'apply') {
    setBusy(true)
    setError('')
    try {
      const response = await post<{ id: string }>(runPath(run.id) + '/region/' + action, {})
      if (response.id !== run.id) open(response.id)
      else await refresh()
    } catch (failure) { setError((failure as Error).message) } finally { setBusy(false) }
  }

  return <section className="card checks">
    <h2>Проверки</h2>
    <p role="status">{area ? working ? 'Разбираем область…' : failed ? 'Прогон области завершился с ошибкой.'
      : releasing ? 'Выпускаем версию…' : 'Разбор области завершён.'
      : working ? 'Прогон выполняется…' : confirmed ? 'Прогон завершён.' : 'Нет подтверждённых результатов.'}</p>
    {loading && <p role="status">Загружаем итог сверки…</p>}
    {(error || run.reconciliationError || run.publicationError) && <p role="alert">{error || run.reconciliationError || run.publicationError}</p>}
    {!area && <div className="actions">
      <button onClick={event => preview({ id: 'source-pdf', page: run.figures[0]?.page ?? run.stages[0]?.page ?? 1, box: null },
        event.currentTarget)}>Открыть исходный PDF</button>
    </div>}
    {area && <div className="actions">
      {failed && <button disabled={busy || working} onClick={() => void act('retry')}>Повторить разбор</button>}
      {run.publication ? <a className="link action" href={labUrl({ article: article.id, run: run.id, tab: 'results' })}>Открыть опубликованную версию</a>
        : confirmed > 0 && <button className="action" disabled={busy || releasing} onClick={() => void act('apply')}>Применить</button>}
      {target?.stage === 'region' && <a className="link" href={labUrl({ article: article.id, run: target.parent_run_id, tab: 'checks' })}>Предыдущий прогон</a>}
    </div>}
    <div className="issue-list lab-issues">
      {area && <button className={!current ? 'chosen' : ''} onClick={event => select(null, area, event.currentTarget)}>Рамка оператора · стр. {area.page}</button>}
      {remarks.map(check =>
        <button key={check.id} className={current?.id === check.id ? 'chosen' : ''}
          onClick={event => select(check.id, check.source, event.currentTarget)}>{check.title}</button>)}
      {!working && !loading && checks.length === 0 && !area && <p>Замечаний нет.</p>}
    </div>
    {area && <><h3>Подтверждённые результаты · {confirmed}</h3>
      <div className="issue-list lab-issues">{checks.filter(check => check.kind === 'result').map(check =>
        <button key={check.id} className={current?.id === check.id ? 'chosen' : ''}
          onClick={event => select(check.id, check.source, event.currentTarget)}>{check.title}</button>)}</div>
    </>}
    {current && <article className="review">
      <h3>{current.title}</h3>
      {current.detail && <p>{current.detail}</p>}
      {current.source && <div className="actions">
        <button onClick={event => preview(current.source!, event.currentTarget)}>Источник · стр. {current.source.page}</button>
      </div>}
      {!!current.otherSources?.length && <details className="review-conditions"><summary>Источники условий</summary>
        <div className="actions">{current.otherSources.map((source, index) => <button key={source.id}
          onClick={event => preview(source, event.currentTarget)}>Фрагмент {index + 1} · стр. {source.page}</button>)}</div>
      </details>}
    </article>}
  </section>
}
