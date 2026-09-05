// URL Inspector (task-61) — reached from the header search field on every
// route, not a nav tab. One operator-pasted page URL -> everything the
// system knows about it: GSC/GA4 series scoped to that URL, its Web Archive
// timeline, Site Monitor state/changes, meta + copy QA, and an on-demand
// live micro technical audit. See doc/dev/new/task-61-url-inspector.md.
//
// Two calls, deliberately not one (mirrors the backend split): stored data
// renders immediately from `getUrlInspector`; the live check is a separate,
// slow, opt-in `runUrlInspectorLive` the operator triggers explicitly.
import { getUrlInspector, runUrlInspectorLive, isAbortError } from "./api.js?v=20260905-t61c";
import { replaceUrlInspectorHash } from "./router.js?v=20260905-t61c";
import { escapeHtml, formatDateRange, formatTimestamp, icon } from "./formatters.js?v=20260821-spin3";
import { sectionSkeleton } from "./skeleton.js?v=20260821-spin3";

const root = document.querySelector("#url-inspector-page-root");

const GRANULARITIES = [
  { key: "week", label: "Weekly" },
  { key: "month", label: "Monthly" },
  { key: "quarter", label: "Quarterly" },
];

const GSC_METRICS = [
  { key: "clicks", label: "Clicks" },
  { key: "impressions", label: "Impressions" },
  { key: "ctr", label: "CTR" },
  { key: "position", label: "Position" },
];

const GA4_METRICS = [
  { key: "sessions", label: "Sessions" },
  { key: "users", label: "Users" },
  { key: "conversions", label: "Conversions" },
  { key: "views", label: "Views" },
  { key: "engagement_rate", label: "Engagement rate" },
];

// This page's own small state vocabulary — distinct from `STATE_META`
// (formatters.js) since none of `has_data`/`no_data`/`unknown_client` exist
// there, and formatters.js already carries unrelated uncommitted changes
// from another session (kept fully self-contained deliberately).
const SOURCE_STATE_META = {
  has_data: { label: "Data available", icon: "circle-check", tone: "ui-tone-success" },
  no_data: { label: "No data for this URL", icon: "cloud-off", tone: "ui-tone-dim" },
  not_configured: { label: "Not connected", icon: "plug-off", tone: "ui-tone-dim" },
  failed: { label: "Collection error", icon: "circle-x", tone: "ui-tone-danger" },
  unknown_client: { label: "Unknown client", icon: "help-circle", tone: "ui-tone-dim" },
};

const SOURCE_LABELS = {
  gsc: "Search Console",
  reporting_ninja: "GA4",
  monitor: "Site Monitor",
  archive: "Web Archive",
  page_inventory: "Meta & Copy",
  tech_audit: "Technical Audit",
  client: "Client",
};

const SEVERITY_META = {
  critical: { label: "Critical", tone: "ui-tone-danger" },
  error: { label: "Error", tone: "ui-tone-danger" },
  warning: { label: "Warning", tone: "ui-tone-warning" },
  warn: { label: "Warning", tone: "ui-tone-warning" },
  info: { label: "Info", tone: "ui-tone-info" },
};

const numberFormatter = new Intl.NumberFormat("en-US", { maximumFractionDigits: 2 });
const shortDay = new Intl.DateTimeFormat("en-US", { day: "numeric", month: "short" });

let current = null; // { url, granularity, gscMetric, ga4Metric }
let dto = null;
let loadError = null;
let loading = false;
let requestToken = 0;
let gscChart = null;
let ga4Chart = null;
let liveState = { loading: false, error: null, result: null, withSchema: false };

