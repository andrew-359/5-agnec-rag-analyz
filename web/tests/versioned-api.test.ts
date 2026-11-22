import { test } from 'node:test';
import assert from 'node:assert/strict';
import { once } from 'node:events';
import { createHash } from 'node:crypto';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { resolve } from 'node:path';
import { serve } from '@hono/node-server';
import { createTRPCClient, httpBatchLink, TRPCClientError } from '@trpc/client';
import { createApp, type AppRouter } from '../src/server/app.ts';
import { prepareDemo } from '../src/server/prepare-demo.ts';
import { versionedData } from './helpers/versioned-data.ts';

test('HTTP resolves an archived evidence ID to its own run while the publication points at the latest run', async () => {
  const outDir = await mkdtemp(resolve(tmpdir(), 'rag-versioned-api-'));
  try {
    const prepared = await prepareDemo({ snapshotPath: resolve('data/d001-run.json'), manifestPath: resolve('data/manifest.json'), pdfPath: process.env.D001_PDF ?? resolve('../docs/context/RAG_ВИНИТИ_участникам_2026-11-20/dev/D001.pdf'), outDir });
    const { current, archived, readDataset } = versionedData(prepared);
    const server = serve({ fetch: createApp(readDataset, { pdfPath: resolve(outDir, 'source.pdf') }).fetch, hostname: '127.0.0.1', port: 0 });
    await once(server, 'listening');
    const address = server.address();
    assert.ok(address && typeof address !== 'string');
    const origin = `http://127.0.0.1:${address.port}`;
    const client = createTRPCClient<AppRouter>({ links: [httpBatchLink({ url: `${origin}/trpc` })] });
    try {
      const publicationId = current.publication.id;
      assert.equal((await client.publications.get.query({ publicationId })).publication.publishedRunId, current.run.id);
      const source = await client.evidence.get.query({ publicationId, evidenceId: 'evidence-synthetic-archived', resultId: 'result-synthetic-archived' });
      assert.equal(source.run.id, 'run-synthetic-archived');
      assert.equal(source.result?.method, 'Synthetic archived result');
      assert.equal(source.evidence.pdfId, archived.pdf.id);
      assert.equal(source.pdf.sha256, archived.manifest.pdfSha256);
      const listed = await client.results.list.query({ publicationId, runId: source.run.id });
      assert.equal(listed.count, 1);
      assert.equal(listed.results[0].id, 'result-synthetic-archived');
      const pdf = await fetch(`${origin}${source.pdf.url}`);
      assert.equal(pdf.headers.get('content-type'), 'application/pdf');
      assert.equal(createHash('sha256').update(Buffer.from(await pdf.arrayBuffer())).digest('hex'), archived.pdf.sha256);
      // A second extraction may share the identical PDF; this fixture claims no new PDF version.
      assert.equal(archived.pdf.id, current.pdf.id);
      await assert.rejects(client.evidence.get.query({ publicationId, evidenceId: 'evidence-unknown' }), (error: unknown) => error instanceof TRPCClientError && error.data?.code === 'NOT_FOUND');
      await assert.rejects(client.evidence.get.query({ publicationId, evidenceId: 'evidence-synthetic-archived', resultId: current.results[0].id }), (error: unknown) => error instanceof TRPCClientError && error.data?.code === 'BAD_REQUEST');
      await assert.rejects(client.evidence.get.query({ publicationId, evidenceId: current.evidence[0].id, resultId: 'result-synthetic-archived' }), (error: unknown) => error instanceof TRPCClientError && error.data?.code === 'BAD_REQUEST');
      await assert.rejects(client.evidence.get.query({ publicationId: 'publication-unknown', evidenceId: 'evidence-synthetic-archived' }), (error: unknown) => error instanceof TRPCClientError && error.data?.code === 'NOT_FOUND');
    } finally { await new Promise<void>((done, reject) => server.close((error) => error ? reject(error) : done())); }
  } finally { await rm(outDir, { recursive: true, force: true }); }
});
