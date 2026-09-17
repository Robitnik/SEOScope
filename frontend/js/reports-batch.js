// Reports — batch detail (task-47 §2.4/§3.4). A parent row over N ordinary
// report runs (task-47 §2.2): this page is the progress MATRIX (client ×
// status/periods) plus navigation into each client's own already-existing
// run detail page — it never duplicates that page's periods/table/log
// rendering, only summarizes enough to answer "who is done, who failed, who
// is still running" at a glance while the rest keeps going.

import {
  cancelReportBatch, createClientPdf, getBatchPortfolioTable, getReportBatch, getReportTypes,
  isAbortError, isFixtureMode, reportBatchPdfArchiveUrl, reportBatchPortfolioPdfUrl,
  reportBatchPortfolioXlsxUrl, reportDocumentUrl, reportFileUrl, retryReportBatchClient,
} from "./api.js?v=20260914-t93";
import { openReportsWizard } from "./reports-wizard.js?v=20260917-nsrc";
// Під тегом самого `session.js`, не під тегом цього файлу: модуль
// кешується per-URL, і другий тег дав би ДРУГИЙ інстанс із порожнім
// `cached` — тобто `isAdmin()` завжди false, ще до першого запиту.
import { isAdmin } from "./session.js?v=20260906-t72";
import { authorPlaceholder, loadAuthors, patchAuthorCells } from "./authorship.js?v=20260914-t93";
import { buildReportsHash, buildReportsRunHash } from "./router.js?v=20260821-spin3";
import {
  REPORT_KIND_META, REPORT_RUN_STATUS_META, REPORT_SOURCE_META,
  escapeHtml, formatDateRange, formatDuration, formatDurationSeconds, formatMetricValue,
  formatTimestamp, icon, isTerminal, stateClass, statusIcon,
} from "./formatters.js?v=20260907-t84";
import { sectionSkeleton } from "./skeleton.js?v=20260821-spin3";

const root = document.querySelector("#reports-batch-page-root");
const fileDialog = document.querySelector("#reports-file-dialog");
const fileDialogTitle = document.querySelector("#reports-file-title");
const fileDialogSubtitle = document.querySelector("#reports-file-subtitle");
const fileDialogBody = document.querySelector("#reports-file-body");
const POLL_MS = 2500;
const FIXTURE_POLL_MS = 700;

let current = null; // { batchId }
let requestToken = 0;
let batch = null;
let pollTimer = null;
let pendingClientPdf = new Set();
// Portfolio table preview state (task-50): fetched once, on demand, then
// kept around so switching the client `<select>` never re-fetches.
let portfolioTable = null; // BatchPortfolioTableDTO | null
let portfolioTableOpen = false;
let portfolioTableClientId = null;

function toast(message) {
  const region = document.querySelector("#toast-region");
  const element = document.createElement("div");
  element.className = "toast-message";
  element.textContent = message;
  region.append(element);
  setTimeout(() => element.remove(), 4200);
}

export async function initReportsBatchPage(batchId) {
  current = { batchId };
  batch = null;
  stopPoll();
  root.innerHTML = shell();
  bindDelegatedEvents();
  await load();
}

function shell() {
  return `
    <a class="back-link" href="${buildReportsHash()}">${icon("arrow-left")}Back to reports</a>
    <div id="rb-header"></div>
    <div id="rb-body" aria-busy="true">${sectionSkeleton({ label: "batch progress", rows: 8, columns: 4 })}</div>`;
}

function bindDelegatedEvents() {
  root.addEventListener("click", onRootClick);
  root.addEventListener("change", onRootChange);
}

async function load() {
  const token = ++requestToken;
  try {
    batch = await getReportBatch(current.batchId);
    if (token !== requestToken) return;
    render();
    schedulePoll();
  } catch (error) {
    if (isAbortError(error)) return;
    renderNotFound(error);
  }
}

function renderNotFound(error) {
  root.querySelector("#rb-header").innerHTML = "";
  root.querySelector("#rb-body").outerHTML = `
    <div class="state-panel state-failed" id="rb-body">
      ${icon("wifi-off")}
      <p><strong>Could not load this batch.</strong><br>${escapeHtml(error.message)}</p>
      <a class="button button--primary" href="${buildReportsHash()}">${icon("arrow-left")}Back to reports</a>
    </div>`;
}

