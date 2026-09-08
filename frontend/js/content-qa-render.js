/* Shared findings rendering for Strategy Check + Framework Check (task-53,
 * extended task-87). One engine, two thin tool pages — this module is the
 * part they would otherwise duplicate: report header, scope block, severity
 * summary, filter/sort/group controls, the findings list, copy-to-clipboard,
 * the reports list. Both `ContentQaReportDTO` responses (strategy and
 * framework) share the exact same shape, so one renderer covers both.
 *
 * task-87: a real report can carry 2000+ findings where 70%+ share one
 * `check` (measured live on LAXTON_SEO_STRATEGY: 1514/2066 were
 * `meta-desc-duplicate`) — three severity checkboxes were not enough to
 * find the other 27%. `wireReport` now keeps the full findings array in a
 * closure and re-renders only the findings area (`#cq-groups`) when the
 * filter/sort/group state changes, never the whole panel.
 */
import { escapeHtml, formatTimestamp, icon } from "./formatters.js?v=20260908-t87";
import { buildToolsHash } from "./router.js?v=20260908-t87";

export const SEVERITY_META = {
  error: { label: "Error", icon: "alert-triangle", cls: "cq-sev--error" },
  warn: { label: "Warning", icon: "alert-circle", cls: "cq-sev--warn" },
  info: { label: "Info", icon: "info-circle", cls: "cq-sev--info" },
};
const SEVERITY_ORDER = ["error", "warn", "info"];
const SEVERITY_RANK = Object.fromEntries(SEVERITY_ORDER.map((s, i) => [s, i]));

const KIND_LABEL = { page: "Page", blog: "Blog" };
const LOCALE_LABEL = { "en-US": "American English", "en-GB": "British English", "sv-SE": "Swedish" };

const SHEET_STATE_META = {
  checked: { icon: "circle-check", cls: "cq-sheet--checked", label: "Checked" },
  not_selected: { icon: "circle-dashed", cls: "cq-sheet--not-selected", label: "Not selected" },
  no_recognized_columns: { icon: "table-off", cls: "cq-sheet--unrecognized", label: "No title/description columns" },
};

const GROUP_LABELS = { severity: "Severity", check: "Check type", sheet: "Sheet", none: "Flat list" };
const SORT_LABELS = { severity: "Severity", check: "Check A→Z", count: "Count", document: "Document order" };

const NO_SHEET = "__none__"; // findings whose ref carries no "Sheet!row N" prefix (DOCX, or malformed ref)
const DEFAULT_GROUP_LIMIT = 50;

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

/** First ref's sheet name from `"Sheet!row 3"` / `"Sheet!row 3, Other!row 9"`
 * — `null` when the ref carries no "Sheet!" prefix (Framework Check/DOCX has
 * no sheets, and a cross-page finding shows under its first-listed sheet). */
function sheetOfRef(ref) {
  const first = String(ref || "").split(",")[0].trim();
  const bang = first.indexOf("!");
  return bang > -1 ? first.slice(0, bang) : null;
}

/** Full results panel: header (filename, when, profile, shareable link),
 * scope block (task-87: which sheets were actually checked), summary chips,
 * filter/sort/group controls, findings list. Caller owns the surrounding
 * `<section>`/heading; call `wireReport(container, report)` once this
 * markup is in the DOM. */
export function renderReport(report, { toolKey } = {}) {
  const findings = report.findings || [];
  const skipped = report.skipped_sheets || [];
  const sheets = report.sheets || [];
  return `
    <div class="cq-report" data-testid="cq-report">
      ${renderHeader(report, { toolKey })}
      ${sheets.length ? renderScope(sheets) : (skipped.length ? renderSkipped(skipped) : "")}
      <ul class="cq-summary">${renderSummary(report)}</ul>
      ${findings.length ? renderControlsShell() : renderClean()}
      <div id="cq-groups"></div>
    </div>`;
}

