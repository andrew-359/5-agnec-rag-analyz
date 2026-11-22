import type { ComparisonSide, ComparisonResponse, ScopeProof, ResultRef } from '../../shared/comparison.ts'
export function exactDifference(a: string, b: string) {
  function decimal(value: string) {
    const match = /^([+-]?)(\d*)(?:\.(\d+))?$/.exec(value)
    if (!match || !(match[2] || match[3])) throw new Error('Некорректное число')
    return { n: BigInt((match[1] === '-' ? '-' : '') + (match[2] || '0') + (match[3] || '')), scale: (match[3] || '').length }
  }
  const x = decimal(a), y = decimal(b), scale = Math.max(x.scale, y.scale)
  let n = y.n * 10n ** BigInt(scale - y.scale) - x.n * 10n ** BigInt(scale - x.scale)
  if (n < 0n) n = -n
  const digits = n.toString().padStart(scale + 1, '0')
  return scale ? (digits.slice(0, -scale) + '.' + digits.slice(-scale)).replace(/\.?0+$/, '') || '0' : digits
}
const sameRef = (a: ResultRef, b: ResultRef) => a.publicationId === b.publicationId && a.versionId === b.versionId && a.resultId === b.resultId
const requiredProof = ['evaluation_sample', 'dataset_version', 'metric_definition', 'aggregation', 'protocol']
export function tableColumn(side: ComparisonSide) {
  const cell = side.evidence.find(item => item.id === side.result.evidenceIds[0])
  if (!cell || !['cell', 'estimated_cell'].includes(cell.precision) || cell.table == null || cell.row == null || cell.column == null) return null
  return cell
}

