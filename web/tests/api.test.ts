import { test } from 'node:test';
import assert from 'node:assert/strict';
import { once } from 'node:events';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { resolve } from 'node:path';
import { serve } from '@hono/node-server';
import { createTRPCClient, httpBatchLink, TRPCClientError } from '@trpc/client';
import { createApp } from '../src/server/app.ts';
import type { AppRouter } from '../src/server/app.ts';
import { prepareDemo } from '../src/server/prepare-demo.ts';

const hasCode = (code: string) => (error: unknown) => error instanceof TRPCClientError && error.data?.code === code;

test('unsupported prepared data and a missing known PDF have distinct safe HTTP outcomes', async () => {
  const data = JSON.parse(await readFile('.runtime/dataset.json', 'utf8'));
  let unsupported = true;
  const app = createApp(async () => unsupported ? { ...data, manifest: { ...data.manifest, schemaVersion: 99 } } : data,
    { pdfPath: '/private/participant-data/nonexistent-source.pdf' });
  const server = serve({ fetch: app.fetch, hostname: '127.0.0.1', port: 0 });
  await once(server, 'listening');
  const address = server.address();
  assert.ok(address && typeof address !== 'string');
  const origin = `http://127.0.0.1:${address.port}`;
  const client = createTRPCClient<AppRouter>({ links: [httpBatchLink({ url: `${origin}/trpc` })] });
  try {
    await assert.rejects(client.publications.get.query({ publicationId: data.publication.id }), (error: unknown) => {
      assert.ok(error instanceof TRPCClientError);
      assert.equal(error.data?.code, 'INTERNAL_SERVER_ERROR');
      assert.equal(error.message, 'Prepared data is invalid');
      return true;
    });
    const invalidPdf = await fetch(`${origin}/api/pdfs/${data.pdf.id}`);
    assert.equal(invalidPdf.status, 500);
    assert.deepEqual(await invalidPdf.json(), { error: 'Prepared data is invalid' });
    unsupported = false;
    const missing = await fetch(`${origin}/api/pdfs/${data.pdf.id}`);
    assert.equal(missing.status, 503);
    assert.deepEqual(await missing.json(), { error: 'Prepared PDF is unavailable' });
    assert.equal((await fetch(`${origin}/api/pdfs/unknown`)).status, 404);
    const empty = await client.results.list.query({ publicationId: data.publication.id, runId: data.run.id, dataset: 'synthetic-no-matches' });
    assert.deepEqual(empty.results, []);
    assert.equal(empty.count, 0);
  } finally { await new Promise<void>((done, reject) => server.close((error) => error ? reject(error) : done())); }
});

test('a failed dataset read reports a safe technical error and can recover through HTTP', async () => {
  const privatePath = '/private/participant-data/secret/dataset.json';
  let unavailable = true;
  const data = JSON.parse(await readFile('.runtime/dataset.json', 'utf8'));
  const app = createApp(async () => {
    if (unavailable) throw new Error(`ENOENT: permission denied, open '${privatePath}'`);
    return data;
  }, { pdfPath: resolve('.runtime/source.pdf') });
  const server = serve({ fetch: app.fetch, hostname: '127.0.0.1', port: 0 });
  await once(server, 'listening');
  const address = server.address();
  assert.ok(address && typeof address !== 'string');
  const origin = `http://127.0.0.1:${address.port}`;
  const client = createTRPCClient<AppRouter>({ links: [httpBatchLink({ url: `${origin}/trpc` })] });
  try {
    await assert.rejects(client.publications.get.query({ publicationId: data.publication.id }), (error: unknown) => {
      assert.ok(error instanceof TRPCClientError);
      assert.equal(error.data?.code, 'INTERNAL_SERVER_ERROR');
      assert.equal(error.message, 'Unable to read prepared demo');
      assert.ok(!JSON.stringify(error.shape).includes(privatePath));
      return true;
    });
    const pdfFailure = await fetch(`${origin}/api/pdfs/${data.pdf.id}`);
    assert.equal(pdfFailure.status, 500);
    assert.deepEqual(await pdfFailure.json(), { error: 'Unable to read prepared demo' });
    unavailable = false;
    assert.equal((await client.publications.get.query({ publicationId: data.publication.id })).run.id, data.run.id);
  } finally { await new Promise<void>((done, reject) => server.close((error) => error ? reject(error) : done())); }
});

test('HTTP contract binds evidence and selected result to the known publication, run and PDF', async () => {
  const outDir = await mkdtemp(resolve(tmpdir(), 'rag-api-'));
  try {
    const data = await prepareDemo({ snapshotPath: resolve('data/d001-run.json'), manifestPath: resolve('data/manifest.json'), pdfPath: process.env.D001_PDF ?? resolve('../docs/context/RAG_ВИНИТИ_участникам_2026-11-20/dev/D001.pdf'), outDir });
    const app = createApp(async () => data, { pdfPath: resolve(outDir, 'source.pdf') });
    const server = serve({ fetch: app.fetch, hostname: '127.0.0.1', port: 0 });
    await once(server, 'listening');
    const address = server.address();
    assert.ok(address && typeof address !== 'string');
    const origin = `http://127.0.0.1:${address.port}`;
    const client = createTRPCClient<AppRouter>({ links: [httpBatchLink({ url: `${origin}/trpc` })] });
    try {
      const input = { publicationId: data.publication.id, runId: data.run.id, dataset: 'HotpotQA', metric: 'EM', generator: 'LLaMA3-8B-Instruct' };
      const listed = await client.results.list.query(input);
      const thread = listed.results.find((row) => row.method === 'ThreadRAG')!;
      const other = listed.results.find((row) => row.method === 'RA-CoT')!;
      const source = await client.evidence.get.query({ publicationId: input.publicationId, evidenceId: thread.evidenceIds[0], resultId: thread.id });
      assert.equal(source.result!.id, thread.id);
      assert.equal(source.evidence.pdfId, data.pdf.id);
      const direct = await client.evidence.get.query({ publicationId: input.publicationId, evidenceId: thread.evidenceIds[0] });
      assert.equal(direct.result, null);
      await assert.rejects(client.evidence.get.query({ publicationId: input.publicationId, evidenceId: thread.evidenceIds[0], resultId: other.id }), hasCode('BAD_REQUEST'));
      await assert.rejects(client.results.list.query({ ...input, runId: 'unknown-run' }), hasCode('NOT_FOUND'));
      await assert.rejects(client.publications.get.query({ publicationId: '../source.pdf' }), hasCode('BAD_REQUEST'));
      const pdf = await fetch(`${origin}${source.pdf.url}`);
      assert.equal(pdf.headers.get('content-type'), 'application/pdf');
      assert.equal(Buffer.from(await pdf.arrayBuffer()).subarray(0, 4).toString(), '%PDF');
      assert.equal((await fetch(`${origin}/api/pdfs/unknown`)).status, 404);
      assert.equal((await fetch(`${origin}/api/pdfs/..%2Fsource.pdf`)).status, 404);
    } finally { await new Promise<void>((done, reject) => server.close((error) => error ? reject(error) : done())); }
  } finally { await rm(outDir, { recursive: true, force: true }); }
});
