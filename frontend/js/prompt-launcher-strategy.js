/* Prompt Launcher, шаблони зі стратегії (task-102): оператор дає посилання на
 * Google Sheets або шлях до xlsx — бек віддає рядки сторінок/статей і для кожного
 * готовий, повністю заповнений лаунчер. Цей модуль лише показує таблицю й
 * кладе `row.rendered` у буфер: жодного шматка промта він не склеює сам
 * (PLAN §12 — бекенд рахує семантику, фронт рендерить).
 *
 * Відмітки «скопійовано» — `localStorage`, і на відміну від клієнтських шаблонів
 * (task-94) БЕЗ тижневого скидання: квартальний план робиться тижнями, і
 * автоскидання посеред роботи стерло б відмітки. Ключ рядка — `row_key`
 * (URL/slug теми), не номер рядка: стратег додає рядки, номери зсуваються.
 */
import {
  getPromptClients,
  getPromptLocalStrategies,
  loadPromptStrategy,
} from "./api.js?v=20260930-t102";
import { escapeHtml, formatTimestamp, icon } from "./formatters.js?v=20260914-t94";
import { panelSkeleton } from "./skeleton.js?v=20260914-t94";

const SOURCE_PLACEHOLDER =
  "https://docs.google.com/spreadsheets/d/… or out/…/GRP_PAGE_STRATEGY_….xlsx";

let host = null;
let template = null;
let toast = () => {};
let data = null;
let hideDone = false;
let clientOptions = null; // [{id, name}] — один раз за життя тула
let localLoaded = false;
let loadToken = 0;

const sourceKey = (templateKey) => `yovth.prompt-launcher.source.${templateKey}`;
const progressKey = (templateKey, ref) => `yovth.prompt-launcher.${templateKey}.${ref}`;

// Кожне читання/запис у try/catch: приватне вікно чи заблокований storage
// не мають ламати таблицю (frontend/AGENTS.md).
function readStorage(key) {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}
function writeStorage(key, value) {
  try {
    if (value === null) localStorage.removeItem(key);
    else localStorage.setItem(key, value);
  } catch {
    // best-effort
  }
}
function loadProgress(ref) {
  try {
    const parsed = JSON.parse(readStorage(progressKey(template.key, ref)) || "{}");
    return parsed.rows && typeof parsed.rows === "object" ? parsed.rows : {};
  } catch {
    return {};
  }
}
function saveProgress(ref, rows) {
  writeStorage(progressKey(template.key, ref), JSON.stringify({ rows }));
}

export function renderStrategyPanel(body, activeTemplate, hooks = {}) {
  host = body;
  template = activeTemplate;
  toast = hooks.toast || (() => {});
  data = null;
  hideDone = false;
  loadToken += 1;
  const noun = template.strategy_role === "blog" ? "articles" : "pages";
  host.innerHTML = `
    <form class="pls-form" id="pls-form" novalidate>
      <label class="form-field pls-input-field" for="pls-input">Strategy link or path
        <input id="pls-input" type="text" list="pls-local" autocomplete="off" spellcheck="false"
          placeholder="${escapeHtml(SOURCE_PLACEHOLDER)}">
      </label>
      <datalist id="pls-local"></datalist>
      <button class="button button--primary" type="submit" id="pls-load">${icon("table-import", "icon icon--tiny")}Load ${noun}</button>
    </form>
    <p class="pls-hint">A Google Sheets link (the agency Google account must have access) or a path to
      an <code>.xlsx</code> inside AISeoTools. Prefilled fields come straight from the strategy sheet.</p>
    <p class="pls-form-error" id="pls-error" role="alert" hidden></p>
    <div id="pls-result"></div>`;
  const input = host.querySelector("#pls-input");
  input.value = readStorage(sourceKey(template.key)) || "";
  host.querySelector("#pls-form").addEventListener("submit", (event) => {
    event.preventDefault();
    void load({});
  });
  void fillLocalSuggestions();
  if (input.value.trim()) void load({});
}