export async function initUrlInspectorPage(query) {
  const url = query.get("u") || "";
  const granularityParam = query.get("g");
  const granularity = GRANULARITIES.some((g) => g.key === granularityParam) ? granularityParam : "week";
  current = { url, granularity, gscMetric: "clicks", ga4Metric: "sessions" };
  dto = null;
  loadError = null;
  loading = false;
  liveState = { loading: false, error: null, result: null, withSchema: false };
  destroyCharts();

  if (!url) {
    root.innerHTML = emptyShell();
    return;
  }

  root.innerHTML = shell();
  wireControls();
  await load();
  // Runs once per URL, right after the stored data is in — the operator
  // asked for the live check to happen automatically rather than wait for
  // a click. Skips silently if the initial load itself failed (loadError
  // set) or the URL changed again before this resolved (token guard inside
  // `load()` already handles the latter for `dto`/`render()`).
  if (dto && !liveState.result && !liveState.loading) {
    await runLiveCheck({ withSchema: false });
  }
}

function destroyCharts() {
  gscChart?.destroy();
  ga4Chart?.destroy();
  gscChart = null;
  ga4Chart = null;
}

function emptyShell() {
  return `
    <div class="state-panel">
      <div>${icon("search")}</div>
      <div>
        <p><strong>Paste a page URL in the search field above to inspect it.</strong></p>
        <p>Search analytics, Web Archive history, Site Monitor changes, meta/copy QA, and a live technical check — all for one URL.</p>
      </div>
    </div>`;
}

function shell() {
  return `
    <header class="url-inspector__header" id="ui-header" aria-live="polite" aria-busy="true">
      ${sectionSkeleton({ label: "the URL", heading: false, status: true })}
    </header>
    <section class="url-inspector__body" id="ui-body" aria-live="polite" aria-busy="true">
      ${sectionSkeleton({ label: "this URL's data", rows: 6, columns: 3, chart: true })}
    </section>`;
}

async function load() {
  const token = ++requestToken;
  loading = true;
  loadError = null;
  try {
    const result = await getUrlInspector(current.url, { granularity: current.granularity });
    if (token !== requestToken) return;
    dto = result;
  } catch (error) {
    if (isAbortError(error)) return;
    if (token !== requestToken) return;
    loadError = error.message || "Could not load this URL.";
    dto = null;
  } finally {
    if (token === requestToken) loading = false;
  }
  render();
}

function render() {
  const header = document.querySelector("#ui-header");
  const body = document.querySelector("#ui-body");
  if (!header || !body) return;

  header.removeAttribute("aria-busy");
  body.removeAttribute("aria-busy");

  if (loadError) {
    header.innerHTML = headerShell(null);
    body.innerHTML = `
      <div class="state-panel">
        <div>${icon("alert-triangle")}</div>
        <div><p><strong>Could not load this URL.</strong><br>${escapeHtml(loadError)}</p></div>
      </div>`;
    return;
  }
  if (!dto) return;

  header.innerHTML = headerShell(dto);
  body.innerHTML = [
    renderSearchAnalytics(dto),
    renderArchive(dto),
    renderMonitor(dto),
    renderMeta(dto),
    renderTechAudit(dto),
    renderLive(),
  ].join("");

  requestAnimationFrame(() => {
    renderGscChart();
    renderGa4Chart();
  });
}

// --- header: identity + coverage strip --------------------------------------

function headerShell(data) {
  const client = data?.client;
  const identity = client
    ? `
      <div class="url-inspector__client">
        ${client.logo_path ? `<img class="url-inspector__logo" src="${escapeHtml(client.logo_path)}" alt="" width="32" height="32">` : ""}
        <div>
          <strong>${escapeHtml(client.name)}</strong>
          <small>${client.match === "subdomain" ? "matched by subdomain" : "matched by domain"} · ${escapeHtml(client.domain)}</small>
        </div>
      </div>`
    : `<div class="url-inspector__client url-inspector__client--unknown">${icon("help-circle")}<span>No client matches this URL's host</span></div>`;

  return `
    <a class="back-link" href="#/">${icon("arrow-left")}<span>Back to dashboard</span></a>
    <div class="url-inspector__identity">
      <p class="eyebrow">URL Inspector</p>
      <p class="url-inspector__url" translate="no">${escapeHtml(data ? data.input_url : current.url)}</p>
      ${identity}
    </div>
    ${data ? renderCoverageChips(data.coverage) : ""}`;
}

