import { useQuery } from '@tanstack/react-query'
import { z } from 'zod'
import type { PublishedVersion } from '../shared/publication'
import type { Dataset, Evidence } from '../shared/schema'
import { json, publicationFile } from './api'
import { MetricRing } from './dashboard-visuals'
import { passportAudits } from './passport-audits'
import './passport-metrics.css'

const count = z.number().int().nonnegative()
const coverageDetails = z.object({
  readTables: count.optional(),
  plannedStages: count.optional(),
  region: z.object({ page: z.number().int().positive() }).optional(),
})
const checkerReport = z.object({
  errors: z.array(z.unknown()),
  warnings: z.array(z.unknown()),
  quotes: z.object({ requested: count, matched: count }).optional(),
  source_text_mode: z.string().optional(),
})

const number = new Intl.NumberFormat('ru-RU')
const percentage = new Intl.NumberFormat('ru-RU', { style: 'percent' })
const sourceLabels: Record<Evidence['precision'], string> = {
  cell: 'Ячейка', estimated_cell: 'Приблизительная ячейка', plot_point: 'Точка графика',
  text_fragment: 'Фрагмент текста', table: 'Область таблицы', figure: 'Область рисунка', page: 'Только страница',
}

function PublicationSources({ data }: { data?: Dataset }) {
  if (!data) return null
  const sources = new Map(data.evidence.map(source => [source.id, source]))
  const breakdown = new Map<string, number>()
  let located = 0
  for (const result of data.results) {
    const source = sources.get(result.evidenceIds[0])
    if (source) located++
    const label = source ? sourceLabels[source.box ? source.precision : 'page'] : 'Без источника'
    breakdown.set(label, (breakdown.get(label) ?? 0) + 1)
  }
  return <article className="passport-metric passport-source-quality">
    <h4>Источники результатов</h4>
    <p><b>{number.format(located)} из {number.format(data.results.length)}</b> записей имеют адрес в PDF.</p>
    <ul className="passport-quality-sources">{Array.from(breakdown, ([label, count]) =>
      <li key={label}>{label} <b>{number.format(count)}</b></li>)}</ul>
  </article>
}

export function PassportMetrics({ version, data, resultLink }: {
  version: PublishedVersion; data?: Dataset; resultLink: (id: string) => string
}) {
  const details = coverageDetails.safeParse(version.coverage).data
  const { readTables, plannedStages, region } = details ?? {}
  const ratio = readTables !== undefined && plannedStages !== undefined && plannedStages > 0 && readTables <= plannedStages
    ? readTables / plannedStages : undefined

  const checker = useQuery({
    queryKey: ['passport-checker', version.publicationId, version.versionId],
    queryFn: async ({ signal }) => checkerReport.parse(await json(publicationFile(version, 'report.json'), { signal })),
    staleTime: Infinity,
  })
  const report = checker.data
  const quotes = report?.quotes
  const nativeOnly = report?.source_text_mode === 'native PDF text only'
  const audits = passportAudits.filter(audit => audit.versionId === version.versionId &&
    audit.publicationId === version.publicationId && audit.pdfSha256 === version.pdfSha256)

  return <section className="passport-metrics" aria-labelledby="passport-metrics-title">
    <h3 id="passport-metrics-title">Качество извлечения</h3>
    <div className="passport-metrics-grid">
      <article className="passport-metric">
        <h4>Охват чтения</h4>
        {ratio !== undefined ? <div className="passport-metric-mix">
          <MetricRing slices={`var(--chart-blue) ${ratio}turn, var(--chart-track) ${ratio}turn`}>
            <strong>{percentage.format(ratio)}</strong>
          </MetricRing>
          <p><b>{number.format(readTables!)} из {number.format(plannedStages!)}</b> таблиц по плану прочитано</p>
        </div> : region ? <p>Точечный разбор · стр. {number.format(region.page)}</p>
          : <p>{plannedStages === 0 ? 'Таблицы не входили в план.' : 'Для этой версии нет счётчиков чтения.'}</p>}
      </article>
      <PublicationSources data={data} />
      <article className="passport-metric">
        <h4>Проверка выгрузки</h4>
        {report ? <>
          <dl className="passport-metric-checker">
            <div data-attention={report.errors.length > 0}><dt>Ошибки</dt><dd>{number.format(report.errors.length)}</dd></div>
            <div data-attention={report.warnings.length > 0}><dt>Предупреждения</dt><dd>{number.format(report.warnings.length)}</dd></div>
          </dl>
          {!!quotes?.requested && quotes.matched <= quotes.requested &&
            <p>Найдено цитат · <b>{number.format(quotes.matched)} из {number.format(quotes.requested)}</b></p>}
          {nativeOnly && <p>Поиск цитат только в текстовом слое PDF.</p>}
        </> : <p role="status">{checker.isPending ? 'Читаем отчёт…' : 'Данные чекера недоступны.'}</p>}
      </article>
    </div>
    {audits.map(audit => {
      const record = data?.results.find(row => row.id === audit.control.resultId &&
        row.evidenceIds[0] === audit.control.evidenceId)
      const source = data?.evidence.find(item => item.id === audit.control.evidenceId &&
        item.page === audit.control.page && item.box)
      return <details className="passport-quality-audit" key={audit.scope}>
        <summary>{audit.method} · {audit.scope}</summary>
        <dl className="passport-audit-facts">
          <div><dt>{audit.referenceLabel}</dt><dd>{number.format(audit.referenceCount)}</dd></div>
          <div><dt>Опубликовано</dt><dd>{number.format(audit.publishedCount)}</dd></div>
          <div><dt>Совпало с эталоном</dt><dd>{number.format(audit.matchedCount)}</dd></div>
          {audit.referenceCount > audit.matchedCount && <div>
            <dt>{audit.publishedCount === audit.matchedCount ? 'Не попали в выпуск' : 'Без совпадения'}</dt>
            <dd>{number.format(audit.referenceCount - audit.matchedCount)}</dd>
          </div>}
        </dl>
        <p>{audit.criteria}</p>
        {record && source && <p>{audit.control.label} <a className="passport-source" href={resultLink(record.id)}>
          Открыть контрольный источник в PDF
        </a></p>}
      </details>
    })}
  </section>
}
