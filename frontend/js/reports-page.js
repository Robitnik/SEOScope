// Reports — portfolio history (task-43 §2). See
// doc/dev/new/task-43-reports-ui.md for the full spec this file implements.
// Structured like monitor-page.js: a top-level page with no client id, its
// own `paramsFromQuery`/`persist()` pair backed by
// `buildReportsHash`/`replaceReportsHash`, `root.innerHTML = shell()` on
// every `initReportsPage` call, and one delegated listener set bound once on
// the freshly-created shell.

import {
  getDashboard, isAbortError, listReportBatches, listReportRuns, reportBatchPdfArchiveUrl,
} from "./api.js?v=20260819-spin3";
import {
  buildReportsBatchHash, buildReportsRunHash, replaceReportsHash,
} from "./router.js?v=20260819-spin3";
import {
  REPORT_CROSS_CHECK_META, REPORT_KIND_META, REPORT_RUN_STATUS_META, REPORT_SOURCE_META,
  escapeHtml, formatDuration, formatTimestamp, icon, isTerminal, stateClass, statusIcon,
} from "./formatters.js?v=20260819-spin3";
import { sectionSkeleton } from "./skeleton.js?v=20260819-spin3";
import { openReportsWizard } from "./reports-wizard.js?v=20260819-spin3";

const root = document.querySelector("#reports-page-root");
const PAGE_SIZE = 25;
// Live rows (queued/running) need their own progress to move without a full
// reload — a light poll on the same `reports-history` intent, only while at
// least one such row is on screen (cleared otherwise so an all-terminal page
// never re-fetches for nothing).
const LIVE_POLL_MS = 2500;

function toast(message) {
  const region = document.querySelector("#toast-region");
  const element = document.createElement("div");
  element.className = "toast-message";
  element.textContent = message;
  region.append(element);
  setTimeout(() => element.remove(), 4200);
}

function paramsFromQuery(query) {
  return {
    client: query.get("client") || "",
    kind: ["monthly", "yearly", "custom_months", "comparator"].includes(query.get("kind")) ? query.get("kind") : "",
    source: ["api", "pdf"].includes(query.get("source")) ? query.get("source") : "",
    status: ["queued", "running", "succeeded", "partial", "failed", "cancelled"].includes(query.get("status")) ? query.get("status") : "",
    // Scope (task-47 §3.5 point 6): `all` is the neutral default (single
    // runs + batch cards, never a batch's own children as separate rows —
    // that would be the "14 near-identical rows" problem this filter exists
    // to prevent).
    scope: ["single", "batches"].includes(query.get("scope")) ? query.get("scope") : "all",
    q: query.get("q") || "",
    offset: Math.max(0, Number.parseInt(query.get("offset"), 10) || 0),
  };
}

function persist() {
  replaceReportsHash({
    client: current.client || "", kind: current.kind || "", source: current.source || "",
    status: current.status || "", scope: current.scope === "all" ? "" : current.scope,
    q: current.q || "", offset: current.offset || "",
  });
}

let current = null;
let requestToken = 0;
let lastData = null;
let liveTimer = null;

export async function initReportsPage(query) {
  current = paramsFromQuery(query);
  lastData = null;
  stopLivePoll();
  root.innerHTML = shell();
  bindDelegatedEvents();
  await load();
}

function shell() {
  return `
    <header class="reports-header">
      <div>
        <p class="eyebrow">Reports</p>
        <h1>Client report runs</h1>
        <p class="reports-lead">Every report ever generated, API-fast or PDF-verified, with live progress while it runs.</p>
      </div>
      <button type="button" class="button button--primary" id="reports-generate">
        ${icon("plus")}Generate report
      </button>
    </header>
    <section class="reports-content" id="rp-content" aria-busy="true">
      ${sectionSkeleton({ label: "report history", rows: 8, columns: 8 })}
    </section>`;
}

