import { createSyncJob, getDashboard, getClientAnalytics, getClientAlerts, getClientAnalyticsChart, getReportingNinjaDataset, getTechAuditLatest, getTechAuditFindings, startTechAudit, recheckTechAuditFindings, techAuditExportUrl, getBlogsLatest, getMonitorSummary, isAbortError, isFixtureMode } from "./api.js?v=20260811-monitor1";
import { clientHeaderSkeleton, panelSkeleton, sectionSkeleton } from "./skeleton.js?v=20260811-monitor1";
import { clientStatusBadge, clientStatusNote, isPaused, openClientStatusDialog } from "./client-status.js?v=20260811-monitor1";
import { renderAuditPanel, renderAuditError } from "./audit-panel.js?v=20260811-monitor1";
import { buildAhrefsHash, buildBlogsHash, buildMonitorClientHash, replaceClientHash } from "./router.js?v=20260811-monitor1";
import { escapeHtml, formatTimestamp, icon, initials, isTerminal, MONITOR_SEVERITY_META, percent, PROVIDERS, STAGE_LABELS, STATE_META, stateClass } from "./formatters.js?v=20260811-monitor1";
import { createAnalyticsTab } from "./analytics-tabs.js?v=20260811-monitor1";
import { renderAlertsBlock } from "./alerts-block.js?v=20260811-monitor1";
import { renderChart, destroyChart } from "./charts.js?v=20260811-monitor1";
import { destroyReportingNinjaChart, renderReportingNinjaError, renderReportingNinjaPanel } from "./reporting-ninja-panel.js?v=20260811-monitor1";
import { watchJob } from "./sse.js?v=20260811-monitor1";

const PERIODS = [
  { key: "week", label: "Week" },
  { key: "month", label: "Month" },
  { key: "quarter", label: "Quarter" },
  { key: "year", label: "Year" },
  { key: "custom", label: "Custom" },
];

const uaDay = new Intl.DateTimeFormat("en-US", { day: "numeric", month: "short" });
const uaDayYear = new Intl.DateTimeFormat("en-US", { day: "numeric", month: "short", year: "numeric" });

export function formatUaRange(start, end) {
  if (!start || !end) return "dates unavailable";
  const startDate = new Date(`${start}T12:00:00`);
  const endDate = new Date(`${end}T12:00:00`);
  const sameYear = startDate.getFullYear() === endDate.getFullYear();
  return sameYear
    ? `${uaDay.format(startDate)} – ${uaDayYear.format(endDate)}`
    : `${uaDayYear.format(startDate)} – ${uaDayYear.format(endDate)}`;
}

export function formatUaNumber(value) {
  if (value === null || value === undefined) return "—";
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  return Math.abs(number) >= 10000
    ? new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 }).format(number)
    : new Intl.NumberFormat("en-US").format(number);
}

export function formatUaDelta(value) {
  if (value === null || value === undefined) return "—";
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  return new Intl.NumberFormat("en-US", { signDisplay: "exceptZero" }).format(number);
}

export function formatUaPercent(value) {
  if (value === null || value === undefined) return null;
  const number = Number(value);
  if (!Number.isFinite(number)) return null;
  return `${new Intl.NumberFormat("en-US", { maximumFractionDigits: 1, signDisplay: "exceptZero" }).format(number)}%`;
}

export function formatUaCtr(value) {
  if (value === null || value === undefined) return "—";
  const number = Number(value) * 100;
  if (!Number.isFinite(number)) return "—";
  return `${new Intl.NumberFormat("en-US", { maximumFractionDigits: 2 }).format(number)}%`;
}

export function formatUaPosition(value) {
  if (value === null || value === undefined) return "—";
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  return new Intl.NumberFormat("en-US", { maximumFractionDigits: 1 }).format(number);
}

const root = document.querySelector("#client-page-root");
let requestToken = 0;
let current = null;
let analyticsTab = null;
let stopLiveWatching = null;
let liveCollection = null;
const autoLiveAttempts = new Set();
// Technical SEO audit (task-25). Independent of the selected period: an audit
// is a point-in-time crawl, so it loads once per client, not per period change.
let auditSummary = null;
let auditFindings = null;
let auditBusy = false;
let stopAuditWatching = null;
const pendingRecheck = new Set();
// Client Blogs (task-26). Independent of the selected period: an inventory is
// a point-in-time crawl, so it loads once per client, not per period change.
let blogsSummary = null;
// Site Monitor (task-36). Same reasoning: a continuous background check, not
// tied to the selected reporting period.
let monitorSummary = null;
const AUDIT_PAGE_SIZE = 25;
const GA4_DATASETS = ["ga4_channels", "ga4_landing_pages", "ga4_events", "ga4_devices", "ga4_countries"];

function paramsFromQuery(query) {
  const period = ["week", "month", "quarter", "year", "custom"].includes(query.get("period")) ? query.get("period") : "week";
  return {
    period,
    anchor: query.get("anchor") || "",
    customStart: query.get("start") || "",
    customEnd: query.get("end") || "",
    tab: query.get("tab") === "query" ? "query" : "page",
    sortBy: ["clicks", "impressions", "ctr", "position"].includes(query.get("sort")) ? query.get("sort") : "clicks",
    search: query.get("q") || "",
    page: Math.max(1, Number(query.get("page")) || 1),
    ga4Dataset: GA4_DATASETS.includes(query.get("ga4")) ? query.get("ga4") : "ga4_channels",
    ga4Search: query.get("ga4q") || "",
    auditCategory: query.get("cat") || "",
    auditSearch: query.get("fq") || "",
    auditOffset: Math.max(0, Number(query.get("foff")) || 0),
    focus: query.get("focus") === "audit" ? "audit" : "",
  };
}

