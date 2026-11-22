import { createHash } from 'node:crypto'
import { rmdirSync } from 'node:fs'
import { mkdir, readFile, rename, rmdir, stat, writeFile } from 'node:fs/promises'
import { basename, join } from 'node:path'
import { setTimeout as delay } from 'node:timers/promises'
import { parseArgs } from 'node:util'
import { uploadLimit, type Article, type ArticleRun } from '../src/shared/article.ts'
import { reconciliationFilename, reconciliationSchema } from '../src/shared/reconciliation.ts'
import { strategyCatalogRoute, strategyCatalogSchema } from '../src/shared/strategy-catalog.ts'

const stages = ['table', 'text', 'figure'] as const
type Stage = typeof stages[number]
type Readers = Partial<Record<Stage, string>>
type Attempt = {
  readers: Readers
  directory: string
  run_id?: string
  status: 'planned' | 'pending' | 'failed' | 'done'
  reconciliation?: boolean
  error?: string
}
const id = encodeURIComponent
const auto = 'auto'
const strategyOptions = {
  'base-url': { type: 'string', default: 'http://127.0.0.1:4310' },
  help: { type: 'boolean' },
  json: { type: 'boolean' },
} as const

async function save(path: string, value: unknown) {
  await writeFile(path + '.tmp', JSON.stringify(value, null, 2) + '\n')
  await rename(path + '.tmp', path)
}

async function json<T>(origin: string, path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(new URL(path, origin), {
    ...init, redirect: 'error', signal: AbortSignal.timeout(init?.method === 'POST' ? 610_000 : 30_000),
  })
  if (!response.ok) throw new Error(`HTTP ${response.status}: ${await response.text()}`)
  return response.json()
}

async function listStrategies(origin: string, asJson = false) {
  const catalog = strategyCatalogSchema.parse(await json(origin, '/api' + strategyCatalogRoute))
  if (asJson) {
    console.log(JSON.stringify(catalog, null, 2))
    return
  }

  const titles = { text: 'Текст', table: 'Таблицы', figure: 'Графики' }
  for (const stage of ['text', 'table', 'figure'] as const) {
    console.log(titles[stage])
    for (const reader of catalog[stage]) {
      const crop = 'requires_crop' in reader && reader.requires_crop
        ? ' Требуется выделенная область (--crop в Python CLI).'
        : ''
      console.log(`  ${reader.id} — ${reader.description}${crop}`)
    }
  }
}

function post(value: unknown): RequestInit {
  return { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(value) }
}

async function documentInput(input: string) {
  if (/^https:\/\//i.test(input)) {
    const url = new URL(input)
    const response = await fetch(url, { redirect: 'error', signal: AbortSignal.timeout(60_000) })
    if (!response.ok || !response.body) throw new Error(`Не удалось скачать PDF: HTTP ${response.status}.`)
    const reader = response.body.getReader()
    const chunks: Uint8Array[] = []
    let size = 0
    try {
      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        size += value.length
        if (size > uploadLimit) throw new Error('PDF превышает лимит загрузки.')
        chunks.push(value)
      }
    } finally {
      await reader.cancel()
    }
    return { pdf: Buffer.concat(chunks), name: basename(url.pathname) || 'document.pdf' }
  }

  if (input.includes('://')) throw new Error('Для удалённого PDF нужна прямая HTTPS-ссылка.')

  if (input.includes('/') || input.toLowerCase().endsWith('.pdf')) {
    if ((await stat(input)).size > uploadLimit) throw new Error('PDF превышает лимит загрузки.')
    return { pdf: await readFile(input), name: basename(input) }
  }

  return { articleId: input }
}

