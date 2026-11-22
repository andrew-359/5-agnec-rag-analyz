import { createHash } from 'node:crypto';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { datasetSchema, manifestSchema, snapshotSchema, type Dataset, type Evidence } from '../shared/schema.ts';

const sha256 = (content: string | Uint8Array) => createHash('sha256').update(content).digest('hex');
const identity = (kind: string, coordinates: unknown) => `${kind}-${sha256(JSON.stringify(coordinates)).slice(0, 24)}`;

/** Public preparation boundary: only reads the saved extraction; never executes Python/OCR. */
export async function prepareDemo(input: { snapshotPath: string; manifestPath: string; pdfPath: string; outDir: string }): Promise<Dataset> {
  const rawManifest: unknown = JSON.parse(await readFile(input.manifestPath, 'utf8'));
  if (rawManifest && typeof rawManifest === 'object' && 'schemaVersion' in rawManifest && rawManifest.schemaVersion !== 1) {
    throw new Error(`Unsupported demo schema version: ${String(rawManifest.schemaVersion)}`);
  }
  const manifest = manifestSchema.parse(rawManifest);
  const bytes = await readFile(input.snapshotPath);
  if (sha256(bytes) !== manifest.snapshotSha256) throw new Error('Saved extraction SHA-256 does not match manifest');
  const snapshot = snapshotSchema.parse(JSON.parse(bytes.toString('utf8')));
  let pdf: Buffer;
  try { pdf = await readFile(input.pdfPath); }
  catch { throw new Error('D001 PDF is unavailable. Supply --pdf <path-to-participant-D001.pdf>; preparation does not download or extract it.'); }
  if (sha256(pdf) !== manifest.pdfSha256) throw new Error('PDF SHA-256 does not match the saved D001 extraction');
  if (snapshot.document !== manifest.document) throw new Error('Saved extraction document does not match manifest');
  const sources = new Map<string, Evidence>();
  const sourceId = (source: (typeof snapshot.results)[number]['source']) => {
    const id = identity('evidence', [manifest.runId, source.page, source.table, source.row, source.column]);
    const record: Evidence = { ...source, id, runId: manifest.runId, pdfId: manifest.pdfId, precision: source.row !== null && source.column !== null ? 'cell' : 'table' };
    const existing = sources.get(id);
    if (existing && JSON.stringify(existing) !== JSON.stringify(record)) throw new Error('Conflicting source geometry for the same cell');
    sources.set(id, record);
    return id;
  };
  const results = snapshot.results.map((row) => ({
    id: identity('result', [manifest.runId, row.source.page, row.source.table, row.source.row, row.source.column, row.statistic, row.method, row.generator, row.dataset, row.metric]),
    runId: manifest.runId, method: row.method, generator: row.generator, dataset: row.dataset, metric: row.metric,
    task: row.task, experiment: row.experiment, protocol: row.protocol, rawValue: row.raw_value, value: row.value,
    unit: row.unit, statistic: row.statistic, retrievalTopK: row.retrieval_top_k, evidenceIds: [sourceId(row.source)],
  }));
  const remarks = snapshot.remarks.map((remark, index) => ({
    id: identity('remark', [manifest.runId, index]), runId: manifest.runId, reason: remark.reason, rawText: remark.raw_text, evidenceIds: [sourceId(remark.source)],
  }));
  const dataset = datasetSchema.parse({ manifest,
    publication: { id: manifest.publicationId, title: manifest.document, pdfId: manifest.pdfId, publishedRunId: manifest.runId },
    pdf: { id: manifest.pdfId, sha256: manifest.pdfSha256, url: `/api/pdfs/${manifest.pdfId}` },
    run: { id: manifest.runId, schemaVersion: manifest.schemaVersion, publicationId: manifest.publicationId, pdfId: manifest.pdfId, provenance: manifest.provenance },
    results, evidence: [...sources.values()], remarks,
  });
  await mkdir(input.outDir, { recursive: true });
  await writeFile(resolve(input.outDir, 'source.pdf'), pdf);
  await writeFile(resolve(input.outDir, 'dataset.json'), `${JSON.stringify(dataset, null, 2)}\n`);
  return dataset;
}
