// Page Inventory sub-page (task-57) — reached via "Open Page Inventory"
// from the compact summary card on the client page (same split as Monthly
// Performance, Ahrefs, Blogs: `client-page.js` only shows counts, the full
// two-tab table + controls live here on their own route).
//
// `Rebuild` reads whatever Site Monitor has ALREADY collected (fast, no
// site fetch). `Re-crawl site` is different: it does NOT call a
// page-inventory endpoint at all — it starts a real, full Site Monitor
// crawl (`startMonitorRun(clientId, "full")`, the exact same call
// `monitor-client.js`'s own "Check now · Full" button makes, task-36),
// polls it the same plain-`setInterval` way monitor-client.js does (no job
// engine, no SSE — task-57 §2.2a), and only calls `rebuildPageInventory`
// once that crawl finishes. This module never touches `app/monitor/**`.
import {
  getDashboard, getPageInventory, rebuildPageInventory, pageInventoryXlsxUrl,
  startMonitorRun, listMonitorRuns, isAbortError,
} from "./api.js?v=20260821-spin3";
import { clientHeaderSkeleton, sectionSkeleton } from "./skeleton.js?v=20260821-spin3";
import { buildClientHash } from "./router.js?v=20260821-spin3";
import { escapeHtml, formatTimestamp, icon, initials, STATE_META, stateClass } from "./formatters.js?v=20260821-spin3";
import { renderInventoryTab } from "./page-inventory-table.js?v=20260904-t57";
import { renderQaTab, renderProfileToggle } from "./page-inventory-qa.js?v=20260904-t57";

const POLL_INTERVAL_MS = 4000;
const PAGE_SIZE = 50;
// Only statuses a plain poll of `listMonitorRuns` can ever observe here —
// mirrors `monitor-client.js`'s own `RUN_TERMINAL_STATUSES`.
const RUN_TERMINAL_STATUSES = new Set(["succeeded", "failed", "failed_interrupted"]);

const root = document.querySelector("#page-inventory-page-root");

let current = null;
let table = null;
let loadError = null;
let loading = false;
let rebuilding = false;
let recrawl = null; // { runId, startedAt } | null
let recrawlTimer = null;
let expandedUrl = null;

export async function initPageInventoryPage(clientId, _query) {
  table = null;
  loadError = null;
  loading = false;
  rebuilding = false;
  stopRecrawlPolling();
  expandedUrl = null;
  current = { clientId, tab: "inventory", profile: "calibrated", q: "", status: "", page: 1 };
  root.innerHTML = shell();
  await loadClient();
  await loadTable();
  resumeRecrawlIfActive();
}

function shell() {
  return `
    <a class="back-link" href="${buildClientHash(current.clientId)}">${icon("arrow-left")}<span>Back to client</span></a>
    <header class="client-page__header" id="pi-header" aria-live="polite" aria-busy="true">
      ${clientHeaderSkeleton()}
    </header>
    <section class="pages-section" id="pi-content" aria-label="Page inventory" aria-live="polite" aria-busy="true">
      ${sectionSkeleton({ label: "the page inventory", rows: 8, columns: 6 })}
    </section>`;
}

async function loadClient() {
  try {
    const dashboard = await getDashboard({ period: "week", requestKey: `pi-shell-${current.clientId}` });
    const client = (dashboard.clients || []).find((item) => item.client_id === current.clientId);
    if (!client) {
      renderNotFound();
      return;
    }
    renderHeader(client);
  } catch (error) {
    if (isAbortError(error)) return;
    const header = document.querySelector("#pi-header");
    header.setAttribute("aria-busy", "false");
    header.innerHTML = `
      <div class="state-panel state-failed">
        ${icon("wifi-off")}
        <p><strong>Could not load this client.</strong><br>${escapeHtml(error.message)}.</p>
        <button type="button" class="button button--primary" id="pi-retry">${icon("refresh")}Retry</button>
      </div>`;
    header.querySelector("#pi-retry").addEventListener("click", () => {
      header.setAttribute("aria-busy", "true");
      header.innerHTML = clientHeaderSkeleton();
      loadClient();
    });
  }
}

