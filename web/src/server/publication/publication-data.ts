import { basename } from 'node:path';
import { z } from 'zod';
import { boxSchema, datasetSchema, digitizationSchema, idSchema, parametersSchema, type Dataset, type Evidence, type Result } from '../../shared/schema.ts';
import { coverageSchema, type PublishedVersion, submissionPublicationIdSchema, versionUrl } from '../../shared/publication.ts';
import { savedRemarkOptionSchema } from '../../shared/saved-run.ts';
import { measurementRelationshipsSchema } from '../../shared/reconciliation.ts';
import { applyReconciliation } from './publication-reconciliation.ts';

const hash = z.string().regex(/^[a-f0-9]{64}$/);
const page = z.number().int().positive();
const figure = z.union([z.string().min(1), page]).transform(String).nullish();
const source = z.object({
  page, table: page.nullish(), figure, fragment: z.string().optional(),
  section: z.string().optional(),
  row: z.number().int().nonnegative().nullish(),
  column: z.number().int().nonnegative().nullish(),
  headers: z.array(z.string()).default([]), box: boxSchema.nullish(),
  evidence_ids: z.array(idSchema).default([]),
});
const linked = {
  id: idSchema,
  source,
  source_fragment: z.string().nullish(),
  condition_sources: z.record(z.string(), z.array(idSchema)).nullish(),
};
const normalized = z.string().regex(/^[+-]?(?:\d+(?:\.\d+)?|\.\d+)$/);
const artifactData = z.object({
  source: z.object({ sha256: hash }),
  page: page.nullish(),
  table: page.nullish(),
  region: z.object({ caption_status: z.string(), designation: z.string().min(1).nullable() }).optional(),
  strategy: z.string().optional(),
  versions: z.record(z.string(), z.string()).optional(),
  settings: z.record(z.string(), z.unknown()).optional(),
  evidence: z.array(z.object({
    id: idSchema, page, table: page.nullish(), text: z.string().default(''), box: boxSchema.nullish(), strategy: z.string().optional(),
    precision: z.enum(['cell', 'estimated_cell', 'table', 'page', 'text_fragment', 'figure', 'plot_point']),
  })).default([]),
  results: z.array(z.object({
    ...linked,
    method: z.string(), generator: z.string().nullish(), dataset: z.string(),
    metric: z.string().min(1), task: z.string(), experiment: z.string(),
    protocol: z.string().nullish(), raw_value: z.string().min(1), value: normalized.nullable(),
    unit: z.string().nullish(), statistic: z.string().nullish(),
    retrieval_top_k: z.number().int().nonnegative().nullish(),
    components: z.record(z.string(), z.boolean()).nullish(),
    result_origin: z.enum(['primary', 'cited']).optional(),
    cited_publication_id: z.string().optional(),
    metric_k: z.number().int().positive().nullish(),
    metric_direction: z.enum(['higher_is_better', 'lower_is_better', 'target']).nullish(),
    metric_group: z.string().nullish(), metric_variant: z.string().nullish(), baseline_value: normalized.nullish(),
    note: z.string().nullish(),
    partial: z.literal(true).optional(),
    parameters: parametersSchema.optional(), digitization: digitizationSchema.optional(),
  })),
  remarks: z.array(z.object({
    ...linked,
    kind: z.string().nullish(), reason: z.string().min(1),
    raw_text: z.string(), options: z.array(savedRemarkOptionSchema).optional(),
    field: z.string().nullish(), conditions: z.record(z.string(), z.unknown()).optional(),
    statistic: z.string().nullish(),
    result_ids: z.array(idSchema).default([]),
  })),
});

const verifiedArtifact = Symbol('verified publication artifact');

