import { escapeHtml, icon } from "./formatters.js?v=20260803-connect2";
import { formatUaNumber } from "./client-page.js?v=20260803-connect2";

const DATASETS = {
  ga4_channels: { label: "Channels", dimension: "Channel", primary: "sessions" },
  ga4_landing_pages: { label: "Landing pages", dimension: "Landing page", primary: "sessions" },
  ga4_events: { label: "Events", dimension: "Event", primary: "events" },
  ga4_devices: { label: "Devices", dimension: "Device", primary: "sessions" },
  ga4_countries: { label: "Countries", dimension: "Country", primary: "sessions" },
};

const METRIC_LABELS = {
  users: "Users", sessions: "Sessions", conversions: "Conversions",
  engagement_rate: "Engagement", views: "Views", events: "Events",
};

let dailyChart = null;
let gbpChart = null;

export function destroyReportingNinjaChart() {
  dailyChart?.destroy();
  dailyChart = null;
  gbpChart?.destroy();
  gbpChart = null;
}

function valueMarkup(value, metric) {
  if (value === null || value === undefined) return "—";
  if (metric === "engagement_rate") {
    return `${new Intl.NumberFormat("en-US", { maximumFractionDigits: 1 }).format(Number(value))}%`;
  }
  return formatUaNumber(value);
}

function kpiMarkup(summary) {
  const metrics = summary?.metrics || [];
  if (!metrics.length) {
    return `<div class="state-panel"><p><strong>No GA4 KPI snapshot for this period.</strong><br>Run Reporting Ninja collection for this exact range.</p></div>`;
  }
  return `<div class="rn-kpi-grid">${metrics.map((metric) => {
    const delta = metric.delta_pct === null ? null : Number(metric.delta_pct);
    const trend = metric.is_improvement === true ? "good" : metric.is_improvement === false ? "bad" : "neutral";
    return `<article class="rn-kpi-card">
      <span>${escapeHtml(metric.label)}</span>
      <strong>${formatUaNumber(metric.current_value)}</strong>
      <small><span data-trend="${trend}">${delta === null ? "—" : `${delta > 0 ? "+" : ""}${delta.toFixed(1)}%`}</span> vs ${formatUaNumber(metric.previous_value)}</small>
    </article>`;
  }).join("")}</div>`;
}

function renderDailyChart(container, payload) {
  destroyReportingNinjaChart();
  const rows = payload?.rows || [];
  if (!rows.length) {
    container.innerHTML = `<div class="state-panel"><p><strong>No daily GA4 series yet.</strong><br>It will appear after the next Reporting Ninja collection.</p></div>`;
    return;
  }
  const totals = (key) => rows.reduce((sum, row) => sum + Number(row.metrics?.[key] || 0), 0);
  container.innerHTML = `<div class="chart-wrap"><canvas role="img" aria-label="Daily GA4 users, sessions and conversions"></canvas></div>
    <p class="chart-summary">${rows.length} days · ${formatUaNumber(totals("sessions"))} sessions · ${formatUaNumber(totals("conversions"))} conversions.</p>`;
  if (!window.Chart) return;
  dailyChart = new window.Chart(container.querySelector("canvas"), {
    type: "line",
    data: {
      labels: rows.map((row) => row.dimension_value.slice(5)),
      datasets: [
        { key: "users", label: "Users", color: "#ff4a17" },
        { key: "sessions", label: "Sessions", color: "#7bc7ff" },
        { key: "conversions", label: "Conversions", color: "#b9e769" },
      ].map((item) => ({
        label: item.label,
        data: rows.map((row) => Number(row.metrics?.[item.key] || 0)),
        borderColor: item.color,
        backgroundColor: item.color,
        borderWidth: 2,
        pointRadius: rows.length > 31 ? 0 : 2,
        pointHoverRadius: 4,
        tension: 0.22,
      })),
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      animation: window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ? false : undefined,
      interaction: { intersect: false, mode: "index" },
      plugins: { legend: { labels: { color: "#aaa6a0", usePointStyle: true } } },
      scales: {
        x: { grid: { color: "#2a2a2a" }, ticks: { color: "#aaa6a0", maxTicksLimit: 8 } },
        y: { grid: { color: "#2a2a2a" }, ticks: { color: "#aaa6a0" }, beginAtZero: true },
      },
    },
  });
}

