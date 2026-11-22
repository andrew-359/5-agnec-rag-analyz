import assert from 'node:assert/strict'
import { execFile, spawn, type ChildProcess } from 'node:child_process'
import { constants } from 'node:fs'
import { access, mkdir, mkdtemp, readFile, readdir, rm, stat, writeFile } from 'node:fs/promises'
import { createHash, randomUUID } from 'node:crypto'
import { createServer } from 'node:http'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { promisify } from 'node:util'
import { test } from 'node:test'
import amqp from 'amqplib'

const execute = promisify(execFile)
const root = fileURLToPath(new URL('../../', import.meta.url))
const web = join(root, 'web')

async function stop(child: ChildProcess) {
  if (child.exitCode !== null || child.signalCode !== null) return
  await new Promise<void>(resolve => {
    const force = setTimeout(() => child.kill('SIGKILL'), 3_000)
    child.once('exit', () => { clearTimeout(force); resolve() })
    child.kill('SIGTERM')
  })
}

function serverAddress(child: ChildProcess) {
  return new Promise<string>((resolve, reject) => {
    let output = ''
    let diagnosis = ''
    const timeout = setTimeout(() => finish(new Error(`Hono did not start: ${diagnosis}`)), 15_000)
    const onOutput = (chunk: Buffer) => {
      output += chunk.toString()
      const address = output.match(/RAG web: (http:\/\/127\.0\.0\.1:\d+)/)?.[1]
      if (address) finish(undefined, address)
    }
    const onError = (error: Error) => finish(error)
    const onExit = (code: number | null) => finish(new Error(`Hono exited with ${code}: ${diagnosis}`))
    const onDiagnosis = (chunk: Buffer) => { diagnosis = (diagnosis + chunk.toString()).slice(-4_000) }
    function finish(error?: Error, address?: string) {
      clearTimeout(timeout)
      child.stdout?.off('data', onOutput)
      child.stderr?.off('data', onDiagnosis)
      child.off('error', onError)
      child.off('exit', onExit)
      if (error) reject(error)
      else resolve(address!)
    }
    child.stdout?.on('data', onOutput)
    child.stderr?.on('data', onDiagnosis)
    child.once('error', onError)
    child.once('exit', onExit)
  })
}