function renderNotFound() {
  root.querySelector("#pi-header").setAttribute("aria-busy", "false");
  root.querySelector("#pi-header").innerHTML = `
    <div class="state-panel state-failed">
      ${icon("user-x")}
      <p><strong>Client not found.</strong><br>The link may be outdated.</p>
      <a class="button button--primary" href="#/">Back to client list</a>
    </div>`;
  document.querySelector("#pi-content")?.remove();
}

function renderHeader(client) {
  const state = STATE_META[client.overall_status] || STATE_META.attention;
  const logo = client.logo_path
    ? `<img class="client-logo" src="${escapeHtml(client.logo_path)}" width="52" height="52" alt="Logo of ${escapeHtml(client.name)}" loading="lazy">`
    : `<span class="client-logo" aria-hidden="true">${escapeHtml(initials(client.name))}</span>`;
  document.querySelector("#pi-header").innerHTML = `
    <div class="client-page__identity">
      ${logo}
      <div>
        <p class="eyebrow">Page Inventory</p>
        <h1>${escapeHtml(client.name)}</h1>
        <a href="https://${encodeURI(client.domain)}" target="_blank" rel="noreferrer">${escapeHtml(client.domain)}</a>
      </div>
    </div>
    <div class="client-page__status">
      <span class="status-label ${stateClass(client.overall_status)}">${icon(state.icon)}${state.label}</span>
    </div>`;
  document.querySelector("#pi-header").setAttribute("aria-busy", "false");
}

async function loadTable() {
  loading = true;
  loadError = null;
  renderSection();
  try {
    table = await getPageInventory(current.clientId, {
      profile: current.profile, q: current.q || undefined, status: current.status || undefined,
      page: current.page, pageSize: PAGE_SIZE,
    });
  } catch (error) {
    if (isAbortError(error)) { loading = false; return; }
    table = null;
    loadError = error;
  }
  loading = false;
  renderSection();
}

function toolbarMarkup() {
  const summary = table?.summary;
  const builtNote = summary?.built_at
    ? `Built from monitor data collected ${formatTimestamp(summary.source_collected_at || summary.built_at)}${summary.stale ? ` <span class="tag-new">stale</span>` : ""}`
    : "Never built yet.";
  return `
    <div class="section-heading">
      <div><p class="eyebrow">Page Inventory</p><h2>Site architecture &amp; SEO QA</h2><p class="ahrefs-collected">${builtNote}</p></div>
      <div class="pi-actions">
        <button type="button" class="button button--quiet" data-action="rebuild" ${rebuilding || recrawl ? "disabled" : ""}>${icon(rebuilding ? "loader-2" : "refresh", rebuilding ? "icon icon--spin" : "icon")}${rebuilding ? "Rebuilding…" : "Rebuild"}</button>
        <button type="button" class="button button--primary" data-action="recrawl" ${recrawl ? "disabled" : ""}>${icon(recrawl ? "loader-2" : "world-search", recrawl ? "icon icon--spin" : "icon")}${recrawl ? recrawlLabel() : "Re-crawl site"}</button>
        ${table ? `<a class="button button--quiet" href="${pageInventoryXlsxUrl(current.clientId)}">${icon("file-spreadsheet")}Export to Excel</a>` : ""}
      </div>
    </div>
    ${recrawl ? `<p class="pi-recrawl-note">${icon("info-circle")}Fetching every page fresh from the live site — this can take several minutes. You can keep browsing the last-built inventory below while it runs.</p>` : ""}
    <div class="pi-toolbar">
      <label class="search-field">
        <span class="visually-hidden">Search pages</span>
        ${icon("search")}
        <input type="search" id="pi-search" placeholder="Search by URL, title or description" value="${escapeHtml(current.q)}" autocomplete="off">
      </label>
      <div class="tablist" role="tablist" aria-label="View">
        <button type="button" role="tab" data-action="tab-inventory" aria-selected="${String(current.tab === "inventory")}">${icon("list")}Inventory</button>
        <button type="button" role="tab" data-action="tab-qa" aria-selected="${String(current.tab === "qa")}">${icon("shield-check")}SEO QA</button>
      </div>
    </div>
    ${current.tab === "qa" ? renderProfileToggle(current.profile) : ""}`;
}