function schedulePoll() {
  stopPoll();
  if (isTerminal(batch.status)) return;
  const interval = isFixtureMode() ? FIXTURE_POLL_MS : POLL_MS;
  pollTimer = window.setInterval(async () => {
    try {
      const fresh = await getReportBatch(current.batchId);
      if (!current) return;
      batch = fresh;
      render();
      if (isTerminal(batch.status)) stopPoll();
    } catch (error) {
      if (!isAbortError(error)) stopPoll();
    }
  }, interval);
}

function stopPoll() {
  if (pollTimer) { window.clearInterval(pollTimer); pollTimer = null; }
}

// --- Rendering -------------------------------------------------------------

function render() {
  renderHeader();
  renderBody();
}

function statusCounts() {
  const counts = {};
  batch.clients.forEach((client) => { counts[client.status] = (counts[client.status] || 0) + 1; });
  return counts;
}

const STATUS_ORDER = ["succeeded", "partial", "running", "queued", "failed", "cancelled"];

function statusSummaryLine() {
  const counts = statusCounts();
  return STATUS_ORDER
    .filter((status) => counts[status])
    .map((status) => `${counts[status]} ${REPORT_RUN_STATUS_META[status]?.label.toLowerCase() || status}`)
    .join(" · ") || "—";
}

// A total `failed` OR a `partial` (some periods failed, not all — e.g. one
// flaky PDF download) can both be retried server-side
// (`_RETRYABLE_RUN_STATUSES` in app/reports/batch.py includes `partial`) —
// except a missing Reporting Ninja binding, which a retry cannot fix
// (`Connect Reporting Ninja` replaces it instead, task-49 §2.3/§2.4). One
// predicate shared by the per-row button, the header's "has anything to
// retry" check, and the bulk "Rebuild failed clients" action, so the three
// never drift out of sync the way the per-row-only fix once did.
function isRetryable(client) {
  const nameMissing = client.status === "failed" && client.error_code === "report_name_missing";
  return (client.status === "failed" || client.status === "partial") && !nameMissing;
}

