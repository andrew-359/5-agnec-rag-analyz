const element = (id) => document.getElementById(id);
const dataset = element("dataset");
const status = element("status");
const sourceLink = element("source-link");
const body = element("results");
const preview = element("preview");
const pageImage = element("page-image");
const highlight = element("highlight");
const precisionLabels = {
  cell: "Ячейка",
  text_fragment: "Фрагмент текста",
  estimated_cell: "Оценочная область ячейки",
  table: "Область таблицы",
  page: "Страница",
};
const statisticLabels = {
  mean: "Среднее", sd: "SD", sample_sd: "Выборочное SD", sem: "SEM", decrease: "Снижение", increase: "Прирост",
};
const unitLabels = { percentage_point: "п.п.", percentage_points: "п.п.", fraction: "доля" };
const conditionLabels = {
  method: "Метод",
  generator: "Генератор",
  dataset: "Датасет",
  metric: "Метрика",
  retrieval_top_k: "Top-k",
};
let runSource;
let tableSource;
let tableLabel;
let evidenceById;
let selection;

function openSource(source, description) {
  const fragments = (source.evidence_ids ?? []).map((id) => evidenceById.get(id)).filter(Boolean);
  const fragment = fragments.find((item) => item.box) ?? fragments[0];
  const page = fragment?.page ?? source.page;
  const box = fragment ? fragment.box : source.box;
  const precision = box ? (fragment?.precision ?? "cell") : "page";
  const table = fragment?.table ?? source.table;
  const location = table == null ? (precision === "page" ? null : precisionLabels[precision]) :
    (table === tableSource.table ? tableLabel : `Table ${table}`);
  element("source-location").textContent = [`Страница ${page}`, location].filter(Boolean).join(" · ");
  sourceLink.href = `${runSource.pdf}#page=${page}`;
  const headings = (source.headers ?? []).join(" → ");
  const details = element("source-details");
  element("source-description").textContent = [description, headings].filter(Boolean).join(" · ");
  element("source-fragment").textContent = fragment?.text === description ? "" : (fragment?.text ?? "");
  element("source-precision").textContent = `Точность привязки · ${precisionLabels[precision] ?? precisionLabels.page}`;
  details.hidden = false;

  const imagePath = runSource.previews?.[page] ?? (page === runSource.page ? runSource.preview : null);
  const message = element("preview-status");
  message.textContent = "Превью страницы недоступно. Откройте PDF по ссылке.";
  message.hidden = Boolean(imagePath);
  preview.hidden = !imagePath;
  highlight.hidden = !box || !imagePath;
  if (box) {
    const [left, top, right, bottom] = box;
    highlight.style.left = `${100 * left}%`;
    highlight.style.top = `${100 * top}%`;
    highlight.style.width = `${100 * (right - left)}%`;
    highlight.style.height = `${100 * (bottom - top)}%`;
  }

  pageImage.alt = `Страница ${page} исходного PDF`;
  if (imagePath && pageImage.getAttribute("src") !== imagePath) {
    pageImage.src = imagePath;
  } else if (pageImage.complete && pageImage.naturalWidth) {
    positionPreview();
  }
}

function positionPreview() {
  preview.scrollTop = highlight.hidden ? 0 :
    highlight.parentElement.offsetTop + highlight.offsetTop + (highlight.offsetHeight - preview.clientHeight) / 2;
}

pageImage.addEventListener("load", positionPreview);
pageImage.addEventListener("error", () => {
  preview.hidden = true;
  element("preview-status").hidden = false;
  pageImage.removeAttribute("src");
});

function sourceButton(text, source, description = text) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "value";
  button.textContent = text;
  button.setAttribute("aria-pressed", "false");
  button.addEventListener("click", () => {
    selection?.setAttribute("aria-pressed", "false");
    button.setAttribute("aria-pressed", "true");
    selection = button;
    openSource(source, description);
    if (matchMedia("(max-width: 860px)").matches) {
      element("pdf-source").scrollIntoView({ block: "start" });
    }
  });
  return button;
}

function resultNote(text) {
  const note = document.createElement("span");
  note.className = "result-note muted";
  note.textContent = text;
  return note;
}

function evidenceButtons(label, ids) {
  const fragments = Array.isArray(ids)
    ? [...new Set(ids)].map((id) => evidenceById.get(id)).filter(Boolean)
    : [];
  return fragments.map((fragment) => {
    const excerpt = typeof fragment.text === "string" ? sourceExcerpt(fragment.text) : "";
    const text = fragments.length > 1 ? [label, `стр. ${fragment.page}`, excerpt].filter(Boolean).join(" · ") : label;
    return sourceButton(text, { ...tableSource, evidence_ids: [fragment.id] }, label);
  });
}