function bindDelegatedEvents() {
  root.addEventListener("click", onRootClick);
  root.addEventListener("change", onRootChange);
  root.addEventListener("input", onRootInput);
  root.addEventListener("keydown", onRootKeydown);
}

function onRootClick(event) {
  if (event.target.closest("#reports-generate")) { openWizard(event.target.closest("#reports-generate")); return; }
  const retry = event.target.closest("[data-retry]");
  if (retry) { void load(); return; }
  const clearFilters = event.target.closest("[data-clear-filters]");
  if (clearFilters) {
    current = { client: "", kind: "", source: "", status: "", scope: "all", q: "", offset: 0 };
    persist();
    void load();
    return;
  }
  const batchRow = event.target.closest("[data-batch-row]");
  if (batchRow && !event.target.closest("a,button")) {
    location.hash = buildReportsBatchHash(batchRow.dataset.batchRow);
    return;
  }
  const row = event.target.closest("[data-run-row]");
  if (row && !event.target.closest("a,button")) {
    location.hash = buildReportsRunHash(row.dataset.runRow);
    return;
  }
  const page = event.target.closest("[data-page]");
  if (page) {
    current.offset = page.dataset.page === "prev"
      ? Math.max(0, current.offset - PAGE_SIZE)
      : current.offset + PAGE_SIZE;
    persist();
    void load({ quiet: true });
  }
}

let searchDebounceTimer = null;

function onRootChange(event) {
  if (event.target.id === "rp-f-search") return; // handled by onRootInput (debounced, live)
  if (!["rp-f-client", "rp-f-kind", "rp-f-source", "rp-f-status", "rp-f-scope"].includes(event.target.id)) return;
  const key = event.target.id.replace("rp-f-", "");
  current[key] = event.target.value;
  current.offset = 0;
  persist();
  void load({ quiet: true });
}

// Local, instant filter (frontend/AGENTS.md §12 "abort obsolete fetches" +
// task-47 §3.5 point 2) — 14 clients is faster to type-ahead than to scroll
// a dropdown for, but there is no `GET /reports/runs?q=` on the backend
// (CLAUDE.md's own rule against adding a server param nobody asked for
// yet), so this filters the ALREADY LOADED page rather than re-querying.
function onRootInput(event) {
  if (event.target.id !== "rp-f-search") return;
  current.q = event.target.value;
  window.clearTimeout(searchDebounceTimer);
  searchDebounceTimer = window.setTimeout(() => {
    persist();
    renderTable();
  }, 200);
}

function onRootKeydown(event) {
  if (event.target.id === "rp-f-search" && event.key === "Escape" && current.q) {
    current.q = "";
    event.target.value = "";
    persist();
    renderTable();
  }
}

function openWizard(trigger) {
  if (!lastData) return;
  openReportsWizard({
    clients: lastData.clients,
    trigger,
    onCreated: (runId) => { location.hash = buildReportsRunHash(runId); },
    onBatchCreated: (batchId) => { location.hash = buildReportsBatchHash(batchId); },
  });
}

// --- Data loading ------------------------------------------------------
//
// `Scope` (task-47 §3.5 point 6) decides which of the two independent,
// independently-paginated sources to fetch — a batch's own children are
// NEVER fetched as standalone rows in any scope (`hasBatch: false`), so a
// 14-client batch is always exactly one row here, however scope is set.

function fetchRuns() {
  if (current.scope === "batches") return Promise.resolve({ rows: [], total: 0 });
  return listReportRuns({
    clientId: current.client || undefined, kind: current.kind || undefined,
    source: current.source || undefined, status: current.status || undefined,
    hasBatch: false, limit: PAGE_SIZE, offset: current.offset,
  });
}

function fetchBatches() {
  if (current.scope === "single") return Promise.resolve({ rows: [], total: 0 });
  return listReportBatches({
    clientId: current.client || undefined, status: current.status || undefined,
    limit: PAGE_SIZE, offset: current.offset,
  });
}