function renderHeader() {
  const host = root.querySelector("#rb-header");
  const kindMeta = REPORT_KIND_META[batch.kind];
  const sourceMeta = REPORT_SOURCE_META[batch.source] || REPORT_SOURCE_META.api;
  const statusMeta = REPORT_RUN_STATUS_META[batch.status] || REPORT_RUN_STATUS_META.queued;
  // Reading a batch is open to every signed-in role; running/cancelling
  // one stays admin-only server-side (`DEFAULT_ADMIN_WRITE_ONLY_PATHS`), so
  // the buttons that POST are drawn only where they would actually work.
  const canManage = isAdmin();
  const canCancel = canManage && !isTerminal(batch.status);
  const hasClientPdfs = batch.clients_done > 0;
  // task-49 §2.5: rebuild is a terminal-batch, PDF-only action — force_refresh
  // has no effect on the API path (runner.py's `_run_api` never reads it),
  // and rebuilding a client while a sibling is still running isn't a choice
  // that means anything yet.
  const canRebuild = canManage && isTerminal(batch.status) && batch.source === "pdf";
  const hasFailed = batch.clients.some(isRetryable);
  host.innerHTML = `
    <header class="reports-run-header reports-batch-header">
      <div class="reports-run-header__identity">
        <p class="eyebrow">Reports · Batch</p>
        <h1>${escapeHtml(kindMeta?.label || batch.kind)} report — ${batch.client_count} client${batch.client_count === 1 ? "" : "s"}</h1>
        <div class="reports-run-header__meta">
          <span class="reports-source-chip reports-source-chip--${batch.source}">${icon(sourceMeta.icon, "icon icon--tiny")}${escapeHtml(sourceMeta.label)}</span>
          <span class="status-label ${stateClass(batch.status)}">${statusIcon(statusMeta.icon)}${escapeHtml(statusMeta.label)}</span>
          <span>${escapeHtml(statusSummaryLine())}</span>
          <span>Started <strong>${escapeHtml(batch.started_at ? formatTimestamp(batch.started_at) : "not yet")}</strong></span>
          <span>Duration <strong>${escapeHtml(formatDuration(batch.started_at, batch.finished_at))}</strong></span>
          ${canManage ? `<span>Started by ${authorPlaceholder(batch.id)}</span>` : ""}
        </div>
      </div>
      <div class="reports-run-header__actions">
        ${canManage ? `
          <a class="button button--quiet" href="#/admin/audit?resource_type=report_batch&resource_id=${encodeURIComponent(batch.id)}">
            ${icon("history", "icon icon--tiny")}Activity
          </a>` : ""}
        ${hasClientPdfs ? `<a class="button button--quiet" href="${reportBatchPdfArchiveUrl(batch.id)}">${icon("download", "icon icon--tiny")}Download all PDFs</a>` : ""}
        ${hasClientPdfs ? `<button type="button" class="button button--quiet" id="rb-preview-pdf">${icon("eye", "icon icon--tiny")}Preview PDF</button>` : ""}
        ${hasClientPdfs ? `<a class="button button--quiet" href="${reportBatchPortfolioPdfUrl(batch.id)}">${icon("file-text", "icon icon--tiny")}Portfolio PDF</a>` : ""}
        ${hasClientPdfs ? `<button type="button" class="button button--quiet" id="rb-preview-table" aria-expanded="${portfolioTableOpen}">${icon("table", "icon icon--tiny")}${portfolioTableOpen ? "Hide table" : "Preview table"}</button>` : ""}
        ${hasClientPdfs ? `<a class="button button--quiet" href="${reportBatchPortfolioXlsxUrl(batch.id)}">${icon("download", "icon icon--tiny")}Portfolio XLSX</a>` : ""}
        ${canRebuild && hasFailed ? `<button type="button" class="button button--quiet" id="rb-rebuild-failed">${icon("refresh", "icon icon--tiny")}Rebuild failed/partial clients</button>` : ""}
        ${canRebuild ? `<button type="button" class="button button--quiet" id="rb-rebuild-all">${icon("refresh", "icon icon--tiny")}Rebuild all</button>` : ""}
        ${canCancel ? `
          <button type="button" class="button button--quiet" id="rb-cancel" ${batch.cancel_requested ? "disabled" : ""}>
            ${icon("player-stop", "icon icon--tiny")}${batch.cancel_requested ? "Stopping…" : "Cancel batch"}
          </button>` : ""}
      </div>
    </header>`;

  if (canManage) {
    loadAuthors("report_batch", [batch.id]).then((authors) => patchAuthorCells(host, authors));
  }
}

function renderBody() {
  const bodyEl = root.querySelector("#rb-body");
  bodyEl.setAttribute("aria-busy", "false");
  const rows = [...batch.clients].sort((a, b) => (a.client_name || a.client_id).localeCompare(b.client_name || b.client_id));
  bodyEl.innerHTML = `
    <section class="reports-run-section">
      <div class="section-heading">
        <div><p class="eyebrow">Progress</p><h2>Clients</h2></div>
        <div class="reports-progress-summary">
          <span class="reports-progress-summary__count">${batch.clients_done} of ${batch.client_count} done</span>
          <progress value="${batch.clients_done}" max="${Math.max(batch.client_count, 1)}" aria-label="${batch.clients_done} of ${batch.client_count} clients done"></progress>
        </div>
      </div>
      <ul class="reports-batch-matrix">
        ${rows.map(renderClientRow).join("")}
      </ul>
    </section>
    ${renderPortfolioTableSection()}`;
}

// --- Portfolio table preview (task-50) --------------------------------------
//
// The XLSX has no browser-native inline viewer the way the PDF gets via
// `?disposition=inline` — this is the renderable substitute: the same
// `ReportTableDTO` shape `reports-run.js`'s own result table already
// renders for one client, here with a client `<select>` swapping which
// one is shown (mirrors the agency's own hand-kept workbook's `Overview`
// dropdown + `DATA` sheet — see task-50's plan — without the live-formula
// machinery a static preview has no use for).