export function parsePublicationArtifact(raw: unknown, id: string, pdfSha256: string) {
  const fail = (detail: string): never => { throw new Error(`Артефакт ${id}: ${detail}`); };
  const checked = artifactData.safeParse(raw);
  if (!checked.success) return fail(checked.error.issues.map(issue => issue.path.join('.') + ' — ' + issue.message).join('; '));
  const data = checked.data;
  if (data.source.sha256 !== pdfSha256) fail('относится к другой версии PDF.');
  const fragments = new Map(data.evidence.map(item => [item.id, item]));
  if (fragments.size !== data.evidence.length) fail('повторяются идентификаторы источника.');
  const fragment = (ref: string) => fragments.get(ref) ?? fail(`отсутствует источник ${ref}.`);
  const resultIds = new Set(data.results.map(row => row.id));
  const remarkIds = new Set(data.remarks.map(row => row.id));
  if (resultIds.size !== data.results.length) fail('повторяются идентификаторы результатов.');
  if (remarkIds.size !== data.remarks.length) fail('повторяются идентификаторы замечаний.');
  for (const row of [...data.results, ...data.remarks]) {
    const address = row.source;
    if (data.table != null && (address.table !== data.table || (data.page != null && address.page !== data.page))) {
      fail(`источник ${row.id} относится к другой таблице или странице.`);
    }
    const refs = address.evidence_ids.map(fragment);
    if (refs.some(item => item.page !== address.page)) fail(`несовместимые ссылки на страницу ${address.page}.`);
    const valueSource = refs[0];
    if (address.box && valueSource?.box && address.box.some((edge, index) => Math.abs(edge - valueSource.box![index]) > 1e-6)) {
      fail(`несовместимые координаты источника ${valueSource.id}.`);
    }
    for (const ref of Object.values(row.condition_sources ?? {}).flat()) fragment(ref);
    if ('result_ids' in row) {
      for (const ref of row.result_ids) if (!resultIds.has(ref)) fail(`замечание ${row.id} ссылается на отсутствующий результат ${ref}.`);
      if (row.kind === 'reading-conflict') for (const option of row.options ?? []) {
        if (option.evidence_ids.map(fragment).some(item => item.page !== address.page)) {
          fail(`несовместимые ссылки на страницу ${address.page} у варианта чтения.`);
        }
        for (const ref of Object.values(option.condition_sources ?? {}).flat()) fragment(ref);
      }
      if (row.kind === 'partial-measurement') for (const option of row.options ?? []) {
        for (const ref of [...option.evidence_ids, ...Object.values(option.condition_sources ?? {}).flat()]) fragment(ref);
      }
    } else if (address.table == null && address.figure == null && !(row.source_fragment || address.fragment || valueSource?.text)?.trim()) {
      fail(`результат ${row.id} не содержит таблицу, рисунок или текст источника.`);
    }
  }
  return Object.freeze({ [verifiedArtifact]: true as const, id, pdfSha256, raw, data, fragment });
}

function isVerifiedArtifact(value: unknown): value is ReturnType<typeof parsePublicationArtifact> {
  return typeof value === 'object' && value !== null && verifiedArtifact in value && value[verifiedArtifact] === true;
}

export function rawPublicationArtifact(value: unknown) {
  return isVerifiedArtifact(value) ? value.raw : value;
}

export const publicationInputSchema = z.object({
  publicationId: idSchema,
  runId: idSchema,
  document: z.string().refine(value => value.trim().length > 0),
  pdfPath: z.string().min(1),
  pdfSha256: hash,
  publication: z.object({
    title: z.string().optional(), year: z.number().int().positive().optional(),
    DOI: z.string().optional(), authors: z.string().optional(),
  }).optional(),
  coverage: coverageSchema,
  artifacts: z.array(z.object({ id: idSchema, runId: idSchema, data: z.unknown() })).min(1),
  relationships: measurementRelationshipsSchema.optional(),
}).transform(input => {
  const filename = basename(input.document.replaceAll('\\', '/'));
  const stem = /\.pdf$/i.test(filename) ? filename.slice(0, -4) : '';
  const checked = submissionPublicationIdSchema.safeParse(stem);
  // Preserve the PDF name when the checker can use it. A content ID is deterministic for other uploads.
  const submissionPublicationId = checked.success ? checked.data : `publication-${input.pdfSha256}`;
  return { ...input, submissionPublicationId: submissionPublicationId === input.publicationId ? undefined : submissionPublicationId };
});

