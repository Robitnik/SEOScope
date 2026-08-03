// Portfolio Trends (task-28): one line per client across the whole
// portfolio for a single daily metric (GSC clicks/impressions, GA4
// sessions/users). See docs/dev/done/task-28-portfolio-trends-page.md.
//
// Reading stays read-only: the chart renders whatever day-level history is
// already stored, and an empty result is a normal, well-explained state (§5),
// not a failure. task-30 adds the one thing that was missing — telling the
// operator HOW STALE that history is and letting them start the rolling-window
// collection that fixes it, without leaving the page for a CLI.

import { getPortfolioSeries, isAbortError } from "./api.js?v=20260803-connect1";
import { buildTrendsHash, replaceTrendsHash } from "./router.js?v=20260803-connect1";
import { STATE_META, escapeHtml, icon, stateClass } from "./formatters.js?v=20260803-connect1";
import { sectionSkeleton } from "./skeleton.js?v=20260803-connect1";
import { filterByClientGroup } from "./client-groups.js?v=20260803-connect1";
import { configureJobProgress, getJobProgress, showToast } from "./job-progress.js?v=20260803-connect1";
import {
  MAX_SERIES,
  buildSummary,
  colorAllocator,
  destroySparklines,
  destroyTrendsChart,
  renderLineChart,
  renderSparklineGrid,
} from "./trends-chart.js?v=20260803-connect1";

// Both daily datasets behind this page's four metrics (`gsc_daily`,
// `ga4_daily`) are refreshed together: the operator asks "make this page
// current", not "make this one series current", and re-collecting one provider
// would leave the other three metrics visibly behind.
const REFRESH_PROVIDERS = ["gsc", "reporting_ninja"];
const REFRESH_WINDOWS = [14, 30, 90];
const DEFAULT_REFRESH_WINDOW = 30;
const DAY_MS = 86_400_000;

const root = document.querySelector("#trends-page-root");

const METRICS = [
  { key: "gsc_clicks", label: "Clicks" },
  { key: "gsc_impressions", label: "Impressions" },
  { key: "rn_sessions", label: "Sessions" },
  { key: "rn_users", label: "Users" },
];
const BUCKETS = [
  { key: "day", label: "Day" },
  { key: "week", label: "Week" },
  { key: "month", label: "Month" },
];
const RANGES = [7, 30, 90, 180, 365];
// Below this, a weekly or monthly bucket collapses the whole range into one or
// two points. Picking 7d gets nudged to a daily bucket once, so the first click
// shows a readable series — but the operator can switch back to Week afterwards
// and that choice sticks (a coarse bucket over a short range is coarse, not
// wrong: `build_buckets` clips `days_expected` to the real span).
const DAILY_BUCKET_BELOW_DAYS = 14;
const VIEWS = [
  { key: "compare", label: "Compare" },
  { key: "all", label: "All clients" },
  { key: "indexed", label: "Indexed" },
];
const GROUPS = [
  { key: "all", label: "All" },
  { key: "vision", label: "Vision" },
  { key: "vertex", label: "Vertex" },
];

const numberFormatter = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });

let current = null;
let requestToken = 0;
// The raw API response for the current metric/bucket/days — cached so the
// client picker and group filter can re-render instantly from it without a
// network round-trip (they only ever narrow a set already in hand).
let lastPayload = null;

function paramsFromQuery(query) {
  const metric = METRICS.some((m) => m.key === query.get("metric")) ? query.get("metric") : "gsc_clicks";
  const bucket = BUCKETS.some((b) => b.key === query.get("bucket")) ? query.get("bucket") : "week";
  const days = RANGES.includes(Number(query.get("days"))) ? Number(query.get("days")) : 90;
  const view = VIEWS.some((v) => v.key === query.get("view")) ? query.get("view") : "compare";
  const group = GROUPS.some((g) => g.key === query.get("group")) ? query.get("group") : "all";
  // `null` = automatic (top-N by volume); a concrete (possibly empty) array
  // means the operator has explicitly curated the set via the picker.
  const clientsParam = query.get("clients");
  const clients = clientsParam === null ? null : clientsParam.split(",").filter(Boolean);
  return { metric, bucket, days, view, group, clients };
}

