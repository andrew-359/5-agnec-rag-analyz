import { z } from 'zod';
import type { RunEvidence, Source } from './saved-run.ts';

const artifactIdSchema = z.string().regex(/^[a-f0-9]{64}$/);
const recordIdSchema = z.string().regex(/^[a-f0-9]{64}:[A-Za-z0-9_-]{1,160}$/);
export const reviewFieldSchema = z.enum(['generator', 'retrieval_top_k', 'protocol']);

export const reviewCaseInputSchema = z.strictObject({
  artifactId: artifactIdSchema,
  selection: z.discriminatedUnion('kind', [
    z.strictObject({ kind: z.literal('remark'), remarkId: recordIdSchema }),
    z.strictObject({ kind: z.literal('result'), resultId: recordIdSchema, field: reviewFieldSchema }),
  ]),
});

export type ReviewField = z.infer<typeof reviewFieldSchema>;
export type ReviewCaseInput = z.infer<typeof reviewCaseInputSchema>;

export function reviewRecordId(artifactId: string, localId: string) {
  return `${artifactId}:${localId}`;
}

export function hasReviewConditionValue(value: unknown): value is string | number {
  return typeof value === 'number' || (typeof value === 'string' && value.trim().length > 0);
}

type CitedValue = { value: string | number; sourceIds: string[] };
export type ReviewCase = {
  artifactId: string;
  pdfSha256: string | null;
  selection: ReviewCaseInput['selection'];
  record: { method: string | null; dataset: string | null; metric: string | null; rawValue: string };
  cell: Required<Pick<Source, 'page' | 'table' | 'row' | 'column' | 'box'>>;
  confirmed: Array<CitedValue & { field: ReviewField }>;
  openField: { field: string | null; reason: string; options: CitedValue[] } | null;
  sources: RunEvidence[];
};

export type ReviewExplanation =
  | { status: 'proposed'; field: ReviewField; value: string | number; sourceIds: string[]; explanation: string }
  | { status: 'needs_review'; explanation: string };

export type ReviewResponse = { reviewCase: ReviewCase; explanation: ReviewExplanation; answerId: string; chatId: string };
