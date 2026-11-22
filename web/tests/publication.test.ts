import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, readFile, readdir, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createApp } from '../src/server/app.ts';
import { buildPublication, parsePublicationArtifact, publicationInputSchema } from '../src/server/publication/publication-data.ts';
import { createPublicationStore } from '../src/server/publication/publication-store.ts';
import { demoPublicationId } from '../src/shared/publication.ts';

const root = fileURLToPath(new URL('../../', import.meta.url));
const pdfPath = process.env.D001_PDF || join(root, 'docs/context/RAG_ВИНИТИ_участникам_2026-11-20/dev/D001.pdf');

test('reconciled duplicates keep their source in CSV and conflicting values remain inspectable', async () => {
  const run = JSON.parse(await readFile(join(root, 'runs/snapshots/D001/run.json'), 'utf8'));
  const template = run.results.find((row: { raw_value: string }) => row.raw_value === '.422');
  const makeArtifact = (artifact: string, values: string[], page: number) => ({
    id: artifact, runId: 'trial', data: {
      source: run.source, remarks: [],
      evidence: values.map((value, i) => ({ id: `s${i}`, text: value, page, precision: 'text_fragment' })),
      results: values.map((value, i) => ({
        ...template, id: `r${i}`, experiment: `Experiment ${i}`, value, raw_value: value, condition_sources: {},
        source: { page, evidence_ids: [`s${i}`] },
      })),
    },
  });
  const artifacts = [makeArtifact('table', ['0.422', '0.5', '0.9'], 6), makeArtifact('text', ['0.422', '0.6'], 7)];
  const relationships = (['agreement', 'conflict'] as const).map((status, i) => ({
    status, members: artifacts.map(({ id }) => ({ artifact_id: id, result_id: `r${i}` })),
  }));
  const input = publicationInputSchema.parse({ publicationId: 'D001', runId: 'trial', document: 'D001.pdf', pdfPath,
    pdfSha256: run.source.sha256, coverage: { partial: true, summary: 'Test' }, artifacts, relationships });

  const { dataset, tables } = buildPublication(input, 'version-123', '0'.repeat(64));
  assert.deepEqual(dataset.results.map(row => row.value), ['0.422', '0.9']);
  assert.deepEqual(dataset.results[0].evidenceIds.map(id => dataset.evidence.find(source => source.id === id)?.page), [6, 7]);
  assert.deepEqual(tables.results.map(row => [row.metric_value, row.source_page, row.duplicate_of]), [
    ['0.422', 6, undefined], ['0.5', 6, undefined], ['0.9', 6, undefined], ['0.422', 7, 'R1'], ['0.6', 7, undefined],
  ]);
  assert.deepEqual(dataset.remarks[0].options?.map(option => [
    option.value, dataset.evidence.find(source => source.id === option.evidenceIds[0])?.page,
  ]), [['0.5', 6], ['0.6', 7]]);
  assert.match(String(tables.issues[0].record_ids), /^R2;R5$/);
  const [observation] = relationships[0].members;
  assert.throws(() => publicationInputSchema.parse({ ...input, relationships: [
    { status: 'agreement', members: [observation, observation] },
  ] }), /повторяется наблюдение/);
});