function persist() {
  replaceClientHash(current.clientId, {
    period: current.period,
    anchor: current.anchor,
    start: current.period === "custom" ? current.customStart : "",
    end: current.period === "custom" ? current.customEnd : "",
    tab: current.tab === "query" ? "query" : "",
    sort: current.sortBy === "clicks" ? "" : current.sortBy,
    q: current.search,
    page: current.page > 1 ? current.page : "",
    ga4: current.ga4Dataset === "ga4_channels" ? "" : current.ga4Dataset,
    ga4q: current.ga4Search,
    cat: current.auditCategory,
    fq: current.auditSearch,
    foff: current.auditOffset > 0 ? current.auditOffset : "",
  });
}

export async function initClientPage(clientId, query) {
  stopLiveWatching?.();
  stopLiveWatching = null;
  stopAuditWatching?.();
  stopAuditWatching = null;
  liveCollection = null;
  auditSummary = null;
  auditFindings = null;
  auditBusy = false;
  pendingRecheck.clear();
  blogsSummary = null;
  monitorSummary = null;
  destroyChart();
  destroyReportingNinjaChart();
  current = { clientId, ...paramsFromQuery(query) };
  root.innerHTML = shell();
  analyticsTab = createAnalyticsTab({
    container: document.querySelector("#cp-tabpanel"),
    getState: () => current,
    onChange(patch) {
      Object.assign(current, patch);
      persist();
      analyticsTab.render();
    },
  });
  bindShell();
  await loadClient();
  if (current.focus === "audit") {
    const audit = document.querySelector("#cp-audit");
    audit?.setAttribute("tabindex", "-1");
    audit?.classList.add("audit-section--focused");
    requestAnimationFrame(() => {
      audit?.scrollIntoView({ behavior: "smooth", block: "start" });
      audit?.focus({ preventScroll: true });
    });
  }
}

// Skeleton in / content out. `aria-busy` is what tells a screen reader that a
// live region is mid-update, so it is set and cleared in exactly one pair of
// helpers rather than left to each renderer to remember.
function setBusy(selector, html) {
  const node = document.querySelector(selector);
  if (!node) return;
  node.setAttribute("aria-busy", "true");
  node.innerHTML = html;
}

function settle(selector) {
  document.querySelector(selector)?.setAttribute("aria-busy", "false");
}

// The two exact date ranges are the anchor of every number on this page, so
// their placeholder keeps their shape (two labelled chips) instead of collapsing
// the row to a one-line "Loading…" that later pushes the panel taller.
function periodDatesSkeleton() {
  return `<span class="skeleton period-dates-skeleton"><span class="visually-hidden">Loading period dates…</span>
    <span class="skeleton-line"></span><span class="skeleton-line"></span></span>`;
}

function shell() {
  return `
    <a class="back-link" href="#/">${icon("arrow-left")}<span>All clients</span></a>
    <header class="client-page__header" id="cp-header" aria-live="polite" aria-busy="true">
      ${clientHeaderSkeleton()}
    </header>
    <section class="period-panel" aria-label="Comparison period">
      <div class="period-panel__kinds" role="group" aria-label="Period type" id="cp-period-kinds">
        ${PERIODS.map((item) => `<button type="button" data-period-kind="${item.key}" aria-pressed="${item.key === current.period}">${item.label}</button>`).join("")}
      </div>
      <div class="period-panel__custom" id="cp-custom-range" ${current.period === "custom" ? "" : "hidden"}>
        <label>From<input type="date" id="cp-custom-start" value="${escapeHtml(current.customStart)}"></label>
        <label>To<input type="date" id="cp-custom-end" value="${escapeHtml(current.customEnd)}"></label>
        <button type="button" class="button button--quiet" id="cp-custom-apply">Apply</button>
      </div>
      <p class="period-panel__dates" id="cp-period-dates" aria-live="polite" aria-busy="true">${periodDatesSkeleton()}</p>
      <div class="period-panel__live">
        <button type="button" class="button button--quiet" id="cp-live-refresh">${icon("refresh")}Refresh live data</button>
        <div class="live-collection" id="cp-live-status" aria-live="polite" hidden></div>
      </div>
      <p class="period-panel__note" id="cp-coverage-note" hidden></p>
    </section>
    <div id="cp-gate" class="state-panel" hidden></div>
    <div id="cp-content">
      <section class="monitor-section" id="cp-monitor" aria-label="Site monitor" aria-live="polite" aria-busy="true">
        ${sectionSkeleton({ label: "site monitor", kpis: 1 })}
      </section>
      <section class="blogs-section" id="cp-blogs" aria-label="Client blogs" aria-live="polite" aria-busy="true">
        ${sectionSkeleton({ label: "blogs", kpis: 1 })}
      </section>
      <section class="ahrefs-section" id="cp-ahrefs" aria-label="Ahrefs live Site Audit" aria-live="polite" aria-busy="true">
        ${sectionSkeleton({ label: "Ahrefs Site Audit", kpis: 3 })}
      </section>
      <section class="audit-section" id="cp-audit" aria-label="Technical SEO audit" aria-live="polite" aria-busy="true">
        ${sectionSkeleton({ label: "the technical audit", kpis: 4, rows: 5, columns: 3 })}
      </section>
      <section class="rn-section" id="cp-reporting-ninja" aria-label="Reporting Ninja and GA4 analytics" aria-live="polite" aria-busy="true">
        ${sectionSkeleton({ label: "analytics", kpis: 4, chart: true, rows: 4, columns: 3 })}
      </section>
      <div id="cp-gsc-content">
      <section class="analytics-tabs" aria-label="Page and query analytics">
        <div class="section-heading"><div><p class="eyebrow">Reporting Ninja · Search Console</p><h2>Organic search detail</h2></div></div>
        <div class="tablist" role="tablist" aria-label="Data breakdown">
          <button type="button" role="tab" id="cp-tab-page" aria-controls="cp-tabpanel" aria-selected="${String(current.tab === "page")}">${icon("file-text")}Pages</button>
          <button type="button" role="tab" id="cp-tab-query" aria-controls="cp-tabpanel" aria-selected="${String(current.tab === "query")}">${icon("search")}Queries</button>
        </div>
        <div id="cp-tabpanel" role="tabpanel" aria-live="polite" aria-busy="true">${panelSkeleton({ label: "pages and queries", rows: 8, columns: 4 })}</div>
      </section>
      <section class="alerts-section" id="cp-alerts" aria-label="Alerts: drops and growth" aria-live="polite"></section>
      <section class="chart-section" id="cp-chart" aria-label="Clicks and impressions trend"></section>
      </div>
    </div>`;
}