function renderHeader(report, { toolKey }) {
  const profile = report.profile || {};
  const chips = [
    profile.kind ? KIND_LABEL[profile.kind] || profile.kind : null,
    profile.locale ? LOCALE_LABEL[profile.locale] || profile.locale : null,
    profile.cross_sheet === false ? "Per-sheet comparison" : null,
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

/** task-87 §1.3: which sheets were checked, which the operator left out on
 * purpose, and which had no recognized columns — three different things
 * `skipped_sheets` alone could not say apart. Not a findings list: leaving
 * a sheet unselected is the operator's choice, not a defect of the file. */
function renderScope(sheets) {
  return `
    <div class="cq-scope">
      <p class="cq-scope__label">${icon("list-check", "icon icon--tiny")}<span>Sheets in this run</span></p>
      <ul class="cq-scope__list">
        ${sheets.map((s) => {
          const meta = SHEET_STATE_META[s.state] || SHEET_STATE_META.no_recognized_columns;
          return `
            <li class="cq-scope__item ${meta.cls}">
              ${icon(meta.icon, "icon icon--tiny")}
              <span class="cq-scope__name">${escapeHtml(s.name)}</span>
              <span class="cq-scope__detail">${s.item_count} row${s.item_count === 1 ? "" : "s"} — ${meta.label}</span>
            </li>`;
        }).join("")}
      </ul>
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

function renderClean() {
  return `
    <p class="cq-clean">${icon("circle-check")}<span>No issues found by the current rule set.</span></p>`;
}

/** Static shell for the filter/sort/group toolbar — the actual chips (which
 * depend on what checks/sheets exist in THIS report) are filled in by
 * `wireReport` once it has the findings array. Kept in the initial HTML
 * (rather than built as a string in JS-land) so the panel has a stable
 * skeleton the moment `renderReport()`'s output lands in the DOM. */
function renderControlsShell() {
  return `
    <div class="cq-controls" id="cq-controls">
      <div class="cq-controls__row">
        <div class="cq-filter" role="group" aria-label="Filter findings by severity" id="cq-severity-filter">
          ${SEVERITY_ORDER.map((sev) => {
            const meta = SEVERITY_META[sev];
            return `
              <label class="cq-filter__option ${meta.cls}">
                <input type="checkbox" data-cq-severity="${sev}" checked>
                ${icon(meta.icon, "icon icon--tiny")}<span>${meta.label}</span>
              </label>`;
          }).join("")}
        </div>
        <label class="cq-search">
          ${icon("search", "icon icon--tiny")}
          <input type="search" id="cq-search" placeholder="Filter by text, ref or check id…" aria-label="Search findings">
        </label>
      </div>
      <div class="cq-controls__row">
        <div class="cq-checkchips" id="cq-check-chips" role="group" aria-label="Filter findings by check type"></div>
      </div>
      <div class="cq-controls__row cq-controls__row--group" id="cq-sheet-chips-row" hidden>
        <div class="cq-checkchips" id="cq-sheet-chips" role="group" aria-label="Filter findings by sheet"></div>
      </div>
      <div class="cq-controls__row cq-controls__row--group">
        <label class="form-field form-field--inline">Group by
          <select id="cq-group-by">
            ${Object.entries(GROUP_LABELS).map(([v, label]) => `<option value="${v}">${escapeHtml(label)}</option>`).join("")}
          </select>
        </label>
        <label class="form-field form-field--inline">Sort
          <select id="cq-sort">
            ${Object.entries(SORT_LABELS).map(([v, label]) => `<option value="${v}">${escapeHtml(label)}</option>`).join("")}
          </select>
        </label>
        <button class="button button--quiet" type="button" id="cq-clear-filters">Clear filters</button>
      </div>
    </div>`;
}

function checkCounts(findings) {
  const counts = new Map();
  for (const f of findings) counts.set(f.check, (counts.get(f.check) || 0) + 1);
  return [...counts.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
}

function distinctSheets(findings) {
  const sheets = new Map(); // name -> count, NO_SHEET last
  for (const f of findings) {
    const sheet = sheetOfRef(f.ref) ?? NO_SHEET;
    sheets.set(sheet, (sheets.get(sheet) || 0) + 1);
  }
  return sheets;
}

function matchesQuery(finding, query) {
  if (!query) return true;
  const haystack = `${finding.check} ${finding.detail} ${finding.ref} ${finding.value || ""}`.toLowerCase();
  return haystack.includes(query);
}

function visibleFindings(findings, filterState) {
  return findings.filter((f) => {
    if (!filterState.severities.has(f.severity)) return false;
    if (!filterState.checks.has(f.check)) return false;
    const sheet = sheetOfRef(f.ref) ?? NO_SHEET;
    if (!filterState.sheets.has(sheet)) return false;
    return matchesQuery(f, filterState.query);
  });
}

function checkFrequencyRank(findings) {
  const counts = new Map();
  for (const f of findings) counts.set(f.check, (counts.get(f.check) || 0) + 1);
  return counts;
}

function sortWithinGroup(items, sort, frequency) {
  const sorted = [...items];
  if (sort === "severity") {
    sorted.sort((a, b) => SEVERITY_RANK[a.severity] - SEVERITY_RANK[b.severity] || a.check.localeCompare(b.check));
  } else if (sort === "check") {
    sorted.sort((a, b) => a.check.localeCompare(b.check));
  } else if (sort === "count") {
    sorted.sort((a, b) => (frequency.get(b.check) || 0) - (frequency.get(a.check) || 0) || a.check.localeCompare(b.check));
  }
  // "document" — залишає порядок відповіді API як є.
  return sorted;
}

/** Ділить видимі findings на секції за `groupBy` і сортує і секції, і
 * findings усередині кожної за `sort` — та сама пара опцій, що керує
 * панеллю (task-87 §1.4). `groupBy:"none"` дає один "flat" список. */
function groupFindings(items, groupBy, sort) {
  const frequency = checkFrequencyRank(items);
  if (groupBy === "none") {
    return [{ key: "flat", label: null, items: sortWithinGroup(items, sort, frequency) }];
  }
  const keyOf = groupBy === "check" ? (f) => f.check
    : groupBy === "sheet" ? (f) => sheetOfRef(f.ref) ?? NO_SHEET
    : (f) => f.severity;
  const buckets = new Map();
  for (const item of items) {
    const key = keyOf(item);
    if (!buckets.has(key)) buckets.set(key, []);
    buckets.get(key).push(item);
  }
  let groups = [...buckets.entries()].map(([key, rows]) => ({
    key, label: groupLabel(groupBy, key), items: sortWithinGroup(rows, sort, frequency),
  }));
  if (groupBy === "severity") {
    groups.sort((a, b) => SEVERITY_RANK[a.key] - SEVERITY_RANK[b.key]);
  } else if (sort === "count") {
    groups.sort((a, b) => b.items.length - a.items.length);
  } else {
    groups.sort((a, b) => String(a.label).localeCompare(String(b.label)));
  }
  return groups;
}

function groupLabel(groupBy, key) {
  if (groupBy === "severity") return SEVERITY_META[key]?.label || key;
  if (groupBy === "sheet") return key === NO_SHEET ? "No sheet" : key;
  return key; // check id — shown verbatim, it's already the vocabulary operators search for
}

function groupIcon(groupBy, key, items) {
  if (groupBy === "severity") return SEVERITY_META[key]?.icon || "circle";
  if (groupBy === "sheet") return key === NO_SHEET ? "file-text" : "table";
  // "check" — colour by the group's dominant/most-severe finding, same visual language as severity groups
  const worst = SEVERITY_ORDER.find((sev) => items.some((f) => f.severity === sev));
  return SEVERITY_META[worst || "info"].icon;
}

function groupClass(groupBy, key, items) {
  if (groupBy === "severity") return SEVERITY_META[key]?.cls || "";
  const worst = SEVERITY_ORDER.find((sev) => items.some((f) => f.severity === sev));
  return SEVERITY_META[worst || "info"].cls;
}

/** Renders the findings area for the current filter/sort/group state.
 * Groups over `limit` render truncated with a "Show all N" control — a
 * 2000+-finding report (measured live: 73% one check) must not put 2000
 * `<li>` nodes in the DOM on first paint (AGENTS.md §13). */
function renderFindingsArea(report, filterState, expanded) {
  const all = report.findings || [];
  const visible = visibleFindings(all, filterState);
  if (all.length && !visible.length) {
    return renderNoMatches();
  }
  const groups = groupFindings(visible, filterState.groupBy, filterState.sort);
  return `
    <div class="cq-groups">
      ${groups.map((g) => renderGroupSection(g, filterState.groupBy, expanded)).join("")}
    </div>`;
}

function renderNoMatches() {
  // Deliberately different copy from renderClean(): a filter hiding every
  // row is not the same claim as "the file has no issues" (AGENTS.md §6).
  return `
    <div class="cq-no-matches">
      ${icon("filter-x")}
      <p><strong>No findings match the current filters.</strong></p>
      <button class="button button--quiet" type="button" data-cq-clear-inline">Clear filters</button>
    </div>`;
}

function renderGroupSection(group, groupBy, expanded) {
  const isFlat = group.key === "flat";
  const cls = isFlat ? "" : groupClass(groupBy, group.key, group.items);
  const isExpanded = expanded.has(group.key);
  const shown = isExpanded ? group.items : group.items.slice(0, DEFAULT_GROUP_LIMIT);
  const hiddenCount = group.items.length - shown.length;
  return `
    <section class="cq-group ${cls}" data-cq-group-key="${escapeHtml(String(group.key))}">
      ${isFlat ? "" : `
        <h3 class="cq-group__title">${icon(groupIcon(groupBy, group.key, group.items), "icon icon--tiny")}${escapeHtml(String(group.label))}
          <span class="cq-group__count">${group.items.length}</span></h3>`}
      <ul class="cq-group__list">
        ${shown.map(renderFinding).join("")}
      </ul>
      ${hiddenCount > 0 ? `
        <button class="button button--quiet cq-group__more" type="button" data-cq-show-all="${escapeHtml(String(group.key))}">
          Show all ${group.items.length} (${hiddenCount} more)
        </button>` : ""}
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

function renderCheckChips(counts, activeChecks) {
  return counts.map(([check, count]) => `
    <button type="button" class="cq-chip${activeChecks.has(check) ? " cq-chip--active" : ""}" data-cq-check-chip="${escapeHtml(check)}">
      <span class="cq-chip__label">${escapeHtml(check)}</span><span class="cq-chip__count">${count}</span>
    </button>`).join("")
    + `<button type="button" class="button button--quiet cq-chip__all" data-cq-checks-all>All</button>`
    + `<button type="button" class="button button--quiet cq-chip__all" data-cq-checks-none>None</button>`;
}

function renderSheetChips(sheetCounts, activeSheets) {
  return [...sheetCounts.entries()].map(([sheet, count]) => `
    <button type="button" class="cq-chip${activeSheets.has(sheet) ? " cq-chip--active" : ""}" data-cq-sheet-chip="${escapeHtml(sheet)}">
      <span class="cq-chip__label">${sheet === NO_SHEET ? "No sheet" : escapeHtml(sheet)}</span><span class="cq-chip__count">${count}</span>
    </button>`).join("");
}

/** Attach filter/sort/group/copy handlers and do the first render of the
 * findings area. Call once after `renderReport()` output is in the DOM,
 * passing the SAME `report` object. Event delegation on `container` — one
 * listener per panel, not one per row (AGENTS.md §13). */
export function wireReport(container, report) {
  const findings = report?.findings || [];
  const groupsMount = container.querySelector("#cq-groups");
  if (!groupsMount) return; // clean report — no controls, nothing to wire

  const checkCountsList = checkCounts(findings);
  const sheetCounts = distinctSheets(findings);
  const hasSheetDimension = sheetCounts.size > 1 || (sheetCounts.size === 1 && !sheetCounts.has(NO_SHEET));

  const filterState = {
    severities: new Set(SEVERITY_ORDER),
    checks: new Set(checkCountsList.map(([check]) => check)),
    sheets: new Set(sheetCounts.keys()),
    query: "",
    groupBy: "severity",
    sort: "severity",
  };
  const expanded = new Set();

  function rerender() {
    groupsMount.innerHTML = renderFindingsArea(report, filterState, expanded);
    const checkChipsMount = container.querySelector("#cq-check-chips");
    if (checkChipsMount) checkChipsMount.innerHTML = renderCheckChips(checkCountsList, filterState.checks);
    const sheetChipsRow = container.querySelector("#cq-sheet-chips-row");
    if (sheetChipsRow) {
      sheetChipsRow.hidden = !hasSheetDimension;
      const sheetChipsMount = container.querySelector("#cq-sheet-chips");
      if (sheetChipsMount && hasSheetDimension) {
        sheetChipsMount.innerHTML = renderSheetChips(sheetCounts, filterState.sheets);
      }
    }
  }

  if (findings.length) rerender();

  container.addEventListener("change", (event) => {
    if (event.target.matches("[data-cq-severity]")) {
      const sev = event.target.dataset.cqSeverity;
      if (event.target.checked) filterState.severities.add(sev); else filterState.severities.delete(sev);
      rerender();
    } else if (event.target.id === "cq-group-by") {
      filterState.groupBy = event.target.value;
      expanded.clear();
      rerender();
    } else if (event.target.id === "cq-sort") {
      filterState.sort = event.target.value;
      rerender();
    }
  });

  let searchDebounce = null;
  container.addEventListener("input", (event) => {
    if (event.target.id !== "cq-search") return;
    window.clearTimeout(searchDebounce);
    searchDebounce = window.setTimeout(() => {
      filterState.query = event.target.value.trim().toLowerCase();
      rerender();
    }, 150);
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

    const checkChip = event.target.closest("[data-cq-check-chip]");
    if (checkChip) {
      const check = checkChip.dataset.cqCheckChip;
      if (filterState.checks.has(check)) filterState.checks.delete(check); else filterState.checks.add(check);
      rerender();
      return;
    }
    if (event.target.closest("[data-cq-checks-all]")) {
      filterState.checks = new Set(checkCountsList.map(([check]) => check));
      rerender();
      return;
    }
    if (event.target.closest("[data-cq-checks-none]")) {
      filterState.checks = new Set();
      rerender();
      return;
    }

    const sheetChip = event.target.closest("[data-cq-sheet-chip]");
    if (sheetChip) {
      const sheet = sheetChip.dataset.cqSheetChip;
      if (filterState.sheets.has(sheet)) filterState.sheets.delete(sheet); else filterState.sheets.add(sheet);
      rerender();
      return;
    }

    const showAll = event.target.closest("[data-cq-show-all]");
    if (showAll) {
      expanded.add(showAll.dataset.cqShowAll);
      rerender();
      return;
    }

    if (event.target.closest("#cq-clear-filters") || event.target.closest("[data-cq-clear-inline]")) {
      filterState.severities = new Set(SEVERITY_ORDER);
      filterState.checks = new Set(checkCountsList.map(([check]) => check));
      filterState.sheets = new Set(sheetCounts.keys());
      filterState.query = "";
      const searchInput = container.querySelector("#cq-search");
      if (searchInput) searchInput.value = "";
      container.querySelectorAll("[data-cq-severity]").forEach((el) => { el.checked = true; });
      rerender();
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

/** Client autofill for the Strategy/Framework Check forms (task-87 §1.6).
 * Same prefix-match heuristic as `guessClient()` in `alt-text-tool.js`
 * (deliberately duplicated, not imported — that file belongs to task-27's
 * owned paths; this ~20-line pure function is small enough that a
 * cross-task import would trade a well-known duplication for a coupling
 * that would break silently on the next unrelated refactor of that tool).
 * Update both places together if the heuristic ever changes. */
export function guessContentQaClient(filename, clients) {
  const normalize = (value) => String(value).toLowerCase().replace(/[^a-z0-9]+/g, "");
  const stem = normalize(String(filename).replace(/\.[^.]+$/, ""));
  if (!stem) return null;
  const candidatesFor = (client) =>
    [client.name, String(client.domain || "").replace(/\.[a-z.]+$/, "")]
      .map(normalize)
      .filter((value) => value.length >= 3);

  let best = null;
  let bestLength = 0;
  clients.forEach((client) => {
    candidatesFor(client).forEach((candidate) => {
      if (stem.startsWith(candidate) && candidate.length > bestLength) {
        best = client;
        bestLength = candidate.length;
      }
    });
  });
  return best;
}

// TLD -> locale autofill (task-87 §1.6). Deliberately narrow: the portfolio
// has exactly one non-English-TLD client (`laxton.se`) today, so a fuller
// country->locale registry would be a guess dressed as a decision. Add a
// row here the day a second one shows up (task-87 §7 follow-ups).
const TLD_LOCALE = { ".se": "sv-SE" };
const DEFAULT_LOCALE = "en-US";

export function guessLocaleForDomain(domain) {
  const host = String(domain || "").toLowerCase();
  for (const [tld, locale] of Object.entries(TLD_LOCALE)) {
    if (host.endsWith(tld)) return locale;
  }
  return DEFAULT_LOCALE;
}
