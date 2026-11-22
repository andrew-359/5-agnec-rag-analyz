import { createHash } from 'node:crypto';
import { readFile } from 'node:fs/promises';
import { basename, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { parseArgs } from 'node:util';
import { createPublicationStore, defaultPublicationDirectory } from '../src/server/publication/publication-store.ts';

async function main() {
  const { values } = parseArgs({ options: {
    run: { type: 'string' }, pdf: { type: 'string' }, 'publication-id': { type: 'string' }, store: { type: 'string' },
  } });
  if (!values.run) throw new Error('Укажите --run <каталог сохранённой таблицы>.');
  const root = fileURLToPath(new URL('../../', import.meta.url));
  const directory = resolve(values.run);
  const bytes = await readFile(join(directory, 'run.json'));
  const sha = createHash('sha256').update(bytes).digest('hex');
  const run = JSON.parse(bytes.toString()) as {
    document: string; page: number; table: number; source: { sha256: string };
  };
  if (typeof run.document !== 'string' || !Number.isInteger(run.page) || !Number.isInteger(run.table)) {
    throw new Error('В run.json отсутствует документ, номер страницы или таблицы.');
  }
  const runId = `saved-${sha.slice(0, 40)}`;
  const store = createPublicationStore({ root, directory: values.store || defaultPublicationDirectory(root) });
  const published = await store.publish({
    publicationId: values['publication-id'] || basename(run.document, '.pdf'), runId, document: run.document,
    pdfPath: values.pdf ? resolve(values.pdf) : join(directory, 'source.pdf'), pdfSha256: run.source?.sha256,
    coverage: {
      partial: true, summary: `Table ${run.table}, страница ${run.page} PDF.`,
      tables: [{ table: run.table, page: run.page }],
    },
    artifacts: [{ id: `p${run.page}-t${run.table}`, runId, data: run }],
  });
  console.log(JSON.stringify(published, null, 2));
}

try {
  await main();
} catch (error) {
  console.error((error as Error).message);
  process.exitCode = 1;
}