function renderCoverageChips(coverage) {
  return `
    <ul class="url-inspector__coverage" aria-label="Data sources for this URL">
      ${coverage.map((item) => {
        const meta = SOURCE_STATE_META[item.state] || SOURCE_STATE_META.no_data;
        const label = SOURCE_LABELS[item.source] || item.source;
        return `
          <li class="url-inspector__chip ${meta.tone}" title="${escapeHtml(item.detail || meta.label)}">
            ${icon(meta.icon, "icon icon--tiny")}<span>${escapeHtml(label)}</span>
          </li>`;
      }).join("")}
    </ul>`;
}

// --- search analytics: GSC + GA4 series -------------------------------------

function renderSearchAnalytics(data) {
  const { search_analytics: sa } = data;
  return `
    <section class="url-inspector__section" aria-labelledby="ui-sa-heading">
      <div class="url-inspector__section-head">
        <h2 id="ui-sa-heading">Search analytics</h2>
        <div class="url-inspector__granularity" role="group" aria-label="Chart granularity">
          ${GRANULARITIES.map((g) => `
            <button type="button" data-action="granularity" data-value="${g.key}" aria-pressed="${current.granularity === g.key}">${g.label}</button>
          `).join("")}
        </div>
      </div>
      ${renderSeriesBlock({
        id: "gsc", title: "Search Console", state: sa.gsc, series: sa.gsc_series,
        metrics: GSC_METRICS, selected: current.gscMetric,
      })}
      ${renderSeriesBlock({
        id: "ga4", title: "GA4 (Reporting Ninja)", state: sa.reporting_ninja, series: sa.ga4_series,
        metrics: GA4_METRICS, selected: current.ga4Metric,
      })}
    </section>`;
}

function renderSeriesBlock({ id, title, state, series, metrics, selected }) {
  const meta = SOURCE_STATE_META[state.state] || SOURCE_STATE_META.no_data;
  if (state.state !== "has_data") {
    return `
      <div class="url-inspector__series" data-series="${id}">
        <h3>${escapeHtml(title)}</h3>
        <div class="state-panel state-panel--compact ${meta.tone}">
          ${icon(meta.icon)}<p>${escapeHtml(state.detail || meta.label)}</p>
        </div>
      </div>`;
  }

  const activeSeries = series[selected];
  const points = activeSeries?.points || [];
  const otherLength = activeSeries?.other_length_periods || 0;

  return `
    <div class="url-inspector__series" data-series="${id}">
      <div class="url-inspector__series-head">
        <h3>${escapeHtml(title)}</h3>
        <div class="url-inspector__metric-picker" role="group" aria-label="${escapeHtml(title)} metric">
          ${metrics.map((m) => `
            <button type="button" data-action="metric" data-series="${id}" data-value="${m.key}" aria-pressed="${selected === m.key}">${m.label}</button>
          `).join("")}
        </div>
      </div>
      ${points.length === 0
        ? `<div class="state-panel state-panel--compact"><div>${icon("cloud-off")}</div><p>No ${current.granularity}ly periods stored for this URL yet.</p></div>`
        : `
          <div class="chart-wrap url-inspector__chart-wrap">
            <canvas id="ui-${id}-canvas" role="img" aria-label="Line chart of ${escapeHtml(selected)} for this URL"></canvas>
          </div>
          ${otherLength ? `<p class="url-inspector__note">${otherLength} more period(s) of a different length exist and are not shown here (never averaged across period lengths).</p>` : ""}
          ${renderSeriesTable(points)}
        `}
    </div>`;
}

