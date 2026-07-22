import { PROVIDERS, PROVIDER_ORDER, SOURCE_LABELS, STATE_META, escapeHtml, formatDate, formatDelta, formatRange, formatTimestamp, formatValue, icon, initials, metricLabel, stateClass, trend } from "./formatters.js";

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

function metricsTable(summary) {
  return `
    <table class="metric-table">
      <thead><tr>
        <th scope="col">Metric</th>
        <th scope="col">Current</th>
        <th scope="col">Previous</th>
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
      ${metricsTable(summary)}
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
function healthChip(health) {
  const configured = health?.coverage_configured ?? 0;
  const contributing = health?.coverage_contributing ?? 0;
  if (health?.value === null || health?.value === undefined) {
    return `<span class="health-chip" data-empty="true">${icon("gauge", "icon icon--tiny")}<span class="health-chip__label">Health</span><span class="health-chip__value">—</span><small>${configured ? `0/${configured} services` : "no services connected"}</small></span>`;
  }
  const rounded = Math.round(Number(health.value));
  return `<span class="health-chip">${icon("gauge", "icon icon--tiny")}<span class="health-chip__label">Health</span><span class="health-chip__value">${rounded}%</span><small>${contributing}/${configured} services</small></span>`;
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
        <div class="card-actions">
          <button class="button button--quiet" type="button" data-action="detail">History</button>
          <button class="button button--primary" type="button" data-action="sync">Refresh</button>
        </div>
      </header>
      <div class="client-card__meta">
        <span class="status-label ${stateClass(client.overall_status)}">${icon(state.icon)}${state.label}</span>
        ${healthChip(client.health)}
        <span>Last collection: ${formatTimestamp(client.last_synced_at)}</span>
      </div>
      <div class="client-card__providers">
        ${withMetrics.map((summary) => providerBlock(summary, { current: dashboard.current_range, previous: dashboard.previous_range })).join("")}
        ${setupPanel(withoutMetrics)}
      </div>
    </article>`;
}

export function renderSkeletons(count = 4) {
  return Array.from({ length: count }, () => `<article class="skeleton-card" aria-hidden="true"><div class="skeleton-line skeleton-line--title"></div><div class="skeleton-line skeleton-line--short"></div><div class="skeleton-block"></div><div class="skeleton-line"></div><div class="skeleton-line skeleton-line--short"></div></article>`).join("");
}