export type PublicationInput = z.input<typeof publicationInputSchema>;
type Input = z.output<typeof publicationInputSchema>;
type ExportRow = Record<string, string | number | null | undefined>;
export const columns = {
  publications: 'publication_id,title,year,DOI,authors,in_scope,exclusion_reason',
  configurations: 'publication_id,config_id,rag_variant,retrieval_method,retriever_model,embedding_model,embedding_version,chunking_method,chunk_size,chunk_overlap,chunk_unit,index_type,vector_store,top_k,reranker_model,generator_model,generator_version,prompt_strategy,context_strategy,note',
  datasets: 'publication_id,dataset_id,dataset_name,dataset_version,language,data_split,scientific_domain,note',
  tasks: 'publication_id,task_id,task_type,note',
  experiments: 'publication_id,experiment_id,evaluation_protocol,baseline_name,hardware,latency_ms,tokens_per_query,sample_size,note',
  results: 'publication_id,result_id,experiment_id,config_id,dataset_id,task_id,metric_group,metric_name,metric_variant,metric_direction,metric_k,metric_value,metric_unit,baseline_value,aggregation,sample_size,raw_value,raw_unit,normalized_value,normalized_unit,result_origin,cited_publication_id,source_page,source_section,source_table,source_figure,source_fragment,extraction_status,duplicate_of,note',
  issues: 'publication_id,issue_id,record_ids,check_name,description,source_page,source_fragment',
};
export type ExportTables = Record<keyof typeof columns, ExportRow[]>;

function entities(rows: ExportRow[], field: string, prefix: string) {
  const keys = new Map<string, string>();
  return (identity: unknown[], row: ExportRow) => {
    const key = JSON.stringify(identity);
    const previous = keys.get(key);
    if (previous) return previous;
    const id = `${prefix}${keys.size + 1}`;
    keys.set(key, id);
    rows.push({ ...row, [field]: id });
    return id;
  };
}

