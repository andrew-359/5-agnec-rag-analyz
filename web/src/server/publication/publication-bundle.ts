import { execFile } from 'node:child_process'
import { access, mkdir, readFile, writeFile } from 'node:fs/promises'
import { join } from 'node:path'
import { promisify } from 'node:util'
import { columns, type buildPublication } from './publication-data.ts'

const execute = promisify(execFile)
const csvFiles = Object.keys(columns).map((name) => `${name}.csv`)
export const publicationBundleFiles = [...csvFiles, 'PASSPORT.md', 'report.json', 'manifest.json']
const json = (value: unknown) => JSON.stringify(value, null, 2) + '\n'
const csv = (value: string | number | null | undefined) => {
  const text = String(value ?? '')
  return /[,"\n\r]/.test(text) ? '"' + text.replaceAll('"', '""') + '"' : text
}

type PublicationFiles = ReturnType<typeof buildPublication> & {
  pdf: Uint8Array
  submissionPublicationId: string
  snapshotSha256: string
}

type SubmissionTools = { root: string; checkerPath: string; python?: string }

export async function writePublicationBundle(
  folder: string,
  { dataset, tables, origin, pdf, submissionPublicationId, snapshotSha256 }: PublicationFiles,
  options: SubmissionTools,
) {
  const bundle = join(folder, 'bundle')
  const pdfDir = join(folder, 'pdf')
  await Promise.all([mkdir(bundle), mkdir(pdfDir)])
  await Promise.all([
    writeFile(join(pdfDir, `${submissionPublicationId}.pdf`), pdf),
    ...Object.entries(columns).map(([key, header]) => {
      const table = key as keyof typeof columns
      const fields = header.split(',')
      const output = tables[table].map(row => fields
        .map(field => csv(field === 'publication_id' ? submissionPublicationId : row[field]))
        .join(','))
      return writeFile(join(bundle, `${table}.csv`), [header, ...output, ''].join('\r\n'))
    }),
  ])

  const python = options.python || process.env.RAG_PYTHON || await access(join(options.root, '.venv/bin/python'))
    .then(() => join(options.root, '.venv/bin/python'), () => 'python3')
  const checkerExit = await checkSubmission(python, options.checkerPath, bundle, pdfDir)
  const publishedAt = new Date().toISOString()
  await Promise.all([
    writeFile(join(folder, 'dataset.json'), json(dataset)),
    writeFile(join(bundle, 'manifest.json'), json({ ...dataset.version, snapshotSha256, publishedAt, ...origin })),
    writeFile(join(bundle, 'PASSPORT.md'), passport(dataset, checkerExit)),
  ])
  await execute(
    python,
    ['-m', 'zipfile', '-c', join(folder, 'export.zip'), ...publicationBundleFiles],
    { cwd: bundle, timeout: 120_000 },
  )
}

async function checkSubmission(python: string, checkerPath: string, bundle: string, pdfDir: string) {
  const reportPath = join(bundle, 'report.json')
  let exitCode = 0
  let checkerOutput = ''
  try {
    await execute(python, [checkerPath, bundle, pdfDir, '--json-report', reportPath], { timeout: 120_000 })
  } catch (error) {
    const failure = error as { code?: number; stderr?: string; stdout?: string }
    checkerOutput = (failure.stderr || failure.stdout || String(error)).trim().slice(0, 1000)
    if (failure.code !== 2) throw new Error(`Чекер ВИНИТИ не принял комплект: ${checkerOutput}`)
    exitCode = failure.code
  }
  const report = await readFile(reportPath, 'utf8').then(text => JSON.parse(text) as unknown).catch(() => {
    throw new Error(`Чекер ВИНИТИ не сохранил читаемый отчёт. ${checkerOutput}`.trim())
  }) as { exit_code?: number }
  if (report.exit_code !== exitCode) throw new Error('Некорректный отчёт чекера ВИНИТИ.')
  return report.exit_code
}

function passport(data: ReturnType<typeof buildPublication>['dataset'], checkerExit: number) {
  const version = data.version!
  const example = data.results.find(row => row.method === 'ThreadRAG' && row.rawValue === '.422') ?? data.results[0]
  const source = data.evidence.find(item => item.id === example.evidenceIds[0])!
  const sampleAddress = source.tableDesignation
    ?? (source.table ? `Table ${source.table}` : source.figure ? `Figure ${source.figure}` : 'Фрагмент текста')
  const schema = Object.entries(columns).map(([table, header]) => `- \`${table}.csv\` — \`${header}\`.`).join('\n')
  return `# Паспорт данных RAG

Публикация \`${version.submissionPublicationId ?? version.publicationId}\`, версия \`${version.versionId}\`, прогон \`${version.runId}\`.
Снимок PDF SHA-256 \`${version.pdfSha256}\`. Происхождение записей приведено в \`manifest.json\`.

## Фактический охват

**${version.coverage.partial ? 'Частичный охват' : 'Полный заявленный охват'}**. ${version.coverage.summary}

Выгружено ${data.results.filter(row => !row.partial).length} полных измерений,
${data.results.filter(row => row.partial).length} измерений с открытыми полями и ${data.remarks.length} замечаний.
Чекер ВИНИТИ завершился с кодом ${checkerExit}. Проверки формата, ключей и наличия цитат отражены в \`report.json\`.
${checkerExit === 2 ? 'Предупреждения чекера требуют сверки по источникам.\n' : ''}

## Контрольная запись

\`${version.submissionPublicationId ?? version.publicationId} / ${example.id}\`: ${example.method}, ${example.generator || 'генератор не указан'}, ${example.dataset}, ${example.metric}.
Исходная запись \`${example.rawValue}\`${example.value === null ? '' : `, нормализованное значение \`${example.value}\` ${example.unit ?? ''}`}.
Эксперимент \`${example.experimentId ?? 'открыт'}\`, конфигурация \`${example.configId ?? 'открыта'}\`.
Исходный фрагмент — страница ${source.page}, ${sampleAddress}, ${source.headers.join(' → ') || 'см. цитату'}.
[Ссылка внутри приложения](${version.url}/evidence/${source.id}?resultId=${example.id})

## Связи и типы

Строка результатов хранит одно измерение одной метрики. Ключ статьи — \`publication_id\`, для прочих таблиц ключ составной, с локальным идентификатором. Связи с конфигурацией, набором, задачей и экспериментом разрешаются в пределах той же статьи. Замечания к частичным измерениям содержат ID результата и открытое поле. Несколько ID разделяются точкой с запятой.

CSV использует UTF-8, запятую и десятичную точку. Пустая ячейка означает неизвестное или неподтверждённое значение. Числовой ноль сохраняется. \`in_scope\` — булево поле. Страница PDF начинается с 1. Год, \`top_k\`, \`metric_k\`, размер выборки, размер и перекрытие чанка и токены записываются целыми числами, значения метрик и задержка — десятичными. Прочие описания и ключи текстовые.

Исходное значение и напечатанная единица сохраняются в \`raw_value\` и \`raw_unit\`. Нормализация записывается в \`normalized_value\` и \`normalized_unit\`. Доли EM и F1 без символа единицы представлены как дроби от 0 до 1, \`percentage_points\` обозначает процентные пункты. \`top_k\` относится к поиску, \`metric_k\` обозначает отсечку метрики. Координаты источников доступны в приложении. Текст и/или адрес в CSV указывает страницу и проверяемый фрагмент.

## Поля

${schema}
`
}
