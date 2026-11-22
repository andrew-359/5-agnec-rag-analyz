import { mkdir, readFile, writeFile } from 'node:fs/promises'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { DatabaseSync } from 'node:sqlite'
import { setTimeout } from 'node:timers/promises'
import type { ArticleRun } from '../src/shared/article.ts'
import { reconciliationSchema } from '../src/shared/reconciliation.ts'
import { reconciliationPath } from '../src/server/article/article-publication-artifacts.ts'
import { startArticlePublication } from '../src/server/article/article-publication.ts'
import { openArticleRuns } from '../src/server/article/article-run-store.ts'
import { createPublicationStore } from '../src/server/publication/publication-store.ts'

const [folderArg, pdfArg, outputArg, mode] = process.argv.slice(2)
if (!folderArg || !pdfArg || !outputArg || (mode && mode !== '--append')) {
  throw new Error('Usage: node web/scripts/replay-article-publication.ts SAVED_RUN PDF OUTPUT [--append]')
}
const append = mode === '--append'
const [folder, pdf, output] = [folderArg, pdfArg, outputArg].map(item => resolve(item))
const read = async (name: string) => JSON.parse(await readFile(join(folder, name), 'utf8'))
const saved: ArticleRun = await read('run.json')
const report = reconciliationSchema.parse(await read('reconciliation.json'))
if (report.run_id !== saved.id || report.source.sha256 !== saved.pdfSha256) throw new Error('Run and reconciliation disagree')
await mkdir(output, { recursive: append })
const runs = openArticleRuns(output)
const { id } = runs.create(saved, saved.stages, saved.coverage ?? {
  excludedCandidates: [], deferredFigures: [], discoveryWarnings: [], discoveryLimitations: [],
}, { publish: true, figures: saved.figures, withText: false })
const queued = runs.get(id)!
const ids = new Map([
  ...saved.stages.map(stage => [stage.id, queued.stages.find(s => s.candidateId === stage.candidateId)!.id] as const),
  ...saved.figures.map(stage => [stage.id, queued.figures.find(s => s.candidateId === stage.candidateId)!.id] as const),
])
const remap = (old: string) => ids.get(old) ?? old
const db = new DatabaseSync(join(output, 'article-runs.sqlite'))
try {
  const mark = db.prepare(`UPDATE article_run_stages SET status = ?, result_count = ?, remark_count = ?, error = ? WHERE id = ?`)
  for (const stage of [...saved.stages, ...saved.figures]) {
    mark.run(stage.status, stage.resultCount, stage.remarkCount, stage.error, remap(stage.id))
  }
} finally {
  db.close()
}
const path = reconciliationPath(output, id)
await mkdir(dirname(path), { recursive: true })
const reference = <T extends { artifact_id: string }>(item: T) => ({ ...item, artifact_id: remap(item.artifact_id) })
const relationship = (relation: typeof report.relationships[number]) => ({
  ...relation, members: relation.members.map(reference),
})
await writeFile(path, JSON.stringify({
  ...report, run_id: id,
  artifacts: report.artifacts.map(item => ({ ...item, id: remap(item.id) })),
  contexts: report.contexts.map(reference),
  rejectedArtifacts: report.rejectedArtifacts.map(item => ({ ...item, id: remap(item.id) })),
  relationships: report.relationships.map(relationship),
  summary: report.summary && {
    confirmed: report.summary.confirmed.map(reference),
    disputed: report.summary.disputed.map(relationship),
    stage_remarks: report.summary.stage_remarks.map(reference),
  },
}))
const directory = join(output, 'publications')
const publications = createPublicationStore({ directory, root: fileURLToPath(new URL('../..', import.meta.url)) })
const abort = new AbortController()
startArticlePublication(runs, { store: output, sourcePdf: () => pdf, publications,
  shutdown: abort.signal, pythonTask: async () => { throw new Error('Expected a saved reconciliation') } })
try {
  const deadline = Date.now() + 120_000
  let completed = false
  while (Date.now() < deadline) {
    const run = runs.get(id)!
    if (run.publicationStatus === 'empty' && append) {
      console.log(JSON.stringify({ runId: id, status: 'empty', note: run.publicationNote }))
      completed = true
      break
    }
    if (run.publicationStatus === 'empty' || run.publicationStatus === 'failed') {
      throw new Error(`${run.publicationStatus}: ${run.publicationError ?? run.publicationNote ?? ''}`)
    }
    if (run.publication) {
      const { publicationId, versionId, coverage } = run.publication
      const result = { zip: join(directory, publicationId, versionId, 'export.zip'), coverage }
      if (!append) await writeFile(join(output, 'publication.json'), JSON.stringify(result, null, 2) + '\n')
      console.log(JSON.stringify({ ...result, runId: id }))
      completed = true
      break
    }
    await setTimeout(100)
  }
  if (!completed) throw new Error('Publication timed out')
} finally {
  abort.abort()
}
