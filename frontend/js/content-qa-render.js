/* Shared findings rendering for Strategy Check + Framework Check (task-53).
 *
 * One engine, two thin tool pages — this module is the part they would
 * otherwise duplicate: report header, severity summary, filter toggles, the
 * findings list, copy-to-clipboard, the reports list. Both `ContentQaReportDTO`
 * responses (strategy and framework) share the exact same shape, so one
 * renderer covers both.
 */
import { escapeHtml, formatTimestamp, icon } from "./formatters.js?v=20260821-spin3";
import { buildToolsHash } from "./router.js?v=20260821-spin3";

export const SEVERITY_META = {
  error: { label: "Error", icon: "alert-triangle", cls: "cq-sev--error" },
  warn: { label: "Warning", icon: "alert-circle", cls: "cq-sev--warn" },
  info: { label: "Info", icon: "info-circle", cls: "cq-sev--info" },
};
const SEVERITY_ORDER = ["error", "warn", "info"];

const KIND_LABEL = { page: "Page", blog: "Blog" };
const LOCALE_LABEL = { "en-US": "American English", "en-GB": "British English" };

function toast(message) {
  const region = document.querySelector("#toast-region");
  if (!region) return;
  const element = document.createElement("div");
  element.className = "toast-message";
  element.textContent = message;
  region.append(element);
  setTimeout(() => element.remove(), 4200);
}

async function copyText(value) {
  try {
    await navigator.clipboard.writeText(value);
    return true;
  } catch {
    toast("Could not copy — the clipboard is unavailable in this browser.");
    return false;
  }
}

function flashCopied(button) {
  const original = button.innerHTML;
  button.innerHTML = icon("copy-check", "icon icon--tiny");
  setTimeout(() => { button.innerHTML = original; }, 1400);
}

/** Full results panel: header (filename, when, profile, shareable link),
 * summary chips, severity filter, skipped-sheets notice, findings list.
 * Caller owns the surrounding `<section>`/heading. */
export function renderReport(report, { toolKey } = {}) {
  const findings = report.findings || [];
  const skipped = report.skipped_sheets || [];
  return `
    <div class="cq-report" data-testid="cq-report">
      ${renderHeader(report, { toolKey })}
      <ul class="cq-summary">${renderSummary(report)}</ul>
      ${skipped.length ? renderSkipped(skipped) : ""}
      ${findings.length ? renderFilterBar() : ""}
      ${findings.length ? renderList(findings) : renderClean()}
    </div>`;
}

function renderHeader(report, { toolKey }) {
  const profile = report.profile || {};
  const chips = [
    profile.kind ? KIND_LABEL[profile.kind] || profile.kind : null,
    profile.locale ? LOCALE_LABEL[profile.locale] || profile.locale : null,
    profile.brand_names && profile.brand_names.length
      ? `Brand: ${profile.brand_names.join(", ")}` : null,
  ].filter(Boolean);
  return `
    <div class="cq-report__meta">
      <span class="cq-report__file">${icon("file-text", "icon icon--tiny")}${escapeHtml(report.source_filename)}</span>
      <span class="cq-report__count">${report.item_count} item${report.item_count === 1 ? "" : "s"} checked</span>
      ${report.client_name ? `<span class="cq-report__client">${icon("building-store", "icon icon--tiny")}${escapeHtml(report.client_name)}</span>` : ""}
      ${report.created_at ? `<span class="cq-report__date">${escapeHtml(formatTimestamp(report.created_at))}</span>` : ""}
      ${chips.length ? `<span class="cq-report__profile">${chips.map(escapeHtml).join(" · ")}</span>` : ""}
      ${report.id && toolKey ? `
        <button class="button button--quiet cq-report__link" type="button" data-cq-copy-link="${escapeHtml(buildToolsHash(toolKey, report.id))}">
          ${icon("link", "icon icon--tiny")}Copy link
        </button>` : ""}
    </div>`;
}

function renderSummary(report) {
  const counts = report.severity_counts || {};
  return SEVERITY_ORDER.map((sev) => {
    const meta = SEVERITY_META[sev];
    const n = counts[sev] || 0;
    return `
      <li class="cq-summary__item ${meta.cls}">
        ${icon(meta.icon, "icon icon--tiny")}<strong>${n}</strong><span>${meta.label}${n === 1 ? "" : "s"}</span>
      </li>`;
  }).join("");
}

function renderSkipped(skipped) {
  return `
    <p class="cq-skipped">${icon("table-off", "icon icon--tiny")}
      <span>Not checked — no recognized title/description columns: ${skipped.map((s) => `<strong>${escapeHtml(s)}</strong>`).join(", ")}.</span>
    </p>`;
}

function renderFilterBar() {
  return `
    <div class="cq-filter" role="group" aria-label="Filter findings by severity">
      ${SEVERITY_ORDER.map((sev) => {
        const meta = SEVERITY_META[sev];
        return `
          <label class="cq-filter__option ${meta.cls}">
            <input type="checkbox" data-cq-filter="${sev}" checked>
            ${icon(meta.icon, "icon icon--tiny")}<span>${meta.label}</span>
          </label>`;
      }).join("")}
    </div>`;
}

function renderClean() {
  return `
    <p class="cq-clean">${icon("circle-check")}<span>No issues found by the current rule set.</span></p>`;
}

