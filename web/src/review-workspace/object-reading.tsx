import { useQuery } from '@tanstack/react-query'
import type { ArticleRun } from '../shared/article'
import type { PlotRun } from '../shared/article-plot'
import { runSchema } from '../shared/saved-run'
import { json, runPath, runPdf } from './api'
import type { Source } from './pdf'
import { ErrorNotice } from './publication'
import { ExtractedPoints } from './dashboard-visuals'

type RecordSource = { page: number; box?: Source['box'] }
type ReadingRecord = { label: string; source: RecordSource }
type Preview = (source: Source) => void

function ReadingRecords({ records, url, preview }: { records: ReadingRecord[]; url: string; preview: Preview }) {
  return <ul className="stages">
    {records.map(({ label, source }, index) => <li key={index}>
      <button type="button" onClick={event => preview({ ...source, url, trigger: event.currentTarget })}>{label}</button>
    </li>)}
  </ul>
}

export function PlotReading({ plot, url, preview }: { plot: PlotRun; url: string; preview: Preview }) {
  const { figure, panel, series, x_axis, y_axis } = plot.settings
  return <section className="object-reading">
    <h3>Рисунок {figure} · панель {panel}</h3>
    <p>{series}</p>
    <ExtractedPoints plot={plot} select={(point, trigger) => preview({ ...point.source, url, trigger })} />
    {plot.points.length ? <ReadingRecords url={url} preview={preview} records={plot.points.map(point => ({
      label: `${y_axis} ≈ ${point.y} · ${x_axis} = ${point.x}`, source: point.source,
    }))} /> : <p>Значения для этой панели не извлечены.</p>}
    {plot.issues.length > 0 && <ReadingRecords url={url} preview={preview} records={plot.issues.map(issue => ({
      label: issue.message, source: issue.source,
    }))} />}
  </section>
}

export function TableReading({ run, stageId, preview }: { run: ArticleRun; stageId: string; preview: Preview }) {
  const query = useQuery({
    queryKey: ['table-reading', run.id, stageId],
    queryFn: async ({ signal }) => {
      const response = await json<{ run: unknown }>(runPath(run.id) + '/stages/' + encodeURIComponent(stageId), { signal })
      return runSchema.parse(response.run)
    },
  })
  const url = runPdf(run.id, run.pdfSha256)

  if (query.error) return <ErrorNotice error={query.error} retry={() => void query.refetch()} />
  if (!query.data) return <p role="status">Загружаем результаты таблицы…</p>

  return <section className="object-reading">
    <h3>Результаты таблицы</h3>
    {query.data.results.length ? <ReadingRecords url={url} preview={preview} records={query.data.results.map(result => ({
      label: [result.method, result.dataset, result.metric, result.raw_value].filter(Boolean).join(' · '), source: result.source,
    }))} /> : <p>Значения для этой таблицы не извлечены.</p>}
    {query.data.remarks.length > 0 && <details><summary>Замечания · {query.data.remarks.length}</summary>
      <ReadingRecords url={url} preview={preview} records={query.data.remarks.map(remark => ({
        label: remark.reason, source: remark.source,
      }))} />
    </details>}
  </section>
}