async function collect(origin: string, runId: string, sha256: string, folder: string, deadline: number) {
  const route = `/api/article-runs/${id(runId)}`
  let run: ArticleRun
  while (true) {
    run = await json<ArticleRun>(origin, route)
    if (run.id !== runId || run.pdfSha256 !== sha256) throw new Error('Прогон относится к другой версии PDF.')
    await save(join(folder, 'run.json'), run)
    const statuses = [...run.stages.map(x => x.status), ...run.figures.map(x => x.status),
      run.articleText?.status, run.publicationStatus, run.reconciliationStatus]
    if (!statuses.some(status => status === 'queued' || status === 'running')) break
    if (Date.now() >= deadline) return { status: 'pending' as const }
    await delay(500)
  }

  const saveStage = async (name: string, path: string) => {
    const artifact = await json<Record<string, unknown>>(origin, path)
    await save(join(folder, name), { ...artifact, run_id: runId })
  }
  for (const stage of run.stages.filter(x => x.status === 'done')) {
    await saveStage(`table-${id(stage.id)}.json`, `${route}/stages/${id(stage.id)}`)
  }
  for (const { gridRead } of run.stages) {
    if (!gridRead) continue
    for (const attempt of gridRead.attempts.filter(item => item.hasGridArtifact)) {
      const grid = await json(origin, `${route}/stages/${id(gridRead.id)}/attempts/${attempt.number}/table-grid.json`)
      await save(join(folder, `grid-${id(gridRead.id)}-${attempt.number}.json`), grid)
    }
  }
  for (const stage of run.figures.filter(x => x.status === 'done')) {
    await saveStage(`figure-${id(stage.id)}.json`, `${route}/stages/${id(stage.id)}/extracted.json`)
  }
  if (run.articleText?.status === 'done') {
    await saveStage('article-text.json', `${route}/stages/${id(run.articleText.id)}/article-text.json`)
  }
  const reconciliation = run.reconciliationStatus === 'done' || run.reconciliationStatus === 'empty'
  if (reconciliation) {
    const report = reconciliationSchema.parse(await json(origin, `${route}/${reconciliationFilename}`))
    if (report.run_id !== runId || report.source.sha256 !== sha256) throw new Error('Итог относится к другому PDF или прогону.')
    await save(join(folder, reconciliationFilename), report)
  }
  if (run.publication) {
    const response = await fetch(new URL(run.publication.downloadUrl, origin), {
      redirect: 'error', signal: AbortSignal.timeout(60_000),
    })
    if (!response.ok) throw new Error(`Выгрузка недоступна: HTTP ${response.status}.`)
    await writeFile(join(folder, 'export.zip.tmp'), Buffer.from(await response.arrayBuffer()))
    await rename(join(folder, 'export.zip.tmp'), join(folder, 'export.zip'))
  }

  const failed = [...run.stages, ...run.figures, run.articleText].some(stage => stage?.status === 'failed')
    || run.publicationStatus === 'failed' || run.reconciliationStatus === 'failed'
  return { status: failed ? 'failed' as const : 'done' as const, reconciliation }
}

