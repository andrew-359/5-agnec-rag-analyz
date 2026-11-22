import { lazy, Suspense, useEffect, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider, useMutation, useQuery } from '@tanstack/react-query'
import { overviewTranslationSchema, type Article, type ArticleRun, type LibraryArticle, type OverviewExcerpt } from '../shared/article'
import type { Result } from '../shared/schema'
import { articlePath, json, labUrl, loadArticle, readPublication, readRun, runPath, runPdf, start, updateQuery } from './api'
import { Checks, Comparisons, ErrorNotice, Passport, Results, resultMatches, type ResultFilters } from './publication'
import type { Source } from './pdf'
import { RunChecks, runChecks, type Report } from './run-checks'
import { Upload } from './upload'
import { PublishedWorkspace } from './published-workspace'
import { PublishedPlots } from './published-plots'
import { ArticleMap } from './article-map'
import { TableReading } from './object-reading'
import { followWorkspaceLink, navigateWorkspace, useWorkspaceLocation } from './navigation'
import { AttemptTimeline, Distribution, type ChartItem } from './dashboard-visuals'
import './base.css'
import './workspace.css'

const client = new QueryClient({ defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } } })
const SourcePanel = lazy(() => import('./pdf').then(module => ({ default: module.SourcePanel })))
const tabs = [['overview', 'Обзор'], ['results', 'Результаты'], ['compare', 'Сравнения'], ['checks', 'Проверки'], ['run', 'Прогон']] as const
const statuses: Record<string, string> = { queued: 'В очереди', running: 'В работе', done: 'Готово', failed: 'Ошибка', empty: 'Нет результатов' }
const pending = (status?: string) => status === 'queued' || status === 'running'

function StatusBadge({ label, kind = 'neutral' }: { label: string; kind?: 'neutral' | 'partial' | 'done' | 'unavailable' | 'empty' }) {
  const icon = { neutral: '', partial: '◐', done: '✓', unavailable: '!', empty: '—' }[kind]
  return <span className={`badge badge-${kind}`}>{icon && <span className="badge-icon" aria-hidden="true">{icon}</span>}{label}</span>
}

function App() {
  const address = useWorkspaceLocation()
  const url = new URL(address, location.origin)
  const params = url.searchParams
  const publication = url.pathname.startsWith('/publications/')
  const plots = /^\/plots(?:\/([\w-]+))?\/?$/.exec(url.pathname)
  const articleId = params.get('article')
  const uploading = !publication && !plots && params.has('upload')
  return <div className="workspace-shell" onClick={followWorkspaceLink}>
    <header className="top"><div className="top-inner"><a className="link brand" href={labUrl()}>RAG</a>
      {uploading && <a className="link" href={labUrl()}>Библиотека</a>}
    </div></header>
    {uploading ? <Upload /> : <main className={articleId || publication || plots ? 'article-page' : 'library-page'}>
      {plots ? <PublishedPlots version={plots[1]} /> : publication ? <PublishedWorkspace address={url} /> : articleId ? <Workspace key={`${articleId}:${params.get('run') ?? 'latest'}`} articleId={articleId} params={params} /> : <Library />}
    </main>}
  </div>
}