function persist() {
  replaceTrendsHash({
    metric: current.metric !== "gsc_clicks" ? current.metric : "",
    bucket: current.bucket !== "week" ? current.bucket : "",
    days: current.days !== 90 ? current.days : "",
    view: current.view !== "compare" ? current.view : "",
    group: current.group !== "all" ? current.group : "",
    clients: current.clients !== null ? current.clients.join(",") : "",
  });
}

export async function initTrendsPage(query) {
  current = paramsFromQuery(query);
  lastPayload = null;
  root.innerHTML = shell();
  bindControls();
  await load();
}

function segmented({ name, options, active, }) {
  return options.map((opt) => `
    <button type="button" data-${name}="${escapeHtml(opt.key)}" aria-pressed="${opt.key === active}">${escapeHtml(opt.label)}</button>
  `).join("");
}

function shell() {
  return `
    <header class="trends-header">
      <p class="eyebrow">Portfolio / Trends</p>
      <h1>Client Trends</h1>
      <p class="trends-lead">Every client, one metric, side by side — who is growing and who needs a look this week.</p>
      <div class="trends-freshness" id="tr-freshness" aria-live="polite"></div>
    </header>

    <section class="trends-toolbar" aria-label="Trend controls">
      <div class="trends-control">
        <span class="trends-control__label">Metric</span>
        <div class="period-panel__kinds" role="group" aria-label="Metric" id="tr-metric">
          ${segmented({ name: "metric", options: METRICS, active: current.metric })}
        </div>
      </div>
      <div class="trends-control">
        <span class="trends-control__label">Bucket</span>
        <div class="period-panel__kinds" role="group" aria-label="Bucket size" id="tr-bucket">
          ${segmented({ name: "bucket", options: BUCKETS, active: current.bucket })}
        </div>
      </div>
      <div class="trends-control">
        <span class="trends-control__label">Range</span>
        <div class="period-panel__kinds" role="group" aria-label="Date range" id="tr-days">
          ${segmented({ name: "days", options: RANGES.map((d) => ({ key: String(d), label: `${d}d` })), active: String(current.days) })}
        </div>
      </div>
      <div class="trends-control">
        <span class="trends-control__label">Portfolio</span>
        <div class="period-panel__kinds" role="group" aria-label="Portfolio group" id="tr-group">
          ${segmented({ name: "group", options: GROUPS, active: current.group })}
        </div>
      </div>
      <div class="trends-control">
        <span class="trends-control__label">View</span>
        <div class="period-panel__kinds" role="group" aria-label="View" id="tr-view">
          ${segmented({ name: "view", options: VIEWS, active: current.view })}
        </div>
      </div>
    </section>

    <section class="trends-section" id="tr-content" aria-live="polite" aria-busy="true">
      ${sectionSkeleton({ label: "portfolio trends", chart: true, rows: 6, columns: 5 })}
    </section>`;
}

const SEGMENT_GROUPS = [
  ["tr-metric", "metric"], ["tr-bucket", "bucket"], ["tr-days", "days"],
  ["tr-group", "group"], ["tr-view", "view"],
];

function syncSegments() {
  SEGMENT_GROUPS.forEach(([id, key]) => {
    root.querySelectorAll(`#${id} button`).forEach((button) => {
      button.setAttribute("aria-pressed", String(button.dataset[key] === String(current[key])));
    });
  });
}