test('the CLI lists all registered strategies and keeps the figure catalog compatible', { timeout: 30_000 }, async t => {
  const python = process.env.RAG_PYTHON
  if (!python) return t.skip('Set RAG_PYTHON to an available Python interpreter')
  try {
    await access(python, constants.X_OK)
  } catch {
    return t.skip('RAG_PYTHON is not executable')
  }

  const store = await mkdtemp(join(tmpdir(), 'rag-figure-cli-'))
  const env = {
    ...process.env, RAG_PYTHON: python, PORT: '0', RAG_HOST: '127.0.0.1', RAG_AMQP_URL: '',
    RAG_ARTICLE_STORE: join(store, 'articles'), RAG_PUBLICATION_STORE: join(store, 'publications'),
  }
  const hono = spawn(process.execPath, ['--import', 'tsx', 'src/server/index.ts'], {
    cwd: web, env, stdio: ['ignore', 'pipe', 'pipe'],
  })
  try {
    const origin = await serverAddress(hono)
    const command = await execute(process.execPath, [
      join(web, 'scripts/figure-runs.ts'), 'catalog', '--base-url', origin,
    ], { cwd: root, env, timeout: 15_000 })
    const catalog = JSON.parse(command.stdout) as { readers: Array<{
      id: string
      parameters_schema: {
        additionalProperties?: boolean
        properties?: Record<string, unknown>
        required?: string[]
      }
    }> }
    assert.ok(Array.isArray(catalog.readers))
    const bars = catalog.readers.find(reader => reader.id === 'vector-bar')
    assert.ok(bars, 'vector-bar is available for explicit figure reading')
    assert.equal(bars.parameters_schema.additionalProperties, false)
    const scatter = catalog.readers.find(reader => reader.id === 'vector-scatter')
    assert.ok(scatter)
    assert.ok(scatter.parameters_schema.required?.includes('marker_size'))
    for (const reader of catalog.readers) {
      const parameters = Object.keys(reader.parameters_schema.properties ?? {})
      assert.ok(!parameters.some(name => /^(?:model|model_name|server|server_url|base_url|ollama_url|ollama_model)$/.test(name)),
        `${reader.id} does not expose worker model or server configuration`)
    }

    const args = ['strategies', '--base-url', origin]
    const [{ stdout: json }, { stdout: plain }, { stdout: pythonCatalog }, response] = await Promise.all([
      execute(join(root, 'rag'), [...args, '--json'], { cwd: root, env }),
      execute(join(root, 'rag'), args, { cwd: root, env }),
      execute(python, ['-m', 'rag_mvp.reader_catalog'], { cwd: root, env }),
      fetch(origin + '/api/strategies'),
    ])
    assert.equal(response.status, 200)
    const all = JSON.parse(json)
    const articleReaders = JSON.parse(pythonCatalog)
    assert.deepEqual(all, await response.json())
    assert.deepEqual(all.text.map(({ id }: { id: string }) => id), articleReaders.text)
    assert.deepEqual(Object.fromEntries(all.table.map(({ id, requires_crop, accepts_region }: {
      id: string; requires_crop: boolean; accepts_region: boolean
    }) => [id, { requires_crop, accepts_region }])), articleReaders.table)
    assert.deepEqual(all.figure, catalog.readers)
    assert.match(plain, /Текст[\s\S]+Таблицы[\s\S]+Графики/)
    for (const { id } of [...all.text, ...all.table, ...all.figure]) assert.ok(plain.includes(id))
    await assert.rejects(
      execute(join(root, 'rag'), ['strategies', '--base-url', 'invalid', '--table-reader', 'pdfplumber'], { cwd: root, env }),
      /Использование: rag strategies/,
    )
  } finally {
    await stop(hono)
    await rm(store, { recursive: true, force: true })
  }
})

async function eventually<T>(read: () => Promise<T>, done: (value: T) => boolean): Promise<T> {
  const deadline = Date.now() + 45_000
  while (true) {
    const value = await read()
    if (done(value)) return value
    if (Date.now() >= deadline) throw new Error(`Timed out: ${JSON.stringify(value)}`)
    await new Promise(resolve => setTimeout(resolve, 250))
  }
}