function resultRow(result) {
  const row = document.createElement("tr");
  for (const field of ["method", "generator", "dataset", "metric"]) {
    const cell = row.insertCell();
    const links = evidenceButtons(result[field], result.condition_sources?.[field]);
    if (links.length) cell.append(...links);
    else cell.textContent = field === "generator" && !result[field] ? "Неизвестен" : result[field];
  }
  const components = Object.entries(result.components ?? {});
  if (components.length) row.firstElementChild.append(resultNote(
    components.map(([name, enabled]) => `${name} ${enabled ? "+" : "−"}`).join(" · ")
  ));
  if (result.baseline_method) row.firstElementChild.append(resultNote(`Относительно ${result.baseline_method}`));
  const topK = result.retrieval_top_k == null ? null : `Top-k ${result.retrieval_top_k}`;
  const topKLinks = topK ? evidenceButtons(topK, result.condition_sources?.retrieval_top_k) : [];
  const statistic = statisticLabels[result.statistic];
  const notes = [statistic, topKLinks.length ? null : topK].filter(Boolean).join(" · ");
  if (notes) row.lastElementChild.append(resultNote(notes));
  row.lastElementChild.append(...topKLinks);
  const valueCell = row.insertCell();
  valueCell.className = "number";
  const value = sourceButton(result.raw_value, result.source, [result.method, result.raw_value, statistic].filter(Boolean).join(" · "));
  value.setAttribute("aria-label", [
    result.method, result.generator, result.dataset, `${result.metric} ${result.raw_value}`,
    [statistic, topK].filter(Boolean).join(" · "),
  ].filter(Boolean).join(", "));
  valueCell.append(value);
  if (result.value !== result.raw_value || result.unit) {
    valueCell.append(resultNote([result.value, unitLabels[result.unit] ?? result.unit].filter(Boolean).join(" ")));
  }
  valueCell.append(...evidenceButtons("Основание шкалы", result.condition_sources?.gain_scale));
  return row;
}

function renderResults(results) {
  const filtered = results.filter((result) => !dataset.value || result.dataset === dataset.value);
  element("count").textContent = `${filtered.length} из ${results.length}`;
  status.textContent = "Нет результатов.";
  status.hidden = filtered.length > 0;
  element("table-wrap").hidden = filtered.length === 0;
  body.replaceChildren(...filtered.map(resultRow));
}

function sourceExcerpt(text) {
  const excerpt = text.replace(/\s+/g, " ").trim();
  return excerpt.length > 55 ? `${excerpt.slice(0, 55).replace(/\s+\S*$/, "")}…` : excerpt;
}

function remarkItem(remark) {
  const item = document.createElement("li");
  const rawText = remark.raw_text ?? "";
  const excerpt = sourceExcerpt(rawText);
  const label = excerpt ? `${remark.reason} · ${excerpt}` : remark.reason;
  item.append(sourceButton(label, remark.source, rawText));

  for (const [field, value] of Object.entries(remark.conditions ?? {})) {
    if (typeof value !== "string" && typeof value !== "number") continue;
    const text = `${conditionLabels[field] ?? field} · ${value}`;
    const links = evidenceButtons(text, remark.condition_sources?.[field]);
    item.append(...(links.length ? links : [resultNote(text)]));
  }
  const options = Array.isArray(remark.options) ? remark.options : [];
  const fieldLabel = conditionLabels[remark.field];
  for (const option of options) {
    if (typeof option?.value !== "string" && typeof option?.value !== "number") continue;
    const text = ["Вариант", fieldLabel, option.value, option.binding?.join(" / ")].filter((part) => part != null).join(" · ");
    const links = evidenceButtons(text, option.evidence_ids);
    item.append(...(links.length ? links : [resultNote(text)]));
  }
  return item;
}