function bindControls() {
  const groups = SEGMENT_GROUPS;
  groups.forEach(([id, key]) => {
    root.querySelector(`#${id}`).addEventListener("click", (event) => {
      const button = event.target.closest("button");
      if (!button) return;
      const value = button.dataset[key];
      const parsed = key === "days" ? Number(value) : value;
      if (parsed === current[key]) return;
      current[key] = parsed;
      if (key === "days" && parsed < DAILY_BUCKET_BELOW_DAYS) current.bucket = "day";
      persist();
      // Both groups are re-synced, not just the clicked one: the range switch
      // above can move the bucket, and a segment whose `aria-pressed` disagrees
      // with the state that was actually loaded is a lie to every user.
      syncSegments();
      void load();
    });
  });
}

// --- Freshness + refresh (task-30) -----------------------------------------

function parseDay(value) {
  const parsed = Date.parse(`${value}T00:00:00Z`);
  return Number.isFinite(parsed) ? parsed : null;
}

function formatDay(value) {
  return new Date(`${value}T00:00:00Z`).toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "UTC" });
}

/** How current the stored history is, judged against what the SOURCE can
 * actually have finalized by now — not against today. GSC revises its last
 * ~3 days and GA4 its last ~2 (`lag_days`), so "yesterday is missing" is the
 * normal state of a healthy series, and calling that "1 day behind" would make
 * the chip permanently red for no reason. */
function freshnessOf(payload, visibleSeries) {
  const days = visibleSeries.map((item) => item.last_day).filter(Boolean).map(parseDay).filter((value) => value !== null);
  if (!days.length) return { state: "no_data", dataThrough: null, daysBehind: null };
  const dataThrough = Math.max(...days);
  const lagDays = Number(payload.metric?.lag_days ?? 0);
  const expected = Date.parse(`${new Date(Date.now() - DAY_MS).toISOString().slice(0, 10)}T00:00:00Z`) - lagDays * DAY_MS;
  const daysBehind = Math.max(0, Math.round((expected - dataThrough) / DAY_MS));
  return {
    state: daysBehind === 0 ? "current" : daysBehind <= 2 ? "lagging" : "stale",
    dataThrough: new Date(dataThrough).toISOString().slice(0, 10),
    daysBehind,
  };
}

function refreshableClientIds(payload, group) {
  // Everything the page could plot for this metric: clients already in the
  // chart plus clients that have the integration but no rows yet. A
  // `not_configured` client is deliberately left out — its cell would resolve
  // to `not_configured` and add noise to the progress matrix without ever
  // producing a data point.
  const inChart = filterByClientGroup(payload.series || [], group).map((item) => item.client_id);
  const noData = filterByClientGroup((payload.excluded || []).filter((item) => item.state === "no_data"), group)
    .map((item) => item.client_id);
  return [...new Set([...inChart, ...noData])];
}

function clientDirectory(payload) {
  return [...(payload.series || []), ...(payload.excluded || [])].map((item) => ({
    client_id: item.client_id,
    name: item.name,
    domain: item.domain,
  }));
}

function renderFreshness(payload) {
  const host = root.querySelector("#tr-freshness");
  const visible = filterByClientGroup(payload.series || [], current.group);
  const freshness = freshnessOf(payload, visible);
  const clientIds = refreshableClientIds(payload, current.group);
  const chip = freshness.state === "no_data"
    ? `<span class="freshness-chip freshness-chip--stale">${icon("cloud-off", "icon icon--tiny")}No daily history collected</span>`
    : `<span class="freshness-chip freshness-chip--${freshness.state}">${icon(freshness.state === "current" ? "circle-check" : "clock-exclamation", "icon icon--tiny")}Data through ${escapeHtml(formatDay(freshness.dataThrough))}${freshness.daysBehind === 0 ? " · up to date" : ` · ${freshness.daysBehind} ${freshness.daysBehind === 1 ? "day" : "days"} behind`}</span>`;
  host.innerHTML = `
    ${chip}
    <div class="trends-refresh">
      <label class="trends-refresh__window">
        <span class="visually-hidden">Refresh window</span>
        <select id="tr-window">${REFRESH_WINDOWS.map((days) => `<option value="${days}" ${days === DEFAULT_REFRESH_WINDOW ? "selected" : ""}>Last ${days} days</option>`).join("")}</select>
      </label>
      <button type="button" class="button button--primary" id="tr-refresh" ${clientIds.length ? "" : "disabled"}>${icon("refresh")}Refresh data</button>
    </div>
    <p class="trends-refresh__note">Collects day-level Search Console and GA4 history. GSC finalizes its last 3 days and GA4 its last 2, so the newest buckets stay provisional.</p>`;
  host.querySelector("#tr-refresh")?.addEventListener("click", (event) => startRefresh(payload, event.currentTarget));
}

