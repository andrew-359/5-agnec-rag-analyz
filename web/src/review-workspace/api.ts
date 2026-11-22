import { navigateWorkspace } from './navigation'
import { articleRunReceiptSchema, type Article, type ArticleRun, type ArticleOverviewStatus } from '../shared/article'
import { plotRunSchema } from '../shared/article-plot'
import type { PublishedVersion } from '../shared/publication'
import type { Dataset } from '../shared/schema'
import type { z } from 'zod'
import type { strategyCatalogSchema } from '../shared/strategy-catalog'

export type Readers = Partial<Record<'text' | 'table' | 'figure', string>>

export function labUrl(params: Record<string, string> = {}) {
  return '/?' + new URLSearchParams({ lab: '1', ...params })
}

export function updateQuery(values: Record<string, string | null>) {
  const url = new URL(location.href)
  for (const [name, value] of Object.entries(values)) {
    if (value === null) url.searchParams.delete(name)
    else url.searchParams.set(name, value)
  }
  navigateWorkspace(url.href, true)
}

export function articlePath(id: string) {
  return '/api/articles/' + encodeURIComponent(id)
}

export function runPath(id: string) {
  return '/api/article-runs/' + encodeURIComponent(id)
}

export function runPdf(id: string, pdfSha256: string) {
  return runPath(id) + '/source.pdf?sha256=' + encodeURIComponent(pdfSha256)
}

export async function json<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(path, options)
  if (response.status === 204) return undefined as T
  const data = await response.json().catch(() => undefined)
  if (!response.ok || data === undefined) {
    throw new Error(data?.error?.message ?? data?.error ?? `Запрос не выполнен (${response.status}).`)
  }
  return data as T
}

export function post<T>(path: string, data: object) {
  return json<T>(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) })
}

export function upload(file: File) {
  return json<Article>('/api/articles', {
    method: 'POST', body: file,
    headers: { 'Content-Type': 'application/pdf', 'X-PDF-Name': encodeURIComponent(file.name) },
  })
}

export function catalogue() {
  return json<z.infer<typeof strategyCatalogSchema>>('/api/strategies')
}

export function start(id: string, readers?: Readers) {
  return post<{ id: string }>(articlePath(id) + '/runs', readers ? { readers } : {})
}

export async function startTable(articleId: string, candidateId: string) {
  const response = await json<unknown>(articlePath(articleId) + '/tables/' + encodeURIComponent(candidateId), { method: 'POST' })
  const receipt = articleRunReceiptSchema.safeParse(response)
  if (!receipt.success) throw new Error('Не удалось запустить прогон таблицы.')
  return receipt.data
}

export async function readPlot(article: Article, candidate: Article['candidates'][number]) {
  const response = await json<unknown>(articlePath(article.id) + '/plots/' + encodeURIComponent(candidate.id), { method: 'POST' })
  const result = plotRunSchema.safeParse(response)
  if (!result.success) throw new Error('Не удалось прочитать результат разбора графика.')
  if (result.data.source.page !== candidate.page || (article.sha256 && result.data.sha256 !== article.sha256)) {
    throw new Error('Разбор графика относится к другому PDF или странице.')
  }
  return result.data
}

export async function loadArticle(articleId: string, runId: string | null, signal: AbortSignal) {
  const [article, status] = await Promise.all([
    json<Article>(articlePath(articleId), { signal }),
    runId ? null : json<ArticleOverviewStatus>(articlePath(articleId) + '/overview-status', { signal }),
  ])
  return { article, runId: runId ?? status?.runId ?? null, resultRunId: runId ?? status?.resultRunId ?? null }
}

export async function readRun(runId: string, articleId: string, signal: AbortSignal) {
  const run = await json<ArticleRun>(runPath(runId), { signal })
  if (run.articleId !== articleId) throw new Error('Прогон относится к другой статье.')
  return run
}

export function publicationFile({ publicationId, versionId }: PublishedVersion, name: string) {
  return `/api/publications/${encodeURIComponent(publicationId)}/versions/${encodeURIComponent(versionId)}/${name}`
}

export async function readPublication(version: PublishedVersion, article: Article, signal: AbortSignal) {
  const { publicationId, versionId } = version
  const input = new URLSearchParams({ input: JSON.stringify({ publicationId, versionId }) })
  const response = await json<{ result: { data: Dataset } }>('/trpc/publications.review?' + input, { signal })
  const data = response.result.data
  if (data.pdf.sha256 !== article.sha256 || data.version?.versionId !== versionId || data.publication.id !== publicationId) {
    throw new Error('Выпуск не совпадает с выбранным прогоном.')
  }
  return data
}
