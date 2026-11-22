import { useRef, useState, type FormEvent } from 'react'
import { catalogue, labUrl, start, upload, type Readers } from './api'
import { navigateWorkspace } from './navigation'
import type { Article } from '../shared/article'

function applicableReaders(article: Article, catalog: Awaited<ReturnType<typeof catalogue>>) {
  const tables = article.candidates.filter(item => item.kind === 'table' && item.extractable)
  const tablesNeedOcr = tables.some(item => item.table_read_features?.text_layer === 'ocr-required' ||
    item.discovery === 'ocr' || item.strategyPlan?.strategy?.includes('ocr'))
  const hasVectorFigure = article.overview?.has_text !== false && article.candidates.some(item =>
    item.kind === 'figure' && item.figureStatus === 'data' && item.discovery !== 'ocr')
  return {
    text: catalog.text.filter(item => item.id !== 'pdfplumber' || article.overview?.has_text !== false),
    table: tables.length ? catalog.table.filter(item => !item.requires_crop && item.id !== 'paddle-structure' &&
      (item.accepts_region || !tables.some(table => table.region)) &&
      (item.id !== 'docling-tableformer' || !tablesNeedOcr) &&
      (item.id !== 'docling-rapidocr' || tables.every(table => table.table_read_features?.header === 'method-top-k'))) : [],
    figure: hasVectorFigure ? catalog.figure.filter(item => !Array.isArray(item.parameters_schema.required) ||
      item.parameters_schema.required.length === 0).filter(item => item.id !== 'qwen-vision') : [],
  }
}

const kinds = [['text', 'Текст'], ['table', 'Таблицы'], ['figure', 'Графики']] as const

export function Upload() {
  const input = useRef<HTMLInputElement>(null)
  const [file, setFile] = useState<File>()
  const [article, setArticle] = useState<Article>()
  const [custom, setCustom] = useState(false)
  const [options, setOptions] = useState<ReturnType<typeof applicableReaders>>()
  const [readers, setReaders] = useState<Readers>({})
  const [feedback, setFeedback] = useState('')
  const [error, setError] = useState('')
  const [dragging, setDragging] = useState(false)

  function choose(files: FileList | null) {
    if (feedback || !files?.length) return
    const candidate = files[0]
    if (files.length !== 1) return setError('Выберите один PDF.')
    if (candidate.type !== 'application/pdf' && !/\.pdf$/i.test(candidate.name)) return setError('Выберите файл в формате PDF.')
    if (candidate.size > 64 * 1024 * 1024) return setError('PDF должен быть не больше 64 МБ.')
    setFile(candidate)
    setArticle(undefined)
    setOptions(undefined)
    setReaders({})
    setError('')
  }

  async function submit(event: FormEvent) {
    event.preventDefault()
    if (!file || feedback) return
    setError('')
    try {
      let uploaded = article
      if (!uploaded) {
        setFeedback('Загружаем PDF и строим карту…')
        uploaded = await upload(file)
        setArticle(uploaded)
      }
      if (custom && !options) {
        setFeedback('Загружаем доступные ридеры…')
        setOptions(applicableReaders(uploaded, await catalogue()))
        return
      }
      setFeedback('Ставим разбор в очередь…')
      const run = await start(uploaded.id, custom ? readers : undefined)
      navigateWorkspace(labUrl({ article: uploaded.id, run: run.id, tab: 'run' }))
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : 'Не удалось начать разбор.')
    } finally {
      setFeedback('')
    }
  }

  return <main className="parse-start">
    <section className="heading"><h1>Разобрать статью</h1><p>Выберите PDF и настройте чтение текста, таблиц и графиков.</p></section>
    <form onSubmit={submit}>
      <section className="card" aria-labelledby="file-heading">
        <h2 id="file-heading">Файл статьи</h2>
        <input id="pdf-file" ref={input} type="file" accept=".pdf,application/pdf" hidden onChange={event => {
          choose(event.target.files); event.target.value = ''
        }} />
        {file ? <div className="parse-file-row">
          <div><strong>{file.name}</strong><small>{article ? `Карта готова · страниц: ${article.page_count}` : 'Файл выбран локально.'}</small></div>
          <button type="button" disabled={!!feedback} onClick={() => input.current?.click()}>Заменить</button>
        </div> : <div className="parse-drop" data-dragging={dragging} onDragOver={event => {
          if (event.dataTransfer.types.includes('Files')) { event.preventDefault(); setDragging(true) }
        }} onDragLeave={event => {
          if (!(event.relatedTarget instanceof Node) || !event.currentTarget.contains(event.relatedTarget)) setDragging(false)
        }} onDrop={event => { event.preventDefault(); setDragging(false); choose(event.dataTransfer.files) }}>
          <p><strong>Перетащите PDF сюда</strong><small>Один файл до 64 МБ</small></p>
          <button className="action" type="button" onClick={() => input.current?.click()}>Выбрать файл</button>
        </div>}
        {error && <p className="notice" role="alert">{error}</p>}
      </section>
      <section className="card" aria-labelledby="settings-heading">
        <h2 id="settings-heading">Режим разбора</h2>
        <fieldset className="parse-modes" aria-labelledby="settings-heading" disabled={!!feedback}>
          <input id="parse-auto" type="radio" name="mode" checked={!custom} onChange={() => { setCustom(false); setError('') }} />
          <label htmlFor="parse-auto">Авто</label>
          <input id="parse-custom" type="radio" name="mode" checked={custom} onChange={() => { setCustom(true); setError('') }} />
          <label htmlFor="parse-custom">Настроить разбор</label>
        </fieldset>
        {!custom && <p>Пайплайн подбирает ридеры по карте статьи и признакам PDF.</p>}
        {custom && !options && <p>Доступные ридеры появятся после построения карты.</p>}
        {custom && options && <div className="parse-readers">
          <p>Выбор действует на весь прогон. При «Авто» ридер подбирается для каждого этапа.</p>
          {kinds.map(([name, label]) => options[name].length > 0 && <label key={name}><span>{label}</span>
            <select name={name} value={readers[name] ?? 'auto'} disabled={!!feedback}
              onChange={event => setReaders({ ...readers, [name]: event.target.value })}>
              <option value="auto">Авто</option>
              {options[name].map(reader => <option key={reader.id} value={reader.id}>{reader.id} · {reader.description}</option>)}
            </select>
          </label>)}
        </div>}
        <div className="parse-start-actions">
          {feedback && <p className="notice parse-feedback" role="status">{feedback}</p>}
          <button className="action" type="submit" disabled={!file || !!feedback}>
            {custom && !options ? article && error ? 'Повторить загрузку ридеров' : 'Загрузить и построить карту'
              : article && error ? 'Повторить запуск' : 'Запустить разбор'}
          </button>
        </div>
      </section>
    </form>
  </main>
}