function bindShell() {
  document.querySelector("#cp-period-kinds").addEventListener("click", (event) => {
    const button = event.target.closest("[data-period-kind]");
    if (!button) return;
    const kind = button.dataset.periodKind;
    if (kind === current.period) return;
    current.period = kind;
    current.anchor = "";
    current.periodCurrentStart = "";
    current.periodCurrentEnd = "";
    current.page = 1;
    document.querySelectorAll("[data-period-kind]").forEach((item) => item.setAttribute("aria-pressed", String(item === button)));
    document.querySelector("#cp-custom-range").hidden = kind !== "custom";
    persist();
    if (kind !== "custom" || (current.customStart && current.customEnd)) loadPeriodData();
  });
  document.querySelector("#cp-custom-apply").addEventListener("click", () => {
    const start = document.querySelector("#cp-custom-start").value;
    const end = document.querySelector("#cp-custom-end").value;
    if (!start || !end || start > end) {
      document.querySelector("#cp-period-dates").textContent = "Enter a valid range: start must not be after end.";
      return;
    }
    current.customStart = start;
    current.customEnd = end;
    current.periodCurrentStart = "";
    current.periodCurrentEnd = "";
    current.page = 1;
    persist();
    loadPeriodData();
  });
  document.querySelector("#cp-live-refresh").addEventListener("click", () => startLiveCollection());
  document.querySelector("#cp-tab-page").addEventListener("click", () => switchTab("page"));
  document.querySelector("#cp-tab-query").addEventListener("click", () => switchTab("query"));
  document.querySelector(".tablist").addEventListener("keydown", (event) => {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    event.preventDefault();
    switchTab(current.tab === "page" ? "query" : "page");
    document.querySelector(`#cp-tab-${current.tab}`).focus();
  });
}

function switchTab(tab) {
  if (tab === current.tab) return;
  current.tab = tab;
  current.search = "";
  current.page = 1;
  document.querySelector("#cp-tab-page").setAttribute("aria-selected", String(tab === "page"));
  document.querySelector("#cp-tab-query").setAttribute("aria-selected", String(tab === "query"));
  persist();
  loadPeriodData();
}

async function loadClient({ allowAutoLive = true } = {}) {
  try {
    const dashboard = await getDashboard({ period: "week", requestKey: "client-shell" });
    const client = (dashboard.clients || []).find((item) => item.client_id === current.clientId);
    if (!client) {
      renderNotFound();
      return;
    }
    renderHeader(client);
    const gsc = (client.providers || []).find((item) => item.provider === "gsc");
    const ahrefs = (client.providers || []).find((item) => item.provider === "ahrefs");
    const reportingNinja = (client.providers || []).find((item) => item.provider === "reporting_ninja");
    current.gscConfigured = Boolean(gsc && gsc.state !== "not_configured");
    current.ahrefsConfigured = Boolean(ahrefs && ahrefs.state !== "not_configured");
    current.ahrefsSummary = ahrefs || null;
    renderAhrefsSummaryCard();
    // Period-independent: kicked off here, never re-run by a period change.
    loadTechAudit();
    loadBlogsSummary();
    loadMonitorSummary();
    current.reportingNinjaConfigured = Boolean(reportingNinja && reportingNinja.state !== "not_configured");
    current.reportingNinjaSummary = reportingNinja || null;
    if (!gsc || gsc.state === "not_configured") {
      renderGate("not_configured");
    } else if (gsc.state === "auth_required") {
      renderGate("auth_required");
    }
    await loadPeriodData({ allowAutoLive });
  } catch (error) {
    // A superseded request is not a failure: a newer load is already running
    // and will paint the header. Reporting it would replace a loading page with
    // a red panel for no reason (that is what "signal is aborted without
    // reason" was — see `isAbortError`).
    if (isAbortError(error)) return;
    renderClientLoadError(error);
  }
}

function renderClientLoadError(error) {
  const header = document.querySelector("#cp-header");
  if (!header) return;
  header.setAttribute("aria-busy", "false");
  header.innerHTML = `
    <div class="state-panel state-failed">
      ${icon("wifi-off")}
      <p><strong>Could not load this client.</strong><br>${escapeHtml(error.message)}. Nothing was changed — retry once the connection is back.</p>
      <button type="button" class="button button--primary" id="cp-retry-client">${icon("refresh")}Retry</button>
    </div>`;
  header.querySelector("#cp-retry-client").addEventListener("click", () => {
    setBusy("#cp-header", clientHeaderSkeleton());
    loadClient();
  });
}