function renderList(findings) {
  const bySeverity = SEVERITY_ORDER.map((sev) => [sev, findings.filter((f) => f.severity === sev)])
    .filter(([, rows]) => rows.length);
  return `
    <div class="cq-groups" id="cq-groups">
      ${bySeverity.map(([sev, rows]) => renderGroup(sev, rows)).join("")}
    </div>`;
}

function renderGroup(severity, rows) {
  const meta = SEVERITY_META[severity];
  return `
    <section class="cq-group ${meta.cls}" data-cq-group="${severity}">
      <h3 class="cq-group__title">${icon(meta.icon, "icon icon--tiny")}${meta.label} <span class="cq-group__count">${rows.length}</span></h3>
      <ul class="cq-group__list">
        ${rows.map(renderFinding).join("")}
      </ul>
    </section>`;
}

function renderFinding(finding) {
  return `
    <li class="cq-finding">
      <div class="cq-finding__head">
        <code class="cq-finding__check">${escapeHtml(finding.check)}</code>
        <button class="icon-button" type="button" data-cq-copy="${escapeHtml(finding.ref)}"
          aria-label="${escapeHtml(`Copy reference ${finding.ref}`)}">${icon("copy", "icon icon--tiny")}</button>
      </div>
      <p class="cq-finding__ref">${escapeHtml(finding.ref)}</p>
      <p class="cq-finding__detail">${escapeHtml(finding.detail)}</p>
      ${finding.value ? renderFindingValue(finding.value) : ""}
    </li>`;
}

function renderFindingValue(value) {
  return `
    <div class="cq-finding__value">
      <blockquote>${escapeHtml(value)}</blockquote>
      <button class="icon-button" type="button" data-cq-copy="${escapeHtml(value)}"
        aria-label="${escapeHtml(`Copy value: ${value}`)}">${icon("copy", "icon icon--tiny")}</button>
    </div>`;
}

/** Attach filter + copy handlers. Call once after `renderReport()` output is
 * in the DOM. Event delegation on `container` — one listener per panel, not
 * one per row (AGENTS.md §13). */
export function wireReport(container) {
  container.querySelectorAll("[data-cq-filter]").forEach((checkbox) => {
    checkbox.addEventListener("change", () => applyFilter(container));
  });
  container.addEventListener("click", (event) => {
    const linkButton = event.target.closest("[data-cq-copy-link]");
    if (linkButton) {
      void (async () => {
        const url = `${location.origin}${location.pathname}${linkButton.dataset.cqCopyLink}`;
        if (!(await copyText(url))) return;
        toast("Link copied");
      })();
      return;
    }
    const button = event.target.closest("[data-cq-copy]");
    if (!button) return;
    void (async () => {
      if (!(await copyText(button.dataset.cqCopy))) return;
      flashCopied(button);
      toast("Copied");
    })();
  });
}

function applyFilter(container) {
  const active = new Set(
    Array.from(container.querySelectorAll("[data-cq-filter]:checked")).map((el) => el.dataset.cqFilter)
  );
  container.querySelectorAll("[data-cq-group]").forEach((group) => {
    group.hidden = !active.has(group.dataset.cqGroup);
  });
}

export function renderLoading() {
  return `<p class="cq-loading">${icon("loader-2", "icon icon--spin")}<span>Checking the file…</span></p>`;
}

export function renderError(error) {
  return `
    <div class="state-panel state-failed">
      ${icon("wifi-off")}
      <p><strong>Could not run the check.</strong><br>${escapeHtml(error.message)}</p>
    </div>`;
}

/** Past reports table for the tool's landing view — mirrors alt-text-tool.js's
 * `reportRow`. `toolKey` builds the `#/tools/<key>/<id>` link for each row. */
export function renderReportsList(reports, toolKey) {
  if (!reports.length) {
    return `
      <p class="tool-empty">${icon("file-off")}<span>No reports yet. Run a check above to create the first one.</span></p>`;
  }
  return `
    <div class="table-scroll">
      <table class="tool-table">
        <caption class="visually-hidden">Past reports</caption>
        <thead><tr>
          <th scope="col">File</th><th scope="col">Client</th>
          <th scope="col">Findings</th><th scope="col">Checked</th>
          <th scope="col"><span class="visually-hidden">Open</span></th>
        </tr></thead>
        <tbody>${reports.map((r) => renderReportRow(r, toolKey)).join("")}</tbody>
      </table>
    </div>`;
}

function renderReportRow(report, toolKey) {
  const counts = report.severity_counts || {};
  const href = buildToolsHash(toolKey, report.id);
  const badges = SEVERITY_ORDER.filter((sev) => counts[sev]).map((sev) => {
    const meta = SEVERITY_META[sev];
    return `<span class="cq-row-badge ${meta.cls}">${counts[sev]} ${meta.label.toLowerCase()}</span>`;
  }).join("") || `<span class="muted">No issues</span>`;
  return `
    <tr>
      <td class="tool-table__main">
        <a href="${href}">${escapeHtml(report.source_filename)}</a>
      </td>
      <td>${report.client_name
        ? `<span class="report-row__client">${icon("building-store", "icon icon--tiny")}${escapeHtml(report.client_name)}</span>`
        : '<span class="muted">No client</span>'}</td>
      <td>${badges}</td>
      <td class="report-row__date">${escapeHtml(formatTimestamp(report.created_at, "—"))}</td>
      <td class="tool-table__actions">
        <a class="button button--quiet" href="${href}">Open${icon("arrow-right", "icon icon--tiny")}</a>
      </td>
    </tr>`;
}