function renderSeriesTable(points) {
  const latest = points.at(-1);
  const previous = points.at(-2);
  if (!latest) return "";
  const rows = [latest, previous].filter(Boolean);
  return `
    <div class="url-inspector__table-scroll">
      <table class="metric-table url-inspector__series-table">
        <thead><tr><th>Period</th><th>Value</th></tr></thead>
        <tbody>
          ${rows.map((point) => `
            <tr>
              <td>${escapeHtml(formatDateRange(point.period_start, point.period_end))}</td>
              <td class="${point.value === null ? "metric-null" : ""}">${formatSeriesValue(point)}</td>
            </tr>`).join("")}
        </tbody>
      </table>
    </div>`;
}

function formatSeriesValue(point) {
  if (point.value === null) {
    return point.absence_reason === "truncated"
      ? "Unknown (outside top-N)"
      : "No data";
  }
  return numberFormatter.format(Number(point.value));
}

function chartDataset(points) {
  return {
    borderColor: "#ff4a17",
    backgroundColor: "#ff4a17",
    borderWidth: 2,
    pointRadius: 3,
    pointHoverRadius: 5,
    spanGaps: false,
    data: points.map((p) => (p.value === null ? null : Number(p.value))),
  };
}

function chartLabels(points) {
  return points.map((p) => {
    const start = new Date(`${p.period_start}T12:00:00`);
    const end = new Date(`${p.period_end}T12:00:00`);
    return p.period_start === p.period_end ? shortDay.format(start) : `${start.getDate()}–${shortDay.format(end)}`;
  });
}

function chartOptions(points) {
  return {
    responsive: true,
    maintainAspectRatio: false,
    animation: window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ? false : undefined,
    interaction: { intersect: false, mode: "index" },
    plugins: {
      legend: { display: false },
      tooltip: {
        callbacks: {
          label: (ctx) => {
            const point = points[ctx.dataIndex];
            if (point.value === null) {
              return point.absence_reason === "truncated" ? "Unknown (outside top-N)" : "No data";
            }
            return numberFormatter.format(Number(point.value));
          },
        },
      },
    },
    scales: {
      x: { grid: { color: "#2a2a2a" }, ticks: { color: "#aaa6a0", maxRotation: 0, autoSkip: true, maxTicksLimit: 8 } },
      y: { grid: { color: "#2a2a2a" }, ticks: { color: "#aaa6a0" } },
    },
  };
}

function renderGscChart() {
  gscChart?.destroy();
  gscChart = null;
  const canvas = document.querySelector("#ui-gsc-canvas");
  const points = dto?.search_analytics?.gsc_series?.[current.gscMetric]?.points;
  if (!canvas || !points?.length || !window.Chart) return;
  gscChart = new window.Chart(canvas, {
    type: "line",
    data: { labels: chartLabels(points), datasets: [chartDataset(points)] },
    options: chartOptions(points),
  });
}

function renderGa4Chart() {
  ga4Chart?.destroy();
  ga4Chart = null;
  const canvas = document.querySelector("#ui-ga4-canvas");
  const points = dto?.search_analytics?.ga4_series?.[current.ga4Metric]?.points;
  if (!canvas || !points?.length || !window.Chart) return;
  ga4Chart = new window.Chart(canvas, {
    type: "line",
    data: { labels: chartLabels(points), datasets: [chartDataset(points)] },
    options: chartOptions(points),
  });
}

// --- Web Archive -------------------------------------------------------------