// Batch list has no `kind`/`source` server filter (few enough batches that
// a client-side pass is simpler than a new backend parameter — CLAUDE.md's
// own rule against adding one nobody has asked for yet).
function filterBatchRows(rows) {
  return rows
    .filter((batch) => !current.kind || batch.kind === current.kind)
    .filter((batch) => !current.source || batch.source === current.source);
}

function batchContainsQuery(batch, query, clientsById) {
  return batch.clients.some((c) => clientMatchesQuery(c.client_id, query, clientsById));
}

function clientMatchesQuery(clientId, query, clientsById) {
  const client = clientsById.get(clientId);
  const name = (client?.name || clientId).toLowerCase();
  const domain = (client?.domain || "").toLowerCase();
  return name.includes(query) || domain.includes(query);
}

function buildMergedRows(runsPage, batchesPage, clientsById) {
  const runRows = (runsPage.rows || []).map((run) => ({ kind: "run", createdAt: run.queued_at, run }));
  const batchRows = filterBatchRows(batchesPage.rows || [])
    .map((batch) => ({ kind: "batch", createdAt: batch.queued_at, batch }));
  let rows = [...runRows, ...batchRows].sort((a, b) => Date.parse(b.createdAt) - Date.parse(a.createdAt));
  const query = current.q.trim().toLowerCase();
  if (query) {
    rows = rows.filter((row) => row.kind === "run"
      ? clientMatchesQuery(row.run.client_id, query, clientsById)
      : batchContainsQuery(row.batch, query, clientsById));
  }
  return rows;
}

async function load({ quiet = false } = {}) {
  const token = ++requestToken;
  const content = root.querySelector("#rp-content");
  if (!quiet) {
    content.setAttribute("aria-busy", "true");
    content.innerHTML = sectionSkeleton({ label: "report history", rows: 8, columns: 8 });
  }
  try {
    const [dashboard, runsPage, batchesPage] = await Promise.all([
      getDashboard({ period: "week", requestKey: "reports-dashboard-clients" }),
      fetchRuns(),
      fetchBatches(),
    ]);
    if (token !== requestToken) return;
    const clients = dashboard.clients || [];
    const clientsById = new Map(clients.map((client) => [client.client_id, client]));
    lastData = {
      clients, clientsById, runsPage, batchesPage,
      rows: buildMergedRows(runsPage, batchesPage, clientsById),
      total: (runsPage.total || 0) + (batchesPage.total || 0),
    };
    content.setAttribute("aria-busy", "false");
    renderAll(content);
    scheduleLivePoll();
  } catch (error) {
    if (isAbortError(error)) return;
    content.setAttribute("aria-busy", "false");
    content.innerHTML = `
      <div class="state-panel state-failed">
        ${icon("wifi-off")}
        <p><strong>Could not load report history.</strong><br>${escapeHtml(error.message)}. Previous values were not replaced with zeros.</p>
        <button type="button" class="button button--primary" data-retry>${icon("refresh")}Retry</button>
      </div>`;
  }
}

function hasLiveRow() {
  const runs = lastData?.runsPage.rows || [];
  const batches = lastData?.batchesPage.rows || [];
  return runs.some((run) => !isTerminal(run.status)) || batches.some((batch) => !isTerminal(batch.status));
}

function scheduleLivePoll() {
  stopLivePoll();
  if (!hasLiveRow()) return;
  liveTimer = window.setInterval(async () => {
    try {
      const [runsPage, batchesPage] = await Promise.all([fetchRuns(), fetchBatches()]);
      if (!lastData) return;
      lastData.runsPage = runsPage;
      lastData.batchesPage = batchesPage;
      lastData.rows = buildMergedRows(runsPage, batchesPage, lastData.clientsById);
      lastData.total = (runsPage.total || 0) + (batchesPage.total || 0);
      renderTable();
      if (!hasLiveRow()) stopLivePoll();
    } catch (error) {
      if (!isAbortError(error)) stopLivePoll();
    }
  }, LIVE_POLL_MS);
}

