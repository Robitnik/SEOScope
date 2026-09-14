/* Prompt Launcher (task-94): pick a weekly prompt template and copy it
 * pre-filled for any client, without retyping the client code and domain
 * into the same header by hand every Monday.
 *
 * Read-only tool, no persistence server-side: the backend renders the
 * launcher text and hands it over ready-to-copy (PLAN §12 — this module
 * never assembles the prompt itself). "Copied this week" is a per-browser
 * convenience (`localStorage`), not a server fact — it self-clears on a new
 * ISO week, same rule documented in the task plan §5.
 */
import { getPromptClients, getPromptSource, getPromptTemplates } from "./api.js?v=20260914-t94";
import { buildToolsHash } from "./router.js?v=20260914-t94";
import { CLIENT_STATUS_META, devBadge } from "./client-status.js?v=20260914-t94";
import { escapeHtml, formatTimestamp, icon } from "./formatters.js?v=20260914-t94";
import { panelSkeleton } from "./skeleton.js?v=20260914-t94";

let root = null;
let templates = [];
let currentKey = null;
let currentRows = [];
const sourceCache = new Map(); // templateKey -> { text, truncated }

function toast(message) {
  const region = document.querySelector("#toast-region");
  if (!region) return;
  const element = document.createElement("div");
  element.className = "toast-message";
  element.textContent = message;
  region.append(element);
  setTimeout(() => element.remove(), 4200);
}

// ISO 8601 week, computed from the LOCAL calendar date (task-94 §5: "за
// таймзоною браузера") — deliberately not the UTC-normalized textbook
// version, since the point is "which week does the operator see today".
function isoWeekKey(date = new Date()) {
  const d = new Date(date.getFullYear(), date.getMonth(), date.getDate());
  const dayNum = (d.getDay() + 6) % 7; // Monday = 0 … Sunday = 6
  d.setDate(d.getDate() - dayNum + 3);
  const firstThursday = new Date(d.getFullYear(), 0, 4);
  const firstDayNum = (firstThursday.getDay() + 6) % 7;
  firstThursday.setDate(firstThursday.getDate() - firstDayNum + 3);
  const week = 1 + Math.round((d - firstThursday) / (7 * 86400000));
  return `${d.getFullYear()}-W${String(week).padStart(2, "0")}`;
}

function progressKey(templateKey) {
  return `yovth.prompt-launcher.${templateKey}`;
}

// Every read/write is wrapped: a private window or blocked site data must
// never break the table, it should just run without a memory of what was
// copied (frontend/AGENTS.md — no feature may hard-depend on storage).
function loadProgress(templateKey) {
  try {
    const raw = localStorage.getItem(progressKey(templateKey));
    if (!raw) return {};
    const parsed = JSON.parse(raw);
    if (parsed.week !== isoWeekKey()) return {}; // new week — start clean
    return parsed.clients && typeof parsed.clients === "object" ? parsed.clients : {};
  } catch {
    return {};
  }
}

function saveProgress(templateKey, clients) {
  try {
    localStorage.setItem(
      progressKey(templateKey),
      JSON.stringify({ week: isoWeekKey(), clients }),
    );
  } catch {
    // Best-effort only — see loadProgress above.
  }
}

function clearProgress(templateKey) {
  try {
    localStorage.removeItem(progressKey(templateKey));
  } catch {
    // Best-effort only.
  }
}

export async function initTool(mount) {
  root = mount;
  root.innerHTML = shell();
  root.querySelector("#pl-template-select").addEventListener("change", onTemplateChange);
  root.querySelector("#pl-source").addEventListener("toggle", onSourceToggle);
  root.querySelector("#pl-reset").addEventListener("click", onReset);
  await loadTemplates();
}

