// Reports — run detail (task-43 §4, task-45 §3). Live progress + streaming
// logs while a run is `queued`/`running`; finished table, monthly history,
// cross-check verdict, and files once it reaches a terminal status.
//
// Progress is READ, not reconstructed (task-45 §2.1): `ReportRunDTO.periods`
// carries each period's label, exact date range, state
// (`waiting|running|done|cached|failed|skipped`), timestamps and duration.
// task-43 had to derive all of that from log message text (`startsWith
// ("Downloading")`, `endsWith(" done")`), which meant no dates, no durations,
// no `Running` at all on the API path, and a display that a reworded backend
// log line would silently break. That heuristic is gone.

import {
  cancelReportRun, createClientPdf, getDashboard, getReportRun,
  getReportRunEvents, getReportTable, isAbortError, isFixtureMode, listReportFiles,
  rebuildReportRun, reportDocumentUrl, reportFileUrl, reportRunEventsUrl, retryReportPeriod,
} from "./api.js?v=20260821-spin3";
import { buildReportsHash, buildReportsRunHash } from "./router.js?v=20260821-spin3";
import {
  REPORT_CROSS_CHECK_META, REPORT_KIND_META, REPORT_RUN_STATUS_META, REPORT_SOURCE_META,
  escapeHtml, formatDateRange, formatDuration, formatDurationSeconds, formatFileSize,
  formatMetricValue, formatTimestamp, icon, isTerminal, stateClass, statusIcon,
} from "./formatters.js?v=20260907-t84";
import { sectionSkeleton } from "./skeleton.js?v=20260821-spin3";

const root = document.querySelector("#reports-run-page-root");
const fileDialog = document.querySelector("#reports-file-dialog");
const fileDialogTitle = document.querySelector("#reports-file-title");
const fileDialogSubtitle = document.querySelector("#reports-file-subtitle");
const fileDialogBody = document.querySelector("#reports-file-body");

const REFRESH_INTERVAL_MS = 250;
const HEARTBEAT_MS = 10_000;
const FIXTURE_POLL_MS = 700;

// `cached` reads `Reused`, not `Cached`: on the PDF path it is the difference
// between 0 and ~60 seconds of browser work, and that is what the operator is
// actually reading the row for (task-45 §3.1).
const PERIOD_STATE_META = {
  waiting: { label: "Waiting", icon: "clock" },
  running: { label: "Running", icon: "loader-2" },
  cached: { label: "Reused", icon: "database" },
  done: { label: "Done", icon: "circle-check" },
  failed: { label: "Failed", icon: "circle-x" },
  skipped: { label: "Skipped", icon: "player-skip-forward" },
};
const PERIOD_STATE_CLASS = {
  waiting: "state-queued", running: "state-running", cached: "state-cached",
  done: "state-succeeded", failed: "state-failed", skipped: "state-skipped",
};
const PERIOD_DONE_STATES = new Set(["done", "cached"]);

const LOG_LEVEL_META = {
  debug: { label: "Debug", icon: "bug" },
  info: { label: "Info", icon: "info-circle" },
  warning: { label: "Warning", icon: "alert-triangle" },
  error: { label: "Error", icon: "alert-circle" },
};

