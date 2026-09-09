import { escapeHtml, formatTimestamp, icon } from "./formatters.js?v=20260821-spin3";
import { panelSkeleton, sectionSkeleton } from "./skeleton.js?v=20260821-spin3";
import { formatUaNumber } from "./client-page.js?v=20260909-t89c";

const RECHECK_META = {
  fixed: { label: "Fixed", tone: "complete", icon: "circle-check" },
  still_present: { label: "Still present", tone: "failed", icon: "alert-triangle" },
  unreachable: { label: "Unreachable", tone: "auth_required", icon: "plug-off" },
  skipped: { label: "Needs full re-run", tone: "not_configured", icon: "minus" },
};

function severityClass(value) {
  return `ahrefs-severity--${String(value || "notice").toLocaleLowerCase("en-US")}`;
}

function severityBadge(value) {
  const label = value || "Notice";
  return `<span class="ahrefs-severity ${severityClass(label)}">${escapeHtml(label)}</span>`;
}

function recheckBadge(status, at) {
  if (!status) return `<span class="audit-recheck audit-recheck--none">Not re-checked</span>`;
  const meta = RECHECK_META[status] || { label: status, tone: "not_configured", icon: "help-circle" };
  const when = at ? ` title="Last re-checked ${escapeHtml(formatTimestamp(at, ""))}"` : "";
  return `<span class="audit-recheck status-${meta.tone}"${when}>${icon(meta.icon)}${escapeHtml(meta.label)}</span>`;
}

function actions(summary, exportUrl, busy) {
  // `.icon--spin` is what actually animates (app.css); `icon()` only applies it
  // when passed as the class — without it the spinner renders frozen.
  const runLabel = busy
    ? `${icon("loader-2", "icon icon--spin")}Running audit…`
    : `${icon("refresh")}Run audit`;
  const exportButton = summary?.has_run
    ? `<a class="button button--quiet" href="${escapeHtml(exportUrl)}" download>${icon("download")}Export to XLSX</a>`
    : `<span class="button button--quiet is-disabled" aria-disabled="true" title="Run the audit first">${icon("download")}Export to XLSX</span>`;
  return `<div class="audit-actions">
    <button type="button" class="button button--primary" id="audit-run" ${busy ? "disabled" : ""}>${runLabel}</button>
    ${exportButton}
  </div>`;
}

function meta(summary) {
  if (!summary?.has_run) return "";
  const bits = [
    ["Crawled", summary.pages_crawled === null || summary.pages_crawled === undefined ? "—" : `${formatUaNumber(summary.pages_crawled)} pages`],
    ["Health", summary.health_score === null || summary.health_score === undefined ? "—" : `${formatUaNumber(summary.health_score)}%`],
    ["Platform", summary.cms || "—"],
  ];
  return `<div class="audit-meta">
    <p class="audit-collected">Last crawl: ${formatTimestamp(summary.observed_at, "not run yet")}</p>
    ${bits.map(([label, value]) => `<article class="audit-kpi"><span>${escapeHtml(label)}</span><strong>${escapeHtml(String(value))}</strong></article>`).join("")}
  </div>`;
}

function warnings(summary) {
  const list = summary?.warnings || [];
  if (!list.length) return "";
  return `<div class="state-panel state-partial audit-warnings">${icon("alert-triangle")}
    <div><strong>The crawl reported ${list.length} warning${list.length === 1 ? "" : "s"}.</strong>
    <ul>${list.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul></div></div>`;
}