test('the public CLI runs Figure 5 through RabbitMQ and keeps observations, binding and PDF outside the library',
  { timeout: 120_000 }, async t => {
  const { RAG_PYTHON: python, D001_PDF: pdf, RAG_AMQP_URL: broker } = process.env
  if (!python || !pdf || !broker) return t.skip('Set RAG_PYTHON, D001_PDF and RAG_AMQP_URL for queue acceptance')
  const store = await mkdtemp(join(tmpdir(), 'rag-figure-explicit-'))
  const queue = `rag.figure.ticket01.${randomUUID()}`
  const env = { ...process.env, RAG_PYTHON: python, PORT: '0', RAG_HOST: '127.0.0.1',
    RAG_AMQP_QUEUE: queue, RAG_ARTICLE_STORE: join(store, 'articles'), RAG_PUBLICATION_STORE: join(store, 'publications') }
  const connection = await amqp.connect(broker)
  const channel = await connection.createChannel()
  await channel.assertQueue(queue, { durable: true })
  const hono = spawn(process.execPath, ['--import', 'tsx', 'src/server/index.ts'], {
    cwd: web, env, stdio: ['ignore', 'pipe', 'pipe'],
  })
  let worker: ChildProcess | undefined
  try {
    const origin = await serverAddress(hono)
    const cli = async (...args: string[]) => JSON.parse((await execute(process.execPath,
      [join(web, 'scripts/figure-runs.ts'), ...args, '--base-url', origin], { cwd: root, env, timeout: 30_000 })).stdout)
    const bytes = await readFile(pdf)
    const uploaded = await fetch(`${origin}/api/articles`, { method: 'POST', body: bytes,
      headers: { 'Content-Type': 'application/pdf', 'X-PDF-Name': 'D001.pdf' } })
    assert.equal(uploaded.status, 201)
    const article = await uploaded.json()
    const figure = article.candidates.find((item: { kind: string; label: string }) => item.kind === 'figure' && item.label === '5')
    assert.ok(figure)
    const accepted = await cli('run', '--article-id', article.id, '--candidate-id', figure.id, '--reader', 'vector-bar')
    assert.ok(accepted.run_id)
    const planned = await cli('status', '--run-id', accepted.run_id)
    assert.equal(planned.pdfSha256, createHash('sha256').update(bytes).digest('hex'))
    const { region, ...target } = planned.figures[0].plan.target
    assert.ok(region, 'the queued plan freezes the actual figure region separately from its caption')
    assert.equal(region.precision, 'figure')
    assert.equal(region.panels.length, 1)
    assert.notDeepEqual(region.box, figure.box)
    assert.deepEqual({ ...planned.figures[0].plan, target }, {
      target: { stage: 'figure', candidate_id: figure.id, page: 8, figure: '5', box: figure.box },
      selection: { mode: 'explicit', strategy_id: 'vector-bar', parameters: {} },
    })
    const delivery = await eventually(() => channel.checkQueue(queue), state => state.messageCount === 1)
    assert.equal(delivery.consumerCount, 0, 'the text stage waits durably in RabbitMQ before starting Python')
    worker = spawn(python, ['-m', 'rag_mvp.article_worker'], { cwd: root, env, stdio: ['ignore', 'pipe', 'pipe'] })
    const completed = await eventually(() => cli('status', '--run-id', accepted.run_id), run => run.figures[0].status === 'done')
    assert.equal(completed.figures[0].resultCount, 12)
    assert.equal(completed.figures[0].plan.selection.strategy_id, 'vector-bar')
    assert.equal(completed.figures[0].attempts[0].strategy, 'vector-bar')
    assert.equal(completed.publicationStatus, undefined)
    const saved = await cli('artifact', '--run-id', accepted.run_id)
    const artifact = saved.artifact
    assert.equal(artifact.requested_strategy, 'vector-bar')
    assert.equal(artifact.attempted_strategy, 'vector-bar')
    assert.equal(artifact.status, 'succeeded')
    assert.equal(artifact.observations.length, 12)
    assert.equal(artifact.binding.results.length, 12)
    assert.equal(artifact.source.sha256, planned.pdfSha256)
    const control = artifact.binding.results.find((row: { value: string }) => row.value === '0.588')
    assert.deepEqual([control.method, control.dataset, control.metric, control.unit], ['STG-Paths (Ours)', '2Wiki', 'EM', 'fraction'])
    assert.equal(control.source.page, 8)
    assert.equal(control.source.figure, '5')
    assert.equal(control.source.box.length, 4)
    assert.ok(control.source.box[0] >= region.box[0] && control.source.box[1] >= region.box[1]
      && control.source.box[2] <= region.box[2] && control.source.box[3] <= region.box[3], 'the observed label belongs to the planned region')
    assert.ok(artifact.observations.some((row: { raw_value: string; source: { box: number[] } }) =>
      row.raw_value === '0.588' && JSON.stringify(row.source.box) === JSON.stringify(control.source.box)))
    const source = await fetch(saved.source_url)
    assert.equal(source.status, 200)
    assert.deepEqual(Buffer.from(await source.arrayBuffer()), bytes)

    const unsuitable = await cli('run', '--article-id', article.id, '--candidate-id', figure.id, '--reader', 'vector-line')
    const unsuitableRun = await eventually(() => cli('status', '--run-id', unsuitable.run_id), run => run.figures[0].status === 'done')
    const unsuitableSaved = await cli('artifact', '--run-id', unsuitable.run_id)
    assert.equal(unsuitableSaved.artifact.requested_strategy, 'vector-line')
    assert.equal(unsuitableSaved.artifact.attempted_strategy, 'vector-line')
    assert.equal(unsuitableSaved.artifact.status, 'not_applicable')
    assert.ok(unsuitableSaved.artifact.reason)
    assert.deepEqual(unsuitableSaved.artifact.observations, [])
    assert.deepEqual(unsuitableSaved.artifact.binding.results, [])
    assert.deepEqual(unsuitableRun.figures[0].attempts.map((attempt: { strategy: string }) => attempt.strategy), ['vector-line'])

    const missingPanel = await cli('run', '--article-id', article.id, '--candidate-id', figure.id,
      '--reader', 'vector-bar', '--parameters', '{"panel":"9"}')
    const missingPanelRun = await eventually(() => cli('status', '--run-id', missingPanel.run_id), run => run.figures[0].status === 'done')
    const missingPanelSaved = await cli('artifact', '--run-id', missingPanel.run_id)
    assert.equal(missingPanelSaved.artifact.requested_strategy, 'vector-bar')
    assert.equal(missingPanelSaved.artifact.status, 'not_applicable')
    assert.match(missingPanelSaved.artifact.reason, /no frozen vector frame/)
    assert.equal(missingPanelSaved.artifact.attempted_strategy, null, 'an inapplicable panel does not invoke a reader')
    assert.equal(missingPanelRun.figures[0].attempts[0].strategy, null)

    const automatic = await cli('run', '--article-id', article.id, '--candidate-id', figure.id, '--reader', 'auto')
    await eventually(() => cli('status', '--run-id', automatic.run_id), run => run.figures[0].status === 'done')
    const automaticSaved = await cli('artifact', '--run-id', automatic.run_id)
    assert.equal(automaticSaved.artifact.requested_strategy, 'auto')
    assert.equal(automaticSaved.artifact.attempted_strategy, 'vector-bar')
    assert.deepEqual(automaticSaved.artifact.results, artifact.results)

    // A one-page excerpt lacks the experiment setup on page 7. Auto stops at binding prerequisites before reading.
    const excerptPath = join(store, 'D001-page8.pdf')
    await execute(python, ['-c', 'import pypdfium2 as p,sys; source=p.PdfDocument(sys.argv[1]); excerpt=p.PdfDocument.new(); excerpt.import_pages(source,[7]); excerpt.save(sys.argv[2])', pdf, excerptPath])
    const excerptResponse = await fetch(`${origin}/api/articles`, { method: 'POST', body: await readFile(excerptPath),
      headers: { 'Content-Type': 'application/pdf', 'X-PDF-Name': 'D001-page8.pdf' } })
    assert.equal(excerptResponse.status, 201)
    const excerpt = await excerptResponse.json()
    const sensitivity = excerpt.candidates.find((item: { kind: string; label: string }) => item.kind === 'figure' && item.label === '3')
    assert.ok(sensitivity)
    const noRead = await cli('run', '--article-id', excerpt.id, '--candidate-id', sensitivity.id, '--reader', 'auto')
    const noReadRun = await eventually(() => cli('status', '--run-id', noRead.run_id), run => run.figures[0].status === 'done')
    const noReadSaved = await cli('artifact', '--run-id', noRead.run_id)
    assert.equal(noReadSaved.artifact.attempted_strategy, null)
    assert.equal(noReadRun.figures[0].attempts[0].strategy, null, 'history never substitutes a planner name for a reader that was not invoked')
    const output = join(store, 'document-comparison')
    await execute(join(root, 'rag'), [
      'compare', article.id, '--figure', '5', '--figure-reader', 'vector-bar',
      '--figure-reader', 'vector-line', '--base-url', origin, '--out', output,
    ], { cwd: root, env, timeout: 30_000 })
    const comparison = JSON.parse(await readFile(join(output, 'comparison.json'), 'utf8'))
    assert.equal(comparison.attempts.length, 2)
    for (const [index, attempt] of comparison.attempts.entries()) {
      const folder = join(output, attempt.directory)
      const run = JSON.parse(await readFile(join(folder, 'run.json'), 'utf8'))
      const result = JSON.parse(await readFile(join(folder, `figure-${run.figures[0].id}.json`), 'utf8'))
      assert.equal(run.pdfSha256, completed.pdfSha256)
      assert.equal(result.status, index === 0 ? 'succeeded' : 'not_applicable')
      assert.equal(result.observations.length, index === 0 ? 12 : 0)
    }
    const publications = await readdir(env.RAG_PUBLICATION_STORE).catch(() => [])
    assert.deepEqual(publications, [], 'experiments never publish into the library')
    if (process.env.RAG_FIGURE_TEST_OUTPUT) {
      const evidence = process.env.RAG_FIGURE_TEST_OUTPUT
      await mkdir(evidence, { recursive: true })
      await writeFile(join(evidence, 'explicit-figure5.json'), JSON.stringify({ queue, accepted, completed, saved,
        unsuitable: { run: unsuitableRun, saved: unsuitableSaved }, automatic: automaticSaved,
        missingPanel: { run: missingPanelRun, saved: missingPanelSaved },
        noRead: { run: noReadRun, saved: noReadSaved } }, null, 2))
    }
  } finally {
    if (worker) await stop(worker)
    await stop(hono)
    await channel.deleteQueue(queue)
    await connection.close()
    await rm(store, { recursive: true, force: true })
  }
})