function renderNotFound() {
  root.querySelector("#cp-header").setAttribute("aria-busy", "false");
  root.querySelector("#cp-header").innerHTML = `
    <div class="state-panel state-failed">
      ${icon("user-x")}
      <p><strong>Client not found.</strong><br>The link may be outdated.</p>
      <a class="button button--primary" href="#/">Back to client list</a>
    </div>`;
  document.querySelector(".period-panel")?.remove();
  document.querySelector("#cp-content")?.remove();
}

function renderGate(kind) {
  const gate = document.querySelector("#cp-gate");
  gate.hidden = false;
  if (kind === "not_configured") {
    gate.className = "state-panel state-not_configured";
    gate.innerHTML = `${icon("plug-off")}<p><strong>Google Search Console is not connected.</strong><br>The GA4 section remains available; search pages, queries and alerts require GSC.</p><a class="button button--quiet" href="#/">Manage integrations</a>`;
    document.querySelector("#cp-gsc-content").hidden = true;
  } else if (kind === "auth_required") {
    gate.className = "state-panel state-auth_required";
    gate.innerHTML = `${icon("key")}<p><strong>GSC re-authorization required.</strong><br>Data below may be stale or missing until authorization is refreshed on the main dashboard.</p><a class="button button--quiet" href="#/">Go to dashboard</a>`;
  }
}

function renderHeader(client) {
  const state = STATE_META[client.overall_status] || STATE_META.attention;
  const health = client.health;
  const healthText = health && health.value !== null && health.value !== undefined ? `${formatUaNumber(health.value)}%` : "—";
  const logo = client.logo_path
    ? `<img class="client-logo" src="${escapeHtml(client.logo_path)}" width="52" height="52" alt="Logo of ${escapeHtml(client.name)}" loading="lazy">`
    : `<span class="client-logo" aria-hidden="true">${escapeHtml(initials(client.name))}</span>`;
  const header = document.querySelector("#cp-header");
  header.innerHTML = `
    <div class="client-page__identity">
      ${logo}
      <div>
        <p class="eyebrow">Client analytics</p>
        <h1>${escapeHtml(client.name)}</h1>
        <a href="https://${encodeURI(client.domain)}" target="_blank" rel="noreferrer">${escapeHtml(client.domain)}</a>
      </div>
    </div>
    <div class="client-page__status">
      <span class="status-label ${stateClass(client.overall_status)}">${icon(state.icon)}${state.label}</span>
      ${clientStatusBadge(client)}
      <span class="client-page__sync">Last collection: ${formatTimestamp(client.last_synced_at, "not yet")}</span>
      ${isPaused(client) ? `<p class="client-status-note">${icon("info-circle", "icon icon--tiny")}<span>Bulk collections skip this client. Its history is untouched.</span></p>` : ""}
      ${clientStatusNote(client)}
    </div>
    <div class="client-page__health" title="Health % is calculated only from complete-quality services">
      <strong>${healthText}</strong>
      <small>Health${health ? ` · ${health.coverage_contributing}/${health.coverage_configured} services` : ""}</small>
    </div>`;
  // Той самий діалог, що й на дашборді — пауза ставиться там, де оператор
  // саме зараз дивиться на клієнта, а не тільки в одному «правильному» місці.
  header.querySelector('[data-action="client-status"]')?.addEventListener("click", (event) => {
    openClientStatusDialog({
      client,
      trigger: event.currentTarget,
      onSaved: () => loadClient(),
    });
  });
  settle("#cp-header");
}

function coverageNote() {
  const note = document.querySelector("#cp-coverage-note");
  if (current.period === "week" || current.period === "month") { note.hidden = true; return; }
  note.hidden = false;
  note.innerHTML = `${icon("info-circle")} Quarter, year and custom ranges are collected as exact live periods. Opening an empty range starts collection automatically.`;
}

function livePeriodKey() {
  return [
    current.clientId,
    current.period,
    current.periodCurrentStart || current.customStart,
    current.periodCurrentEnd || current.customEnd,
  ].join("|");
}

function datasetHasRows(dataset) {
  return Boolean(dataset?.rows?.length);
}

function reportingNinjaHasRows(data) {
  return datasetHasRows(data?.daily)
    || datasetHasRows(data?.currentDataset)
    || datasetHasRows(data?.gbp?.daily)
    || datasetHasRows(data?.gbp?.locations)
    || datasetHasRows(data?.gbp?.reviews);
}

function configuredLiveProviders() {
  return [
    current.ahrefsConfigured && "ahrefs",
    current.gscConfigured && "gsc",
    current.reportingNinjaConfigured && "reporting_ninja",
  ].filter(Boolean);
}

function setLiveButtonBusy(busy) {
  const button = document.querySelector("#cp-live-refresh");
  if (!button) return;
  button.disabled = busy;
  // `.icon--spin` is the class that animates (app.css) — without it the spinner
  // rendered frozen here too.
  button.innerHTML = busy
    ? `${icon("loader-2", "icon icon--spin")}Collecting live data…`
    : `${icon("refresh")}Refresh live data`;
}

