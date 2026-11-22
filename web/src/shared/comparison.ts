import { z } from 'zod'
import { idSchema } from './schema.ts'
import type { Result, Evidence } from './schema'
export const resultRefSchema = z.object({ publicationId: idSchema, versionId: z.string().regex(/^version-[a-f0-9]{40}$/), resultId: idSchema }).strict()
export type ResultRef = z.infer<typeof resultRefSchema>
export type ComparisonSide = { ref: ResultRef; title: string; pdfSha256: string; result: Result; evidence: Evidence[]; pdfUrl: string }
export const scopeProofSchema = z.object({ a: resultRefSchema, b: resultRefSchema,
  aPdfSha256: z.string().regex(/^[a-f0-9]{64}$/), bPdfSha256: z.string().regex(/^[a-f0-9]{64}$/),
  note: z.string().min(1), source: z.object({ publicationId: idSchema, versionId: z.string().regex(/^version-[a-f0-9]{40}$/), evidenceId: idSchema }),
  reviewedFields: z.array(z.string()),
  origin: z.literal('extracted').optional(),
  scopeEvidenceId: idSchema.optional(),
})
export type ScopeProof = z.infer<typeof scopeProofSchema>
export type ComparisonResponse = { a: ComparisonSide; b: ComparisonSide; status: 'comparable' | 'different' | 'insufficient';
  checks: Array<{ field: string; a: string | null; b: string | null; state: 'same' | 'different' | 'unknown' | 'reviewed' | 'documented' | 'equivalent' }>;
  reasons: string[]; delta?: string; unit?: string; proof?: ScopeProof }
export type ComparisonCandidate = {
  ref: ResultRef;
  title: string;
  method: string;
  generator: string;
  rawValue: string;
  status: Exclude<ComparisonResponse['status'], 'different'>;
  reasons: string[];
  sameExperiment: boolean;
}
export type ComparisonRecommendation = {
  a: ResultRef; b: ResultRef; methods: [string, string]; dataset: string; metric: string; delta: string; unit: string;
}
export function comparisonUrl(a?: ResultRef, b?: ResultRef) {
  const params = new URLSearchParams()
  if (a) params.set('a', JSON.stringify(a))
  if (b) params.set('b', JSON.stringify(b))
  return `/compare?${params}`
}
