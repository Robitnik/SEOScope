import { PROVIDERS, PROVIDER_ORDER, SOURCE_LABELS, STATE_META, escapeHtml, formatDate, formatDelta, formatRange, formatShortDate, formatTimestamp, formatValue, icon, initials, metricLabel, stateClass, trend } from "./formatters.js";

function providerIcon(provider) {
  const meta = PROVIDERS[provider];
  if (meta?.icon) return `<img class="provider-icon" src="${meta.icon}" width="20" height="20" alt="">`;
  return `<span class="provider-letter" aria-hidden="true">${meta?.short?.[0] || "S"}</span>`;
}

function statusMarkup(state, className = "provider-state") {
  const meta = STATE_META[state] || STATE_META.unavailable;
  return `<span class="${className} ${stateClass(state)}">${meta.label}</span>`;
}

function metricCell(metric, field) {
  const empty = metric[`${field}_value`] === null && !metric[`${field}_text`];
  return `<td class="${empty ? "metric-null" : ""}" title="${empty ? "API returned no data" : ""}">${formatValue(metric, field)}</td>`;
}

// Snapshot metrics (Ahrefs/Semrush) carry one `observed_at`/`data_through`
// instead of a range; range metrics (GSC/RN) carry exact current/previous
// ranges. Rendered inline so the exact dates are visible without hover
// (frontend/AGENTS.md §3/§5) and so the two kinds never look the same (§12).
function periodInfoLine(summary, ranges) {
  const metric = summary.metrics?.[0];
  if (!metric) return "";
  if (metric.comparison_kind === "snapshot") {
    const date = formatDate(metric.data_through || metric.observed_at);
    if (!date) return "";
    return `<p class="provider-dates" data-kind="snapshot">${icon("camera", "icon icon--tiny")}<span>Snapshot as of ${date}</span></p>`;
  }
  const current = formatRange(metric.current_range || ranges.current);
  const previous = formatRange(metric.previous_range || ranges.previous);
  return `<p class="provider-dates" data-kind="range">${icon("calendar", "icon icon--tiny")}<span>${current} <span aria-hidden="true">vs</span> ${previous}</span></p>`;
}

// Column headers carry each period's end date ("Current (19 Jul)") so a
// reader can tell the two number columns apart without scanning back up to
// periodInfoLine's range line above the table (§4/§12: snapshot vs range
// must stay visually distinct — snapshot tables have no range to anchor to,
// so they keep the plain "Current"/"Previous" label).
function metricsTable(summary, ranges) {
  const metric = summary.metrics?.[0];
  const isRange = metric && metric.comparison_kind !== "snapshot";
  const currentEnd = isRange ? formatShortDate((metric.current_range || ranges.current)?.end) : null;
  const previousEnd = isRange ? formatShortDate((metric.previous_range || ranges.previous)?.end) : null;
  return `
    <table class="metric-table">
      <thead><tr>
        <th scope="col">Metric</th>
        <th scope="col">Current${currentEnd ? `<small>${currentEnd}</small>` : ""}</th>
        <th scope="col">Previous${previousEnd ? `<small>${previousEnd}</small>` : ""}</th>
        <th scope="col">Change</th>
      </tr></thead>
      <tbody>${summary.metrics.slice(0, 4).map((metric) => `
        <tr>
          <td>${escapeHtml(metricLabel(metric))}</td>
          ${metricCell(metric, "current")}
          ${metricCell(metric, "previous")}
          <td class="delta" data-trend="${trend(metric)}" title="${escapeHtml(metric.delta_reason || "Comparison determined by API")}">${formatDelta(metric)}</td>
        </tr>`).join("")}</tbody>
    </table>`;
}

function providerBlock(summary, ranges) {
  const provider = summary.provider;
  const meta = PROVIDERS[provider] || { label: provider };
  const source = SOURCE_LABELS[summary.source_mode];
  return `
    <section class="provider-block ${stateClass(summary.state)}" aria-label="${meta.label}">
      <div class="provider-heading">
        <div class="provider-identity">${providerIcon(provider)}<span>${meta.label}</span></div>
        <span>${statusMarkup(summary.state)}${source ? `<small class="provider-source">${source}</small>` : ""}</span>
      </div>
      ${periodInfoLine(summary, ranges)}
      ${metricsTable(summary, ranges)}
    </section>`;
}

