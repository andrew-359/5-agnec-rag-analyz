import { createHash } from 'node:crypto'
import { constants } from 'node:fs'
import { access, mkdir, mkdtemp, readFile, readdir, rename, rm, stat } from 'node:fs/promises'
import { join, resolve } from 'node:path'
import { datasetSchema, idSchema } from '../../shared/schema.ts'
import { publishedVersionSchema } from '../../shared/publication.ts'
import { buildPublication, publicationInputSchema, rawPublicationArtifact, type PublicationInput } from './publication-data.ts'
import { publicationBundleFiles, writePublicationBundle } from './publication-bundle.ts'

export type { PublicationInput }
export type { PublishedVersion } from '../../shared/publication.ts'

const versionPattern = /^version-[a-f0-9]{40}$/
const pdfManifest = publishedVersionSchema.pick({ submissionPublicationId: true })
const readJson = async (path: string): Promise<unknown> => JSON.parse(await readFile(path, 'utf8'))
const digest = (bytes: string | Uint8Array) => createHash('sha256').update(bytes).digest('hex')
const missing = (error: unknown) => (error as NodeJS.ErrnoException).code === 'ENOENT'

export async function canReadPdf(path: string) {
  try {
    const info = await stat(path)
    if (!info.isFile() || !info.size) return false
    await access(path, constants.R_OK)
    return true
  } catch {
    return false
  }
}

export function defaultPublicationDirectory(root: string) {
  return resolve(process.env.RAG_PUBLICATION_STORE || join(root, 'output/publications'))
}

export function createPublicationStore(options: { directory: string; root: string; checkerPath?: string; python?: string }) {
  const directory = resolve(options.directory)
  const checkerPath = resolve(options.checkerPath || process.env.RAG_SUBMISSION_CHECKER ||
    join(options.root, 'docs/context/RAG_ВИНИТИ_участникам_2026-11-20/tools/check_submission.py'))
  const versionFolder = (publicationId: string, versionId: string) =>
    idSchema.safeParse(publicationId).success && versionPattern.test(versionId)
      ? join(directory, publicationId, versionId)
      : null
  const sourcePdfPath = async (folder: string, publicationId: string) => {
    const manifest = pdfManifest.parse(await readJson(join(folder, 'bundle', 'manifest.json')))
    return join(folder, 'pdf', `${manifest.submissionPublicationId ?? publicationId}.pdf`)
  }

  const read = async (publicationId: string, versionId: string) => {
    const folder = versionFolder(publicationId, versionId)
    if (!folder) return null
    try {
      const data = datasetSchema.parse(await readJson(join(folder, 'dataset.json')))
      if (data.version?.publicationId !== publicationId || data.version.versionId !== versionId) throw new Error('Неверный адрес опубликованной версии.')
      return data
    } catch (error) {
      if (missing(error)) return null
      throw error
    }
  }

  async function latest(publicationId: string) {
    if (!idSchema.safeParse(publicationId).success) return null
    try {
      const parent = join(directory, publicationId)
      const candidates = (await readdir(parent, { withFileTypes: true }))
        .filter(entry => entry.isDirectory() && versionPattern.test(entry.name))
      const versions = await Promise.all(candidates.map(async ({ name }) => {
        const manifest = await readJson(join(parent, name, 'bundle', 'manifest.json')) as { publishedAt: string }
        return { name, publishedAt: manifest.publishedAt }
      }))
      versions.sort((a, b) => b.publishedAt.localeCompare(a.publishedAt) || b.name.localeCompare(a.name))
      return versions.length ? read(publicationId, versions[0].name) : null
    } catch (error) {
      if (missing(error)) return null
      throw error
    }
  }

  async function listLatest(excludePublicationId?: string) {
    const entries = await readdir(directory, { withFileTypes: true }).catch((error: unknown) => {
      if (missing(error)) return []
      throw error
    })
    const datasets = await Promise.all(entries.filter(entry => entry.isDirectory() && entry.name !== excludePublicationId)
      .map(entry => latest(entry.name)))
    return datasets.filter(item => item !== null)
  }

  const versionForRun = (publicationId: string, runId: string) =>
    'version-' + digest(JSON.stringify([publicationId, runId])).slice(0, 40)

  return {
    async forRun(publicationId: string, runId: string) {
      return (await read(publicationId, versionForRun(publicationId, runId)))?.version ?? null
    },
    read,
    latest,
    listLatest,
    async list() {
      return (await listLatest()).flatMap(item => item.version ? [{ ...item.version, title: item.publication.title }] : [])
    },
    async findEvidence(publicationId: string, evidenceId: string) {
      const data = await latest(publicationId)
      return data?.evidence.some(item => item.id === evidenceId) ? data : null
    },
    async hasSourcePdf(publicationId: string, versionId: string) {
      const folder = versionFolder(publicationId, versionId)
      return folder !== null && await canReadPdf(await sourcePdfPath(folder, publicationId))
    },
    async file(publicationId: string, versionId: string, name: string) {
      const folder = versionFolder(publicationId, versionId)
      if (!folder) return null
      try {
        if (name === 'source.pdf') return await readFile(await sourcePdfPath(folder, publicationId))
        if (name === 'export.zip') return await readFile(join(folder, name))
        if (publicationBundleFiles.includes(name)) return await readFile(join(folder, 'bundle', name))
        return null
      } catch (error) {
        if (missing(error)) return null
        throw error
      }
    },
    async publish(request: PublicationInput) {
      const input = publicationInputSchema.parse(request)
      const { publicationId, runId, pdfSha256, pdfPath, ...snapshot } = input
      const submissionPublicationId = input.submissionPublicationId ?? publicationId
      const versionId = versionForRun(publicationId, runId)
      const snapshotArtifacts = snapshot.artifacts.map(artifact => ({ ...artifact, data: rawPublicationArtifact(artifact.data) }))
      const snapshotSha256 = digest(JSON.stringify([runId, pdfSha256, { ...snapshot, artifacts: snapshotArtifacts }], (_key, value: unknown) => {
        if (value && typeof value === 'object' && !Array.isArray(value)) return Object.fromEntries(Object.entries(value).sort(([a], [b]) => a.localeCompare(b)))
        return value
      }))
      const previous = async () => {
        const saved = await read(publicationId, versionId)
        if (!saved) return null
        if (saved.manifest.snapshotSha256 !== snapshotSha256) {
          throw new Error(`Для прогона ${runId} уже опубликован выход с другим составом данных или версией PDF.`)
        }
        return saved.version!
      }
      const existing = await previous()
      if (existing) return existing

      const { dataset, tables, origin } = buildPublication(input, versionId, snapshotSha256)
      const pdf = await readFile(pdfPath)
      if (digest(pdf) !== pdfSha256) throw new Error('SHA-256 снимка PDF не совпадает с версией принятого прогона.')

      const parent = join(directory, publicationId)
      await mkdir(parent, { recursive: true, mode: 0o700 })
      const temporary = await mkdtemp(join(parent, '.building-'))
      try {
        await writePublicationBundle(temporary, {
          dataset, tables, origin, pdf, submissionPublicationId, snapshotSha256,
        }, { root: options.root, python: options.python, checkerPath })
        try {
          await rename(temporary, join(parent, versionId))
        } catch (error) {
          const winner = await previous()
          if (winner) return winner
          throw error
        }
        return dataset.version!
      } finally {
        await rm(temporary, { recursive: true, force: true })
      }
    },
  }
}

export type PublicationStore = ReturnType<typeof createPublicationStore>
