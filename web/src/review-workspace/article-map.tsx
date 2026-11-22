import { useEffect, useRef, useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import type { Article } from '../shared/article'
import type { PlotRun } from '../shared/article-plot'
import { articlePath, labUrl, readPlot, startTable } from './api'
import { navigateWorkspace } from './navigation'
import { PlotReading } from './object-reading'
import type { Source } from './pdf'

const groups = [['section', 'Разделы'], ['table', 'Таблицы'], ['figure', 'Рисунки']] as const
type Group = typeof groups[number][0]
type Item = Article['candidates'][number]

function readingAction(item: Item) {
  if (item.kind === 'table' && item.extractable) return 'Разобрать таблицу'
  if (item.kind === 'figure' && item.label && item.figureStatus === 'data') return 'Разобрать график'
}

export function ArticleMap({ article, preview }: { article: Article; preview: (source: Source) => void }) {
  const [kind, setKind] = useState<Group>(() => groups.find(([key]) => article.candidates.some(item => item.kind === key))?.[0] ?? 'section')
  const [selected, setSelected] = useState<string>()
  const [plot, setPlot] = useState<{ candidateId: string; result: PlotRun }>()
  const list = useRef<HTMLUListElement>(null)
  const scroll = useRef<Partial<Record<Group, number>>>({})
  const reading = useMutation({
    mutationFn: async (item: Item) => item.kind === 'table'
      ? { kind: 'table' as const, run: await startTable(article.id, item.id) }
      : { kind: 'figure' as const, result: await readPlot(article, item) },
  })
  useEffect(() => {
    if (list.current) list.current.scrollTop = scroll.current[kind] ?? 0
  }, [kind])

  const entries = article.candidates.filter(item => item.kind === kind)
  const pdf = articlePath(article.id) + '/source.pdf'
  const visiblePlot = plot && plot.candidateId === selected && entries.some(item => item.id === selected)

  function read(item: Item) {
    setSelected(item.id)
    setPlot(undefined)
    reading.mutate(item, { onSuccess: outcome => {
      if (outcome.kind === 'table') navigateWorkspace(labUrl({ article: article.id, run: outcome.run.id, tab: 'run' }))
      else setPlot({ candidateId: item.id, result: outcome.result })
    } })
  }

  return <details className="card article-map"><summary>Карта статьи</summary>
    {article.candidates.length ? <>
      <div className="map-groups" role="group" aria-label="Объекты карты">
        {groups.map(([key, label]) => {
          const count = article.candidates.filter(item => item.kind === key).length
          return <button key={key} type="button" disabled={!count} aria-pressed={kind === key}
            onClick={() => setKind(key)}>{label} <small>{count}</small></button>
        })}
      </div>
      <ul key={kind} className="map-items" ref={list} onScroll={event => { scroll.current[kind] = event.currentTarget.scrollTop }}>
        {entries.map(item => {
          const action = readingAction(item)
          const active = reading.variables?.id === item.id
          return <li key={item.id} className="map-item">
            <button type="button" aria-pressed={selected === item.id} onClick={event => {
              setSelected(item.id)
              preview({ ...item, url: pdf, trigger: event.currentTarget })
            }}>
              <span>{item.text || item.label || item.kind}</span><small>стр. {item.page}</small>
            </button>
            {action && <button type="button" className="quiet map-action" disabled={reading.isPending}
              onClick={() => read(item)}>{active && reading.isPending ? 'Разбираем…' : action}</button>}
            {active && reading.error && <p role="alert">{reading.error.message}</p>}
          </li>
        })}
      </ul>
      {visiblePlot && <PlotReading plot={plot.result} url={pdf} preview={preview} />}
    </> : <p>На карте пока нет разделов, таблиц или рисунков.</p>}
  </details>
}
