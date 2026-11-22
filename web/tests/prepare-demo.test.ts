import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { resolve } from 'node:path';
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { prepareDemo } from '../src/server/prepare-demo.ts';

const pdfPath = process.env.D001_PDF ?? resolve('../docs/context/RAG_ВИНИТИ_участникам_2026-11-20/dev/D001.pdf');

test('missing participant PDF explains preparation recovery without publishing a dataset', async () => {
  const outDir = await mkdtemp(resolve(tmpdir(), 'rag-missing-pdf-'));
  try {
    const published = resolve(outDir, 'prepared');
    await assert.rejects(prepareDemo({ snapshotPath: resolve('data/d001-run.json'), manifestPath: resolve('data/manifest.json'), pdfPath: resolve(outDir, 'unavailable.pdf'), outDir: published }), /D001 PDF is unavailable\. Supply --pdf/);
    await assert.rejects(readFile(resolve(published, 'dataset.json')), { code: 'ENOENT' });
  } finally { await rm(outDir, { recursive: true, force: true }); }
});

test('prepares the saved D001 extraction reproducibly with exact values and independent source identities', async () => {
  const outDir = await mkdtemp(resolve(tmpdir(), 'rag-preparation-'));
  try {
    const input = { snapshotPath: resolve('data/d001-run.json'), manifestPath: resolve('data/manifest.json'), pdfPath, outDir };
    const first = await prepareDemo(input);
    const second = await prepareDemo(input);
    assert.deepEqual(second, first);
    assert.equal(first.manifest.schemaVersion, 1);
    assert.equal(first.manifest.provenance.processorVersion, null);
    assert.equal(first.results.length, 252);
    assert.equal(new Set(first.results.map((row) => row.id)).size, 252);
    const thread = first.results.find((row) => row.method === 'ThreadRAG' && row.generator === 'LLaMA3-8B-Instruct' && row.dataset === 'HotpotQA' && row.metric === 'EM')!;
    assert.partialDeepStrictEqual(thread, { rawValue: '.422', value: '0.422', unit: 'fraction', statistic: null, retrievalTopK: null, runId: first.manifest.runId });
    assert.partialDeepStrictEqual(first.evidence.find((source) => source.id === thread.evidenceIds[0]), { runId: first.manifest.runId, pdfId: first.manifest.pdfId, page: 6, row: 18, column: 1, precision: 'cell', headers: ['LLaMA3-8B-Instruct', 'HotpotQA', 'EM'] });
    const sameValues = first.results.filter((row) => row.rawValue === '.204');
    assert.ok(sameValues.length > 1);
    assert.equal(new Set(sameValues.map((row) => row.evidenceIds[0])).size, sameValues.length);
    assert.equal((await readFile(resolve(outDir, 'source.pdf'))).subarray(0, 4).toString(), '%PDF');
    assert.deepEqual(JSON.parse(await readFile(resolve(outDir, 'dataset.json'), 'utf8')), first);
  } finally { await rm(outDir, { recursive: true, force: true }); }
});

test('refuses an unsupported schema with a clear preparation error', async () => {
  const outDir = await mkdtemp(resolve(tmpdir(), 'rag-schema-'));
  try {
    const manifest = JSON.parse(await readFile('data/manifest.json', 'utf8'));
    await writeFile(resolve(outDir, 'manifest.json'), JSON.stringify({ ...manifest, schemaVersion: 2 }));
    await assert.rejects(prepareDemo({ snapshotPath: resolve('data/d001-run.json'), manifestPath: resolve(outDir, 'manifest.json'), pdfPath, outDir }), /Unsupported demo schema version: 2/);
  } finally { await rm(outDir, { recursive: true, force: true }); }
});

test('synthetic preparation fixture preserves zero, decimal precision, unknowns and shared mean/SD evidence', async () => {
  const outDir = await mkdtemp(resolve(tmpdir(), 'rag-synthetic-contract-'));
  try {
    const original = JSON.parse(await readFile('data/d001-run.json', 'utf8'));
    const template = original.results[0];
    const synthetic = JSON.stringify({ ...original, results: [
      { ...template, raw_value: '0', value: '0', unit: null, protocol: null, statistic: 'mean' },
      { ...template, raw_value: '.00000000000000000001', value: '0.00000000000000000001', unit: null, protocol: null, statistic: 'sd' },
    ] });
    const manifest = JSON.parse(await readFile('data/manifest.json', 'utf8'));
    await writeFile(resolve(outDir, 'snapshot.json'), synthetic);
    await writeFile(resolve(outDir, 'manifest.json'), JSON.stringify({ ...manifest, runId: 'run-synthetic-contract-fixture', snapshotSha256: createHash('sha256').update(synthetic).digest('hex'), provenance: { ...manifest.provenance, description: 'Synthetic contract fixture, not D001 extraction results.' } }));
    const data = await prepareDemo({ snapshotPath: resolve(outDir, 'snapshot.json'), manifestPath: resolve(outDir, 'manifest.json'), pdfPath, outDir });
    assert.equal(data.results[0].value, '0');
    assert.equal(data.results[0].rawValue, '0');
    assert.equal(data.results[1].value, '0.00000000000000000001');
    assert.equal(data.results[1].rawValue, '.00000000000000000001');
    assert.equal(data.results[0].unit, null);
    assert.equal(data.results[0].protocol, null);
    assert.notEqual(data.results[0].id, data.results[1].id);
    assert.deepEqual(data.results[0].evidenceIds, data.results[1].evidenceIds);
  } finally { await rm(outDir, { recursive: true, force: true }); }
});

test('preparation rejects an altered PDF or saved snapshot before publishing files', async () => {
  const outDir = await mkdtemp(resolve(tmpdir(), 'rag-hash-'));
  try {
    const input = { snapshotPath: resolve('data/d001-run.json'), manifestPath: resolve('data/manifest.json'), pdfPath, outDir: resolve(outDir, 'prepared') };
    await writeFile(resolve(outDir, 'wrong.pdf'), '%PDF wrong document');
    await assert.rejects(prepareDemo({ ...input, pdfPath: resolve(outDir, 'wrong.pdf') }), /PDF SHA-256 does not match/);
    await writeFile(resolve(outDir, 'wrong.json'), '{}');
    await assert.rejects(prepareDemo({ ...input, snapshotPath: resolve(outDir, 'wrong.json') }), /Saved extraction SHA-256 does not match/);
    await assert.rejects(readFile(resolve(input.outDir, 'dataset.json')), { code: 'ENOENT' });
  } finally { await rm(outDir, { recursive: true, force: true }); }
});
