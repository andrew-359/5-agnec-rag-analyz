import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { after, before, test } from 'node:test'
import { fileURLToPath } from 'node:url'
import { chromium, expect, type Browser, type Page } from '@playwright/test'
import { createServer, type ViteDevServer } from 'vite'

const root = new URL('../../', import.meta.url)
const pdf = await readFile(new URL('../../../qa/java/src/test/resources/synthetic.pdf', import.meta.url))
const article = '/api/articles/paper'
const run = '/api/article-runs/table-run'
const box = [.2, .3, .5, .4]
const digest = 'a'.repeat(64)
const table = { id: 'table-1', kind: 'table', page: 1, text: 'Table 1. Results', box, extractable: true, task: null }
const figure = { id: 'figure-1', kind: 'figure', page: 2, label: '1', text: 'Figure 1. Curve', box, figureStatus: 'data' }
let vite: ViteDevServer
let browser: Browser
let origin: string

before(async () => {
  vite = await createServer({ configFile: fileURLToPath(new URL('vite.config.ts', root)), server: { host: '127.0.0.1' } })
  await vite.listen(0)
  const address = vite.httpServer?.address()
  assert.ok(address && typeof address === 'object')
  origin = 'http://127.0.0.1:' + address.port
  browser = await chromium.launch({ channel: process.env.PLAYWRIGHT_CHANNEL })
})

after(async () => {
  await browser?.close()
  await vite?.close()
})

async function openOverview(page: Page) {
  page.setDefaultTimeout(5000)
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname
    switch (path) {
      case article:
        return route.fulfill({ json: { id: 'paper', document: 'paper.pdf', page_count: 2, sha256: digest, warnings: [],
          candidates: [table, figure, { ...figure, id: 'illustration', text: 'Figure 2. Illustration', figureStatus: 'illustration' }],
          overviewTranslationAvailable: true, overview: { abstract: { text: 'Original abstract', page: 1, box },
            conclusion: { text: 'Original conclusion', page: 2, box }, has_text: true } } })
      case article + '/overview-status':
        return route.fulfill({ json: null })
      case article + '/overview/translation':
        return route.fulfill({ json: { abstract: 'Перевод аннотации', conclusion: 'Перевод выводов' } })
      case article + '/source.pdf':
      case run + '/source.pdf':
        return route.fulfill({ contentType: 'application/pdf', body: pdf })
      default:
        return route.fulfill({ status: 404, json: { error: 'Unexpected request ' + path } })
    }
  })
  await page.goto(origin + '/?lab=1&article=paper')
}

test('overview opens the original source while translated text is visible', async () => {
  const page = await browser.newPage()
  try {
    await openOverview(page)
    await page.getByRole('button', { name: 'Перевести на русский' }).click()
    await expect(page.getByText('Перевод аннотации', { exact: true })).toBeVisible()
    await page.getByRole('button', { name: 'Источник аннотации · стр. 1' }).click()
    await expect(page.getByRole('img', { name: 'Страница 1 · подсвечен источник' })).toBeVisible()
    await page.getByText('Выводы авторов', { exact: true }).click()
    await page.getByRole('button', { name: 'Источник выводов · стр. 2' }).click()
    await expect(page.getByRole('img', { name: 'Страница 2 · подсвечен источник' })).toBeVisible()
    await page.getByRole('button', { name: 'Показать оригинал' }).click()
    await expect(page.getByText('Original conclusion', { exact: true })).toBeVisible()
  } finally { await page.close() }
})

test('one table queues a real run and opens extracted values without a publication', async () => {
  const page = await browser.newPage()
  let starts = 0
  try {
    await openOverview(page)
    await page.route('**' + article + '/tables/table-1', route => {
      assert.equal(route.request().method(), 'POST')
      starts++
      if (starts === 1) return route.fulfill({ status: 202, json: { status: 'queued' } })
      return route.fulfill({ status: 202, json: { id: 'table-run', status: 'queued' } })
    })
    await page.route('**/api/article-runs/table-run', route => route.fulfill({ json: {
      id: 'table-run', articleId: 'paper', pdfSha256: digest, figures: [], articleText: null, coverage: null,
      stages: [{ id: 'reading', candidateId: 'table-1', page: 1, table: 1, status: 'done', attempts: [] }],
    } }))
    await page.route('**' + run + '/stages/reading', route => route.fulfill({ json: { run: {
      document: 'paper.pdf', page: 1, table: 1,
      results: [{ method: 'Alpha', generator: '', dataset: 'QA', metric: 'EM', raw_value: '.422',
        value: '0.422', unit: null, experiment: '', task: '', protocol: null, source: { page: 1, table: 1, box } }],
      remarks: [],
    } } }))
    await page.getByText('Карта статьи', { exact: true }).click()
    const start = page.getByRole('button', { name: 'Разобрать таблицу' })
    await start.click()
    await expect(page.getByRole('alert')).toHaveText('Не удалось запустить прогон таблицы.')
    assert.equal(new URL(page.url()).searchParams.get('run'), null)
    await start.click()
    await expect(page).toHaveURL(/run=table-run&tab=run/)
    await page.getByRole('button', { name: /Alpha.*\.422/ }).click()
    await expect(page.getByRole('img', { name: 'Страница 1 · подсвечен источник' })).toBeVisible()
    assert.equal(starts, 2)
  } finally { await page.close() }
})

test('a graph response stays on the map and opens its own point', async () => {
  const page = await browser.newPage()
  try {
    await openOverview(page)
    await page.route('**' + article + '/plots/figure-1', route => {
      assert.equal(route.request().method(), 'POST')
      return route.fulfill({ json: { sha256: digest, source: { page: 2, box },
        settings: { figure: '1', panel: 'a', series: 'Alpha', x_axis: 'k', y_axis: 'EM', strategy: 'vector-line' },
        points: [{ index: 1, x: 1, y: .75, source: { page: 2, box } }], issues: [] } })
    })
    await page.getByText('Карта статьи', { exact: true }).click()
    await page.getByRole('button', { name: /Рисунки/ }).click()
    await expect(page.getByRole('button', { name: 'Разобрать график', exact: true })).toHaveCount(1)
    await page.getByRole('button', { name: 'Разобрать график', exact: true }).click()
    await page.getByRole('button', { name: /EM.*0.75/ }).click()
    await expect(page.getByRole('img', { name: 'Страница 2 · подсвечен источник' })).toBeVisible()
    assert.equal(new URL(page.url()).searchParams.get('run'), null)
  } finally { await page.close() }
})
