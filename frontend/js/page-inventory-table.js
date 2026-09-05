// Page Inventory (task-57) — "Inventory" tab: the plain architecture table
// (`Pages | URL | Breadcrumbs | Title | Description | Keywords`, the exact
// column order from the operator's own screenshot). No QA badges here —
// that's `page-inventory-qa.js`'s job; this module only knows how to show
// what the site actually has.

import { escapeHtml, icon } from "./formatters.js?v=20260821-spin3";

const STATUS_LABELS = {
  live: "Live", broken: "Broken", redirect: "Redirect",
  noindex: "Noindex", not_checked: "Not checked",
};

// `""` here — never `row.page_label` — when there is no REAL breadcrumb
// trail (`breadcrumb_source === "none"`): a URL-derived page name belongs
// in the "Pages" column, not disguised as a breadcrumb path this row does
// not actually have (task-57 live report 2026-09-04, client GOREPORT).
export function breadcrumbText(row) {
  if (row.breadcrumbs && row.breadcrumbs.length) {
    return row.breadcrumbs.map((node) => node.name).join(" > ");
  }
  return "";
}

export function rowStatusClass(row) {
  if (row.duplicate_urls && row.duplicate_urls.length) return "pi-row--duplicate";
  if (row.canonical_missing) return "pi-row--warn";
  if (row.status_label === "broken" || row.status_label === "redirect") return "pi-row--broken";
  if (row.status_label === "not_checked") return "pi-row--pending";
  return "";
}

function fieldValue(value, { emptyLabel = "—" } = {}) {
  if (value === null || value === undefined) return `<span class="pi-cell-muted">not checked</span>`;
  if (value === "") return `<span class="pi-cell-empty">${escapeHtml(emptyLabel)}</span>`;
  return escapeHtml(value);
}

function inventoryRow(row) {
  const statusLabel = STATUS_LABELS[row.status_label] || row.status_label;
  const keywords = row.keywords && row.keywords.length
    ? escapeHtml(row.keywords.join(", "))
    : `<span class="pi-cell-empty">no meta keywords</span>`;
  return `
    <tr class="${rowStatusClass(row)}">
      <td>${escapeHtml(row.page_label || row.url)}</td>
      <td><a href="${escapeHtml(row.url)}" target="_blank" rel="noopener">${escapeHtml(row.url)}</a>
        <span class="pi-status pi-status--${escapeHtml(row.status_label)}">${escapeHtml(statusLabel)}</span>
        ${row.duplicate_urls?.length ? `<span class="tag-new" title="${row.duplicate_urls.length} more URL(s) share this canonical">+${row.duplicate_urls.length} dup</span>` : ""}
      </td>
      <td>${escapeHtml(breadcrumbText(row)) || "—"}</td>
      <td>${fieldValue(row.title)}</td>
      <td>${fieldValue(row.description)}</td>
      <td>${keywords}</td>
    </tr>`;
}

export function renderInventoryTab(container, { rows }) {
  if (!rows.length) {
    container.innerHTML = `<div class="state-panel">${icon("table-off")}<p><strong>No pages match this filter.</strong></p></div>`;
    return;
  }
  container.innerHTML = `
    <div class="table-scroll pi-table-scroll">
      <table class="analytics-table pi-table">
        <caption class="visually-hidden">Site architecture: pages, URLs, breadcrumbs, title, description and keywords</caption>
        <thead><tr>
          <th scope="col">Pages</th>
          <th scope="col">URL</th>
          <th scope="col">Breadcrumbs</th>
          <th scope="col">Title</th>
          <th scope="col">Description</th>
          <th scope="col">Keywords</th>
        </tr></thead>
        <tbody>${rows.map(inventoryRow).join("")}</tbody>
      </table>
    </div>`;
}
