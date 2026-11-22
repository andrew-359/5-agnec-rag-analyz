import { randomUUID } from 'node:crypto'
import { existsSync } from 'node:fs'
import { homedir } from 'node:os'
import { readFile, rename, rm, writeFile } from 'node:fs/promises'
import path from 'node:path'
import { Hono } from 'hono'
import { z } from 'zod'
import { bodyLimit } from 'hono/body-limit'
import { figureDecisionSchema, plotRunSchema } from '../../shared/article-plot.ts'
import { figureStatus, plotArguments, rasterPilot } from '../article-plot.ts'
import { articleOverviewSchema, overviewTranslationSchema, uploadLimit } from '../../shared/article.ts'
import type { Article, ArticleRun, ArticleTableTarget, Candidate, DeferredFigure, ExcludedTableCandidate } from '../../shared/article.ts'
import { strategyCatalogRoute } from '../../shared/strategy-catalog.ts'
import { createArticlePythonRunner } from './article-python.ts'
import { figureReaderCatalog } from './figure-reader-catalog.ts'
import { figureRunRequestSchema, preparedFigureTargetSchema, type CandidateFigurePlan, type RegionTarget } from '../../shared/figure-run.ts'
import { articleReaderCatalog, articleRunRequestSchema, figureSelectionError } from './article-run-selection.ts'
import { openArticleDocuments } from './article-documents.ts'
import { exists, noStore, pdfHeaders, readJson, readOptionalTableSnapshot, readTableArtifact, readTableRun } from './article-files.ts'
import { articleRunRoutes } from './article-run-routes.ts'
import { openArticleRuns } from './article-run-store.ts'
import { startArticleDelivery, type ArticleDelivery } from './article-queue.ts'
import { startArticlePublication } from './article-publication.ts'
import { planRegionReading } from './table-reading-plan.ts'
import type { PublicationStore } from '../publication/publication-store.ts'
import { matchesReviewPdf } from '../review-case.ts'
import type { ReviewConfig } from '../review-assistant.ts'

function extractNumber(candidate: Candidate) {
  const label = candidate.label
  const args = candidate.extract_arguments
  const number = Number(label)
  return candidate.kind === 'table' && Number.isSafeInteger(number) && number > 0 && String(number) === label
    && args?.length === 4 && args[0] === '--page' && args[1] === String(candidate.page) && args[2] === '--table' && args[3] === label
    ? number : null
}

const romanDigits = new Map(Object.entries({ I: 1, V: 5, X: 10, L: 50, C: 100, D: 500, M: 1000 }))

function labelNumber(label: string | null) {
  if (!label) return null
  if (/^[1-9]\d*$/.test(label)) {
    const number = Number(label)
    return Number.isSafeInteger(number) ? number : null
  }
  if (!/^[IVXLCDM]+$/i.test(label)) return null
  const digits = [...label.toUpperCase()].map(digit => romanDigits.get(digit)!)
  return digits.reduce((sum, digit, index) => sum + (digit < (digits[index + 1] ?? 0) ? -digit : digit), 0)
}

function tableNumbers(candidates: Candidate[], pageCount: number) {
  const numbers = new Map<string, number>()
  const usedByPage = new Map<number, Set<number>>()
  const regions = []
  for (const candidate of candidates) {
    if (candidate.kind !== 'table' || candidate.page > pageCount) continue
    if (!usedByPage.has(candidate.page)) usedByPage.set(candidate.page, new Set())
    if (candidate.region) {
      if (candidate.region.bounds_status === 'confirmed') regions.push(candidate)
      continue
    }
    const number = extractNumber(candidate)
    if (number === null) continue
    usedByPage.get(candidate.page)!.add(number)
    numbers.set(candidate.id, number)
  }
  // Preserve named legacy addresses and reserve the preferred numbers before assigning unnamed ones.
  for (const candidate of regions) {
    const preferred = candidate.region?.caption_status === 'confirmed' ? labelNumber(candidate.label) : null
    const used = usedByPage.get(candidate.page)!
    if (preferred === null || preferred < 1 || used.has(preferred)) continue
    used.add(preferred)
    numbers.set(candidate.id, preferred)
  }
  for (const candidate of regions) {
    if (numbers.has(candidate.id)) continue
    const used = usedByPage.get(candidate.page)!
    let number = 1
    while (used.has(number)) number++
    used.add(number)
    numbers.set(candidate.id, number)
  }
  return numbers
}