function stopLivePoll() {
  if (liveTimer) { window.clearInterval(liveTimer); liveTimer = null; }
}

// --- Rendering -----------------------------------------------------------

function filtersActive() {
  return Boolean(current.client || current.kind || current.source || current.status || current.scope !== "all" || current.q);
}

function renderAll(content) {
  const clientOptions = [...lastData.clients]
    .sort((a, b) => a.name.localeCompare(b.name))
    .map((client) => `<option value="${escapeHtml(client.client_id)}" ${client.client_id === current.client ? "selected" : ""}>${escapeHtml(client.name)}</option>`)
    .join("");
  const kindOptions = Object.entries(REPORT_KIND_META)
    .map(([key, meta]) => `<option value="${key}" ${key === current.kind ? "selected" : ""}>${escapeHtml(meta.label)}</option>`)
    .join("");
  const sourceOptions = Object.entries(REPORT_SOURCE_META)
    .map(([key, meta]) => `<option value="${key}" ${key === current.source ? "selected" : ""}>${escapeHtml(meta.label)}</option>`)
    .join("");
  const statusOptions = Object.entries(REPORT_RUN_STATUS_META)
    .map(([key, meta]) => `<option value="${key}" ${key === current.status ? "selected" : ""}>${escapeHtml(meta.label)}</option>`)
    .join("");
  const scopeOptions = [["single", "Single runs"], ["batches", "Batches"]]
    .map(([key, label]) => `<option value="${key}" ${key === current.scope ? "selected" : ""}>${escapeHtml(label)}</option>`)
    .join("");

  content.innerHTML = `
    <div class="reports-filters" role="group" aria-label="Report filters">
      <label class="search-field reports-filters__search">
        <span class="visually-hidden">Search clients by name or domain</span>${icon("search")}
        <input type="search" id="rp-f-search" placeholder="Search clients…" value="${escapeHtml(current.q)}">
      </label>
      <label>Client<select id="rp-f-client"><option value="">All clients</option>${clientOptions}</select></label>
      <label>Report<select id="rp-f-kind"><option value="">All types</option>${kindOptions}</select></label>
      <label>Source<select id="rp-f-source"><option value="">All sources</option>${sourceOptions}</select></label>
      <label>Status<select id="rp-f-status"><option value="">All statuses</option>${statusOptions}</select></label>
      <label>Scope<select id="rp-f-scope"><option value="all" ${current.scope === "all" ? "selected" : ""}>All</option>${scopeOptions}</select></label>
      ${filtersActive() ? `<button type="button" class="button button--quiet reports-filters__clear" data-clear-filters>${icon("filter-x", "icon icon--tiny")}Clear filters</button>` : ""}
      <p class="result-count" aria-live="polite">${resultCountLabel()}</p>
    </div>
    <div id="rp-table"></div>`;
  renderTable();
}

// "The truth about the filter" (task-47 §3.5 point 5): once anything narrows
// the view, the count says how much was hidden, not just what remains.
function resultCountLabel() {
  const shown = lastData.rows.length;
  const total = lastData.total;
  if (!filtersActive() || shown === total) return `${total} run${total === 1 ? "" : "s"}`;
  return `${shown} of ${total} run${total === 1 ? "" : "s"}`;
}

