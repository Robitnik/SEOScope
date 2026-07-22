import { getDashboard, getClientAnalytics, getClientAlerts, getClientAnalyticsChart } from "./api.js";
import { replaceClientHash } from "./router.js";
import { escapeHtml, formatTimestamp, icon, initials, STATE_META, stateClass } from "./formatters.js";
import { createAnalyticsTab } from "./analytics-tabs.js";
import { renderAlertsBlock } from "./alerts-block.js";
import { renderChart, destroyChart } from "./charts.js";

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
  });
}

export async function initClientPage(clientId, query) {
  destroyChart();
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
}

function shell() {
  return `
    <a class="back-link" href="/">${icon("arrow-left")}<span>All clients</span></a>
    <header class="client-page__header" id="cp-header" aria-live="polite">
      <div class="skeleton-line skeleton-line--title"></div>
      <div class="skeleton-line skeleton-line--short"></div>
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
      <p class="period-panel__dates" id="cp-period-dates" aria-live="polite">Loading period dates…</p>
      <p class="period-panel__note" id="cp-coverage-note" hidden></p>
    </section>
    <div id="cp-gate" class="state-panel" hidden></div>
    <div id="cp-content">
      <section class="analytics-tabs" aria-label="Page and query analytics">
        <div class="tablist" role="tablist" aria-label="Data breakdown">
          <button type="button" role="tab" id="cp-tab-page" aria-controls="cp-tabpanel" aria-selected="${String(current.tab === "page")}">${icon("file-text")}Pages</button>
          <button type="button" role="tab" id="cp-tab-query" aria-controls="cp-tabpanel" aria-selected="${String(current.tab === "query")}">${icon("search")}Queries</button>
        </div>
        <div id="cp-tabpanel" role="tabpanel" aria-live="polite" aria-busy="true"></div>
      </section>
      <section class="alerts-section" id="cp-alerts" aria-label="Alerts: drops and growth" aria-live="polite"></section>
      <section class="chart-section" id="cp-chart" aria-label="Clicks and impressions trend"></section>
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
    current.page = 1;
    persist();
    loadPeriodData();
  });
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

async function loadClient() {
  try {
    const dashboard = await getDashboard({ period: "week" });
    const client = (dashboard.clients || []).find((item) => item.client_id === current.clientId);
    if (!client) {
      renderNotFound();
      return;
    }
    renderHeader(client);
    const gsc = (client.providers || []).find((item) => item.provider === "gsc");
    if (!gsc || gsc.state === "not_configured") {
      renderGate("not_configured");
      return;
    }
    if (gsc.state === "auth_required") {
      renderGate("auth_required");
    }
    await loadPeriodData();
  } catch (error) {
    document.querySelector("#cp-header").innerHTML = `<p class="state-panel state-failed">${icon("wifi-off")}<span><strong>Failed to load client.</strong><br>${escapeHtml(error.message)}</span></p>`;
  }
}

function renderNotFound() {
  root.querySelector("#cp-header").innerHTML = `
    <div class="state-panel state-failed">
      ${icon("user-x")}
      <p><strong>Client not found.</strong><br>The link may be outdated.</p>
      <a class="button button--primary" href="/">Back to client list</a>
    </div>`;
  document.querySelector(".period-panel")?.remove();
  document.querySelector("#cp-content")?.remove();
}

function renderGate(kind) {
  const gate = document.querySelector("#cp-gate");
  gate.hidden = false;
  if (kind === "not_configured") {
    gate.className = "state-panel state-not_configured";
    gate.innerHTML = `${icon("plug-off")}<p><strong>Google Search Console is not connected.</strong><br>Page, query analytics and alerts are only available for clients with GSC connected. Add the integration on the main dashboard.</p><a class="button button--primary" href="/">Go to dashboard</a>`;
    document.querySelector("#cp-content").hidden = true;
  } else if (kind === "auth_required") {
    gate.className = "state-panel state-auth_required";
    gate.innerHTML = `${icon("key")}<p><strong>GSC re-authorization required.</strong><br>Data below may be stale or missing until authorization is refreshed on the main dashboard.</p><a class="button button--quiet" href="/">Go to dashboard</a>`;
  }
}

function renderHeader(client) {
  const state = STATE_META[client.overall_status] || STATE_META.attention;
  const health = client.health;
  const healthText = health && health.value !== null && health.value !== undefined ? `${formatUaNumber(health.value)}%` : "—";
  const logo = client.logo_path
    ? `<img class="client-logo" src="${escapeHtml(client.logo_path)}" width="52" height="52" alt="Logo of ${escapeHtml(client.name)}" loading="lazy">`
    : `<span class="client-logo" aria-hidden="true">${escapeHtml(initials(client.name))}</span>`;
  document.querySelector("#cp-header").innerHTML = `
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
      <span class="client-page__sync">Last collection: ${formatTimestamp(client.last_synced_at, "not yet")}</span>
    </div>
    <div class="client-page__health" title="Health % is calculated only from complete-quality services">
      <strong>${healthText}</strong>
      <small>Health${health ? ` · ${health.coverage_contributing}/${health.coverage_configured} services` : ""}</small>
    </div>`;
}

function coverageNote() {
  const note = document.querySelector("#cp-coverage-note");
  if (current.period === "week" || current.period === "month") { note.hidden = true; return; }
  note.hidden = false;
  note.innerHTML = `${icon("info-circle")} Extended aggregation for quarter/year/custom is not yet implemented: only days that exactly match a stored period are shown. No match means the section will be empty, not a fabricated total.`;
}

async function loadPeriodData() {
  const token = ++requestToken;
  coverageNote();
  document.querySelector("#cp-tabpanel").setAttribute("aria-busy", "true");
  document.querySelector("#cp-alerts").innerHTML = `<div class="skeleton-line skeleton-line--short"></div><div class="skeleton-block"></div>`;
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

    const [rowsCurrent, rowsPrevious, chart] = await Promise.all([
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
      getClientAnalyticsChart(current.clientId, current.period),
    ]);
    if (token !== requestToken) return;

    analyticsTab.setData(rowsCurrent, rowsPrevious);
    analyticsTab.render();
    renderAlertsBlock(document.querySelector("#cp-alerts"), alerts);
    renderChart(document.querySelector("#cp-chart"), chart, current.period);
  } catch (error) {
    if (error.name === "AbortError" || token !== requestToken) return;
    document.querySelector("#cp-tabpanel").innerHTML = `<div class="state-panel state-failed">${icon("wifi-off")}<p><strong>Failed to load data.</strong><br>${escapeHtml(error.message)}</p></div>`;
    document.querySelector("#cp-alerts").innerHTML = "";
  } finally {
    document.querySelector("#cp-tabpanel").setAttribute("aria-busy", "false");
  }
}
