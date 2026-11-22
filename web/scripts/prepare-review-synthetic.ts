import { createHash } from 'node:crypto';
import { mkdir, writeFile } from 'node:fs/promises';
import { join } from 'node:path';

const directory = process.argv[2];
if (!directory) throw new Error('Укажите каталог для учебного прогона.');

// A single artificial result and a blank PDF page. No publication supplied this data.
const objects = [
  '<< /Type /Catalog /Pages 2 0 R >>',
  '<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
  '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] >>',
];
let pdf = '%PDF-1.4\n';
const offsets = objects.map((object, index) => {
  const offset = pdf.length;
  pdf += `${index + 1} 0 obj\n${object}\nendobj\n`;
  return `${String(offset).padStart(10, '0')} 00000 n \n`;
});
pdf += `xref\n0 4\n0000000000 65535 f \n${offsets.join('')}trailer\n<< /Size 4 /Root 1 0 R >>\nstartxref\n${pdf.length}\n%%EOF\n`;
const contents = JSON.stringify({
  document: 'Synthetic review example',
  page: 1, table: 1, source: { sha256: createHash('sha256').update(pdf).digest('hex') },
  results: [{
    id: 'synthetic-result', method: 'Toy method', generator: 'Toy model', dataset: 'Toy data', metric: 'Toy score',
    raw_value: '42', value: '42', unit: null, experiment: 'Synthetic example', task: 'Demonstration', protocol: null,
    source: { page: 1, table: 1 },
    condition_sources: { generator: ['synthetic-source'] },
  }],
  remarks: [],
  evidence: [{
    id: 'synthetic-source', page: 1, box: null, precision: 'page', text: 'The toy method uses the toy model.',
  }],
});

await mkdir(directory, { recursive: true });
await Promise.all([
  writeFile(join(directory, 'source.pdf'), pdf, { flag: 'wx' }),
  writeFile(join(directory, 'run.json'), contents, { flag: 'wx' }),
]);
