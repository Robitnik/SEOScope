// Portfolio Trends (task-28): one line per client across the whole
// portfolio for a single daily metric (GSC clicks/impressions, GA4
// sessions/users). See docs/dev/done/task-28-portfolio-trends-page.md.
//
// The page never triggers collection — it only reads whatever day-level
// history the routine sync (or the `backfill-daily-history` CLI) already
// stored. An empty result is a normal, well-explained state (§5), not a
// failure.

import { getPortfolioSeries, isAbortError } from "./api.js?v=20260727-trends1";
import { buildTrendsHash, replaceTrendsHash } from "./router.js?v=20260727-trends1";
import { STATE_META, escapeHtml, icon, stateClass } from "./formatters.js?v=20260727-trends1";
import { sectionSkeleton } from "./skeleton.js?v=20260727-trends1";
import { filterByClientGroup } from "./client-groups.js?v=20260727-trends1";
import {
  MAX_SERIES,
  buildSummary,
  colorAllocator,
  destroySparklines,
  destroyTrendsChart,
  renderLineChart,
  renderSparklineGrid,
} from "./trends-chart.js?v=20260727-trends1";

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
const RANGES = [30, 90, 180, 365];
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

function bindControls() {
  const groups = [
    ["tr-metric", "metric"], ["tr-bucket", "bucket"], ["tr-days", "days"],
    ["tr-group", "group"], ["tr-view", "view"],
  ];
  groups.forEach(([id, key]) => {
    root.querySelector(`#${id}`).addEventListener("click", (event) => {
      const button = event.target.closest("button");
      if (!button) return;
      const value = button.dataset[key];
      const parsed = key === "days" ? Number(value) : value;
      if (parsed === current[key]) return;
      current[key] = parsed;
      persist();
      root.querySelectorAll(`#${id} button`).forEach((btn) => btn.setAttribute("aria-pressed", String(btn.dataset[key] === value)));
      void load();
    });
  });
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

async function load() {
  const token = ++requestToken;
  const content = root.querySelector("#tr-content");
  content.setAttribute("aria-busy", "true");
  content.innerHTML = sectionSkeleton({ label: "portfolio trends", chart: true, rows: 6, columns: 5 });
  destroyTrendsChart();
  destroySparklines();

  try {
    const payload = await getPortfolioSeries({ metric: current.metric, bucket: current.bucket, days: current.days });
    if (token !== requestToken) return;
    content.setAttribute("aria-busy", "false");
    lastPayload = payload;
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
    <li class="trends-picker__row is-disabled">
      <label>
        <input type="checkbox" value="${escapeHtml(item.client_id)}" disabled>
        <span class="trends-picker__name">${escapeHtml(item.name)}</span>
        <span class="trends-picker__reason">${escapeHtml(item.reason)}</span>
      </label>
    </li>`).join("");

  return `
    <details class="trends-picker" id="tr-picker" ${manualMode ? "open" : ""}>
      <summary>Clients (${selectedCount} / ${total} shown)</summary>
      <div class="trends-picker__body">
        <p class="trends-picker__hint">Pick specific clients for Compare/Indexed (up to ${MAX_SERIES}) — All clients has no limit.</p>
        ${manualMode ? `<button type="button" class="link-button" id="tr-picker-reset">Reset to automatic (top ${MAX_SERIES} by volume)</button>` : ""}
        <ul class="trends-picker__list">${rows}${excludedRows}</ul>
      </div>
    </details>`;
}

function bindPickerEvents(content) {
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