function datasetTable(currentPayload, previousPayload, state) {
  const definition = DATASETS[state.ga4Dataset];
  const previous = new Map((previousPayload?.rows || []).map((row) => [row.dimension_value, row]));
  const query = state.ga4Search.trim().toLocaleLowerCase("en-US");
  const rows = (currentPayload?.rows || [])
    .filter((row) => !query || row.dimension_value.toLocaleLowerCase("en-US").includes(query));
  const metricKeys = [...new Set(rows.flatMap((row) => Object.keys(row.metrics || {})))];
  rows.sort((a, b) => Number(b.metrics?.[definition.primary] || 0) - Number(a.metrics?.[definition.primary] || 0));
  const visible = rows.slice(0, 100);
  const toolbar = `<div class="analytics-toolbar rn-toolbar">
    <label>Breakdown<select id="rn-dataset">${Object.entries(DATASETS).map(([key, item]) => `<option value="${key}" ${key === state.ga4Dataset ? "selected" : ""}>${item.label}</option>`).join("")}</select></label>
    <label class="search-field"><span class="visually-hidden">Search ${definition.label}</span>${icon("search")}<input id="rn-search" type="search" placeholder="Search ${definition.label.toLocaleLowerCase("en-US")}" value="${escapeHtml(state.ga4Search)}"></label>
  </div>`;
  if (!currentPayload?.period_start) {
    return `${toolbar}<div class="state-panel"><p><strong>No ${definition.label.toLocaleLowerCase("en-US")} for this period.</strong><br>Collect Reporting Ninja to populate this dataset.</p></div>`;
  }
  if (!visible.length) {
    return `${toolbar}<div class="state-panel"><p><strong>No matching rows.</strong></p></div>`;
  }
  return `${toolbar}<div class="table-scroll"><table class="analytics-table rn-table">
    <thead><tr><th>${definition.dimension}</th>${metricKeys.map((key) => `<th>${METRIC_LABELS[key] || escapeHtml(key)}</th>`).join("")}<th>Δ ${METRIC_LABELS[definition.primary] || definition.primary}</th></tr></thead>
    <tbody>${visible.map((row) => {
      const before = previous.get(row.dimension_value);
      const nowValue = row.metrics?.[definition.primary];
      const beforeValue = before?.metrics?.[definition.primary];
      const delta = nowValue === null || nowValue === undefined || beforeValue === null || beforeValue === undefined ? null : Number(nowValue) - Number(beforeValue);
      return `<tr><td class="analytics-row__value">${escapeHtml(row.dimension_value)}</td>${metricKeys.map((key) => `<td class="num">${valueMarkup(row.metrics?.[key], key)}</td>`).join("")}<td class="num delta" data-trend="${delta > 0 ? "good" : delta < 0 ? "bad" : "neutral"}">${delta === null ? "—" : `${delta > 0 ? "+" : ""}${formatUaNumber(delta)}`}</td></tr>`;
    }).join("")}</tbody>
  </table></div>${rows.length > visible.length ? `<p class="truncated-note">Showing the first ${visible.length} of ${rows.length} matching rows.</p>` : ""}`;
}

function sumRows(rows, key) {
  return (rows || []).reduce((sum, row) => sum + Number(row.metrics?.[key] || 0), 0);
}

