/* Schema Markup Checker (task-56): paste a URL, see what schema.org
 * structured data is already there against live Google rich-result rules,
 * get a ready-to-paste JSON-LD snippet for what's missing.
 *
 * Persisted server-side (`schema_markup_checks`), list+detail with a
 * shareable `#/tools/schema-markup/<id>` link — the same two-view shape as
 * `alt-text-tool.js`/`strategy-check-tool.js`.
 */
import {
  checkSchemaMarkup,
  getDashboard,
  getSchemaMarkupCheck,
  getSchemaMarkupChecks,
} from "./api.js?v=20260821-spin3";
import { buildToolsHash } from "./router.js?v=20260821-spin3";
import { escapeHtml, formatTimestamp, icon } from "./formatters.js?v=20260821-spin3";
import { panelSkeleton } from "./skeleton.js?v=20260821-spin3";

const TOOL_KEY = "schema-markup";
let root = null;
let clientOptions = null;

const SEVERITY_META = {
  error: { label: "Error", icon: "alert-triangle", cls: "sm-sev--error" },
  warn: { label: "Warning", icon: "alert-triangle", cls: "sm-sev--warn" },
  info: { label: "Info", icon: "help-circle", cls: "sm-sev--info" },
};
const SEVERITY_ORDER = ["error", "warn", "info"];

const VERDICT_META = {
  clean: { label: "Clean", icon: "circle-check", cls: "sm-verdict--clean" },
  has_gaps: { label: "Has gaps", icon: "alert-triangle", cls: "sm-verdict--gaps" },
  missing: { label: "No structured data", icon: "circle-x", cls: "sm-verdict--missing" },
  fetch_failed: { label: "Could not fetch", icon: "wifi-off", cls: "sm-verdict--failed" },
};

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

export async function initTool(mount, route) {
  root = mount;
  if (route.reportId) {
    await renderDetailView(route.reportId);
    return;
  }
  await renderListView();
}

async function renderListView() {
  root.innerHTML = `
    <a class="back-link" href="${buildToolsHash()}">${icon("arrow-left")}<span>All tools</span></a>
    <header class="tool-header">
      <p class="eyebrow">Tools / Schema Markup Checker</p>
      <h1>Schema markup checker</h1>
      <p class="tool-lead">Paste a page URL — see what schema.org structured data is already
        there, checked against schema.org's own vocabulary and Google's live rich-result rules,
        and get a ready-to-paste JSON-LD snippet for whatever's missing.</p>
    </header>
    <section class="tool-panel" aria-labelledby="sm-panel-title">
      <div class="section-heading"><div><h2 id="sm-panel-title">Check a page</h2>
        <p class="tool-panel__hint">One HTTP GET to the page, no login required — public pages only.</p></div></div>
      <div class="tool-panel__body" id="sm-form-body"></div>
    </section>
    <section class="tool-panel" id="sm-checks" aria-labelledby="sm-checks-title">
      <div class="section-heading"><div><h2 id="sm-checks-title">Checks</h2></div></div>
      <div class="tool-panel__body" id="sm-checks-body">
        ${panelSkeleton({ label: "checks", rows: 3, columns: 4 })}
      </div>
    </section>`;
  renderForm();
  await loadChecks();
}

function renderForm() {
  const body = root.querySelector("#sm-form-body");
  body.innerHTML = `
    <form id="sm-form" novalidate class="sm-fields">
      <label class="form-field">Page URL <span class="sm-required">required</span>
        <input id="sm-url" type="url" placeholder="https://example.com/blog/post" required>
      </label>
      <label class="form-field">Client <span class="sm-optional">optional — used as the Organization name</span>
        <select id="sm-client"><option value="">No client (or a prospect)</option></select>
      </label>
      <div class="sm-submit-row">
        <button class="button button--primary" type="submit">${icon("search")}Check the page</button>
      </div>
    </form>
    <div id="sm-form-error" hidden></div>`;
  body.querySelector("#sm-form").addEventListener("submit", (event) => {
    event.preventDefault();
    void submitCheck();
  });
  void fillClientOptions(body.querySelector("#sm-client"));
}

async function fillClientOptions(select) {
  if (!select) return;
  try {
    if (clientOptions === null) {
      const dashboard = await getDashboard({ period: "week" });
      clientOptions = (dashboard.clients || []).map((client) => ({
        id: client.client_id,
        name: client.name,
      }));
    }
    clientOptions.forEach((client) => {
      const option = document.createElement("option");
      option.value = client.id;
      option.textContent = client.name;
      select.append(option);
    });
  } catch {
    // Клієнт опційний: список недоступний -> перевірка все одно працює,
    // просто без client:name джерела для Organization.
  }
}