function renderPortfolioTableSection() {
  if (!portfolioTableOpen) return "";
  if (portfolioTable === null) {
    return `
    <section class="reports-run-section" id="rb-portfolio-table-section">
      <div class="section-heading"><div><p class="eyebrow">Portfolio</p><h2>Table preview</h2></div></div>
      ${sectionSkeleton({ label: "portfolio table", rows: 8, columns: 6 })}
    </section>`;
  }
  const clients = portfolioTable.clients || [];
  if (!clients.length) {
    return `
    <section class="reports-run-section" id="rb-portfolio-table-section">
      <div class="section-heading"><div><p class="eyebrow">Portfolio</p><h2>Table preview</h2></div></div>
      <div class="state-panel"><p>No client in this batch has a finished result yet.</p></div>
    </section>`;
  }
  const activeId = clients.some((c) => c.client_id === portfolioTableClientId)
    ? portfolioTableClientId
    : clients[0].client_id;
  const active = clients.find((c) => c.client_id === activeId);
  return `
    <section class="reports-run-section" id="rb-portfolio-table-section">
      <div class="section-heading">
        <div><p class="eyebrow">Portfolio</p><h2>Table preview</h2></div>
        <select id="rb-portfolio-table-client" aria-label="Client">
          ${clients.map((c) => `<option value="${escapeHtml(c.client_id)}" ${c.client_id === activeId ? "selected" : ""} translate="no">${escapeHtml(c.client_name)}</option>`).join("")}
        </select>
      </div>
      ${renderPortfolioClientTable(active.table)}
    </section>`;
}

function renderPortfolioClientTable(table) {
  const periods = table.periods || [];
  const changeLabels = table.change_labels || [];
  return `
    <div class="table-scroll">
      <table class="analytics-table reports-result-table">
        <thead><tr>
          <th>Metric</th>
          ${periods.map((p) => `
            <th>
              <span class="reports-period-head__label" translate="no">${escapeHtml(p.label)}</span>
              <small class="reports-period-head__range">${escapeHtml(formatDateRange(p.date_from, p.date_to))}</small>
            </th>`).join("")}
          ${changeLabels.map((label) => `<th class="muted">${escapeHtml(label)}</th>`).join("")}
        </tr></thead>
        <tbody>${(table.rows || []).map(renderPortfolioTableRow).join("")}</tbody>
      </table>
    </div>`;
}

function renderPortfolioTableRow(row) {
  const valueCells = row.values.map((value) => `<td class="${value === null ? "muted" : ""}">${formatMetricValue(value)}</td>`).join("");
  const changeCells = (row.changes || []).map((change) => {
    if (change === null || change === undefined) return `<td class="muted">—</td>`;
    const pct = (change * 100).toFixed(1);
    const cls = change > 0 ? "reports-change--good" : change < 0 ? "reports-change--bad" : "";
    return `<td class="${cls}">${change > 0 ? "+" : ""}${pct}%</td>`;
  }).join("");
  return `<tr><td class="analytics-row__value">${escapeHtml(row.label)}</td>${valueCells}${changeCells}</tr>`;
}

// task-49 §2.6: the matrix's own composition line — `cached_count` comes
// straight off the run's `periods_json` (no extra request). A `From cache`
// chip when EVERY period was reused is the honest label for what the live
// incident's two "succeeded 6/6" clients actually did (nothing — the whole
// report came from yesterday's PDFs).
function cacheNote(client) {
  if (!client.cached_count || !client.period_count) return "";
  if (client.cached_count >= client.period_count) {
    return `<span class="status-label state-cached reports-batch-matrix-row__cache">${icon("database", "icon icon--tiny")}From cache</span>`;
  }
  const downloaded = client.period_count - client.cached_count;
  return `<span class="reports-batch-matrix-row__cache-note">${downloaded} downloaded · ${client.cached_count} cached</span>`;
}

