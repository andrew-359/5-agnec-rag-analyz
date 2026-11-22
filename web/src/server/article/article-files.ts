import { access, readFile } from 'node:fs/promises'
import { join } from 'node:path'
import { runSchema, type Run, type RunResponse } from '../../shared/saved-run.ts'

export const exists = (filename: string) => access(filename).then(() => true, () => false)
export const readJson = async (filename: string): Promise<unknown> => JSON.parse(await readFile(filename, 'utf8'))
export const noStore = { 'Cache-Control': 'no-store' }
export const pdfHeaders = { 'Content-Type': 'application/pdf', 'X-Content-Type-Options': 'nosniff' }

const tableSnapshotPath = (folder: string) => join(folder, 'run.json')
const readSnapshot = async (filename: string) => runSchema.parse(await readJson(filename))

export async function readOptionalTableSnapshot(folder: string) {
  const filename = tableSnapshotPath(folder)
  return await exists(filename) ? readSnapshot(filename) : null
}

export function tableRunResponse(run: Run, document: string): RunResponse {
  return { run: { ...run, document }, demo: false, assets: { image: true, pdf: true, csv: true } }
}

export async function readTableRun(document: string, folder: string): Promise<RunResponse> {
  const run = await readSnapshot(tableSnapshotPath(folder))
  return tableRunResponse(run, document)
}

export async function readTableArtifact(folder: string, file: string) {
  const headers: Record<string, string> | null = file === 'page.png' ? { 'Content-Type': 'image/png' }
    : file === 'results.csv' ? { 'Content-Type': 'text/csv; charset=utf-8', 'Content-Disposition': 'attachment; filename="results.csv"' }
      : null
  if (!headers) return null
  const filename = join(folder, file)
  return (await exists(filename)) ? { body: await readFile(filename), headers } : null
}
