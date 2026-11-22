import { lazy, Suspense, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { publishedPlotDetailSchema, publishedPlotsSchema } from '../shared/published-plot'
import { json } from './api'
import { PlotReading } from './object-reading'
import type { Source } from './pdf'
import { ErrorNotice } from './publication'

const SourcePanel = lazy(() => import('./pdf').then(module => ({ default: module.SourcePanel })))
const files = ['PASSPORT.md', 'check-report.json', 'run.json', 'publications.csv', 'configurations.csv',
  'datasets.csv', 'tasks.csv', 'experiments.csv', 'results.csv', 'issues.csv']

export function PublishedPlots({ version }: { version?: string }) {
  return version ? <PublishedPlot key={version} version={version} /> : <PlotLibrary />
}

function PlotLibrary() {
  const query = useQuery({ queryKey: ['published-plots'], queryFn: async ({ signal }) =>
    publishedPlotsSchema.parse(await json('/api/published-plots', { signal })) })
  return <>
    <h1>Опубликованные графики</h1>
    {query.error ? <ErrorNotice error={query.error} retry={() => void query.refetch()} />
      : !query.data ? <p role="status">Загружаем графики…</p>
        : query.data.versions.length ? <ul>{query.data.versions.map(item => <li key={item.version_id}>
          <a href={`/plots/${item.version_id}`}>{item.publication_id} · {item.coverage.figure} ({item.coverage.panel})</a>
        </li>)}</ul> : <p>Опубликованных графиков пока нет.</p>}
    {!!query.data?.unavailable.length && <p>Недоступных версий: {query.data.unavailable.length}.</p>}
  </>
}

function PublishedPlot({ version }: { version: string }) {
  const [source, setSource] = useState<Source | null>(null)
  const query = useQuery({ queryKey: ['published-plot', version], queryFn: async ({ signal }) =>
    publishedPlotDetailSchema.parse(await json('/api/published-plots/' + encodeURIComponent(version), { signal })) })
  if (query.error) return <ErrorNotice error={query.error} retry={() => void query.refetch()} />
  if (!query.data) return <p role="status">Загружаем график…</p>
  const { run, plot, check } = query.data
  return <>
    <div className="heading article-heading"><div><a href="/plots">Опубликованные графики</a>
      <h1>{run.publication_id} · {run.coverage.figure} ({run.coverage.panel})</h1></div></div>
    <div className={`split${source ? ' with-source' : ''}`}>
      <div className="card">
        <PlotReading plot={plot} url={run.pdf.url} preview={setSource} />
        <p>Ошибок проверки: {check.errors.length}. Предупреждений: {check.warnings.length}.</p>
        <details><summary>Файлы выпуска</summary><ul>{files.map(file =>
          <li key={file}><a href={`/published/${run.version_id}/${file}`} download>{file}</a></li>)}</ul></details>
      </div>
      {source && <Suspense fallback={<p role="status">Открываем PDF…</p>}>
        <SourcePanel key={JSON.stringify([source.page, source.box])} source={source} pages={source.page} close={() => setSource(null)} />
      </Suspense>}
    </div>
  </>
}
