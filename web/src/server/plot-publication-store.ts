import { createHash } from 'node:crypto'
import { readFile, readdir } from 'node:fs/promises'
import { resolve } from 'node:path'
import { z } from 'zod'
import { publishedPlotDetailSchema, publishedPlotSchema } from '../shared/published-plot.ts'

export const plotVersionPattern = /^plot-[a-f0-9]{20}$/

const manifestSchema = z.object({
  version_id: z.string(),
  files: z.unknown().optional(),
})

const hashesSchema = z.record(z.string(), z.unknown())

export function openPlotPublicationStore(root: string) {
  async function openVersion(version: string) {
    const folder = resolve(root, version)
    const manifestBytes = await readFile(resolve(folder, 'manifest.json'))
    const manifest = manifestSchema.parse(JSON.parse(manifestBytes.toString('utf8')))
    if (manifest.version_id !== version) throw new Error('Publication identity mismatch')
    const hashes = hashesSchema.safeParse(manifest.files)

    return {
      async file(name: string) {
        if (name === 'manifest.json') return manifestBytes
        const bytes = await readFile(resolve(folder, name))
        const digest = createHash('sha256').update(bytes).digest('hex')
        if (!hashes.success || digest !== hashes.data[name]) throw new Error('Published artifact has changed')
        return bytes
      },
    }
  }

  async function readPlot(publication: Awaited<ReturnType<typeof openVersion>>, version: string) {
    const raw = JSON.parse((await publication.file('run.json')).toString('utf8'))
    const run = publishedPlotSchema.parse(raw)
    if (run.version_id !== version) throw new Error('Publication identity mismatch')
    return run
  }

  async function list(sha?: string) {
    const entries = await readdir(root, { withFileTypes: true })
    const loaded = await Promise.all(entries.filter(entry => entry.isDirectory() && plotVersionPattern.test(entry.name))
      .map(async ({ name }) => {
        try {
          return { run: await readPlot(await openVersion(name), name) }
        } catch {
          return { unavailable: name }
        }
      }))
    return {
      versions: loaded.flatMap(item => item.run && (!sha || item.run.pdf.sha256 === sha) ? [item.run] : []),
      unavailable: loaded.flatMap(item => item.unavailable ? [item.unavailable] : []),
    }
  }

  async function detail(version: string) {
    const publication = await openVersion(version)
    const [run, plotBytes, checkBytes] = await Promise.all([
      readPlot(publication, version),
      publication.file('plot-input.json'),
      publication.file('check-report.json'),
    ])
    const detail = publishedPlotDetailSchema.parse({
      run,
      plot: JSON.parse(plotBytes.toString('utf8')),
      check: JSON.parse(checkBytes.toString('utf8')),
    })
    const { plot } = detail
    if (plot.sha256 !== run.pdf.sha256 || run.coverage.figure !== `Figure ${plot.settings.figure}`
      || run.coverage.panel !== plot.settings.panel) throw new Error('PDF or panel identity mismatch')

    const points = plot.points.filter(point => run.coverage.point_indices.includes(point.index))
    if (points.length !== run.coverage.point_indices.length || points.length !== run.results.length) {
      throw new Error('Published observation mismatch')
    }

    for (const [index, result] of run.results.entries()) {
      const point = points[index]
      if (result.source.page !== point.source.page || result.source.figure !== run.coverage.figure
        || JSON.stringify(result.source.box) !== JSON.stringify(point.source.box)
        || result.parameter.value !== point.x || Number(result.value) !== point.y) {
        throw new Error('Published observation mismatch')
      }
    }
    plot.points = points

    return detail
  }

  return {
    list,
    detail,
    async file(version: string, name: string) {
      return (await openVersion(version)).file(name)
    },
  }
}
