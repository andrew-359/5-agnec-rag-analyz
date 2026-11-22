import { z } from 'zod';
import { publishedVersionSchema, versionUrl } from './publication.ts';

export const idSchema = z.string().min(1).max(160).regex(/^[A-Za-z0-9_-]+$/);
export const boxSchema = z.tuple([z.number(), z.number(), z.number(), z.number()]).refine(
  ([l, t, r, b]) => 0 <= l && l < r && r <= 1 && 0 <= t && t < b && b <= 1,
  'Expected normalized top-left page coordinates',
);
const hashSchema = z.string().regex(/^[a-f0-9]{64}$/);
export const manifestSchema = z.object({
  schemaVersion: z.literal(1), publicationId: idSchema, pdfId: idSchema, runId: idSchema,
  document: z.string().min(1), pdfSha256: hashSchema, snapshotSha256: hashSchema,
  provenance: z.object({ kind: z.literal('saved-extraction'), description: z.string(), processorVersion: z.string().nullable() }),
});
const sourceSchema = z.object({
  page: z.number().int().positive(), table: z.number().int().positive(),
  row: z.number().int().nonnegative().nullable().default(null), column: z.number().int().nonnegative().nullable().default(null),
  headers: z.array(z.string()).default([]), box: boxSchema.nullable().default(null),
});
const decimalSchema = z.string().regex(/^[+-]?(?:\d+(?:\.\d+)?|\.\d+)$/);
const resultFields = {
  method: z.string(), generator: z.string(), dataset: z.string(), metric: z.string(),
  task: z.string(), experiment: z.string(), protocol: z.string().nullable(),
  unit: z.string().nullable(), statistic: z.string().nullable(),
};
export const snapshotSchema = z.object({
  document: z.string(), page: z.number().int().positive(), table: z.number().int().positive(),
  results: z.array(z.object({ ...resultFields, raw_value: z.string(), value: decimalSchema, retrieval_top_k: z.number().int().nullable(), source: sourceSchema })),
  remarks: z.array(z.object({ reason: z.string(), raw_text: z.string(), source: sourceSchema })),
});
export const parametersSchema = z.record(z.string().min(1), z.number());
export const digitizationSchema = z.object({
  method: z.literal('vector-line'), resolution: z.number().positive(), series: z.string().min(1).optional(),
});
export const resultSchema = z.object({
  id: idSchema, runId: idSchema, ...resultFields, rawValue: z.string(), value: decimalSchema.nullable(),
  configId: idSchema.optional(), experimentId: idSchema.optional(),
  metricGroup: z.string().optional(), metricVariant: z.string().optional(), baselineValue: decimalSchema.optional(),
  conditionEvidence: z.record(z.string(), z.array(idSchema)).optional(),
  parameters: parametersSchema.optional(), digitization: digitizationSchema.optional(),
  retrievalTopK: z.number().int().nullable(), evidenceIds: z.array(idSchema).min(1),
  partial: z.literal(true).optional(),
});
export const evidenceSchema = sourceSchema.extend({
  id: idSchema, runId: idSchema, pdfId: idSchema,
  table: sourceSchema.shape.table.nullable(), tableDesignation: z.string().optional(),
  figure: z.string().optional(), text: z.string().optional(), strategy: z.string().optional(),
  precision: z.enum(['cell', 'estimated_cell', 'table', 'page', 'text_fragment', 'figure', 'plot_point']),
});
const remarkOptionSchema = z.object({
  value: z.union([z.string(), z.number()]),
  evidenceIds: z.array(idSchema),
  binding: z.array(z.string()).optional(),
  retrievalTopK: z.number().int().nonnegative().nullable().optional(),
  conditionEvidence: z.record(z.string(), z.array(idSchema)).optional(),
});
export const datasetSchema = z.object({
  manifest: manifestSchema,
  version: publishedVersionSchema.optional(),
  publication: z.object({ id: idSchema, title: z.string(), pdfId: idSchema, publishedRunId: idSchema }),
  pdf: z.object({ id: idSchema, sha256: hashSchema, url: z.string() }),
  run: z.object({ id: idSchema, schemaVersion: z.literal(1), publicationId: idSchema, pdfId: idSchema, provenance: manifestSchema.shape.provenance }),
  results: z.array(resultSchema), evidence: z.array(evidenceSchema),
  remarks: z.array(z.object({
    id: idSchema, runId: idSchema, reason: z.string(), rawText: z.string(), evidenceIds: z.array(idSchema),
    resultIds: z.array(idSchema).optional(), options: z.array(remarkOptionSchema).optional(),
  })),
}).superRefine((data, ctx) => {
  const { manifest: m, publication: p, pdf, run } = data;
  const fail = () => ctx.addIssue({ code: 'custom', message: 'Inconsistent publication, run, PDF or source identity' });
  const pdfUrl = data.version ? `/api${versionUrl(p.id, run.id)}/source.pdf` : `/api/pdfs/${m.pdfId}`;
  if (p.id !== m.publicationId || p.pdfId !== m.pdfId || p.publishedRunId !== m.runId || pdf.id !== m.pdfId || pdf.sha256 !== m.pdfSha256 || pdf.url !== pdfUrl || run.id !== m.runId || run.pdfId !== m.pdfId || run.publicationId !== m.publicationId) fail();
  if (data.version && (data.version.publicationId !== p.id || data.version.versionId !== run.id || data.version.pdfSha256 !== pdf.sha256)) fail();
  const evidence = new Map(data.evidence.map((source) => [source.id, source]));
  if (evidence.size !== data.evidence.length || new Set(data.results.map((r) => r.id)).size !== data.results.length || new Set(data.remarks.map((r) => r.id)).size !== data.remarks.length) fail();
  for (const source of data.evidence) if (source.runId !== m.runId || source.pdfId !== m.pdfId) fail();
  for (const row of [...data.results, ...data.remarks]) if (row.runId !== m.runId || row.evidenceIds.some((id) => !evidence.has(id))) fail();
  for (const remark of data.remarks) for (const option of remark.options ?? []) {
    const ids = [...option.evidenceIds, ...Object.values(option.conditionEvidence ?? {}).flat()];
    if (ids.some(id => !remark.evidenceIds.includes(id))) fail();
  }
});
export type Dataset = z.infer<typeof datasetSchema>;
export type Result = z.infer<typeof resultSchema>;
export type Evidence = z.infer<typeof evidenceSchema>;
export type RemarkOption = z.infer<typeof remarkOptionSchema>;
