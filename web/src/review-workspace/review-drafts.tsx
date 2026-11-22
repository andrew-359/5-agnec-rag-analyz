import { useEffect, useLayoutEffect, useRef, useState, type FormEvent } from 'react'
import { useQueries } from '@tanstack/react-query'
import { draftFields, proposalError, type DraftField, type ReviewDraft } from '../shared/review-draft'
import type { Dataset, Evidence, Result } from '../shared/schema'
import { json } from './api'
import type { Preview } from './publication'

type Form = {
  disposition: ReviewDraft['disposition']; field: DraftField; value: string; reason: string
  evidenceId: string; existing?: ReviewDraft
}

const endpoint = '/api/review/drafts'
const display = (value: unknown) => value === null || value === undefined || value === '' ? 'Не установлено' : String(value)

export function ReviewDrafts({ data, remark, result, preview, onBusy }: {
  data: Dataset; remark: Dataset['remarks'][number]; result?: Result; preview: Preview; onBusy: (value: boolean) => void
}) {
  const version = data.version!
  const address = { publicationId: version.publicationId, versionId: version.versionId, remarkId: remark.id }
  const addresses = result ? [address, { ...address, resultId: result.id }] : [address]
  const queries = useQueries({ queries: addresses.map(item => ({
    queryKey: ['review-drafts', item],
    queryFn: ({ signal }: { signal: AbortSignal }) => json<{ drafts: ReviewDraft[] }>(
      endpoint + '?' + new URLSearchParams(item), { signal }),
  })) })
  const drafts = queries.flatMap(item => item.data?.drafts ?? [])
  const [form, setForm] = useState<Form>()
  const [dirty, setDirty] = useState(false)
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')
  const retry = useRef<{ payload: string; requestId: string } | null>(null)
  const evidence = [...new Set([...(result?.evidenceIds ?? []), ...remark.evidenceIds])]
    .map(id => data.evidence.find(source => source.id === id)).filter((source): source is Evidence => !!source)
  const proposal = form?.disposition === 'proposal'
  const formActive = !!form
  const editor = useRef<HTMLFieldSetElement>(null)
  const reasonField = useRef<HTMLLabelElement>(null)
  useLayoutEffect(() => {
    if (!formActive) return
    editor.current?.querySelector<HTMLElement>('select:not(:disabled), textarea')?.focus({ preventScroll: true })
    reasonField.current?.scrollIntoView({ block: 'nearest' })
  }, [formActive])


  useEffect(() => {
    onBusy(dirty || pending)
    return () => onBusy(false)
  }, [dirty, pending, onBusy])

  useEffect(() => {
    if (!dirty && !pending) return
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = '' }
    window.addEventListener('beforeunload', warn)
    return () => window.removeEventListener('beforeunload', warn)
  }, [dirty, pending])

  function open(disposition: Form['disposition'], existing?: ReviewDraft) {
    if (dirty && !window.confirm('Отменить несохранённые изменения?')) return
    const options = (disposition === 'proposal' ? result?.evidenceIds : remark.evidenceIds) ?? []
    setForm({ disposition, field: existing?.field ?? 'value', value: String(existing?.proposedValue ?? ''),
      evidenceId: existing?.evidenceIds[0] ?? options[0] ?? '', reason: existing?.reason ?? '', existing })
    setDirty(false)
    setError('')
    retry.current = null
  }

  function change(values: Partial<Form>) {
    setForm(previous => previous && { ...previous, ...values })
    setDirty(true)
  }

  function close() {
    setForm(undefined)
    setDirty(false)
    setError('')
    retry.current = null
  }

  async function save(event: FormEvent) {
    event.preventDefault()
    if (!form || pending) return
    setError('')
    if (!form.reason.trim()) return setError('Объясните причину.')
    const value = form.value.trim()
    const proposedValue = proposal ? form.field === 'retrievalTopK' && /^\d+$/.test(value) ? Number(value) : value : null
    if (proposal) {
      const message = proposalError(form.field, proposedValue)
      if (message) return setError(message)
      if (!form.evidenceId) return setError('Выберите подтверждающий фрагмент.')
    }
    const content = { disposition: form.disposition, reason: form.reason.trim(), proposedValue,
      evidenceIds: form.evidenceId ? [form.evidenceId] : [] }
    setPending(true)
    try {
      if (form.existing) {
        await send('PATCH', form.existing.id, { ...content, expectedRevision: form.existing.revision })
      } else {
        const body = { ...address, pdfSha256: data.pdf.sha256,
          ...(proposal ? { field: form.field, resultId: result?.id } : {}), ...content }
        const payload = JSON.stringify(body)
        if (retry.current?.payload !== payload) retry.current = { payload, requestId: crypto.randomUUID() }
        await send('POST', undefined, { ...body, requestId: retry.current.requestId })
      }
      close()
      await Promise.all(queries.map(item => item.refetch()))
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : 'Не удалось сохранить черновик.')
    } finally {
      setPending(false)
    }
  }

  async function remove(draft: ReviewDraft) {
    if (!window.confirm('Удалить сохранённый черновик?')) return
    setPending(true)
    setError('')
    try {
      await send('DELETE', draft.id, { expectedRevision: draft.revision })
      if (form?.existing?.id === draft.id) close()
      await Promise.all(queries.map(item => item.refetch()))
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : 'Не удалось удалить черновик.')
    } finally {
      setPending(false)
    }
  }

  function showSource(id: string, trigger: HTMLElement) {
    const source = evidence.find(item => item.id === id) ?? drafts.flatMap(draft => draft.evidence).find(item => item.id === id)
    if (source) preview({ ...source, url: data.pdf.url, trigger })
  }

  return <div className="review-drafts">
    {queries.some(item => item.isPending) && <p role="status">Загружаем черновики…</p>}
    {queries.some(item => item.error) && <div className="notice" role="alert">
      <p>{queries.find(item => item.error)?.error?.message}</p>
      <button onClick={() => void Promise.all(queries.map(item => item.refetch()))}>Перечитать черновики</button>
    </div>}
    {drafts.map(draft => <div className="notice" key={draft.id}>
      <strong>{draft.disposition === 'proposal' ? 'Черновик · ожидает применения' : 'Оставлено открытым'}</strong>
      {draft.field && <p>{draftFields[draft.field]} · {display(draft.originalValue)} → {display(draft.proposedValue)}</p>}
      <p>{draft.reason}</p>
      <div className="actions">
        {draft.evidence.map(source => <button key={source.id} onClick={event =>
          showSource(source.id, event.currentTarget)}>Источник · стр. {source.page}</button>)}
        <button disabled={pending} onClick={() => open(draft.disposition, draft)}>Изменить черновик</button>
        <button disabled={pending} className="quiet" onClick={() => void remove(draft)}>Удалить черновик</button>
      </div>
    </div>)}
    {!form && <div className="actions">
      {result && evidence.length > 0 && <button className="action" disabled={pending}
        onClick={() => open('proposal')}>Предложить исправление</button>}
      {!drafts.some(draft => draft.disposition === 'leave-open') && <button disabled={pending}
        onClick={() => open('leave-open')}>Оставить открытым</button>}
    </div>}
    {form && <form onSubmit={save}>
      <fieldset ref={editor} disabled={pending}>
        <legend>{proposal ? 'Предложение по полю' : 'Оставить замечание открытым'}</legend>
        {proposal && <div className="review-fields">
          <label>Поле<select value={form.field} disabled={!!form.existing}
            onChange={event => change({ field: event.target.value as DraftField, value: '' })}>
            {Object.entries(draftFields).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select></label>
          <div className="review-values">
            <div><small>Опубликовано</small><p>{display(result?.[form.field])}</p></div>
            <label>Предлагаемое значение<input value={form.value} onChange={event => change({ value: event.target.value })} required /></label>
          </div>
        </div>}
        {evidence.length > 0 && <div className="review-evidence">
          <label>Подтверждающий источник<select value={form.evidenceId} onChange={event => change({ evidenceId: event.target.value })}>
            <option value="">Выберите фрагмент</option>
            {evidence.map((source, index) => <option key={source.id} value={source.id}>Стр. {source.page} · фрагмент {index + 1}</option>)}
          </select></label>
          <button type="button" disabled={!form.evidenceId} onClick={event => showSource(form.evidenceId, event.currentTarget)}>Открыть в PDF</button>
        </div>}
        <label ref={reasonField}>Причина<textarea required value={form.reason} maxLength={4000}
          onChange={event => change({ reason: event.target.value })} /></label>
        <div className="actions">
          <button type="submit" className="action">{proposal ? 'Сохранить черновик' : 'Сохранить причину'}</button>
          <button type="button" onClick={() => { if (!dirty || window.confirm('Отменить несохранённые изменения?')) close() }}>Отмена</button>
        </div>
      </fieldset>
    </form>}
    {error && <p className="notice" role="alert">{error}</p>}
  </div>
}

function send(method: 'POST' | 'PATCH' | 'DELETE', id: string | undefined, body: object) {
  return json(endpoint + (id ? '/' + encodeURIComponent(id) : ''), {
    method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  })
}
