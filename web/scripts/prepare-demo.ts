import { parseArgs } from 'node:util';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { prepareDemo } from '../src/server/prepare-demo.ts';
const root = fileURLToPath(new URL('../', import.meta.url));
const { values } = parseArgs({ options: { pdf: { type: 'string', default: resolve(root, '../docs/context/RAG_ВИНИТИ_участникам_2026-11-20/dev/D001.pdf') } } });
try {
  const dataset = await prepareDemo({ snapshotPath: resolve(root, 'data/d001-run.json'), manifestPath: resolve(root, 'data/manifest.json'), pdfPath: values.pdf!, outDir: resolve(root, '.runtime') });
  console.log(`Prepared ${dataset.results.length} results, PDF SHA-256 verified; run ${dataset.run.id}.`);
} catch (error) { console.error(error instanceof Error ? error.message : 'Demo preparation failed'); process.exitCode = 1; }