function tableTarget(candidate: Candidate, table: number, images: NonNullable<Candidate['box']>[]): ArticleTableTarget {
  const address = { candidateId: candidate.id, page: candidate.page, table, region: candidate.region }
  if (candidate.region) return {
    ...address,
    ...planRegionReading(candidate.table_read_features ?? {
      text_layer: candidate.discovery === 'ocr' ? 'ocr-required' : 'usable', header: 'unconfirmed',
    }),
  }
  if (candidate.discovery === 'ocr') return {
    ...address, strategy: 'docling-rapidocr', strategyReason: 'Подпись таблицы найдена через OCR.',
  }
  const target = {
    ...address, strategy: 'docling-tableformer', strategyReason: 'Для подписи без OCR используется TableFormer по умолчанию.',
  }
  if (!candidate.box) return target
  const [left, top, right, bottom] = candidate.box
  const nearRaster = images.some(([x0, y0, x1, y1]) =>
    Math.min(right, x1) - Math.max(left, x0) >= (right - left) / 2
    && y1 - y0 >= 0.05 && top - y1 <= 0.04 && y0 - bottom <= 0.04)
  if (!nearRaster) return target
  return {
    ...target,
    fallback: { strategy: 'docling-rapidocr', reason: 'Карта PDF показывает растровую область рядом с подписью таблицы.' },
  }
}