function gbpMarkup(gbp) {
  const locations = gbp?.locations?.rows || [];
  const searchTerms = gbp?.searchTerms?.rows || [];
  const reviews = gbp?.reviews?.rows || [];
  if (!locations.length && !searchTerms.length && !reviews.length) return "";
  const reviewCount = sumRows(reviews, "reviews");
  const weightedRating = reviewCount
    ? reviews.reduce((sum, row) => sum + Number(row.metrics?.average_rating || 0) * Number(row.metrics?.reviews || 0), 0) / reviewCount
    : null;
  const cards = [
    ["Impressions", sumRows(locations, "impressions")],
    ["Interactions", sumRows(locations, "interactions")],
    ["Calls", sumRows(locations, "calls")],
    ["Directions", sumRows(locations, "directions")],
    ["Website clicks", sumRows(locations, "website_clicks")],
    ["Reviews", reviewCount],
    ["Average rating", weightedRating === null ? null : weightedRating.toFixed(2)],
  ];
  return `<section class="rn-gbp">
    <div class="section-heading"><div><p class="eyebrow">Reporting Ninja · Business Profile</p><h2>Local visibility</h2></div><span class="status-label status-complete">${locations.length} locations</span></div>
    <div class="rn-kpi-grid rn-kpi-grid--gbp">${cards.map(([label, value]) => `<article class="rn-kpi-card"><span>${label}</span><strong>${formatUaNumber(value)}</strong></article>`).join("")}</div>
    <div class="rn-chart" id="rn-gbp-chart"></div>
    <div class="rn-gbp-grid">
      <div><h3>Location performance</h3><div class="table-scroll"><table class="analytics-table rn-table"><thead><tr><th>Location</th><th>Impressions</th><th>Interactions</th><th>Calls</th><th>Directions</th><th>Website clicks</th></tr></thead><tbody>${locations.map((row) => `<tr><td class="analytics-row__value">${escapeHtml(row.dimension_value)}</td>${["impressions", "interactions", "calls", "directions", "website_clicks"].map((key) => `<td class="num">${formatUaNumber(row.metrics?.[key])}</td>`).join("")}</tr>`).join("")}</tbody></table></div></div>
      <div><h3>Top search terms</h3><p class="history-note">Google publishes Business Profile search terms monthly; ReportNinja rounds this range to full months.</p><ol class="rn-term-list">${searchTerms.slice(0, 12).map((row) => `<li><span>${escapeHtml(row.dimension_value)}</span><strong>${formatUaNumber(row.metrics?.impressions)}</strong></li>`).join("")}</ol></div>
    </div>
  </section>`;
}

function renderGbpChart(container, payload) {
  const rows = payload?.rows || [];
  if (!container || !rows.length) {
    if (container) container.innerHTML = `<div class="state-panel"><p><strong>No daily Business Profile series for this range.</strong></p></div>`;
    return;
  }
  container.innerHTML = `<div class="chart-wrap"><canvas role="img" aria-label="Daily Business Profile impressions and interactions"></canvas></div>`;
  if (!window.Chart) return;
  gbpChart = new window.Chart(container.querySelector("canvas"), {
    type: "bar",
    data: {
      labels: rows.map((row) => row.dimension_value.slice(5)),
      datasets: [
        { key: "impressions", label: "Impressions", color: "#7bc7ff" },
        { key: "interactions", label: "Interactions", color: "#ff4a17" },
      ].map((item) => ({ label: item.label, data: rows.map((row) => Number(row.metrics?.[item.key] || 0)), backgroundColor: item.color, borderColor: item.color })),
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      animation: window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ? false : undefined,
      plugins: { legend: { labels: { color: "#aaa6a0", usePointStyle: true } } },
      scales: {
        x: { grid: { display: false }, ticks: { color: "#aaa6a0" } },
        y: { beginAtZero: true, grid: { color: "#2a2a2a" }, ticks: { color: "#aaa6a0" } },
      },
    },
  });
}

export function renderReportingNinjaPanel(container, {
  summary, daily, currentDataset, previousDataset, gbp, state, onChange,
}) {
  container.innerHTML = `<div class="section-heading"><div><p class="eyebrow">Reporting Ninja · GA4</p><h2>Traffic and conversions</h2></div><span class="status-label status-${summary?.state || "unavailable"}">${escapeHtml(summary?.state || "no data")}</span></div>
    ${kpiMarkup(summary)}
    <div class="rn-chart" id="rn-daily-chart"></div>
    <div id="rn-dataset-table">${datasetTable(currentDataset, previousDataset, state)}</div>
    ${gbpMarkup(gbp)}`;
  renderDailyChart(container.querySelector("#rn-daily-chart"), daily);
  renderGbpChart(container.querySelector("#rn-gbp-chart"), gbp?.daily);
  container.querySelector("#rn-dataset")?.addEventListener("change", (event) => onChange({ ga4Dataset: event.target.value, ga4Search: "" }));
  let timer = null;
  container.querySelector("#rn-search")?.addEventListener("input", (event) => {
    clearTimeout(timer);
    const value = event.target.value;
    timer = setTimeout(() => onChange({ ga4Search: value }, { reload: false }), 200);
  });
}

export function renderReportingNinjaError(container, message) {
  destroyReportingNinjaChart();
  container.innerHTML = `<div class="section-heading"><div><p class="eyebrow">Reporting Ninja · GA4</p><h2>Traffic and conversions</h2></div></div><div class="state-panel state-failed">${icon("wifi-off")}<p><strong>Failed to load GA4 analytics.</strong><br>${escapeHtml(message)}</p></div>`;
}