function categoryTable(summary, state) {
  const rows = summary?.categories || [];
  if (!rows.length) {
    return `<div class="state-panel">${icon("circle-check")}<p><strong>No technical issues found.</strong><br>The last crawl produced no findings.</p></div>`;
  }
  return `<div class="table-scroll"><table class="analytics-table audit-categories-table">
    <caption class="visually-hidden">Problem categories found by the last crawl</caption>
    <thead><tr><th scope="col">Category</th><th scope="col">Severity</th><th scope="col">Findings</th><th scope="col">Re-check status</th></tr></thead>
    <tbody>${rows.map((row) => {
      const selected = state.auditCategory === row.category;
      const rc = row.recheck || {};
      const verdicts = [
        rc.fixed ? `${rc.fixed} fixed` : "",
        rc.still_present ? `${rc.still_present} still present` : "",
        rc.unreachable ? `${rc.unreachable} unreachable` : "",
      ].filter(Boolean).join(" · ");
      return `<tr class="${selected ? "is-selected" : ""}">
        <td class="analytics-row__value">
          <button type="button" class="ahrefs-issue-link" data-audit-category="${escapeHtml(row.category)}" aria-expanded="${String(selected)}">
            <strong>${escapeHtml(row.name)}</strong>
            ${row.how_to_fix ? `<small><b>Fix:</b> ${escapeHtml(row.how_to_fix)}</small>` : ""}
          </button>
        </td>
        <td>${severityBadge(row.severity)}</td>
        <td class="num">${formatUaNumber(row.finding_count)}</td>
        <td>${verdicts ? escapeHtml(verdicts) : `<span class="audit-recheck audit-recheck--none">—</span>`}</td>
      </tr>`;
    }).join("")}</tbody></table></div>`;
}

function measuredMarkup(measured) {
  const entries = Object.entries(measured || {}).filter(([, value]) => value !== "" && value !== null && value !== undefined);
  if (!entries.length) return "";
  return `<details class="ahrefs-row-details"><summary>${entries.length} measured field${entries.length === 1 ? "" : "s"}</summary><dl>${entries.map(([key, value]) => `<dt>${escapeHtml(key)}</dt><dd>${escapeHtml(typeof value === "object" ? JSON.stringify(value) : String(value))}</dd>`).join("")}</dl></details>`;
}

function findingsBlock(findings, state, pendingRecheck) {
  if (!state.auditCategory) {
    return `<div class="state-panel audit-findings">${icon("list-search")}<p><strong>Select a category to see every affected URL.</strong></p></div>`;
  }
  if (!findings) {
    return `<div class="audit-findings" aria-busy="true">${panelSkeleton({ label: "findings", rows: 6, columns: 2 })}</div>`;
  }
  const rows = findings.rows || [];
  const start = findings.total ? findings.offset + 1 : 0;
  const end = Math.min(findings.offset + findings.limit, findings.total);
  const title = rows[0]?.category_name || state.auditCategory;
  return `<section class="audit-findings">
    <div class="section-heading">
      <div><p class="eyebrow">Findings</p><h3>${escapeHtml(title)}</h3></div>
      <span class="status-label status-complete">${formatUaNumber(findings.total)} findings</span>
    </div>
    <div class="analytics-toolbar">
      <label class="search-field"><span class="visually-hidden">Search findings by URL or problem</span>${icon("search")}<input id="audit-search" type="search" placeholder="Search URL or problem" value="${escapeHtml(state.auditSearch)}"></label>
    </div>
    ${rows.length ? `<ul class="audit-finding-list">${rows.map((row) => {
      const busy = pendingRecheck.has(row.id);
      return `<li class="audit-finding">
        <div class="audit-finding__head">
          ${severityBadge(row.severity)}
          <strong class="audit-finding__name">${escapeHtml(row.name)}</strong>
          ${recheckBadge(row.recheck_status, row.recheck_at)}
        </div>
        <a class="audit-finding__url" href="${escapeHtml(row.url)}" target="_blank" rel="noreferrer">${escapeHtml(row.url)}</a>
        ${row.current_value ? `<p class="audit-finding__current"><b>Current:</b> ${escapeHtml(row.current_value)}</p>` : ""}
        ${row.evidence ? `<p class="audit-finding__current"><b>Measured:</b> ${escapeHtml(row.evidence)}</p>` : ""}
        ${row.how_to_fix ? `<p class="audit-finding__fix"><b>Fix:</b> ${escapeHtml(row.how_to_fix)}</p>` : ""}
        ${measuredMarkup(row.measured)}
        <div class="audit-finding__actions">
          ${row.recheckable
            ? `<button type="button" class="button button--quiet" data-audit-recheck="${escapeHtml(row.id)}" ${busy ? "disabled" : ""}>${busy ? `${icon("loader-2", "icon icon--spin")}Re-checking…` : `${icon("refresh")}Re-check`}</button>`
            : `<span class="button button--quiet is-disabled" aria-disabled="true" title="Site-graph scoped: only a full audit re-run can verify this">${icon("refresh")}Requires a full re-run</span>`}
        </div>
      </li>`;
    }).join("")}</ul>` : `<div class="state-panel"><p><strong>No findings match this search.</strong></p></div>`}
    <div class="pagination">
      <button type="button" class="button button--quiet" id="audit-prev" ${findings.offset <= 0 ? "disabled" : ""}>${icon("chevron-left")}Previous</button>
      <span>${start}–${end} of ${formatUaNumber(findings.total)}</span>
      <button type="button" class="button button--quiet" id="audit-next" ${end >= findings.total ? "disabled" : ""}>Next${icon("chevron-right")}</button>
    </div>
  </section>`;
}