function renderLiveStatus(job, { phase = "running", message = "" } = {}) {
  const container = document.querySelector("#cp-live-status");
  if (!container) return;
  container.hidden = false;
  const progress = percent(job?.progress_percent);
  const cells = job?.cells || [];
  const activeCell = cells.find((cell) => !["succeeded", "partial", "failed", "failed_interrupted", "not_configured", "cancelled", "skipped"].includes(cell.status));
  const stage = STAGE_LABELS[activeCell?.current_stage || job?.current_stage] || "Waiting for the collector";
  const failed = phase === "failed" || job?.status === "failed" || job?.status === "partial";
  const complete = phase === "complete";
  const title = complete
    ? "Live data updated"
    : failed
      ? "Live collection finished with issues"
      : phase === "starting" ? "Starting live collection" : `Fetching live data · ${progress}%`;
  const detail = message || (complete
    ? "The latest values have been saved and the page is being refreshed."
    : failed
      ? "Available results were saved. Use Refresh live data to retry failed services."
      : stage);
  const providers = cells.length
    ? cells.map((cell) => `${PROVIDERS[cell.provider]?.label || cell.provider}: ${STATE_META[cell.status]?.label || STAGE_LABELS[cell.current_stage] || cell.status}`).join(" · ")
    : (liveCollection?.providers || []).map((provider) => PROVIDERS[provider]?.label || provider).join(" · ");
  container.className = `live-collection ${failed ? "live-collection--failed" : complete ? "live-collection--complete" : "live-collection--running"}`;
  container.innerHTML = `
    <div class="live-collection__copy">
      ${icon(complete ? "circle-check" : failed ? "alert-triangle" : "cloud-download")}
      <p><strong>${escapeHtml(title)}</strong><br><span>${escapeHtml(detail)}</span>${providers ? `<small>${escapeHtml(providers)}</small>` : ""}</p>
    </div>
    ${complete || failed ? "" : `<progress max="100" value="${progress}" aria-label="Live collection progress: ${progress}%"></progress>`}`;
}

async function startLiveCollection({ providers = null, automatic = false } = {}) {
  if (liveCollection?.active) return;
  const selectedProviders = providers || configuredLiveProviders();
  if (!selectedProviders.length) {
    renderLiveStatus({ status: "failed", progress_percent: 0 }, { phase: "failed", message: "Connect Ahrefs, Search Console or Reporting Ninja before starting live collection." });
    return;
  }
  if (current.period === "custom" && (!current.customStart || !current.customEnd || current.customStart > current.customEnd)) {
    renderLiveStatus({ status: "failed", progress_percent: 0 }, { phase: "failed", message: "Enter a valid custom date range first." });
    return;
  }

  const key = livePeriodKey();
  liveCollection = { active: true, key, providers: selectedProviders };
  setLiveButtonBusy(true);
  renderLiveStatus({ status: "queued", progress_percent: 0, cells: [] }, { phase: "starting" });
  try {
    const accepted = await createSyncJob({
      client_ids: [current.clientId],
      providers: selectedProviders,
      period_kind: current.period,
      comparison_mode: "closed",
      anchor: current.anchor || undefined,
      custom_start: current.period === "custom" ? current.customStart : undefined,
      custom_end: current.period === "custom" ? current.customEnd : undefined,
      mode: "force_refresh",
      source_mode: "auto",
      idempotency_key: automatic
        ? `client-page-auto:${key}:${[...selectedProviders].sort().join(",")}`
        : (crypto.randomUUID?.() || `${Date.now()}-${Math.random()}`),
    }, {
      period_kind: current.period,
      clients: [{
        client_id: current.clientId,
        providers: selectedProviders.map((provider) => ({ provider, state: "complete" })),
      }],
    });
    const id = accepted.batch_id || accepted.job_id;
    stopLiveWatching?.();
    stopLiveWatching = watchJob({
      id,
      eventsUrl: accepted.events_url,
      onProgress(job) { renderLiveStatus(job); },
      onConnection(state) {
        if (state === "offline" && liveCollection?.active) {
          renderLiveStatus({ status: "running", progress_percent: 0, cells: [] }, { message: "Live connection interrupted; the server collection continues and will reconnect automatically." });
        }
      },
      async onTerminal(job) {
        const completedKey = liveCollection?.key;
        liveCollection = { ...(liveCollection || {}), active: false };
        stopLiveWatching = null;
        setLiveButtonBusy(false);
        const hasFailure = ["failed", "partial", "cancelled"].includes(job.status)
          || (job.cells || []).some((cell) => ["failed", "failed_interrupted", "not_configured", "cancelled"].includes(cell.status));
        renderLiveStatus(job, { phase: hasFailure ? "failed" : "complete" });
        if (job.status !== "cancelled") {
          await loadClient({ allowAutoLive: completedKey !== livePeriodKey() });
        }
      },
      onError(error) {
        renderLiveStatus({ status: "running", progress_percent: 0, cells: [] }, { message: `Progress temporarily unavailable: ${error.message}` });
      },
    });
  } catch (error) {
    liveCollection = { ...(liveCollection || {}), active: false };
    setLiveButtonBusy(false);
    renderLiveStatus({ status: "failed", progress_percent: 0, cells: [] }, { phase: "failed", message: `Could not start live collection: ${error.message}` });
  }
}

function chartHasTrend(chart) {
  return Boolean(chart?.some((series) => (series.points || []).filter((point) => point.value_numeric !== null).length >= 2));
}

async function maybeStartLiveCollection(rowsCurrent, ga4, chart) {
  const key = livePeriodKey();
  if (autoLiveAttempts.has(key) || liveCollection?.active) return;
  const missing = [];
  if (current.ahrefsConfigured && ["unavailable", "auth_required"].includes(current.ahrefsSummary?.state)) missing.push("ahrefs");
  if (current.gscConfigured && (!rowsCurrent?.rows?.length || !chartHasTrend(chart))) missing.push("gsc");
  if (current.reportingNinjaConfigured && !ga4?.error && !reportingNinjaHasRows(ga4)) missing.push("reporting_ninja");
  if (!missing.length) return;
  autoLiveAttempts.add(key);
  await startLiveCollection({ providers: missing, automatic: true });
}

