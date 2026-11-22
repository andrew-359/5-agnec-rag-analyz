import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { execFile } from 'node:child_process'
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import { createServer } from 'node:http'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { test } from 'node:test'
import { promisify } from 'node:util'

const execute = promisify(execFile)
const cli = new URL('../../rag', import.meta.url).pathname

test('compare uploads a PDF once, saves two independent attempts and resumes without submitting again', async t => {
  const directory = await mkdtemp(join(tmpdir(), 'rag-document-cli-'))
  t.after(() => rm(directory, { recursive: true, force: true }))
  const path = join(directory, 'paper.pdf')
  const out = join(directory, 'out')
  const content = '%PDF-1.4\n'
  const sha256 = createHash('sha256').update(content).digest('hex')
  await writeFile(path, content)
  let uploads = 0
  let reportsRead = 0
  const submitted: Array<{ target: { candidate_id: string }; selection: { strategy_id: string } }> = []
  const server = createServer(async (req, res) => {
    const chunks: Buffer[] = []
    for await (const chunk of req) chunks.push(chunk)
    const body = Buffer.concat(chunks).toString()
    let response: unknown
    if (req.method === 'POST' && req.url === '/api/articles') {
      uploads++
      assert.match(body, /^%PDF-/)
      response = { id: 'paper', sha256, candidates: [{ kind: 'figure', label: '5', id: 'figure-5' }] }
    } else if (req.method === 'POST' && req.url === '/api/articles/paper/figure-runs') {
      submitted.push(JSON.parse(body))
      response = { run_id: String(submitted.length) }
    } else {
      const id = req.url?.split('/')[3]
      const artifact = { strategy: submitted[Number(id) - 1]?.selection.strategy_id, results: [], observations: [], remarks: [] }
      if (req.url?.endsWith('reconciliation.json')) {
        reportsRead++
        response = { schema_version: 1, run_id: id, source: { sha256 },
          artifacts: [{ id: 'f', kind: 'figure', data: artifact }], relationships: [],
          contexts: [{ artifact_id: 'f', data: { schema_version: 1, sha256, target: { page: 8, candidate: 'figure-5' },
            status: 'caption_only', reason: 'Found caption.',
            basis: [{ kind: 'caption', source: { id: 'figure-5', page: 8, text: 'Figure 5', precision: 'figure' } }] } }] }
      } else response = req.url?.endsWith('extracted.json') ? artifact : {
        id, pdfSha256: sha256, figures: [{ id: 'f', status: 'done', page: 8 }], stages: [], articleText: null,
        reconciliationStatus: 'done',
      }
    }
    res.setHeader('Content-Type', 'application/json')
    res.end(JSON.stringify(response))
  })
  await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve))
  t.after(() => server.close())
  const address = server.address()
  assert.ok(address && typeof address !== 'string')
  const args = ['compare', path, '--base-url', `http://127.0.0.1:${address.port}`, '--figure', '5',
    '--figure-reader', 'vector-bar', '--figure-reader', 'vector-line', '--jobs', '1', '--out', out]
  await execute(cli, args)
  const resultPath = (attempt: string) => join(out, attempt, 'reconciliation.json')
  const result = async (attempt: string) => JSON.parse(await readFile(resultPath(attempt), 'utf8'))
  assert.equal((await result('01')).artifacts[0].data.strategy, 'vector-bar')
  assert.equal((await result('02')).artifacts[0].data.strategy, 'vector-line')
  assert.deepEqual((await result('01')).contexts, (await result('02')).contexts)
  await rm(resultPath('02'))
  await execute(cli, args)
  assert.equal(uploads, 1)
  assert.equal(reportsRead, 3)
  assert.equal((await result('02')).run_id, '2')
  assert.deepEqual(submitted.map(x => [x.target.candidate_id, x.selection.strategy_id]), [
    ['figure-5', 'vector-bar'], ['figure-5', 'vector-line'],
  ])
  const report = JSON.parse(await readFile(join(out, 'comparison.json'), 'utf8'))
  assert.deepEqual(report.attempts.map((x: { run_id: string }) => x.run_id), ['1', '2'])
  assert.deepEqual(JSON.parse(await readFile(join(out, '02', 'figure-f.json'), 'utf8')), {
    strategy: 'vector-line', run_id: '2', results: [], observations: [], remarks: [],
  })
})

test('compare holds the next attempt until the pending run has finished', async t => {
  const out = await mkdtemp(join(tmpdir(), 'rag-document-pending-'))
  t.after(() => rm(out, { recursive: true, force: true }))
  const requests: unknown[] = []
  let finished = false
  const server = createServer(async (req, res) => {
    let result: unknown
    if (req.method === 'POST') {
      let body = ''
      for await (const chunk of req) body += chunk
      requests.push(JSON.parse(body))
      result = { id: String(requests.length) }
    } else if (req.url === '/api/articles/paper') {
      result = { id: 'paper', sha256: 'abc', candidates: [] }
    } else {
      result = {
        id: req.url?.split('/')[3], pdfSha256: 'abc', stages: [], figures: [],
        articleText: finished ? null : { id: 't', status: 'running' },
      }
    }
    res.setHeader('Content-Type', 'application/json')
    res.end(JSON.stringify(result))
  })
  await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve))
  t.after(() => server.close())
  const address = server.address()
  assert.ok(address && typeof address !== 'string')
  const args = ['compare', 'paper', '--base-url', `http://127.0.0.1:${address.port}`,
    '--text-reader', 'pdfplumber', '--text-reader', 'pdfplumber-ocr', '--wait-seconds', '0', '--out', out]
  await assert.rejects(execute(cli, args), { code: 1 })
  assert.deepEqual(requests, [{ readers: { text: 'pdfplumber' }, figure_parameters: {}, publish: false }])
  finished = true
  await execute(cli, args)
  assert.deepEqual(requests, [
    { readers: { text: 'pdfplumber' }, figure_parameters: {}, publish: false },
    { readers: { text: 'pdfplumber-ocr' }, figure_parameters: {}, publish: false },
  ])
})