function shell() {
  return `
    <a class="back-link" href="${buildToolsHash()}">${icon("arrow-left")}<span>All tools</span></a>
    <header class="tool-header">
      <p class="eyebrow">Tools / Prompt Launcher</p>
      <h1>Prompt launcher</h1>
      <p class="tool-lead">Pick a weekly prompt template, then copy it pre-filled for any
        client — no more typing the client code and domain into the same header by hand.</p>
    </header>
    <section class="tool-panel" aria-labelledby="pl-panel-title">
      <div class="section-heading">
        <div>
          <h2 id="pl-panel-title">Prompt</h2>
          <p class="tool-panel__hint" id="pl-doc-hint">Loading…</p>
        </div>
        <label class="form-field pl-template-field" for="pl-template-select">Template
          <select id="pl-template-select"></select>
        </label>
      </div>
      <div class="tool-panel__body">
        <p class="pl-summary" id="pl-summary"></p>
        <details class="pl-source" id="pl-source">
          <summary>${icon("code", "icon icon--tiny")}Prompt source</summary>
          <div class="pl-source-body" id="pl-source-body"></div>
        </details>
      </div>
    </section>
    <section class="tool-panel" id="pl-clients" aria-labelledby="pl-clients-title">
      <div class="section-heading">
        <div><h2 id="pl-clients-title">Clients</h2></div>
        <div class="pl-clients-meta">
          <p class="pl-copy-count" id="pl-copy-count" aria-live="polite"></p>
          <button class="button button--quiet" type="button" id="pl-reset">Reset</button>
        </div>
      </div>
      <div class="tool-panel__body" id="pl-clients-body">
        ${panelSkeleton({ label: "clients", rows: 6, columns: 4 })}
      </div>
    </section>`;
}

async function loadTemplates() {
  try {
    const data = await getPromptTemplates();
    templates = data.templates || [];
  } catch (error) {
    renderTemplateLoadError(error);
    return;
  }
  if (!templates.length) {
    root.querySelector("#pl-doc-hint").textContent = "No prompt templates are registered yet.";
    root.querySelector("#pl-clients-body").innerHTML =
      `<p class="tools-empty">${icon("file-off")}<span>No prompt templates are registered yet.</span></p>`;
    return;
  }
  const select = root.querySelector("#pl-template-select");
  select.innerHTML = templates
    .map((t) => `<option value="${escapeHtml(t.key)}">${escapeHtml(t.name)}</option>`)
    .join("");
  currentKey = templates[0].key;
  select.value = currentKey;
  renderTemplateHeader();
  await loadClients();
}

function renderTemplateLoadError(error) {
  root.querySelector("#pl-doc-hint").textContent = "";
  root.querySelector("#pl-clients-body").innerHTML = `
    <div class="state-panel state-failed">
      ${icon("wifi-off")}
      <p><strong>Could not load prompt templates.</strong><br>${escapeHtml(error.message)}</p>
      <button class="button button--primary" type="button" id="pl-retry-templates">Try again</button>
    </div>`;
  root.querySelector("#pl-retry-templates").addEventListener("click", () => void loadTemplates());
}

function currentTemplate() {
  return templates.find((t) => t.key === currentKey) || null;
}

function renderTemplateHeader() {
  const template = currentTemplate();
  const hint = root.querySelector("#pl-doc-hint");
  const summary = root.querySelector("#pl-summary");
  if (!template) {
    hint.textContent = "";
    summary.textContent = "";
    return;
  }
  summary.textContent = template.summary;
  if (!template.doc_available) {
    hint.innerHTML = `${icon("alert-triangle", "icon icon--tiny")}
      Prompt file is not available at <code>${escapeHtml(template.doc_host_path)}</code> —
      copying is disabled until it is mounted.`;
  } else {
    const updated = template.doc_updated_at
      ? `updated ${escapeHtml(formatTimestamp(template.doc_updated_at))}`
      : "update date unknown";
    hint.innerHTML = `<code>${escapeHtml(template.doc_relpath)}</code> · ${updated}`;
  }
  // Switching templates invalidates whatever was open — collapse it rather
  // than show the previous template's source under the new one's summary.
  const details = root.querySelector("#pl-source");
  details.open = false;
  root.querySelector("#pl-source-body").innerHTML = "";
}