function extractedProof(a: ComparisonSide, b: ComparisonSide): ScopeProof | undefined {
  if (a.ref.publicationId !== b.ref.publicationId || a.ref.versionId !== b.ref.versionId || a.pdfSha256 !== b.pdfSha256 ||
      !a.result.experimentId || a.result.experimentId !== b.result.experimentId || !a.result.protocol || a.result.protocol !== b.result.protocol) return
  const left = tableColumn(a), right = tableColumn(b)
  if (!left || !right || left.page !== right.page || left.table !== right.table || left.column !== right.column || left.row === right.row) return
  const shared = (field: string) => a.evidence.filter(item => item.precision === 'text_fragment' && item.text &&
    a.result.conditionEvidence?.[field]?.includes(item.id) && b.result.conditionEvidence?.[field]?.includes(item.id) &&
    b.evidence.some(other => other.id === item.id && other.text === item.text))
  const protocols = shared('protocol')
  let source = protocols.find(item =>
    /(?:^|[.!?] )all (?:compared )?(?:methods|systems) (?:were |are )?evaluated (?:on|using) the (?:same|identical) (?:test|evaluation) split (?:with|using) the (?:same|identical) metric definitions?, aggregation(?: rules?)?, and (?:evaluation )?protocol\./i.test(item.text!.replace(/\s+/g, ' ').trim()))
  let scopeEvidenceId: string | undefined
  if (!source && a.result.metric === 'EM' && a.result.statistic === null && b.result.statistic === null &&
      a.result.generator && a.result.generator === b.result.generator && a.result.unit === 'fraction' && b.result.unit === 'fraction' &&
      /^Table \d+: Main (?:experimental )?results\b/i.test(a.result.experiment)) {
    const scope = shared('comparison_scope').find(item => {
      const statement = /^Baselines\.\s+We evaluate (.+?) against [^:]+:\s*(.+)/i.exec(item.text!.replace(/\s+/g, ' ').trim())
      if (!statement) return false
      const [focal, baseline] = statement[1].toLowerCase() === a.result.method.toLowerCase() ? [a, b] : [b, a]
      const baselinePattern = new RegExp(`(?:^|[^\\w-])${baseline.result.method.replace(/[.*+?^\${}()|[\]\\]/g, '\\$&')}\\s*\\(`, 'i')
      return statement[1].toLowerCase() === focal.result.method.toLowerCase() && baselinePattern.test(statement[2])
    })
    if (scope) {
      source = protocols.find(item => {
        const text = item.text!.replace(/\s+/g, ' ')
        return /\bwe use the same data splits as previous works\b/i.test(text) &&
          /\bwe report Exact Match \(EM\)/i.test(text) &&
          /applying standard text normalization to both predictions and ground-truth answers/i.test(text)
      })
      if (source) scopeEvidenceId = scope.id
    }
  }
  if (!source) return
  return { a: a.ref, b: b.ref, aPdfSha256: a.pdfSha256, bPdfSha256: b.pdfSha256, origin: 'extracted',
    source: { publicationId: a.ref.publicationId, versionId: a.ref.versionId, evidenceId: source.id }, reviewedFields: requiredProof,
    scopeEvidenceId, note: scopeEvidenceId ? 'Авторы сравнили свой метод с указанным базовым методом. Обе оценки EM стоят в одной колонке основной таблицы, а раздел эксперимента описывает разбиение и нормализацию.' :
      'Авторы указали общую выборку, определения метрик, агрегацию и протокол для методов. Обе записи относятся к одной колонке таблицы.' }
}
export function compareResults(a: ComparisonSide, b: ComparisonSide, proofs: ScopeProof[] = []): ComparisonResponse {
  const proof = proofs.find(p => requiredProof.every(field => p.reviewedFields.includes(field)) &&
    ((sameRef(p.a, a.ref) && sameRef(p.b, b.ref) && p.aPdfSha256 === a.pdfSha256 && p.bPdfSha256 === b.pdfSha256) ||
      (sameRef(p.b, a.ref) && sameRef(p.a, b.ref) && p.bPdfSha256 === a.pdfSha256 && p.aPdfSha256 === b.pdfSha256))) ?? extractedProof(a, b)
  const checks: ComparisonResponse['checks'] = [
    ['Задача', a.result.task, b.result.task], ['Датасет', a.result.dataset, b.result.dataset],
    ['Метрика', a.result.metric, b.result.metric], ['Вариант метрики', a.result.metricVariant, b.result.metricVariant],
    ['Единица', a.result.unit, b.result.unit], ['Статистика', a.result.statistic, b.result.statistic],
  ].map(([field, x, y]) => ({ field: field!, a: x || null, b: y || null, state: !x || !y ? 'unknown' : x === y ? 'same' : 'different' }))
  checks.push(...requiredProof.map(field => ({ field: ({ evaluation_sample: 'Общая оценочная выборка', dataset_version: 'Версия датасета', metric_definition: 'Определение метрики и cutoff', aggregation: 'Агрегация', protocol: 'Протокол оценки' })[field]!, a: null, b: null, state: proof?.origin === 'extracted' ? 'documented' as const : proof ? 'reviewed' as const : 'unknown' as const })))
  // A different free-text task label is not proof of a different evaluation task.
  const taskCheck = checks.find(check => check.field === 'Задача')!
  if (taskCheck.state === 'different') taskCheck.state = 'unknown'
  const percentUnits = ['%', 'percent', 'percentage']
  const convertible = (a.result.unit === 'fraction' && percentUnits.includes(b.result.unit ?? '')) || (b.result.unit === 'fraction' && percentUnits.includes(a.result.unit ?? ''))
  if (convertible) checks.find(check => check.field === 'Единица')!.state = 'equivalent'
  function commonValue(side: ComparisonSide) {
    const value = side.result.value!
    if (!convertible || side.result.unit === 'fraction') return value
    const negative = value.startsWith('-')
    const unsigned = value.replace(/^[+-]/, '')
    const [integer, fraction = ''] = unsigned.split('.')
    const digits = (integer || '0').padStart(3, '0') + fraction
    const split = (integer || '0').padStart(3, '0').length - 2
    return (negative ? '-' : '') + digits.slice(0, split) + '.' + digits.slice(split)
  }
  const reasons: string[] = []
  if (taskCheck.state === 'unknown') reasons.push('Общая задача не подтверждена: текстовые обозначения различаются или отсутствуют.')
  const different = checks.some(check => check.state === 'different')
  if (different) reasons.push('Есть известные различия оценочных условий или несовместимых единиц.')
  if ((!!a.result.statistic !== !!b.result.statistic) || (!!a.result.metricVariant !== !!b.result.metricVariant)) reasons.push('Не установлена эквивалентность статистики или варианта метрики одной из записей.')
  if (!proof) reasons.push('Нет проверенного основания общей выборки, определения метрики, агрегации и протокола для этой пары. Совпадение названий или текста протокола недостаточно.')
  if (a.result.value === null || b.result.value === null) reasons.push('Нет нормализованного числового значения одной из записей.')
  if (!a.result.unit || !b.result.unit) reasons.push('Шкала значения не установлена.')
  if (!a.result.task || !b.result.task || !a.result.dataset || !b.result.dataset || !a.result.metric || !b.result.metric) reasons.push('Отсутствуют обязательные сведения о задаче, датасете или метрике.')
  const status = different ? 'different' : reasons.length ? 'insufficient' : 'comparable'
  return { a, b, status, checks, reasons, proof, ...(status === 'comparable' ? { delta: exactDifference(commonValue(a), commonValue(b)), unit: convertible ? 'fraction' : a.result.unit! } : {}) }
}
