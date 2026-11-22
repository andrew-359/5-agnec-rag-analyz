import { mkdir, readFile, rm, writeFile } from 'node:fs/promises'
import { basename, dirname, join } from 'node:path'
import type { ArticleRun } from '../../shared/article.ts'
import { reconciliationSchema } from '../../shared/reconciliation.ts'
import { parsePublicationArtifact } from '../publication/publication-data.ts'
import { loadReleaseArtifacts, publicationErrorMessage, reconciliationPath } from './article-publication-artifacts.ts'
import type { ArticleReleaseWork } from './article-release-store.ts'
import type { openArticleRuns } from './article-run-store.ts'

type Runs = ArticleReleaseWork & Pick<ReturnType<typeof openArticleRuns>, 'result'>

export async function readReconciliation(path: string, run: ArticleRun) {
  const report = reconciliationSchema.parse(JSON.parse(await readFile(path, 'utf8')))
  if (report.run_id !== run.id || report.source.sha256 !== run.pdfSha256) {
    throw new Error('Итог сверки относится к другому прогону или версии PDF.')
  }
  const artifacts = report.artifacts.map(({ id, kind, data }) => ({
    id, kind, artifact: parsePublicationArtifact(data, id, run.pdfSha256),
  }))
  const observations = new Map(artifacts.map(({ id, artifact }) => [id, new Set(artifact.data.results.map(row => row.id))]))
  if (observations.size !== artifacts.length) throw new Error('В итоге повторяются идентификаторы артефактов.')
  for (const relation of report.relationships) {
    for (const member of relation.members) {
      if (!observations.get(member.artifact_id)?.has(member.result_id)) {
        throw new Error(`Связь ссылается на отсутствующий результат ${member.result_id}.`)
      }
    }
  }
  for (const context of report.contexts) {
    if (!observations.has(context.artifact_id) || context.data.sha256 !== run.pdfSha256) {
      throw new Error('Подборка относится к другому PDF или этапу.')
    }
  }
  return { report, artifacts }
}

export async function ensureReconciliation(
  store: string,
  runs: Runs,
  run: ArticleRun,
  token: string,
  pythonTask: (args: string[], timeout: number) => Promise<Buffer>,
) {
  const destination = reconciliationPath(store, run.id)
  try {
    return await readReconciliation(destination, run)
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error
  }

  const { sourceArtifacts, rejectedArtifacts } = await loadReleaseArtifacts(run, runs.releaseArtifacts(run.id))
  const temporary = destination + '.' + token + '.tmp'
  const args = ['-m', 'rag_mvp.reconciliation', '--run-id', run.id, '--pdf-sha256', run.pdfSha256,
    ...sourceArtifacts.flatMap(({ id, kind, path }) => ['--' + kind, id, path]), '--out', temporary]
  const region = run.figures[0]?.plan?.target.stage === 'region'
  const text = !region && run.articleText && runs.result(run.id, run.articleText.id)
  let proseError = run.articleText?.error ?? null
  try {
    await mkdir(dirname(destination), { recursive: true })
    if (text) {
      const filename = join(text.folder, 'article-text.json')
      try {
        await pythonTask([...args, '--article-text', filename], 60_000)
      } catch (error) {
        const detail = publicationErrorMessage(error).replaceAll(filename, basename(filename))
        proseError = detail.replaceAll(temporary, basename(destination)).slice(0, 1_000)
      }
    }
    if (!text || proseError) await pythonTask(args, 60_000)
    const { report, artifacts } = await readReconciliation(temporary, run)
    const contexts = sourceArtifacts.flatMap(({ id, context }) => context ? [{ artifact_id: id, data: context }] : [])
    const ready = { ...report, contexts, rejectedArtifacts, proseError }
    await writeFile(temporary, JSON.stringify(ready, null, 2) + '\n')
    runs.saveReleaseArtifact(run.id, token, temporary, destination)
    return { report: ready, artifacts }
  } finally {
    await rm(temporary, { force: true })
  }
}