function startRefresh(payload, trigger) {
  const clientIds = refreshableClientIds(payload, current.group);
  if (!clientIds.length) {
    showToast("No client on this page has a connected Search Console or GA4 integration.");
    return;
  }
  const windowDays = Number(root.querySelector("#tr-window")?.value) || DEFAULT_REFRESH_WINDOW;
  // The modal names clients and (in fixture mode) builds its cells from this;
  // the trends page has no dashboard payload, so it supplies the same shape
  // from what it already knows about the portfolio.
  configureJobProgress({
    getDashboard: () => ({ clients: clientDirectory(payload), period_kind: "custom" }),
    onFinished: async () => {
      showToast("Trend data updated.");
      await load({ quiet: true });
    },
    onToast: showToast,
  });
  getJobProgress().openMetricsRefresh({ clientIds, providers: REFRESH_PROVIDERS, windowDays, trigger });
}

function totalOf(series) {
  return series.points.reduce((sum, point) => sum + (point.value === null ? 0 : Number(point.value)), 0);
}

function lastFinishedBucketIndex(buckets) {
  for (let i = buckets.length - 1; i >= 0; i -= 1) {
    if (!buckets[i].provisional) return i;
  }
  return -1;
}

// `quiet` reloads keep the current chart on screen while the new payload is
// fetched (used after a refresh run finishes): swapping a rendered chart for a
// skeleton and back would throw away scroll position and read as a failure
// even though nothing went wrong.
async function load({ quiet = false } = {}) {
  const token = ++requestToken;
  const content = root.querySelector("#tr-content");
  if (!quiet) {
    content.setAttribute("aria-busy", "true");
    content.innerHTML = sectionSkeleton({ label: "portfolio trends", chart: true, rows: 6, columns: 5 });
    destroyTrendsChart();
    destroySparklines();
  }

  try {
    const payload = await getPortfolioSeries({ metric: current.metric, bucket: current.bucket, days: current.days });
    if (token !== requestToken) return;
    content.setAttribute("aria-busy", "false");
    lastPayload = payload;
    renderFreshness(payload);
    if (quiet) {
      destroyTrendsChart();
      destroySparklines();
    }
    renderContent(content, payload);
  } catch (error) {
    if (isAbortError(error)) return;
    content.setAttribute("aria-busy", "false");
    content.innerHTML = `
      <div class="state-panel state-failed">
        ${icon("wifi-off")}
        <p><strong>Could not load portfolio trends.</strong><br>${escapeHtml(error.message)}. The last view stays available above once you retry.</p>
        <button type="button" class="button button--primary" id="tr-retry">${icon("refresh")}Retry</button>
      </div>`;
    content.querySelector("#tr-retry").addEventListener("click", () => void load());
  }
}

function backfillHint(metric) {
  const provider = metric.provider === "gsc" ? "gsc" : "reporting_ninja";
  return `docker compose run --rm api poetry run yovth-report backfill-daily-history --days 90 --providers ${provider}`;
}