function renderGrid(table, cells, contextIds, hasResults) {
  if (!table) return;
  const grid = element("grid");
  grid.hidden = false;
  grid.open = !hasResults || Boolean(table.unplaced_evidence_ids?.length);
  element("grid-size").textContent = `${table.rows} × ${table.columns}`;

  const tableEvidence = element("table-evidence");
  const links = [
    [table.evidence_ids?.[0], table.label],
    [table.caption_evidence_id, "Подпись таблицы"],
  ];
  for (const id of table.unplaced_evidence_ids ?? []) {
    const fragment = evidenceById.get(id);
    if (fragment) links.push([id, `OCR без позиции · ${fragment.text}`]);
  }
  for (const id of contextIds) {
    const fragment = evidenceById.get(id);
    if (!fragment) continue;
    const excerpt = sourceExcerpt(fragment.text);
    links.push([id, excerpt ? `Стр. ${fragment.page} · ${excerpt}` : `Контекст · стр. ${fragment.page}`]);
  }
  for (const [id, label] of links) {
    const fragment = evidenceById.get(id);
    if (fragment) tableEvidence.append(sourceButton(label, { ...tableSource, evidence_ids: [id] }, ""));
  }
  tableEvidence.hidden = tableEvidence.childElementCount === 0;

  const positions = new Map(cells.map((cell) => [cell.row * table.columns + cell.column, cell]));
  const covered = new Set();
  for (let row = 0; row < table.rows; row++) {
    const tr = element("grid-cells").insertRow();
    for (let column = 0; column < table.columns; column++) {
      const position = row * table.columns + column;
      if (covered.has(position)) continue;
      const cell = positions.get(position);
      const isHeader = ["column_header", "row_header"].includes(cell?.role);
      const td = document.createElement(isHeader ? "th" : "td");
      tr.append(td);
      if (!cell) continue;
      const rowSpan = cell.row_span ?? 1;
      const columnSpan = cell.column_span ?? 1;
      td.rowSpan = rowSpan;
      td.colSpan = columnSpan;
      if (isHeader) td.scope = cell.role === "row_header" ? "row" : "col";
      for (let y = row; y < row + rowSpan; y++) {
        for (let x = column; x < column + columnSpan; x++) covered.add(y * table.columns + x);
      }
      const source = { ...tableSource, evidence_ids: cell.evidence_ids?.length ? cell.evidence_ids : table.evidence_ids };
      const button = sourceButton(cell.text || "—", source, cell.text);
      if (!cell.text) button.setAttribute("aria-label", `Пустая ячейка, строка ${row + 1}, столбец ${column + 1}`);
      td.append(button);
    }
  }
}

function renderMetadata(run) {
  if (!run.strategy) return;
  const artifacts = element("raw-artifacts");
  for (const filename of run.raw_artifacts ?? []) {
    const item = document.createElement("li");
    const link = document.createElement("a");
    link.href = filename;
    link.target = "_blank";
    link.rel = "noopener";
    link.textContent = filename;
    item.append(link);
    artifacts.append(item);
  }
  artifacts.hidden = artifacts.childElementCount === 0;
  const info = element("run-info");
  const fields = {
    "Стратегия": run.strategy,
    "Длительность": run.elapsed_ms == null ? null : `${run.elapsed_ms} мс`,
    "Версии": Object.entries(run.versions ?? {}).map(([name, version]) => `${name} ${version}`).join(", "),
    "SHA-256 PDF": runSource.sha256,
  };
  for (const [label, value] of Object.entries(fields)) {
    if (!value) continue;
    const term = document.createElement("dt");
    const definition = document.createElement("dd");
    term.textContent = label;
    definition.textContent = value;
    info.append(term, definition);
  }
  if (Object.keys(run.settings ?? {}).length) {
    element("run-settings").textContent = JSON.stringify(run.settings, null, 2);
    element("run-settings").hidden = false;
  }
  element("run-metadata").hidden = false;
}

async function loadRun() {
  const response = await fetch("run.json");
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  const run = await response.json();

  runSource = run.source ?? { name: run.document, page: run.page, pdf: "source.pdf", preview: "page.png" };
  evidenceById = new Map((run.evidence ?? []).map((fragment) => [fragment.id, fragment]));
  tableSource = { page: runSource.page, table: run.table };
  tableLabel = run.grid?.label ?? `Table ${run.table}`;
  element("document-name").textContent = runSource.name;
  sourceLink.hidden = false;
  openSource(tableSource, "");

  const names = Array.from(new Set(run.results.map((result) => result.dataset))).sort();
  for (const name of names) dataset.add(new Option(name, name));
  dataset.disabled = names.length === 0;
  dataset.addEventListener("change", () => {
    selection?.setAttribute("aria-pressed", "false");
    selection = undefined;
    openSource(tableSource, "");
    renderResults(run.results);
  });
  renderResults(run.results);
  renderGrid(run.grid, run.cells ?? [], run.context_evidence_ids ?? [], run.results.length > 0);
  renderMetadata(run);

  for (const remark of run.remarks) {
    element("remark-list").append(remarkItem(remark));
  }
  element("remarks").hidden = run.remarks.length === 0;
}

loadRun().catch((error) => {
  status.textContent = `Не удалось открыть сохранённый прогон. ${error.message}`;
  status.hidden = false;
});
