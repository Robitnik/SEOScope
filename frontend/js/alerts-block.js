import { escapeHtml, icon } from "./formatters.js?v=20260811-monitor1";
import { formatUaDelta, formatUaNumber, formatUaPercent, formatUaRange } from "./client-page.js?v=20260811-monitor1";

const SKIP_REASON_COPY = {
  missing_period_data: "GSC dimensional data has not been collected for one of the two periods yet.",
  current_period_partial: "The current period was not fully collected — an alert on incomplete data could be misleading.",
  current_period_truncated: "The current period is truncated to top-N — comparison on a truncated set is unreliable.",
  previous_period_partial: "The previous period was not fully collected — the comparison base cannot be considered reliable.",
  previous_period_truncated: "The previous period is truncated to top-N — the comparison base cannot be considered complete.",
};

function entryRow(entry, tone) {
  const pct = formatUaPercent(entry.delta_pct);
  return `
    <li class="alert-row">
      <span class="alert-row__value">${escapeHtml(entry.dimension_value)}</span>
      <span class="alert-row__clicks">${formatUaNumber(entry.clicks_current)}<small>was ${formatUaNumber(entry.clicks_previous)}</small></span>
      <span class="alert-row__delta" data-trend="${tone}">${formatUaDelta(entry.delta_abs)}${pct ? ` · ${pct}` : ""}</span>
    </li>`;
}

function group(title, icon_, entries, tone, emptyNote) {
  return `
    <div class="alert-group">
      <h3>${icon(icon_)}${title}<small>${entries.length}</small></h3>
      ${entries.length
        ? `<ul class="alert-list">${entries.map((entry) => entryRow(entry, tone)).join("")}</ul>`
        : `<p class="alert-group__empty">${emptyNote}</p>`}
    </div>`;
}

export function renderAlertsBlock(container, alerts) {
  const noun = alerts.dimension === "page" ? "pages" : "queries";
  const dates = `<p class="alerts-section__dates">${icon("calendar-stats")}Comparing ${noun}: <strong>${formatUaRange(alerts.period_current_start, alerts.period_current_end)}</strong> vs <strong>${formatUaRange(alerts.period_previous_start, alerts.period_previous_end)}</strong></p>`;

  if (alerts.skipped_reason) {
    container.innerHTML = `
      ${dates}
      <div class="state-panel state-partial">
        ${icon("info-circle")}
        <p><strong>Not enough data for alerts.</strong><br>${escapeHtml(SKIP_REASON_COPY[alerts.skipped_reason] || alerts.skipped_reason)}</p>
      </div>`;
    return;
  }

  container.innerHTML = `
    ${dates}
    <div class="alerts-grid">
      ${group("Declining", "trending-down", alerts.declining, "bad", `No ${noun} losing clicks significantly.`)}
      ${group("Rising", "trending-up", alerts.rising, "good", `No ${noun} with notable growth.`)}
      ${group("New", "sparkles", alerts.new, "neutral", `No new ${noun} with a notable amount of clicks appeared.`)}
    </div>`;
}