function recrawlLabel() {
  if (!recrawl) return "";
  const minutes = Math.max(0, Math.round((Date.now() - recrawl.startedAt) / 60000));
  return minutes < 1 ? "Re-crawling… (just started)" : `Re-crawling… (started ${minutes}m ago)`;
}

function paginationMarkup() {
  if (!table || table.total <= table.page_size) return "";
  const start = (table.page - 1) * table.page_size + 1;
  const end = Math.min(table.page * table.page_size, table.total);
  return `<nav class="pagination" aria-label="Table pages">
    <button type="button" class="button button--quiet" data-action="page-prev" ${table.page <= 1 ? "disabled" : ""}>${icon("chevron-left")}Back</button>
    <span>${start}–${end} of ${table.total}</span>
    <button type="button" class="button button--quiet" data-action="page-next" ${end >= table.total ? "disabled" : ""}>Next${icon("chevron-right")}</button>
  </nav>`;
}

function renderSection() {
  const container = document.querySelector("#pi-content");
  if (!container) return;
  container.setAttribute("aria-busy", loading ? "true" : "false");

  if (loadError && !table) {
    const isNeverBuilt = loadError.code === "never_built";
    container.innerHTML = isNeverBuilt
      ? `<div class="section-heading"><div><p class="eyebrow">Page Inventory</p><h2>Site architecture &amp; SEO QA</h2></div><div class="pi-actions"><button type="button" class="button button--primary" data-action="rebuild">${icon("refresh")}Rebuild</button></div></div>
         <div class="state-panel state-not_configured">${icon("sitemap")}<p><strong>No page inventory built yet.</strong><br>Press Rebuild to read it from Site Monitor's latest crawl, or Re-crawl site for a fresh one.</p></div>`
      : `<div class="state-panel state-failed">${icon("wifi-off")}<p><strong>Could not load the page inventory.</strong><br>${escapeHtml(loadError.message || "")}</p><button type="button" class="button button--primary" data-action="retry">${icon("refresh")}Retry</button></div>`;
    bindActions(container);
    return;
  }

  if (loading && !table) {
    container.innerHTML = sectionSkeleton({ label: "the page inventory", rows: 8, columns: 6 });
    return;
  }

  if (!table) {
    container.innerHTML = "";
    return;
  }

  container.innerHTML = `${toolbarMarkup()}<div id="pi-table"></div>${paginationMarkup()}`;
  const tableContainer = container.querySelector("#pi-table");
  if (current.tab === "inventory") {
    renderInventoryTab(tableContainer, { rows: table.rows });
  } else {
    renderQaTab(tableContainer, {
      rows: table.rows, clusters: table.duplicate_clusters, profile: current.profile, expandedUrl,
    });
  }
  bindActions(container);
}

