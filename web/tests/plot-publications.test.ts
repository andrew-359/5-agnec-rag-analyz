import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { mkdtemp, mkdir, writeFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { plotPublications } from '../src/server/plot-publications.ts';
import { createApp } from '../src/server/app.ts';

test('published downloads use the version manifest and refuse tampered or unknown files', async () => {
  const root = await mkdtemp(join(tmpdir(), 'plot-http-'));
  const version = 'plot-0123456789abcdef0123';
  const folder = join(root, version);
  const csv = 'result_id,metric_value\nR1,0.528\n';
  try {
    await mkdir(folder);
    await writeFile(join(folder, 'results.csv'), csv);
    await writeFile(join(folder, 'manifest.json'), JSON.stringify({ version_id: version, files: {
      'results.csv': createHash('sha256').update(csv).digest('hex'),
    } }));
    const app = plotPublications(root);
    const response = await app.request(`/published/${version}/results.csv`);
    assert.equal(response.status, 200);
    assert.equal(await response.text(), csv);
    assert.equal(response.headers.get('content-disposition'), 'attachment; filename="results.csv"');
    assert.equal((await app.request(`/published/${version}/missing.csv`)).status, 404);
    assert.equal((await app.request('/published/invalid/results.csv')).status, 404);
    assert.equal((await app.request('/published/plot-ffffffffffffffffffff/results.csv')).status, 404);
    await writeFile(join(folder, 'results.csv'), 'edited');
    assert.equal((await app.request(`/published/${version}/results.csv`)).status, 409);
  } finally { await rm(root, { recursive: true, force: true }); }
});

test('main web exposes a versioned graph catalog and only its published observations', async () => {
  const root = await mkdtemp(join(tmpdir(), 'plot-catalog-'));
  const version = 'plot-0123456789abcdef0123';
  const folder = join(root, version);
  const source = { page: 8, box: [.17, .15, .19, .16] };
  const sha = 'a'.repeat(64);
  try {
    await mkdir(folder);
    await writeFile(join(root, 'index.html'), '<html>application shell</html>');
    const run = { kind: 'published-plot', version_id: version, publication_id: 'D001',
      pdf: { sha256: sha, url: '/untrusted-input.pdf' },
      results: [{ id: 'R1', approximate: true, absolute_tolerance: .001,
        parameter: { name: 'anchors', value: 1 }, method: 'ThreadRAG', generator: '', dataset: 'HotpotQA',
        metric: 'EM', raw_value: '~0.528', value: '0.528', unit: 'fraction', experiment: 'E1', task: 'QA', protocol: null,
        source: { ...source, table: null, figure: 'Figure 3' } }],
      coverage: { figure: 'Figure 3', panel: 'a', point_indices: [1], whole_publication: false } };
    const files = {
      'run.json': JSON.stringify(run),
      'plot-input.json': JSON.stringify({ sha256: sha, source, settings: {
        figure: '3', panel: 'a', series: 'HotpotQA', x_axis: 'anchors', y_axis: 'EM', strategy: 'vector-line' },
        points: [{ index: 1, x: 1, y: .528, source }, { index: 2, x: 2, y: .555, source }], issues: [] }),
      'check-report.json': JSON.stringify({ errors: [], warnings: [], exit_code: 0 }),
    };
    for (const [name, value] of Object.entries(files)) await writeFile(join(folder, name), value);
    const hashes = Object.fromEntries(Object.entries(files).map(([name, value]) => [name, createHash('sha256').update(value).digest('hex')]));
    await writeFile(join(folder, 'manifest.json'), JSON.stringify({ version_id: version, files: hashes }));
    const app = createApp(async () => null, { pdfPath: '', assetsDir: root, publishedPlots: root });
    const listed = await (await app.request('/api/published-plots')).json();
    assert.equal(listed.versions[0].version_id, version);
    assert.equal(listed.versions[0].pdf.url, `/published/${version}/source.pdf`);
    assert.equal((await (await app.request('/api/published-plots?sha256=missing')).json()).versions.length, 0);
    const detail = await (await app.request(`/api/published-plots/${version}`)).json();
    assert.equal(detail.run.pdf.url, `/published/${version}/source.pdf`);
    assert.equal(detail.plot.points.length, 1);
    assert.equal(detail.plot.points[0].y, Number(detail.run.results[0].value));
    for (const path of ['/plots', `/plots/${version}`]) {
      const shell = await app.request(path);
      assert.equal(shell.status, 200);
      assert.match(await shell.text(), /application shell/);
    }
    assert.equal((await app.request('/api/published-plots/invalid')).status, 404);
    // Even a self-consistent manifest cannot bind a record to a different point or panel.
    for (const changed of [
      { ...run, coverage: { ...run.coverage, panel: 'b' } },
      { ...run, results: [{ ...run.results[0], parameter: { name: 'anchors', value: 2 } }] },
      { ...run, results: [{ ...run.results[0], source: { ...run.results[0].source, box: [.2, .2, .3, .3] } }] },
    ]) {
      const value = JSON.stringify(changed);
      await writeFile(join(folder, 'run.json'), value);
      hashes['run.json'] = createHash('sha256').update(value).digest('hex');
      await writeFile(join(folder, 'manifest.json'), JSON.stringify({ version_id: version, files: hashes }));
      assert.equal((await app.request(`/api/published-plots/${version}`)).status, 409);
    }
    await writeFile(join(folder, 'run.json'), JSON.stringify({ ...run, results: [] }));
    assert.equal((await app.request(`/api/published-plots/${version}`)).status, 409);
    const corrupted = await (await app.request('/api/published-plots')).json();
    assert.deepEqual(corrupted.unavailable, [version]);
    assert.deepEqual(corrupted.versions, []);
  } finally { await rm(root, { recursive: true, force: true }); }
});
