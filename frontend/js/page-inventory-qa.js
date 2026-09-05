// Page Inventory (task-57) — "SEO QA" tab: same rows as `page-inventory-table.js`,
// but every field carries a severity badge from `app.contentqa` (via
// `app.pageinventory.calibrate`) and a click reveals what's actually wrong.
// Also owns the duplicate-cluster summary strip and the Calibrated/Strict
// toggle — both act on the SAME finding set the table shows, never a
// separately-fetched one (task-57 §2.3).

import { escapeHtml, icon } from "./formatters.js?v=20260821-spin3";
import { breadcrumbText, rowStatusClass } from "./page-inventory-table.js?v=20260904-t57";

const SEVERITY_ORDER = { error: 0, warn: 1, info: 2 };
const SEVERITY_ICON = { error: "alert-circle", warn: "alert-triangle", info: "info-circle" };

function fieldBadge(row, field) {
  const findings = row.findings_by_field?.[field] || [];
  if (!findings.length) return "";
  const worst = findings.reduce(
    (acc, f) => (SEVERITY_ORDER[f.severity] < SEVERITY_ORDER[acc] ? f.severity : acc),
    "info",
  );
  return `<span class="pi-badge pi-badge--${escapeHtml(worst)}" data-field="${escapeHtml(field)}">${icon(SEVERITY_ICON[worst], "icon icon--tiny")}${findings.length}</span>`;
}

function fieldCell(value, row, field) {
  const text = value === null || value === undefined
    ? `<span class="pi-cell-muted">not checked</span>`
    : value === "" ? `<span class="pi-cell-empty">empty</span>` : escapeHtml(value);
  return `<td class="pi-qa-cell">${text}${fieldBadge(row, field)}</td>`;
}

function findingsDetailMarkup(row) {
  const fields = ["title", "description", "keywords", "h1", "url", "page"];
  const groups = fields
    .map((field) => ({ field, findings: row.findings_by_field?.[field] || [] }))
    .filter((g) => g.findings.length);
  if (!groups.length) {
    return `<p class="pi-qa-detail-empty">${icon("circle-check")}No findings for this page.</p>`;
  }
  return groups.map((group) => `
    <div class="pi-qa-detail-group">
      <p class="pi-qa-detail-field">${escapeHtml(group.field)}</p>
      <ul>${group.findings.map((f) => `<li class="pi-badge pi-badge--${escapeHtml(f.severity)}-inline">${icon(SEVERITY_ICON[f.severity], "icon icon--tiny")}${escapeHtml(f.detail)}</li>`).join("")}</ul>
    </div>`).join("");
}

function qaRow(row, expanded) {
  const isExpanded = expanded === row.url;
  const rowClasses = [rowStatusClass(row), isExpanded ? "pi-row--expanded" : ""].filter(Boolean).join(" ");
  const worst = row.worst_severity;
  return `
    <tr class="${rowClasses}" data-row-url="${escapeHtml(row.url)}">
      <td>
        <button type="button" class="pi-row-toggle" data-action="toggle-row" aria-expanded="${String(isExpanded)}">
          ${icon(isExpanded ? "chevron-down" : "chevron-right", "icon icon--tiny")}${escapeHtml(row.page_label || row.url)}
        </button>
      </td>
      <td><a href="${escapeHtml(row.url)}" target="_blank" rel="noopener">${escapeHtml(row.url)}</a></td>
      <td>${escapeHtml(breadcrumbText(row)) || "—"}</td>
      ${fieldCell(row.title, row, "title")}
      ${fieldCell(row.description, row, "description")}
      ${fieldCell(row.keywords?.length ? row.keywords.join(", ") : "", row, "keywords")}
    </tr>
    ${isExpanded ? `<tr class="pi-qa-detail-row"><td colspan="6">${findingsDetailMarkup(row)}</td></tr>` : ""}`;
}

export function renderClusterSummary(clusters) {
  if (!clusters.length) return "";
  const top = clusters.slice(0, 8);
  return `
    <div class="pi-cluster-summary">
      <p class="pi-cluster-summary__label">${icon("copy")}${clusters.length} duplicate group${clusters.length === 1 ? "" : "s"}</p>
      <div class="pi-cluster-chips">${top.map((c) => `<span class="pi-cluster-chip" title="${escapeHtml(c.members.join(", "))}">${c.members.length} pages · ${escapeHtml(c.field)}</span>`).join("")}${clusters.length > top.length ? `<span class="pi-cluster-chip pi-cluster-chip--more">+${clusters.length - top.length} more</span>` : ""}</div>
    </div>`;
}

export function renderProfileToggle(profile) {
  return `
    <div class="pi-profile-toggle" role="group" aria-label="QA strictness">
      <button type="button" class="button button--quiet ${profile === "calibrated" ? "pi-profile-active" : ""}" data-action="profile-calibrated" aria-pressed="${String(profile === "calibrated")}">Calibrated</button>
      <button type="button" class="button button--quiet ${profile === "strict" ? "pi-profile-active" : ""}" data-action="profile-strict" aria-pressed="${String(profile === "strict")}">Strict</button>
    </div>
    <p class="pi-profile-note">${profile === "calibrated"
      ? "Duplicate groups collapse to one finding per page; a short title/description is a note, not a warning."
      : "Every rule fires exactly as the underlying content-QA engine reports it, including one finding per pair of duplicate pages."}</p>`;
}

export function renderQaTab(container, { rows, clusters, profile, expandedUrl }) {
  const clusterPanel = renderClusterSummary(clusters);
  if (!rows.length) {
    container.innerHTML = `${clusterPanel}<div class="state-panel">${icon("table-off")}<p><strong>No pages match this filter.</strong></p></div>`;
    return;
  }
  container.innerHTML = `
    ${clusterPanel}
    <div class="table-scroll pi-table-scroll">
      <table class="analytics-table pi-table pi-table--qa">
        <caption class="visually-hidden">Page-level SEO and AI-slop findings, one row per page</caption>
        <thead><tr>
          <th scope="col">Pages</th>
          <th scope="col">URL</th>
          <th scope="col">Breadcrumbs</th>
          <th scope="col">Title</th>
          <th scope="col">Description</th>
          <th scope="col">Keywords</th>
        </tr></thead>
        <tbody>${rows.map((row) => qaRow(row, expandedUrl)).join("")}</tbody>
      </table>
    </div>`;
}
