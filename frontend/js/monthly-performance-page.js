// Monthly Performance sub-page (task-54) — the full sheet-fidelity table,
// reached via "Open Monthly Performance" from the compact summary card on
// the client page. Same split as Ahrefs/Blogs: `client-page.js` only shows a
// KPI-style preview, the full content (and its own controls — Refresh,
// Configure, Export) lives on its own route.
import { getClientMonthlyTable, refreshClientMonthlyTable, getClientMonthlyKeyEvents, putClientMonthlyTableSettings, monthlyTableXlsxUrl, getDashboard, isAbortError } from "./api.js?v=20260821-spin3";
import { clientHeaderSkeleton, sectionSkeleton } from "./skeleton.js?v=20260821-spin3";
import { buildClientHash } from "./router.js?v=20260821-spin3";
import { escapeHtml, formatTimestamp, icon, initials, STATE_META, stateClass } from "./formatters.js?v=20260821-spin3";
import { renderMonthlyTablePanel, renderMonthlyTableError, renderMonthlyTableSettingsNote } from "./monthly-table.js?v=20260821-spin3";

const root = document.querySelector("#monthly-performance-page-root");
let current = null;
let monthlyTable = null;
let keyEventCandidates;
let busy = false;
let refreshError = "";
let detailsOpen = false;

export async function initMonthlyPerformancePage(clientId, _query) {
  monthlyTable = null;
  keyEventCandidates = undefined;
  busy = false;
  refreshError = "";
  detailsOpen = false;
  current = { clientId };
  root.innerHTML = shell();
  await loadClient();
}

function shell() {
  return `
    <a class="back-link" href="${buildClientHash(current.clientId)}">${icon("arrow-left")}<span>Back to client</span></a>
    <header class="client-page__header" id="mp-header" aria-live="polite" aria-busy="true">
      ${clientHeaderSkeleton()}
    </header>
    <div id="mp-content">
      <section class="monthly-section" id="mp-monthly" aria-label="Monthly performance" aria-live="polite" aria-busy="true">
        ${sectionSkeleton({ label: "the monthly performance table", rows: 6, columns: 7 })}
      </section>
    </div>`;
}

async function loadClient() {
  try {
    const dashboard = await getDashboard({ period: "week" });
    const client = (dashboard.clients || []).find((item) => item.client_id === current.clientId);
    if (!client) {
      renderNotFound();
      return;
    }
    renderHeader(client);
    await loadMonthlyTable();
  } catch (error) {
    if (isAbortError(error)) return;
    const header = document.querySelector("#mp-header");
    header.setAttribute("aria-busy", "false");
    header.innerHTML = `
      <div class="state-panel state-failed">
        ${icon("wifi-off")}
        <p><strong>Could not load this client.</strong><br>${escapeHtml(error.message)}. Nothing was changed — retry once the connection is back.</p>
        <button type="button" class="button button--primary" id="mp-retry">${icon("refresh")}Retry</button>
      </div>`;
    header.querySelector("#mp-retry").addEventListener("click", () => {
      header.setAttribute("aria-busy", "true");
      header.innerHTML = clientHeaderSkeleton();
      loadClient();
    });
  }
}

function renderNotFound() {
  root.querySelector("#mp-header").setAttribute("aria-busy", "false");
  root.querySelector("#mp-header").innerHTML = `
    <div class="state-panel state-failed">
      ${icon("user-x")}
      <p><strong>Client not found.</strong><br>The link may be outdated.</p>
      <a class="button button--primary" href="#/">Back to client list</a>
    </div>`;
  document.querySelector("#mp-content")?.remove();
}

function renderHeader(client) {
  const state = STATE_META[client.overall_status] || STATE_META.attention;
  const logo = client.logo_path
    ? `<img class="client-logo" src="${escapeHtml(client.logo_path)}" width="52" height="52" alt="Logo of ${escapeHtml(client.name)}" loading="lazy">`
    : `<span class="client-logo" aria-hidden="true">${escapeHtml(initials(client.name))}</span>`;
  document.querySelector("#mp-header").innerHTML = `
    <div class="client-page__identity">
      ${logo}
      <div>
        <p class="eyebrow">Monthly Performance</p>
        <h1>${escapeHtml(client.name)}</h1>
        <a href="https://${encodeURI(client.domain)}" target="_blank" rel="noreferrer">${escapeHtml(client.domain)}</a>
      </div>
    </div>
    <div class="client-page__status">
      <span class="status-label ${stateClass(client.overall_status)}">${icon(state.icon)}${state.label}</span>
      <span class="client-page__sync">Last collection: ${formatTimestamp(client.last_synced_at, "not yet")}</span>
    </div>`;
  document.querySelector("#mp-header").setAttribute("aria-busy", "false");
}

async function loadMonthlyTable() {
  try {
    monthlyTable = await getClientMonthlyTable(current.clientId);
  } catch (error) {
    if (isAbortError(error)) return;
    renderMonthlyTableError(document.querySelector("#mp-monthly"), error.message, {
      onRetry: () => loadMonthlyTable(),
    });
    return;
  }
  renderSection();
}

function renderSection() {
  const container = document.querySelector("#mp-monthly");
  if (!container) return;
  renderMonthlyTablePanel(container, {
    table: monthlyTable,
    settings: monthlyTable?.settings,
    keyEventCandidates,
    busy,
    refreshError,
    detailsOpen,
    exportUrl: monthlyTable ? monthlyTableXlsxUrl(current.clientId) : null,
    onRefresh: () => refresh(),
    onSaveSettings: (payload) => saveSettings(payload),
    onLoadKeyEvents: () => loadKeyEvents(),
  });
  container.setAttribute("aria-busy", "false");
}

async function refresh() {
  if (busy) return;
  busy = true;
  refreshError = "";
  renderSection();
  try {
    const result = await refreshClientMonthlyTable(current.clientId);
    monthlyTable = result.table;
  } catch (error) {
    if (isAbortError(error)) { busy = false; return; }
    refreshError = `Could not refresh: ${error.message}`;
  } finally {
    busy = false;
    renderSection();
  }
}

async function loadKeyEvents() {
  detailsOpen = true;
  try {
    const result = await getClientMonthlyKeyEvents(current.clientId);
    keyEventCandidates = result.candidates;
  } catch (error) {
    if (isAbortError(error)) return;
    keyEventCandidates = [];
  }
  renderSection();
}

async function saveSettings(payload) {
  detailsOpen = true;
  let noteMessage = "Settings saved.";
  let noteIsError = false;
  try {
    await putClientMonthlyTableSettings(current.clientId, payload);
  } catch (error) {
    if (isAbortError(error)) return;
    if (error.code === "attribution_changed") {
      // Non-blocking (task-54 §2.5) — the settings really were saved
      // server-side despite the 409; the message names which months were
      // collected under the previous attribution.
      noteMessage = error.message;
    } else {
      noteMessage = `Could not save: ${error.message}`;
      noteIsError = true;
    }
  }
  if (!noteIsError) {
    // Reload the full table, not just the settings: `coverage.has_key_event_column`
    // and the named-event column's header label live on the table DTO too.
    try {
      monthlyTable = await getClientMonthlyTable(current.clientId);
    } catch {
      /* keep the previous table in view; the note above still reports what happened */
    }
  }
  renderSection();
  renderMonthlyTableSettingsNote(document.querySelector("#mp-monthly"), noteMessage, { isError: noteIsError });
}