async function submitCheck() {
  const form = root.querySelector("#sm-form");
  const errorBox = root.querySelector("#sm-form-error");
  const url = root.querySelector("#sm-url").value.trim();
  const clientId = root.querySelector("#sm-client").value || null;
  errorBox.hidden = true;
  const button = form.querySelector("button[type=submit]");
  button.disabled = true;
  button.innerHTML = `${icon("loader-2", "icon icon--spin")}Checking…`;
  try {
    const check = await checkSchemaMarkup(url, clientId);
    location.hash = buildToolsHash(TOOL_KEY, check.id);
  } catch (error) {
    errorBox.hidden = false;
    errorBox.innerHTML = `
      <div class="state-panel state-failed">
        ${icon("wifi-off")}
        <p><strong>Could not check that page.</strong><br>${escapeHtml(error.message)}</p>
      </div>`;
  } finally {
    button.disabled = false;
    button.innerHTML = `${icon("search")}Check the page`;
  }
}

async function loadChecks() {
  const body = root.querySelector("#sm-checks-body");
  try {
    const page = await getSchemaMarkupChecks();
    renderChecksList(body, page.checks || []);
  } catch (error) {
    body.innerHTML = `
      <div class="state-panel state-failed">
        ${icon("wifi-off")}
        <p><strong>Could not load past checks.</strong><br>${escapeHtml(error.message)}</p>
      </div>`;
  }
}