function renderContent(content, payload) {
  const universe = filterByClientGroup(payload.series, current.group);
  const excludedByGroup = filterByClientGroup(payload.excluded, current.group);

  if (universe.length === 0 && excludedByGroup.length === 0) {
    content.innerHTML = `
      <div class="state-panel">
        ${icon("filter-off")}
        <p><strong>No clients in this portfolio group.</strong><br>Switch the Portfolio filter back to All.</p>
      </div>`;
    return;
  }

  if (universe.length === 0) {
    content.innerHTML = `
      <div class="state-panel">
        ${icon("cloud-download")}
        <p><strong>No daily history collected yet for this range.</strong><br>
        Run a one-time backfill, then reload this page:</p>
        <p class="trends-cli"><code>${escapeHtml(backfillHint(payload.metric))}</code></p>
      </div>
      ${renderExcludedList(excludedByGroup)}`;
    return;
  }

  const sorted = [...universe].sort((a, b) => totalOf(b) - totalOf(a));
  const lastFinished = lastFinishedBucketIndex(payload.buckets);

  if (current.clients !== null && current.clients.length === 0) {
    content.innerHTML = `
      ${renderPicker({ sorted, excluded: excludedByGroup, manualMode: true, manualSet: new Set(), capped: [] })}
      <div class="state-panel">
        ${icon("filter-off")}
        <p><strong>No clients selected.</strong><br>${sorted.length} ${sorted.length === 1 ? "client has" : "clients have"} data in this range — pick some above, or take them all.</p>
        <button type="button" class="button button--primary" id="tr-empty-all">${icon("check")}Select all</button>
      </div>`;
    bindPickerEvents(content);
    content.querySelector("#tr-empty-all").addEventListener("click", () => selectAllClients(content));
    return;
  }

  // Manual selection (the picker) narrows `sorted` down first; automatic mode
  // (the picker's default) leaves it untouched and relies on the view-based
  // cap below — same top-N-by-volume behaviour as before the picker existed.
  const manualMode = current.clients !== null;
  const manualSet = manualMode ? new Set(current.clients) : null;
  const eligible = manualMode ? sorted.filter((item) => manualSet.has(item.client_id)) : sorted;
  const capped = current.view === "all" ? eligible : eligible.slice(0, MAX_SERIES);
  const truncatedCount = eligible.length - capped.length;
  // Auto mode keeps the table honest about the WHOLE portfolio even while the
  // chart caps at 8; manual mode means the operator explicitly chose this set,
  // so table and chart show the same clients.
  const tableRows = manualMode ? eligible : sorted;

  const scopeLabel = manualMode ? "your selection" : "this range";
  const truncationNote = truncatedCount > 0
    ? `<p class="trends-note">${icon("info-circle")}Showing the top ${MAX_SERIES} of ${eligible.length} clients by total ${escapeHtml(payload.metric.label.toLowerCase())} in ${scopeLabel}.
       <a href="${buildTrendsHash({ ...current, view: "all" })}" data-view-switch="all">See all clients</a> instead.</p>`
    : "";

  content.innerHTML = `
    ${renderPicker({ sorted, excluded: excludedByGroup, manualMode, manualSet, capped })}
    ${truncationNote}
    <div class="chart-section trends-chart-section">
      <div id="tr-chart-wrap"></div>
      <p class="chart-summary" id="tr-summary"></p>
    </div>
    <div class="table-scroll">
      <table class="analytics-table trends-table">
        <thead><tr>
          <th>Client</th>
          <th>Current period</th>
          <th>Previous period</th>
          <th>Δ</th>
          <th>Total (window)</th>
          <th>Coverage</th>
          <th>Last data day</th>
        </tr></thead>
        <tbody>${tableRows.map((item) => renderTableRow(item, lastFinished)).join("")}</tbody>
      </table>
    </div>
    ${current.view === "all" ? "" : renderExcludedList(excludedByGroup)}`;

  const chartWrap = content.querySelector("#tr-chart-wrap");
  if (current.view === "all") {
    // Small multiples show every client's own reason inline (dashed cards) —
    // the separate "Not shown" list below is redundant for this view only.
    renderSparklineGrid(chartWrap, { series: capped, excluded: excludedByGroup, buckets: payload.buckets });
  } else {
    chartWrap.classList.add("chart-wrap");
    chartWrap.innerHTML = `<canvas id="tr-canvas" role="img" aria-label="Line chart of ${escapeHtml(payload.metric.label)} per client"></canvas>`;
    renderLineChart(chartWrap.querySelector("#tr-canvas"), {
      series: capped,
      buckets: payload.buckets,
      bucketKind: payload.bucket,
      indexed: current.view === "indexed",
    });
    // Keep the table's color swatches in sync with the chart's stable
    // per-client slots (same call, same order -> same colors, no reshuffle).
    colorAllocator.assign(capped.map((item) => item.client_id));
  }
  content.querySelector("#tr-summary").textContent = buildSummary({
    series: capped, buckets: payload.buckets, metric: payload.metric, bucketKind: payload.bucket,
  });

  content.querySelector("[data-view-switch]")?.addEventListener("click", (event) => {
    event.preventDefault();
    current.view = "all";
    persist();
    root.querySelectorAll("#tr-view button").forEach((btn) => btn.setAttribute("aria-pressed", String(btn.dataset.view === "all")));
    void load();
  });

  bindPickerEvents(content);
}