function Library() {
  const [filter, setFilter] = useState('')
  const query = useQuery({
    queryKey: ['articles'],
    queryFn: ({ signal }) => json<{ articles: LibraryArticle[]; unavailable: number }>('/api/articles', { signal }),
  })
  if (query.error) return <ErrorNotice error={query.error} retry={() => void query.refetch()} />
  if (!query.data) return <p role="status">Загружаем библиотеку…</p>
  const { articles, unavailable } = query.data
  const visible = articles.filter(article => `${article.title} ${article.document}`.toLowerCase().includes(filter.trim().toLowerCase()))
  return <>
    <section className="heading article-heading"><div><h1>Библиотека</h1><p>Статьи и опубликованные результаты</p></div>
      <div className="actions"><a className="link quiet" href="/plots">Опубликованные графики</a>
      <a className="link action" href={labUrl({ upload: '1' })}>Разобрать статью</a></div></section>
    <div className="query"><label className="search-field">Поиск статей<input type="search" placeholder="Название или файл" value={filter}
      onChange={event => setFilter(event.target.value)} /></label></div>
    {unavailable > 0 && <p>Недоступных карт: {unavailable}</p>}
    <div className="library">
      {visible.length ? visible.map(article => <section className="card library-item" key={article.id}>
        <div><div className="eyebrow">{article.document}
          <StatusBadge label={article.status} kind={article.resultUrl ? 'done' : article.status.includes('нет подтверждённых')
            ? 'empty' : /ошибк|требует проверки/.test(article.status) ? 'unavailable' : 'neutral'} /></div>
          <h2>{article.title}</h2><small>{article.pageCount} стр.{Number.isFinite(article.uploadedAt) && ` · ${new Date(article.uploadedAt).toLocaleString('ru-RU', { dateStyle: 'short', timeStyle: 'short' })}`}</small></div>
        <div className="actions">
          <a className="link article-open" href={labUrl({ article: article.id })}>Открыть статью</a>
          {article.runId && <a className="link quiet" href={labUrl({ article: article.id, run: article.resultRunId ?? article.runId,
            tab: article.resultUrl ? 'results' : 'run' })}>{article.resultUrl ? 'Результаты' : 'Открыть прогон'}</a>}
        </div>
      </section>) : <p>{articles.length ? 'По запросу статей нет.' : 'Загрузите первую статью.'}</p>}
    </div>
  </>
}

function Overview({ article, runId, preview }: { article: Article; runId: string | null; preview: (source: Source) => void }) {
  const [starting, setStarting] = useState(false)
  const [error, setError] = useState('')
  const [showTranslation, setShowTranslation] = useState(false)
  const translation = useMutation({
    mutationFn: async () => overviewTranslationSchema.parse(await json(articlePath(article.id) + '/overview/translation', { method: 'POST' })),
    onSuccess: () => setShowTranslation(true),
  })
  const translated = showTranslation ? translation.data : undefined
  const abstract = article.overview?.abstract
  const conclusion = article.overview?.conclusion
  const conclusionText = translated?.conclusion ?? conclusion?.text
  function openExcerpt(excerpt: OverviewExcerpt, trigger: HTMLButtonElement) {
    preview({ page: excerpt.page, box: excerpt.box, url: articlePath(article.id) + '/source.pdf', trigger })
  }
  async function startRun() {
    setError('')
    setStarting(true)
    try {
      const run = await start(article.id)
      navigateWorkspace(labUrl({ article: article.id, run: run.id, tab: 'run' }))
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : 'Не удалось начать разбор.')
      setStarting(false)
    }
  }
  return <>
    <section className="card"><h2>О статье</h2>
      {article.overview?.topic && <p>{article.overview.topic}</p>}
      {article.overviewTranslationAvailable && (article.overview?.abstract || article.overview?.conclusion) &&
        <div className="actions">
          <button type="button" disabled={translation.isPending} onClick={() => {
            if (translation.data) setShowTranslation(value => !value)
            else translation.mutate()
          }}>{translation.isPending ? 'Переводим…' : showTranslation ? 'Показать оригинал'
            : translation.data ? 'Показать перевод' : translation.isError ? 'Повторить перевод' : 'Перевести на русский'}</button>
          {translated && <small className="muted">Машинный перевод</small>}
        </div>}
      {translation.isError && <p role="alert">{translation.error.message}</p>}
      <p lang={translated?.abstract ? 'ru' : undefined}>{translated?.abstract ?? abstract?.text ?? article.document}</p>
      {abstract && <button type="button" className="quiet" onClick={event => openExcerpt(abstract, event.currentTarget)}>
        Источник аннотации · стр. {abstract.page}
      </button>}
      {conclusionText && <details><summary>Выводы авторов</summary>
        <p lang={translated?.conclusion ? 'ru' : undefined}>{conclusionText}</p>
        {conclusion && <button type="button" className="quiet" onClick={event => openExcerpt(conclusion, event.currentTarget)}>
          Источник выводов · стр. {conclusion.page}
        </button>}
      </details>}
      {!runId && <div className="actions"><button className="action" disabled={starting} onClick={startRun}>
        {starting ? 'Ставим разбор в очередь…' : error ? 'Повторить запуск' : 'Запустить разбор'}</button>
        {error && <p role="alert">{error}</p>}</div>}
    </section>
    <ArticleMap article={article} preview={preview} />
  </>
}