function renderClientRow(client) {
  const statusMeta = REPORT_RUN_STATUS_META[client.status] || REPORT_RUN_STATUS_META.queued;
  const isLive = !isTerminal(client.status);
  const succeeded = client.status === "succeeded" || client.status === "partial";
  const pdfPending = pendingClientPdf.has(client.run_id);
  const nameMissing = client.status === "failed" && client.error_code === "report_name_missing";
  const retryable = isAdmin() && isRetryable(client);
  return `
    <li class="reports-batch-matrix-row" data-state="${escapeHtml(client.status)}">
      <div class="reports-batch-matrix-row__label">
        <a href="${buildReportsRunHash(client.run_id)}" translate="no">${escapeHtml(client.client_name || client.client_id)}</a>
        <small>${client.periods_done} / ${client.period_count} periods${client.periods_failed ? ` · ${client.periods_failed} failed` : ""}</small>
      </div>
      <div class="reports-batch-matrix-row__status">
        <span class="status-label ${stateClass(client.status)}">${statusIcon(statusMeta.icon)}${escapeHtml(statusMeta.label)}</span>
        ${cacheNote(client)}
      </div>
      <span class="reports-batch-matrix-row__duration">${isLive ? "" : escapeHtml(formatDuration(client.started_at, client.finished_at))}</span>
      <span class="reports-batch-matrix-row__actions">
        ${succeeded ? `
          <a class="button button--quiet" href="${reportDocumentUrl(client.run_id)}" target="_blank" rel="noopener">${icon("file-text", "icon icon--tiny")}Open document</a>
          <button type="button" class="button button--quiet" data-client-pdf="${escapeHtml(client.run_id)}" ${pdfPending ? "disabled" : ""}>
            ${pdfPending ? statusIcon("loader-2") : icon("download", "icon icon--tiny")}Client PDF
          </button>` : ""}
        ${nameMissing ? `
          <button type="button" class="button button--quiet" data-connect-client="${escapeHtml(client.client_id)}">${icon("plug-connected", "icon icon--tiny")}Connect Reporting Ninja</button>` : ""}
        ${retryable && batch.source === "pdf" ? `
          <button type="button" class="button button--quiet" data-retry-client="${escapeHtml(client.client_id)}" data-force-refresh="false">${icon("refresh", "icon icon--tiny")}Retry (use cached PDFs)</button>
          <button type="button" class="button button--quiet" data-retry-client="${escapeHtml(client.client_id)}" data-force-refresh="true">${icon("refresh", "icon icon--tiny")}Rebuild (re-download)</button>` : ""}
        ${retryable && batch.source !== "pdf" ? `
          <button type="button" class="button button--quiet" data-retry-client="${escapeHtml(client.client_id)}" data-force-refresh="false">${icon("refresh", "icon icon--tiny")}Retry this client</button>` : ""}
      </span>
      ${client.status === "failed" && client.error_message ? `
        <p class="reports-batch-matrix-row__error">${icon("circle-x", "icon icon--tiny")}${escapeHtml(client.error_message)}</p>` : ""}
    </li>`;
}

// --- Actions -----------------------------------------------------------

async function onCancel(button) {
  button.disabled = true;
  try {
    batch = await cancelReportBatch(current.batchId);
    render();
    toast("Cancelling — clients already finished were kept.");
  } catch (error) {
    if (isAbortError(error)) return;
    toast(`Could not cancel: ${error.message}`);
    button.disabled = false;
  }
}

// task-49 §2.4: stays inside the batch (the server-side `rebuild_run`
// preserves `batch_id`) and re-renders the SAME matrix in place — no
// navigation away, unlike the old behaviour this replaces. `forceRefresh` is
// always explicit: the button that was clicked names exactly which one.
async function onRetryClient(clientId, forceRefresh, button) {
  button.disabled = true;
  try {
    batch = await retryReportBatchClient(current.batchId, clientId, { forceRefresh });
    render();
    toast(forceRefresh ? "Rebuilding this client — re-downloading everything." : "Retrying this client.");
  } catch (error) {
    if (isAbortError(error)) return;
    toast(`Could not retry: ${error.message}`);
    button.disabled = false;
  }
}

function onConnectClient(clientId, button) {
  const client = batch.clients.find((c) => c.client_id === clientId);
  openReportsWizard({
    clients: [{ client_id: clientId, name: client?.client_name || clientId, providers: [] }],
    trigger: button,
    focusClientId: clientId,
    focusKind: batch.kind,
    onCreated: (runId) => { location.hash = buildReportsRunHash(runId); },
    onBatchCreated: () => {},
  });
}

// task-49 §2.5: the estimate has to be in front of the operator BEFORE they
// confirm a batch-wide rebuild (~70 min for 12 PDF clients, live-measured) —
// same per-column estimate the wizard's own Source step already shows, since
// the PDF path shares one browser sequentially regardless of client count.
async function estimateRebuildSeconds(clientCount) {
  try {
    const types = await getReportTypes();
    const perColumn = types.types.find((t) => t.kind === batch.kind)?.estimate.pdf_seconds_per_column;
    const periodCount = batch.clients[0]?.period_count || 0;
    if (perColumn === undefined || !periodCount) return null;
    return perColumn * periodCount * clientCount;
  } catch {
    return null;
  }
}