async function loadPeriodData({ allowAutoLive = true } = {}) {
  const token = ++requestToken;
  coverageNote();
  const tabpanel = document.querySelector("#cp-tabpanel");
  tabpanel.setAttribute("aria-busy", "true");
  tabpanel.innerHTML = panelSkeleton({ label: "pages and queries", rows: 8, columns: 4 });
  setBusy("#cp-alerts", panelSkeleton({ label: "alerts", rows: 3, columns: 2 }));
  setBusy("#cp-reporting-ninja", sectionSkeleton({ label: "analytics", kpis: 4, chart: true, rows: 4, columns: 3 }));
  setBusy("#cp-period-dates", periodDatesSkeleton());
  try {
    const alerts = await getClientAlerts(current.clientId, {
      dimension: current.tab,
      period: current.period,
      anchor: current.anchor || undefined,
      customStart: current.period === "custom" ? current.customStart : undefined,
      customEnd: current.period === "custom" ? current.customEnd : undefined,
    });
    if (token !== requestToken) return;
    document.querySelector("#cp-period-dates").innerHTML =
      `<span><strong>Current:</strong> ${formatUaRange(alerts.period_current_start, alerts.period_current_end)}</span>` +
      `<span><strong>Previous:</strong> ${formatUaRange(alerts.period_previous_start, alerts.period_previous_end)}</span>`;
    settle("#cp-period-dates");
    current.periodCurrentStart = alerts.period_current_start;
    current.periodCurrentEnd = alerts.period_current_end;

    const ga4Promise = loadReportingNinjaPeriodData().catch((error) => ({ error }));
    const [rowsCurrent, rowsPrevious, chart, ga4] = await Promise.all([
      getClientAnalytics(current.clientId, {
        dimension: current.tab, variant: "current", period: current.period,
        anchor: current.anchor || undefined,
        customStart: current.period === "custom" ? current.customStart : undefined,
        customEnd: current.period === "custom" ? current.customEnd : undefined,
        sortBy: "clicks", limit: 500, offset: 0,
      }),
      getClientAnalytics(current.clientId, {
        dimension: current.tab, variant: "previous", period: "custom",
        customStart: alerts.period_previous_start, customEnd: alerts.period_previous_end,
        sortBy: "clicks", limit: 500, offset: 0,
      }),
      getClientAnalyticsChart(current.clientId, current.period, {
        anchor: current.anchor || undefined,
        customStart: current.period === "custom" ? current.customStart : undefined,
        customEnd: current.period === "custom" ? current.customEnd : undefined,
      }),
      ga4Promise,
    ]);
    if (token !== requestToken) return;

    analyticsTab.setData(rowsCurrent, rowsPrevious);
    analyticsTab.render();
    renderAlertsBlock(document.querySelector("#cp-alerts"), alerts);
    settle("#cp-alerts");
    renderChart(document.querySelector("#cp-chart"), chart, current.period);
    if (ga4?.error) {
      renderReportingNinjaError(document.querySelector("#cp-reporting-ninja"), ga4.error.message);
    } else {
      renderGa4Data(ga4);
    }
    settle("#cp-reporting-ninja");
    if (allowAutoLive) await maybeStartLiveCollection(rowsCurrent, ga4, chart);
  } catch (error) {
    if (isAbortError(error) || token !== requestToken) return;
    document.querySelector("#cp-tabpanel").innerHTML = `<div class="state-panel state-failed">${icon("wifi-off")}<p><strong>Failed to load data.</strong><br>${escapeHtml(error.message)}</p></div>`;
    document.querySelector("#cp-alerts").innerHTML = "";
  } finally {
    document.querySelector("#cp-tabpanel").setAttribute("aria-busy", "false");
  }
}

function ahrefsMetric(summary, key) {
  return (summary?.metrics || []).find((item) => item.metric_key === key)?.current_value ?? null;
}

function renderAhrefsSummaryCard() {
  const container = document.querySelector("#cp-ahrefs");
  const link = `<a class="button button--quiet" href="${buildAhrefsHash(current.clientId)}">${icon("arrow-right")}Open Site Audit</a>`;
  if (!current.ahrefsConfigured) {
    container.innerHTML = `<div class="section-heading"><div><p class="eyebrow">Ahrefs · Live Site Audit</p><h2>Technical health and crawl issues</h2></div></div><div class="state-panel state-not_configured">${icon("plug-off")}<p><strong>Ahrefs is not connected.</strong><br>Connect Ahrefs to see Site Audit health.</p><a class="button button--quiet" href="#/">Manage integrations</a></div>`;
    settle("#cp-ahrefs");
    return;
  }
  const summary = current.ahrefsSummary;
  const status = summary?.state || "unavailable";
  const cards = [
    ["Health score", ahrefsMetric(summary, "ahrefs_health_score"), "%"],
    ["Errors", ahrefsMetric(summary, "ahrefs_errors"), ""],
    ["Warnings", ahrefsMetric(summary, "ahrefs_warnings"), ""],
  ];
  container.innerHTML = `<div class="section-heading"><div><p class="eyebrow">Ahrefs · Live Site Audit</p><h2>Technical health and crawl issues</h2><p class="ahrefs-collected">Last collection: ${formatTimestamp(summary?.last_success_at, "not collected yet")}</p></div><span class="status-label status-${escapeHtml(status)}">${escapeHtml(status)}</span></div>
    <div class="ahrefs-kpis">${cards.map(([label, value, suffix]) => `<article class="ahrefs-kpi"><span>${label}</span><strong>${formatUaNumber(value)}${value === null || value === undefined ? "" : suffix}</strong></article>`).join("")}</div>
    <div class="ahrefs-summary-actions">${link}</div>`;
  settle("#cp-ahrefs");
}

// --- Client Blogs (task-26) -------------------------------------------------

async function loadMonitorSummary() {
  try {
    monitorSummary = await getMonitorSummary(current.clientId);
  } catch {
    monitorSummary = null; // renders as "never checked" — never a crash on this card
  }
  renderMonitorSummaryCard();
}

