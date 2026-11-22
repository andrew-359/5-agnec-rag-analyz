import { parseArgs } from 'node:util'
import type { ArticleRun } from '../src/shared/article.ts'

async function main() {
  const { values, positionals } = parseArgs({ allowPositionals: true, options: {
    'base-url': { type: 'string', default: 'http://127.0.0.1:4310' },
    'article-id': { type: 'string' }, 'candidate-id': { type: 'string' },
    reader: { type: 'string' }, parameters: { type: 'string', default: '{}' },
    'run-id': { type: 'string' },
  } })
  if (positionals.length !== 1) throw new Error('Expected catalog, run, status or artifact command.')
  const [command] = positionals
  let path = '/api/figure-readers'
  let options: RequestInit = {}
  if (command === 'run') {
    if (!values['article-id'] || !values['candidate-id'] || !values.reader) {
      throw new Error('run requires --article-id, --candidate-id and --reader (ID or auto).')
    }
    path = `/api/articles/${encodeURIComponent(values['article-id'])}/figure-runs`
    options = { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({
      target: { stage: 'figure', candidate_id: values['candidate-id'] },
      selection: values.reader === 'auto' ? { mode: 'auto' }
        : { mode: 'explicit', strategy_id: values.reader, parameters: JSON.parse(values.parameters) },
    }) }
  } else if (command === 'status' || command === 'artifact') {
    if (!values['run-id']) throw new Error(`${command} requires --run-id.`)
    path = `/api/article-runs/${encodeURIComponent(values['run-id'])}`
  } else if (command !== 'catalog') throw new Error('Expected catalog, run, status or artifact command.')
  const response = await fetch(new URL(path, values['base-url']), { ...options, signal: AbortSignal.timeout(30_000) })
  if (!response.ok) throw new Error(`HTTP ${response.status}: ${await response.text()}`)
  const data = await response.json()
  if (command === 'artifact') {
    const run = data as ArticleRun
    if (run.figures.length !== 1) throw new Error('Expected a run with exactly one figure.')
    const [figure] = run.figures
    if (figure.status !== 'done') throw new Error(`Figure artifact is not available: ${figure.status}. ${figure.error ?? ''}`)
    const artifact_url = new URL(`${path}/stages/${encodeURIComponent(figure.id)}/extracted.json`, values['base-url']).href
    const artifact = await fetch(artifact_url, { signal: AbortSignal.timeout(30_000) })
    if (!artifact.ok) throw new Error(`HTTP ${artifact.status}: ${await artifact.text()}`)
    const source_url = new URL(`${path}/source.pdf?sha256=${run.pdfSha256}#page=${figure.page}`, values['base-url']).href
    console.log(JSON.stringify({ run_id: run.id, artifact_url, source_url, artifact: await artifact.json() }, null, 2))
  } else console.log(JSON.stringify(data, null, 2))
}

try { await main() } catch (error) {
  console.error(error instanceof Error ? error.message : String(error))
  process.exitCode = 1
}