function setupAction(summary) {
  const provider = summary.provider;
  const meta = PROVIDERS[provider] || { label: provider };
  const state = STATE_META[summary.state] || STATE_META.unavailable;
  // task-22: a dead session gets its own `login` action (dashboard.js drives
  // `check_session`/`start_login` directly) instead of `integrate`'s
  // credentials form — re-entering credentials was never the fix for a
  // browser session that just needs a fresh sign-in.
  const isAuthRequired = summary.state === "auth_required";
  const needsConnection = summary.state === "not_configured" || isAuthRequired;
  const action = isAuthRequired ? "login" : needsConnection ? "integrate" : "sync-provider";
  const actionLabel = summary.state === "not_configured" ? "Connect" : isAuthRequired ? "Увійти" : "Retry";
  return `
    <button class="source-setup ${stateClass(summary.state)}" type="button" data-action="${action}" data-provider="${provider}" aria-disabled="false" title="${escapeHtml(`${meta.label}: ${state.label}`)}">
      ${providerIcon(provider)}
      <span><strong>${escapeHtml(meta.label)}</strong><small>${escapeHtml(state.label)}</small></span>
      <span class="source-setup__action">${actionLabel}</span>
    </button>`;
}

function setupPanel(summaries) {
  if (!summaries.length) return "";
  return `
    <section class="source-setup-panel" aria-label="Sources without current data">
      <div class="source-setup-panel__heading"><span>Needs attention</span><small>${summaries.length}</small></div>
      <div class="source-setup-list">${summaries.map(setupAction).join("")}</div>
    </section>`;
}

// Composite performance signal (PLAN §21.2). `value` is `None` — never `0`
// — when no service contributed; coverage is always shown alongside so
// "72% · 3/4 сервіси" reads differently from a client with one service total.
// Corner speedometer (moved off the meta row per feedback: a glanceable dial
// reads faster than an inline number). Ring is an SVG circle whose visible
// arc length is set from the health percentage; tone bands mirror the same
// good/warn/bad thresholds used elsewhere so the color always means the same
// thing across the dashboard.
const GAUGE_RADIUS = 16;
const GAUGE_CIRCUMFERENCE = 2 * Math.PI * GAUGE_RADIUS;

function healthTone(value) {
  if (value >= 70) return "good";
  if (value >= 40) return "warn";
  return "bad";
}

function healthGauge(health) {
  const configured = health?.coverage_configured ?? 0;
  const contributing = health?.coverage_contributing ?? 0;
  const empty = health?.value === null || health?.value === undefined;
  const rounded = empty ? 0 : Math.round(Number(health.value));
  const tone = empty ? "empty" : healthTone(rounded);
  const arc = empty ? 0 : (rounded / 100) * GAUGE_CIRCUMFERENCE;
  const label = empty
    ? (configured ? `Health: no data yet · 0/${configured} services` : "Health: no services connected")
    : `Health: ${rounded}% · ${contributing}/${configured} services`;
  return `
    <div class="health-gauge" data-tone="${tone}" title="${escapeHtml(label)}">
      <svg class="health-gauge__ring" viewBox="0 0 40 40" aria-hidden="true">
        <circle class="health-gauge__track" cx="20" cy="20" r="${GAUGE_RADIUS}"></circle>
        <circle class="health-gauge__value" cx="20" cy="20" r="${GAUGE_RADIUS}" stroke-dasharray="${arc} ${GAUGE_CIRCUMFERENCE}"></circle>
      </svg>
      <span class="health-gauge__number">${empty ? "—" : `${rounded}<small>%</small>`}</span>
      <span class="visually-hidden">${escapeHtml(label)}</span>
    </div>`;
}

// Sparkline slot: a placeholder above the Ahrefs block, filled in later by
// `renderHealthTrend` once `getClientHistory` resolves (dashboard.js calls
// it per card after the initial synchronous render — the dashboard payload
// itself only carries current/previous, not the ~12-period series a "rises
// and falls" line needs). Kept a plain empty box until then so layout
// doesn't jump when the fetch lands.
function healthTrendSlot(clientId) {
  return `<div class="health-trend" data-health-trend="${clientId}"></div>`;
}

