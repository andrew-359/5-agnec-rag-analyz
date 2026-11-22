import { useLayoutEffect, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import type { Dataset, Evidence, Result } from '../shared/schema'
import { resultConditions } from '../shared/review-context'
import type { PublishedVersion } from '../shared/publication'
import { resultRefSchema, type ComparisonCandidate, type ComparisonRecommendation, type ComparisonResponse, type ResultRef } from '../shared/comparison'
import { labUrl, post, publicationFile, updateQuery } from './api'
import type { Source } from './pdf'
import { ReviewDrafts } from './review-drafts'
import { PassportMetrics } from './passport-metrics'

export type Preview = (source: Source | null) => void

export function ErrorNotice({ error, retry }: { error: Error; retry: () => void }) {
  return <div className="notice" role="alert"><p>{error.message}</p><button onClick={retry}>Повторить</button></div>
}

export type ResultFilters = { resultSearch: string; dataset: string; metric: string; generator: string }

const generatorKey = (row: Result) => row.generator || '__unknown__'
const filterFields = [['dataset', 'Датасет'], ['metric', 'Метрика'], ['generator', 'Генератор']] as const
const columns = [
  ['method', 'Метод'], ['generator', 'Генератор'], ['dataset', 'Датасет'], ['metric', 'Метрика'],
  ['value', 'Значение'], ['source', 'Источник'],
] as const

export function resultMatches(row: Result, { resultSearch, dataset, metric, generator }: ResultFilters) {
  const fields = [row.method, row.generator, row.dataset, row.metric, row.rawValue].join(' ').toLowerCase()
  return (!dataset || row.dataset === dataset) && (!metric || row.metric === metric) &&
    (!generator || generatorKey(row) === generator) &&
    resultSearch.toLowerCase().trim().split(/\s+/).every(term => fields.includes(term))
}

function sourceTitle(source: Evidence) {
  return source.tableDesignation || (source.table ? `Таблица ${source.table}`
    : source.figure ? `Рисунок ${source.figure}` : source.precision === 'text_fragment' ? 'Текст' : 'PDF')
}

export function Results({ data, version, selected, select, filters, changeFilters, comparisonLink, checksLink, preview }: {
  data: Dataset; version?: PublishedVersion; selected: string | null; select: (result: Result, button: HTMLButtonElement) => void
  filters: ResultFilters; changeFilters: (filters: ResultFilters) => void; comparisonLink?: (resultId: string) => string
  checksLink: (remarkId: string, resultId: string) => string; preview: Preview
}) {
  const evidence = new Map(data.evidence.map(item => [item.id, item]))
  const visible = data.results.filter(row => resultMatches(row, filters))
  const current = visible.find(row => row.id === selected)
  const remarks = current ? data.remarks.filter(remark => remark.resultIds?.includes(current.id)) : []
  return <section className="card results">
    <h2>Результаты</h2>{version && <p>{version.coverage.summary}</p>}
    <div className="query">
      <label className="search-field">Поиск<input type="search" value={filters.resultSearch}
        placeholder="Метод, датасет, метрика, число…"
        onChange={event => changeFilters({ ...filters, resultSearch: event.target.value })} /></label>
      {filterFields.map(([key, label]) => <label key={key} className={key === 'generator' ? 'generator-field' : undefined}>{label}
        <select value={filters[key]} onChange={event => changeFilters({ ...filters, [key]: event.target.value })}>
          <option value="">{key === 'generator' ? 'Все генераторы' : 'Все'}</option>
          {[...new Set(data.results.map(row => key === 'generator' ? generatorKey(row) : row[key]))].filter(Boolean)
            .map(value => <option key={value} value={value}>{value === '__unknown__' ? 'Не установлен' : value}</option>)}
        </select>
      </label>)}
    </div>
    {current && <div className="result-actions">
      <div className="actions">{comparisonLink && <a className="link" href={comparisonLink(current.id)}>Подобрать сравнение для {current.rawValue}</a>}
        {remarks.length === 1 && <a className="link quiet" href={checksLink(remarks[0].id, current.id)}>Связанное замечание</a>}
      </div>
      <details className="review-conditions" key={current.id}><summary>Значение и условия</summary>
        <p>Нормализовано · {current.value ?? 'Не установлено'} {current.unit ?? ''}</p>
        <ConditionFacts result={current} evidence={evidence} pdf={data.pdf.url} preview={preview} />
        {remarks.length > 1 && <ul>{remarks.map(remark => <li key={remark.id}>
          <a href={checksLink(remark.id, current.id)}>{remark.reason}</a></li>)}</ul>}
      </details>
    </div>}
    <p className="results-count">Показано {visible.length} из {data.results.length}</p>
    <div className="scroll-table"><table>
      <colgroup>{columns.map(([key]) => <col key={key} className={key + '-column'} />)}</colgroup>
      <thead><tr>{columns.map(([key, label]) => <th key={key} scope="col" className={key === 'value' ? 'numeric value-column' : key + '-column'}>{label}</th>)}</tr></thead>
      <tbody>{visible.length ? visible.map(row => {
        const source = evidence.get(row.evidenceIds[0])
        return <tr key={row.id} className={selected === row.id ? 'current' : ''}>
          <td><strong>{row.method}</strong>
            {row.partial === true && <span className="badge badge-partial result-partial-badge">Открытые поля</span>}
            <small className="compact-result-context">{row.dataset} · {row.metric}</small></td>
          <td className="generator-column">{row.generator || 'Не установлен'}</td>
          <td className="dataset-column">{row.dataset}</td><td className="metric-column">{row.metric}</td>
          <td className="numeric"><button className="value" data-result={row.id}
            onClick={event => select(row, event.currentTarget)}>{row.rawValue}</button></td>
          <td className="source-column">{source ? <>{sourceTitle(source)}<small>стр. {source.page}</small></> : '—'}</td>
        </tr>
      }) : <tr><td colSpan={columns.length}>Нет результатов по этим фильтрам.</td></tr>}</tbody>
    </table></div>
  </section>
}

function ConditionFacts({ result, evidence, pdf, preview }: {
  result: Result; evidence: Map<string, Evidence>; pdf: string; preview: Preview
}) {
  return <dl>{resultConditions(result).map(field => <div key={field.key}><dt>{field.label}</dt>
    <dd>{field.value === null || field.value === '' ? 'Не установлено' : field.value}</dd>
    {field.evidenceIds.map(id => {
      const source = evidence.get(id)
      return source && <button key={id} className="quiet" onClick={event =>
        preview({ ...source, url: pdf, trigger: event.currentTarget })}>Источник · стр. {source.page}</button>
    })}
  </div>)}</dl>
}

export function Checks({ data, articleId, runId, selected, select, preview }: {
  data: Dataset; articleId: string; runId: string; selected: string | null
  select: (id: string | null, trigger?: HTMLButtonElement) => void; preview: Preview
}) {
  const [editing, setEditing] = useState(false)
  const resultId = new URLSearchParams(location.search).get('reviewResult')
  const list = useRef<HTMLDivElement>(null)
  const scroll = useRef(0)
  const returnTo = useRef<number | null>(null)
  const index = data.remarks.findIndex(item => item.id === selected)
  const current = data.remarks[index]
  const linked = data.results.filter(row => current?.resultIds?.includes(row.id))
  const result = linked.find(row => row.id === resultId) ?? (linked.length === 1 ? linked[0] : undefined)
  const evidence = current?.evidenceIds.map(id => data.evidence.find(item => item.id === id)).filter((item): item is Evidence => !!item)
  const canNavigate = () => !editing || window.confirm('Отменить несохранённое исправление?')
  useLayoutEffect(() => {
    if (!current && list.current) {
      list.current.scrollTop = scroll.current
      if (returnTo.current !== null) {
        list.current.querySelector<HTMLButtonElement>(`[data-remark="${returnTo.current}"]`)?.focus({ preventScroll: true })
        returnTo.current = null
      }
    }
  }, [current])
  return <section className="card checks">
    <h2>Проверки</h2>
    {!current && <div className="issue-list lab-issues" ref={list} onScroll={event => { scroll.current = event.currentTarget.scrollTop }}>
      {data.remarks.length ? data.remarks.map((item, i) =>
        <button key={item.id} data-remark={i} onClick={event => select(item.id, event.currentTarget)}>{item.reason}</button>)
        : <p>Замечаний нет.</p>}
    </div>}
    {current && <article className="review">
      <div className="review-navigation">
        <button type="button" onClick={() => {
          if (!canNavigate()) return
          returnTo.current = index
          select(null)
        }}>← К замечаниям</button>
        <small>{index + 1} из {data.remarks.length}</small>
        <button type="button" aria-label="Предыдущее замечание" disabled={index === 0}
          onClick={event => { if (canNavigate()) select(data.remarks[index - 1].id, event.currentTarget) }}>‹</button>
        <button type="button" aria-label="Следующее замечание" disabled={index + 1 === data.remarks.length}
          onClick={event => { if (canNavigate()) select(data.remarks[index + 1].id, event.currentTarget) }}>›</button>
      </div>
      <h3>{current.reason}</h3>{current.rawText && <p>{current.rawText}</p>}
      <div className="actions">
        {evidence?.map((item, i) => <button key={item.id} onClick={event =>
          preview({ ...item, url: data.pdf.url, trigger: event.currentTarget })}>Источник {i + 1} · стр. {item.page}</button>)}
        {result && <a className="link" href={labUrl({ article: articleId, run: runId, tab: 'results', result: result.id })}>
          {result.method} · {result.rawValue}</a>}
      </div>
      {linked.length > 1 && <label>Связанный результат<select value={result?.id ?? ''} onChange={event => {
        if (!canNavigate()) return
        const next = event.target.value
        updateQuery({ reviewResult: next || null })
        const row = linked.find(item => item.id === next)
        const source = row && data.evidence.find(item => item.id === row.evidenceIds[0])
        if (source) preview({ ...source, url: data.pdf.url, trigger: event.currentTarget })
      }}>
        <option value="">Выберите результат</option>
        {linked.map(row => <option key={row.id} value={row.id}>{row.method} · {row.dataset} · {row.rawValue}</option>)}
      </select></label>}
      {!evidence?.length && <p>У замечания нет доступного фрагмента.</p>}
      {!linked.length && <small>Замечание пока не связано с опубликованной записью.</small>}
      {data.version && <ReviewDrafts key={current.id + '/' + (result?.id ?? '')}
        data={data} remark={current} result={result} preview={preview} onBusy={setEditing} />}
      {result && <details className="review-conditions"><summary>Условия записи</summary>
        <ConditionFacts result={result} evidence={new Map(data.evidence.map(source => [source.id, source]))}
          pdf={data.pdf.url} preview={preview} />
      </details>}
    </article>}
  </section>
}

const verdicts = { comparable: 'Сопоставимость подтверждена', insufficient: 'Недостаточно оснований', different: 'Условия различаются' }
const labels = { same: 'Совпадает', different: 'Различается', unknown: 'Не установлено',
  reviewed: 'Сверено', documented: 'Подтверждено источником', equivalent: 'Эквивалентно' }

export function Comparisons({ publication, version, preview }: { publication: Dataset; version: PublishedVersion; preview: Preview }) {
  const { publicationId, versionId } = version
  const ref = { publicationId, versionId }
  const params = new URLSearchParams(location.search)
  const [a, setA] = useState<ResultRef | undefined>(() => comparisonRef(params.get('compareA'), ref))
  const [b, setB] = useState<ResultRef | undefined>(() => comparisonRef(params.get('compareB'), ref))
  const [scope, setScope] = useState<'article' | 'corpus'>('article')
  const [picking, setPicking] = useState(!params.has('compareB'))
  const pair = a && b ? { a, b } : null
  const suggestions = useQuery({
    queryKey: ['recommendations', ref],
    queryFn: () => post<{ recommendations: ComparisonRecommendation[] }>('/api/comparisons/recommendations', ref),
  })
  const comparison = useQuery({
    queryKey: ['comparison', pair], enabled: !!pair,
    queryFn: () => post<ComparisonResponse>('/api/comparisons', pair!),
  })
  const candidates = useQuery({
    queryKey: ['comparison-candidates', a, scope], enabled: !!a && picking,
    queryFn: () => post<{ candidates: ComparisonCandidate[] }>('/api/comparisons/candidates', { ...a, scope }),
  })
  function choosePair(left: ResultRef, right: ResultRef) {
    preview(null)
    setA(left)
    setB(right)
    setPicking(false)
    updateQuery({ compareA: left.resultId, compareB: right.versionId === versionId && right.publicationId === publicationId
      ? right.resultId : JSON.stringify(right) })
  }
  function suggestion({ a, b, methods, dataset, metric }: ComparisonRecommendation) {
    return <div className="recommended-pair" key={a.resultId + '/' + b.resultId}>
      <div><strong>{methods.join(' и ')}</strong><small>{dataset} · {metric}</small></div>
      <button onClick={() => choosePair(a, b)}>Сравнить</button>
    </div>
  }
  const list = suggestions.data?.recommendations ?? []
  const data = comparison.data
  const proof = data?.proof
  const proofSide = proof && [data.a, data.b].find(side => side.ref.versionId === proof.source.versionId &&
    side.ref.publicationId === proof.source.publicationId && side.evidence.some(item => item.id === proof.source.evidenceId))
  const proofSource = proofSide?.evidence.find(item => item.id === proof?.source.evidenceId)
  const scopeSource = proofSide?.evidence.find(item => item.id === proof?.scopeEvidenceId)
  const base = publication.results.find(row => row.id === a?.resultId)
  return <>
    {pair && comparison.isPending && <p role="status">Проверяем условия…</p>}
    {comparison.error && <ErrorNotice error={comparison.error} retry={() => void comparison.refetch()} />}
    {data && <>
      <section className={`comparison-verdict ${data.status}`}><h2>{verdicts[data.status]}</h2>
        {data.status === 'comparable' && data.delta !== undefined &&
          <p className="comparison-delta">Абсолютная разница <strong>{data.delta}</strong> · {data.unit === 'fraction' ? 'доля' : data.unit}</p>}
        {(data.reasons[0] || proof?.note) && <p>{data.reasons[0] ?? proof?.note}</p>}
        {proofSource && proofSide && <button onClick={event =>
          preview({ ...proofSource, url: proofSide.pdfUrl, trigger: event.currentTarget })}>Протокол в PDF</button>}
        {scopeSource && scopeSource.id !== proofSource?.id && proofSide && <button onClick={event =>
          preview({ ...scopeSource, url: proofSide.pdfUrl, trigger: event.currentTarget })}>Основание сопоставимости в PDF</button>}
      </section>
      <div className="comparison">{[data.a, data.b].map((side, index) => {
        const source = side.evidence.find(item => item.id === side.result.evidenceIds[0])
        return <section className="card comparison-item" key={index}><h2>{side.result.method}</h2>
          <p className="big-value">{side.result.rawValue}</p><small>{side.result.dataset} · {side.result.metric}</small>
          {side.result.generator && <small>{side.result.generator}</small>}
          {data.a.ref.publicationId !== data.b.ref.publicationId && <small>{side.title}</small>}
          {source && <button onClick={event => preview({ ...source, url: side.pdfUrl, trigger: event.currentTarget })}>
            Источник {side.result.method} · стр. {source.page}</button>}
          {index === 1 && base && !picking && <button onClick={() => setPicking(true)}>Заменить вторую запись</button>}
        </section>
      })}</div>
      <details className="card comparison-conditions"><summary>Условия сравнения</summary>
        {data.reasons.slice(1).map((reason, index) => <p key={index}>{reason}</p>)}
        <div className="scroll-table"><table><thead><tr><th>Условие</th><th>A</th><th>B</th><th>Проверка</th></tr></thead>
          <tbody>{data.checks.map((row, index) => <tr key={index}><th>{row.field}</th><td>{row.a ?? 'Не извлечено'}</td>
            <td>{row.b ?? 'Не извлечено'}</td><td>{labels[row.state]}</td></tr>)}</tbody></table></div>
      </details>
    </>}
    <section className="card comparison-suggestions"><h2>Предложения для сравнения</h2>
      {suggestions.error ? <ErrorNotice error={suggestions.error} retry={() => void suggestions.refetch()} />
        : suggestions.isPending ? <p role="status">Подбираем пары…</p>
        : !list.length ? <p>Для этого выпуска нет подтверждённых пар.</p>
        : <div className="comparison-recommendations">{suggestion(list[0])}{list.length > 1 &&
          <details><summary>Другие пары · {list.length - 1}</summary>{list.slice(1).map(suggestion)}</details>}</div>}
    </section>
    {!a && <p className="comparison-help">Чтобы проверить конкретное число, откройте его в «Результатах» и нажмите «Подобрать сравнение».</p>}
    {a && base && (!data || picking) && <section className="card comparison-picker">
      <h2>Подбор второй записи</h2>
      <p><strong>{base.method} · {base.rawValue}</strong><small>{base.dataset} · {base.metric}</small></p>
      {!picking ? <button onClick={() => setPicking(true)}>Заменить вторую запись</button> : <>
        <div className="switch" aria-label="Где искать сравнение">
          <button className={scope === 'article' ? 'chosen' : ''} aria-pressed={scope === 'article'}
            onClick={() => setScope('article')}>В этой статье</button>
          <button className={scope === 'corpus' ? 'chosen' : ''} aria-pressed={scope === 'corpus'}
            onClick={() => setScope('corpus')}>В других статьях</button>
        </div>
        {candidates.error ? <ErrorNotice error={candidates.error} retry={() => void candidates.refetch()} />
          : candidates.isPending ? <p role="status">Подбираем записи…</p>
          : !candidates.data?.candidates.length ? <p>Подходящих кандидатов с тем же датасетом и метрикой нет.</p>
          : <ul className="comparison-candidates">{candidates.data.candidates.map(item =>
            <li className="recommended-pair" key={`${item.ref.versionId}/${item.ref.resultId}`}>
              <div><strong>{item.method} · {item.rawValue}</strong>
                <small>{[item.generator, item.title, verdicts[item.status]].filter(Boolean).join(' · ')}</small></div>
              <button onClick={() => choosePair(a, item.ref)}>Проверить пару</button>
            </li>)}</ul>}
      </>}
    </section>}
  </>
}

function comparisonRef(value: string | null, ref: Pick<ResultRef, 'publicationId' | 'versionId'>) {
  if (!value) return
  try {
    const result = resultRefSchema.safeParse(value.startsWith('{') ? JSON.parse(value) : { ...ref, resultId: value })
    return result.success ? result.data : undefined
  } catch {
    return undefined
  }
}

export function Passport({ version, title, resultLink, data, close }: {
  version: PublishedVersion; title: string; resultLink: (id: string) => string; data?: Dataset; close: () => void
}) {
  const dialog = useRef<HTMLDialogElement>(null)
  useLayoutEffect(() => { dialog.current?.showModal() }, [])
  const query = useQuery({
    queryKey: ['passport', version.versionId],
    queryFn: async ({ signal }) => {
      const response = await fetch(publicationFile(version, 'PASSPORT.md'), { signal })
      if (!response.ok) throw new Error('Не удалось загрузить паспорт.')
      const markdown = await response.text()
      if (!markdown.includes(version.versionId)) throw new Error('Паспорт другой версии.')
      return markdown
    },
  })
  const sections = query.data?.split(/^## /m).slice(1) ?? []
  const example = sections.find(section => section.startsWith('Контрольная запись\n'))?.match(/[?&]resultId=([\w-]+)/)?.[1]
  return <dialog id="dialog" ref={dialog} aria-labelledby="passport-title" onClose={close}>
    <header className="passport-header"><h2 id="passport-title">Паспорт данных</h2>
      <button className="passport-close" aria-label="Закрыть паспорт" autoFocus onClick={() => dialog.current?.close()}>×</button>
    </header>
    <div className="passport-body">
      {query.error ? <ErrorNotice error={query.error} retry={() => void query.refetch()} />
        : query.isPending ? <p role="status">Загружаем паспорт…</p> : <>
          <section className="passport-hero"><h3>{title}</h3><p>{version.coverage.summary}</p>
            {data && <dl className="passport-facts"><div><dt>Результаты</dt><dd>{data.results.length}</dd></div>
              <div><dt>Замечания</dt><dd>{data.remarks.length}</dd></div></dl>}
            <div className="passport-meta"><span className={`passport-coverage ${version.coverage.partial ? 'is-partial' : ''}`}>
              {version.coverage.partial ? 'Частичный охват' : 'Выпуск готов'}</span><details className="passport-version-id"><summary>Версия {version.versionId.replace(/^version-/, '').slice(0, 7)}</summary>
                <code>{version.versionId}</code>
              </details></div>
          </section>
          {example && <p className="passport-contents"><a className="passport-source"
            href={resultLink(example)}>Открыть контрольную ячейку в PDF</a></p>}
          <article className="passport-document">{sections.map(section => {
            const [header, ...lines] = section.split('\n')
            return <details key={header}><summary>{header}</summary><div className="passport-version">
              {lines.join('\n').trim().replace(/\[([^\]]+)\]\([^)]*\)/g, '$1').replace(/[`*]/g, '').split(/\n\n+/)
                .map((paragraph, index) => <p key={index}>{paragraph}</p>)}
            </div></details>
          })}</article>
          <PassportMetrics version={version} data={data} resultLink={resultLink} />
        </>}
    </div>
    {query.data && <footer className="passport-footer"><a className="link quiet" href={publicationFile(version, 'report.json')} download>Отчёт проверки (JSON)</a>
      <a className="passport-download link" href={publicationFile(version, 'export.zip')} download>Скачать ZIP · 7 CSV</a></footer>}
  </dialog>
}