function renderChecksList(body, checks) {
  if (!checks.length) {
    body.innerHTML = `
      <div class="state-panel">
        ${icon("braces")}
        <p>No checks yet — run one above.</p>
      </div>`;
    return;
  }
  const rows = checks.map((check) => {
    const meta = VERDICT_META[check.verdict] || VERDICT_META.missing;
    const types = (check.found_types || []).join(", ") || "—";
    return `
      <tr>
        <td><a href="${buildToolsHash(TOOL_KEY, check.id)}">${escapeHtml(check.url)}</a></td>
        <td><span class="sm-row-badge ${meta.cls}">${icon(meta.icon)}${meta.label}</span></td>
        <td>${escapeHtml(types)}</td>
        <td>${check.client_name ? escapeHtml(check.client_name) : "—"}</td>
        <td>${escapeHtml(formatTimestamp(check.created_at))}</td>
      </tr>`;
  }).join("");
  body.innerHTML = `
    <div class="table-scroll">
      <table class="sm-table">
        <thead><tr><th>URL</th><th>Verdict</th><th>Found types</th><th>Client</th><th>Checked</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;
}

async function renderDetailView(checkId) {
  root.innerHTML = `
    <a class="back-link" href="${buildToolsHash(TOOL_KEY)}">${icon("arrow-left")}<span>Back to checks</span></a>
    <div id="sm-detail">${panelSkeleton({ label: "check", rows: 5, columns: 1 })}</div>`;
  const container = root.querySelector("#sm-detail");
  try {
    const check = await getSchemaMarkupCheck(checkId);
    renderCheck(container, check);
  } catch (error) {
    container.innerHTML = `
      <div class="state-panel state-failed">
        ${icon("wifi-off")}
        <p><strong>Could not load this check.</strong><br>${escapeHtml(error.message)}</p>
      </div>`;
  }
}

function renderCheck(container, check) {
  const meta = VERDICT_META[check.verdict] || VERDICT_META.missing;
  const shareUrl = `${location.origin}${location.pathname}${buildToolsHash(TOOL_KEY, check.id)}`;
  container.innerHTML = `
    <div class="sm-report">
      <div class="sm-report__meta">
        <span class="sm-verdict ${meta.cls}">${icon(meta.icon)}${meta.label}</span>
        <span class="sm-report__url">${icon("world")}<a href="${escapeHtml(check.url)}" target="_blank" rel="noopener">${escapeHtml(check.url)}</a></span>
        ${check.page_kind ? `<span>${icon("file-text")}${escapeHtml(pageKindLabel(check.page_kind))}</span>` : ""}
        ${check.client_name ? `<span>${icon("building")}${escapeHtml(check.client_name)}</span>` : ""}
        <span>${icon("calendar")}${escapeHtml(formatTimestamp(check.created_at))}</span>
        <span class="sm-report__actions">
          <button class="button" type="button" id="sm-recheck">${icon("refresh")}Recheck</button>
          <button class="button" type="button" id="sm-copy-link">${icon("link")}Copy link</button>
        </span>
      </div>
      ${check.verdict === "fetch_failed" ? renderFetchFailed(check) : ""}
      ${check.verdict !== "fetch_failed" ? renderFoundTypes(check) : ""}
      ${renderRawMarkup(check)}
      ${check.findings.length ? renderFindings(check.findings) : renderCleanNotice(check)}
      ${check.generated ? renderGenerated(check.generated) : ""}
    </div>`;

  container.querySelector("#sm-copy-link").addEventListener("click", async (event) => {
    if (await copyText(shareUrl)) flashCopied(event.currentTarget);
  });
  container.querySelector("#sm-recheck").addEventListener("click", (event) => {
    void rerunCheck(check, event.currentTarget);
  });
  container.querySelectorAll("[data-copy-jsonld]").forEach((button) => {
    button.addEventListener("click", async () => {
      const jsonld = button.closest(".sm-entity").querySelector("pre").textContent;
      if (await copyText(jsonld)) flashCopied(button);
    });
  });
  container.querySelectorAll("[data-copy-raw]").forEach((button) => {
    button.addEventListener("click", async () => {
      const raw = button.closest(".sm-raw-block").querySelector("pre").textContent;
      if (await copyText(raw)) flashCopied(button);
    });
  });
  wireFilter(container);
}

// Every check is immutable once created (task-56) — "Recheck" doesn't edit
// this row, it runs a fresh check against the live page and navigates to the
// new one, same as submitting the form again with the same URL/client.
async function rerunCheck(check, button) {
  const original = button.innerHTML;
  button.disabled = true;
  button.innerHTML = `${icon("loader-2", "icon icon--spin")}Rechecking…`;
  try {
    const fresh = await checkSchemaMarkup(check.url, check.client_id);
    location.hash = buildToolsHash(TOOL_KEY, fresh.id);
  } catch (error) {
    toast(`Could not recheck this page — ${error.message}`);
    button.disabled = false;
    button.innerHTML = original;
  }
}

function flashCopied(button) {
  const original = button.innerHTML;
  button.innerHTML = `${icon("circle-check")}Copied`;
  button.disabled = true;
  setTimeout(() => {
    button.innerHTML = original;
    button.disabled = false;
  }, 1600);
}

function pageKindLabel(kind) {
  return {
    home: "Home page", blog_post: "Blog post", product: "Product page",
    service: "Service page", contact: "Contact page", other: "Page",
  }[kind] || kind;
}

function renderFetchFailed(check) {
  return `
    <div class="state-panel state-failed">
      ${icon("wifi-off")}
      <p><strong>Could not fetch this page.</strong><br>${escapeHtml(check.error || `HTTP ${check.http_status ?? "?"}`)}</p>
    </div>`;
}

function renderFoundTypes(check) {
  if (!check.found_types.length) {
    return `
      <div class="sm-empty-types">
        ${icon("braces")}
        <p>No JSON-LD or Microdata found on this page.</p>
      </div>`;
  }
  const badges = check.found_types.map((t) => `<span class="sm-type-badge">${escapeHtml(t)}</span>`).join("");
  return `<div class="sm-found-types"><strong>Found on the page:</strong> ${badges}</div>`;
}

// The badges above are just names — this is the actual JSON-LD/microdata
// behind them, collapsed by default (can be long), one block per
// `<script type="application/ld+json">` or top-level microdata item.
function renderRawMarkup(check) {
  const blocks = check.raw_markup || [];
  if (!blocks.length) return "";
  const items = blocks.map((block, index) => {
    if (block.format === "json-ld" && block.error) {
      return `
        <div class="sm-raw-block sm-raw-block--error">
          <div class="sm-raw-block__head"><span class="sm-raw-block__format">JSON-LD block ${index + 1}</span></div>
          <p class="sm-raw-block__error">${icon("alert-triangle")}Could not parse: ${escapeHtml(block.error)}</p>
        </div>`;
    }
    const label = block.format === "json-ld" ? `JSON-LD block ${index + 1}` : `Microdata item ${index + 1}`;
    return `
      <div class="sm-raw-block">
        <div class="sm-raw-block__head">
          <span class="sm-raw-block__format">${escapeHtml(label)}</span>
          <button class="button icon-button" type="button" data-copy-raw title="Copy JSON">${icon("copy")}</button>
        </div>
        <pre class="sm-code">${escapeHtml(JSON.stringify(block.raw, null, 2))}</pre>
      </div>`;
  }).join("");
  return `
    <details class="sm-raw">
      <summary>${icon("code")}<span>Raw markup found on this page</span><span class="sm-raw__count">${blocks.length}</span></summary>
      <div class="sm-raw__body">${items}</div>
    </details>`;
}

function renderCleanNotice() {
  return `
    <div class="sm-clean">
      ${icon("circle-check")}
      <p>No issues found against schema.org's vocabulary or Google's rich-result rules.</p>
    </div>`;
}

function renderFindings(findings) {
  const bySeverity = new Map(SEVERITY_ORDER.map((s) => [s, []]));
  findings.forEach((f) => bySeverity.get(f.severity)?.push(f));
  const groups = SEVERITY_ORDER.filter((s) => bySeverity.get(s).length).map((severity) => {
    const meta = SEVERITY_META[severity];
    const rows = bySeverity.get(severity).map(renderFinding).join("");
    return `
      <div class="sm-group ${meta.cls}" data-severity="${severity}">
        <h3 class="sm-group__title">${icon(meta.icon)}${meta.label}<span class="sm-group__count">${bySeverity.get(severity).length}</span></h3>
        <ul class="sm-group__list">${rows}</ul>
      </div>`;
  }).join("");
  return `
    <div class="sm-filter" role="group" aria-label="Filter by severity">
      ${SEVERITY_ORDER.map((s) => `
        <label class="sm-filter__option">
          <input type="checkbox" value="${s}" checked>${SEVERITY_META[s].label} (${bySeverity.get(s).length})
        </label>`).join("")}
    </div>
    <div class="sm-groups">${groups}</div>`;
}

function renderFinding(finding) {
  const propLine = finding.property_name
    ? `<p class="sm-finding__ref">${escapeHtml(finding.entity_types.join("/"))} → <code>${escapeHtml(finding.property_name)}</code> <span class="sm-finding__level">${finding.level}</span></p>`
    : `<p class="sm-finding__ref">${escapeHtml(finding.entity_types.join("/"))} <span class="sm-finding__level">${finding.level}</span></p>`;
  return `
    <li class="sm-finding">
      ${finding.entity_types.length ? propLine : `<p class="sm-finding__ref"><span class="sm-finding__level">${finding.level}</span></p>`}
      <p class="sm-finding__detail">${escapeHtml(finding.message)}</p>
    </li>`;
}

function wireFilter(container) {
  const checkboxes = container.querySelectorAll(".sm-filter__option input");
  if (!checkboxes.length) return;
  const apply = () => {
    const active = new Set([...checkboxes].filter((c) => c.checked).map((c) => c.value));
    container.querySelectorAll(".sm-group").forEach((group) => {
      group.hidden = !active.has(group.dataset.severity);
    });
  };
  checkboxes.forEach((c) => c.addEventListener("change", apply));
}

function renderGenerated(generated) {
  if (!generated.entities.length) {
    const skippedNote = generated.skipped.length
      ? `<p class="sm-skipped-note">${generated.skipped.map(([t, reason]) => `<strong>${escapeHtml(t)}</strong>: ${escapeHtml(reason)}`).join(" · ")}</p>`
      : "";
    return `
      <section class="sm-generate">
        <h3>Suggested markup</h3>
        <p class="tool-panel__hint">Nothing to generate for this page — every relevant type is already present.</p>
        ${skippedNote}
      </section>`;
  }
  const entities = generated.entities.map((entity) => {
    const needsInput = entity.needs_input.length
      ? `<p class="sm-needs-input">${icon("help-circle")}Needs input: ${entity.needs_input.map((p) => `<code>${escapeHtml(p)}</code>`).join(", ")} — not guessed, fill in by hand before publishing.</p>`
      : "";
    return `
      <div class="sm-entity">
        <div class="sm-entity__head">
          <strong>${escapeHtml(entity.type_name)}</strong>
          <button class="button icon-button" type="button" data-copy-jsonld title="Copy JSON-LD">${icon("copy")}</button>
        </div>
        <pre class="sm-code">${escapeHtml(JSON.stringify(entity.jsonld, null, 2))}</pre>
        ${needsInput}
      </div>`;
  }).join("");
  return `
    <section class="sm-generate">
      <h3>Suggested markup</h3>
      <p class="tool-panel__hint">Built only from facts actually found on this page — every field below traces back to
        where it came from. Paste as-is inside a <code>&lt;script type="application/ld+json"&gt;</code> tag.</p>
      ${entities}
    </section>`;
}
