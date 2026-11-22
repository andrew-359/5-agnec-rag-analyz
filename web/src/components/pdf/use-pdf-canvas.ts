import { useEffect, useMemo, useRef, useState } from 'react'
import { getDocument, GlobalWorkerOptions, type PDFDocumentProxy, type PDFPageProxy } from 'pdfjs-dist/legacy/build/pdf.mjs'
import pdfWorker from 'pdfjs-dist/legacy/build/pdf.worker.min.mjs?url'

GlobalWorkerOptions.workerSrc = pdfWorker

type DocumentRequest = { url: string; attempt: number }
type DocumentLoad = { request: DocumentRequest; pdf: PDFDocumentProxy | null }
type PageLoad = { pdf: PDFDocumentProxy; number: number; page: PDFPageProxy | null }
type FrameSize = { width: number; height: number }
type PageRender = { page: PDFPageProxy; viewport?: FrameSize; error: boolean }

export function usePdfCanvas(url: string, number: number, widthRatio: number, zoom: number) {
  const host = useRef<HTMLDivElement>(null)
  const canvas = useRef<HTMLCanvasElement>(null)
  const [width, setWidth] = useState(0)
  const [attempt, setAttempt] = useState(0)
  const [documentLoad, setDocumentLoad] = useState<DocumentLoad>()
  const [pageLoad, setPageLoad] = useState<PageLoad>()
  const [pageRender, setPageRender] = useState<PageRender>()
  const request = useMemo(() => ({ url, attempt }), [url, attempt])
  const doc = documentLoad?.request === request ? documentLoad : undefined
  const pdf = doc?.pdf
  const loaded = pageLoad?.pdf === pdf && pageLoad?.number === number ? pageLoad : undefined
  const page = loaded?.page
  const currentRender = pageRender?.page === page ? pageRender : undefined

  useEffect(() => {
    const observer = new ResizeObserver(([entry]) => setWidth(entry.contentRect.width))
    if (host.current) observer.observe(host.current)
    return () => observer.disconnect()
  }, [])

  useEffect(() => {
    let active = true
    const task = getDocument({ url: request.url })
    void task.promise.then(pdf => {
      if (active) setDocumentLoad({ request, pdf })
    }).catch(() => {
      if (active) setDocumentLoad({ request, pdf: null })
    })
    return () => {
      active = false
      // Only the URL and a deliberate retry create a new document session.
      void task.destroy().catch(() => {})
    }
  }, [request])

  useEffect(() => {
    if (!pdf) return
    let active = true
    void pdf.getPage(number).then(page => {
      if (active) setPageLoad({ pdf, number, page })
    }).catch(() => {
      if (active) setPageLoad({ pdf, number, page: null })
    })
    return () => {
      active = false
    }
  }, [pdf, number])

  useEffect(() => {
    if (!page || !canvas.current || width <= 0) return
    let active = true
    const viewport = page.getViewport({ scale: width / widthRatio / page.getViewport({ scale: 1 }).width * zoom / 100 })
    const element = canvas.current
    const dpr = window.devicePixelRatio || 1
    // Keep the last committed frame during resize. Cancelled tasks never draw on the visible canvas.
    const buffer = document.createElement('canvas')
    buffer.width = Math.ceil(viewport.width * dpr)
    buffer.height = Math.ceil(viewport.height * dpr)
    const task = page.render({ canvas: buffer, viewport, transform: dpr === 1 ? undefined : [dpr, 0, 0, dpr, 0, 0] })
    void task.promise.then(() => {
      if (!active) return
      element.width = buffer.width
      element.height = buffer.height
      element.style.width = `${viewport.width}px`
      element.style.height = `${viewport.height}px`
      element.getContext('2d')!.drawImage(buffer, 0, 0)
      setPageRender({ page, viewport: { width: viewport.width, height: viewport.height }, error: false })
    }).catch((cause: unknown) => {
      if (!active || (cause instanceof Error && cause.name === 'RenderingCancelledException')) return
      setPageRender(previous => ({
        page,
        viewport: previous?.page === page ? previous.viewport : undefined,
        error: true,
      }))
    })
    return () => {
      active = false
      task.cancel()
    }
  }, [page, width, widthRatio, zoom])

  return {
    host, canvas,
    viewport: currentRender?.viewport,
    pageCount: pdf?.numPages,
    error: doc?.pdf === null || loaded?.page === null || currentRender?.error === true,
    retry: () => setAttempt(value => value + 1),
  }
}