test('publishes each conflicting reading with its own value and retrieval source', async t => {
  const directory = await mkdtemp(join(tmpdir(), 'rag-publication-reading-conflict-'));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const run = JSON.parse(await readFile(join(root, 'runs/snapshots/D001/run.json'), 'utf8'));
  const { source } = run.results.find((row: { raw_value: string }) => row.raw_value === '.422');
  const binding = ['ThreadRAG', 'LLaMA3-8B-Instruct', 'HotpotQA', 'EM'];
  const options = [0, 5].map(topK => ({ value: '.422', binding, retrieval_top_k: topK,
    evidence_ids: [`reading-${topK}`], condition_sources: { retrieval_top_k: [`top-k-${topK}`] },
  }));
  run.evidence.push(...[0, 5].flatMap(topK => [
    { id: `reading-${topK}`, page: source.page, text: '.422', box: source.box, precision: 'cell' },
    { id: `top-k-${topK}`, page: topK + 1, text: `Top-k=${topK}`, box: [.1, .2, .3, .4], precision: 'text_fragment' },
  ]));
  run.remarks = [{ id: 'conflict', kind: 'reading-conflict', reason: 'Чтения расходятся.', raw_text: '.422 / .422',
    source: { ...source, evidence_ids: ['reading-0', 'reading-5'] }, options,
  }];
  const store = createPublicationStore({ directory, root });
  const version = await store.publish({ publicationId: 'D001', runId: 'reading-conflict', document: 'D001.pdf',
    pdfPath, pdfSha256: run.source.sha256, coverage: { partial: true, summary: 'Table 1' },
    artifacts: [{ id: 'table-1', runId: 'reading-conflict', data: run }],
  });
  const app = createApp(async () => null, { pdfPath, publicationStore: store });
  const request = (name: string, fields: object = {}) => app.request(`/trpc/${name}?input=${encodeURIComponent(JSON.stringify({
    publicationId: version.publicationId, versionId: version.versionId, ...fields,
  }))}`);
  const response = await request('publications.review');
  assert.equal(response.status, 200);
  const { result: { data } } = await response.json();
  const [remark] = data.remarks;
  assert.deepEqual(remark.options.map(({ value, binding, retrievalTopK }: {
    value: string; binding: string[]; retrievalTopK: number;
  }) => ({ value, binding, retrievalTopK })), [0, 5].map(retrievalTopK => ({ value: '.422', binding, retrievalTopK })));
  for (const option of remark.options) {
    const [valueEvidence] = option.evidenceIds;
    const [conditionEvidence] = option.conditionEvidence.retrieval_top_k;
    for (const [evidenceId, text, page] of [[valueEvidence, '.422', source.page],
      [conditionEvidence, `Top-k=${option.retrievalTopK}`, option.retrievalTopK + 1]]) {
      const sourceResponse = await request('evidence.get', { evidenceId, remarkId: remark.id });
      assert.equal(sourceResponse.status, 200);
      const { result: { data: selected } } = await sourceResponse.json();
      assert.equal(selected.evidence.text, text);
      assert.equal(selected.evidence.page, page);
    }
  }
  const issues = (await store.file(version.publicationId, version.versionId, 'issues.csv'))!.toString();
  assert.match(issues, /Top-k=0.*Top-k=5/);
  assert.equal((await app.request(version.downloadUrl)).status, 200);
});

test('the D001 demo needs the PDF from the same published version', async (t) => {
  const directory = await mkdtemp(join(tmpdir(), 'rag-demo-publication-'));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const run = JSON.parse(await readFile(join(root, 'runs/snapshots/D001/run.json'), 'utf8'));
  const store = createPublicationStore({ directory, root });
  const published = await store.publish({
    publicationId: demoPublicationId, runId: 'demo-version', document: 'D001.pdf', pdfPath,
    pdfSha256: run.source.sha256, coverage: { partial: true, summary: 'Table 1' },
    artifacts: [{ id: 'table-1', runId: 'demo-version', data: run }],
  });
  const app = createApp(async () => null, { pdfPath, publicationStore: store });
  const available = async () => {
    const response = await app.request('/trpc/publications.demoAvailable');
    assert.equal(response.status, 200);
    return (await response.json()).result.data;
  };
  assert.equal(await available(), true);
  await rm(join(directory, demoPublicationId, published.versionId, 'pdf', 'D001.pdf'));
  assert.equal(await available(), false);
});