function renderArchive(data) {
  const { archive } = data;
  const meta = SOURCE_STATE_META[archive.state.state] || SOURCE_STATE_META.no_data;
  return `
    <section class="url-inspector__section" aria-labelledby="ui-archive-heading">
      <h2 id="ui-archive-heading">Web Archive</h2>
      ${archive.state.state !== "has_data"
        ? `<div class="state-panel state-panel--compact ${meta.tone}">${icon(meta.icon)}<p>${escapeHtml(archive.state.detail || meta.label)}</p></div>`
        : archive.snapshots.length === 0
          ? `<div class="state-panel state-panel--compact"><div>${icon("cloud-off")}</div><p>Tracked, but no snapshot has been captured yet.</p></div>`
          : `
            <ul class="url-inspector__list">
              ${archive.snapshots.map((s) => `
                <li class="url-inspector__row">
                  <span class="url-inspector__row-time">${escapeHtml(formatTimestamp(s.observed_at))}</span>
                  <span class="url-inspector__row-main">
                    ${s.title ? escapeHtml(s.title) : "<em>Untitled</em>"}
                    <small>${s.http_status ?? "—"} · ${escapeHtml(s.store_reason)}</small>
                  </span>
                  ${s.has_change ? `<span class="url-inspector__badge ui-tone-accent">${icon("history", "icon icon--tiny")} Changed</span>` : ""}
                  <a class="button button--quiet button--small" href="#/archive/view?snapshot=${encodeURIComponent(s.id)}">${icon("external-link", "icon icon--tiny")} View</a>
                </li>`).join("")}
            </ul>
            ${archive.snapshot_count > archive.snapshots.length ? `<p class="url-inspector__note">Showing ${archive.snapshots.length} of ${archive.snapshot_count} snapshots.</p>` : ""}
          `}
    </section>`;
}

// --- Site Monitor ------------------------------------------------------------

function renderMonitor(data) {
  const { monitor } = data;
  const meta = SOURCE_STATE_META[monitor.state.state] || SOURCE_STATE_META.no_data;
  if (monitor.state.state !== "has_data") {
    return `
      <section class="url-inspector__section" aria-labelledby="ui-monitor-heading">
        <h2 id="ui-monitor-heading">Site Monitor</h2>
        <div class="state-panel state-panel--compact ${meta.tone}">${icon(meta.icon)}<p>${escapeHtml(monitor.state.detail || meta.label)}</p></div>
      </section>`;
  }

  return `
    <section class="url-inspector__section" aria-labelledby="ui-monitor-heading">
      <h2 id="ui-monitor-heading">Site Monitor</h2>
      <div class="url-inspector__table-scroll">
        <table class="metric-table url-inspector__facts">
          <tbody>
            <tr><td>Status</td><td>${monitor.status ?? "—"}</td></tr>
            <tr><td>Indexable</td><td>${formatBool(monitor.indexable)}</td></tr>
            <tr><td>Canonical</td><td class="url-inspector__wrap">${monitor.canonical ? escapeHtml(monitor.canonical) : "—"}</td></tr>
            <tr><td>Last checked</td><td>${escapeHtml(formatTimestamp(monitor.last_checked_at))}</td></tr>
            ${monitor.gone_since ? `<tr><td>Gone since</td><td>${escapeHtml(formatTimestamp(monitor.gone_since))}</td></tr>` : ""}
          </tbody>
        </table>
      </div>
      <h3>Changes ${monitor.changes_total ? `(${monitor.changes.length} of ${monitor.changes_total})` : ""}</h3>
      ${monitor.changes.length === 0
        ? `<p class="url-inspector__note">No changes recorded for this URL.</p>`
        : `<ul class="url-inspector__list">
            ${monitor.changes.map((c) => renderChangeRow(c)).join("")}
          </ul>`}
      <h3>Versions ${monitor.versions_total ? `(${monitor.versions.length} of ${monitor.versions_total})` : ""}</h3>
      ${monitor.versions.length === 0
        ? `<p class="url-inspector__note">No versions recorded.</p>`
        : `<ul class="url-inspector__list url-inspector__list--compact">
            ${monitor.versions.map((v) => `
              <li class="url-inspector__row">
                <span class="url-inspector__row-time">${escapeHtml(formatTimestamp(v.observed_at))}</span>
                <span class="url-inspector__row-main">HTTP ${v.http_status ?? "—"}${v.changed_fields.length ? ` · ${v.changed_fields.map(escapeHtml).join(", ")}` : ""}</span>
              </li>`).join("")}
          </ul>`}
    </section>`;
}