function renderMonitorSummaryCard() {
  const container = document.querySelector("#cp-monitor");
  if (!container) return;
  const link = `<a class="button button--quiet" href="${buildMonitorClientHash(current.clientId)}">${icon("arrow-right")}Open monitor</a>`;
  const neverChecked = !monitorSummary?.last_light_run && !monitorSummary?.last_full_run;
  if (neverChecked) {
    container.innerHTML = `<div class="section-heading"><div><p class="eyebrow">Site Monitor</p><h2>Robots, sitemap, pages &amp; DNS</h2></div></div><div class="state-panel state-not_configured">${icon("radar-2")}<p><strong>Never checked.</strong><br>Open the monitor and run "Check now" to start tracking this site.</p>${link}</div>`;
    settle("#cp-monitor");
    return;
  }
  const lastChecked = monitorSummary.last_light_run?.finished_at || monitorSummary.last_full_run?.finished_at;
  const openCritical = monitorSummary.open_critical || 0;
  const openWarning = monitorSummary.open_warning || 0;
  const tone = openCritical ? MONITOR_SEVERITY_META.critical : openWarning ? MONITOR_SEVERITY_META.warning : null;
  const sitemapHealth = monitorSummary.sitemap_health;
  container.innerHTML = `<div class="section-heading"><div><p class="eyebrow">Site Monitor</p><h2>Robots, sitemap, pages &amp; DNS</h2><p class="ahrefs-collected">Last check: ${formatTimestamp(lastChecked, "not finished yet")}</p></div>${tone ? `<span class="status-label monitor-summary-badge monitor-summary-badge--${openCritical ? "critical" : "warning"}">${icon(tone.icon)}${openCritical ? `${formatUaNumber(openCritical)} critical` : `${formatUaNumber(openWarning)} warning`}</span>` : ""}</div>
    <div class="ahrefs-kpis">
      <article class="ahrefs-kpi"><span>Open critical</span><strong>${formatUaNumber(openCritical)}</strong></article>
      <article class="ahrefs-kpi"><span>Open warning</span><strong>${formatUaNumber(openWarning)}</strong></article>
      <article class="ahrefs-kpi"><span>Sitemap health</span><strong>${sitemapHealth?.total ? `${formatUaNumber(sitemapHealth.ok)}/${formatUaNumber(sitemapHealth.total)} ok` : "—"}</strong></article>
    </div>
    <div class="ahrefs-summary-actions">${link}</div>`;
  settle("#cp-monitor");
}

async function loadBlogsSummary() {
  try {
    blogsSummary = await getBlogsLatest(current.clientId);
  } catch {
    blogsSummary = null; // renders as "no run yet" — never a crash on this card
  }
  renderBlogsSummaryCard();
}

function renderBlogsSummaryCard() {
  const container = document.querySelector("#cp-blogs");
  if (!container) return;
  const link = `<a class="button button--quiet" href="${buildBlogsHash(current.clientId)}">${icon("arrow-right")}Open blogs</a>`;
  if (!blogsSummary?.has_run) {
    container.innerHTML = `<div class="section-heading"><div><p class="eyebrow">Blogs</p><h2>Published posts</h2></div></div><div class="state-panel state-not_configured">${icon("rss")}<p><strong>No blog crawl yet.</strong><br>Open blogs and run "Refresh data" to discover every published post.</p>${link}</div>`;
    settle("#cp-blogs");
    return;
  }
  const status = blogsSummary.quality || "complete";
  container.innerHTML = `<div class="section-heading"><div><p class="eyebrow">Blogs</p><h2>Published posts</h2><p class="ahrefs-collected">Last crawl: ${formatTimestamp(blogsSummary.observed_at, "not crawled yet")}</p></div><span class="status-label status-${escapeHtml(status)}">${escapeHtml(status)}</span></div>
    <div class="ahrefs-kpis"><article class="ahrefs-kpi"><span>Posts</span><strong>${formatUaNumber(blogsSummary.post_count)}</strong></article></div>
    <div class="ahrefs-summary-actions">${link}</div>`;
  settle("#cp-blogs");
}

// --- Technical SEO audit (task-25) ----------------------------------------

function renderAudit() {
  const container = document.querySelector("#cp-audit");
  if (!container) return;
  renderAuditPanel(container, {
    summary: auditSummary,
    findings: auditFindings,
    state: current,
    exportUrl: techAuditExportUrl(current.clientId),
    busy: auditBusy,
    pendingRecheck,
    onRunAudit: () => startAuditRun(),
    onSelectCategory(category) {
      const next = current.auditCategory === category ? "" : category;
      current.auditCategory = next;
      current.auditSearch = "";
      current.auditOffset = 0;
      auditFindings = null;
      persist();
      renderAudit();
      if (next) loadAuditFindings();
    },
    onSearch(value) {
      current.auditSearch = value;
      current.auditOffset = 0;
      persist();
      loadAuditFindings();
    },
    onPage(offset) {
      current.auditOffset = offset;
      persist();
      loadAuditFindings();
    },
    onRecheck: (id) => recheckFinding(id),
  });
  settle("#cp-audit");
}

async function loadTechAudit() {
  try {
    auditSummary = await getTechAuditLatest(current.clientId);
    renderAudit();
    if (current.auditCategory) await loadAuditFindings();
  } catch (error) {
    if (isAbortError(error)) return;
    renderAuditError(document.querySelector("#cp-audit"), error.message);
  }
}

async function loadAuditFindings() {
  if (!current.auditCategory) return;
  try {
    auditFindings = await getTechAuditFindings(current.clientId, {
      category: current.auditCategory,
      q: current.auditSearch || undefined,
      limit: AUDIT_PAGE_SIZE,
      offset: current.auditOffset,
    });
    renderAudit();
  } catch (error) {
    if (isAbortError(error)) return;
    renderAuditError(document.querySelector("#cp-audit"), error.message);
  }
}