function sparklinePoints(values, width, height, pad) {
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const stepX = (width - pad * 2) / (values.length - 1);
  return values.map((value, index) => {
    const x = pad + index * stepX;
    const y = pad + (height - pad * 2) * (1 - (value - min) / span);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(" ");
}

// Renders into a slot produced by `healthTrendSlot`. `series` is the raw
// `ClientMetricsHistoryDTO.series` array from `getClientHistory`; we chart
// Ahrefs' own Health Score (the metric already shown in the table right
// below) rather than the composite `client.health` value, which has no
// history endpoint of its own.
export function renderHealthTrend(container, series) {
  const width = 120;
  const height = 28;
  const pad = 3;
  const healthSeries = (series || []).find((item) => item.provider === "ahrefs" && item.metric_key === "health_score");
  const points = (healthSeries?.points || []).filter((point) => point.value_numeric !== null && point.value_numeric !== undefined);
  if (points.length < 2) {
    container.innerHTML = `<span class="health-trend__empty">Not enough history for a trend yet</span>`;
    return;
  }
  const values = points.map((point) => Number(point.value_numeric));
  const first = values[0];
  const last = values.at(-1);
  const deltaPct = first !== 0 ? ((last - first) / first) * 100 : null;
  const tone = last > first ? "good" : last < first ? "bad" : "flat";
  const deltaLabel = deltaPct === null ? "—" : `${deltaPct > 0 ? "+" : ""}${deltaPct.toFixed(1)}%`;
  const a11yLabel = `Ahrefs Health Score over ${points.length} periods: from ${first} to ${last}${deltaPct === null ? "" : ` (${deltaLabel})`}.`;
  container.innerHTML = `
    <div class="health-trend__body" data-tone="${tone}">
      <svg class="health-trend__svg" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" aria-hidden="true">
        <polyline class="health-trend__line" points="${sparklinePoints(values, width, height, pad)}"></polyline>
      </svg>
      <span class="health-trend__delta">${deltaLabel}</span>
      <span class="visually-hidden">${escapeHtml(a11yLabel)}</span>
    </div>`;
}

export function renderClientCard(client, dashboard) {
  const state = STATE_META[client.overall_status] || STATE_META.attention;
  const ordered = [...(client.providers || [])].sort((a, b) => PROVIDER_ORDER.indexOf(a.provider) - PROVIDER_ORDER.indexOf(b.provider));
  const withMetrics = ordered.filter((summary) => Array.isArray(summary.metrics) && summary.metrics.length > 0);
  const withoutMetrics = ordered.filter((summary) => !Array.isArray(summary.metrics) || summary.metrics.length === 0);
  const logo = client.logo_path
    ? `<img class="client-logo" src="${escapeHtml(client.logo_path)}" width="52" height="52" alt="Logo of ${escapeHtml(client.name)}" loading="lazy">`
    : `<span class="client-logo" aria-hidden="true">${escapeHtml(initials(client.name))}</span>`;
  return `
    <article class="client-card ${stateClass(client.overall_status)}" data-client-id="${client.client_id}">
      <header class="client-card__header">
        ${logo}
        <div class="client-title">
          <h2>${escapeHtml(client.name)}</h2>
          <a href="https://${encodeURI(client.domain)}" target="_blank" rel="noreferrer">${escapeHtml(client.domain)}</a>
        </div>
        ${healthGauge(client.health)}
        <div class="card-actions">
          <button class="button button--quiet" type="button" data-action="detail">History</button>
          <button class="button button--primary" type="button" data-action="sync">Refresh</button>
        </div>
      </header>
      <div class="client-card__meta">
        <span class="status-label ${stateClass(client.overall_status)}">${icon(state.icon)}${state.label}</span>
        <span>Last collection: ${formatTimestamp(client.last_synced_at)}</span>
      </div>
      <div class="client-card__providers">
        ${withMetrics.map((summary) => `${summary.provider === "ahrefs" ? healthTrendSlot(client.client_id) : ""}${providerBlock(summary, { current: dashboard.current_range, previous: dashboard.previous_range })}`).join("")}
        ${setupPanel(withoutMetrics)}
      </div>
    </article>`;
}

export function renderSkeletons(count = 4) {
  return Array.from({ length: count }, () => `<article class="skeleton-card" aria-hidden="true"><div class="skeleton-line skeleton-line--title"></div><div class="skeleton-line skeleton-line--short"></div><div class="skeleton-block"></div><div class="skeleton-line"></div><div class="skeleton-line skeleton-line--short"></div></article>`).join("");
}