function renderChangeRow(change) {
  const severity = SEVERITY_META[change.severity] || SEVERITY_META.info;
  return `
    <li class="url-inspector__row ${severity.tone}">
      <span class="url-inspector__badge">${escapeHtml(severity.label)}</span>
      <span class="url-inspector__row-main">
        ${escapeHtml(change.change_type)}
        ${change.old_value || change.new_value
          ? `<small>${change.old_value ? `<span class="url-inspector__old">${escapeHtml(change.old_value)}</span> → ` : ""}${change.new_value ? escapeHtml(change.new_value) : ""}</small>`
          : ""}
      </span>
      <span class="url-inspector__row-time">${escapeHtml(formatTimestamp(change.detected_at))}</span>
    </li>`;
}

// --- Meta & copy -------------------------------------------------------------

function renderMeta(data) {
  const { meta: metaData } = data;
  const meta = SOURCE_STATE_META[metaData.state.state] || SOURCE_STATE_META.no_data;
  if (metaData.state.state !== "has_data") {
    return `
      <section class="url-inspector__section" aria-labelledby="ui-meta-heading">
        <h2 id="ui-meta-heading">Meta &amp; copy</h2>
        <div class="state-panel state-panel--compact ${meta.tone}">${icon(meta.icon)}<p>${escapeHtml(metaData.state.detail || meta.label)}</p></div>
      </section>`;
  }

  return `
    <section class="url-inspector__section" aria-labelledby="ui-meta-heading">
      <h2 id="ui-meta-heading">Meta &amp; copy <small class="url-inspector__source-tag">from ${metaData.meta_source === "page_inventory" ? "Page Inventory" : "Site Monitor"}</small></h2>
      <div class="url-inspector__table-scroll">
        <table class="metric-table url-inspector__facts">
          <tbody>
            <tr><td>Title</td><td class="url-inspector__wrap">${metaData.title ? escapeHtml(metaData.title) : "<em>Missing</em>"}</td></tr>
            <tr><td>Description</td><td class="url-inspector__wrap">${metaData.description ? escapeHtml(metaData.description) : "<em>Missing</em>"}</td></tr>
            <tr><td>H1</td><td class="url-inspector__wrap">${metaData.h1 ? escapeHtml(metaData.h1) : "<em>Missing</em>"}</td></tr>
            ${metaData.keywords.length ? `<tr><td>Keywords</td><td class="url-inspector__wrap">${metaData.keywords.map(escapeHtml).join(", ")}</td></tr>` : ""}
          </tbody>
        </table>
      </div>
      ${renderFindings(metaData.findings)}
    </section>`;
}

function renderFindings(findings) {
  if (!findings.length) return `<p class="url-inspector__note">No QA findings.</p>`;
  return `
    <ul class="url-inspector__list url-inspector__list--compact">
      ${findings.map((f) => {
        const severity = SEVERITY_META[f.severity] || SEVERITY_META.info;
        return `
          <li class="url-inspector__row ${severity.tone}">
            <span class="url-inspector__badge">${escapeHtml(severity.label)}</span>
            <span class="url-inspector__row-main">${escapeHtml(f.message)}<small>${escapeHtml(f.field)} · ${escapeHtml(f.check)}</small></span>
          </li>`;
      }).join("")}
    </ul>`;
}

// --- Technical audit ---------------------------------------------------------

