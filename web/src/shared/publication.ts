import { z } from 'zod';

export const demoPublicationId = 'publication-d001';

export const coverageSchema = z.looseObject({
  partial: z.boolean(),
  summary: z.string().trim().min(1),
});

// The checker requires a leading alphanumeric character. Keep room for .pdf on disk.
export const submissionPublicationIdSchema = z.string().max(251).regex(/^[A-Za-z0-9][A-Za-z0-9_-]*$/);

export const publishedVersionSchema = z.object({
  publicationId: z.string(),
  submissionPublicationId: submissionPublicationIdSchema.optional(),
  versionId: z.string(),
  runId: z.string(),
  url: z.string(),
  downloadUrl: z.string(),
  pdfSha256: z.string(),
  coverage: coverageSchema,
});

export type PublishedVersion = z.infer<typeof publishedVersionSchema>;

export function versionUrl(publicationId: string, versionId: string) {
  return `/publications/${publicationId}/versions/${versionId}`;
}