async function onRebuildSelection(clientIds, button) {
  if (!clientIds.length) return;
  const seconds = await estimateRebuildSeconds(clientIds.length);
  const label = clientIds.length === 1 ? "1 client" : `${clientIds.length} clients`;
  const estimateText = seconds === null ? "an unknown amount of time" : `~${formatDurationSeconds(seconds)}`;
  const proceed = window.confirm(
    `Rebuild ${label}? This re-downloads every period, ignoring the PDF cache — about ${estimateText}.`,
  );
  if (!proceed) return;
  button.disabled = true;
  try {
    for (const clientId of clientIds) {
      // One browser, shared across the whole batch (task-47 §3.1) — these
      // rebuilds are inherently sequential, not a fan-out worth parallelizing.
      batch = await retryReportBatchClient(current.batchId, clientId, { forceRefresh: true }); // eslint-disable-line no-await-in-loop
    }
    render();
    toast(`Rebuilding ${label}.`);
  } catch (error) {
    if (!isAbortError(error)) toast(`Could not rebuild: ${error.message}`);
  } finally {
    button.disabled = false;
  }
}

async function onClientPdf(runId, button) {
  pendingClientPdf.add(runId);
  button.disabled = true;
  button.innerHTML = `${statusIcon("loader-2")}Client PDF`;
  try {
    const file = await createClientPdf(runId);
    window.open(reportFileUrl(file.id, { inline: true }), "_blank", "noopener");
  } catch (error) {
    if (!isAbortError(error)) toast(`Could not generate the client PDF: ${error.message}`);
  } finally {
    pendingClientPdf.delete(runId);
    button.disabled = false;
    button.innerHTML = `${icon("download", "icon icon--tiny")}Client PDF`;
  }
}

function onRootClick(event) {
  const cancelButton = event.target.closest("#rb-cancel");
  if (cancelButton) { void onCancel(cancelButton); return; }
  const retryButton = event.target.closest("[data-retry-client]");
  if (retryButton) {
    void onRetryClient(retryButton.dataset.retryClient, retryButton.dataset.forceRefresh === "true", retryButton);
    return;
  }
  const connectButton = event.target.closest("[data-connect-client]");
  if (connectButton) { onConnectClient(connectButton.dataset.connectClient, connectButton); return; }
  const pdfButton = event.target.closest("[data-client-pdf]");
  if (pdfButton) { void onClientPdf(pdfButton.dataset.clientPdf, pdfButton); return; }
  const rebuildFailed = event.target.closest("#rb-rebuild-failed");
  if (rebuildFailed) {
    void onRebuildSelection(batch.clients.filter(isRetryable).map((c) => c.client_id), rebuildFailed);
    return;
  }
  const rebuildAll = event.target.closest("#rb-rebuild-all");
  if (rebuildAll) { void onRebuildSelection(batch.clients.map((c) => c.client_id), rebuildAll); return; }
  const previewPdf = event.target.closest("#rb-preview-pdf");
  if (previewPdf) { openPortfolioPdfPreview(); return; }
  const previewTable = event.target.closest("#rb-preview-table");
  if (previewTable) { void onTogglePortfolioTable(); }
}

function onRootChange(event) {
  if (event.target.id === "rb-portfolio-table-client") {
    portfolioTableClientId = event.target.value;
    renderBody();
  }
}

function openPortfolioPdfPreview() {
  fileDialogTitle.textContent = "Portfolio PDF";
  fileDialogSubtitle.textContent = `${batch.client_count} client${batch.client_count === 1 ? "" : "s"}`;
  fileDialogBody.innerHTML = `<iframe src="${reportBatchPortfolioPdfUrl(batch.id, { inline: true })}" title="Portfolio PDF"></iframe>`;
  fileDialog.showModal();
}

fileDialog?.addEventListener("close", () => { fileDialogBody.innerHTML = ""; });

async function onTogglePortfolioTable() {
  portfolioTableOpen = !portfolioTableOpen;
  if (portfolioTableOpen && portfolioTable === null) {
    renderBody(); // shows the skeleton immediately while the fetch is in flight
    try {
      portfolioTable = await getBatchPortfolioTable(batch.id);
    } catch (error) {
      if (isAbortError(error)) return;
      toast(`Could not load the table preview: ${error.message}`);
      portfolioTableOpen = false;
      portfolioTable = null;
    }
  }
  renderBody();
}