// Manual mode is entered the moment the operator touches any checkbox — up to
// then the picker just mirrors the automatic top-N-by-volume selection, so
// the boxes always show what is actually on screen, not a separate opinion.
function selectAllClients(content) {
  const selectable = filterByClientGroup(lastPayload.series || [], current.group).map((item) => item.client_id);
  current.clients = selectable;
  if (selectable.length > MAX_SERIES) {
    current.view = "all";
    // The view control lives in the toolbar, outside the re-rendered content —
    // without this it would keep showing «Compare» while the chart is already
    // in All-clients mode.
    root.querySelectorAll("#tr-view button").forEach((button) => {
      button.setAttribute("aria-pressed", String(button.dataset.view === current.view));
    });
  }
  persist();
  renderContent(content, lastPayload);
}

function renderPicker({ sorted, excluded, manualMode, manualSet, capped }) {
  const autoSelectedIds = new Set(capped.map((item) => item.client_id));
  const total = sorted.length + excluded.length;
  const selectedCount = manualMode ? manualSet.size : capped.length;
  const capReached = manualMode && current.view !== "all" && manualSet.size >= MAX_SERIES;

  const rows = sorted.map((item) => {
    const checked = manualMode ? manualSet.has(item.client_id) : autoSelectedIds.has(item.client_id);
    const disabled = !checked && capReached;
    return `
      <li class="trends-picker__row${disabled ? " is-disabled" : ""}">
        <label>
          <input type="checkbox" value="${escapeHtml(item.client_id)}" ${checked ? "checked" : ""} ${disabled ? "disabled" : ""}>
          <span class="trends-picker__name">${escapeHtml(item.name)}</span>
          <span class="trends-picker__domain">${escapeHtml(item.domain)}</span>
        </label>
      </li>`;
  }).join("");

  const excludedRows = excluded.map((item) => `
      <li class="trends-picker__row is-disabled" title="${escapeHtml(item.reason)}">
      <label>
        <input type="checkbox" value="${escapeHtml(item.client_id)}" disabled>
        <span class="trends-picker__name">${escapeHtml(item.name)}</span>
        <span class="trends-picker__reason">not connected</span>
      </label>
    </li>`).join("");

  // The count is the thing an operator reads first, so it is a badge, not part
  // of a sentence; the mode (automatic vs hand-picked) sits next to it because
  // «8 of 14» means something different in each.
  return `
    <details class="trends-picker" id="tr-picker" ${manualMode ? "open" : ""}>
      <summary>
        <span class="trends-picker__title">Clients</span>
        <span class="trends-picker__count"><strong>${selectedCount}</strong> of ${total}</span>
        <span class="trends-picker__mode">${manualMode ? "hand-picked" : `automatic · top ${MAX_SERIES} by volume`}</span>
      </summary>
      <div class="trends-picker__body">
        <div class="trends-picker__actions">
          <button type="button" class="chip-button" id="tr-picker-all">${icon("checks", "icon icon--tiny")}Select all</button>
          <button type="button" class="chip-button" id="tr-picker-none" ${selectedCount ? "" : "disabled"}>${icon("square-off", "icon icon--tiny")}Clear</button>
          ${manualMode ? `<button type="button" class="chip-button" id="tr-picker-reset">${icon("wand", "icon icon--tiny")}Back to automatic</button>` : ""}
          <p class="trends-picker__hint">Compare and Indexed draw up to ${MAX_SERIES} lines; All clients has no limit.</p>
        </div>
        <ul class="trends-picker__list">${rows}${excludedRows}</ul>
      </div>
    </details>`;
}

