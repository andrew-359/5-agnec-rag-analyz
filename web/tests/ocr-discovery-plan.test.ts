import assert from 'node:assert/strict';
import { test } from 'node:test';
import { mkdtemp, mkdir, writeFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { createApp } from '../src/server/app.ts';

test('both HTTP entry points persist the same reader plan for digital and OCR-discovered tables', async () => {
  const store = await mkdtemp(join(tmpdir(), 'rag-ocr-plan-'));
  const articleId = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa';
  const folder = join(store, articleId);
  const shutdown = new AbortController();
  shutdown.abort(); // This test verifies the persisted plan without dispatching a worker.
  try {
    await mkdir(join(folder, 'map'), { recursive: true });
    await writeFile(join(folder, 'name.txt'), 'unseen-scan.pdf');
    await writeFile(join(folder, 'map', 'inventory.json'), JSON.stringify({
      schema_version: 1, document: 'source.pdf', sha256: 'b'.repeat(64), page_count: 7,
      pages: [{ page: 6, image_boxes: [] }, { page: 7, image_boxes: [[.52, .19, .88, .28]] }],
      warnings: [], limitations: [],
      candidates: [
        { id: 'digital-caption', kind: 'table', page: 6, label: '1', text: 'Table 1: Results',
          box: [.1, .2, .9, .3], extract_arguments: ['--page', '6', '--table', '1'] },
        { id: 'ocr-caption', kind: 'table', page: 6, label: '2', text: 'Table 2: Results',
          box: [.1, .2, .9, .3], discovery: 'ocr', extract_arguments: ['--page', '6', '--table', '2'] },
        { id: 'near-raster', kind: 'table', page: 7, label: '3', text: 'Table 3: Results',
          box: [.54, .29, .86, .31], extract_arguments: ['--page', '7', '--table', '3'] },
      ],
    }));
    const app = createApp(async () => null, { pdfPath: '', articleRoot: resolve('..'), articleStore: store,
      articleDelivery: { url: 'amqp://127.0.0.1:1', queue: 'unused-test-queue', shutdown: shutdown.signal } });
    const accepted = await app.request(`/api/articles/${articleId}/runs`, { method: 'POST' });
    assert.equal(accepted.status, 202);
    const { id } = await accepted.json();
    const run = await (await app.request(`/api/article-runs/${id}`)).json();
    assert.equal(run.stages.length, 3);
    for (const [index, [candidateId, strategy, reason, fallbackStrategy]] of [
      ['digital-caption', 'docling-tableformer', /по умолчанию/, null],
      ['ocr-caption', 'docling-rapidocr', /найдена через OCR/, null],
      ['near-raster', 'docling-tableformer', /по умолчанию/, 'docling-rapidocr'],
    ].entries()) {
      const fullStage = run.stages[index];
      const singleAccepted = await app.request(`/api/articles/${articleId}/tables/${candidateId}`, { method: 'POST' });
      assert.equal(singleAccepted.status, 202);
      const single = await (await app.request(singleAccepted.headers.get('location')!)).json();
      assert.equal(single.stages.length, 1);
      const [singleStage] = single.stages;
      assert.equal(fullStage.candidateId, candidateId);
      assert.equal(singleStage.strategy, strategy);
      assert.equal(fullStage.gridRead.strategy, strategy);
      assert.equal(fullStage.strategyPlan.strategy, strategy);
      assert.match(fullStage.strategyPlan.reason, reason as RegExp);
      assert.equal(fullStage.strategyPlan.fallback?.strategy ?? null, fallbackStrategy);
      if (fallbackStrategy) assert.match(fullStage.strategyPlan.fallback.reason, /растровую область рядом с подписью/);
      assert.deepEqual(singleStage.strategyPlan, fullStage.strategyPlan);
    }
  } finally { await rm(store, { recursive: true, force: true }); }
});

test('table regions retain their PDF address and caption status through both run entry points', async () => {
  const store = await mkdtemp(join(tmpdir(), 'rag-table-region-'));
  const articleId = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa';
  const folder = join(store, articleId);
  const shutdown = AbortSignal.abort();
  const roman = {
    box: [.11, .13, .9, .42], bounds_status: 'confirmed', caption_status: 'confirmed',
    designation: 'Table II', caption_box: [.4, .09, .6, .12], caption_text: 'TABLE II',
  };
  const anonymous = {
    box: [.11, .53, .9, .64], bounds_status: 'confirmed', caption_status: 'missing',
    designation: null, caption_box: null, caption_text: null,
  };
  const disputed = {
    ...anonymous, box: [.11, .74, .9, .85], bounds_status: 'needs_review',
    alternative_boxes: [[.09, .73, .9, .99]],
  };
  try {
    await mkdir(join(folder, 'map'), { recursive: true });
    await writeFile(join(folder, 'name.txt'), 'scan.pdf');
    await writeFile(join(folder, 'map', 'inventory.json'), JSON.stringify({
      schema_version: 1, document: 'source.pdf', sha256: 'b'.repeat(64), page_count: 8,
      warnings: [], candidates: [
        { id: 'roman', kind: 'table', page: 8, label: 'II', text: 'TABLE II',
          box: roman.box, discovery: 'ocr', region: roman },
        { id: 'anonymous', kind: 'table', page: 8, label: null, text: '',
          box: anonymous.box, discovery: 'ocr', region: anonymous },
        { id: 'disputed', kind: 'table', page: 8, label: null, text: '',
          box: disputed.box, discovery: 'ocr', region: disputed },
      ],
    }));
    const app = createApp(async () => null, { pdfPath: '', articleRoot: resolve('..'), articleStore: store,
      articleDelivery: { url: 'amqp://127.0.0.1:1', queue: 'unused-test-queue', shutdown } });
    const base = `/api/articles/${articleId}`;
    const map = await (await app.request(base)).json();
    assert.deepEqual(map.candidates.map((candidate: { region: unknown; extractable: boolean }) =>
      [candidate.region, candidate.extractable]), [[roman, true], [anonymous, true], [disputed, false]]);

    const start = async (endpoint: string) => {
      const response = await app.request(endpoint, { method: 'POST' });
      assert.equal(response.status, 202);
      return (await app.request(response.headers.get('location')!)).json();
    };
    const full = await start(`${base}/runs`);
    const single = await start(`${base}/tables/anonymous`);
    const namedStage = full.stages.find((stage: { candidateId: string }) => stage.candidateId === 'roman');
    const unnamedStage = full.stages.find((stage: { candidateId: string }) => stage.candidateId === 'anonymous');
    assert.equal(full.pdfSha256, 'b'.repeat(64));
    assert.equal(full.stages.length, 2);
    assert.equal(namedStage.page, 8);
    assert.deepEqual(namedStage.region, roman);
    assert.deepEqual(namedStage.gridRead.region, roman);
    assert.deepEqual(unnamedStage.region, anonymous);
    assert.deepEqual(unnamedStage.region, single.stages[0].region);
    assert.equal(unnamedStage.table, single.stages[0].table);
    assert.notEqual(unnamedStage.table, namedStage.table);
    assert.equal(full.coverage.excludedCandidates[0].reason, 'table-bounds-review');
    assert.deepEqual(full.coverage.excludedCandidates[0].region, disputed);
    assert.equal((await app.request(`${base}/tables/disputed`, { method: 'POST' })).status, 422);
  } finally { await rm(store, { recursive: true, force: true }); }
});
