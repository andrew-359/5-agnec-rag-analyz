import { constants } from 'node:fs';
import { access, mkdir, readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { serve } from '@hono/node-server';
import { createApp } from './app.ts';
import { datasetSchema } from '../shared/schema.ts';
import { configuredReviewModel } from './review-assistant.ts';
import { defaultPublicationDirectory } from './publication/publication-store.ts';

const root = fileURLToPath(new URL('../../', import.meta.url));
const runtime = resolve(root, '.runtime');
const articleRoot = resolve(root, '..');
const assetsDir = resolve(root, 'dist/client');
const articleStore = resolve(process.env.RAG_ARTICLE_STORE || resolve(articleRoot, 'output/web-articles'));
const publicationStore = defaultPublicationDirectory(articleRoot);
let data: unknown;
let loadError: unknown;
try {
  data = datasetSchema.parse(JSON.parse(await readFile(resolve(runtime, 'dataset.json'), 'utf8')));
} catch (error) {
  loadError = error;
  console.warn('Saved D001 is unavailable. Run pnpm prepare:demo --pdf <path-to-D001.pdf>.');
}
const shutdown = new AbortController();
const amqpUrl = process.env.RAG_AMQP_URL;
const app = createApp(async () => {
  if (loadError) throw loadError;
  return data;
}, {
  pdfPath: resolve(runtime, 'source.pdf'),
  assetsDir,
  articleRoot,
  articleStore,
  articleContext: process.env.RAG_ARTICLE_CONTEXT === undefined ? undefined : process.env.RAG_ARTICLE_CONTEXT === '1',
  articleDelivery: amqpUrl ? {
    url: amqpUrl,
    queue: process.env.RAG_AMQP_QUEUE || 'rag.article-extraction',
    shutdown: shutdown.signal,
  } : undefined,
  savedRun: process.env.RAG_RUN_DIR,
  publishedPlots: process.env.RAG_PUBLISHED_PLOTS || resolve(root, '../runs/published-plots'),
  ...configuredReviewModel(process.env),
});
app.get('/readyz', async c => {
  const sha = process.env.RAG_RELEASE_SHA ?? 'local';
  let ready = false;
  try {
    await mkdir(publicationStore, { recursive: true, mode: 0o700 });
    await Promise.all([
      access(resolve(assetsDir, 'index.html'), constants.R_OK),
      access(articleStore, constants.W_OK | constants.X_OK),
      access(publicationStore, constants.W_OK | constants.X_OK),
      ...(process.env.RAG_SUBMISSION_CHECKER ? [access(process.env.RAG_SUBMISSION_CHECKER, constants.R_OK)] : []),
    ]);
    ready = true;
  } catch {
    // The next healthcheck can recover after storage becomes available.
  }
  const status = ready ? 'ready' : 'unavailable';
  if (c.req.header('X-RAG-Probe') === 'compose') console.log(JSON.stringify({ event: 'readiness_probe', status, sha }));
  return c.json({ status, sha }, ready ? 200 : 503, { 'Cache-Control': 'no-store' });
});
const hostname = process.env.RAG_HOST || '127.0.0.1';
const server = serve({ fetch: app.fetch, hostname, port: Number(process.env.PORT ?? 4310) }, (info) => console.log(`RAG web: http://${hostname}:${info.port}`));
for (const signal of ['SIGINT', 'SIGTERM']) process.on(signal, () => {
  shutdown.abort();
  server.close();
});
