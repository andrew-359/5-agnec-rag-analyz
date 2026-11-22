import { test } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtemp, writeFile, rm } from 'node:fs/promises'
import { join, resolve } from 'node:path'
import { tmpdir } from 'node:os'
import { createComparisonApi } from '../src/server/comparison/comparisons.ts'
import { exactDifference, compareResults } from '../src/server/comparison/comparison-engine.ts'
import type { ComparisonSide, ScopeProof } from '../src/shared/comparison.ts'

const base: ComparisonSide = { ref: { publicationId: 'p', versionId: 'version-'+'a'.repeat(40), resultId: 'r1' },title:'Paper',pdfSha256:'b'.repeat(64),pdfUrl:'/source.pdf',evidence:[{id:'s1',runId:'v',pdfId:'pdf',page:6,table:1,row:1,column:2,headers:[],box:null,precision:'cell'}],result:{id:'r1',runId:'v',method:'A',generator:'G',dataset:'D',metric:'EM',task:'QA',experiment:'main',protocol:'same',unit:'fraction',statistic:null,rawValue:'.204',value:'0.204',retrievalTopK:null,evidenceIds:['s1']} }
const other: ComparisonSide = {...base,ref:{...base.ref,resultId:'r2'},result:{...base.result,id:'r2',method:'B',rawValue:'.422',value:'0.422'}}
const proof: ScopeProof = {a:base.ref,b:other.ref,aPdfSha256:base.pdfSha256,bPdfSha256:base.pdfSha256,note:'Reviewed source',source:{...base.ref,evidenceId:'s1'},reviewedFields:['evaluation_sample','dataset_version','metric_definition','aggregation','protocol']}

test('exact decimal difference retains zero, sign and precision without floating point artifacts',()=>{
 assert.equal(exactDifference('.204','.422'),'0.218')
 assert.equal(exactDifference('0','0'),'0')
 assert.equal(exactDifference('-0.01','0.001'),'0.011')
 assert.equal(exactDifference('10.000','0'),'10')
 assert.equal(exactDifference('0.000000000000000001','0'),'0.000000000000000001')
})

test('candidate lookup reads one pinned version and ranks confirmed pairs above unverified matches', async t => {
 const directory = await mkdtemp(join(tmpdir(), 'rag-candidates-'))
 t.after(() => rm(directory, { recursive: true, force: true }))
 const proofFile = join(directory, 'proofs.json')
 await writeFile(proofFile, JSON.stringify({ proofs: [proof] }))
 const unresolved = { ...other.result, id: 'unresolved', experimentId: 'main' }
 const incompatible = { ...other.result, id: 'incompatible', unit: 'ms' }
 const unrelated = { ...other.result, id: 'unrelated', dataset: 'Other' }
 const data = {
  publication: { id: 'p', title: 'Paper' }, pdf: { sha256: base.pdfSha256, url: '/source.pdf' },
  results: [{ ...base.result, experimentId: 'main' }, unresolved, incompatible, unrelated, other.result],
  evidence: base.evidence,
 } as Awaited<ReturnType<Parameters<typeof createComparisonApi>[0]['publications']['read']>>
 const foreignVersion = { publicationId: 'another-paper', versionId: 'version-' + 'c'.repeat(40) }
 const foreignData = { ...data!, publication: { ...data!.publication, title: 'Another paper' },
  version: foreignVersion, results: [{ ...base.result, experimentId: 'main' }] } as NonNullable<typeof data>
 let reads = 0
 let scans = 0
 const api = createComparisonApi({ proofFile, publications: {
  read: async (publicationId, versionId) => {
   reads++
   return publicationId === base.ref.publicationId && versionId === base.ref.versionId ? data : null
  },
  listLatest: async excluded => {
   scans++
   assert.equal(excluded, base.ref.publicationId)
   return [foreignData]
  },
 } })
 const response = await api.request('/candidates', { method: 'POST', body: JSON.stringify(base.ref) })
 assert.equal(response.status, 200)
 const { candidates } = await response.json()
 assert.equal(reads, 1)
 assert.equal(scans, 0)
 assert.deepEqual(candidates.map(({ ref, status }: { ref: { resultId: string }; status: string }) => [ref.resultId, status]),
  [['r2', 'comparable'], ['unresolved', 'insufficient']])
 assert.deepEqual(candidates[0].ref, other.ref)
 assert.equal(candidates[1].sameExperiment, true)
 assert.ok(candidates[1].reasons.length > 0)

 const corpusResponse = await api.request('/candidates', { method: 'POST', body: JSON.stringify({ ...base.ref, scope: 'corpus' }) })
 assert.equal(corpusResponse.status, 200)
 assert.equal(scans, 1)
 const corpus = (await corpusResponse.json()).candidates
 assert.equal(corpus.length, 1)
 assert.deepEqual(corpus[0].ref, { ...foreignVersion, resultId: base.ref.resultId })
 assert.equal(corpus[0].title, 'Another paper')
 assert.equal(corpus[0].status, 'insufficient')
 assert.equal(corpus[0].sameExperiment, false)
})
test('same experiment labels and protocol cannot grant comparison without scoped proof',()=>{
 const uncertain=compareResults(base,other)
 assert.equal(uncertain.status,'insufficient');assert.ok(!('delta' in uncertain))
 const verified=compareResults(base,other,[proof]);assert.equal(verified.delta,'0.218')
 assert.equal(compareResults(base,{...other,pdfSha256:'c'.repeat(64)},[proof]).status,'insufficient')
 assert.equal(compareResults(base,{...other,result:{...other.result,statistic:'sample_sd'}},[proof]).status,'insufficient')
 const mean={...base,result:{...base.result,statistic:'mean'}}
 assert.equal(compareResults(mean,{...other,result:{...other.result,statistic:'sample_sd'}},[proof]).status,'different')
})

