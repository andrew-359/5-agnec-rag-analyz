import { access, readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { Hono } from 'hono';
import { bodyLimit } from 'hono/body-limit';
import { explainReview, readReviewUsage, reviewEnabled, type ReviewConfig } from './review-assistant.ts';
import { matchesReviewPdf, readReviewArtifact } from './review-case.ts';

export function savedRunApi(root: string, configuredRun?: string, review: ReviewConfig = {}) {
  const app = new Hono();
  const demo = !configuredRun;
  const directory = configuredRun ? resolve(configuredRun) : resolve(root, 'web/demo');
  const files = {
    'page.png': resolve(directory, 'page.png'),
    'source.pdf': demo
      ? resolve(root, 'docs/context/RAG_ВИНИТИ_участникам_2026-11-20/dev/D001.pdf')
      : resolve(directory, 'source.pdf'),
    'results.csv': resolve(directory, 'results.csv'),
  };
  const available = (filename: string) => access(filename).then(() => true, () => false);

  app.get('/run', async (c) => {
    try {
      const { run, artifactId, pdfSha256 } = await readReviewArtifact(directory);
      const [image, pdf, csv] = await Promise.all(Object.values(files).map(available));
      const reviewArtifactId = pdf && reviewEnabled(review, { pdfSha256, artifactId }) ? artifactId : undefined;
      return c.json({ run, demo, assets: { image, pdf, csv }, reviewArtifactId }, 200, { 'Cache-Control': 'no-store' });
    } catch {
      return c.json({ error: 'Не удалось открыть прогон. Проверьте run.json и путь RAG_RUN_DIR на сервере.' }, 422);
    }
  });
  const reviewLocation = () => ({ directory, pdfPath: files['source.pdf'] });
  app.post('/run/review', bodyLimit({ maxSize: 2048 }), c => explainReview(c.req.raw, reviewLocation, review));
  app.get('/run/review', c => readReviewUsage(c.req.raw, reviewLocation, review));

  app.get('/files/:name', async (c) => {
    const name = c.req.param('name');
    if (name !== 'source.pdf' && name !== 'page.png' && name !== 'results.csv') return c.notFound();
    try {
      const data = await readFile(files[name]);
      const sha = c.req.query('sha256');
      if (name === 'source.pdf' && sha !== undefined && !matchesReviewPdf(data, sha)) return c.notFound();
      const contentTypes = { 'page.png': 'image/png', 'source.pdf': 'application/pdf', 'results.csv': 'text/csv; charset=utf-8' };
      c.header('Content-Type', contentTypes[name]);
      c.header('X-Content-Type-Options', 'nosniff');
      if (name === 'results.csv') c.header('Content-Disposition', 'attachment; filename="results.csv"');
      return c.body(data);
    } catch {
      return c.json({ error: 'Файл отсутствует в этом прогоне.' }, 404);
    }
  });
  return app;
}
