import type { Dataset } from '../../src/shared/schema.ts';
import type { DatasetLookup } from '../../src/server/app.ts';

/** A synthetic re-extraction of the same preserved PDF, never extra D001 results. */
export function versionedData(current: Dataset) {
  const template = current.results.find((row) => row.method === 'ThreadRAG' && row.dataset === 'HotpotQA' && row.metric === 'EM' && row.generator === 'LLaMA3-8B-Instruct')!;
  const source = current.evidence.find((entry) => entry.id === template.evidenceIds[0])!;
  const runId = 'run-synthetic-archived';
  const evidenceId = 'evidence-synthetic-archived';
  const archived: Dataset = {
    ...structuredClone(current),
    manifest: { ...current.manifest, runId, provenance: { ...current.manifest.provenance, description: 'Synthetic archived extraction of the same PDF for link pinning.' } },
    publication: { ...current.publication, publishedRunId: runId },
    run: { ...current.run, id: runId },
    results: [{ ...template, id: 'result-synthetic-archived', runId, method: 'Synthetic archived result', evidenceIds: [evidenceId] }],
    evidence: [{ ...source, id: evidenceId, runId }],
    remarks: [],
  };
  const readDataset = async (lookup: DatasetLookup) => {
    if (!lookup || lookup.kind === 'publication') return current;
    if (lookup.kind === 'run') return [current, archived].find((entry) => entry.run.id === lookup.runId) ?? null;
    if (lookup.kind === 'evidence') return [current, archived].find((entry) => entry.evidence.some((entry) => entry.id === lookup.evidenceId)) ?? null;
    return lookup.pdfId === current.pdf.id ? current : null;
  };
  return { current, archived, readDataset };
}