async function fillLocalSuggestions() {
  if (localLoaded) return;
  try {
    const { files } = await getPromptLocalStrategies();
    localLoaded = true;
    const list = host?.querySelector("#pls-local");
    if (!list) return;
    list.innerHTML = files
      .map((f) => `<option value="${escapeHtml(f.path_host)}">${escapeHtml(f.name)}</option>`)
      .join("");
  } catch {
    // Підказки — зручність, а не вимога: без них поле все одно працює.
  }
}

function showError(message) {
  const el = host.querySelector("#pls-error");
  if (!el) return;
  el.hidden = !message;
  el.textContent = message || "";
}

function describeError(error) {
  if (error.status === 403) return "Loading a strategy needs the member role — ask an admin.";
  return error.message || "Could not load the strategy.";
}

async function load({ sheet = null, clientId = null }) {
  const source = host.querySelector("#pls-input").value.trim();
  if (!source) {
    showError("Paste a Google Sheets link or a path to an .xlsx file.");
    return;
  }
  showError("");
  const token = ++loadToken;
  const result = host.querySelector("#pls-result");
  result.innerHTML = panelSkeleton({ label: "strategy", rows: 6, columns: 5 });
  try {
    const response = await loadPromptStrategy(template.key, { source, sheet, clientId });
    if (token !== loadToken) return;
    data = response;
    writeStorage(sourceKey(template.key), source);
  } catch (error) {
    if (token !== loadToken || error.name === "AbortError") return;
    result.innerHTML = "";
    showError(describeError(error));
    return;
  }
  await ensureClientOptions();
  if (token !== loadToken) return;
  renderResult();
}

// Список клієнтів бере з наявного `/{key}/clients` техаудиту (він віддає
// усіх клієнтів) — окремий ендпоінт заради одного `<select>` не потрібен.
async function ensureClientOptions() {
  if (clientOptions) return;
  try {
    const { rows } = await getPromptClients("techaudit");
    clientOptions = rows.map((r) => ({ id: r.client_id, name: r.client_name }));
  } catch {
    clientOptions = [];
  }
}

const RESOLVED_BY = {
  manual: "picked by you",
  domain: "matched by domain",
  filename_prefix: "matched by file name",
};

function clientSelectHtml() {
  const current = data.client?.id || "";
  const options = clientOptions.map(
    (c) => `<option value="${escapeHtml(c.id)}"${c.id === current ? " selected" : ""}>${escapeHtml(c.name)}</option>`,
  );
  if (current && !clientOptions.some((c) => c.id === current)) {
    options.unshift(`<option value="${escapeHtml(current)}" selected>${escapeHtml(data.client.name)}</option>`);
  }
  return `<label class="form-field pls-select-field" for="pls-client">Client
      <select id="pls-client">
        ${current ? "" : `<option value="" selected>Choose a client…</option>`}
        ${options.join("")}
      </select>
    </label>`;
}

function sheetSelectHtml() {
  const options = data.sheets.map((s) => {
    const suffix = s.selectable ? ` · ${s.row_count} ${template.strategy_role === "blog" ? "articles" : "pages"}` : " · not a source";
    return `<option value="${escapeHtml(s.name)}"${s.name === data.sheet ? " selected" : ""}${s.selectable ? "" : " disabled"}>${escapeHtml(s.name)}${escapeHtml(suffix)}</option>`;
  });
  return `<label class="form-field pls-select-field" for="pls-sheet">Sheet
      <select id="pls-sheet">${options.join("")}</select>
    </label>`;
}

function summaryHtml() {
  const src = data.source;
  const name = src.web_view_link
    ? `<a href="${escapeHtml(src.web_view_link)}" target="_blank" rel="noopener noreferrer">${escapeHtml(src.name)}${icon("external-link", "icon icon--tiny")}</a>`
    : escapeHtml(src.name);
  const updated = src.modified_time ? ` · updated ${escapeHtml(formatTimestamp(src.modified_time))}` : "";
  const how = data.client ? ` (${RESOLVED_BY[data.client.resolved_by] || data.client.resolved_by})` : "";
  return `<p class="pls-summary"><strong>${name}</strong>${updated}
      <span class="pls-client-note">${data.client ? `Client code <code>${escapeHtml(data.client.label)}</code>${escapeHtml(how)}` : "Client not recognised — choose one to enable copying."}</span></p>`;
}

