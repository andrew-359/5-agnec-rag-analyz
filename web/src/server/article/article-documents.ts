import { mkdir, readFile, readdir, rm, stat, writeFile } from 'node:fs/promises'
import { join } from 'node:path'
import { inventorySchema } from '../../shared/article.ts'
import { exists, readJson } from './article-files.ts'

const articleIdPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/
const defaultName = 'article.pdf'

function displayNameFor(encodedName: string | undefined) {
  let name = defaultName
  try {
    name = decodeURIComponent(encodedName || name).split(/[\\/]/).pop() || name
  } catch {
    // A malformed header leaves the default display name.
  }
  return name.replace(/[\x00-\x1f\x7f]/g, '').slice(0, 255) || defaultName
}

export function openArticleDocuments(store: string) {
  const articleFolder = (id: string) => join(store, id)
  const mapFolder = (id: string) => join(articleFolder(id), 'map')
  const sourcePdf = (id: string) => join(mapFolder(id), 'source.pdf')

  async function inventoryFile(id: string) {
    if (!articleIdPattern.test(id)) return undefined
    const filename = join(mapFolder(id), 'inventory.json')
    return await exists(filename) ? filename : undefined
  }

  return {
    sourcePdf,
    hasSourcePdf: async (id: string) => articleIdPattern.test(id) && exists(sourcePdf(id)),
    inventoryFile,
    overviewFile: (id: string) => join(mapFolder(id), 'overview.json'),
    async uploadedAt(id: string) {
      return (await stat(join(mapFolder(id), 'inventory.json'))).mtimeMs
    },
    async list() {
      const entries = await readdir(store, { withFileTypes: true })
      return entries.filter(entry => entry.isDirectory() && articleIdPattern.test(entry.name)).map(entry => entry.name)
    },
    async inventory(id: string) {
      const filename = await inventoryFile(id)
      return filename ? inventorySchema.parse(await readJson(filename)) : null
    },
    readName: (id: string) => readFile(join(articleFolder(id), 'name.txt'), 'utf8'),
    legacyRun: (id: string, page: number, table: number) => join(articleFolder(id), `p${page}-t${table}`),
    async save(
      id: string,
      pdf: Buffer,
      encodedName: string | undefined,
      inspect: (input: string, output: string) => Promise<unknown>,
    ) {
      const folder = articleFolder(id)
      await mkdir(folder, { recursive: true, mode: 0o700 })
      const input = join(folder, 'upload.pdf')
      await writeFile(input, pdf)
      await writeFile(join(folder, 'name.txt'), displayNameFor(encodedName))
      await inspect(input, mapFolder(id))
      await rm(input)
    },
    remove: (id: string) => rm(articleFolder(id), { recursive: true, force: true }),
  }
}
