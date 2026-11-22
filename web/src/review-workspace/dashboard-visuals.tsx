import type { CSSProperties, ReactNode } from 'react'
import type { ArticleRunAttempt } from '../shared/article'
import type { PlotRun } from '../shared/article-plot'
import './dashboard-visuals.css'

export type ChartItem = { label: string; count: number; tone?: 'blue' | 'ink' | 'amber' | 'red' | 'slate' }

const format = new Intl.NumberFormat('ru-RU')
const axisNumber = new Intl.NumberFormat('ru-RU', { maximumSignificantDigits: 4 })

export function MetricRing({ slices, children }: { slices: string; children: ReactNode }) {
  return <div className="metrics-ring" style={{ background: `conic-gradient(${slices})` }}><div>{children}</div></div>
}

export function Distribution({ label, items }: { label: string; items: ChartItem[] }) {
  const total = items.reduce((sum, item) => sum + item.count, 0)
  if (!total) return null
  return <figure className="metrics-viz distribution">
    <figcaption>{label}</figcaption>
    <div className="distribution-body">
      <div className="metrics-segments" aria-hidden="true">
        {items.filter(item => item.count > 0).map(item =>
          <span key={item.label} data-tone={item.tone} style={{ flexGrow: item.count }} />)}
      </div>
      <ul className="metrics-legend">{items.map(item =>
        <li key={item.label} data-tone={item.tone}><span aria-hidden="true" />{item.label} <b>{format.format(item.count)}</b></li>)}</ul>
    </div>
  </figure>
}

const numberOfSeconds = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 1 })

export function AttemptTimeline({ stages }: { stages: { label: string; attempts: ArticleRunAttempt[] }[] }) {
  const completed = stages.flatMap(({ label, attempts }) => attempts.flatMap(attempt =>
    attempt.finishedAt !== null && attempt.finishedAt >= attempt.startedAt && Number.isFinite(attempt.finishedAt)
      ? [{ label, ...attempt, end: attempt.finishedAt }] : [],
  )).sort((a, b) => a.startedAt - b.startedAt)
  if (!completed.length) return null
  const start = completed[0].startedAt
  const finish = completed.reduce((last, attempt) => Math.max(last, attempt.end), start)
  const elapsed = finish - start
  if (!elapsed || !Number.isFinite(elapsed)) return null
  const seconds = (millis: number) => `${numberOfSeconds.format(millis / 1000)} с`
  return <details className="attempt-timeline">
    <summary>Хронология попыток · {format.format(completed.length)}</summary>
    <figure className="metrics-viz metric-pair">
      <figcaption>Измеренные интервалы завершённых попыток</figcaption>
      <ol>{completed.map((attempt, index) =>
        <li className="metric-pair-row" key={index} data-tone={attempt.status === 'succeeded' ? 'blue' : 'amber'}>
          <span>{attempt.label}{attempt.number > 1 && ` · попытка ${attempt.number}`}</span>
          <b>{seconds(attempt.end - attempt.startedAt)}</b>
          <div className="metric-pair-line" aria-hidden="true"><span style={{
            left: `${(attempt.startedAt - start) / elapsed * 100}%`,
            width: `${(attempt.end - attempt.startedAt) / elapsed * 100}%`,
          }} /></div>
        </li>)}</ol>
      <div className="metrics-axis"><span>0 с</span><span>{seconds(elapsed)}</span></div>
    </figure>
  </details>
}

export function ExtractedPoints({ plot, select }: {
  plot: PlotRun; select: (point: PlotRun['points'][number], trigger: HTMLButtonElement) => void
}) {
  if (!plot.points.length) return null
  const initial = plot.points[0]
  const { xMin, xMax, yMin, yMax } = plot.points.reduce((bounds, point) => ({
    xMin: Math.min(bounds.xMin, point.x), xMax: Math.max(bounds.xMax, point.x),
    yMin: Math.min(bounds.yMin, point.y), yMax: Math.max(bounds.yMax, point.y),
  }), { xMin: initial.x, xMax: initial.x, yMin: initial.y, yMax: initial.y })
  const position = (value: number, min: number, max: number) => max === min ? 50 : (value - min) / (max - min) * 100
  if (![xMax - xMin, yMax - yMin].every(Number.isFinite)) return null
  const { x_axis, y_axis } = plot.settings
  return <figure className="metrics-viz extracted-points">
    <figcaption>Извлечённые точки · {y_axis} по {x_axis}</figcaption>
    <div className="points-layout">
      <div className="points-y" aria-hidden="true"><span>{axisNumber.format(yMax)}</span>
        {yMax !== yMin && <span>{axisNumber.format(yMin)}</span>}</div>
      <div className="points-grid"><div>
        {plot.points.map(point => {
          const label = `${x_axis} = ${point.x} · ${y_axis} ≈ ${point.y}`
          return <button type="button" key={point.index} title={label} aria-label={`Открыть источник точки: ${label}`}
            style={{ '--x': `${position(point.x, xMin, xMax)}%`, '--y': `${100 - position(point.y, yMin, yMax)}%` } as CSSProperties}
            onClick={event => select(point, event.currentTarget)} />
        })}
      </div></div>
      <div className="points-x metrics-axis" aria-hidden="true"><span>{axisNumber.format(xMin)}</span>
        {xMax !== xMin && <span>{axisNumber.format(xMax)}</span>}</div>
    </div>
  </figure>
}