async function main() {
  const { values, positionals: [command, input, ...extra] } = parseArgs({ allowPositionals: true, options: {
    ...strategyOptions,
    out: { type: 'string' }, figure: { type: 'string' }, jobs: { type: 'string' },
    'table-reader': { type: 'string', multiple: true }, 'text-reader': { type: 'string', multiple: true },
    'figure-reader': { type: 'string', multiple: true }, 'parameters-file': { type: 'string' },
    'wait-seconds': { type: 'string' },
  } })
  if (values.help) {
    console.log('Usage: rag run|compare PDF_OR_ARTICLE_ID --out DIR [--figure LABEL_OR_ID] [--table-reader ID] [--text-reader ID] [--figure-reader ID] [--parameters-file JSON] [--jobs N] [--wait-seconds N] [--base-url URL]')
    console.log('       rag strategies [--json] [--base-url URL]')
    console.log('       rag demo [--port N] [--lab-api URL | --corpus DIR --run DIR --version ID --comparison-api URL]')
    return
  }
  if (command === 'strategies') {
    if (input !== undefined || Object.keys(values).some(option => !Object.hasOwn(strategyOptions, option))) {
      throw new Error('Использование: rag strategies [--json] [--base-url URL].')
    }
    await listStrategies(new URL(values['base-url']).origin, values.json)
    return
  }
  if (values.json !== undefined) throw new Error('--json доступен для rag strategies.')
  if ((command !== 'run' && command !== 'compare') || !input || extra.length || !values.out) {
    throw new Error('Использование: rag run|compare ДОКУМЕНТ --out КАТАЛОГ (см. --help).')
  }
  const origin = new URL(values['base-url']).origin
  const out = values.out
  const jobs = Number(values.jobs ?? '1')
  const wait = Number(values['wait-seconds'] ?? '1800')
  if (!Number.isSafeInteger(jobs) || jobs < 1 || !Number.isFinite(wait) || wait < 0) {
    throw new Error('Нужны положительный целый --jobs и неотрицательный --wait-seconds.')
  }
  const selected = stages.filter(stage => values[`${stage}-reader`]?.length)
  if (command === 'run' && selected.some(stage => values[`${stage}-reader`]!.length > 1)) {
    throw new Error('Для run выберите по одному ридеру на этап.')
  }
  if (command === 'compare' && (selected.length !== 1 || values[`${selected[0]}-reader`]!.length < 2)) {
    throw new Error('Для compare повторите флаг ридера одного этапа.')
  }
  if (values.figure && selected.some(stage => stage !== 'figure')) {
    throw new Error('--figure запускает только рисунок.')
  }
  const parameters = values['parameters-file'] ? JSON.parse(await readFile(values['parameters-file'], 'utf8')) : {}
  if (!parameters || typeof parameters !== 'object' || Array.isArray(parameters)
      || (values['parameters-file'] && !selected.includes('figure'))) {
    throw new Error('--parameters-file требует JSON-объект и выбор ридера графика.')
  }
  const plans: Readers[] = command === 'compare'
    ? values[`${selected[0]}-reader`]!.map(reader => ({ [selected[0]]: reader }))
    : [Object.fromEntries(selected.map(stage => [stage, values[`${stage}-reader`]![0]]))]
  if (command === 'compare' && new Set(values[`${selected[0]}-reader`]!).size !== plans.length) {
    throw new Error('Для сравнения укажите разные ридеры.')
  }
  if (values['parameters-file'] && plans.some(plan => !plan.figure || plan.figure === auto)) {
    throw new Error('--parameters-file требует явного ридера графика для каждого прогона.')
  }
  const key = JSON.stringify({ command, input, origin, figure: values.figure, plans, parameters, jobs })
  const checkpoint = join(out, command === 'compare' ? 'comparison.json' : 'manifest.json')
  const source = await documentInput(input)
  const digest = source.pdf && createHash('sha256').update(source.pdf).digest('hex')
  await mkdir(out, { recursive: true })
  const lock = join(out, '.rag-lock')
  await mkdir(lock).catch((error: NodeJS.ErrnoException) => {
    if (error.code === 'EEXIST') throw new Error('Каталог --out уже занят другим процессом.')
    throw error
  })
  const interrupt = (signal: NodeJS.Signals) => {
    rmdirSync(lock)
    process.kill(process.pid, signal)
  }
  process.once('SIGINT', interrupt)
  process.once('SIGTERM', interrupt)
  try {
    const otherMode = command === 'run' ? 'comparison.json' : 'manifest.json'
    const occupied = await stat(join(out, otherMode)).then(() => true, (error: NodeJS.ErrnoException) => {
      if (error.code !== 'ENOENT') throw error
      return false
    })
    if (occupied) throw new Error('Каталог содержит результаты другой команды. Выберите новый --out.')

    let state: { key: string; sha256?: string; article?: Article; attempts: Attempt[] }
    try {
      state = JSON.parse(await readFile(checkpoint, 'utf8'))
      if (state.key !== key || state.sha256 !== digest) throw new Error('Вход изменился; выберите новый --out.')
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error
      state = { key, sha256: digest, attempts: plans.map((readers, index) => ({
        readers, directory: String(index + 1).padStart(2, '0'), status: 'planned',
      })) }
    }
    state.article ??= source.pdf
      ? await json<Article>(origin, '/api/articles', {
        method: 'POST', headers: { 'Content-Type': 'application/pdf', 'X-PDF-Name': encodeURIComponent(source.name) },
        body: new Uint8Array(source.pdf),
      })
      : await json<Article>(origin, `/api/articles/${id(source.articleId)}`)
    const article = state.article
    const sha256 = article.sha256
    if (!sha256) throw new Error('Сервер не вернул SHA-256 PDF.')
    if (digest && sha256 !== digest) throw new Error('Сервер сохранил другую версию PDF.')
    await save(checkpoint, state)
    const matches = values.figure ? article.candidates.filter(candidate =>
      candidate.kind === 'figure' && (candidate.id === values.figure || candidate.label === values.figure)) : []
    if (values.figure && matches.length !== 1) {
      throw new Error('Рисунок не найден однозначно в карте. Укажите ID кандидата или используйте адресный диагностический ридер.')
    }
    for (const attempt of state.attempts) {
      if (!attempt.reconciliation || attempt.status !== 'done') continue
      await stat(join(out, attempt.directory, reconciliationFilename)).catch((error: NodeJS.ErrnoException) => {
        if (error.code !== 'ENOENT') throw error
        attempt.status = 'pending'
      })
    }
    const remaining = state.attempts.filter(attempt => attempt.status !== 'done')
      .sort((a, b) => Number(Boolean(b.run_id)) - Number(Boolean(a.run_id)))
    let cursor = 0
    let persistence = Promise.resolve()
    const persist = () => persistence = persistence.then(() => save(checkpoint, state))
    async function worker(sha256: string) {
      while (cursor < remaining.length) {
        const attempt = remaining[cursor++]
        const folder = join(out, attempt.directory)
        await mkdir(folder, { recursive: true })
        try {
          if (!attempt.run_id) {
            const reader = attempt.readers.figure ?? auto
            const selection = reader === auto ? { mode: auto } : { mode: 'explicit', strategy_id: reader, parameters }
            const route = `/api/articles/${id(article.id)}`
            const accepted = matches.length
              ? await json<{ run_id: string }>(origin, route + '/figure-runs', post({
                target: { stage: 'figure', candidate_id: matches[0].id }, selection,
              }))
              : await json<{ id: string }>(origin, route + '/runs', post({
                readers: attempt.readers, figure_parameters: parameters, ...(command === 'compare' ? { publish: false } : {}),
              }))
            attempt.run_id = 'run_id' in accepted ? accepted.run_id : accepted.id
            await persist()
          }
          Object.assign(attempt, await collect(origin, attempt.run_id, sha256, folder, Date.now() + wait * 1000))
          delete attempt.error
        } catch (error) {
          attempt.status = attempt.run_id ? 'pending' : 'failed'
          attempt.error = error instanceof Error ? error.message : String(error)
        }
        await persist()
        if (attempt.status === 'pending') return
      }
    }
    await Promise.all(Array.from({ length: Math.min(jobs, remaining.length) }, () => worker(sha256)))
    console.log(checkpoint)
    for (const attempt of state.attempts) {
      if (attempt.status === 'done') continue
      process.exitCode = 1
      if (attempt.status !== 'planned') {
        console.error(`${attempt.directory}: ${attempt.status}${attempt.error ? ` | ${attempt.error}` : ''}`)
      }
    }
  } finally {
    process.removeListener('SIGINT', interrupt)
    process.removeListener('SIGTERM', interrupt)
    await rmdir(lock)
  }
}

try { await main() } catch (error) {
  console.error(error instanceof Error ? error.message : String(error))
  process.exitCode = 1
}
