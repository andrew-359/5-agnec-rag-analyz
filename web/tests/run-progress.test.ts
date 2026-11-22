import { test } from 'node:test';
import assert from 'node:assert/strict';
import { runProgress } from '../src/shared/run-progress.ts';
import type { ArticleRun, ArticleRunStage } from '../src/shared/article.ts';

function table(status: ArticleRunStage['status']): ArticleRunStage {
  return { kind: 'table', id: 'table-1', candidateId: 'candidate-1', page: 6, table: 1, strategy: 'docling-tableformer',
    status, resultCount: null, remarkCount: null, ambiguousBindingCount: null, emptyResultReason: null, error: null,
    attempts: [], gridRead: null };
}
function run(): ArticleRun {
  return { id: 'run-1', articleId: 'article-1', document: 'D001.pdf', pdfSha256: 'abc', articleText: null,
    stages: [], figures: [], coverage: null, publicationStatus: 'queued' };
}

test('a running table-grid is current work while queued extraction remains one unfinished table', () => {
  const data = run();
  const stage = table('queued');
  stage.gridRead = { id: 'grid-1', kind: 'table-grid', status: 'running', strategy: 'docling-tableformer', error: null, attempts: [] };
  data.stages = [stage];
  const progress = runProgress(data);
  assert.equal(progress.steps.length, 2);
  assert.equal(progress.completed, 0);
  assert.equal(progress.active[0].id, stage.id);
  assert.match(progress.label, /чтение структуры/);
  assert.equal(progress.pending, true);
  stage.gridRead.status = 'done';
  stage.status = 'running';
  assert.equal(runProgress(data).active[0].note, undefined);
});

test('empty, failed and published releases remain distinct after extraction finishes', () => {
  const data = run();
  data.stages = [{ ...table('done'), resultCount: 0, emptyResultReason: 'Числа не связаны с условиями.' }];
  data.publicationStatus = 'running';
  assert.match(runProgress(data).label, /Результаты из текста/);
  data.publicationStatus = 'empty';
  data.publicationNote = 'Нет подтверждённых результатов';
  const empty = runProgress(data);
  assert.equal(empty.pending, false);
  assert.equal(empty.completed, 2);
  assert.match(empty.label, /нет подтверждённых/);
  assert.equal(empty.steps[0].note, data.stages[0].emptyResultReason);
  data.publicationStatus = 'failed';
  data.publicationError = 'Не удалось сохранить комплект.';
  const failed = runProgress(data);
  assert.equal(failed.failed.length, 1);
  assert.equal(failed.failed[0].error, data.publicationError);
  assert.doesNotMatch(failed.label, /опубликованы/);
  data.publicationStatus = 'done';
  data.publication = { coverage: { partial: true } } as NonNullable<ArticleRun['publication']>;
  assert.match(runProgress(data).label, /частичный охват/);
});

test('failed extraction is terminal without hiding a concurrent figure or pending publication', () => {
  const data = run();
  data.stages = [{ ...table('failed'), error: 'Повреждённый PDF' }];
  data.figures = [{ id: 'figure-1', candidateId: 'figure-candidate', page: 8, label: '3', status: 'running',
    resultCount: null, remarkCount: null, error: null, attempts: [] }];
  const progress = runProgress(data);
  assert.equal(progress.completed, 1);
  assert.equal(progress.failed.length, 1);
  assert.equal(progress.pending, true);
  assert.match(progress.label, /Рисунок 3/);
});