function renderTechAudit(data) {
  const { tech_audit: audit } = data;
  const meta = SOURCE_STATE_META[audit.state.state] || SOURCE_STATE_META.no_data;
  if (audit.state.state !== "has_data") {
    return `
      <section class="url-inspector__section" aria-labelledby="ui-audit-heading">
        <h2 id="ui-audit-heading">Technical audit</h2>
        <div class="state-panel state-panel--compact ${meta.tone}">${icon(meta.icon)}<p>${escapeHtml(audit.state.detail || meta.label)}</p></div>
      </section>`;
  }
  return `
    <section class="url-inspector__section" aria-labelledby="ui-audit-heading">
      <h2 id="ui-audit-heading">Technical audit${audit.audit_date ? ` <small class="url-inspector__source-tag">as of ${escapeHtml(audit.audit_date)}</small>` : ""}</h2>
      ${audit.findings.length === 0
        ? `<p class="url-inspector__note">No open findings for this URL in the latest crawl.</p>`
        : `<ul class="url-inspector__list url-inspector__list--compact">
            ${audit.findings.map((f) => {
              const severity = SEVERITY_META[(f.severity || "").toLowerCase()] || SEVERITY_META.info;
              return `
                <li class="url-inspector__row ${severity.tone}">
                  <span class="url-inspector__badge">${escapeHtml(f.severity || "—")}</span>
                  <span class="url-inspector__row-main">
                    ${f.category ? escapeHtml(f.category) : escapeHtml(f.check)}
                    ${f.detail ? `<small>${escapeHtml(f.detail)}</small>` : ""}
                  </span>
                </li>`;
            }).join("")}
          </ul>`}
    </section>`;
}

// --- live micro technical audit ----------------------------------------------

function renderLive() {
  return `
    <section class="url-inspector__section url-inspector__live" aria-labelledby="ui-live-heading">
      <div class="url-inspector__section-head">
        <h2 id="ui-live-heading">Live audit</h2>
        <div class="url-inspector__live-controls">
          <label class="url-inspector__checkbox">
            <input type="checkbox" id="ui-live-schema" ${liveState.withSchema ? "checked" : ""}>
            <span>Also check schema markup</span>
          </label>
          <button type="button" class="button button--quiet button--small" data-action="run-live" ${liveState.loading ? "disabled" : ""}>
            ${icon(liveState.loading ? "loader-2" : "refresh", liveState.loading ? "icon icon--tiny icon--spin" : "icon icon--tiny")}
            ${liveState.loading ? "Checking…" : "Recheck"}
          </button>
        </div>
      </div>
      <div id="ui-live-result">${renderLiveResult()}</div>
    </section>`;
}

function renderLiveResult() {
  if (liveState.loading) {
    return `<p class="url-inspector__note">Fetching the live page — this can take up to 20s on a slow or protected site.</p>`;
  }
  if (liveState.error) {
    return `<div class="state-panel state-panel--compact ui-tone-danger">${icon("alert-triangle")}<p>${escapeHtml(liveState.error)}</p></div>`;
  }
  const result = liveState.result;
  if (!result) return `<p class="url-inspector__note">Not run yet.</p>`;

  if (result.verdict === "blocked") {
    return `
      <div class="state-panel state-panel--compact ui-tone-warning">
        ${icon("shield-off")}
        <p><strong>Blocked by the site's bot protection.</strong>${result.error ? ` ${escapeHtml(result.error)}` : ""}</p>
      </div>
      ${renderHopChain(result.redirect_chain)}`;
  }
  if (result.verdict !== "ok") {
    return `
      <div class="state-panel state-panel--compact ui-tone-danger">
        ${icon("circle-x")}
        <p><strong>Live check failed (${escapeHtml(result.verdict)}).</strong>${result.error ? ` ${escapeHtml(result.error)}` : ""}</p>
      </div>`;
  }

  const s = result.signals;
  return `
    ${renderHopChain(result.redirect_chain)}
    <div class="url-inspector__table-scroll">
      <table class="metric-table url-inspector__facts">
        <tbody>
          <tr><td>HTTP status</td><td>${result.http_status ?? "—"}</td></tr>
          <tr><td>Indexable</td><td>${formatBool(result.indexable)}</td></tr>
          <tr><td>Title</td><td class="url-inspector__wrap">${s?.title ? escapeHtml(s.title) : "<em>Missing</em>"}</td></tr>
          <tr><td>Description</td><td class="url-inspector__wrap">${s?.description ? escapeHtml(s.description) : "<em>Missing</em>"}</td></tr>
          <tr><td>H1</td><td class="url-inspector__wrap">${s?.h1 ? escapeHtml(s.h1) : "<em>Missing</em>"}</td></tr>
          <tr><td>Canonical</td><td class="url-inspector__wrap">${s?.canonical ? escapeHtml(s.canonical) : "—"}</td></tr>
          <tr><td>Meta robots</td><td>${s?.meta_robots ? escapeHtml(s.meta_robots) : "—"}</td></tr>
          <tr><td>Word count</td><td>${s?.word_count ?? "—"}</td></tr>
          ${s?.json_ld_types?.length ? `<tr><td>JSON-LD types</td><td class="url-inspector__wrap">${s.json_ld_types.map(escapeHtml).join(", ")}</td></tr>` : ""}
        </tbody>
      </table>
    </div>
    ${renderFindings(result.findings)}
    ${result.schema_check_id
      ? `<p class="url-inspector__note">${icon("shield-check", "icon icon--tiny")} Schema markup check saved — <a href="#/tools/schema-markup/${encodeURIComponent(result.schema_check_id)}">view full result</a>.</p>`
      : ""}`;
}