type Attempted = { attempts: { strategy: string | null }[]; strategy?: string }
function reader(stage: Attempted, planned?: string, fallback?: { strategy: string; reason: string } | null) {
  const actual = stage.attempts.findLast(attempt => attempt.strategy)?.strategy ?? stage.strategy
  if (!actual || !planned || actual === planned) return actual ?? planned
  return `План: ${planned} · Применён: ${actual}${fallback?.strategy === actual ? ' · ' + fallback.reason : ''}`
}

function Progress({ article, run, preview }: {
  article: Article; run: ArticleRun; preview: (source: Source) => void
}) {
  const steps = [
    ...(run.articleText ? [{ id: run.articleText.id, label: 'Текст статьи', status: run.articleText.status,
      error: run.articleText.error, reader: reader(run.articleText) }] : []),
    ...run.stages.map(stage => ({ id: stage.id, label: `Таблица ${stage.table} · стр. ${stage.page}`,
      status: stage.gridRead?.status === 'running' ? 'running' : stage.status, error: stage.error,
      reader: reader(stage.gridRead ?? stage, stage.strategyPlan?.strategy, stage.strategyPlan?.fallback) })),
    ...run.figures.map(stage => ({ id: stage.id, label: stage.plan?.target.stage === 'region' ? `Область · стр. ${stage.page}`
      : `Рисунок ${stage.label ?? ''} · стр. ${stage.page}`,
      status: stage.status, error: stage.error, reader: reader(stage, stage.plan?.selection.mode === 'explicit' ? stage.plan.selection.strategy_id : undefined) })),
    ...(run.reconciliationStatus ? [{ id: 'reconciliation', label: 'Сверка результатов', status: run.reconciliationStatus,
      error: run.reconciliationError, reader: undefined }] : []),
    ...(run.publicationStatus ? [{ id: 'publication', label: 'Сборка выпуска', status: run.publicationStatus,
      error: run.publicationError ?? run.publicationNote, reader: undefined }] : []),
  ]
  const active = steps.some(stage => pending(stage.status))
  const message = run.publication ? `Результаты опубликованы${run.publication.coverage.partial ? ' · частичный охват' : ''}`
    : active ? 'Идёт разбор' : run.publicationStatus === 'empty' ? 'Нет подтверждённых результатов'
    : steps.some(stage => stage.status === 'failed') ? 'Разбор завершён с ошибками' : 'Обработка завершена'
  const failed = steps.filter(step => step.status === 'failed')
  const done = steps.filter(step => step.status === 'done').length
  const running = steps.filter(step => step.status === 'running').length
  const queued = steps.filter(step => step.status === 'queued').length
  const empty = steps.filter(step => step.status === 'empty').length
  const stageCounts: ChartItem[] = [
    { label: 'Готово', count: done, tone: 'blue' },
    { label: 'В работе', count: running, tone: 'ink' },
    { label: 'В очереди', count: queued, tone: 'slate' },
    { label: 'Без результатов', count: empty, tone: 'amber' },
    { label: 'Сбой', count: failed.length, tone: 'red' },
  ]
  const singleTable = !run.coverage && !run.publicationStatus && !run.figures.length && run.stages.length === 1
    ? run.stages[0] : undefined
  return <section className="card"><h2>Прогон</h2><p role="status">{message}</p>
    <Distribution label={`Этапы прогона · ${done} из ${steps.length} готовы`} items={stageCounts.filter(item => item.count > 0)} />
    <div className="actions">
      {run.publication && <a className="link action" href={labUrl({ article: article.id, run: run.id, tab: 'results' })}>Открыть результаты</a>}
      {!active && <a className="link" href={labUrl({ article: article.id, run: run.id, tab: 'checks' })}>Проверки</a>}
    </div>
    {failed.length > 0 && <p className="notice">Сбои: {failed.map(step => step.label).join(', ')}.</p>}
    <details><summary>Этапы · {done} успешно
      {failed.length > 0 && ` · ${failed.length} со сбоем`}
      {active && ` · ${running + queued} в обработке или очереди`}
    </summary>
      <ol className="stages">{steps.map(step => <li key={step.id}>
        <strong>{step.label}</strong><StatusBadge label={statuses[step.status] ?? step.status}
          kind={step.status === 'done' ? 'done' : step.status === 'failed' ? 'unavailable' : step.status === 'empty' ? 'empty' : 'neutral'} />
        {step.reader && <small>{step.reader}</small>}{step.error && <p>{step.error}</p>}
      </li>)}</ol>
    </details>
    <AttemptTimeline stages={[
      ...(run.articleText ? [{ label: 'Текст статьи', attempts: run.articleText.attempts }] : []),
      ...run.stages.flatMap(stage => {
        const label = `Таблица ${stage.table} · стр. ${stage.page}`
        return [{ label, attempts: stage.attempts }, ...(stage.gridRead
          ? [{ label: `Сетка · ${label}`, attempts: stage.gridRead.attempts }] : [])]
      }),
      ...run.figures.map(stage => ({ label: `${stage.label ? `Рисунок ${stage.label}` : 'Область'} · стр. ${stage.page}`, attempts: stage.attempts })),
    ]} />
    {singleTable?.status === 'done' && <TableReading run={run} stageId={singleTable.id} preview={preview} />}
  </section>
}