function frameworkCell(row) {
  if (!row.has_framework) return `<span class="pl-missing">—</span>`;
  const badge = `<span class="pl-badge pl-badge--done">${icon("circle-check", "icon icon--tiny")}Done</span>`;
  return row.framework_link
    ? `<a class="pls-fw-link" href="${escapeHtml(row.framework_link)}" target="_blank" rel="noopener noreferrer">${badge}</a>`
    : badge;
}

function prefilledCell(row) {
  const total = data.prefillable.length;
  const title = data.prefillable
    .map((label) => `${row.prefilled.includes(label) ? "✓" : "–"} ${label}`)
    .join("\n");
  return `<span class="pls-prefilled" title="${escapeHtml(title)}">${row.prefilled.length} of ${total}</span>`;
}

function rowHtml(row, progress, isPage) {
  const copiedAt = progress[row.row_key];
  const disabled = !row.rendered;
  const titleAttr = row.blocked_reason ? ` title="${escapeHtml(row.blocked_reason)}"` : "";
  const warn = row.warnings.length
    ? `<span class="pl-badge pl-badge--warn" title="${escapeHtml(row.warnings.join("; "))}">${icon("alert-triangle", "icon icon--tiny")}${escapeHtml(row.warnings[0])}</span>`
    : "";
  return `
    <tr class="pl-row${copiedAt ? " is-copied" : ""}" data-row-key="${escapeHtml(row.row_key)}">
      <td class="pl-client-cell"><span class="pl-client-name">${escapeHtml(row.title)}</span>${warn}</td>
      ${isPage ? `<td><span class="pl-badge pl-badge--mode-${escapeHtml(row.mode || "existing")}">${escapeHtml(row.mode || "")}</span></td>
      <td class="pls-url">${row.url ? escapeHtml(row.url) : `<span class="pl-missing">—</span>`}</td>` : ""}
      <td>${row.primary_keyword ? escapeHtml(row.primary_keyword) : `<span class="pl-missing">—</span>`}</td>
      <td>${prefilledCell(row)}</td>
      <td>${frameworkCell(row)}</td>
      <td class="pl-copied-cell">${copiedAt ? escapeHtml(formatTimestamp(copiedAt)) : "—"}</td>
      <td class="pl-action-cell">
        <button class="button button--quiet pl-copy-btn" type="button" data-copy-row="${escapeHtml(row.row_key)}"${disabled ? " disabled" : ""}${titleAttr}>
          ${icon("copy", "icon icon--tiny")}Copy
        </button>
      </td>
    </tr>`;
}