async function onTemplateChange(event) {
  currentKey = event.currentTarget.value;
  renderTemplateHeader();
  await loadClients();
}

async function onSourceToggle(event) {
  if (!event.currentTarget.open || !currentKey) return;
  const body = root.querySelector("#pl-source-body");
  if (sourceCache.has(currentKey)) {
    renderSource(body, sourceCache.get(currentKey));
    return;
  }
  body.innerHTML = panelSkeleton({ label: "prompt source", rows: 8, columns: 1 });
  try {
    const source = await getPromptSource(currentKey);
    sourceCache.set(currentKey, source);
    renderSource(body, source);
  } catch (error) {
    body.innerHTML = `
      <div class="state-panel state-failed">
        ${icon("wifi-off")}
        <p><strong>Could not load the prompt file.</strong><br>${escapeHtml(error.message)}</p>
      </div>`;
  }
}

function renderSource(body, source) {
  body.innerHTML = `
    <pre class="pl-source-pre">${escapeHtml(source.text)}</pre>
    ${source.truncated ? `<p class="pl-source-truncated">${icon("info-circle", "icon icon--tiny")}Showing the first part only — the file is larger than this preview.</p>` : ""}`;
}

async function loadClients() {
  const body = root.querySelector("#pl-clients-body");
  body.innerHTML = panelSkeleton({ label: "clients", rows: 6, columns: 4 });
  const template = currentTemplate();
  if (!template) return;
  try {
    const data = await getPromptClients(template.key);
    currentRows = data.rows || [];
  } catch (error) {
    body.innerHTML = `
      <div class="state-panel state-failed">
        ${icon("wifi-off")}
        <p><strong>Could not load clients.</strong><br>${escapeHtml(error.message)}</p>
        <button class="button button--primary" type="button" id="pl-retry-clients">Try again</button>
      </div>`;
    body.querySelector("#pl-retry-clients").addEventListener("click", () => void loadClients());
    return;
  }
  renderClientsTable();
}

function statusBadge(row) {
  if (row.collectable) return "";
  if (!row.enabled) {
    return `<span class="pl-badge pl-badge--hidden">${icon("eye-off", "icon icon--tiny")}Hidden</span>`;
  }
  const meta = CLIENT_STATUS_META[row.status] || CLIENT_STATUS_META.paused;
  return `<span class="pl-badge pl-badge--paused">${icon(meta.icon, "icon icon--tiny")}${escapeHtml(meta.label)}</span>`;
}

function fieldCell(value) {
  if (value === null || value === undefined) return `<span class="pl-missing">—</span>`;
  return escapeHtml(value);
}

function rowHtml(row, fields, progress) {
  const copiedAt = progress[row.client_id];
  const disabled = !row.rendered;
  const titleAttr = row.blocked_reason ? ` title="${escapeHtml(row.blocked_reason)}"` : "";
  return `
    <tr class="pl-row${copiedAt ? " is-copied" : ""}" data-client-id="${escapeHtml(row.client_id)}">
      <td class="pl-client-cell">
        <span class="pl-client-name">${escapeHtml(row.client_name)}</span>
        ${devBadge(row)}
        ${statusBadge(row)}
      </td>
      ${fields.map((f) => `<td>${fieldCell(row.values[f.key])}</td>`).join("")}
      <td class="pl-copied-cell">${copiedAt ? escapeHtml(formatTimestamp(copiedAt)) : "—"}</td>
      <td class="pl-action-cell">
        <button class="button button--quiet pl-copy-btn" type="button"
          data-copy-client="${escapeHtml(row.client_id)}"${disabled ? " disabled" : ""}${titleAttr}>
          ${icon("copy", "icon icon--tiny")}Copy
        </button>
      </td>
    </tr>`;
}

