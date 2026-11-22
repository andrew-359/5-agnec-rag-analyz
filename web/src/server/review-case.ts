import { createHash } from 'node:crypto';
import { readFile } from 'node:fs/promises';
import { join } from 'node:path';
import { z } from 'zod';
import { runSchema, type Run } from '../shared/saved-run.ts';
import { hasReviewConditionValue, reviewCaseInputSchema, reviewFieldSchema, reviewRecordId, type ReviewCase } from '../shared/review-case.ts';

const reviewSourceSchema = z.object({
  source: z.object({ sha256: z.string().regex(/^[a-f0-9]{64}$/) }),
});

export type ReviewArtifact = { artifactId: string; run: Run; pdfSha256: string | null };
type ResolveArtifact = (artifactId: string) => Promise<ReviewArtifact | null>;

export class ReviewCaseError extends Error {
  readonly code: 'invalid_input' | 'artifact_unavailable' | 'artifact_changed' | 'record_unavailable' | 'field_unavailable' | 'pdf_unavailable';

  constructor(code: ReviewCaseError['code'], message: string) {
    super(message);
    this.code = code;
  }
}

export function parseReviewArtifact(bytes: Buffer): ReviewArtifact {
  const data: unknown = JSON.parse(bytes.toString('utf8'));
  const run = runSchema.parse(data);
  const source = reviewSourceSchema.safeParse(data);
  return { artifactId: createHash('sha256').update(bytes).digest('hex'), run, pdfSha256: source.data?.source.sha256 ?? null };
}

export function matchesReviewPdf(pdf: Buffer, expectedSha256: string) {
  return createHash('sha256').update(pdf).digest('hex') === expectedSha256;
}

export async function verifyReviewPdf(pdfPath: string, sha: string | null, expectedPdfSha256?: string) {
  const pdf = sha && await readFile(pdfPath).catch(() => null);
  if (!sha || !pdf || (expectedPdfSha256 !== undefined && expectedPdfSha256 !== sha) || !matchesReviewPdf(pdf, sha)) {
    throw new ReviewCaseError('pdf_unavailable', 'PDF выбранной версии прогона недоступен.');
  }
}

export async function readReviewArtifact(directory: string) {
  try {
    return parseReviewArtifact(await readFile(join(directory, 'run.json')));
  } catch {
    throw new ReviewCaseError('artifact_unavailable', 'Не удалось прочитать разрешённый артефакт прогона.');
  }
}

// The adapter determines what this request may read. The application checks the
// version against the loaded bytes rather than trusting the selection or folder name.
export async function getReviewCase(input: unknown, resolveArtifact: ResolveArtifact): Promise<ReviewCase> {
  const parsed = reviewCaseInputSchema.safeParse(input);
  if (!parsed.success) throw new ReviewCaseError('invalid_input', 'Укажите SHA-256 артефакта и ID записи с допустимым полем.');
  const { artifactId, selection } = parsed.data;
  const artifact = await resolveArtifact(artifactId);
  if (!artifact) throw new ReviewCaseError('artifact_unavailable', 'Артефакт прогона недоступен.');
  if (artifact.artifactId !== artifactId) throw new ReviewCaseError('artifact_changed', 'Указанная версия артефакта недоступна. Откройте текущую версию прогона.');

  const { run, pdfSha256 } = artifact;
  const recordId = selection.kind === 'result' ? selection.resultId : selection.remarkId;
  const matches = (record: { id?: string }) => record.id !== undefined && reviewRecordId(artifactId, record.id) === recordId;
  const result = selection.kind === 'result' ? run.results.find(matches) : undefined;
  const remark = selection.kind === 'remark' ? run.remarks.find(matches) : undefined;
  const record = result ?? remark;
  if (!record) throw new ReviewCaseError('record_unavailable', 'Запись отсутствует в указанной версии артефакта.');

  const evidence = new Map(run.evidence?.map(source => [source.id, source]));
  const linkedIds = (ids?: string[]) => [...new Set(ids)].filter(id => evidence.has(id));
  const confirmed: ReviewCase['confirmed'] = [];
  for (const field of reviewFieldSchema.options) {
    const value = result ? result[field] : remark?.conditions?.[field];
    const sourceIds = linkedIds(record.condition_sources?.[field]);
    if (hasReviewConditionValue(value) && sourceIds.length) {
      confirmed.push({ field, value, sourceIds });
    }
  }
  if (selection.kind === 'result' && !confirmed.some(condition => condition.field === selection.field)) {
    throw new ReviewCaseError('field_unavailable', 'У выбранного поля нет подтверждённого значения с источником.');
  }

  const openField = remark ? {
    field: remark.field ?? null,
    reason: remark.reason,
    options: (remark.options ?? []).map(option => ({ value: option.value, sourceIds: linkedIds(option.evidence_ids) })),
  } : null;
  const sourceIds = new Set([...confirmed, ...(openField?.options ?? [])].flatMap(value => value.sourceIds));
  const sources = [...sourceIds].flatMap(id => {
    const source = evidence.get(id);
    return source ? [{ ...source, text: source.text?.slice(0, 700) }] : [];
  });

  const { page, table, row = null, column = null, box, evidence_ids } = record.source;
  const cellEvidence = linkedIds(evidence_ids).map(id => evidence.get(id))
    .find(source => source?.page === page && source.precision === 'cell' && source.box);
  const remarkLabel = (key: 'method' | 'dataset' | 'metric') => {
    const value = remark?.conditions?.[key];
    return typeof value === 'string' ? value : null;
  };
  return {
    artifactId, pdfSha256, selection,
    record: {
      method: result?.method ?? remarkLabel('method'), dataset: result?.dataset ?? remarkLabel('dataset'),
      metric: result?.metric ?? remarkLabel('metric'), rawValue: result?.raw_value ?? remark?.raw_text ?? '',
    },
    cell: { page, table, row, column, box: box ?? cellEvidence?.box ?? null },
    confirmed, openField, sources,
  };
}
