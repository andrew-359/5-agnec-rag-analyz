import { createComparisonApi } from './comparison/comparisons.ts';
import { readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { Hono } from 'hono';
import { plotPublications } from './plot-publications.ts';
import { serveStatic } from '@hono/node-server/serve-static';
import { fetchRequestHandler } from '@trpc/server/adapters/fetch';
import { initTRPC, TRPCError } from '@trpc/server';
import { z } from 'zod';
import { datasetSchema, idSchema } from '../shared/schema.ts';
import { demoPublicationId } from '../shared/publication.ts';
import { createReviewDraftsApi } from './review-drafts.ts';
import { articlesApi } from './article/articles.ts';
import type { ArticleDelivery } from './article/article-queue.ts';
import { savedRunApi } from './saved-run.ts';
import { canReadPdf, createPublicationStore, defaultPublicationDirectory, type PublicationStore } from './publication/publication-store.ts';
import type { ReviewModelConfig } from './review-assistant.ts';
import { openReviewUsage } from './review-usage.ts';

const trpc = initTRPC.create({ isDev: false });
const publicationInput = z.object({ publicationId: idSchema });
const versionInput = publicationInput.extend({ versionId: idSchema.optional() });

export type DatasetLookup =
  | { kind: 'publication'; publicationId: string; versionId?: string }
  | { kind: 'run'; publicationId: string; runId: string }
  | { kind: 'evidence'; publicationId: string; evidenceId: string; versionId?: string }
  | { kind: 'pdf'; pdfId: string };
type DatasetReader = (lookup: DatasetLookup) => Promise<unknown>;

function createRouter(readDataset: DatasetReader, pdfPath: string, publicationStore?: PublicationStore) {
  const load = async (lookup: Exclude<DatasetLookup, { kind: 'pdf' }>) => {
    let raw: unknown;
    try {
      if (lookup.kind === 'run' && lookup.runId.startsWith('version-') && publicationStore) {
        raw = await publicationStore.read(lookup.publicationId, lookup.runId);
      } else if (lookup.kind !== 'run' && lookup.versionId) {
        raw = await publicationStore?.read(lookup.publicationId, lookup.versionId);
      } else {
        const published = lookup.kind === 'publication'
          ? await publicationStore?.latest(lookup.publicationId)
          : lookup.kind === 'evidence' ? await publicationStore?.findEvidence(lookup.publicationId, lookup.evidenceId) : null;
        raw = published ?? await readDataset(lookup);
      }
    }
    catch { throw new TRPCError({ code: 'INTERNAL_SERVER_ERROR', message: 'Unable to read prepared demo' }); }
    if (raw == null) throw new TRPCError({ code: 'NOT_FOUND', message: 'Publication, run or evidence not found' });
    const parsed = datasetSchema.safeParse(raw);
    if (!parsed.success) throw new TRPCError({ code: 'INTERNAL_SERVER_ERROR', message: 'Prepared data is invalid' });
    if (parsed.data.publication.id !== lookup.publicationId) throw new TRPCError({ code: 'NOT_FOUND', message: 'Publication not found' });
    return parsed.data;
  };
  return trpc.router({
    publications: trpc.router({
      list: trpc.procedure.query(() => publicationStore?.list() ?? []),
      demoAvailable: trpc.procedure.query(async () => {
        try {
          const demo = await load({ kind: 'publication', publicationId: demoPublicationId });
          return demo.version
            ? publicationStore !== undefined && await publicationStore.hasSourcePdf(demoPublicationId, demo.version.versionId)
            : await canReadPdf(pdfPath);
        } catch {
          return false;
        }
      }),
      review: trpc.procedure.input(versionInput).query(({ input }) => load({ kind: 'publication', ...input })),
      get: trpc.procedure.input(versionInput).query(async ({ input }) => {
        const { publication, pdf, run, manifest, version, remarks } = await load({ kind: 'publication', ...input });
        return { publication, pdf, run, manifest, version, remarks };
      }),
    }),
    results: trpc.router({ list: trpc.procedure.input(publicationInput.extend({
      runId: idSchema, dataset: z.string().min(1).max(200).optional(), metric: z.string().min(1).max(200).optional(), generator: z.string().min(1).max(200).optional(),
    })).query(async ({ input }) => {
      const data = await load({ kind: 'run', publicationId: input.publicationId, runId: input.runId });
      if (data.run.id !== input.runId) throw new TRPCError({ code: 'NOT_FOUND', message: 'Extraction run not found' });
      const results = data.results.filter((row) => (input.dataset === undefined || row.dataset === input.dataset) && (input.metric === undefined || row.metric === input.metric) && (input.generator === undefined || row.generator === input.generator));
      const values = (field: 'dataset' | 'metric' | 'generator') => [...new Set(data.results.map((row) => row[field]))];
      return { results, count: results.length, options: { datasets: values('dataset'), metrics: values('metric'), generators: values('generator').filter(Boolean) } };
    }) }),
    evidence: trpc.router({ get: trpc.procedure.input(versionInput.extend({ evidenceId: idSchema, resultId: idSchema.optional(), remarkId: idSchema.optional() })).query(async ({ input }) => {
      const { resultId, remarkId, ...lookup } = input;
      const data = await load({ kind: 'evidence', ...lookup });
      const evidence = data.evidence.find((source) => source.id === input.evidenceId);
      if (!evidence) throw new TRPCError({ code: 'NOT_FOUND', message: 'Source evidence not found' });
      const result = resultId ? data.results.find((row) => row.id === resultId) : null;
      if (resultId && (!result || !result.evidenceIds.includes(evidence.id))) throw new TRPCError({ code: 'BAD_REQUEST', message: 'Selected result does not belong to this evidence' });
      if (resultId && remarkId) throw new TRPCError({ code: 'BAD_REQUEST', message: 'Select either a result or a remark' });
      const selectedRemark = remarkId ? data.remarks.find(remark => remark.id === remarkId) : undefined;
      if (remarkId && (!selectedRemark || !selectedRemark.evidenceIds.includes(evidence.id))) {
        throw new TRPCError({ code: 'BAD_REQUEST', message: 'Selected remark does not belong to this evidence' });
      }
      const relatedIds = new Set(result?.evidenceIds ?? selectedRemark?.evidenceIds);
      const relatedEvidence = data.evidence.filter((item) => relatedIds.has(item.id));
      const remarks = selectedRemark ? [selectedRemark] : data.remarks.filter(remark => remark.evidenceIds.includes(evidence.id) || (result && remark.resultIds?.includes(result.id)));
      return { evidence, result: result ?? null, pdf: data.pdf, run: data.run, relatedEvidence, remarks };
    }) }),
  });
}
export type AppRouter = ReturnType<typeof createRouter>;

/** The saved dataset reader is supplied at the server boundary. No extraction logic lives here. */
export function createApp(readDataset: DatasetReader, options: {
  pdfPath: string;
  assetsDir?: string;
  articleRoot?: string;
  articleStore?: string;
  reviewDraftStore?: string;
  articleDelivery?: ArticleDelivery;
  articleContext?: boolean;
  savedRun?: string;
  publicationStore?: PublicationStore;
  publishedPlots?: string;
} & ReviewModelConfig) {
  const app = new Hono();
  const publicationStore = options.publicationStore ?? (options.articleRoot ? createPublicationStore({
    root: options.articleRoot, directory: defaultPublicationDirectory(options.articleRoot),
  }) : undefined);
  const router = createRouter(readDataset, options.pdfPath, publicationStore);
  if (options.publishedPlots) app.route('/', plotPublications(options.publishedPlots));
  if (publicationStore) {
    app.route('/api/comparisons', createComparisonApi({ publications: publicationStore, proofFile: resolve(options.articleRoot ?? '.', 'docs/research/controls/comparison-scopes.json') }));
    const draftDirectory = options.reviewDraftStore ?? resolve(options.articleStore ?? resolve(options.articleRoot ?? '.', 'output/web-articles'), 'review-drafts');
    app.route('/api/review', createReviewDraftsApi({ publications: publicationStore, directory: draftDirectory }));
    app.get('/api/publications/:publicationId/versions/:versionId/:name', async (c) => {
      const { publicationId, versionId, name } = c.req.param();
      const file = await publicationStore.file(publicationId, versionId, name);
      if (!file) return c.notFound();
      if (name !== 'source.pdf') c.header('Content-Disposition', `attachment; filename="${name}"`);
      c.header('Content-Type', name === 'source.pdf' ? 'application/pdf'
        : name === 'export.zip' ? 'application/zip'
          : name.endsWith('.csv') ? 'text/csv; charset=utf-8'
            : name.endsWith('.json') ? 'application/json' : 'text/markdown; charset=utf-8');
      c.header('X-Content-Type-Options', 'nosniff');
      return c.body(new Uint8Array(file));
    });
  }
  if (options.articleRoot) {
    const review = {
      reviewModel: options.reviewModel,
      reviewSyntheticOnly: options.reviewSyntheticOnly,
      reviewUsage: options.reviewModel
        ? openReviewUsage(resolve(options.articleStore || resolve(options.articleRoot, 'output/web-articles')))
        : undefined,
    };
    app.route('/api', articlesApi(options.articleRoot, {
      store: options.articleStore, delivery: options.articleDelivery, articleContext: options.articleContext, publications: publicationStore,
      review,
    }));
    app.route('/api', savedRunApi(options.articleRoot, options.savedRun, review));
  }
  app.all('/trpc/*', (c) => fetchRequestHandler({ endpoint: '/trpc', req: c.req.raw, router, createContext: () => ({}) }));
  app.get('/api/pdfs/:pdfId', async (c) => {
    const raw = await readDataset({ kind: 'pdf', pdfId: c.req.param('pdfId') });
    if (raw == null) return c.json({ error: 'PDF not found' }, 404);
    const parsed = datasetSchema.safeParse(raw);
    if (!parsed.success) return c.json({ error: 'Prepared data is invalid' }, 500);
    if (c.req.param('pdfId') !== parsed.data.pdf.id) return c.json({ error: 'PDF not found' }, 404);
    try {
      return c.body(new Uint8Array(await readFile(options.pdfPath)), 200, { 'Content-Type': 'application/pdf', 'X-Content-Type-Options': 'nosniff' });
    } catch { return c.json({ error: 'Prepared PDF is unavailable' }, 503); }
  });
  app.all('/api/*', (c) => c.json({ error: 'API route not found' }, 404));
  if (options.assetsDir) {
    app.use('/assets/*', serveStatic({ root: options.assetsDir }));
    const index = serveStatic({ path: resolve(options.assetsDir, 'index.html') });
    app.get('/', index);
    app.get('/publications/*', index);
    app.get('/plots', index);
    app.get('/plots/*', index);
  }
  app.onError((_error, c) => c.json({ error: 'Unable to read prepared demo' }, 500));
  return app;
}