function bindPickerEvents(content) {
  // "Select all" also switches the view when needed: Compare/Indexed cap the
  // chart at MAX_SERIES lines, so selecting every client while one of those is
  // active would silently drop the overflow — the one thing a control called
  // «Select all» must never do.
  content.querySelector("#tr-picker-all")?.addEventListener("click", () => selectAllClients(content));
  content.querySelector("#tr-picker-none")?.addEventListener("click", () => {
    current.clients = [];
    persist();
    renderContent(content, lastPayload);
  });
  content.querySelector("#tr-picker-reset")?.addEventListener("click", () => {
    current.clients = null;
    persist();
    renderContent(content, lastPayload);
  });
  content.querySelector(".trends-picker__list")?.addEventListener("change", (event) => {
    if (event.target.tagName !== "INPUT") return;
    const checked = [...content.querySelectorAll(".trends-picker__list input[type=checkbox]:checked")].map((cb) => cb.value);
    current.clients = checked;
    persist();
    renderContent(content, lastPayload);
  });
}

function renderTableRow(item, lastFinished) {
  const current_ = lastFinished >= 0 ? item.points[lastFinished] : null;
  const previous = lastFinished >= 1 ? item.points[lastFinished - 1] : null;
  const currentValue = current_?.value !== null && current_?.value !== undefined ? Number(current_.value) : null;
  const previousValue = previous?.value !== null && previous?.value !== undefined ? Number(previous.value) : null;
  const delta = currentValue !== null && previousValue !== null ? currentValue - previousValue : null;
  const deltaPct = delta !== null && previousValue ? (delta / previousValue) * 100 : null;
  const trend = delta === null ? "neutral" : delta >= 0 ? "good" : "bad";
  return `
    <tr>
      <td class="analytics-row__value">${escapeHtml(item.name)}<br><small class="muted">${escapeHtml(item.domain)}</small></td>
      <td>${currentValue !== null ? numberFormatter.format(currentValue) : "—"}</td>
      <td>${previousValue !== null ? numberFormatter.format(previousValue) : "—"}</td>
      <td class="delta" data-trend="${trend}">${deltaPct !== null ? `${deltaPct > 0 ? "+" : ""}${deltaPct.toFixed(1)}%` : "—"}</td>
      <td>${numberFormatter.format(totalOf(item))}</td>
      <td>${item.days_with_data} / ${item.days_expected} days</td>
      <td>${item.last_day ? escapeHtml(item.last_day) : "—"}</td>
    </tr>`;
}

function renderExcludedList(excluded) {
  if (!excluded.length) return "";
  return `
    <div class="trends-excluded">
      <h2>Not shown (${excluded.length})</h2>
      <ul>
        ${excluded.map((item) => {
          const state = STATE_META[item.state === "not_configured" ? "not_configured" : "unavailable"];
          return `
          <li>
            <span class="status-label ${stateClass(item.state === "not_configured" ? "not_configured" : "unavailable")}">${icon(state.icon)}${state.label}</span>
            <strong>${escapeHtml(item.name)}</strong>
            <span class="muted">${escapeHtml(item.reason)}</span>
          </li>`;
        }).join("")}
      </ul>
    </div>`;
}
