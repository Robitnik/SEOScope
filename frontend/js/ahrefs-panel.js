import { escapeHtml, formatTimestamp, icon } from "./formatters.js?v=20260812-monitor7";
import { panelSkeleton } from "./skeleton.js?v=20260812-monitor7";
import { formatUaNumber } from "./client-page.js?v=20260812-monitor7";

function metricValue(summary, key) {
  const metric = (summary?.metrics || []).find((item) => item.metric_key === key);
  return metric?.current_value ?? null;
}

function severityClass(value) {
  return `ahrefs-severity--${String(value || "notice").toLocaleLowerCase("en-US")}`;
}

function kpis(summary, audit) {
  const cards = [
    ["Health score", metricValue(summary, "ahrefs_health_score"), "%"],
    ["URLs crawled", metricValue(summary, "ahrefs_urls_crawled"), ""],
    ["Errors", audit?.severity_counts?.Error ?? metricValue(summary, "ahrefs_errors"), ""],
    ["Warnings", audit?.severity_counts?.Warning ?? metricValue(summary, "ahrefs_warnings"), ""],
    ["Notices", audit?.severity_counts?.Notice, " issue types"],
    ["Affected rows", audit?.total_affected_rows, ""],
  ];
  return `<div class="ahrefs-kpis">${cards.map(([label, value, suffix]) => `
    <article class="ahrefs-kpi"><span>${label}</span><strong>${formatUaNumber(value)}${value === null || value === undefined ? "" : suffix}</strong></article>`).join("")}</div>`;
}

function issueTable(audit, state) {
  const query = state.ahrefsSearch.trim().toLocaleLowerCase("en-US");
  const rows = (audit?.issues || []).filter((issue) => {
    const severityMatches = !state.ahrefsSeverity || issue.severity === state.ahrefsSeverity;
    const text = `${issue.name} ${issue.description} ${issue.how_to_fix}`.toLocaleLowerCase("en-US");
    return severityMatches && (!query || text.includes(query));
  });
  const toolbar = `<div class="analytics-toolbar ahrefs-toolbar">
    <label>Severity<select id="ahrefs-severity"><option value="">All issues</option>${["Error", "Warning", "Notice"].map((value) => `<option value="${value}" ${state.ahrefsSeverity === value ? "selected" : ""}>${value}s</option>`).join("")}</select></label>
    <label class="search-field"><span class="visually-hidden">Search Ahrefs issues</span>${icon("search")}<input id="ahrefs-search" type="search" placeholder="Search issues" value="${escapeHtml(state.ahrefsSearch)}"></label>
  </div>`;
  if (!audit?.total_issue_types) {
    return `${toolbar}<div class="state-panel"><p><strong>No detailed Ahrefs export saved yet.</strong><br>Live collection will export all Site Audit issues and affected URLs.</p></div>`;
  }
  if (!rows.length) return `${toolbar}<div class="state-panel"><p><strong>No matching issues.</strong></p></div>`;
  return `${toolbar}<div class="table-scroll"><table class="analytics-table ahrefs-issues-table">
    <thead><tr><th>Issue</th><th>Severity</th><th>Affected URLs</th><th>Exported rows</th></tr></thead>
    <tbody>${rows.map((issue) => {
      const selected = state.ahrefsSelectedIssue === issue.slug;
      return `<tr class="${selected ? "is-selected" : ""}">
      <td class="analytics-row__value"><button type="button" class="ahrefs-issue-link" data-ahrefs-issue="${escapeHtml(issue.slug)}" aria-expanded="${String(selected)}" aria-controls="ahrefs-affected-pages"><strong>${escapeHtml(issue.name)}${icon(selected ? "chevron-down" : "chevron-right")}</strong>${issue.description ? `<span>${escapeHtml(issue.description)}</span>` : ""}${issue.how_to_fix ? `<small><b>Fix:</b> ${escapeHtml(issue.how_to_fix)}</small>` : ""}</button></td>
      <td><span class="ahrefs-severity ${severityClass(issue.severity)}">${escapeHtml(issue.severity)}</span></td>
      <td class="num">${formatUaNumber(issue.affected_urls)}</td><td class="num">${formatUaNumber(issue.exported_rows)}</td>
    </tr>`;
    }).join("")}</tbody></table></div>`;
}

function detailsMarkup(details) {
  const entries = Object.entries(details || {}).filter(([, value]) => value !== "" && value !== null && value !== undefined);
  if (!entries.length) return "—";
  return `<details class="ahrefs-row-details"><summary>${entries.length} exported fields</summary><dl>${entries.map(([key, value]) => `<dt>${escapeHtml(key)}</dt><dd>${escapeHtml(value)}</dd>`).join("")}</dl></details>`;
}