async function startAuditRun() {
  if (auditBusy) return;
  auditBusy = true;
  renderAudit();
  renderLiveStatus({ status: "queued", progress_percent: 0, cells: [] }, {
    phase: "starting",
    message: "Crawling the site and running the deterministic detectors.",
  });
  try {
    const accepted = await startTechAudit(current.clientId);
    if (isFixtureMode()) {
      auditBusy = false;
      renderLiveStatus({ status: "succeeded", progress_percent: 100, cells: [] }, { phase: "complete" });
      await loadTechAudit();
      return;
    }
    stopAuditWatching?.();
    stopAuditWatching = watchJob({
      id: accepted.job_id,
      eventsUrl: accepted.events_url,
      onProgress(job) { renderLiveStatus(job); },
      onConnection(state) {
        if (state === "offline" && auditBusy) {
          renderLiveStatus({ status: "running", progress_percent: 0, cells: [] }, {
            message: "Live connection interrupted; the crawl continues on the server.",
          });
        }
      },
      async onTerminal(job) {
        auditBusy = false;
        stopAuditWatching = null;
        const failed = ["failed", "cancelled"].includes(job.status);
        renderLiveStatus(job, { phase: failed ? "failed" : "complete" });
        auditFindings = null;
        await loadTechAudit();
      },
      onError(error) {
        renderLiveStatus({ status: "running", progress_percent: 0, cells: [] }, {
          message: `Progress temporarily unavailable: ${error.message}`,
        });
      },
    });
  } catch (error) {
    auditBusy = false;
    renderAudit();
    renderLiveStatus({ status: "failed", progress_percent: 0, cells: [] }, {
      phase: "failed",
      message: `Could not start the audit: ${error.message}`,
    });
  }
}

async function recheckFinding(id) {
  if (pendingRecheck.has(id)) return;
  const row = (auditFindings?.rows || []).find((item) => item.id === id);
  if (!row) return;
  pendingRecheck.add(id);
  renderAudit();
  const release = () => { pendingRecheck.delete(id); renderAudit(); };
  try {
    const accepted = await recheckTechAuditFindings(current.clientId, [
      { slug: row.slug, url: row.url },
    ]);
    if (isFixtureMode()) { release(); return; }
    watchJob({
      id: accepted.job_id,
      eventsUrl: accepted.events_url,
      onProgress() {},
      async onTerminal() {
        pendingRecheck.delete(id);
        await loadAuditFindings();
        try { auditSummary = await getTechAuditLatest(current.clientId); } catch { /* keep the old summary */ }
        renderAudit();
      },
      onError() { release(); },
    });
  } catch (error) {
    release();
    renderLiveStatus({ status: "failed", progress_percent: 0, cells: [] }, {
      phase: "failed",
      message: error.code === "needs_full_rerun"
        ? "This finding is site-graph scoped — run a full audit to re-verify it."
        : `Could not start the re-check: ${error.message}`,
    });
  }
}

async function loadReportingNinjaPeriodData() {
  if (!current.reportingNinjaConfigured) {
    return { summary: current.reportingNinjaSummary, daily: null, currentDataset: null, previousDataset: null };
  }
  const common = {
    period: current.period,
    anchor: current.anchor || undefined,
    customStart: current.period === "custom" ? current.customStart : undefined,
    customEnd: current.period === "custom" ? current.customEnd : undefined,
    limit: 500,
  };
  const definitionSort = current.ga4Dataset === "ga4_events" ? "events" : "sessions";
  const dashboardPromise = current.period === "week" || current.period === "month"
    ? getDashboard({ period: current.period, anchor: current.anchor || undefined, requestKey: "client-reporting-ninja" })
    : Promise.resolve(null);
  const [dashboard, daily, currentDataset, previousDataset, gbpDaily, gbpLocations, gbpSearchTerms, gbpReviews] = await Promise.all([
    dashboardPromise,
    getReportingNinjaDataset(current.clientId, "ga4_daily", { ...common, role: "current", sortBy: "date", limit: 500 }),
    getReportingNinjaDataset(current.clientId, current.ga4Dataset, { ...common, role: "current", sortBy: definitionSort }),
    getReportingNinjaDataset(current.clientId, current.ga4Dataset, { ...common, role: "previous", sortBy: definitionSort }),
    getReportingNinjaDataset(current.clientId, "gbp_daily", { ...common, role: "current", sortBy: "date" }),
    getReportingNinjaDataset(current.clientId, "gbp_locations", { ...common, role: "current", sortBy: "interactions" }),
    getReportingNinjaDataset(current.clientId, "gbp_search_terms", { ...common, role: "current", sortBy: "impressions" }),
    getReportingNinjaDataset(current.clientId, "gbp_reviews", { ...common, role: "current", sortBy: "reviews" }),
  ]);
  const card = dashboard?.clients?.find((item) => item.client_id === current.clientId);
  const summary = card?.providers?.find((item) => item.provider === "reporting_ninja") || current.reportingNinjaSummary;
  return {
    summary, daily, currentDataset, previousDataset,
    gbp: { daily: gbpDaily, locations: gbpLocations, searchTerms: gbpSearchTerms, reviews: gbpReviews },
  };
}

function renderGa4Data(data) {
  renderReportingNinjaPanel(document.querySelector("#cp-reporting-ninja"), {
    ...data,
    state: current,
    onChange(patch, options = { reload: true }) {
      Object.assign(current, patch);
      persist();
      if (options.reload === false) {
        renderGa4Data(data);
      } else {
        loadPeriodData();
      }
    },
  });
}
