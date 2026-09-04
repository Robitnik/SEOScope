// Web Archive "what changed, and when" feed (task-59 §5) —
// `#/archive/changes`. Structured like monitor-page.js: a top-level page,
// `paramsFromQuery`/`persist()` backed by `replaceArchiveChangesHash`, full
// `root.innerHTML = shell()` on every `initArchiveChangesPage` call.
import {
  getArchiveOverview, getArchiveSnapshotDiff, listArchiveChanges, isAbortError,
} from "./api.js?v=20260821-spin3";
import { buildArchiveHash, buildArchiveViewHash, replaceArchiveChangesHash } from "./router.js?v=20260821-spin3";
import { escapeHtml, formatTimestamp, icon } from "./formatters.js?v=20260821-spin3";
import { sectionSkeleton } from "./skeleton.js?v=20260821-spin3";

const root = document.querySelector("#archive-changes-page-root");
const PAGE_SIZE = 30;

const KIND_OPTIONS = [
  { key: "", label: "All kinds" },
  { key: "title", label: "Title" },
  { key: "description", label: "Description" },
  { key: "h1", label: "H1" },
  { key: "text", label: "Text" },
  { key: "links", label: "Links" },
  { key: "images", label: "Images" },
  { key: "status", label: "Status" },
  { key: "redirect", label: "Redirect" },
  { key: "noindex", label: "Noindex" },
];
const DAY_OPTIONS = [7, 30, 90, 365];

let current = null;
let clients = [];
let page = null;
let loading = false;
let loadError = null;
let expandedChangeId = null;
let expandedDiff = null;
let expandedDiffLoading = false;

function paramsFromQuery(query) {
  const client = query.get("client") || "";
  const kind = KIND_OPTIONS.some((o) => o.key === query.get("kind")) ? query.get("kind") : "";
  const days = DAY_OPTIONS.includes(Number(query.get("days"))) ? Number(query.get("days")) : 30;
  const pageNum = Math.max(1, Number(query.get("page")) || 1);
  return { client, kind, days, page: pageNum };
}

function persist() {
  replaceArchiveChangesHash({
    client: current.client || "",
    kind: current.kind || "",
    days: current.days !== 30 ? String(current.days) : "",
    page: current.page > 1 ? String(current.page) : "",
  });
}

export async function initArchiveChangesPage(query) {
  current = paramsFromQuery(query);
  page = null;
  loadError = null;
  expandedChangeId = null;
  expandedDiff = null;
  root.innerHTML = shell();
  bindFilterActions();
  loadClientOptions();
  await loadPage();
}

function shell() {
  return `
    <a class="back-link" href="${buildArchiveHash()}">${icon("arrow-left")}<span>Back to Archive</span></a>
    <div class="section-heading">
      <div><p class="eyebrow">Web Archive</p><h1>What changed, and when</h1></div>
    </div>
    <div class="archive-changes-filters">
      <label>
        <span class="visually-hidden">Client</span>
        <select id="archive-changes-client"><option value="">All clients</option></select>
      </label>
      <label>
        <span class="visually-hidden">Kind of change</span>
        <select id="archive-changes-kind">
          ${KIND_OPTIONS.map((o) => `<option value="${o.key}" ${o.key === current.kind ? "selected" : ""}>${o.label}</option>`).join("")}
        </select>
      </label>
      <label>
        <span class="visually-hidden">Time range</span>
        <select id="archive-changes-days">
          ${DAY_OPTIONS.map((d) => `<option value="${d}" ${d === current.days ? "selected" : ""}>Last ${d} days</option>`).join("")}
        </select>
      </label>
    </div>
    <section id="archive-changes-list" aria-live="polite" aria-busy="true">
      ${sectionSkeleton({ label: "the change feed", rows: 8, columns: 4 })}
    </section>`;
}

async function loadClientOptions() {
  try {
    const overview = await getArchiveOverview();
    clients = overview.clients.map((c) => ({ id: c.client_id, name: c.client_name }));
    const select = root.querySelector("#archive-changes-client");
    if (!select) return;
    select.innerHTML = `<option value="">All clients</option>${clients
      .map((c) => `<option value="${escapeHtml(c.id)}" ${c.id === current.client ? "selected" : ""}>${escapeHtml(c.name)}</option>`)
      .join("")}`;
  } catch {
    // Client filter degrades to "All clients only" — the feed itself still works.
  }
}

function bindFilterActions() {
  root.querySelector("#archive-changes-client").addEventListener("change", (event) => {
    current.client = event.target.value;
    current.page = 1;
    persist();
    loadPage();
  });
  root.querySelector("#archive-changes-kind").addEventListener("change", (event) => {
    current.kind = event.target.value;
    current.page = 1;
    persist();
    loadPage();
  });
  root.querySelector("#archive-changes-days").addEventListener("change", (event) => {
    current.days = Number(event.target.value);
    current.page = 1;
    persist();
    loadPage();
  });
}

async function loadPage() {
  loading = true;
  loadError = null;
  renderList();
  try {
    page = await listArchiveChanges({
      clientId: current.client || undefined,
      kind: current.kind || undefined,
      days: current.days,
      page: current.page,
      pageSize: PAGE_SIZE,
      requestKey: "archive-changes-list",
    });
  } catch (error) {
    if (isAbortError(error)) { loading = false; return; }
    page = null;
    loadError = error;
  }
  loading = false;
  renderList();
}

function changePage(next) {
  if (next < 1) return;
  current.page = next;
  persist();
  loadPage();
}