test('the public CLI rejects invalid explicit selections before RabbitMQ', { timeout: 90_000 }, async t => {
  const { RAG_PYTHON: python, D001_PDF: pdf, RAG_AMQP_URL: broker } = process.env
  if (!python || !pdf || !broker) return t.skip('Set RAG_PYTHON, D001_PDF and RAG_AMQP_URL for queue acceptance')
  const store = await mkdtemp(join(tmpdir(), 'rag-figure-invalid-'))
  const queue = `rag.figure.ticket01.${randomUUID()}`
  const env = { ...process.env, RAG_PYTHON: python, PORT: '0', RAG_HOST: '127.0.0.1',
    RAG_AMQP_QUEUE: queue, RAG_ARTICLE_STORE: join(store, 'articles'), RAG_PUBLICATION_STORE: join(store, 'publications') }
  const connection = await amqp.connect(broker)
  const channel = await connection.createChannel()
  await channel.assertQueue(queue, { durable: true })
  const hono = spawn(process.execPath, ['--import', 'tsx', 'src/server/index.ts'], {
    cwd: web, env, stdio: ['ignore', 'pipe', 'pipe'],
  })
  try {
    const origin = await serverAddress(hono)
    const uploaded = await fetch(`${origin}/api/articles`, { method: 'POST', body: await readFile(pdf),
      headers: { 'Content-Type': 'application/pdf', 'X-PDF-Name': 'D001.pdf' } })
    assert.equal(uploaded.status, 201)
    const article = await uploaded.json()
    const figure = article.candidates.find((item: { kind: string; label: string }) => item.kind === 'figure' && item.label === '5')
    assert.ok(figure)
    for (const [reader, parameters] of [
      ['no-such-reader', '{}'], ['vector-bar', '{"typo":true}'],
      ['vector-scatter', '{"marker_size":"large"}'], ['vector-scatter', '{}'],
      ['vector-scatter', '{"marker_size":0}'], ['vector-bar', '[]'],
      ['qwen-vision', '{"model":"caller-model"}'], ['qwen-vision', '{"ollama_url":"http://127.0.0.1"}'],
    ]) {
      await assert.rejects(execute(process.execPath, [join(web, 'scripts/figure-runs.ts'), 'run',
        '--base-url', origin, '--article-id', article.id, '--candidate-id', figure.id,
        '--reader', reader, '--parameters', parameters,
      ], { cwd: root, env, timeout: 30_000 }), (error: { stderr?: string }) => {
        assert.match(error.stderr ?? '', /HTTP 400/)
        return true
      })
    }
    // Let the delivery timer run. No worker is connected, so anything queued remains observable.
    await new Promise(resolve => setTimeout(resolve, 1_000))
    const queued = await channel.checkQueue(queue)
    assert.equal(queued.messageCount, 0)
  } finally {
    await stop(hono)
    await channel.deleteQueue(queue)
    await connection.close()
    await rm(store, { recursive: true, force: true })
  }
})