function renderResult() {
  const result = host.querySelector("#pls-result");
  const isPage = template.strategy_role === "page";
  const noun = isPage ? "page" : "article";
  const rows = data.rows;
  const progress = loadProgress(data.source.ref);
  const visible = hideDone ? rows.filter((r) => !r.has_framework) : rows;

  const notices = data.warnings.map(
    (w) => `<p class="pls-notice">${icon("alert-triangle", "icon icon--tiny")}${escapeHtml(w)}</p>`,
  ).join("");

  let tableHtml;
  if (!data.sheet) {
    const names = data.sheets.map((s) => s.name).join(", ");
    tableHtml = `<p class="tools-empty">${icon("file-off")}<span>No ${noun} sheet found — this file has: ${escapeHtml(names || "no sheets")}.
      Pick the other template if it is a ${isPage ? "blog" : "page"} strategy.</span></p>`;
  } else if (!rows.length) {
    tableHtml = `<p class="tools-empty">${icon("file-off")}<span>Sheet “${escapeHtml(data.sheet)}” has no ${noun} rows.</span></p>`;
  } else {
    tableHtml = `
      <div class="pl-table-wrap">
        <table class="pl-table pls-table">
          <thead><tr>
            <th>${isPage ? "Page" : "Article"}</th>
            ${isPage ? "<th>Mode</th><th>URL</th>" : ""}
            <th>Primary keyword</th><th>Prefilled</th><th>Framework</th><th>Copied</th><th></th>
          </tr></thead>
          <tbody>
            ${visible.length
              ? visible.map((r) => rowHtml(r, progress, isPage)).join("")
              : `<tr><td colspan="${isPage ? 8 : 6}" class="pl-empty-row">Every ${noun} already has a framework.</td></tr>`}
          </tbody>
        </table>
      </div>`;
  }

  result.innerHTML = `
    ${summaryHtml()}
    <div class="pls-controls">
      ${sheetSelectHtml()}
      ${clientSelectHtml()}
      <button class="button button--quiet" type="button" id="pls-reload">${icon("refresh", "icon icon--tiny")}Reload</button>
      <label class="pls-toggle"><input type="checkbox" id="pls-hide-done"${hideDone ? " checked" : ""}>Hide rows that already have a framework</label>
      <span class="pl-copy-count" id="pls-count" aria-live="polite"></span>
      <button class="button button--quiet" type="button" id="pls-reset">Reset</button>
    </div>
    ${notices}
    ${tableHtml}
    <div class="pls-fallback" id="pls-fallback" hidden></div>`;

  result.querySelector("#pls-sheet")?.addEventListener("change", (e) => void load({
    sheet: e.currentTarget.value, clientId: manualClientId(),
  }));
  result.querySelector("#pls-client")?.addEventListener("change", (e) => {
    if (e.currentTarget.value) void load({ sheet: data.sheet, clientId: e.currentTarget.value });
  });
  result.querySelector("#pls-reload")?.addEventListener("click", () => void load({
    sheet: data.sheet, clientId: manualClientId(),
  }));
  result.querySelector("#pls-hide-done")?.addEventListener("change", (e) => {
    hideDone = e.currentTarget.checked;
    renderResult();
  });
  result.querySelector("#pls-reset")?.addEventListener("click", () => {
    writeStorage(progressKey(template.key, data.source.ref), null);
    renderResult();
  });
  result.querySelectorAll("[data-copy-row]").forEach((button) => {
    button.addEventListener("click", () => void onCopy(button));
  });
  updateCount();
}

// Ручний вибір переживає перезавантаження аркуша лише поки він у `data.client`
// з `resolved_by=manual` — автоматично знайдений клієнт не «прилипає».
function manualClientId() {
  return data?.client?.resolved_by === "manual" ? data.client.id : null;
}

function updateCount() {
  const counter = host.querySelector("#pls-count");
  if (!counter || !data) return;
  const progress = loadProgress(data.source.ref);
  const copyable = data.rows.filter((r) => r.rendered);
  if (!copyable.length) {
    counter.textContent = "";
    return;
  }
  const copied = copyable.filter((r) => progress[r.row_key]).length;
  counter.textContent = `${copied} of ${copyable.length} copied`;
}

async function onCopy(button) {
  const key = button.dataset.copyRow;
  const row = data.rows.find((r) => r.row_key === key);
  if (!row || !row.rendered) return;
  try {
    await navigator.clipboard.writeText(row.rendered);
  } catch {
    showFallback(row);
    return;
  }
  const progress = loadProgress(data.source.ref);
  progress[key] = new Date().toISOString();
  saveProgress(data.source.ref, progress);
  const tr = button.closest("tr");
  tr?.classList.add("is-copied");
  const cell = tr?.querySelector(".pl-copied-cell");
  if (cell) cell.textContent = formatTimestamp(progress[key]);
  flashCopied(button);
  updateCount();
}

// Браузер без дозволу на буфер: показуємо текст уже виділеним — «нічого не
// сталось» гірше за зайвий крок.
function showFallback(row) {
  toast("Could not copy automatically — press Ctrl/⌘+C on the selected text.");
  const box = host.querySelector("#pls-fallback");
  box.hidden = false;
  box.innerHTML = `<label class="form-field" for="pls-fallback-text">Prompt for “${escapeHtml(row.title)}”
    <textarea id="pls-fallback-text" rows="10" readonly></textarea></label>`;
  const area = box.querySelector("textarea");
  area.value = row.rendered;
  area.focus();
  area.select();
}

function flashCopied(button) {
  const original = button.innerHTML;
  button.innerHTML = `${icon("circle-check", "icon icon--tiny")}Copied`;
  button.disabled = true;
  setTimeout(() => {
    button.innerHTML = original;
    button.disabled = false;
  }, 1600);
}
