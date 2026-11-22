import { test } from 'node:test';
import assert from 'node:assert/strict';
import { once } from 'node:events';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { resolve } from 'node:path';
import { serve } from '@hono/node-server';
import { createTRPCClient, httpBatchLink, TRPCClientError } from '@trpc/client';
import { createApp } from '../src/server/app.ts';
import type { AppRouter } from '../src/server/app.ts';
import { prepareDemo } from '../src/server/prepare-demo.ts';

test('HTTP filters a saved run, preserves full-run options and rejects malformed filters', async () => {
  const outDir = await mkdtemp(resolve(tmpdir(), 'rag-filters-'));
  try {
    const data = await prepareDemo({ snapshotPath: resolve('data/d001-run.json'), manifestPath: resolve('data/manifest.json'), pdfPath: process.env.D001_PDF ?? resolve('../docs/context/RAG_ВИНИТИ_участникам_2026-11-20/dev/D001.pdf'), outDir });
    const server = serve({ fetch: createApp(async () => data, { pdfPath: resolve(outDir, 'source.pdf') }).fetch, hostname: '127.0.0.1', port: 0 });
    await once(server, 'listening');
    const address = server.address();
    assert.ok(address && typeof address !== 'string');
    const client = createTRPCClient<AppRouter>({ links: [httpBatchLink({ url: `http://127.0.0.1:${address.port}/trpc` })] });
    try {
      const input = { publicationId: data.publication.id, runId: data.run.id };
      const all = await client.results.list.query(input);
      assert.equal(all.count, 252);
      assert.equal(all.results.length, 252);
      const expectedOptions = { datasets: ['HotpotQA', '2WikiMultihopQA', 'MuSiQue'], metrics: ['EM', 'F1'], generators: ['LLaMA3-8B-Instruct', 'Qwen3.5 plus', 'GPT-4o mini'] };
      assert.deepEqual(all.options, expectedOptions);
      for (const [filters, count] of [[{ dataset: 'MuSiQue' }, 84], [{ metric: 'F1' }, 126], [{ generator: 'GPT-4o mini' }, 84]] as const) {
        const listed = await client.results.list.query({ ...input, ...filters });
        assert.equal(listed.count, count);
        assert.equal(listed.results.length, count);
        for (const [field, value] of Object.entries(filters)) assert.ok(listed.results.every((row) => row[field as keyof typeof filters] === value));
        assert.deepEqual(listed.options, expectedOptions);
      }
      const combined = await client.results.list.query({ ...input, dataset: 'MuSiQue', metric: 'F1', generator: 'GPT-4o mini' });
      assert.equal(combined.count, 14);
      assert.equal(combined.results.length, 14);
      assert.equal(combined.results.find((row) => row.method === 'ThreadRAG')?.rawValue, '.437');
      assert.deepEqual(combined.options, expectedOptions);
      const empty = await client.results.list.query({ ...input, dataset: 'Unknown dataset' });
      assert.equal(empty.count, 0);
      assert.deepEqual(empty.results, []);
      assert.deepEqual(empty.options, expectedOptions);
      for (const field of ['dataset', 'metric', 'generator'] as const) {
        for (const value of ['', 'x'.repeat(201), 5]) await assert.rejects(
          client.results.list.query({ ...input, [field]: value as string }),
          (error: unknown) => error instanceof TRPCClientError && error.data?.code === 'BAD_REQUEST',
        );
      }
    } finally { await new Promise<void>((done, reject) => server.close((error) => error ? reject(error) : done())); }
  } finally { await rm(outDir, { recursive: true, force: true }); }
});
