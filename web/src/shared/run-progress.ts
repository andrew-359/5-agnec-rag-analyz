import type { ArticleRun } from './article.ts'
import { tableStageTitle } from './article.ts'

type State = 'queued' | 'running' | 'done' | 'failed'
export type ProgressStep = {
  id: string
  label: string
  status: State
  error: string | null
  note?: string
  page?: number
  candidateId?: string
  tableId?: string
  resultCount?: number | null
  remarkCount?: number | null
}

export function runProgress(run: ArticleRun) {
  const steps: ProgressStep[] = []
  if (run.articleText) steps.push({ id: run.articleText.id, label: 'Чтение текста статьи',
    status: run.articleText.status, error: run.articleText.error })
  for (const table of run.stages) {
    const reading = table.gridRead?.status === 'running' || (table.status === 'queued' && table.gridRead?.status === 'queued')
    steps.push({ id: table.id, label: `${tableStageTitle(table)} · стр. ${table.page}`,
      status: reading ? table.gridRead!.status : table.status, error: table.error,
      note: reading ? 'Чтение структуры таблицы' : table.emptyResultReason ?? undefined,
      page: table.page, candidateId: table.candidateId, tableId: table.id,
      resultCount: table.resultCount, remarkCount: table.remarkCount })
  }
  for (const figure of run.figures) steps.push({ id: figure.id,
    label: `Рисунок ${figure.label ?? 'без номера'} · стр. ${figure.page}`,
    status: figure.status, error: figure.error, candidateId: figure.candidateId, page: figure.page,
    resultCount: figure.resultCount, remarkCount: figure.remarkCount })
  if (run.publicationStatus) steps.push({ id: 'publication', label: 'Результаты из текста и сборка комплекта',
    status: run.publicationStatus === 'empty' ? 'done' : run.publicationStatus,
    error: run.publicationError ?? null, note: run.publicationNote ?? undefined })
  const active = steps.filter(step => step.status === 'running')
  const queued = steps.filter(step => step.status === 'queued')
  const failed = steps.filter(step => step.status === 'failed')
  const completed = steps.filter(step => step.status === 'done' || step.status === 'failed').length
  const pending = active.length > 0 || queued.length > 0
  const label = active.length ? active.slice(0, 2).map(step => step.note === 'Чтение структуры таблицы' ? `${step.label}: чтение структуры` : step.label).join(' · ') + (active.length > 2 ? ` · Ещё в работе: ${active.length - 2}` : '')
    : queued.length ? queued.some(step => step.id !== 'publication') ? 'Этапы ожидают запуска' : 'Ожидаем сборку комплекта'
    : run.publicationStatus === 'failed' ? 'Не удалось собрать комплект результатов'
    : run.publicationStatus === 'empty' ? 'Разбор завершён · нет подтверждённых результатов'
    : run.publication ? run.publication.coverage.partial ? 'Результаты опубликованы · частичный охват' : 'Результаты опубликованы'
    : failed.length ? 'Обработка завершена с ошибками' : steps.length ? 'Обработка этапов завершена' : 'Нет запланированных этапов'
  return { steps, active, queued, failed, completed, pending, label }
}
