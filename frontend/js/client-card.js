import { PROVIDERS, PROVIDER_ORDER, SOURCE_LABELS, STATE_META, escapeHtml, formatDelta, formatRange, formatTimestamp, formatValue, icon, initials, metricLabel, stateClass, trend } from "./formatters.js";

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
  return `<td class="${empty ? "metric-null" : ""}" title="${empty ? "No value returned by the API" : ""}">${formatValue(metric, field)}</td>`;
}

function metricsTable(summary, ranges) {
  return `
    <table class="metric-table">
      <thead><tr>
        <th scope="col">Metric</th>
        <th scope="col" title="${formatRange(ranges.current)}">Current</th>
        <th scope="col" title="${formatRange(ranges.previous)}">Previous</th>
        <th scope="col">Change</th>
      </tr></thead>
      <tbody>${summary.metrics.slice(0, 4).map((metric) => `
        <tr>
          <td>${escapeHtml(metricLabel(metric))}</td>
          ${metricCell(metric, "current")}
          ${metricCell(metric, "previous")}
          <td class="delta" data-trend="${trend(metric)}" title="${metric.delta_reason || "Comparison supplied by the API"}">${formatDelta(metric)}</td>
        </tr>`).join("")}</tbody>
    </table>`;
}

function stateMessage(summary, provider) {
  const messages = {
    not_configured: ["Integration not configured", "Connect this source to start collecting data."],
    auth_required: ["Authorization lost", "Reconnect the service to continue."],
    failed: ["Collection failed", summary.last_error_code || "Review the configuration and retry."],
    unavailable: ["Source temporarily unavailable", "Stored values are never replaced with zeroes."],
  };
  const [title, copy] = messages[summary.state] || ["No data yet", "Run a refresh for this client." ];
  const action = summary.state === "not_configured" || summary.state === "auth_required"
    ? `<button class="text-button" type="button" data-action="integrate" data-provider="${provider}">${summary.state === "not_configured" ? "Connect" : "Reconnect"}</button>`
    : `<button class="text-button" type="button" data-action="sync-provider" data-provider="${provider}">Retry</button>`;
  return `<div class="provider-message"><p><strong>${title}</strong>${copy}</p>${action}</div>`;
}

function providerBlock(summary, ranges) {
  const provider = summary.provider;
  const meta = PROVIDERS[provider] || { label: provider };
  const source = SOURCE_LABELS[summary.source_mode];
  const hasMetrics = Array.isArray(summary.metrics) && summary.metrics.length > 0;
  return `
    <section class="provider-block ${stateClass(summary.state)}" aria-label="${meta.label}">
      <div class="provider-heading">
        <div class="provider-identity">${providerIcon(provider)}<span>${meta.label}</span></div>
        <span>${statusMarkup(summary.state)}${source ? `<small class="provider-source">${source}</small>` : ""}</span>
      </div>
      ${hasMetrics ? metricsTable(summary, ranges) : stateMessage(summary, provider)}
    </section>`;
}

export function renderClientCard(client, dashboard) {
  const state = STATE_META[client.overall_status] || STATE_META.attention;
  const providers = new Map((client.providers || []).map((item) => [item.provider, item]));
  const ordered = PROVIDER_ORDER.map((provider) => providers.get(provider) || { provider, state: "not_configured", metrics: [] });
  const logo = client.logo_path
    ? `<img class="client-logo" src="${escapeHtml(client.logo_path)}" width="52" height="52" alt="${escapeHtml(client.name)} logo" loading="lazy">`
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
        <span>Last sync: ${formatTimestamp(client.last_synced_at)}</span>
      </div>
      <div class="client-card__providers">
        ${ordered.map((summary) => providerBlock(summary, { current: dashboard.current_range, previous: dashboard.previous_range })).join("")}
      </div>
    </article>`;
}

export function renderSkeletons(count = 2) {
  return Array.from({ length: count }, () => `<article class="skeleton-card" aria-hidden="true"><div class="skeleton-line skeleton-line--title"></div><div class="skeleton-line skeleton-line--short"></div>${Array.from({ length: 4 }, () => `<div class="skeleton-block"></div><div class="skeleton-line"></div><div class="skeleton-line skeleton-line--short"></div>`).join("")}</article>`).join("");
}