function renderTable() {
  const host = root.querySelector("#rp-table");
  if (!host || !lastData) return;
  const rows = lastData.rows;
  const countEl = root.querySelector(".result-count");
  if (countEl) countEl.textContent = resultCountLabel();

  if (!lastData.clients.length && !rows.length) {
    host.innerHTML = `
      <div class="state-panel">
        ${icon("file-report")}
        <p><strong>No clients in the portfolio yet.</strong><br>Add a client via the dashboard first.</p>
      </div>`;
    return;
  }
  if (!rows.length) {
    host.innerHTML = filtersActive()
      ? `
        <div class="state-panel">
          ${icon("filter-off")}
          <p><strong>No reports match these filters.</strong><br>Try a different client, type, source, status, or scope.</p>
          <button type="button" class="button button--quiet" data-clear-filters>${icon("filter-x")}Clear filters</button>
        </div>`
      : `
        <div class="state-panel">
          ${icon("file-report")}
          <p><strong>No reports generated yet.</strong><br>Run your first client report — three clicks, no terminal.</p>
          <button type="button" class="button button--primary" id="reports-generate-empty">${icon("plus")}Generate report</button>
        </div>`;
    host.querySelector("#reports-generate-empty")?.addEventListener("click", (event) => openWizard(event.currentTarget));
    return;
  }

  host.innerHTML = `
    <div class="table-scroll">
      <table class="analytics-table reports-table">
        <thead><tr>
          <th>Client</th><th>Report</th><th>Periods</th><th>Source</th><th>Status</th>
          <th>Cross-check</th><th>Duration</th><th>Files</th><th>Created</th>
        </tr></thead>
        <tbody>${rows.map((row) => (row.kind === "run" ? renderRow(row.run) : renderBatchRow(row.batch))).join("")}</tbody>
      </table>
    </div>
    ${renderPagination(lastData.total, PAGE_SIZE, current.offset)}`;
}

function renderPagination(total, limit, offset) {
  if (total <= limit) return "";
  const from = offset + 1;
  const to = Math.min(total, offset + limit);
  return `
    <nav class="pagination" aria-label="Report history pages">
      <button type="button" class="icon-button" data-page="prev" ${offset === 0 ? "disabled" : ""} aria-label="Previous page">${icon("chevron-left")}</button>
      <span>${from}–${to} of ${total}</span>
      <button type="button" class="icon-button" data-page="next" ${to >= total ? "disabled" : ""} aria-label="Next page">${icon("chevron-right")}</button>
    </nav>`;
}

function periodSignature(run) {
  const kindLabel = REPORT_KIND_META[run.kind]?.label || run.kind;
  return `${kindLabel} · ${run.period_count} period${run.period_count === 1 ? "" : "s"}`;
}

function renderRow(run) {
  const client = lastData.clientsById.get(run.client_id);
  const clientName = client ? client.name : run.client_id;
  const statusMeta = REPORT_RUN_STATUS_META[run.status] || REPORT_RUN_STATUS_META.queued;
  const sourceMeta = REPORT_SOURCE_META[run.source] || REPORT_SOURCE_META.api;
  const crossCheckMeta = run.cross_check_status ? REPORT_CROSS_CHECK_META[run.cross_check_status] : null;
  const isLive = !isTerminal(run.status);
  const filesCell = filesCountLabel(run);

  return `
    <tr data-run-row="${escapeHtml(run.id)}" tabindex="0">
      <td class="analytics-row__value" translate="no">${escapeHtml(clientName)}</td>
      <td>${escapeHtml(periodSignature(run))}</td>
      <td>${isLive
        ? `<span class="reports-live-progress"><progress max="${run.period_count}" value="${run.periods_done}"></progress>${run.periods_done} / ${run.period_count}</span>`
        : `${run.periods_done} / ${run.period_count}${run.periods_failed ? ` <small class="muted">(${run.periods_failed} failed)</small>` : ""}`}
      </td>
      <td><span class="reports-source-chip reports-source-chip--${run.source}">${icon(sourceMeta.icon, "icon icon--tiny")}${escapeHtml(sourceMeta.label)}</span></td>
      <td><span class="status-label ${stateClass(run.status)}">${statusIcon(statusMeta.icon)}${escapeHtml(statusMeta.label)}</span></td>
      <td>${crossCheckMeta
        ? `<span class="status-label reports-cross-check ${stateClass(run.cross_check_status)}">${icon(crossCheckMeta.icon, "icon icon--tiny")}${escapeHtml(crossCheckMeta.label)}</span>`
        : `<span class="muted">—</span>`}
      </td>
      <td>${escapeHtml(formatDuration(run.started_at, run.finished_at))}</td>
      <td class="reports-files-count">${filesCell}</td>
      <td>${escapeHtml(formatTimestamp(run.queued_at))}</td>
    </tr>`;
}