export function buildPublication(input: Input, versionId: string, snapshotSha256: string) {
  const { publicationId, submissionPublicationId, runId, document, pdfSha256, coverage } = input;
  const url = versionUrl(publicationId, versionId);
  const version: PublishedVersion = {
    publicationId, runId, versionId, pdfSha256, coverage, url, downloadUrl: `/api${url}/export.zip`,
    ...(submissionPublicationId === undefined ? {} : { submissionPublicationId }),
  };
  const pdfId = `pdf-${versionId}`;
  const provenance = { kind: 'saved-extraction' as const, description: 'Сохранённые результаты извлечения', processorVersion: null };
  const manifest = { schemaVersion: 1 as const, publicationId, pdfId, runId: versionId, document, pdfSha256, snapshotSha256, provenance };
  const dataset: Dataset = {
    version, manifest,
    publication: { id: publicationId, title: input.publication?.title || document, pdfId, publishedRunId: versionId },
    pdf: { id: pdfId, sha256: pdfSha256, url: `/api${url}/source.pdf` },
    run: { id: versionId, schemaVersion: 1, publicationId, pdfId, provenance },
    results: [], evidence: [], remarks: [],
  };
  const tables: ExportTables = {
    publications: [{ ...input.publication, in_scope: 'true' }],
    configurations: [], datasets: [], tasks: [], experiments: [], results: [], issues: [],
  };
  const config = entities(tables.configurations, 'config_id', 'C');
  const datasetIdFor = entities(tables.datasets, 'dataset_id', 'D');
  const task = entities(tables.tasks, 'task_id', 'T');
  const experiment = entities(tables.experiments, 'experiment_id', 'E');
  const artifactIds = new Set<string>();
  const origin: Array<{ id: string; runId: string; strategy?: string; versions?: Record<string, string>; settings?: Record<string, unknown> }> = [];
  const rowOrigins: Record<string, { artifactId: string; sourceId: string }> = {};
  const sourceOrigins: Record<string, { artifactId: string; evidenceId?: string }> = {};

  for (const artifact of input.artifacts) {
    if (artifactIds.has(artifact.id)) throw new Error(`Повторяется идентификатор артефакта ${artifact.id}.`);
    artifactIds.add(artifact.id);
    if (artifact.runId !== runId) throw new Error(`Артефакт ${artifact.id} относится к другому прогону: ${artifact.runId}.`);
    const checked = isVerifiedArtifact(artifact.data)
      ? artifact.data
      : parsePublicationArtifact(artifact.data, artifact.id, pdfSha256);
    if (checked.id !== artifact.id || checked.pdfSha256 !== pdfSha256) {
      throw new Error(`Артефакт ${artifact.id} был проверен для другого этапа или PDF.`);
    }
    const { data, fragment } = checked;
    origin.push({ id: artifact.id, runId, strategy: data.strategy, versions: data.versions, settings: data.settings });

    const conditionFragments = new Map<string, Evidence>();
    const anchors = new Map<string, Evidence>();
    const evidence = (address: z.output<typeof source>, localId?: string, quote?: string | null) => {
      const item = localId ? fragment(localId) : undefined;
      const id = `S${dataset.evidence.length + 1}`;
      const entry: Evidence = {
        id, runId: versionId, pdfId, page: item?.page ?? address.page,
        table: address.table ?? null,
        tableDesignation: address.table != null && data.region?.caption_status === 'confirmed'
          ? data.region.designation ?? undefined : undefined,
        figure: address.figure ?? undefined,
        row: address.row ?? null, column: address.column ?? null, headers: address.headers,
        box: item?.box ?? address.box ?? null,
        precision: item?.precision ?? (address.box ? (address.table ? 'cell' : address.figure ? 'figure' : 'text_fragment') : 'page'),
        text: quote || address.fragment || item?.text,
        strategy: item?.strategy,
      };
      const key = localId && JSON.stringify([localId, entry.table, entry.figure, entry.row, entry.column, entry.headers, entry.box, entry.text]);
      const previous = key ? anchors.get(key) : undefined;
      if (previous) return previous;
      if (key) anchors.set(key, entry);
      dataset.evidence.push(entry);
      sourceOrigins[id] = { artifactId: artifact.id, evidenceId: localId };
      return entry;
    };

    const locateConditions = (conditions?: Record<string, string[]> | null) =>
      Object.fromEntries(Object.entries(conditions ?? {}).map(([field, ids]) => [
        field,
        ids.map((id) => {
          const item = fragment(id);
          let linked = conditionFragments.get(id);
          if (!linked) {
            linked = evidence({ page: item.page, table: item.table, headers: [], evidence_ids: [] }, id);
            conditionFragments.set(id, linked);
          }
          return linked.id;
        }),
      ]));

    const locate = (row: { source: z.output<typeof source>; source_fragment?: string | null; condition_sources?: Record<string, string[]> | null }) => {
      const { source: address } = row;
      const [first, ...rest] = address.evidence_ids;
      const primary = evidence(address, first, row.source_fragment);
      const others = rest.map(id => evidence(address, id));
      const conditionEvidence = locateConditions(row.condition_sources);
      const evidenceIds = [...new Set([primary.id, ...others.map((item) => item.id), ...Object.values(conditionEvidence).flat()])];
      return { primary, evidenceIds, conditionEvidence };
    };
    const resultIds = new Map<string, string>();
    for (const row of data.results) {
      const id = `R${dataset.results.length + 1}`;
      resultIds.set(row.id, id);
      rowOrigins[id] = { artifactId: artifact.id, sourceId: row.id };
      const { primary, evidenceIds, conditionEvidence } = locate(row);
      const components = Object.entries(row.components ?? {}).sort(([a], [b]) => a.localeCompare(b));
      const parameters = Object.entries(row.parameters ?? {}).sort(([a], [b]) => a.localeCompare(b));
      const configId = !row.partial || row.method || row.generator || row.retrieval_top_k != null || components.length || parameters.length
        ? config([row.method, row.generator ?? '', row.retrieval_top_k ?? null, components, parameters], {
        rag_variant: row.method, generator_model: row.generator, top_k: row.retrieval_top_k,
        note: [...components, ...parameters].map(([name, value]) => `${name}=${value}`).join(', '),
      }) : undefined;
      const experimentId = !row.partial || row.experiment || row.protocol
        ? experiment([row.experiment, row.protocol ?? null], { evaluation_protocol: row.protocol, note: row.experiment }) : undefined;
      const datasetId = !row.partial || row.dataset ? datasetIdFor([row.dataset], { dataset_name: row.dataset }) : undefined;
      const taskId = !row.partial || row.task ? task([row.task], { task_type: row.task }) : undefined;
      const metricVariant = row.metric_variant ?? (['decrease', 'increase'].includes(row.statistic ?? '') ? row.statistic : null);
      const result: Result = {
        id, runId: versionId, evidenceIds, conditionEvidence,
        method: row.method, generator: row.generator ?? '', dataset: row.dataset, metric: row.metric,
        experiment: row.experiment, task: row.task, protocol: row.protocol ?? null,
        rawValue: row.raw_value, value: row.value, unit: row.unit ?? null, statistic: row.statistic ?? null,
        metricGroup: row.metric_group ?? undefined, metricVariant: metricVariant ?? undefined,
        baselineValue: row.baseline_value ?? undefined,
        retrievalTopK: row.retrieval_top_k ?? null, configId, experimentId,
        parameters: row.parameters, digitization: row.digitization,
        partial: row.partial,
      };
      dataset.results.push(result);
      const normalizedUnit = row.value === null ? null : row.unit;
      const printedUnit = row.raw_value.match(/%\s*\)?$/) ? '%' : null;
      tables.results.push({
        result_id: id, experiment_id: experimentId, config_id: configId, dataset_id: datasetId, task_id: taskId,
        metric_name: row.metric, metric_group: row.metric_group, metric_variant: metricVariant, baseline_value: row.baseline_value,
        metric_direction: row.metric_direction, metric_k: row.metric_k, metric_value: row.value,
        metric_unit: normalizedUnit, aggregation: row.statistic, raw_value: row.raw_value, raw_unit: printedUnit,
        normalized_value: row.value, normalized_unit: normalizedUnit,
        result_origin: row.result_origin ?? 'primary', cited_publication_id: row.cited_publication_id,
        source_page: primary.page, source_section: row.source.section,
        source_table: primary.tableDesignation ?? (primary.table == null ? undefined : `Table ${primary.table}`),
        source_figure: primary.figure == null ? undefined : `Figure ${primary.figure}`,
        source_fragment: primary.precision === 'plot_point' ? row.source_fragment || row.source.fragment : primary.text,
        extraction_status: 'extracted',
        note: [
          row.source.headers.join(' → '), row.source.row == null ? null : row.method,
          primary.precision === 'plot_point' ? 'Приближённое значение по оцифрованному графику' : null,
          row.digitization ? `Метод ${row.digitization.method}, шаг ${row.digitization.resolution}` : null,
          row.digitization?.series ? `Серия ${row.digitization.series}` : null,
          row.note,
        ].filter(Boolean).join(' · '),
      });
    }

    for (const row of data.remarks) {
      const id = `I${dataset.remarks.length + 1}`;
      rowOrigins[id] = { artifactId: artifact.id, sourceId: row.id };
      const { primary, evidenceIds } = locate(row);
      const linkedResults = row.result_ids.map(ref => resultIds.get(ref)!);
      const options = (row.kind === 'reading-conflict' || row.kind === 'partial-measurement') && row.options?.length ? row.options.map(option => ({
        value: option.value, binding: option.binding, retrievalTopK: option.retrieval_top_k,
        evidenceIds: row.kind === 'reading-conflict' ? option.evidence_ids.map(ref => evidence(row.source, ref).id)
          : locateConditions({ option: option.evidence_ids }).option,
        conditionEvidence: locateConditions(option.condition_sources),
      })) : undefined;
      const optionIds = options?.flatMap(option => [...option.evidenceIds, ...Object.values(option.conditionEvidence).flat()]) ?? [];
      dataset.remarks.push({ id, runId: versionId, reason: row.reason, rawText: row.raw_text,
        evidenceIds: [...new Set([...evidenceIds, ...optionIds])], resultIds: linkedResults, options });
      const showTopK = options?.some(option => option.retrievalTopK != null);
      const alternatives = options?.map(option => [String(option.value), ...(option.binding ?? []),
        ...(showTopK ? [`Top-k=${option.retrievalTopK ?? 'не указан'}`] : [])].join(' · '));
      tables.issues.push({
        issue_id: id, record_ids: linkedResults.join(';'),
        check_name: row.kind === 'partial-measurement' && row.field ? row.field : row.kind ?? 'extraction',
        description: alternatives ? `${row.reason} ${alternatives.join(' / ')}`
          : row.raw_text ? `${row.reason} (${row.raw_text})` : row.reason,
        source_page: primary.page, source_fragment: primary.text,
      });
    }
  }
  if (input.relationships?.length) applyReconciliation(dataset, tables, rowOrigins, input.relationships);
  if (!dataset.results.length) throw new Error('Нет извлечённых результатов для публикации.');
  return { dataset: datasetSchema.parse(dataset), tables, origin: { artifacts: origin, records: rowOrigins, sources: sourceOrigins } };
}