function Workspace({ articleId, params }: { articleId: string; params: URLSearchParams }) {
  const requestedTab = params.get('tab')
  const address = tabs.find(([id]) => id === requestedTab)?.[0] ?? 'overview'
  const articleQuery = useQuery({
    queryKey: ['article', articleId, params.get('run')],
    queryFn: ({ signal }) => loadArticle(articleId, params.get('run'), signal),
  })
  const article = articleQuery.data?.article
  const latestResultRun = articleQuery.data?.resultRunId
  const runId = params.get('run') ?? ((address === 'results' || address === 'compare' || address === 'checks') ? latestResultRun : null)
    ?? articleQuery.data?.runId
  const tab = address === 'run' && !runId ? 'overview' : address
  const runQuery = useQuery({
    queryKey: ['run', articleId, runId], enabled: !!runId,
    queryFn: ({ signal }) => readRun(runId!, articleId, signal),
    refetchInterval: query => {
      const run = query.state.data
      if (!run || run.publication || query.state.status === 'error' ||
          ['empty', 'failed'].includes(run.publicationStatus ?? '')) return false
      return pending(run.publicationStatus) || pending(run.reconciliationStatus) ||
        [run.articleText, ...run.stages, ...run.figures].some(stage => pending(stage?.status)) ? 2500 : false
    },
  })
  const run = runQuery.data
  const version = run?.publication
  const [passport, setPassport] = useState(false)
  const publication = useQuery({
    queryKey: ['publication', version?.publicationId, version?.versionId],
    enabled: !!article && !!version && (passport || (tab !== 'run' && tab !== 'overview')),
    queryFn: ({ signal }) => readPublication(version!, article!, signal),
  })
  const data = publication.data
  const region = run?.figures[0]?.plan?.target
  const draftChecks = tab === 'checks' && run && (!version || region?.stage === 'region')
  const report = useQuery({
    queryKey: ['run-reconciliation', runId],
    queryFn: ({ signal }) => json<Report>(runPath(runId!) + '/reconciliation.json', { signal }),
    enabled: Boolean(draftChecks && run.reconciliationStatus === 'done'),
  })
  const route = params.toString()
  useEffect(() => setPassport(false), [route])
  const selection = params.get(tab === 'checks' ? 'remark' : 'result')
  const filters: ResultFilters = {
    resultSearch: params.get('resultSearch') ?? '', dataset: params.get('dataset') ?? '',
    metric: params.get('metric') ?? '', generator: params.get('generator') ?? '',
  }
  const sourceScope = `${tab}:${selection ?? ''}`
  const [preview, setPreview] = useState<{ scope: string; source: Source | null }>()
  const [previewKey, setPreviewKey] = useState(0)

  function show(source: Source | null, scope = sourceScope) {
    setPreview({ scope, source })
    setPreviewKey(key => key + 1)
  }
  function openRegion(id: string) {
    updateQuery({ run: id, remark: null })
    setPreview(undefined)
    setPreviewKey(key => key + 1)
  }

  function selectResult(row: Result | null, trigger?: HTMLButtonElement) {
    const source = row && data?.evidence.find(item => item.id === row.evidenceIds[0])
    const id = source && row ? row.id : null
    updateQuery({ result: id })
    show(source && data ? { ...source, url: data.pdf.url, trigger } : null, `${tab}:${id ?? ''}`)
  }
  function changeFilters(next: ResultFilters) {
    updateQuery(Object.fromEntries(Object.entries(next).map(([key, value]) => [key, value || null])))
    const selected = data?.results.find(item => item.id === selection)
    const changedSelect = next.dataset !== filters.dataset || next.metric !== filters.metric || next.generator !== filters.generator
    if (selected && (changedSelect || !resultMatches(selected, next))) selectResult(null)
  }
  function selectRemark(id: string | null, trigger?: HTMLButtonElement) {
    const record = data?.remarks.find(item => item.id === id)
    const source = record && data?.evidence.find(item => item.id === record.evidenceIds[0])
    updateQuery({ remark: id, reviewResult: null })
    show(source && data ? { ...source, url: data.pdf.url, trigger } : null, `${tab}:${id ?? ''}`)
  }

  if (articleQuery.error) return <ErrorNotice error={articleQuery.error} retry={() => void articleQuery.refetch()} />
  if (!article) return <p role="status">Загружаем статью…</p>
  const title = article.overview?.title?.text ?? article.document
  const links = { article: article.id, ...(runId ? { run: runId } : {}),
    ...Object.fromEntries(Object.entries(filters).filter(([, value]) => value)) }
  const initialRecord = tab === 'checks' ? data?.remarks.find(item => item.id === selection)
    : data?.results.find(item => item.id === selection && resultMatches(item, filters))
  const initialSource = initialRecord && data?.evidence.find(item => item.id === initialRecord.evidenceIds[0])
  const diagnostics = draftChecks ? runChecks(run, article, report.data) : undefined
  const runSource = draftChecks ? diagnostics?.checks.find(item => item.id === selection)?.source ?? diagnostics?.area : undefined
  const draftPdf = draftChecks ? runPdf(run.id, run.pdfSha256) : undefined
  const initial: Source | undefined = runSource && draftPdf ? { ...runSource, url: draftPdf }
    : initialSource && data ? { ...initialSource, url: data.pdf.url } : undefined
  const source: Source | null | undefined = preview?.scope === sourceScope ? preview.source : initial
  const regionSelection = tab === 'checks' && run && source ? {
    runId: run.id, pdfSha256: run.pdfSha256, candidateId: source.candidateId,
    initial: diagnostics?.area?.box && { page: diagnostics.area.page, box: diagnostics.area.box },
    onStarted: openRegion,
  } : undefined
  function closeSource() {
    if (tab === 'results') selectResult(null)
    else show(null)
    const related = tab === 'results' && selection ? document.querySelector<HTMLButtonElement>(`[data-result="${CSS.escape(selection)}"]`) : null
    const trigger = source?.trigger ?? related
    trigger?.focus({ preventScroll: true })
  }

  return <>
    <div className="heading article-heading"><div>
      {version && <div className="eyebrow"><StatusBadge label={version.coverage.partial ? 'Частичная публикация' : 'Результаты опубликованы'}
        kind={version.coverage.partial ? 'partial' : 'done'} /></div>}
      <h1>{title}</h1><p>{article.document} · {article.page_count} стр.</p></div>
      {version && <button onClick={() => setPassport(true)}>Паспорт и выгрузка</button>}
    </div>
    <div className="workspace variant-B">
      <div className="article-sidebar"><div className="article-return"><a className="link" href={labUrl()}>Библиотека</a></div>
        <nav className="article-nav" aria-label="Статья">
          {tabs.filter(([id]) => id !== 'run' || runId).map(([id, label]) =>
            <a key={id} className={`link ${id === tab ? 'action' : ''}`} aria-current={id === tab ? 'page' : undefined}
              href={labUrl({ ...links, ...(params.has('run') || !latestResultRun || !['results', 'compare', 'checks'].includes(id)
                ? {} : { run: latestResultRun }), tab: id })}>{label}</a>)}
        </nav>
      </div>
      <div className={`split ${source ? 'with-source' : ''}`}>
        <div>
          {tab === 'overview' ? <Overview article={article} runId={runId ?? null} preview={show} />
            : runQuery.error ? <ErrorNotice error={runQuery.error} retry={() => void runQuery.refetch()} />
            : runId && !run ? <p role="status">Загружаем прогон…</p>
            : tab === 'run' && run ? <Progress article={article} run={run} preview={show} />
            : draftChecks && diagnostics ? <>
              {report.error && <ErrorNotice error={report.error} retry={() => void report.refetch()} />}
              <RunChecks key={run.id} article={article} run={run} {...diagnostics} confirmed={report.data?.summary?.confirmed.length ?? 0}
                selectedId={selection} loading={report.fetchStatus === 'fetching'}
                select={(id, location, trigger) => {
                  updateQuery({ remark: id })
                  show(location && draftPdf ? { ...location, url: draftPdf, trigger } : null)
                }}
                preview={(location, trigger) => { if (draftPdf) show({ ...location, url: draftPdf, trigger }) }}
                open={openRegion} refresh={runQuery.refetch} />
            </> : !version ? <section className="card"><h2>Публикация</h2><p>У этого прогона пока нет опубликованного выпуска.</p>
              {run && <a className="link" href={labUrl({ ...links, tab: 'run' })}>Открыть прогон</a>}</section>
            : publication.error ? <ErrorNotice error={publication.error} retry={() => void publication.refetch()} />
            : !data ? <p role="status">Загружаем выпуск…</p>
            : tab === 'results' ? <Results data={data} version={version} selected={selection} select={selectResult}
              filters={filters} changeFilters={changeFilters} preview={show}
              comparisonLink={resultId => labUrl({ ...links, tab: 'compare', compareA: resultId })}
              checksLink={(remark, reviewResult) => labUrl({ ...links, tab: 'checks', remark, reviewResult })} />
            : tab === 'compare' ? <Comparisons publication={data} version={version} preview={show} />
            : <Checks data={data} articleId={article.id} runId={run!.id} selected={selection} select={selectRemark} preview={show} />}
        </div>
        {source && <Suspense fallback={<aside className="pdf"><p role="status">Открываем PDF…</p></aside>}>
          <SourcePanel key={previewKey} source={source} pages={article.page_count} close={closeSource} region={regionSelection} />
        </Suspense>}
      </div>
    </div>
    {passport && version && run && <Passport version={version} title={title} resultLink={id => labUrl({ article: article.id, run: run.id, tab: 'results', result: id })}
      data={data} close={() => setPassport(false)} />}
  </>
}

createRoot(document.getElementById('app')!).render(<QueryClientProvider client={client}><App /></QueryClientProvider>)
