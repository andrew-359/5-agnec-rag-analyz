import { lazy, Suspense, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import type { Dataset } from '../shared/schema'
import { json, labUrl } from './api'
import { Comparisons, ErrorNotice, Passport, Results, type ResultFilters } from './publication'
import type { Source } from './pdf'

const SourcePanel = lazy(() => import('./pdf').then(module => ({ default: module.SourcePanel })))

const publicationPath = /^\/publications\/([\w-]+)(?:\/versions\/([\w-]+))?(?:\/evidence\/([\w-]+))?\/?$/

type PublishedAddress = {
  publicationId: string
  versionId?: string
  evidenceId?: string
}

async function loadPublication(address: PublishedAddress, params: URLSearchParams, signal: AbortSignal) {
  const { publicationId, versionId, evidenceId } = address
  let pinnedVersion = versionId
  if (evidenceId) {
    const input = { publicationId, versionId, evidenceId, resultId: params.get('resultId') || undefined,
      remarkId: params.get('remarkId') || undefined }
    const found = await json<{ result: { data: { run: { id: string } } } }>(
      '/trpc/evidence.get?' + new URLSearchParams({ input: JSON.stringify(input) }), { signal })
    if (found.result.data.run.id.startsWith('version-')) pinnedVersion = found.result.data.run.id
  }
  const input = JSON.stringify({ publicationId, versionId: pinnedVersion })
  const response = await json<{ result: { data: Dataset } }>(
    '/trpc/publications.review?' + new URLSearchParams({ input }), { signal })
  return response.result.data
}

export function PublishedWorkspace({ address }: { address: URL }) {
  const matched = publicationPath.exec(address.pathname)
  if (!matched) return <p role="alert">Ссылка на выпуск некорректна.</p>
  const [, publicationId, versionId, evidenceId] = matched
  const params = address.searchParams
  return <PublishedRelease key={address.pathname + address.search}
    route={{ publicationId, versionId, evidenceId }} params={params} />
}

function PublishedRelease({ route, params }: { route: PublishedAddress; params: URLSearchParams }) {
  const query = useQuery({
    queryKey: ['published-release', route, params.toString()],
    queryFn: ({ signal }) => loadPublication(route, params, signal),
  })
  const [selected, select] = useState(params.get('resultId'))
  const [source, preview] = useState<Source | null>()
  const [passport, setPassport] = useState(false)
  const [filters, setFilters] = useState<ResultFilters>({
    resultSearch: '', dataset: params.get('dataset') ?? '', metric: params.get('metric') ?? '', generator: params.get('generator') ?? '',
  })
  if (query.error) return <ErrorNotice error={query.error} retry={() => void query.refetch()} />
  const data = query.data
  if (!data) return <p role="status">Загружаем выпуск…</p>
  const { version } = data
  const resultSource = data.results.find(item => item.id === selected)?.evidenceIds[0]
  const initial = data.evidence.find(item => item.id === (route.evidenceId ?? resultSource))
  const shown = source === undefined ? initial && { ...initial, url: data.pdf.url } : source
  const path = version?.url ?? '/publications/' + data.publication.id
  const href = (query: Record<string, string>) => path + '?' + new URLSearchParams(query)
  const tab = params.get('tab')
  const remarkId = params.get('remark') ?? params.get('remarkId')
  const remark = data.remarks.find(item => item.id === remarkId)
  return <>
    <div className="heading article-heading">
      <div><a href={labUrl()}>Библиотека</a><h1>{data.publication.title}</h1>
        {tab && <a href={path}>Результаты выпуска</a>}</div>
      {version && <button onClick={() => setPassport(true)}>Паспорт и выгрузка</button>}
    </div>
    <div className={`split${shown ? ' with-source' : ''}`}>
      {tab === 'compare' && version ? <Comparisons publication={data} version={version} preview={preview} />
        : (tab === 'checks' || params.has('remarkId')) && remark ? <section className="card">
          <h2>Замечание</h2><p>{remark.reason}</p><p>{remark.rawText}</p>
          {data.evidence.filter(item => remark.evidenceIds.includes(item.id)).map(item => <button key={item.id}
            onClick={() => preview({ ...item, url: data.pdf.url })}>Источник · стр. {item.page}</button>)}
        </section> : <Results data={data} version={version} selected={selected} filters={filters} changeFilters={setFilters}
          comparisonLink={version ? id => href({ tab: 'compare', compareA: id }) : undefined}
          checksLink={(id, result) => href({ tab: 'checks', remark: id, resultId: result })}
          preview={preview} select={(result, trigger) => {
            select(result.id)
            const evidence = data.evidence.find(item => item.id === result.evidenceIds[0])
            preview(evidence ? { ...evidence, url: data.pdf.url, trigger } : null)
          }} />}
      {shown && <Suspense fallback={<p role="status">Открываем PDF…</p>}>
        <SourcePanel key={JSON.stringify([shown.page, shown.box])} source={shown} pages={shown.page} close={() => preview(null)} />
      </Suspense>}
    </div>
    {passport && version && <Passport version={version} title={data.publication.title} data={data}
      resultLink={id => href({ resultId: id })} close={() => setPassport(false)} />}
  </>
}
