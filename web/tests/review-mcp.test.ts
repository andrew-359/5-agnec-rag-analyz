import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { test } from 'node:test';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js';
import { CallToolResultSchema } from '@modelcontextprotocol/sdk/types.js';

const box = [.4, .2, .5, .3];
const pdf = '%PDF-1.4\nSynthetic test source\n';
const pdfSha256 = createHash('sha256').update(pdf).digest('hex');
const excerpt = 'The primary backbone is Qwen2.5-7B-Instruct for PRO-STEP and most baselines. We set k = 3 across all methods.';
const run = {
  document: 'D009', page: 5, table: 1, source: { sha256: pdfSha256 },
  results: [{
    id: 'result-25', method: 'PRO-STEP', generator: 'Qwen2.5-7B-Instruct', dataset: 'HotpotQA', metric: 'EM',
    raw_value: '38.7', value: '38.7', unit: null, experiment: 'Table 1', task: 'QA', protocol: null,
    retrieval_top_k: 3, source: { page: 5, table: 1, row: 12, column: 4, box },
    condition_sources: { generator: ['setup'], retrieval_top_k: ['setup'] },
  }],
  remarks: [{
    id: 'remark-26', kind: 'ambiguous-binding', field: 'generator', reason: 'Неясна применимость к FLARE.',
    raw_text: '18.8', source: { page: 5, table: 1, row: 4, column: 4, box },
    conditions: { method: 'FLARE', dataset: 'HotpotQA', metric: 'EM', retrieval_top_k: 3 },
    condition_sources: { retrieval_top_k: ['setup'] },
    options: [{ value: 'Qwen2.5-7B-Instruct', evidence_ids: ['setup'] }],
  }],
  evidence: [
    { id: 'setup', page: 5, box: [.1, .6, .5, .9], precision: 'text_fragment', text: excerpt },
    { id: 'unrelated', page: 5, box: null, precision: 'page', text: 'Unrelated evidence' },
  ],
};

test('MCP exposes two review cases and checks the allowed artifact on every stdio call', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'rag-mcp-'));
  const runPath = join(directory, 'run.json');
  const pdfPath = join(directory, 'source.pdf');
  const journal = join(directory, 'mcp.jsonl');
  const contents = JSON.stringify(run);
  await writeFile(runPath, contents);
  await writeFile(pdfPath, pdf);
  const artifactId = createHash('sha256').update(contents).digest('hex');
  const client = new Client({ name: 'rag-review-test', version: '1.0.0' });
  try {
    await client.connect(new StdioClientTransport({
      command: process.execPath,
      args: [resolve('src/server/review-mcp.ts')],
      env: { RAG_RUN_DIR: directory, RAG_MCP_JOURNAL: journal },
    }));
    assert.deepEqual((await client.listTools()).tools.map(tool => tool.name), ['get_review_case']);
    const call = async (args: Record<string, unknown>) => CallToolResultSchema.parse(await client.callTool({
      name: 'get_review_case', arguments: args,
    }));
    const remark = { artifactId, selection: { kind: 'remark', remarkId: `${artifactId}:remark-26` } };
    const response = await call(remark);
    assert.equal(response.isError, undefined);
    assert.deepEqual(response.structuredContent?.record, { method: 'FLARE', dataset: 'HotpotQA', metric: 'EM', rawValue: '18.8' });
    assert.deepEqual(response.structuredContent?.cell, { page: 5, table: 1, row: 4, column: 4, box });
    assert.deepEqual(response.structuredContent?.confirmed, [{ field: 'retrieval_top_k', value: 3, sourceIds: ['setup'] }]);
    assert.deepEqual(response.structuredContent?.openField, {
      field: 'generator', reason: 'Неясна применимость к FLARE.',
      options: [{ value: 'Qwen2.5-7B-Instruct', sourceIds: ['setup'] }],
    });
    assert.deepEqual(response.structuredContent?.sources, [
      { id: 'setup', page: 5, box: [.1, .6, .5, .9], precision: 'text_fragment', text: excerpt },
    ]);

    const result = await call({ artifactId, selection: { kind: 'result', resultId: `${artifactId}:result-25`, field: 'generator' } });
    assert.equal(result.isError, undefined);
    assert.deepEqual(result.structuredContent?.record, { method: 'PRO-STEP', dataset: 'HotpotQA', metric: 'EM', rawValue: '38.7' });
    assert.equal(result.structuredContent?.openField, null);
    assert.deepEqual(result.structuredContent?.cell, { page: 5, table: 1, row: 12, column: 4, box });
    assert.deepEqual(result.structuredContent?.confirmed, [
      { field: 'generator', value: 'Qwen2.5-7B-Instruct', sourceIds: ['setup'] },
      { field: 'retrieval_top_k', value: 3, sourceIds: ['setup'] },
    ]);
    assert.equal((await call({ ...remark, path: '../other-run' })).isError, true);
    assert.equal((await call({ ...remark, selection: { kind: 'result', resultId: `${artifactId}:result-25`, field: 'url' } })).isError, true);

    await writeFile(pdfPath, 'Replaced source');
    assert.equal((await call(remark)).isError, true);
    await rm(pdfPath);
    assert.equal((await call(remark)).isError, true);
    await writeFile(pdfPath, pdf);
    const withoutSha = JSON.stringify({ ...run, source: { sha256: 'invalid' } });
    const withoutShaId = createHash('sha256').update(withoutSha).digest('hex');
    await writeFile(runPath, withoutSha);
    assert.equal((await call({
      artifactId: withoutShaId, selection: { kind: 'remark', remarkId: `${withoutShaId}:remark-26` },
    })).isError, true);

    // A second version uses the same local row IDs. Neither an old version
    // nor an old version's row may silently resolve to the replacement.
    const replacement = JSON.stringify({ ...run, document: 'Second version' });
    const replacementId = createHash('sha256').update(replacement).digest('hex');
    await writeFile(runPath, replacement);
    assert.equal((await call(remark)).isError, true);
    assert.equal((await call({ ...remark, artifactId: replacementId })).isError, true);
    assert.equal((await call({
      artifactId: replacementId, selection: { kind: 'remark', remarkId: `${replacementId}:remark-26` },
    })).isError, undefined);
    await rm(runPath);
    assert.equal((await call({ ...remark, artifactId: replacementId })).isError, true);

    const events = (await readFile(journal, 'utf8')).trim().split('\n').map(line => JSON.parse(line));
    assert.ok(events.every(event => event.tool === 'get_review_case'));
    assert.deepEqual(events.map(event => event.outcome), [
      'ok', 'ok', 'invalid_input', 'invalid_input', 'pdf_unavailable', 'pdf_unavailable', 'pdf_unavailable',
      'artifact_changed', 'record_unavailable', 'ok', 'artifact_unavailable',
    ]);
  } finally {
    await client.close();
    await rm(directory, { recursive: true, force: true });
  }
});