function filesCountLabel(run) {
  if (run.status === "queued" || (run.status === "running" && run.source === "api")) return "—";
  const pdfCount = run.source === "pdf" ? run.periods_done : 0;
  const parts = [];
  if (pdfCount) parts.push(`<strong>${pdfCount}</strong> PDF`);
  if (isTerminal(run.status) && (run.status === "succeeded" || run.status === "partial")) parts.push(`<strong>1</strong> XLSX`);
  return parts.length ? parts.join(" · ") : "—";
}

// --- Batch card (task-47 §3.4) ----------------------------------------------
//
// A full-width row rather than another table column: a batch has no single
// client to put in the "Client" cell, and the aggregate is genuinely
// different information (a status BREAKDOWN across many clients, not one
// status) — visually distinct on purpose, never mistaken for a run.

const BATCH_STATUS_ORDER = ["succeeded", "partial", "running", "queued", "failed", "cancelled"];

function batchStatusSummary(batch) {
  const counts = {};
  batch.clients.forEach((client) => { counts[client.status] = (counts[client.status] || 0) + 1; });
  const parts = BATCH_STATUS_ORDER
    .filter((status) => counts[status])
    .map((status) => `${counts[status]} ${REPORT_RUN_STATUS_META[status]?.label.toLowerCase() || status}`);
  return parts.join(" · ") || "—";
}

function renderBatchRow(batch) {
  const kindMeta = REPORT_KIND_META[batch.kind];
  const sourceMeta = REPORT_SOURCE_META[batch.source] || REPORT_SOURCE_META.api;
  const statusMeta = REPORT_RUN_STATUS_META[batch.status] || REPORT_RUN_STATUS_META.queued;
  const hasClientPdfs = batch.clients_done > 0;
  return `
    <tr class="reports-batch-row" data-batch-row="${escapeHtml(batch.id)}" tabindex="0">
      <td colspan="9">
        <div class="reports-batch-card">
          <span class="reports-batch-card__badge">${icon("stack-2", "icon icon--tiny")}Batch</span>
          <strong translate="no">${escapeHtml(kindMeta?.label || batch.kind)}</strong>
          <span class="reports-source-chip reports-source-chip--${batch.source}">${icon(sourceMeta.icon, "icon icon--tiny")}${escapeHtml(sourceMeta.label)}</span>
          <span class="reports-batch-card__count">${batch.client_count} client${batch.client_count === 1 ? "" : "s"}</span>
          <span class="status-label ${stateClass(batch.status)}">${statusIcon(statusMeta.icon)}${escapeHtml(statusMeta.label)}</span>
          <span class="reports-batch-card__summary">${escapeHtml(batchStatusSummary(batch))}</span>
          <span class="reports-batch-card__duration">${escapeHtml(formatDuration(batch.started_at, batch.finished_at))}</span>
          <span class="reports-batch-card__created">${escapeHtml(formatTimestamp(batch.queued_at))}</span>
          <span class="reports-batch-card__actions">
            <a class="button button--quiet" href="${buildReportsBatchHash(batch.id)}">${icon("arrow-right", "icon icon--tiny")}Open batch</a>
            ${hasClientPdfs ? `<a class="button button--quiet" href="${reportBatchPdfArchiveUrl(batch.id)}">${icon("download", "icon icon--tiny")}Download all PDFs</a>` : ""}
          </span>
        </div>
      </td>
    </tr>`;
}