test('two RabbitMQ workers share one Qwen slot and expose only reviewable observations',
  { timeout: 120_000 }, async t => {
  const { RAG_PYTHON: python, D001_PDF: pdf, RAG_AMQP_URL: broker } = process.env
  if (!python || !pdf || !broker) return t.skip('Set RAG_PYTHON, D001_PDF and RAG_AMQP_URL for queue acceptance')
  const store = await mkdtemp(join(tmpdir(), 'rag-qwen-figure-'))
  const queue = `rag.figure.ticket02.${randomUUID()}`
  const env = { ...process.env, RAG_PYTHON: python, PORT: '0', RAG_HOST: '127.0.0.1',
    RAG_AMQP_QUEUE: queue, RAG_ARTICLE_STORE: join(store, 'articles'), RAG_PUBLICATION_STORE: join(store, 'publications') }
  const proposal = { series: 'STG-Paths (Ours)', category: '2Wiki', metric: 'EM', value: '58.8', approximate: false }
  const privateMarker = 'fixture: private Ollama reasoning'
  const requests: Array<{ model: string; messages: Array<{ role: string; content: string; images?: string[] }> }> = []
  let mode: 'read' | 'unavailable' | 'stall' = 'read'
  let releaseFirstRequest = () => {}
  const firstRequest = new Promise<void>(resolve => { releaseFirstRequest = resolve })
  let active = 0
  let peak = 0
  const ollama = createServer(async (request, response) => {
    const chunks = []
    for await (const chunk of request) chunks.push(chunk)
    requests.push(JSON.parse(Buffer.concat(chunks).toString()))
    if (mode === 'stall') return
    if (mode === 'unavailable') {
      response.writeHead(503).end(privateMarker)
      return
    }
    active += 1
    peak = Math.max(peak, active)
    if (requests.length === 1) await firstRequest
    response.writeHead(200, { 'Content-Type': 'application/json' }).end(JSON.stringify({
      model: 'qwen-fixture', done: true, done_reason: 'stop', prompt_eval_count: 120, eval_count: 32,
      message: { content: JSON.stringify({ observations: [{ ...proposal, box: [0, 0, 1, 1] }] }), thinking: privateMarker },
    }))
    active -= 1
  })
  await new Promise<void>(resolve => ollama.listen(0, '127.0.0.1', resolve))
  t.after(async () => {
    releaseFirstRequest()
    ollama.closeAllConnections()
    await new Promise<void>((resolve, reject) => ollama.close(error => error ? reject(error) : resolve()))
  })
  const address = ollama.address()
  assert.ok(address && typeof address === 'object')
  const connection = await amqp.connect(broker)
  const channel = await connection.createChannel()
  await channel.assertQueue(queue, { durable: true })
  const hono = spawn(process.execPath, ['--import', 'tsx', 'src/server/index.ts'], {
    cwd: web, env, stdio: ['ignore', 'pipe', 'pipe'],
  })
  const workers: ChildProcess[] = []
  const startWorker = () => workers.push(spawn(python, ['-m', 'rag_mvp.article_worker'], {
    cwd: root, env: { ...env, RAG_QWEN_PORT: String(address.port), RAG_QWEN_MODEL: 'operator-model',
      RAG_QWEN_TIMEOUT_SECONDS: '8' }, stdio: ['ignore', 'pipe', 'pipe'],
  }))
  try {
    const origin = await serverAddress(hono)
    const cli = async (...args: string[]) => JSON.parse((await execute(process.execPath,
      [join(web, 'scripts/figure-runs.ts'), ...args, '--base-url', origin], { cwd: root, env, timeout: 30_000 })).stdout)
    const catalog = await cli('catalog')
    const qwen = catalog.readers.find((reader: { id: string }) => reader.id === 'qwen-vision')
    assert.ok(qwen)
    assert.equal(qwen.parameters_schema.additionalProperties, false)
    assert.deepEqual(Object.keys(qwen.parameters_schema.properties), ['panel'])
    const pdfBytes = await readFile(pdf)
    const uploaded = await fetch(`${origin}/api/articles`, { method: 'POST', body: pdfBytes,
      headers: { 'Content-Type': 'application/pdf', 'X-PDF-Name': 'D001.pdf' } })
    assert.equal(uploaded.status, 201)
    const article = await uploaded.json()
    const figure = article.candidates.find((item: { kind: string; label: string }) => item.kind === 'figure' && item.label === '5')
    assert.ok(figure)
    const run = (reader = 'qwen-vision', parameters = '{}') =>
      cli('run', '--article-id', article.id, '--candidate-id', figure.id, '--reader', reader, '--parameters', parameters)
    const status = (id: string) => cli('status', '--run-id', id)
    const done = (id: string) => eventually(() => status(id), value => value.figures[0].status === 'done')
    const artifact = (id: string) => cli('artifact', '--run-id', id)

    startWorker()
    await eventually(() => channel.checkQueue(queue), state => state.consumerCount === 1)
    const first = (await run()).run_id
    await eventually(async () => requests.length, count => count === 1)
    startWorker()
    await eventually(() => channel.checkQueue(queue), state => state.consumerCount === 2)
    const second = (await run()).run_id
    await eventually(() => status(second), value => value.figures[0].status === 'running')
    await new Promise(resolve => setTimeout(resolve, 500))
    assert.equal(requests.length, 1, 'second worker is processing a different attempt while waiting for the shared slot')
    releaseFirstRequest()
    const finished = await Promise.all([done(first), done(second)])
    assert.equal(requests.length, 2)
    assert.equal(peak, 1)
    for (const request of requests) {
      assert.equal(request.model, 'operator-model')
      const [policy, chart] = request.messages
      assert.equal(policy.role, 'system')
      assert.equal(chart.role, 'user')
      assert.equal(chart.images?.length, 1)
      assert.equal(Buffer.from(chart.images![0], 'base64').subarray(1, 4).toString(), 'PNG')
    }
    const qwenRuns = []
    for (const [index, id] of [first, second].entries()) {
      const saved = await artifact(id)
      const figureRun = finished[index].figures[0]
      assert.deepEqual(figureRun.attempts.map((attempt: { strategy: string }) => attempt.strategy), ['qwen-vision'])
      assert.equal(saved.artifact.requested_strategy, 'qwen-vision')
      assert.equal(saved.artifact.attempted_strategy, 'qwen-vision')
      assert.equal(saved.artifact.status, 'succeeded')
      assert.equal(saved.artifact.source.sha256, createHash('sha256').update(pdfBytes).digest('hex'))
      const [observation] = saved.artifact.observations
      for (const [key, value] of Object.entries(proposal)) assert.equal(observation[key], value)
      assert.equal(observation.status, 'unreviewed')
      assert.equal(observation.source.precision, 'figure')
      assert.deepEqual(observation.source.box, figureRun.plan.target.region.box)
      assert.deepEqual(saved.artifact.binding.results, [])
      assert.equal(saved.artifact.binding.remarks[0].kind, 'unreviewed-figure-proposal')
      assert.equal(saved.artifact.model_attempt.reported_model, 'qwen-fixture')
      assert.equal(saved.artifact.model_attempt.prompt.revision, 'qwen-vision-v3')
      assert.match(saved.artifact.model_attempt.prompt.sha256, /^[0-9a-f]{64}$/u)
      assert.deepEqual(saved.artifact.model_attempt.usage, { input_tokens: 120, output_tokens: 32 })
      assert.ok(!JSON.stringify(saved).includes(privateMarker))
      const stageFolder = join(env.RAG_ARTICLE_STORE, 'runs', id, figureRun.id, 'attempts')
      const [attemptId] = await readdir(stageFolder)
      const protectedResponse = join(stageFolder, attemptId, 'ollama-response.txt')
      assert.match(await readFile(protectedResponse, 'utf8'), /private Ollama reasoning/)
      assert.equal((await stat(protectedResponse)).mode & 0o777, 0o600)
      const privateUrl = new URL('ollama-response.txt', saved.artifact_url)
      assert.equal((await fetch(privateUrl)).status, 404)
      qwenRuns.push({ run: finished[index], artifact: saved.artifact })
    }

    mode = 'stall'
    const slow = (await run()).run_id
    await eventually(async () => requests.length, count => count === 3)
    const bars = (await run('vector-bar')).run_id
    await done(bars)
    assert.equal((await artifact(bars)).artifact.binding.results.length, 12)
    await done(slow)
    const timedOut = (await artifact(slow)).artifact
    assert.equal(timedOut.status, 'timed_out')
    assert.equal(timedOut.attempted_strategy, 'qwen-vision')
    assert.match(timedOut.reason, /таймаут/)
    assert.deepEqual(timedOut.binding.results, [])

    mode = 'unavailable'
    const failure = (await run()).run_id
    await done(failure)
    const unavailable = (await artifact(failure)).artifact
    assert.equal(unavailable.status, 'unavailable')
    assert.equal(unavailable.attempted_strategy, 'qwen-vision')
    assert.match(unavailable.reason, /HTTP 503/)
    assert.deepEqual(unavailable.observations, [])

    const missingPanel = (await run('qwen-vision', '{"panel":"99"}')).run_id
    await done(missingPanel)
    const notApplicable = (await artifact(missingPanel)).artifact
    assert.equal(notApplicable.status, 'not_applicable')
    assert.equal(notApplicable.requested_strategy, 'qwen-vision')
    assert.equal(notApplicable.attempted_strategy, null)
    assert.match(notApplicable.reason, /no frozen vector frame/)
    assert.equal(requests.length, 4, 'the missing panel never reaches Ollama')
    assert.deepEqual(await readdir(env.RAG_PUBLICATION_STORE).catch(() => []), [])
    if (process.env.RAG_FIGURE_TEST_OUTPUT) {
      const evidence = process.env.RAG_FIGURE_TEST_OUTPUT
      await mkdir(evidence, { recursive: true })
      await writeFile(join(evidence, 'qwen-contract.json'), JSON.stringify({ fixture: 'local Ollama stub',
        workers: 2, peak, qwen: qwenRuns, vectorRun: bars, timedOut: { run_id: slow, artifact: timedOut },
        unavailable: { run_id: failure, artifact: unavailable } }, null, 2))
    }
  } finally {
    releaseFirstRequest()
    await Promise.all(workers.map(stop))
    await stop(hono)
    await channel.deleteQueue(queue)
    await connection.close()
    await rm(store, { recursive: true, force: true })
  }
})