test('recommendations use an explicit shared protocol linked to distinct rows of the same table column', async () => {
 const text = 'All compared methods were evaluated on the same test split with the same metric definitions, aggregation rules, and evaluation protocol.'
 const source = { ...base.evidence[0], id: 'evaluation', text, precision: 'text_fragment' as const }
 const bind = (side: ComparisonSide, row: number, column: number = 2): ComparisonSide => ({
  ...side, evidence: [{ ...side.evidence[0], id: `cell-${row}`, row, column }, source],
  result: { ...side.result, experimentId: 'main', protocol: text, evidenceIds: [`cell-${row}`, source.id],
   conditionEvidence: { protocol: [source.id] } },
 })
 const a = bind(base, 1), b = bind(other, 2), outside = bind(other, 3, 5)
 const compare = compareResults(a, b)
 assert.equal(compare.delta, '0.218')
 assert.equal(compare.proof?.origin, 'extracted')
 assert.equal(compare.proof?.source.evidenceId, source.id)
 assert.equal(compareResults(a, outside).status, 'insufficient')
 assert.equal(compareResults(a, { ...b, result: { ...b.result, conditionEvidence: {} } }).status, 'insufficient')
 const unclearSource = { ...source, text: 'We use the same data splits as previous works.' }
 assert.equal(compareResults({ ...a, evidence: [a.evidence[0], unclearSource] },
  { ...b, evidence: [b.evidence[0], unclearSource] }).status, 'insufficient')
 const publishedProtocol = { ...source, text: 'We use the same data splits as previous works for evaluation. For evaluation, we report Exact Match (EM) and F1 scores, applying standard text normalization to both predictions and ground-truth answers prior to computation.' }
 const scope = { ...source, id: 'baselines', text: 'Baselines. We evaluate B against two categories of baselines: A (Author, 2025).' }
 const scoped = (side: ComparisonSide): ComparisonSide => ({
  ...side, evidence: [side.evidence[0], publishedProtocol, scope],
  result: { ...side.result, experiment: 'Table 1: Main results.', protocol: publishedProtocol.text,
   evidenceIds: [side.evidence[0].id, publishedProtocol.id, scope.id],
   conditionEvidence: { protocol: [publishedProtocol.id], comparison_scope: [scope.id] } },
 })
 assert.equal(compareResults(scoped(a), scoped(b)).proof?.scopeEvidenceId, scope.id)
 const unnamed = scoped(b)
 assert.equal(compareResults(scoped(a), { ...unnamed, result: { ...unnamed.result, method: 'C' } }).status, 'insufficient')
 const data = { publication: { title: a.title }, pdf: { sha256: a.pdfSha256, url: a.pdfUrl },
  results: [a.result, b.result], evidence: [...a.evidence, b.evidence[0]] } as NonNullable<Awaited<ReturnType<Parameters<typeof createComparisonApi>[0]['publications']['read']>>>
 const api = createComparisonApi({ publications: { read: async () => data, listLatest: async () => [] } })
 const response = await api.request('/recommendations', { method: 'POST',
  body: JSON.stringify({ publicationId: a.ref.publicationId, versionId: a.ref.versionId }) })
 assert.equal(response.status, 200)
 assert.deepEqual((await response.json()).recommendations, [{
  a: a.ref, b: b.ref, methods: ['A', 'B'], dataset: 'D', metric: 'EM', delta: '0.218', unit: 'fraction',
 }])
})
test('HTTP uses immutable refs, verifies proof sources, rejects client-provided values and keeps negative pairs without delta',async()=>{
 const directory=await mkdtemp(join(tmpdir(),'rag-compare-'));const proofFile=join(directory,'proofs.json')
 const data={publication:{id:'p',title:'Paper'},pdf:{sha256:base.pdfSha256,url:'/source.pdf'},results:[base.result,other.result],evidence:base.evidence} as any
 const api=createComparisonApi({publications:{read:async(p,v)=>p==='p'&&v===base.ref.versionId?data:null,listLatest:async()=>[]},proofFile})
 const post=(body:unknown)=>api.request('/',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})
 try {
  assert.equal((await (await post({a:base.ref,b:other.ref})).json()).status,'insufficient')
  await writeFile(proofFile,JSON.stringify({proofs:[proof]}))
  for (const [a, b] of [[base.ref, other.ref], [other.ref, base.ref]]) {
   const response = await post({ a, b })
   assert.equal(response.status, 200)
   const verdict = await response.json()
   assert.equal(verdict.status, 'comparable')
   assert.equal(verdict.delta, '0.218')
   assert.equal(verdict.unit, 'fraction')
   assert.deepEqual(verdict.a.ref, a)
   assert.deepEqual(verdict.b.ref, b)
   assert.deepEqual(verdict.proof.source, {
    publicationId: base.ref.publicationId, versionId: base.ref.versionId, evidenceId: 's1',
   })
  }
  assert.equal((await post({a:{...base.ref,value:'999'},b:other.ref})).status,400)
  assert.equal((await post({a:{...base.ref,resultId:'missing'},b:other.ref})).status,404)
  await writeFile(proofFile,JSON.stringify({proofs:[{...proof,source:{...proof.source,evidenceId:'foreign'}}]}))
  assert.equal((await (await post({a:base.ref,b:other.ref})).json()).status,'insufficient')
 } finally {await rm(directory,{recursive:true,force:true})}
})
