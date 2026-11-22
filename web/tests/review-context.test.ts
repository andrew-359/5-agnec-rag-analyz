import { test } from 'node:test';
import assert from 'node:assert/strict';
import { reviewContext, resultConditions } from '../src/shared/review-context.ts';
import { resultSchema, evidenceSchema } from '../src/shared/schema.ts';

const result = resultSchema.parse({ id: 'r1', runId: 'v1', method: 'A', generator: '', dataset: 'HotpotQA', metric: 'EM', task: '', experiment: '',
  protocol: 'zero-shot', unit: null, statistic: null, rawValue: '.422', value: '0.422', retrievalTopK: 0,
  evidenceIds: ['cell', 'protocol'], conditionEvidence: { protocol: ['protocol'], retrieval_top_k: ['foreign'] } });
const evidence = ['cell', 'protocol', 'foreign'].map(id => evidenceSchema.parse({ id, runId: 'v1', pdfId: 'pdf', page: 6, table: 1, precision: 'cell' }));
const data = { results: [result], evidence, remarks: [{ id: 'm1', runId: 'v1', reason: 'Проверить условие', rawText: '.422', evidenceIds: ['cell'], resultIds: ['r1'] }] };

test('switching from a remark to a condition creates mutually exclusive requests and retains explicit result binding', () => {
  const remark = reviewContext(data, { remarkId: 'm1' });
  assert.equal(remark.request?.remarkId, 'm1');
  assert.equal(remark.request?.resultId, undefined);
  const condition = reviewContext(data, { remarkId: 'm1', resultId: 'r1', evidenceId: 'protocol', sourceMode: 'condition' });
  assert.equal(condition.request?.resultId, 'r1');
  assert.equal(condition.request?.remarkId, undefined);
  assert.equal(condition.remark?.id, 'm1');
  assert.ok(reviewContext(data, { remarkId: 'm1', evidenceId: 'foreign' }).error);
  assert.ok(reviewContext(data, { remarkId: 'm1', resultId: 'other' }).error);
});

test('shared source or number never creates a result link; no-source remarks remain readable', () => {
  const input = { ...data, remarks: [{ ...data.remarks[0], resultIds: [], evidenceIds: [] }] };
  const context = reviewContext(input, { remarkId: 'm1' });
  assert.equal(context.error, undefined);
  assert.equal(context.result, undefined);
  assert.equal(context.request, undefined);
  assert.deepEqual(context.linked, []);
});

test('multiple explicit results require selection; zero survives and unrelated condition sources are excluded', () => {
  const input = { ...data, results: [result, { ...result, id: 'r2', statistic: 'sample_sd' }], remarks: [{ ...data.remarks[0], resultIds: ['r1', 'r2'] }] };
  assert.equal(reviewContext(input, { remarkId: 'm1' }).result, undefined);
  assert.equal(reviewContext(input, { remarkId: 'm1', resultId: 'r2' }).result?.statistic, 'sample_sd');
  const topK = resultConditions(result).find(field => field.key === 'retrieval_top_k')!;
  assert.equal(topK.value, 0);
  assert.deepEqual(topK.evidenceIds, []);
});