function renderClientsTable() {
  const body = root.querySelector("#pl-clients-body");
  const template = currentTemplate();
  if (!currentRows.length) {
    body.innerHTML = `
      <p class="tools-empty">${icon("users")}<span>No clients yet.
        <a href="#/clients">Add one</a> to see it here.</span></p>`;
    updateCopyCounter([]);
    return;
  }

  const fields = template.fields;
  const primary = currentRows.filter((r) => r.collectable);
  const secondary = currentRows.filter((r) => !r.collectable);
  const progress = loadProgress(currentKey);
  const colCount = fields.length + 3;

  body.innerHTML = `
    <div class="pl-table-wrap">
      <table class="pl-table">
        <thead><tr>
          <th>Client</th>
          ${fields.map((f) => `<th><code>{{${escapeHtml(f.label)}}}</code></th>`).join("")}
          <th>Copied</th>
          <th></th>
        </tr></thead>
        <tbody>
          ${primary.length
            ? primary.map((r) => rowHtml(r, fields, progress)).join("")
            : `<tr><td colspan="${colCount}" class="pl-empty-row">No collectable clients right now${secondary.length ? " — check “Show paused clients” below." : "."}</td></tr>`}
        </tbody>
      </table>
    </div>
    ${secondary.length
      ? `<button class="link-button pl-secondary-toggle" type="button" id="pl-secondary-toggle"
           aria-expanded="false">${icon("chevron-down", "icon icon--tiny")}Show ${secondary.length}
           paused/hidden ${secondary.length === 1 ? "client" : "clients"}</button>
         <div class="pl-table-wrap" id="pl-secondary-wrap" hidden>
           <table class="pl-table">
             <tbody>
               ${secondary.map((r) => rowHtml(r, fields, progress)).join("")}
             </tbody>
           </table>
         </div>`
      : ""}`;

  body.querySelector("#pl-secondary-toggle")?.addEventListener("click", (event) => {
    const wrap = body.querySelector("#pl-secondary-wrap");
    const expanded = !wrap.hidden;
    wrap.hidden = expanded;
    event.currentTarget.setAttribute("aria-expanded", String(!expanded));
  });

  body.querySelectorAll("[data-copy-client]").forEach((button) => {
    button.addEventListener("click", () => void onCopy(button));
  });

  updateCopyCounter(primary);
}

async function onCopy(button) {
  const clientId = button.dataset.copyClient;
  const row = currentRows.find((r) => r.client_id === clientId);
  if (!row || !row.rendered) return;
  try {
    await navigator.clipboard.writeText(row.rendered);
  } catch {
    toast("Could not copy — the clipboard is unavailable in this browser.");
    return;
  }
  const progress = loadProgress(currentKey);
  progress[clientId] = new Date().toISOString();
  saveProgress(currentKey, progress);
  flashCopied(button);
  const cell = button.closest("tr")?.querySelector(".pl-copied-cell");
  if (cell) cell.textContent = formatTimestamp(progress[clientId]);
  button.closest("tr")?.classList.add("is-copied");
  updateCopyCounter(currentRows.filter((r) => r.collectable));
}

// Only ever called from `onCopy`, which already required `row.rendered` to
// be truthy before getting here — so the row was never disabled, and the
// button always goes back to enabled once the flash clears.
function flashCopied(button) {
  const original = button.innerHTML;
  button.innerHTML = `${icon("circle-check", "icon icon--tiny")}Copied`;
  button.disabled = true;
  setTimeout(() => {
    button.innerHTML = original;
    button.disabled = false;
  }, 1600);
}

function updateCopyCounter(primaryRows) {
  const counter = root.querySelector("#pl-copy-count");
  if (!counter) return;
  if (!primaryRows.length) {
    counter.textContent = "";
    return;
  }
  const progress = loadProgress(currentKey);
  const copyable = primaryRows.filter((r) => r.rendered);
  const copied = copyable.filter((r) => progress[r.client_id]).length;
  counter.textContent = `${copied} of ${copyable.length} copied this week`;
}

function onReset() {
  if (!currentKey) return;
  clearProgress(currentKey);
  renderClientsTable();
}
