import { PROVIDERS, PROVIDER_ORDER, SOURCE_LABELS, STATE_META, escapeHtml, formatDate, formatDelta, formatRange, formatShortDate, formatTimestamp, formatValue, icon, initials, metricLabel, stateClass, trend } from "./formatters.js?v=20260723-audit1";
import { buildClientHash } from "./router.js?v=20260723-audit1";

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
  const actionLabel = summary.state === "not_configured" ? "Connect" : isAuthRequired ? "Log in" : "Retry";
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
// "72% · 3/4 services" reads differently from a client with one service total.
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

// Two blocks above the Ahrefs section, both driven by the composite health
// score the corner gauge shows — the same number, so the dial, the arrow and
// the line always tell one story:
//
//   1. `healthDeltaBlock` — which way this client moved over the past week.
//   2. `healthSparkline`  — the daily shape of that move.
//
// Both read `client.health` straight from the dashboard payload (the backend
// records a point per client per day and returns `delta_pct` + `history`), so
// they render synchronously with the card. That replaces the previous slot,
// which fetched a *per-card* history request after paint and charted Ahrefs'
// own Health Score — a different series from the gauge it sat under.
function healthTrendBlocks(health) {
  return `<div class="health-trend">${healthDeltaBlock(health)}${healthSparkline(health)}</div>`;
}

// Direction is not read off the sign alone (AGENTS.md §5): health is an
// explicitly "higher is better" score, which is what makes up == good here.
// A missing delta renders as "—", never as 0% (PLAN §12, None != 0).
function healthDeltaBlock(health) {
  const raw = health?.delta_pct;
  if (raw === null || raw === undefined) {
    return `
      <div class="health-delta" data-tone="flat">
        ${icon("minus", "icon icon--tiny")}
        <span class="health-delta__value">—</span>
        <span class="health-delta__caption">no week-over-week basis yet</span>
      </div>`;
  }
  const delta = Number(raw);
  const tone = delta > 0 ? "good" : delta < 0 ? "bad" : "flat";
  const glyph = delta > 0 ? "trending-up" : delta < 0 ? "trending-down" : "minus";
  const formatted = `${delta > 0 ? "+" : ""}${delta.toFixed(1)}%`;
  const caption = delta > 0 ? "up over the week" : delta < 0 ? "down over the week" : "flat over the week";
  return `
    <div class="health-delta" data-tone="${tone}">
      ${icon(glyph, "icon icon--tiny")}
      <span class="health-delta__value">${escapeHtml(formatted)}</span>
      <span class="health-delta__caption">${escapeHtml(caption)}</span>
    </div>`;
}

// Daily line. Days with no contributing service are absent from `history`
// rather than zero, so the line is drawn over the points that exist and the
// a11y summary states the real span instead of implying an unbroken series.
function healthSparkline(health) {
  const width = 120;
  const height = 28;
  const pad = 3;
  const points = (health?.history || []).filter((p) => p.value !== null && p.value !== undefined);
  if (points.length < 2) {
    return `<span class="health-trend__empty">Daily trend appears once there are two days of history</span>`;
  }
  const values = points.map((p) => Number(p.value));
  const first = values[0];
  const last = values.at(-1);
  const tone = last > first ? "good" : last < first ? "bad" : "flat";
  const a11yLabel = `Composite health over ${points.length} days, ${points[0].day} to ${points.at(-1).day}: from ${first}% to ${last}%.`;
  return `
    <div class="health-trend__body" data-tone="${tone}">
      <svg class="health-trend__svg" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" aria-hidden="true">
        <polyline class="health-trend__line" points="${sparklinePoints(values, width, height, pad)}"></polyline>
      </svg>
      <span class="visually-hidden">${escapeHtml(a11yLabel)}</span>
    </div>`;
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

export function renderClientCard(client, dashboard) {
  const state = STATE_META[client.overall_status] || STATE_META.attention;
  const ordered = [...(client.providers || [])]
    .filter((summary) => PROVIDER_ORDER.includes(summary.provider))
    .sort((a, b) => PROVIDER_ORDER.indexOf(a.provider) - PROVIDER_ORDER.indexOf(b.provider));
  const withMetrics = ordered.filter((summary) => Array.isArray(summary.metrics) && summary.metrics.length > 0);
  const withoutMetrics = ordered.filter((summary) => !Array.isArray(summary.metrics) || summary.metrics.length === 0);
  const logo = client.logo_path
    ? `<img class="client-logo" src="${escapeHtml(client.logo_path)}" width="52" height="52" alt="Logo of ${escapeHtml(client.name)}" loading="lazy">`
    : `<span class="client-logo" aria-hidden="true">${escapeHtml(initials(client.name))}</span>`;
  // Вхід на сторінку клієнта (router.js `#/client/<id>`) з трьох цілей: лого,
  // назва і «відкрити» біля домену. Назва — єдина точка табуляції з іменем
  // клієнта; лого й іконка дублюють її для миші, тому вони приховані від
  // скрінрідера й прибрані з tab-порядку (AGENTS.md §15: без дублів у
  // послідовності фокуса). Домен лишається зовнішнім посиланням на сайт.
  const pageHref = buildClientHash(client.client_id);
  // «Копіювати» — справжня кнопка (на відміну від декоративних лого/іконки
  // «відкрити», які лише дублюють посилання): вона доступна з клавіатури,
  // має aria-label і живе поруч із назвою та URL. Копіює повний site URL;
  // клік обробляє dashboard.js і на мить міняє іконку на «галочку».
  const siteUrl = `https://${client.domain}`;
  return `
    <article class="client-card ${stateClass(client.overall_status)}" data-client-id="${client.client_id}">
      <header class="client-card__header">
        <a class="client-logo-link" href="${pageHref}" tabindex="-1" aria-hidden="true">${logo}</a>
        <div class="client-title">
          <h2>
            <a class="client-open-name" href="${pageHref}">${escapeHtml(client.name)}</a>
            <button class="client-copy client-copy--name" type="button" data-action="copy" data-copy="${escapeHtml(client.name)}" aria-label="${escapeHtml(`Copy client name ${client.name}`)}" title="Copy client name">${icon("copy", "icon icon--tiny")}</button>
          </h2>
          <p class="client-domain">
            <a href="${escapeHtml(encodeURI(siteUrl))}" target="_blank" rel="noreferrer">${escapeHtml(client.domain)}</a>
            <a class="client-open" href="${pageHref}" tabindex="-1" aria-hidden="true" title="${escapeHtml(`Open ${client.name} page`)}">${icon("external-link", "icon icon--tiny")}</a>
            <button class="client-copy" type="button" data-action="copy" data-copy="${escapeHtml(siteUrl)}" aria-label="${escapeHtml(`Copy website URL for ${client.name}`)}" title="Copy website URL">${icon("copy", "icon icon--tiny")}</button>
          </p>
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
        ${withMetrics.map((summary) => `${summary.provider === "ahrefs" ? healthTrendBlocks(client.health) : ""}${providerBlock(summary, { current: dashboard.current_range, previous: dashboard.previous_range })}`).join("")}
        ${setupPanel(withoutMetrics)}
      </div>
    </article>`;
}

export function renderSkeletons(count = 4) {
  return Array.from({ length: count }, () => `<article class="skeleton-card" aria-hidden="true"><div class="skeleton-line skeleton-line--title"></div><div class="skeleton-line skeleton-line--short"></div><div class="skeleton-block"></div><div class="skeleton-line"></div><div class="skeleton-line skeleton-line--short"></div></article>`).join("");
}