function summaryLine(change) {
  const s = change.summary || {};
  const parts = [];
  if (s.text) parts.push(`${s.text.delta_words >= 0 ? "+" : ""}${s.text.delta_words} words`);
  if (s.links) parts.push(`+${s.links.added_count}/−${s.links.removed_count} links`);
  if (s.images) parts.push(`+${s.images.added_count}/−${s.images.removed_count} images`);
  if (s.status) parts.push(`${s.status.old ?? "—"} → ${s.status.new ?? "—"}`);
  if (s.redirect) parts.push("redirect target changed");
  if (s.noindex) parts.push(s.noindex.new ? "now noindex" : "noindex removed");
  return parts.join(" · ") || "Field(s) updated";
}

function renderList() {
  const container = root.querySelector("#archive-changes-list");
  if (!container) return;
  container.setAttribute("aria-busy", loading ? "true" : "false");

  if (loadError && !page) {
    container.innerHTML = `
      <div class="state-panel state-failed">
        ${icon("wifi-off")}
        <p><strong>Could not load the change feed.</strong><br>${escapeHtml(loadError.message || "")}</p>
        <button type="button" class="button button--primary" id="archive-changes-retry">${icon("refresh")}Retry</button>
      </div>`;
    container.querySelector("#archive-changes-retry").addEventListener("click", loadPage);
    return;
  }
  if (loading && !page) {
    container.innerHTML = sectionSkeleton({ label: "the change feed", rows: 8, columns: 4 });
    return;
  }
  if (!page) { container.innerHTML = ""; return; }
  if (page.items.length === 0) {
    container.innerHTML = `
      <div class="state-panel state-not_configured">
        ${icon("timeline")}
        <p><strong>No changes in this window.</strong><br>Try a wider time range or a different filter.</p>
      </div>`;
    return;
  }

  container.innerHTML = `
    <ul class="archive-changes-feed">
      ${page.items.map((change) => `
        <li class="archive-change-row">
          <div class="archive-change-row__main">
            <p class="archive-change-row__client">${escapeHtml(change.client_name)}</p>
            <p class="archive-change-row__url" title="${escapeHtml(change.url)}">${escapeHtml(change.url)}</p>
            <p class="archive-change-row__summary">${escapeHtml(summaryLine(change))}</p>
            <div class="archive-change-row__kinds">
              ${change.change_kinds.map((k) => `<span class="tag-new">${escapeHtml(k)}</span>`).join("")}
            </div>
          </div>
          <div class="archive-change-row__meta">
            <span>${formatTimestamp(change.detected_at)}</span>
            <div class="archive-change-row__actions">
              <button type="button" class="button button--quiet button--small" data-action="toggle-diff" data-change-id="${escapeHtml(change.id)}" data-snapshot-id="${escapeHtml(change.snapshot_id)}">
                ${icon("git-compare")}${expandedChangeId === change.id ? "Hide diff" : "View diff"}
              </button>
              <a class="button button--quiet button--small" href="${buildArchiveViewHash({ snapshot: change.snapshot_id })}">${icon("external-link")}Open snapshot</a>
            </div>
          </div>
          ${expandedChangeId === change.id ? diffMarkup() : ""}
        </li>`).join("")}
    </ul>
    ${paginationMarkup()}`;
  bindRowActions(container);
}

function diffMarkup() {
  if (expandedDiffLoading) return `<div class="archive-diff-panel"><p class="archive-empty-note">Loading…</p></div>`;
  if (!expandedDiff) return "";
  if (expandedDiff.diff_lines.length === 0) {
    return `<div class="archive-diff-panel"><p class="archive-empty-note">No textual differences from the previous version.</p></div>`;
  }
  return `<div class="archive-diff-panel"><pre class="archive-diff">${expandedDiff.diff_lines.map(
    (line) => `<span class="${line.startsWith("+") ? "archive-diff-line--add" : line.startsWith("-") ? "archive-diff-line--del" : ""}">${escapeHtml(line)}</span>`,
  ).join("\n")}</pre></div>`;
}

function paginationMarkup() {
  if (!page || page.total <= page.page_size) return "";
  const start = (page.page - 1) * page.page_size + 1;
  const end = Math.min(page.page * page.page_size, page.total);
  return `<nav class="pagination" aria-label="Change feed pages">
    <button type="button" class="button button--quiet" data-action="page-prev" ${page.page <= 1 ? "disabled" : ""}>${icon("chevron-left")}Back</button>
    <span>${start}–${end} of ${page.total}</span>
    <button type="button" class="button button--quiet" data-action="page-next" ${end >= page.total ? "disabled" : ""}>Next${icon("chevron-right")}</button>
  </nav>`;
}

function bindRowActions(container) {
  container.querySelector('[data-action="page-prev"]')?.addEventListener("click", () => changePage(current.page - 1));
  container.querySelector('[data-action="page-next"]')?.addEventListener("click", () => changePage(current.page + 1));
  container.querySelectorAll('[data-action="toggle-diff"]').forEach((button) => {
    button.addEventListener("click", async () => {
      const changeId = button.dataset.changeId;
      if (expandedChangeId === changeId) {
        expandedChangeId = null;
        expandedDiff = null;
        renderList();
        return;
      }
      expandedChangeId = changeId;
      expandedDiff = null;
      expandedDiffLoading = true;
      renderList();
      try {
        expandedDiff = await getArchiveSnapshotDiff(button.dataset.snapshotId);
      } catch (error) {
        if (!isAbortError(error)) expandedDiff = { diff_lines: [`Could not load diff: ${error.message}`] };
      }
      expandedDiffLoading = false;
      renderList();
    });
  });
}