export function articlesApi(root: string, options: {
  store?: string
  delivery?: ArticleDelivery
  articleContext?: boolean
  publications?: PublicationStore
  review?: ReviewConfig
}) {
  const app = new Hono()
  const store = path.resolve(options.store || path.join(root, 'output/web-articles'))
  const runs = openArticleRuns(store, options.articleContext)
  const { delivery, publications } = options
  const fullArticleContext = options.articleContext !== false
  const publishFullRun = Boolean(publications) && fullArticleContext
  const pythonTask = createArticlePythonRunner(root)
  const readers = figureReaderCatalog(pythonTask)
  const articleReaders = articleReaderCatalog(pythonTask)
  const documents = openArticleDocuments(store)
  const modelSetting = process.env.RAG_OVERVIEW_TRANSLATION_MODEL
    ?? path.join(homedir(), '.cache/rag/opus-en-ru-2020-02-11-int8')
  const modelDir = modelSetting ? path.resolve(root, modelSetting) : null
  const translationModel = modelDir && ['model.bin', 'source.spm', 'target.spm']
    .every(file => existsSync(path.join(modelDir, file))) ? modelDir : null
  let translating = false
  let inspecting = false

  if (delivery) {
    startArticleDelivery(runs, delivery)
    startArticlePublication(runs, {
      store, sourcePdf: documents.sourcePdf, publications, pythonTask, shutdown: delivery.shutdown,
    })
  }

  async function article(id: string): Promise<Article | null> {
    const map = await documents.inventory(id)
    if (!map) return null
    const document = await documents.readName(id)
    const tasks = runs.articleTasks(id)
    const decisions = runs.figureDecisions(id)
    const targets = tableNumbers(map.candidates, map.page_count)
    const candidates = await Promise.all(map.candidates.map(async ({ extract_arguments, ...candidate }) => {
      const number = extractNumber({ ...candidate, extract_arguments })
      let resultCount: number | null = null
      const task = tasks.find(task => task.candidateId === candidate.id) ?? null
      if (number && !task) {
        const saved = await readOptionalTableSnapshot(documents.legacyRun(id, candidate.page, number))
        resultCount = saved?.results.length ?? null
      }
      const status = candidate.kind === 'figure'
        ? decisions.get(candidate.id) ?? figureStatus(map.sha256, candidate.page, candidate.label) : undefined
      const target = targets.get(candidate.id)
      const plan = candidate.region && target !== undefined ? tableTarget(candidate, target, []) : undefined
      return { ...candidate, box: candidate.region?.box ?? candidate.box,
        figureStatus: status, extractable: target !== undefined, resultCount, task,
        strategyPlan: plan?.strategyReason ? { strategy: plan.strategy, reason: plan.strategyReason,
          fallback: plan.fallback ?? null } : undefined }
    }))
    return { id, document, sha256: map.sha256, page_count: map.page_count, overview: map.overview,
      overviewTranslationAvailable: Boolean(translationModel), rasterPlot: rasterPilot(map.sha256), warnings: map.warnings, candidates }
  }

  app.get('/', async c => {
    let unavailable = 0
    const articles = []
    for (const id of await documents.list()) {
      try {
        const map = await documents.inventory(id)
        if (!map) continue
        const [document, uploadedAt] = await Promise.all([documents.readName(id), documents.uploadedAt(id)])
        const status = await overviewStatus(id, map.sha256)
        articles.push({
          id, document, title: map.overview?.title?.text ?? document,
          pageCount: map.page_count, uploadedAt,
          status: status?.label ?? 'Готова к разбору', runId: status?.runId ?? null, resultRunId: status?.resultRunId ?? null,
          resultUrl: status?.resultUrl ?? null,
        })
      } catch { unavailable++ }
    }
    articles.sort((a, b) => b.uploadedAt - a.uploadedAt || a.id.localeCompare(b.id))
    return c.json({ articles, unavailable }, 200, noStore)
  })

  app.post('/', bodyLimit({
    maxSize: uploadLimit,
    onError: c => c.json({ error: 'PDF должен быть не больше 64 МБ.' }, 413),
  }), async c => {
    if (c.req.header('Content-Type') !== 'application/pdf') return c.json({ error: 'Выберите файл PDF.' }, 415)
    if (inspecting) return c.json({ error: 'Дождитесь завершения текущей загрузки и повторите.' }, 409)
    const id = randomUUID()
    inspecting = true
    try {
      const pdf = Buffer.from(await c.req.arrayBuffer())
      if (!pdf.subarray(0, 1024).includes('%PDF-')) {
        return c.json({ error: 'Выберите файл PDF.' }, 415)
      }
      await documents.save(id, pdf, c.req.header('X-PDF-Name'), (input, output) =>
        pythonTask(['-m', 'rag_mvp', 'inspect', input, '--out', output], 600_000))
      return c.json(await article(id), 201)
    } catch (error) {
      await documents.remove(id)
      return c.json({ error: (error as Error).message }, 422)
    } finally {
      inspecting = false
    }
  })

  app.get('/tasks', c => c.json(runs.recentTasks(), 200, noStore))

  app.get('/:id', async c => {
    const data = await article(c.req.param('id'))
    return data ? c.json(data, 200, noStore) : c.json({ error: 'Карта статьи не найдена.' }, 404)
  })

  async function loadOverview(id: string, map: NonNullable<Awaited<ReturnType<typeof documents.inventory>>>) {
    if (map.overview) return map.overview
    const cache = documents.overviewFile(id)
    if (await exists(cache)) return articleOverviewSchema.parse(await readJson(cache))
    const inventoryFile = await documents.inventoryFile(id)
    if (!inventoryFile) return null
    const stdout = await pythonTask(['-m', 'rag_mvp.article_overview', documents.sourcePdf(id), '--inventory', inventoryFile], 60_000)
    const overview = articleOverviewSchema.parse(JSON.parse(stdout.toString('utf8')))
    const temp = `${cache}.${randomUUID()}.tmp`
    try {
      await writeFile(temp, JSON.stringify(overview))
      await rename(temp, cache)
    } finally {
      await rm(temp, { force: true })
    }
    return overview
  }

  app.get('/:id/overview', async c => {
    const id = c.req.param('id')
    const map = await documents.inventory(id)
    if (!map) return c.json({ error: 'Карта статьи не найдена.' }, 404)
    try {
      const overview = await loadOverview(id, map)
      return overview ? c.json(overview, 200, noStore) : c.json({ error: 'Карта статьи не найдена.' }, 404)
    } catch {
      return c.json({ error: 'Не удалось прочитать обзор статьи.' }, 422)
    }
  })

  app.post('/:id/overview/translation', async c => {
    if (!translationModel) return c.json({ error: 'Перевод обзора отключён.' }, 503, noStore)
    const id = c.req.param('id')
    const map = await documents.inventory(id)
    if (!map) return c.json({ error: 'Карта статьи не найдена.' }, 404, noStore)
    if (translating) return c.json({ error: 'Переводчик занят. Попробуйте ещё раз.' }, 429, noStore)
    translating = true
    try {
      const overview = await loadOverview(id, map)
      if (!overview) return c.json({ error: 'Карта статьи не найдена.' }, 404, noStore)
      const excerpts = { abstract: overview.abstract?.text ?? null, conclusion: overview.conclusion?.text ?? null }
      if (!overview.has_text || (!excerpts.abstract && !excerpts.conclusion)) return c.json(excerpts, 200, noStore)
      const stdout = await pythonTask(
        ['-m', 'rag_mvp.overview_translation', translationModel], 60_000, false, JSON.stringify(excerpts),
      )
      return c.json(overviewTranslationSchema.parse(JSON.parse(stdout.toString('utf8'))), 200, noStore)
    } catch {
      return c.json({ error: 'Не удалось перевести обзор.' }, 502, noStore)
    } finally {
      translating = false
    }
  })

  async function overviewStatus(articleId: string, pdfSha256?: string) {
    const id = runs.latestFullRunId(articleId)
    const fullRun = id ? runs.get(id) : null
    const hash = pdfSha256 ?? fullRun?.pdfSha256 ?? (await documents.inventory(articleId))?.sha256
    const latest = hash ? (await publications?.latest('publication-' + hash))?.version : undefined
    const publishedRun = latest ? runs.get(latest.runId) : null
    const publishedHere = publishedRun?.articleId === articleId ? publishedRun : null
    const run = fullRun ?? publishedHere
    if (!run) return null
    const stages = [run.articleText, ...run.stages, ...run.figures].filter(stage => stage !== null)
    const publication = publishedHere?.publication ?? run.publication ?? null
    const label = run.publicationStatus === 'done' ? 'Результаты опубликованы'
      : run.publicationStatus === 'empty' ? 'Разбор завершён · нет подтверждённых результатов'
      : run.publicationStatus === 'failed' ? 'Выпуск требует проверки'
      : stages.some(stage => stage.status === 'running') || run.publicationStatus === 'running' ? 'Идёт разбор'
      : stages.some(stage => stage.status === 'queued') || run.publicationStatus === 'queued' ? 'В очереди'
      : stages.some(stage => stage.status === 'failed') ? 'Разбор завершён с ошибками'
      : 'Разбор завершён'
    return { runId: run.id, label, resultRunId: publication?.runId ?? null, resultUrl: publication?.url ?? null, publication }
  }

  app.get('/:id/overview-status', async c => c.json(await overviewStatus(c.req.param('id')), 200, noStore))

  app.get('/:id/source.pdf', async c => {
    const id = c.req.param('id')
    if (!(await documents.hasSourcePdf(id))) return c.notFound()
    return c.body(await readFile(documents.sourcePdf(id)), 200, pdfHeaders)
  })

  app.put('/:id/plots/:target/decision', bodyLimit({ maxSize: 4096 }), async c => {
    const { id, target } = c.req.param()
    const map = await documents.inventory(id)
    const candidate = map?.candidates.find(item => item.kind === 'figure' && item.id === target)
    if (!candidate) return c.json({ error: 'Рисунок не найден.' }, 404)

    const decision = figureDecisionSchema.safeParse(await c.req.json().catch(() => null))
    if (!decision.success) return c.json({ error: 'Выберите тип рисунка.' }, 400)
    runs.setFigureDecision(id, target, decision.data)
    return c.json({ figureStatus: decision.data }, 200, noStore)
  })

  app.post('/:id/plots/:target', async c => {
    const { id, target } = c.req.param()
    const map = await documents.inventory(id)
    if (!map) return c.json({ error: 'Карта статьи не найдена.' }, 404)
    const candidate = map.candidates.find(item => item.kind === 'figure' && item.id === target)
    const address = target === 'raster-pilot' ? rasterPilot(map.sha256)
      : candidate?.label ? { page: candidate.page, figure: candidate.label } : undefined
    if (!address || address.page > map.page_count) return c.json({ error: 'Рисунок не найден.' }, 404)
    if (candidate) {
      const status = runs.figureDecisions(id).get(target) ?? figureStatus(map.sha256, address.page, address.figure)
      if (status !== 'data') return c.json({ error: 'Сначала подтвердите, что в рисунке есть данные для разбора.' }, 409)
    }

    try {
      const stdout = await pythonTask(plotArguments(map.sha256, address.page, address.figure, documents.sourcePdf(id)), 60_000, true)
      const run = plotRunSchema.parse(JSON.parse(stdout.toString('utf8')))
      if (run.sha256 !== map.sha256 || run.source.page !== address.page) {
        return c.json({ error: 'Прогон относится к другой версии PDF или странице.' }, 409)
      }
      if (candidate?.box && !run.points.length) {
        for (const issue of run.issues) {
          if (issue.source.box?.every((value, index) => value === [0, 0, 1, 1][index])) {
            issue.source.box = candidate.box
          }
        }
      }
      return c.json(run, 200, noStore)
    } catch (error) {
      return c.json({ error: error instanceof Error ? error.message : 'Не удалось разобрать график.' }, 422)
    }
  })

  async function prepareFigure(id: string, inventory: string, candidateId: string) {
    const output = await pythonTask(['-m', 'rag_mvp.figure_readers', '--prepare-target', documents.sourcePdf(id),
      '--inventory', inventory, '--candidate', candidateId], 30_000)
    return preparedFigureTargetSchema.parse(JSON.parse(output.toString('utf8')))
  }

  app.post('/:id/runs', bodyLimit({ maxSize: 64 * 1024 }), async c => {
    const id = c.req.param('id')
    const map = await documents.inventory(id)
    if (!map) return c.json({ error: 'Карта статьи не найдена.' }, 404)

    const body = await c.req.text()
    let payload: unknown = {}
    try { if (body) payload = JSON.parse(body) }
    catch { return c.json({ error: 'Тело прогона должно быть JSON.' }, 400) }

    const request = articleRunRequestSchema.safeParse(payload)
    if (!request.success) return c.json({ error: 'Некорректный выбор ридеров.', details: request.error.issues }, 400)
    const selected = request.data.readers
    const tableReader = selected.table === 'auto' ? undefined : selected.table
    const textReader = selected.text === 'auto' ? undefined : selected.text
    const figureReader = selected.figure === 'auto' ? undefined : selected.figure
    const selection: CandidateFigurePlan['selection'] = figureReader
      ? { mode: 'explicit', strategy_id: figureReader, parameters: request.data.figure_parameters }
      : { mode: 'auto' }

    if (!figureReader && Object.keys(request.data.figure_parameters).length) {
      return c.json({ error: 'Параметры требуют явного ридера рисунков.' }, 400)
    }
    if (!fullArticleContext && (textReader || figureReader)) {
      return c.json({ error: 'Текст и рисунки отключены на этом сервере.' }, 400)
    }

    let tableCapabilities
    try {
      if (tableReader || textReader) {
        const catalog = await articleReaders()
        if (textReader && !catalog.text.some(reader => reader.id === textReader)) {
          return c.json({ error: `Неизвестный ридер текста: ${textReader}` }, 400)
        }
        if (tableReader) {
          tableCapabilities = catalog.table.find(reader => reader.id === tableReader)
          if (!tableCapabilities) return c.json({ error: `Неизвестный ридер таблиц: ${tableReader}` }, 400)
          if (tableCapabilities.requires_crop) {
            return c.json({ error: `Ридер ${tableReader} требует ручного crop. Используйте диагностический Python CLI.` }, 400)
          }
        }
      }
      const error = await figureSelectionError(readers, selection)
      if (error) return c.json(error, 400)
    } catch (error) { return c.json({ error: (error as Error).message }, 503) }

    const stages: ArticleTableTarget[] = []
    const seen = new Set<string>()
    const excludedCandidates: ExcludedTableCandidate[] = []
    const deferredFigures: DeferredFigure[] = []
    const images = new Map(map.pages.map(page => [page.page, page.image_boxes]))
    const targets = tableNumbers(map.candidates, map.page_count)
    for (const candidate of map.candidates) {
      const { id: candidateId, page, label, text } = candidate
      if (candidate.kind === 'figure' && (!fullArticleContext || page > map.page_count)) {
        deferredFigures.push({ candidateId, page, label, text, reason: 'figure-reading-deferred' })
      }
      if (candidate.kind !== 'table') continue
      const table = targets.get(candidateId)
      const address = `${page}/${table}`
      if (table === undefined || seen.has(address)) {
        const reason = candidate.region?.bounds_status === 'needs_review' ? 'table-bounds-review'
          : table === undefined ? 'unsupported-address' : 'duplicate-address'
        excludedCandidates.push({ candidateId, page, label, text, reason, region: candidate.region })
        continue
      }
      seen.add(address)
      if (/^Table\s+(?:\d+|[IVXLCDM]+)\s*[:.\-–—]\s*Prompt Templates?\b/i.test(text)) {
        excludedCandidates.push({ candidateId, page, label, text, reason: 'prompt-template' })
        continue
      }
      if (tableCapabilities && !tableCapabilities.accepts_region && candidate.region) {
        return c.json({ error: `Ридер ${tableReader} не поддерживает адрес области таблицы ${candidateId}.` }, 422)
      }
      stages.push(tableReader
        ? { candidateId, page, table, region: candidate.region, strategy: tableReader, strategyReason: 'Ридер выбран явно.' }
        : tableTarget(candidate, table, images.get(page) ?? []))
    }

    const figures = fullArticleContext ? map.candidates
      .filter(candidate => candidate.kind === 'figure' && candidate.page <= map.page_count)
      .map(({ id: candidateId, page, label }): { candidateId: string; page: number; label: string | null; plan?: CandidateFigurePlan } =>
        ({ candidateId, page, label })) : []
    if (!delivery && (stages.length || fullArticleContext)) return c.json({ error: 'Для запуска разбора настройте RAG_AMQP_URL.' }, 503)
    if (figureReader) {
      if (!figures.length) return c.json({ error: 'В карте нет доступных рисунков.' }, 422)
      const inventory = await documents.inventoryFile(id)
      if (!inventory) return c.json({ error: 'Карта статьи не найдена.' }, 404)
      try {
        for (const figure of figures) {
          figure.plan = { target: await prepareFigure(id, inventory, figure.candidateId), selection }
        }
      } catch (error) { return c.json({ error: (error as Error).message }, 422) }
    }
    const document = await documents.readName(id)
    const scanned = fullArticleContext && !textReader && map.pages.length === map.page_count
      && map.pages.every(({ page, text_chars, image_boxes }, index) => page === index + 1 && text_chars === 0
        && image_boxes.some(([left, top, right, bottom]) => (right - left) * (bottom - top) >= 0.8))
    const run = runs.create({ articleId: id, document, pdfSha256: map.sha256 }, stages, {
      excludedCandidates, deferredFigures, discoveryWarnings: map.warnings, discoveryLimitations: map.limitations,
    }, {
      figures, publish: request.data.publish ?? publishFullRun, reconcile: Boolean(delivery),
      withText: fullArticleContext,
      textStrategy: scanned ? 'pdfplumber-ocr' : textReader,
      textStrategyReason: scanned ? 'На каждой странице нет нативного текста, растровое изображение занимает не менее 80% страницы.' : undefined,
    })
    const headers = { ...noStore, Location: `/api/article-runs/${run.id}` }
    return c.json({ id: run.id, status: stages.length || fullArticleContext ? 'queued' : 'done' }, 202, headers)
  })

  app.post('/:id/figure-runs', bodyLimit({ maxSize: 64 * 1024 }), async c => {
    const request = figureRunRequestSchema.safeParse(await c.req.json().catch(() => null))
    if (!request.success) return c.json({ error: 'Некорректный запрос прогона рисунка.', details: request.error.issues }, 400)
    const { target, selection } = request.data
    try {
      const error = await figureSelectionError(readers, selection)
      if (error) return c.json(error, 400)
    } catch (error) { return c.json({ error: (error as Error).message }, 503) }
    const id = c.req.param('id')
    const map = await documents.inventory(id)
    const candidate = map?.candidates.find(item => item.id === target.candidate_id && item.kind === 'figure')
    if (!map || !candidate || candidate.page > map.page_count) return c.json({ error: 'Рисунок не найден в карте.' }, 404)
    if (!delivery) return c.json({ error: 'Для запуска разбора настройте RAG_AMQP_URL.' }, 503)
    const inventory = await documents.inventoryFile(id)
    if (!inventory) return c.json({ error: 'Карта статьи не найдена.' }, 404)
    let preparedTarget
    try {
      preparedTarget = await prepareFigure(id, inventory, candidate.id)
    } catch (error) { return c.json({ error: (error as Error).message }, 422) }
    const plan = { target: preparedTarget, selection }
    const run = runs.create({ articleId: id, document: await documents.readName(id), pdfSha256: map.sha256 }, [], null,
      { figures: [{ candidateId: candidate.id, page: candidate.page, label: candidate.label, plan }], withText: true, reconcile: true })
    const location = `/api/article-runs/${run.id}`
    return c.json({ run_id: run.id, id: run.id, status: 'queued', status_url: location }, 202, { ...noStore, Location: location })
  })

  app.post('/:id/tables/:candidateId', async c => {
    const { id, candidateId } = c.req.param()
    const map = await documents.inventory(id)
    const candidate = map?.candidates.find(item => item.id === candidateId)
    if (!map || !candidate) return c.json({ error: 'Таблица не найдена в карте.' }, 404)
    const number = tableNumbers(map.candidates, map.page_count).get(candidateId)
    if (number === undefined) return c.json({ error: candidate.region?.bounds_status === 'needs_review'
      ? 'Границы таблицы требуют проверки.' : 'Формат адреса этой таблицы пока не поддерживается.' }, 422)
    if (!delivery) return c.json({ error: 'Для запуска разбора настройте RAG_AMQP_URL.' }, 503)

    const document = await documents.readName(id)
    const images = map.pages.find(page => page.page === candidate.page)?.image_boxes ?? []
    const run = runs.create({ articleId: id, document, pdfSha256: map.sha256 }, [tableTarget(candidate, number, images)])
    const location = `/api/article-runs/${run.id}`
    return c.json({ id: run.id, status: 'queued' }, 202, { ...noStore, Location: location })
  })

  function legacyFolder(id: string, candidate: Candidate | undefined) {
    if (!candidate) return null
    const number = extractNumber(candidate)
    return number ? documents.legacyRun(id, candidate.page, number) : null
  }

  app.get('/:id/tables/:candidateId', async c => {
    const { id, candidateId } = c.req.param()
    const candidate = (await documents.inventory(id))?.candidates.find(item => item.id === candidateId)
    const folder = legacyFolder(id, candidate)
    if (!folder || !(await exists(path.join(folder, 'run.json')))) return c.json({ error: 'Результат не найден.' }, 404)
    const document = await documents.readName(id)
    return c.json(await readTableRun(document, folder), 200, noStore)
  })

  app.get('/:id/tables/:candidateId/:file', async c => {
    const { id, candidateId, file } = c.req.param()
    const candidate = (await documents.inventory(id))?.candidates.find(item => item.id === candidateId)
    const folder = legacyFolder(id, candidate)
    const data = folder && await readTableArtifact(folder, file)
    return data ? c.body(data.body, 200, data.headers) : c.notFound()
  })

  const api = new Hono()
  api.get(strategyCatalogRoute, async c => {
    try {
      const [article, figure] = await Promise.all([articleReaders(), readers()])
      return c.json({ ...article, figure: figure.readers }, 200, noStore)
    } catch (error) {
      return c.json({ error: (error as Error).message }, 503, noStore)
    }
  })
  api.get('/figure-readers', async c => {
    try { return c.json(await readers(), 200, noStore) }
    catch (error) { return c.json({ error: (error as Error).message }, 503, noStore) }
  })
  async function startRegion(run: ArticleRun, target: RegionTarget) {
    const map = await documents.inventory(run.articleId)
    if (!map) return { error: 'Карта исходного PDF недоступна.', status: 404 as const }
    if (target.pdf_sha256 !== run.pdfSha256 || map.sha256 !== run.pdfSha256) {
      return { error: 'Выбранная область относится к другой версии PDF.', status: 409 as const }
    }
    const pdf = await readFile(documents.sourcePdf(run.articleId)).catch(() => null)
    if (!pdf || !matchesReviewPdf(pdf, run.pdfSha256)) {
      return { error: 'Закреплённый PDF недоступен или изменился.', status: 409 as const }
    }
    if (target.page > map.page_count) return { error: 'Страница отсутствует в PDF.', status: 422 as const }
    const candidates = map.candidates.filter(item => item.kind === 'figure' && item.page === target.page)
    const candidate = candidates.find(item => item.id === target.candidate_id)
    if (target.candidate_id && !candidate) {
      return { error: 'Ориентир отсутствует на выбранной странице.', status: 422 as const }
    }
    return runs.create(run, [], null, {
      figures: [{
        candidateId: candidate?.id ?? 'operator-region', page: target.page, label: candidate?.label ?? null,
        plan: { target, selection: { mode: 'auto' } },
      }],
      withText: true, reconcile: true,
    })
  }

  return api.route('/articles', app).route('/article-runs', articleRunRoutes(store, runs, documents.sourcePdf, options.review,
    delivery && { start: startRegion, apply: run => Boolean(publications) && runs.applyPublication(run.id) }))
}