function bindActions(container) {
  container.querySelector('[data-action="rebuild"]')?.addEventListener("click", () => rebuild());
  container.querySelector('[data-action="retry"]')?.addEventListener("click", () => loadTable());
  container.querySelector('[data-action="recrawl"]')?.addEventListener("click", () => startRecrawl());
  container.querySelector('[data-action="tab-inventory"]')?.addEventListener("click", () => switchTab("inventory"));
  container.querySelector('[data-action="tab-qa"]')?.addEventListener("click", () => switchTab("qa"));
  container.querySelector('[data-action="profile-calibrated"]')?.addEventListener("click", () => setProfile("calibrated"));
  container.querySelector('[data-action="profile-strict"]')?.addEventListener("click", () => setProfile("strict"));
  container.querySelector('[data-action="page-prev"]')?.addEventListener("click", () => changePage(current.page - 1));
  container.querySelector('[data-action="page-next"]')?.addEventListener("click", () => changePage(current.page + 1));

  let searchTimer = null;
  const searchInput = container.querySelector("#pi-search");
  searchInput?.addEventListener("input", (event) => {
    clearTimeout(searchTimer);
    const value = event.target.value;
    searchTimer = setTimeout(() => {
      current.q = value;
      current.page = 1;
      loadTable();
    }, 250);
  });

  container.querySelectorAll('[data-action="toggle-row"]').forEach((button) => {
    button.addEventListener("click", () => {
      const url = button.closest("tr")?.dataset.rowUrl;
      expandedUrl = expandedUrl === url ? null : url;
      renderSection();
    });
  });
}

function switchTab(tab) {
  if (current.tab === tab) return;
  current.tab = tab;
  expandedUrl = null;
  renderSection();
}

function setProfile(profile) {
  if (current.profile === profile) return;
  current.profile = profile;
  current.page = 1;
  loadTable();
}

function changePage(page) {
  if (page < 1) return;
  current.page = page;
  loadTable();
}

async function rebuild() {
  if (rebuilding) return;
  rebuilding = true;
  renderSection();
  try {
    table = await rebuildPageInventory(current.clientId);
    loadError = null;
    current.page = 1;
  } catch (error) {
    if (!isAbortError(error)) loadError = error;
  } finally {
    rebuilding = false;
    renderSection();
  }
}

// --- Re-crawl (task-57 §2.2a) — orchestrates the EXISTING monitor full-run
// endpoint from here; no page-inventory-specific crawl code exists. -------

function resumeRecrawlIfActive() {
  if (recrawl) return;
  listMonitorRuns(current.clientId, {
    kind: "full", limit: 1, requestKey: `pi-resume-full-${current.clientId}`,
  }).then((runs) => {
    const run = runs[0];
    if (run && !RUN_TERMINAL_STATUSES.has(run.status)) {
      beginRecrawlPolling(run.id, new Date(run.started_at).getTime() || Date.now());
    }
  }).catch(() => { /* best-effort — a manual Re-crawl click still works */ });
}

async function startRecrawl() {
  if (recrawl) return;
  recrawl = { runId: null, startedAt: Date.now() };
  renderSection();
  let accepted;
  try {
    accepted = await startMonitorRun(current.clientId, "full");
  } catch (error) {
    recrawl = null;
    renderSection();
    if (!isAbortError(error)) window.alert(`Could not start the re-crawl: ${error.message}`);
    return;
  }
  beginRecrawlPolling(accepted.run_id, Date.now());
}

function beginRecrawlPolling(runId, startedAt) {
  recrawl = { runId, startedAt };
  renderSection();
  pollRecrawl();
  recrawlTimer = setInterval(pollRecrawl, POLL_INTERVAL_MS);
}

function stopRecrawlPolling() {
  if (recrawlTimer) clearInterval(recrawlTimer);
  recrawlTimer = null;
  recrawl = null;
}

async function pollRecrawl() {
  if (!recrawl) return;
  const runId = recrawl.runId;
  try {
    const runs = await listMonitorRuns(current.clientId, {
      kind: "full", limit: 1, requestKey: `pi-recrawl-poll-${current.clientId}`,
    });
    if (!recrawl || recrawl.runId !== runId) return;
    const run = runs[0];
    if (!run || run.id !== runId) return; // scheduler hasn't claimed it yet this tick
    if (RUN_TERMINAL_STATUSES.has(run.status)) {
      stopRecrawlPolling();
      renderSection();
      await rebuild();
    } else {
      renderSection(); // keeps the "started Xm ago" label ticking
    }
  } catch (error) {
    if (isAbortError(error)) return;
    // Transient poll failure — the crawl keeps running server-side
    // regardless (same reasoning as monitor-client.js's own poll loop).
  }
}