test('publishes the saved 252 results and a checked export for the same D001 PDF', async (t) => {
  const directory = await mkdtemp(join(tmpdir(), 'rag-publication-'));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const run = JSON.parse(await readFile(join(root, 'runs/snapshots/D001/run.json'), 'utf8'));
  const store = createPublicationStore({ directory, root });
  const input = {
    publicationId: 'D001', runId: 'saved-table-1', document: 'D001.pdf', pdfPath,
    pdfSha256: run.source.sha256,
    coverage: { partial: true, summary: 'Table 1, PDF page 6' },
    artifacts: [{ id: 'table-1', runId: 'saved-table-1', data: run }],
  };
  const [artifact] = input.artifacts;
  const checked = parsePublicationArtifact(artifact.data, artifact.id, input.pdfSha256);
  const automaticInput = { ...input, artifacts: [{ ...artifact, data: checked }] };
  const [published, concurrent] = await Promise.all([store.publish(input), store.publish(automaticInput)]);
  assert.deepEqual(concurrent, published);
  assert.deepEqual(await store.publish(input), published);
  assert.equal(published.coverage.partial, true);
  const dataset = await store.read('D001', published.versionId);
  assert.equal(dataset?.results.length, 252);
  const control = dataset.results.find((row) => row.method === 'ThreadRAG' && row.rawValue === '.422');
  assert.ok(control);
  assert.equal(control.metric, 'EM');
  assert.equal(control.generator, 'LLaMA3-8B-Instruct');
  assert.equal(control.dataset, 'HotpotQA');
  assert.equal(control.value, '0.422');
  assert.match(control.experiment, /Main results/);
  const listed = dataset.results.filter((row) => row.method === 'ThreadRAG' && row.rawValue === '.586');
  assert.equal(listed.length, 2);
  const cells = listed.map((row) => dataset.evidence.find((source) => source.id === row.evidenceIds[0]));
  assert.ok(cells.every((cell) => cell?.page === 6 && cell.box));
  assert.notDeepEqual(cells[0]?.box, cells[1]?.box);
  const report = JSON.parse((await store.file('D001', published.versionId, 'report.json'))!.toString());
  assert.equal(report.exit_code, 0);
  assert.equal(report.rows.results, 252);
  const csv = (await store.file('D001', published.versionId, 'results.csv'))!.toString();
  assert.match(csv, new RegExp(`^D001,${control.id},${control.experimentId},${control.configId},`, 'm'));
  const app = createApp(async () => null, { pdfPath, publicationStore: store });
  const archive = await app.request(published.downloadUrl);
  assert.equal(archive.headers.get('Content-Type'), 'application/zip');
  assert.equal(Buffer.from(await archive.arrayBuffer()).subarray(0, 4).toString('hex'), '504b0304');
  const trpc = (path: string, input: object) => app.request(
    `/trpc/${path}?input=${encodeURIComponent(JSON.stringify(input))}`,
  );
  const pinned = { publicationId: 'D001', versionId: published.versionId };
  for (const row of listed) {
    const reply = await trpc('evidence.get', { ...pinned, evidenceId: row.evidenceIds[0], resultId: row.id });
    assert.equal(reply.status, 200);
    const { result: { data } } = await reply.json();
    assert.equal(data.result.rawValue, '.586');
    assert.deepEqual(data.evidence.box, dataset.evidence.find(item => item.id === row.evidenceIds[0])?.box);
    const pdf = await app.request(data.pdf.url);
    assert.deepEqual(Buffer.from(await pdf.arrayBuffer()), await readFile(pdfPath));
  }
  const badChecker = createPublicationStore({ directory, root, checkerPath: join(directory, 'absent-checker.py') });
  await assert.rejects(badChecker.publish({
    ...input, runId: 'next', artifacts: [{ ...input.artifacts[0], runId: 'next' }],
  }), /Чекер ВИНИТИ/);
  const invalidReport = join(directory, 'invalid-report.py');
  await writeFile(invalidReport, [
    'import pathlib, sys',
    `pathlib.Path(sys.argv[-1]).write_text('{"exit_code": 0}')`,
    'raise SystemExit(2)',
    '',
  ].join('\n'));
  const invalidChecker = createPublicationStore({ directory, root, checkerPath: invalidReport });
  await assert.rejects(invalidChecker.publish({
    ...input, runId: 'invalid-report', artifacts: [{ ...input.artifacts[0], runId: 'invalid-report' }],
  }), /Некорректный отчёт чекера/);
  assert.deepEqual(await readdir(join(directory, 'D001')), [published.versionId]);
  assert.equal((await store.latest('D001'))?.version?.versionId, published.versionId);
  assert.equal((await app.request(published.downloadUrl)).status, 200);
  await assert.rejects(store.publish({
    ...input, runId: 'conflict', artifacts: [{ ...input.artifacts[0], runId: 'another-run' }],
  }), /другому прогону/);
});