const logTimeFormatter = new Intl.DateTimeFormat("en-US", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
const shortDateFormatter = new Intl.DateTimeFormat("en-US", { day: "2-digit", month: "short", year: "numeric" });
const ctrFormatter = new Intl.NumberFormat("en-US", { style: "percent", minimumFractionDigits: 2, maximumFractionDigits: 2 });

let current = null; // { runId }
let requestToken = 0;
let clientsById = new Map();
let run = null;
let table = null;
let files = null;
let logs = [];
let logLastSeq = 0;
let logPaused = false;
let logLevelFilter = "";

let eventSource = null;
let fixtureTimer = null;
let heartbeatTimer = null;
let refreshTimer = null;
let lastRefreshAt = 0;
let cellDetailRow = null;

function toast(message) {
  const region = document.querySelector("#toast-region");
  const element = document.createElement("div");
  element.className = "toast-message";
  element.textContent = message;
  region.append(element);
  setTimeout(() => element.remove(), 4200);
}

export async function initReportsRunPage(runId) {
  current = { runId };
  clientsById = new Map();
  run = null; table = null; files = null;
  logs = []; logLastSeq = 0; logPaused = false; logLevelFilter = "";
  stopLive();
  cellDetailRow = null;
  root.innerHTML = shell();
  bindDelegatedEvents();
  await loadInitial();
}

function shell() {
  return `
    <a class="back-link" href="${buildReportsHash()}">${icon("arrow-left")}Back to reports</a>
    <div id="rr-header"></div>
    <div id="rr-body" aria-busy="true">${sectionSkeleton({ label: "report run", rows: 6, columns: 4 })}</div>`;
}

function bindDelegatedEvents() {
  root.addEventListener("click", onRootClick);
  root.addEventListener("change", onRootChange);
}

// --- Data loading ------------------------------------------------------

async function loadInitial() {
  const token = ++requestToken;
  try {
    const [dashboard, runResult] = await Promise.all([
      getDashboard({ period: "week", requestKey: "reports-run-dashboard-clients" }),
      getReportRun(current.runId),
    ]);
    if (token !== requestToken) return;
    clientsById = new Map((dashboard.clients || []).map((client) => [client.client_id, client]));
    run = runResult;
    renderHeader();
    if (isTerminal(run.status)) {
      await loadTerminalData();
    } else {
      renderLiveSections();
      connectLive();
    }
  } catch (error) {
    if (isAbortError(error)) return;
    renderNotFound(error);
  }
}

function renderNotFound(error) {
  root.querySelector("#rr-header").innerHTML = "";
  root.querySelector("#rr-body").outerHTML = `
    <div class="state-panel state-failed" id="rr-body">
      ${icon("wifi-off")}
      <p><strong>Could not load this report run.</strong><br>${escapeHtml(error.message)}</p>
      <a class="button button--primary" href="${buildReportsHash()}">${icon("arrow-left")}Back to reports</a>
    </div>`;
}

// --- Live connection ---------------------------------------------------

function connectLive() {
  if (isFixtureMode()) {
    setConnectionState("live");
    fixtureTimer = window.setInterval(pollFixture, FIXTURE_POLL_MS);
    void pollFixture();
    return;
  }
  setConnectionState("connecting");
  const url = new URL(reportRunEventsUrl(current.runId), location.origin);
  eventSource = new EventSource(url);
  eventSource.onopen = () => setConnectionState("live");
  eventSource.onmessage = (event) => {
    try { appendLogEvent(JSON.parse(event.data)); } catch { /* malformed frame — narration only, never fatal */ }
    scheduleSnapshotRefresh();
  };
  eventSource.onerror = () => { setConnectionState("reconnecting"); scheduleSnapshotRefresh(); };
  heartbeatTimer = window.setInterval(scheduleSnapshotRefresh, HEARTBEAT_MS);
}

async function pollFixture() {
  try {
    const [runResult, newEvents] = await Promise.all([
      getReportRun(current.runId), getReportRunEvents(current.runId, logLastSeq),
    ]);
    if (!current) return;
    newEvents.forEach(appendLogEvent);
    run = runResult;
    renderHeader();
    renderProgressList();
    renderLogList();
    if (isTerminal(run.status)) { stopLive(); await loadTerminalData(); }
  } catch (error) { if (!isAbortError(error)) { /* transient — next tick retries */ } }
}

function scheduleSnapshotRefresh() {
  if (refreshTimer) return;
  const wait = Math.max(0, REFRESH_INTERVAL_MS - (Date.now() - lastRefreshAt));
  refreshTimer = window.setTimeout(async () => {
    refreshTimer = null;
    lastRefreshAt = Date.now();
    try {
      const runResult = await getReportRun(current.runId);
      if (!current) return;
      run = runResult;
      renderHeader();
      renderProgressList();
      if (isTerminal(run.status)) { stopLive(); await loadTerminalData(); }
    } catch (error) { if (isAbortError(error)) return; /* keep last known state, next event retries */ }
  }, wait);
}

function setConnectionState(state) {
  const el = root.querySelector("#rr-log-conn");
  const text = root.querySelector("#rr-log-conn-text");
  if (!el) return;
  el.dataset.state = state;
  text.textContent = state === "live" ? "Live" : state === "connecting" ? "Connecting…" : "Reconnecting…";
}

function stopLive() {
  eventSource?.close(); eventSource = null;
  if (fixtureTimer) { window.clearInterval(fixtureTimer); fixtureTimer = null; }
  if (heartbeatTimer) { window.clearInterval(heartbeatTimer); heartbeatTimer = null; }
  if (refreshTimer) { window.clearTimeout(refreshTimer); refreshTimer = null; }
}

function appendLogEvent(event) {
  if (logs.some((existing) => existing.seq === event.seq)) return;
  logs.push(event);
  logLastSeq = Math.max(logLastSeq, event.seq);
  // The progress list is driven by `run.periods`, refreshed by
  // `scheduleSnapshotRefresh()` — an event no longer changes it (task-45 §2.1).
  renderLogList();
}

// --- Header --------------------------------------------------------------

// task-48 §1.2/§3.3: a saved binding whose name does not start with the
// client's own name reads a DIFFERENT client's report on every PDF run
// (the live SWM case — bound to `EFFYS-Internal report`). Computed here from
// data the page already has (client name + the run's own bound name) rather
// than a new API field, since this run header is the one place an operator
// must see it before comparing 48 cells to notice.
function isForeignBinding(reportName, clientName) {
  return Boolean(
    reportName && clientName && !reportName.toUpperCase().startsWith(clientName.toUpperCase())
  );
}

function renderHeader() {
  const host = root.querySelector("#rr-header");
  const client = clientsById.get(run.client_id);
  const statusMeta = REPORT_RUN_STATUS_META[run.status] || REPORT_RUN_STATUS_META.queued;
  const kindMeta = REPORT_KIND_META[run.kind];
  const sourceMeta = REPORT_SOURCE_META[run.source] || REPORT_SOURCE_META.api;
  const canCancel = !isTerminal(run.status);
  const foreign = run.source === "pdf" && isForeignBinding(run.ninja_report_name, client?.name);
  // task-49 §2.5 wants Rebuild at the RUN level for any finished run, not
  // only a failed one — the whole point of the incident was a `succeeded`
  // run whose 6/6 periods came from yesterday's PDFs, which has no failure
  // panel to hang the action off. A failed run keeps its own Retry/Rebuild
  // pair in `renderFailurePanel()`, so this stays out of that case to avoid
  // offering the same action twice. `force_refresh` is meaningless on the
  // API path (`_run_api` never reads it), hence PDF only.
  const showsFailurePanel = run.status === "failed" || !table;
  const canRebuild = isTerminal(run.status) && run.source === "pdf" && !showsFailurePanel;
  host.innerHTML = `
    <header class="reports-run-header">
      <div class="reports-run-header__identity">
        <p class="eyebrow">Reports · <span translate="no">${escapeHtml(client ? client.name : run.client_id)}</span></p>
        <h1>${escapeHtml(kindMeta?.label || run.kind)} report</h1>
        <div class="reports-run-header__meta">
          <span class="reports-source-chip reports-source-chip--${run.source}">${icon(sourceMeta.icon, "icon icon--tiny")}${escapeHtml(sourceMeta.label)}</span>
          <span class="status-label ${stateClass(run.status)}">${statusIcon(statusMeta.icon)}${escapeHtml(statusMeta.label)}</span>
          <span>Started <strong>${escapeHtml(run.started_at ? formatTimestamp(run.started_at) : "not yet")}</strong></span>
          <span>Duration <strong>${escapeHtml(formatDuration(run.started_at, run.finished_at))}</strong></span>
          ${run.source === "pdf" && run.ninja_report_name
            ? `<span>Reporting Ninja report <strong translate="no">${escapeHtml(run.ninja_report_name)}</strong></span>`
            : ""}
          ${foreign ? `<span class="status-label state-partial reports-foreign-binding-chip">${statusIcon("alert-triangle")}Foreign binding</span>` : ""}
        </div>
      </div>
      <div class="reports-run-header__actions">
        ${canRebuild ? `
          <button type="button" class="button button--quiet" id="rr-rebuild-run">
            ${icon("refresh", "icon icon--tiny")}Rebuild (re-download)
          </button>` : ""}
        ${canCancel ? `
          <button type="button" class="button button--quiet" id="rr-cancel" ${run.cancel_requested ? "disabled" : ""}>
            ${icon("player-stop", "icon icon--tiny")}${run.cancel_requested ? "Stopping…" : "Cancel"}
          </button>` : ""}
      </div>
    </header>`;
}

// --- Live sections: progress + logs --------------------------------------

function renderLiveSections() {
  const bodyEl = root.querySelector("#rr-body");
  bodyEl.setAttribute("aria-busy", "false");
  bodyEl.innerHTML = `
    <section class="reports-run-section" aria-live="polite">
      <div class="section-heading">
        <div><p class="eyebrow">Progress</p><h2>Periods</h2></div>
        <div class="reports-progress-summary" id="rr-progress-summary"></div>
      </div>
      <ul class="reports-progress-list" id="rr-progress"></ul>
    </section>
    <section class="reports-run-section">
      <div class="section-heading">
        <div><p class="eyebrow">Live</p><h2>Logs</h2></div>
        <span class="reports-log-connection" id="rr-log-conn" data-state="connecting"><span class="reports-log-connection__dot"></span><span id="rr-log-conn-text">Connecting…</span></span>
      </div>
      <div class="reports-log-toolbar">
        <label>Level<select id="rr-log-level">
          <option value="">All levels</option>
          <option value="info">Info</option>
          <option value="warning">Warning</option>
          <option value="error">Error</option>
          <option value="debug">Debug</option>
        </select></label>
        <div class="reports-wizard-actions">
          <button type="button" class="button button--quiet" id="rr-log-pause" aria-pressed="false">${icon("player-pause", "icon icon--tiny")}Pause</button>
          <button type="button" class="button button--quiet" id="rr-log-copy">${icon("copy", "icon icon--tiny")}Copy log</button>
        </div>
      </div>
      <ul class="reports-log-list" id="rr-log-list" aria-live="polite" aria-label="Live report log"></ul>
    </section>`;
  renderProgressList();
  renderLogList();
}

function periodRows() {
  return run?.periods || [];
}

// Only a FINISHED period has a duration. A running one deliberately shows
// nothing here — the run's own elapsed time already ticks in the summary line
// and the header, and a second live clock per row would just be noise.
function periodDuration(period) {
  return period.duration_seconds === null || period.duration_seconds === undefined
    ? ""
    : formatDurationSeconds(period.duration_seconds);
}

function renderProgressSummary() {
  const host = root.querySelector("#rr-progress-summary");
  if (!host) return;
  const rows = periodRows();
  const total = rows.length || run?.period_count || 0;
  const done = rows.filter((row) => PERIOD_DONE_STATES.has(row.state)).length;
  const elapsed = run?.started_at ? formatDuration(run.started_at, run.finished_at) : null;
  host.innerHTML = `
    <span class="reports-progress-summary__count">${done} of ${total} period${total === 1 ? "" : "s"}</span>
    ${elapsed ? `<span class="reports-progress-summary__elapsed">${escapeHtml(elapsed)} elapsed</span>` : ""}
    <progress value="${done}" max="${Math.max(total, 1)}" aria-label="${done} of ${total} periods collected"></progress>`;
}

function renderProgressList() {
  const host = root.querySelector("#rr-progress");
  if (!host) return;
  renderProgressSummary();
  const rows = periodRows();
  if (!rows.length) {
    // Only reachable for a run created before the `periods_json` migration —
    // an honest "no per-period detail", never invented placeholder rows.
    host.innerHTML = `<li class="reports-progress-row reports-progress-row--empty"><span class="muted">No per-period detail was recorded for this run.</span></li>`;
    return;
  }
  const canRetryPeriod = isTerminal(run.status);
  host.innerHTML = rows.map((row) => {
    const meta = PERIOD_STATE_META[row.state] || PERIOD_STATE_META.waiting;
    const range = formatDateRange(row.date_from, row.date_to);
    // task-49 §2.5: a `done`/`cached` period is retryable now too ("Rebuild")
    // — `failed` keeps its own dedicated row with the error text below
    // instead, so this stays a light icon-button, not a second alarming row.
    const canRebuild = canRetryPeriod && (row.state === "done" || row.state === "cached");
    return `
      <li class="reports-progress-row" data-state="${escapeHtml(row.state || "waiting")}">
        <div class="reports-progress-row__label">
          <strong translate="no">${escapeHtml(row.label)}</strong>
          ${range ? `<small>${escapeHtml(range)}${row.is_partial ? " · Partial period" : ""}</small>` : ""}
        </div>
        <span class="status-label ${PERIOD_STATE_CLASS[row.state] || "state-queued"}">${statusIcon(meta.icon)}${escapeHtml(periodStatusLabel(row))}</span>
        <span class="reports-progress-row__duration">${escapeHtml(periodDuration(row))}</span>
        ${canRebuild ? `
          <button type="button" class="icon-button reports-progress-row__rebuild" data-retry-period="${escapeHtml(row.label)}" aria-label="Re-download ${escapeHtml(row.label)}">
            ${icon("refresh", "icon icon--tiny")}
          </button>` : ""}
      </li>`;
  }).join("");
  const failed = rows.filter((row) => row.state === "failed" && row.error);
  if (failed.length) {
    // Auto-retry (task-47 follow-up) already used up its budget by the time
    // a period sits here `failed` — this button asks for exactly ONE more,
    // manual attempt, only once the run itself is done (retrying against a
    // still-`running` run is rejected server-side: it isn't finished yet).
    host.insertAdjacentHTML("beforeend", failed.map((row) => `
      <li class="reports-progress-error">
        ${icon("circle-x", "icon icon--tiny")}
        <span><strong translate="no">${escapeHtml(row.label)}</strong> — ${escapeHtml(row.error)}</span>
        ${canRetryPeriod ? `
          <button type="button" class="button button--quiet" data-retry-period="${escapeHtml(row.label)}">
            ${icon("refresh", "icon icon--tiny")}Retry this period
          </button>` : ""}
      </li>`).join(""));
  }
}

// "Running — retry 2/3" while an auto-retry is in flight, "Failed (3/3
// attempts)" once the budget is exhausted — a bare "Running"/"Failed" looked
// identical whether a period succeeded first try or fought through two
// retries, which is exactly the visibility task-47's own bug report asked
// for ("покажи їх").
function periodStatusLabel(row) {
  const meta = PERIOD_STATE_META[row.state] || PERIOD_STATE_META.waiting;
  const maxAttempts = row.max_attempts || 1;
  if (row.state === "running" && row.attempt > 1) {
    return `Running — retry ${row.attempt}/${maxAttempts}`;
  }
  if (row.state === "failed" && maxAttempts > 1) {
    return `Failed (${row.attempt || maxAttempts}/${maxAttempts} attempts)`;
  }
  return meta.label;
}

function formatLogTime(value) {
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? logTimeFormatter.format(new Date(parsed)) : "";
}

function visibleLogs() {
  return logLevelFilter ? logs.filter((event) => event.level === logLevelFilter) : logs;
}

function renderLogList() {
  const host = root.querySelector("#rr-log-list");
  if (!host) return;
  const rows = visibleLogs();
  host.innerHTML = rows.length
    ? rows.map(renderLogRow).join("")
    : `<li class="reports-log-row"><span></span><span></span><span class="muted">No log lines yet.</span></li>`;
  if (!logPaused) host.scrollTop = host.scrollHeight;
}

function renderLogRow(event) {
  const meta = LOG_LEVEL_META[event.level] || LOG_LEVEL_META.info;
  return `
    <li class="reports-log-row reports-log-row--${escapeHtml(event.level)}">
      <time datetime="${escapeHtml(event.timestamp)}">${escapeHtml(formatLogTime(event.timestamp))}</time>
      <span class="reports-log-row__level">${icon(meta.icon, "icon icon--tiny")}${escapeHtml(meta.label)}</span>
      <span class="reports-log-row__message">${escapeHtml(event.message)}</span>
    </li>`;
}

// --- Terminal sections: table, cross-check, files -------------------------

async function loadTerminalData() {
  const bodyEl = root.querySelector("#rr-body");
  bodyEl.setAttribute("aria-busy", "true");
  bodyEl.innerHTML = sectionSkeleton({ label: "report result", rows: 8, columns: 6 });
  try {
    const [tableResult, filesResult] = await Promise.all([
      run.status === "failed" ? Promise.resolve(null) : getReportTable(current.runId).catch(() => null),
      listReportFiles(current.runId).catch(() => []),
    ]);
    table = tableResult;
    files = filesResult;
    bodyEl.setAttribute("aria-busy", "false");
    // The header renders once before this (`loadInitial`), when `table` is
    // still null — and the run-level Rebuild action depends on knowing
    // whether the failure panel will own that action instead, which is only
    // decidable now. Cheap re-render, no flicker: same markup otherwise.
    renderHeader();
    renderTerminalSections(bodyEl);
  } catch (error) {
    if (isAbortError(error)) return;
    bodyEl.setAttribute("aria-busy", "false");
    bodyEl.innerHTML = `
      <div class="state-panel state-failed">
        ${icon("wifi-off")}
        <p><strong>Could not load the finished report.</strong><br>${escapeHtml(error.message)}</p>
        <button type="button" class="button button--primary" data-retry-load>${icon("refresh")}Retry</button>
      </div>`;
  }
}

function renderTerminalSections(bodyEl) {
  const sections = [];
  if (run.status === "failed" || !table) sections.push(renderFailurePanel());
  if (run.status === "partial") sections.push(renderPartialBanner());
  const cacheBanner = renderCacheFreshnessBanner();
  if (cacheBanner) sections.push(cacheBanner);
  if (table?.cross_check) sections.push(renderCrossCheckBanner(table.cross_check));
  if (table) sections.push(`<section class="reports-run-section"><div class="section-heading"><div><p class="eyebrow">Result</p><h2>Metrics</h2></div></div>${renderResultTable()}</section>`);
  // Document/client PDF (task-47 §2.1) — `monthly` only: the tile/YOY layout
  // is built for that column set (task-47 §1.5), so a yearly/custom/
  // comparator run has nothing meaningful to show here.
  if (table && run.kind === "monthly" && (run.status === "succeeded" || run.status === "partial")) {
    sections.push(renderDocumentSection());
  }
  if (table?.history) sections.push(renderHistorySection(table.history));
  if (periodRows().length) sections.push(renderPeriodsSection());
  sections.push(renderFilesSection());
  bodyEl.innerHTML = sections.join("");
  renderProgressList();
}

// The client document, previewed in an `<iframe>` (task-47 §2.1): the
// operator sees LITERALLY what a client PDF would render, because it is the
// same URL Chromium drives to produce that PDF — one rendered markup, two
// surfaces, so a screen and a file can never quietly disagree.
function renderDocumentSection() {
  const pdfFile = (files || []).find((f) => f.kind === "client_pdf");
  return `
    <section class="reports-run-section reports-document-section">
      <div class="section-heading">
        <div><p class="eyebrow">Deliverable</p><h2>Client document</h2></div>
        <div class="reports-document-actions">
          ${pdfFile ? `<a class="button button--quiet" href="${reportFileUrl(pdfFile.id)}">${icon("download", "icon icon--tiny")}Download PDF</a>` : ""}
          <button type="button" class="button button--primary" id="rr-client-pdf">
            ${icon("refresh", "icon icon--tiny")}${pdfFile ? "Regenerate PDF" : "Generate PDF"}
          </button>
        </div>
      </div>
      <iframe class="reports-document-frame" src="${reportDocumentUrl(current.runId)}" title="Client document preview"></iframe>
    </section>`;
}

async function onClientPdf(button) {
  button.disabled = true;
  const original = button.innerHTML;
  button.innerHTML = `${statusIcon("loader-2")}Generating…`;
  try {
    await createClientPdf(current.runId);
    files = await listReportFiles(current.runId);
    const bodyEl = root.querySelector("#rr-body");
    if (bodyEl) renderTerminalSections(bodyEl);
    toast("Client PDF ready.");
  } catch (error) {
    if (isAbortError(error)) return;
    toast(`Could not generate the client PDF: ${error.message}`);
  } finally {
    button.disabled = false;
    button.innerHTML = original;
  }
}

async function onRetryPeriod(label, button) {
  button.disabled = true;
  const original = button.innerHTML;
  button.innerHTML = `${statusIcon("loader-2")}Retrying…`;
  try {
    run = await retryReportPeriod(current.runId, label);
    renderHeader();
    await loadTerminalData();
    toast(run.status === "succeeded" || run.status === "partial"
      ? `${label} retried — report updated.`
      : `${label} failed again — see the error below.`);
  } catch (error) {
    if (isAbortError(error)) return;
    toast(`Could not retry ${label}: ${error.message}`);
    button.disabled = false;
    button.innerHTML = original;
  }
}

// Kept on the finished view too, not just live: after the fact this is where
// "which columns came from cache" and "where did the 4 minutes go" are
// answered (task-45 §2.1).
function renderPeriodsSection() {
  return `
    <section class="reports-run-section">
      <div class="section-heading">
        <div><p class="eyebrow">Collection</p><h2>Periods</h2></div>
        <div class="reports-progress-summary" id="rr-progress-summary"></div>
      </div>
      <ul class="reports-progress-list" id="rr-progress"></ul>
    </section>`;
}

// The second table (task-45 §3.3): complete months, oldest first, CTR derived
// from clicks/impressions. `Conversions` is ORGANIC — named in the column
// header rather than left to a tooltip, because that is what the numbers are.
function renderHistorySection(history) {
  const months = history.months || [];
  const notes = [];
  if (!history.has_gsc) notes.push("Google Search Console isn't connected for this client — Clicks, Impressions and CTR are unavailable.");
  if (!history.has_ga4) notes.push("GA4 monthly channel data is unavailable — Conversions can't be broken out by month.");
  return `
    <section class="reports-run-section">
      <div class="section-heading">
        <div><p class="eyebrow">History</p><h2>Historical Data</h2></div>
        <button type="button" class="button button--quiet" id="rr-history-copy">${icon("copy", "icon icon--tiny")}Copy table</button>
      </div>
      ${notes.map((note) => `<p class="reports-history-note">${icon("info-circle", "icon icon--tiny")}${escapeHtml(note)}</p>`).join("")}
      ${months.length ? `
        <div class="table-scroll">
          <table class="analytics-table reports-history-table" id="rr-history-table">
            <thead><tr>
              <th>Date</th><th>Clicks</th><th>Impressions</th><th>CTR</th>
              <th>Conversions<small>Organic Search</small></th>
            </tr></thead>
            <tbody>${months.map((month) => `
              <tr>
                <td class="analytics-row__value" translate="no">${escapeHtml(month.label)}</td>
                <td class="${month.clicks === null ? "muted" : ""}">${formatMetricValue(month.clicks)}</td>
                <td class="${month.impressions === null ? "muted" : ""}">${formatMetricValue(month.impressions)}</td>
                <td class="${month.ctr === null || month.ctr === undefined ? "muted" : ""}">${escapeHtml(formatCtr(month.ctr))}</td>
                <td class="${month.organic_conversions === null ? "muted" : ""}">${formatMetricValue(month.organic_conversions)}</td>
              </tr>`).join("")}</tbody>
          </table>
        </div>`
        : `<div class="state-panel"><p>No complete month has data yet for this client.</p></div>`}
    </section>`;
}

function formatCtr(value) {
  return value === null || value === undefined ? "—" : ctrFormatter.format(value);
}

async function copyHistoryTable() {
  const months = table?.history?.months || [];
  const header = ["Date", "Clicks", "Impressions", "CTR", "Conversions (Organic)"].join("\t");
  const lines = months.map((month) => [
    month.label,
    month.clicks ?? "",
    month.impressions ?? "",
    month.ctr === null || month.ctr === undefined ? "" : (month.ctr * 100).toFixed(2),
    month.organic_conversions ?? "",
  ].join("\t"));
  try {
    await navigator.clipboard.writeText([header, ...lines].join("\n"));
    toast("Historical data copied — paste it straight into the sheet.");
  } catch {
    toast("Could not copy — clipboard is unavailable in this browser.");
  }
}

function renderFailurePanel() {
  // task-49 §2.4/§2.5: the whole-run Retry used to silently keep
  // `force_refresh: false` no matter WHY the operator retried (task-49 §1.4)
  // — now the choice is explicit, and (PDF only — `force_refresh` has no
  // effect on the API path) offered as two plain buttons rather than a menu,
  // matching the pair already used on the batch matrix for the same reason.
  const showRebuildChoice = run.source === "pdf";
  return `
    <div class="reports-run-error state-panel state-failed">
      ${icon("circle-x")}
      <p><strong>This report failed.</strong><br>${escapeHtml(run.error_message || "Unknown error.")}</p>
      ${showRebuildChoice ? `
        <div class="reports-wizard-actions">
          <button type="button" class="button button--primary" id="rr-retry">${icon("refresh")}Retry (use cached PDFs)</button>
          <button type="button" class="button button--quiet" id="rr-rebuild">${icon("refresh")}Rebuild (re-download)</button>
        </div>`
        : `<button type="button" class="button button--primary" id="rr-retry">${icon("refresh")}Retry</button>`}
    </div>`;
}

// task-49 §2.6: computed client-side from `run.periods` (`from_cache` +
// `cached_at`) rather than a new run-level DTO field — the same "derive it
// from data the page already has" precedent `isForeignBinding()` set above.
function renderCacheFreshnessBanner() {
  if (run.source !== "pdf") return "";
  const rows = periodRows();
  const cachedRows = rows.filter((row) => row.from_cache && row.cached_at);
  if (!cachedRows.length) return "";
  const oldest = [...cachedRows].sort((a, b) => a.cached_at.localeCompare(b.cached_at))[0].cached_at;
  // Reuses the `state-cached`/`--info` tone already defined for the per-
  // period "Reused" chip (task-45 §3.1) — the same neutral, not-an-error
  // meaning applies at the whole-run level here.
  return `
    <div class="state-panel state-cached">
      ${icon("database")}
      <p>Data: <strong>${cachedRows.length} of ${rows.length}</strong> period${rows.length === 1 ? "" : "s"} reused from PDFs downloaded <strong>${escapeHtml(formatShortDate(oldest))}</strong>.</p>
    </div>`;
}

function renderPartialBanner() {
  return `
    <div class="state-panel state-partial">
      ${icon("alert-triangle")}
      <p><strong>${run.periods_failed} of ${run.period_count} period(s) failed.</strong><br>The rest completed and are shown below — check the log for exactly what went wrong.</p>
    </div>`;
}

// `cc` matches `CrossCheckResult.to_dict()` (app/reports/verify.py) exactly:
// `{status, cells: [{period_label, metric_key, metric_label, pdf_value,
// api_value, diff_pct, verdict}]}` for EVERY metric x period cell. Verdict is
// one of 6 (task-48 §2.3): `match`/`within_tolerance` (agree), `mismatch`/
// `missing_api`/`missing_pdf` (a genuine disagreement — the "differ" bucket
// below), `no_data` (neither source had anything for this period — not an
// error, task-48 §1.3). There is no separate pre-filtered `mismatches` list
// or `checked_cells` count on the wire (found live: the first version of
// this file assumed both existed, so a real mismatch run rendered "0 values
// differ").
const DIFFER_VERDICTS = new Set(["mismatch", "missing_api", "missing_pdf"]);

const MISMATCH_VERDICT_LABEL = {
  mismatch: null, // the Δ column already says everything for a numeric mismatch
  missing_api: "API missing",
  missing_pdf: "PDF missing",
  no_data: "No data",
};

function renderCrossCheckBanner(cc) {
  const cells = cc.cells || [];
  const noDataCells = cells.filter((cell) => cell.verdict === "no_data");
  const differCells = cells.filter((cell) => DIFFER_VERDICTS.has(cell.verdict));
  const toleranceCells = cells.filter((cell) => cell.verdict === "within_tolerance");
  const matchCount = cells.length - noDataCells.length - differCells.length;
  const meta = REPORT_CROSS_CHECK_META[cc.status];
  const comparedCells = cells.length - noDataCells.length;
  // task-48 §3.1: when EVERY cell that had anything to compare disagrees,
  // the far more likely explanation is not 48 independent data bugs — it is
  // that this client's Reporting Ninja report belongs to someone else (the
  // live SWM case, task-48 §1.2), so the banner names that possibility
  // directly instead of leaving the operator to re-derive it from a table.
  const likelyForeignBinding = comparedCells > 0 && differCells.length === comparedCells;
  return `
    <div class="reports-cross-check-banner ${stateClass(cc.status)}">
      ${icon(meta.icon)}
      <div>
        <strong class="reports-cross-check-banner__counts">
          <span class="reports-cross-check-count reports-cross-check-count--match">${matchCount} match${toleranceCells.length ? ` <small>(${toleranceCells.length} within tolerance)</small>` : ""}</span> ·
          <span class="reports-cross-check-count reports-cross-check-count--no-data">${noDataCells.length} no data</span> ·
          <span class="reports-cross-check-count reports-cross-check-count--differ">${differCells.length} differ</span>
        </strong>
        <p>The PDF and the API were compared independently for every metric × period — this is what a real client-facing PDF says versus the live API, not the same parser checking itself.</p>
        ${likelyForeignBinding ? `
          <p class="reports-cross-check-banner__hint">${icon("alert-triangle", "icon icon--tiny")}Every value differs — this usually means the client is bound to another client's Reporting Ninja report. Re-check the binding before trusting this run.</p>`
          : ""}
        ${differCells.length || noDataCells.length ? renderMismatchTable([...differCells, ...noDataCells]) : ""}
      </div>
    </div>`;
}

// Rows arrive pre-grouped (§3.2: `differ` before `no data`) — the caller
// already concatenated `[...differCells, ...noDataCells]`, so this just
// renders them in that order rather than re-deriving it.
function renderMismatchTable(cells) {
  return `
    <div class="table-scroll">
      <table class="analytics-table reports-mismatch-table">
        <thead><tr><th>Metric</th><th>Period</th><th>PDF</th><th>API</th><th>Δ</th></tr></thead>
        <tbody>${cells.map((cell) => {
          const isNoData = cell.verdict === "no_data";
          const pct = cell.diff_pct === null || cell.diff_pct === undefined
            ? "—" : `${(cell.pdf_value || 0) >= (cell.api_value || 0) ? "+" : "-"}${(cell.diff_pct * 100).toFixed(1)}%`;
          const verdictLabel = MISMATCH_VERDICT_LABEL[cell.verdict];
          return `
          <tr data-verdict="${escapeHtml(cell.verdict)}" class="${isNoData ? "reports-mismatch-row--no-data" : ""}">
            <td class="analytics-row__value">${escapeHtml(cell.metric_label)}${verdictLabel ? ` <small class="muted">(${escapeHtml(verdictLabel)})</small>` : ""}</td>
            <td>${escapeHtml(cell.period_label)}</td>
            <td>${formatMetricValue(cell.pdf_value)}</td>
            <td>${formatMetricValue(cell.api_value)}</td>
            <td class="${isNoData ? "" : "reports-mismatch--delta"}">${isNoData ? "—" : escapeHtml(pct)}</td>
          </tr>`;
        }).join("")}</tbody>
      </table>
    </div>`;
}

function renderResultTable() {
  const periods = table.periods;
  const changeLabels = table.change_labels || [];
  return `
    <div class="table-scroll">
      <table class="analytics-table reports-result-table" id="rr-table">
        <thead><tr>
          <th>Metric</th>
          ${periods.map((p, i) => `
            <th data-partial="${p.is_partial}" data-period-index="${i}">
              <span class="reports-period-head__label" translate="no">${escapeHtml(p.label)}</span>
              <small class="reports-period-head__range">${escapeHtml(formatDateRange(p.date_from, p.date_to))}</small>
              ${p.is_partial ? `<span class="status-label state-partial reports-period-head__partial">${icon("alert-triangle", "icon icon--tiny")}Partial</span>` : ""}
            </th>`).join("")}
          ${changeLabels.map((label) => `<th class="muted">${escapeHtml(label)}</th>`).join("")}
        </tr></thead>
        <tbody>${table.rows.map((row) => renderResultRow(row)).join("")}</tbody>
      </table>
    </div>`;
}

function renderResultRow(row) {
  // A real `<button>` fills the cell rather than a `tabindex`-only `<td>`
  // (AGENTS §7 — never a clickable div/span): only a native interactive
  // element fires a click from Enter/Space for free, and gets that behavior
  // right in every browser without hand-rolling a keydown handler.
  const valueCells = row.values.map((value, index) => `
    <td class="${value === null ? "muted" : ""}">
      <div class="reports-cell">
        <button type="button" class="reports-cell-button" data-cell data-row-label="${escapeHtml(row.label)}" data-period-index="${index}">${formatMetricValue(value)}</button>
        ${value === null ? "" : `<button type="button" class="icon-button reports-cell-copy" data-copy-value="${escapeHtml(String(value))}" aria-label="Copy ${escapeHtml(row.label)} value for ${escapeHtml(table.periods[index].label)}">${icon("copy", "icon icon--tiny")}</button>`}
      </div>
    </td>`).join("");
  const changeCells = row.changes.map((change) => {
    if (change === null || change === undefined) return `<td class="muted">—</td>`;
    const pct = (change * 100).toFixed(1);
    const cls = change > 0 ? "reports-change--good" : change < 0 ? "reports-change--bad" : "";
    return `<td class="${cls}">${change > 0 ? "+" : ""}${pct}%</td>`;
  }).join("");
  return `<tr><td class="analytics-row__value">${escapeHtml(row.label)}</td>${valueCells}${changeCells}</tr>`;
}

function formatShortDate(isoDate) {
  return shortDateFormatter.format(new Date(`${isoDate}T12:00:00`));
}

// Origin, shown as an inline expandable row right under the clicked cell's
// row — "клік по клітинці відкриває походження" (task-43 §4). A floating
// popover would need `element.style.top/left` set from JS, which the app's
// strict CSP (`default-src 'self'`, no `style-src 'unsafe-inline'`) blocks
// outright (see `sync-modal.js`'s own note on the same constraint) — an
// in-flow table row needs no positioning at all, and reads better in a dense
// table besides. The backend's `sources` map is per-PERIOD (which PDF a
// whole column came from), not per metric cell (`ReportTableDTO` docstring)
// — every cell in a column opens the same detail, honestly matching that
// granularity rather than inventing per-cell provenance the API doesn't have.
function onTableCellClick(event) {
  const copyButton = event.target.closest("[data-copy-value]");
  if (copyButton) { void copyCellValue(copyButton); return; }
  const cell = event.target.closest("[data-cell]");
  if (cell) {
    const period = table.periods[Number(cell.dataset.periodIndex)];
    toggleCellDetailRow(cell.closest("tr"), period);
    return;
  }
  if (event.target.closest("[data-close-detail]")) closeCellDetailRow();
}

async function copyCellValue(button) {
  try {
    await navigator.clipboard.writeText(button.dataset.copyValue);
    toast("Value copied.");
  } catch {
    toast("Could not copy — clipboard is unavailable in this browser.");
  }
}

function closeCellDetailRow() {
  cellDetailRow?.remove();
  cellDetailRow = null;
}

function toggleCellDetailRow(afterRow, period) {
  const reopeningSame = cellDetailRow?.dataset.forPeriod === period.label && cellDetailRow.previousElementSibling === afterRow;
  closeCellDetailRow();
  if (reopeningSame) return;

  const sourceFile = table.source === "pdf" ? table.sources?.[period.label] : null;
  const fileRow = sourceFile ? (files || []).find((f) => f.kind === "pdf" && f.period_label === period.label) : null;
  const tr = document.createElement("tr");
  tr.className = "reports-cell-detail-row";
  tr.dataset.forPeriod = period.label;
  tr.innerHTML = `
    <td colspan="${afterRow.children.length}">
      <div class="reports-cell-detail">
        <strong translate="no">${escapeHtml(period.label)}</strong>
        <span>${escapeHtml(formatShortDate(period.date_from))} – ${escapeHtml(formatShortDate(period.date_to))}${period.is_partial ? " · Partial period" : ""}</span>
        ${table.source === "pdf"
          ? (sourceFile ? `<span translate="no">${escapeHtml(sourceFile)}</span>` : `<span>Source PDF unavailable.</span>`)
          : `<span>Read from the Reporting Ninja API: GA4 totals, plus Organic Search / Direct via First user default channel grouping; GSC clicks/impressions.</span>`}
        ${fileRow ? `<button type="button" class="button button--quiet" data-open-file="${escapeHtml(fileRow.id)}" data-open-name="${escapeHtml(period.label)}">${icon("external-link", "icon icon--tiny")}Open that PDF</button>` : ""}
        <button type="button" class="icon-button" data-close-detail aria-label="Close origin details">${icon("x", "icon icon--tiny")}</button>
      </div>
    </td>`;
  afterRow.after(tr);
  cellDetailRow = tr;
}

// --- Files + diagnostics --------------------------------------------------

function renderFilesSection() {
  if (!files || !files.length) {
    // Every run that collected at least one period now writes an XLSX
    // (task-45 §2.3), so an empty Files section means the run stopped before
    // any data existed — say THAT, not the bare fact (task-45 §3.4).
    const reason = run.status === "failed"
      ? "This run failed before any period was collected, so there was nothing to write."
      : run.status === "cancelled"
        ? "This run was cancelled before any period finished, so no workbook was written."
        : "No period was collected, so no workbook was written.";
    return `
      <section class="reports-run-section">
        <div class="section-heading"><div><p class="eyebrow">Output</p><h2>Files</h2></div></div>
        <div class="state-panel"><p>${escapeHtml(reason)}</p></div>
      </section>`;
  }
  const xlsx = files.find((f) => f.kind === "xlsx");
  const pdfs = files.filter((f) => f.kind === "pdf");
  const screenshots = files.filter((f) => f.kind === "screenshot");
  return `
    <section class="reports-run-section">
      <div class="section-heading">
        <div><p class="eyebrow">Output</p><h2>Files</h2></div>
        ${xlsx ? `<a class="button button--primary" href="${reportFileUrl(xlsx.id)}">${icon("download", "icon icon--tiny")}Download XLSX</a>` : ""}
      </div>
      ${pdfs.length
        ? `<div class="reports-files-grid">${pdfs.map(renderFileRow).join("")}</div>`
        : `<div class="state-panel"><p>API-path runs read the numbers directly — the workbook above is the whole output, with the metrics, the monthly history and an import-ready sheet.</p></div>`}
    </section>
    ${screenshots.length ? renderDiagnostics(screenshots) : ""}`;
}

function renderFileRow(file) {
  return `
    <div class="reports-file-row">
      ${icon("file-text", "reports-file-row__icon icon")}
      <span class="reports-file-row__name" translate="no">${escapeHtml(file.period_label || "PDF")}</span>
      <span class="reports-file-row__meta">${escapeHtml(formatFileSize(file.size_bytes))}${file.is_partial_period ? " · Partial period" : ""}</span>
      <span class="reports-file-row__actions">
        <button type="button" class="button button--quiet" data-view-file="${escapeHtml(file.id)}" data-view-name="${escapeHtml(file.period_label || "PDF")}">${icon("eye", "icon icon--tiny")}View</button>
        <a class="button button--quiet" href="${reportFileUrl(file.id)}">${icon("download", "icon icon--tiny")}Download</a>
      </span>
    </div>`;
}

function renderDiagnostics(screenshots) {
  return `
    <details class="reports-diagnostics">
      <summary>Diagnostics — ${screenshots.length} screenshot${screenshots.length === 1 ? "" : "s"} of failed attempts</summary>
      ${screenshots.map((s) => `
        <figure>
          <img src="${reportFileUrl(s.id, { inline: true })}" alt="Failure screenshot for ${escapeHtml(s.period_label || "a failed period")}" loading="lazy">
          <figcaption class="muted">${escapeHtml(s.period_label || "")}</figcaption>
        </figure>`).join("")}
    </details>`;
}

function openFileDialog(fileId, name) {
  fileDialogTitle.textContent = name || "File";
  fileDialogSubtitle.textContent = "";
  fileDialogBody.innerHTML = `<iframe src="${reportFileUrl(fileId, { inline: true })}" title="${escapeHtml(name || "Report file")}"></iframe>`;
  fileDialog.showModal();
}

fileDialog?.addEventListener("close", () => { fileDialogBody.innerHTML = ""; });

// --- Actions ---------------------------------------------------------------

async function onCancel(button) {
  button.disabled = true;
  try {
    run = await cancelReportRun(current.runId);
    renderHeader();
    toast("Cancelling — files already downloaded will be kept.");
  } catch (error) {
    if (isAbortError(error)) return;
    toast(`Could not cancel: ${error.message}`);
    button.disabled = false;
  }
}

async function onRetry(forceRefresh, button) {
  button.disabled = true;
  try {
    const result = await rebuildReportRun(current.runId, { forceRefresh });
    location.hash = buildReportsRunHash(result.run_id);
  } catch (error) {
    if (isAbortError(error)) return;
    toast(`Could not ${forceRefresh ? "rebuild" : "retry"}: ${error.message}`);
    button.disabled = false;
  }
}

async function copyLog() {
  const text = visibleLogs().map((event) => `${event.timestamp}\t${event.level}\t${event.message}`).join("\n");
  try {
    await navigator.clipboard.writeText(text);
    toast("Log copied.");
  } catch {
    toast("Could not copy — clipboard is unavailable in this browser.");
  }
}

// --- Delegated events --------------------------------------------------

function onRootClick(event) {
  const cancelButton = event.target.closest("#rr-cancel");
  if (cancelButton) { void onCancel(cancelButton); return; }
  const retryButton = event.target.closest("#rr-retry");
  if (retryButton) { void onRetry(false, retryButton); return; }
  const rebuildButton = event.target.closest("#rr-rebuild, #rr-rebuild-run");
  if (rebuildButton) { void onRetry(true, rebuildButton); return; }
  const retryLoad = event.target.closest("[data-retry-load]");
  if (retryLoad) { void loadTerminalData(); return; }
  const pauseButton = event.target.closest("#rr-log-pause");
  if (pauseButton) {
    logPaused = !logPaused;
    pauseButton.setAttribute("aria-pressed", String(logPaused));
    pauseButton.innerHTML = `${icon(logPaused ? "player-play" : "player-pause", "icon icon--tiny")}${logPaused ? "Resume" : "Pause"}`;
    if (!logPaused) renderLogList();
    return;
  }
  if (event.target.closest("#rr-log-copy")) { void copyLog(); return; }
  if (event.target.closest("#rr-history-copy")) { void copyHistoryTable(); return; }
  const clientPdfButton = event.target.closest("#rr-client-pdf");
  if (clientPdfButton) { void onClientPdf(clientPdfButton); return; }
  const retryPeriodButton = event.target.closest("[data-retry-period]");
  if (retryPeriodButton) {
    void onRetryPeriod(retryPeriodButton.dataset.retryPeriod, retryPeriodButton);
    return;
  }
  const viewFile = event.target.closest("[data-view-file]");
  if (viewFile) { openFileDialog(viewFile.dataset.viewFile, viewFile.dataset.viewName); return; }
  const openFile = event.target.closest("[data-open-file]");
  if (openFile) { openFileDialog(openFile.dataset.openFile, openFile.dataset.openName); return; }
  if (event.target.closest("#rr-table")) { onTableCellClick(event); return; }
}

function onRootChange(event) {
  if (event.target.id === "rr-log-level") {
    logLevelFilter = event.target.value;
    renderLogList();
  }
}
