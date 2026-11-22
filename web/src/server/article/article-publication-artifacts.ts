import { readFile } from 'node:fs/promises'
import { basename, dirname, join } from 'node:path'
import type { ArticleRun } from '../../shared/article.ts'
import { articleContextFilename, articleContextSchema, reconciliationFilename } from '../../shared/reconciliation.ts'
import type { ArticleReleaseWork } from './article-release-store.ts'
import { parsePublicationArtifact } from '../publication/publication-data.ts'

export function proseArtifactPath(store: string, runId: string) {
  return join(store, 'runs', runId, 'prose-results.json')
}

export function reconciliationPath(store: string, runId: string) {
  return join(store, 'runs', runId, reconciliationFilename)
}

export const publicationErrorMessage = (error: unknown) => error instanceof Error ? error.message : String(error)

export async function loadPublicationArtifact(path: string, id: string, pdfSha256: string) {
  const raw: unknown = JSON.parse(await readFile(path, 'utf8'))
  return parsePublicationArtifact(raw, id, pdfSha256)
}

async function readContext(path: string) {
  try {
    const raw: unknown = JSON.parse(await readFile(join(dirname(path), articleContextFilename), 'utf8'))
    return articleContextSchema.parse(raw)
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error
  }
}

export async function loadReleaseArtifacts(
  run: ArticleRun,
  files: ReturnType<ArticleReleaseWork['releaseArtifacts']>,
) {
  const tables = new Map(run.stages.map(stage => [stage.id, stage]))
  const figures = new Map(run.figures.map(stage => [stage.id, stage]))

  const loaded = await Promise.all(files.map(async file => {
    try {
      const artifact = await loadPublicationArtifact(file.path, file.id, run.pdfSha256)
      const planned = file.kind === 'table' ? tables.get(file.id) : figures.get(file.id)
      if (!planned) throw new Error('Этап сохранённого артефакта отсутствует в прогоне.')
      if ('table' in planned && (planned.page !== artifact.data.page || planned.table !== artifact.data.table)) {
        throw new Error('Адрес сохранённой таблицы отличается от адреса этапа.')
      }
      const context = await readContext(file.path)
      if (context && (context.sha256 !== run.pdfSha256 || context.target.page !== planned.page
        || ('table' in planned ? context.target.number !== planned.table : context.target.candidate !== planned.candidateId))) {
        throw new Error('Сохранённая подборка относится к другому PDF или этапу.')
      }
      return { ...file, artifact, context }
    } catch (error) {
      return { ...file, reason: publicationErrorMessage(error).replaceAll(file.path, basename(file.path)).slice(0, 1_000) }
    }
  }))
  const sourceArtifacts = loaded.flatMap(file => {
    if (!('artifact' in file)) return []
    const { artifact, ...address } = file
    return [{ ...address, data: artifact.data, artifact }]
  })
  const rejectedArtifacts = loaded.flatMap(file => {
    if (!('reason' in file)) return []
    const { reason, id, kind, path } = file
    const table = tables.get(id)
    const figure = figures.get(id)
    return [{ id, kind, file: basename(path), page: table?.page ?? figure?.page, table: table?.table, figure: figure?.label, reason }]
  })

  return { sourceArtifacts, rejectedArtifacts }
}