function affectedTable(affected, affectedError, state) {
  const targetAttrs = `id="ahrefs-affected-pages" tabindex="-1" aria-live="polite"`;
  if (!state.ahrefsSelectedIssue) return `<section class="ahrefs-affected state-panel" ${targetAttrs}><p><strong>Select an issue to inspect every affected page.</strong></p></section>`;
  if (affectedError) return `<section class="ahrefs-affected state-panel state-failed" ${targetAttrs}>${icon("wifi-off")}<p><strong>Failed to load affected pages.</strong><br>${escapeHtml(affectedError)}</p></section>`;
  if (!affected) return `<section class="ahrefs-affected" ${targetAttrs} aria-busy="true">${panelSkeleton({ label: "affected pages", rows: 6, columns: 2 })}</section>`;
  const rows = affected.rows || [];
  const start = affected.total_rows_available ? affected.offset + 1 : 0;
  const end = Math.min(affected.offset + affected.limit, affected.total_rows_available);
  return `<section class="ahrefs-affected" ${targetAttrs}>
    <div class="section-heading"><div><p class="eyebrow">Affected pages</p><h3>${escapeHtml(rows[0]?.issue_name || state.ahrefsSelectedIssue)}</h3></div><span class="status-label status-complete">${formatUaNumber(affected.total_rows_available)} rows</span></div>
    <div class="analytics-toolbar"><label class="search-field"><span class="visually-hidden">Search affected URLs</span>${icon("search")}<input id="ahrefs-url-search" type="search" placeholder="Search URL or exported fields" value="${escapeHtml(state.ahrefsUrlSearch)}"></label></div>
    ${rows.length ? `<div class="table-scroll"><table class="analytics-table ahrefs-urls-table"><thead><tr><th>URL</th><th>Ahrefs export data</th></tr></thead><tbody>${rows.map((row) => `<tr><td class="analytics-row__value">${row.url ? `<a href="${escapeHtml(row.url)}" target="_blank" rel="noreferrer">${escapeHtml(row.url)}</a>` : "—"}</td><td>${detailsMarkup(row.details)}</td></tr>`).join("")}</tbody></table></div>` : `<div class="state-panel"><p><strong>No affected rows match this search.</strong></p></div>`}
    <div class="pagination"><button type="button" class="button button--quiet" id="ahrefs-prev" ${affected.offset <= 0 ? "disabled" : ""}>${icon("chevron-left")}Previous</button><span>${start}–${end} of ${affected.total_rows_available}</span><button type="button" class="button button--quiet" id="ahrefs-next" ${end >= affected.total_rows_available ? "disabled" : ""}>Next${icon("chevron-right")}</button></div>
  </section>`;
}

export function renderAhrefsPanel(container, { summary, audit, affected, affectedError, state, onChange, onSelectIssue, onAffectedPage }) {
  const status = summary?.state || (audit?.total_issue_types ? "complete" : "unavailable");
  const collectedSummary = audit?.total_issue_types
    ? `${formatUaNumber(audit.total_issue_types)} issue types · ${formatUaNumber(audit.total_affected_rows)} affected rows`
    : "No detailed export saved";
  container.innerHTML = `<div class="section-heading"><div><p class="eyebrow">Ahrefs · Live Site Audit</p><h2>Technical health and crawl issues</h2><p class="ahrefs-collected">Latest live collection: ${formatTimestamp(audit?.observed_at, "not collected yet")} · ${collectedSummary}</p></div><span class="status-label status-${escapeHtml(status)}">${escapeHtml(status)}</span></div>
    ${kpis(summary, audit)}${issueTable(audit, state)}${affectedTable(affected, affectedError, state)}`;
  container.querySelector("#ahrefs-severity")?.addEventListener("change", (event) => onChange({ ahrefsSeverity: event.target.value }));
  let issueTimer = null;
  container.querySelector("#ahrefs-search")?.addEventListener("input", (event) => {
    clearTimeout(issueTimer);
    const value = event.target.value;
    issueTimer = setTimeout(() => onChange({ ahrefsSearch: value }), 180);
  });
  container.querySelectorAll("[data-ahrefs-issue]").forEach((button) => button.addEventListener("click", () => onSelectIssue(button.dataset.ahrefsIssue)));
  let urlTimer = null;
  container.querySelector("#ahrefs-url-search")?.addEventListener("input", (event) => {
    clearTimeout(urlTimer);
    const value = event.target.value;
    urlTimer = setTimeout(() => onAffectedPage(0, value), 250);
  });
  container.querySelector("#ahrefs-prev")?.addEventListener("click", () => onAffectedPage(Math.max(0, (affected?.offset || 0) - (affected?.limit || 100))));
  container.querySelector("#ahrefs-next")?.addEventListener("click", () => onAffectedPage((affected?.offset || 0) + (affected?.limit || 100)));
}

export function renderAhrefsError(container, message) {
  container.innerHTML = `<div class="section-heading"><div><p class="eyebrow">Ahrefs · Live Site Audit</p><h2>Technical health and crawl issues</h2></div></div><div class="state-panel state-failed">${icon("wifi-off")}<p><strong>Failed to load Ahrefs data.</strong><br>${escapeHtml(message)}</p></div>`;
}
