import { useRef, useState, type PointerEvent } from 'react'
import { post, runPath } from './api'
import type { RegionRunRequest } from '../shared/figure-run'
import { usePdfCanvas } from '../components/pdf/use-pdf-canvas'
import type { Evidence } from '../shared/schema'

export type Source = {
  url: string
  page: number
  box?: Evidence['box']
  trigger?: HTMLElement
  candidateId?: string
  locatorText?: string
}

type Box = RegionRunRequest['box']
type Region = { runId: string; pdfSha256: string; candidateId?: string; initial?: { page: number; box: Box } | null; onStarted(id: string): void }

function rectangle(a: [number, number], b: [number, number]): Box {
  return [Math.min(a[0], b[0]), Math.min(a[1], b[1]), Math.max(a[0], b[0]), Math.max(a[1], b[1])]
}

export function SourcePanel({ source, pages, close, region }: { source: Source; pages: number; close: () => void; region?: Region }) {
  const [page, setPage] = useState(source.page)
  const [fragment, setFragment] = useState(!!source.box)
  const [selecting, setSelecting] = useState(false)
  const [boxSelection, setBoxSelection] = useState<{ page: number; box: Box }>()
  const [drag, setDrag] = useState<Box>()
  const anchor = useRef<{ id: number; point: [number, number] }>(null)
  const [launching, setLaunching] = useState(false)
  const [launchError, setLaunchError] = useState('')
  const initialBox = region?.initial?.page === page ? region.initial.box : undefined
  const selectedBox = boxSelection?.page === page ? boxSelection.box : initialBox
  const visibleFrame = drag ?? selectedBox
  const box = page === source.page ? source.box : null
  let [left, top, right, bottom] = [0, 0, 1, 1]
  if (fragment && box) {
    const [x1, y1, x2, y2] = box
    const width = Math.min(1, Math.max(x2 - x1 + .16, .42))
    const height = Math.min(1, Math.max(y2 - y1 + .08, .14))
    left = Math.max(0, Math.min(1 - width, (x1 + x2 - width) / 2))
    top = Math.max(0, Math.min(1 - height, (y1 + y2 - height) / 2))
    right = left + width
    bottom = top + height
  }
  const { host, canvas, viewport, error, retry, pageCount } = usePdfCanvas(source.url, page, right - left, 100)
  const total = pageCount ?? Math.max(pages, page)
  function turn(step: number) {
    if (page + step < 1 || page + step > total) return
    setFragment(false)
    setDrag(undefined)
    setPage(page + step)
  }

  function point(event: PointerEvent<HTMLDivElement>): [number, number] {
    const bounds = canvas.current!.getBoundingClientRect()
    return [
      Math.max(left, Math.min(right, (event.clientX - bounds.left) / bounds.width)),
      Math.max(top, Math.min(bottom, (event.clientY - bounds.top) / bounds.height)),
    ]
  }

  function startDrag(event: PointerEvent<HTMLDivElement>) {
    if (!selecting || launching || !viewport || !event.isPrimary || event.button !== 0) return
    // During a display-mode change the previous canvas can still be visible.
    if (Math.abs(viewport.width * (right - left) - (host.current?.clientWidth ?? 0)) > 2) return
    event.currentTarget.setPointerCapture(event.pointerId)
    const start = point(event)
    anchor.current = { id: event.pointerId, point: start }
    setDrag(rectangle(start, start))
  }

  function moveDrag(event: PointerEvent<HTMLDivElement>) {
    if (anchor.current?.id === event.pointerId) setDrag(rectangle(anchor.current.point, point(event)))
  }

  function finishDrag(event: PointerEvent<HTMLDivElement>) {
    if (anchor.current?.id !== event.pointerId) return
    const box = rectangle(anchor.current.point, point(event))
    cancelDrag()
    if (box[2] - box[0] >= .002 && box[3] - box[1] >= .002) setBoxSelection({ page, box })
  }

  function cancelDrag() {
    anchor.current = null
    setDrag(undefined)
  }

  async function launch() {
    if (!region || !selectedBox || !viewport) return
    setLaunching(true)
    setLaunchError('')
    try {
      const payload: RegionRunRequest = { pdfSha256: region.pdfSha256, page, box: selectedBox,
        candidateId: page === source.page ? region.candidateId : undefined }
      const result = await post<{ id: string }>(runPath(region.runId) + '/region-runs', payload)
      region.onStarted(result.id)
    } catch (failure) { setLaunchError((failure as Error).message) } finally { setLaunching(false) }
  }

  return <aside className="pdf" aria-label="Источник PDF" onKeyDown={event => {
    if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
      event.preventDefault()
      turn(event.key === 'ArrowLeft' ? -1 : 1)
    }
  }}>
    <div className="pdf-heading"><strong>Источник PDF</strong><div className="actions">
      <a className="link quiet" href={`${source.url}#page=${page}`} target="_blank" rel="noreferrer">Открыть PDF целиком</a>
      <button className="quiet" onClick={close}>Закрыть PDF</button>
    </div></div>
    <div className="pdf-toolbar">
      <div className="switch">
        <button className={fragment ? 'chosen' : ''} aria-pressed={fragment} disabled={!source.box}
          onClick={() => { setPage(source.page); setFragment(true) }}>Фрагмент</button>
        <button className={!fragment ? 'chosen' : ''} aria-pressed={!fragment} onClick={() => setFragment(false)}>Вся страница</button>
      </div>
      <div className="pdf-pager">
        <button aria-label="Предыдущая страница" disabled={page === 1} onClick={() => turn(-1)}>‹</button>
        <output>{page} / {total}</output>
        <button aria-label="Следующая страница" disabled={page >= total} onClick={() => turn(1)}>›</button>
      </div>
    </div>
    {source.locatorText && <p className="pdf-source-label">{source.locatorText}</p>}
    {!source.box && <small>Для источника доступна страница целиком.</small>}
    {region && <div className="pdf-region-actions">
      {selecting ? <>
        <div className="actions">
          <button className="action" disabled={!selectedBox || !!drag || launching || !viewport || error} onClick={() => void launch()}>
            {launching ? 'Запускаем…' : 'Разобрать область'}</button>
          <button disabled={launching} onClick={() => { setSelecting(false); setBoxSelection(undefined); setLaunchError('') }}>Отменить выделение</button>
        </div>
        <small>Обведите одну панель с осями и легендой, если она есть.</small>
      </> : <button disabled={!viewport || error} onClick={() => setSelecting(true)}>{initialBox ? 'Уточнить рамку' : 'Выделить область'}</button>}
      {launchError && <p role="alert">{launchError}</p>}
    </div>}
    <div ref={host} className="sheet">
      {error ? <div className="pdf-load-error"><p role="alert">Не удалось загрузить страницу PDF.</p><button onClick={retry}>Повторить</button></div>
        : !viewport && <p role="status">Загружаем страницу…</p>}
      <div className={`react-pdf-frame${selecting ? ' selecting' : ''}`} hidden={!viewport || error}
        onPointerDown={startDrag} onPointerMove={moveDrag} onPointerUp={finishDrag}
        onPointerCancel={cancelDrag} onLostPointerCapture={cancelDrag}
        style={viewport ? { height: (bottom - top) * viewport.height } : undefined}>
        <canvas ref={canvas} role="img" aria-label={`Страница ${page}${box ? ' · подсвечен источник' : ''}`}
          style={viewport ? { position: 'absolute', left: -left * viewport.width, top: -top * viewport.height } : undefined} />
        {viewport && selecting && visibleFrame && <span className="react-pdf-highlight region-frame" role="img" aria-label="Выбранная область" style={{
          left: (visibleFrame[0] - left) * viewport.width, top: (visibleFrame[1] - top) * viewport.height,
          width: (visibleFrame[2] - visibleFrame[0]) * viewport.width, height: (visibleFrame[3] - visibleFrame[1]) * viewport.height,
        }} />}
        {viewport && box && <span className="react-pdf-highlight" aria-hidden="true" style={{
          left: (box[0] - left) * viewport.width, top: (box[1] - top) * viewport.height,
          width: (box[2] - box[0]) * viewport.width, height: (box[3] - box[1]) * viewport.height,
        }} />}
      </div>
    </div>
  </aside>
}