function renderHopChain(hops) {
  if (!hops?.length) return "";
  return `
    <ol class="url-inspector__hops">
      ${hops.map((h) => `<li><span>${h.status}</span><span class="url-inspector__wrap" translate="no">${escapeHtml(h.url)}</span></li>`).join("")}
    </ol>`;
}

function formatBool(value) {
  if (value === null || value === undefined) return "<em>Unknown</em>";
  return value ? "Yes" : "No";
}

// --- wiring -------------------------------------------------------------

async function runLiveCheck({ withSchema }) {
  liveState = { ...liveState, loading: true, error: null, withSchema };
  renderLiveInPlace();
  try {
    const result = await runUrlInspectorLive(current.url, { withSchema });
    liveState = { ...liveState, loading: false, result };
  } catch (error) {
    if (isAbortError(error)) return;
    liveState = { ...liveState, loading: false, error: error.message || "Live check failed." };
  }
  renderLiveInPlace();
}

function wireControls() {
  root.addEventListener("click", async (event) => {
    const granularityButton = event.target.closest('[data-action="granularity"]');
    if (granularityButton) {
      current.granularity = granularityButton.dataset.value;
      replaceUrlInspectorHash({ u: current.url, g: current.granularity });
      root.querySelector("#ui-header")?.setAttribute("aria-busy", "true");
      root.querySelector("#ui-body")?.setAttribute("aria-busy", "true");
      await load();
      return;
    }

    const metricButton = event.target.closest('[data-action="metric"]');
    if (metricButton) {
      const seriesId = metricButton.dataset.series;
      if (seriesId === "gsc") current.gscMetric = metricButton.dataset.value;
      if (seriesId === "ga4") current.ga4Metric = metricButton.dataset.value;
      render();
      return;
    }

    const liveButton = event.target.closest('[data-action="run-live"]');
    if (liveButton) {
      await runLiveCheck({ withSchema: !!root.querySelector("#ui-live-schema")?.checked });
    }
  });
}

function renderLiveInPlace() {
  const button = root.querySelector('[data-action="run-live"]');
  if (button) {
    button.disabled = liveState.loading;
    button.innerHTML = `${icon(liveState.loading ? "loader-2" : "refresh", liveState.loading ? "icon icon--tiny icon--spin" : "icon icon--tiny")} ${liveState.loading ? "Checking…" : "Recheck"}`;
  }
  const resultEl = root.querySelector("#ui-live-result");
  if (resultEl) resultEl.innerHTML = renderLiveResult();
}