function heading(summary, exportUrl, busy) {
  const status = summary?.has_run ? (summary.quality || "complete") : "not_configured";
  return `<div class="section-heading">
    <div>
      <p class="eyebrow">Technical SEO audit · machine findings</p>
      <h2>Site crawl issues</h2>
      <p class="audit-subtitle">Every row is measured by the crawler — no generated copy.</p>
    </div>
    <span class="status-label status-${escapeHtml(status)}">${escapeHtml(summary?.has_run ? status : "no run yet")}</span>
  </div>${actions(summary, exportUrl, busy)}`;
}

export function renderAuditPanel(container, {
  summary, findings, state, exportUrl, busy = false, pendingRecheck = new Set(),
  onRunAudit, onSelectCategory, onSearch, onPage, onRecheck,
}) {
  if (!summary) {
    container.innerHTML = `${heading(null, exportUrl, busy)}${sectionSkeleton({ label: "the technical audit", heading: false, kpis: 4, rows: 5, columns: 3 })}`;
    return;
  }
  if (!summary.has_run) {
    container.innerHTML = `${heading(summary, exportUrl, busy)}
      <div class="state-panel state-not_configured">${icon("radar")}
        <p><strong>No technical audit has been run for this client yet.</strong><br>
        Run the crawler to detect broken links, redirects, missing titles, indexability problems and more.</p>
      </div>`;
    container.querySelector("#audit-run")?.addEventListener("click", () => onRunAudit());
    return;
  }

  container.innerHTML = `${heading(summary, exportUrl, busy)}${meta(summary)}${warnings(summary)}
    ${categoryTable(summary, state)}${findingsBlock(findings, state, pendingRecheck)}`;

  container.querySelector("#audit-run")?.addEventListener("click", () => onRunAudit());
  container.querySelectorAll("[data-audit-category]").forEach((button) => {
    button.addEventListener("click", () => onSelectCategory(button.dataset.auditCategory));
  });
  container.querySelectorAll("[data-audit-recheck]").forEach((button) => {
    button.addEventListener("click", () => onRecheck(button.dataset.auditRecheck));
  });
  let searchTimer = null;
  container.querySelector("#audit-search")?.addEventListener("input", (event) => {
    clearTimeout(searchTimer);
    const value = event.target.value;
    searchTimer = setTimeout(() => onSearch(value), 220);
  });
  container.querySelector("#audit-prev")?.addEventListener("click", () => {
    onPage(Math.max(0, (findings?.offset || 0) - (findings?.limit || 25)));
  });
  container.querySelector("#audit-next")?.addEventListener("click", () => {
    onPage((findings?.offset || 0) + (findings?.limit || 25));
  });
}

export function renderAuditError(container, message) {
  container.innerHTML = `<div class="section-heading"><div><p class="eyebrow">Technical SEO audit · machine findings</p><h2>Site crawl issues</h2></div></div>
    <div class="state-panel state-failed">${icon("wifi-off")}<p><strong>Failed to load the technical audit.</strong><br>${escapeHtml(message)}</p></div>`;
}
