import { Hono } from 'hono'
import type { PublishedPlot } from '../shared/published-plot.ts'
import { openPlotPublicationStore, plotVersionPattern } from './plot-publication-store.ts'

const downloadPath = '/published'

function withPublishedPdf(run: PublishedPlot) {
  return { ...run, pdf: { ...run.pdf, url: `${downloadPath}/${run.version_id}/source.pdf` } }
}

const mediaTypes: Record<string, string> = {
  'index.html': 'text/html; charset=utf-8', 'source.pdf': 'application/pdf',
  'run.json': 'application/json', 'manifest.json': 'application/json',
  'check-report.json': 'application/json', 'PASSPORT.md': 'text/markdown; charset=utf-8',
  'schema.json': 'application/json', 'reviewed-context.json': 'application/json',
}
for (const table of ['publications', 'configurations', 'datasets', 'tasks', 'experiments', 'results', 'issues']) {
  mediaTypes[table + '.csv'] = 'text/csv; charset=utf-8'
}

/** Web and downloads use the exact same immutable publication directory. */
export function plotPublications(root: string) {
  const app = new Hono()
  const store = openPlotPublicationStore(root)

  app.get('/api/published-plots', async c => {
    c.header('Cache-Control', 'no-store')
    try {
      const { versions, unavailable } = await store.list(c.req.query('sha256'))
      return c.json({ versions: versions.map(withPublishedPdf), unavailable })
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === 'ENOENT') return c.json({ versions: [], unavailable: [] })
      return c.json({ error: 'Не удалось открыть опубликованные графики.' }, 500)
    }
  })
  app.get('/api/published-plots/:version', async c => {
    const version = c.req.param('version')
    if (!plotVersionPattern.test(version)) return c.notFound()
    try {
      const detail = await store.detail(version)
      return c.json({ ...detail, run: withPublishedPdf(detail.run) }, 200, { 'Cache-Control': 'no-store' })
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === 'ENOENT') return c.notFound()
      return c.json({ error: 'Не удалось проверить опубликованную версию. Данные повреждены или несовместимы.' }, 409)
    }
  })
  app.get(`${downloadPath}/:version/:file`, async (c) => {
    const { version, file } = c.req.param()
    if (!plotVersionPattern.test(version) || !Object.hasOwn(mediaTypes, file)) return c.notFound()
    try {
      const buffer = await store.file(version, file)
      c.header('Content-Type', mediaTypes[file])
      c.header('X-Content-Type-Options', 'nosniff')
      c.header('Cache-Control', 'no-store')
      if (file.endsWith('.csv')) c.header('Content-Disposition', 'attachment; filename="' + file + '"')
      return c.body(new Uint8Array(buffer))
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === 'ENOENT') return c.notFound()
      return c.json({ error: 'Unable to verify published plot' }, 409)
    }
  })
  return app
}
