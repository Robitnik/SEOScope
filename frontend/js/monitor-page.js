// Site Monitor — portfolio page (task-36). See
// doc/dev/done/task-36-site-monitor-ui.md §3.1 for the full spec this file
// implements. Structured like trends-page.js: a top-level page with no
// client id, its own `paramsFromQuery`/`persist()` pair backed by
// `buildMonitorHash`/`replaceMonitorHash`, and `root.innerHTML = shell()` on
// every `initMonitorPage` call.
//
// The operator's job here is narrow: "who is on fire, right now, without
// opening a client". Top to bottom: a status strip, the open critical list
// (always visible — never hidden by the feed filters below it), a per-client
// table, then the full portfolio change feed with its own filters.
//
// "alerts"/.alert-* is task-17's GSC declining/rising block — a different
// feature. Everything here uses a `.monitor-*` class prefix instead (task-36
// §1.2).

import {
  ackMonitorChange, getDashboard, getMonitorSummary, isAbortError,
  listPortfolioMonitorChanges, startMonitorRun,
} from "./api.js?v=20260812-monitor5";
import { buildMonitorClientHash, replaceMonitorHash } from "./router.js?v=20260812-monitor5";
import {
  MONITOR_SEVERITY_META, changeTypeLabel, escapeHtml, formatTimestamp, icon,
} from "./formatters.js?v=20260812-monitor5";
import { sectionSkeleton } from "./skeleton.js?v=20260812-monitor5";
import { isPaused } from "./client-status.js?v=20260812-monitor5";

const root = document.querySelector("#monitor-page-root");

// How long a fixture "Check now" run stays in its `running` state
// (`MONITOR_FIXTURE_RUN_MS` in api.js) plus a little slack for the real
// backend's async dispatch — long enough that a quiet reload after "Check all
// now" has something new to show, short enough the operator isn't left
// staring at a stale strip.
const CHECK_ALL_SETTLE_MS = 3600;

// Client-table state column — distinct from `MONITOR_SEVERITY_META` (change
// severity) and `client-status.js`'s CLIENT_STATUS_META (commercial
// active/paused badge on dashboard cards). This is the monitor's own read of
// "is this client's checking healthy", per task-36 §3.1 point 3.
const MONITOR_CLIENT_STATE_META = {
  never_checked: { label: "Never checked", icon: "help-hexagon" },
  paused: { label: "Paused — monitoring off", icon: "player-pause" },
  blocked: { label: "Blocked", icon: "shield-lock" },
  ok: { label: "OK", icon: "circle-check" },
  // A single client's `getMonitorSummary` call failing must never take down
  // the whole table (cell isolation, same spirit as collection's
  // (client, provider) isolation) — this cell just says so honestly instead
  // of rendering 0s.
  unavailable: { label: "Could not load", icon: "cloud-off" },
};

const SEVERITY_OPTIONS = [
  { key: "", label: "All severities" },
  { key: "critical", label: "Critical" },
  { key: "warning", label: "Warning" },
  { key: "info", label: "Info" },
];
const SORT_KEYS = ["name", "critical", "warning", "info"];

function toast(message) {
  const region = document.querySelector("#toast-region");
  const element = document.createElement("div");
  element.className = "toast-message";
  element.textContent = message;
  region.append(element);
  setTimeout(() => element.remove(), 4200);
}

function paramsFromQuery(query) {
  const severity = ["critical", "warning", "info"].includes(query.get("severity")) ? query.get("severity") : "";
  const state = query.get("state") === "all" ? "all" : "open";
  const type = query.get("type") || "";
  const client = query.get("client") || "";
  const info = query.get("info") === "1";
  const sort = SORT_KEYS.includes(query.get("sort")) ? query.get("sort") : "name";
  const dir = query.get("dir") === "desc" ? "desc" : "asc";
  return { severity, state, type, client, info, sort, dir };
}

function persist() {
  replaceMonitorHash({
    severity: current.severity || "",
    state: current.state !== "open" ? current.state : "",
    type: current.type || "",
    client: current.client || "",
    info: current.info ? "1" : "",
    sort: current.sort !== "name" ? current.sort : "",
    dir: current.dir !== "asc" ? current.dir : "",
  });
}

let current = null;
let requestToken = 0;
let feedToken = 0;
let typeDebounce = null;
// Everything the last successful full load produced, kept around so a filter
// change can re-render the table/feed without refetching the client list and
// per-client summaries, and so a sort click never needs the network at all.
let lastData = null;

export async function initMonitorPage(query) {
  current = paramsFromQuery(query);
  lastData = null;
  root.innerHTML = shell();
  bindDelegatedEvents();
  await load();
}

function shell() {
  return `
    <header class="monitor-header">
      <p class="eyebrow">Site Monitor</p>
      <h1>Portfolio</h1>
      <p class="monitor-lead">Robots, sitemap, DNS and page checks across every client — what needs attention right now.</p>
    </header>
    <section class="monitor-content" id="mon-content" aria-busy="true">
      ${sectionSkeleton({ label: "Site Monitor portfolio", kpis: 3, rows: 8, columns: 6 })}
    </section>`;
}

// One delegated listener set, bound once per `initMonitorPage` call on the
// freshly-created shell — survives every partial re-render of `#mon-content`
// because those replace innerHTML, not the container node itself (same
// reasoning trends-page.js uses for its picker, applied to the whole page).
function bindDelegatedEvents() {
  root.addEventListener("click", onRootClick);
  root.addEventListener("change", onRootChange);
  root.addEventListener("input", onRootInput);
}

function onRootClick(event) {
  const retry = event.target.closest("[data-retry]");
  if (retry) { void load(); return; }

  const checkAll = event.target.closest("[data-action='check-all']");
  if (checkAll) { void startCheckAll(checkAll); return; }

  const ack = event.target.closest("[data-ack]");
  if (ack) { void acknowledge(ack); return; }

  const sortButton = event.target.closest("[data-sort]");
  if (sortButton) {
    const key = sortButton.dataset.sort;
    if (current.sort === key) current.dir = current.dir === "asc" ? "desc" : "asc";
    else { current.sort = key; current.dir = "asc"; }
    persist();
    renderTableSection();
    return;
  }

  const stateButton = event.target.closest("[data-feed-state]");
  if (stateButton) {
    const value = stateButton.dataset.feedState;
    if (value === current.state) return;
    current.state = value;
    persist();
    void reloadFeed();
    return;
  }

  const clearFilters = event.target.closest("[data-clear-feed-filters]");
  if (clearFilters) {
    current = { ...current, severity: "", type: "", client: "", state: "open", info: false };
    persist();
    void reloadFeed();
  }
}

function onRootChange(event) {
  if (event.target.id === "mon-f-severity") {
    current.severity = event.target.value;
    persist();
    void reloadFeed();
  } else if (event.target.id === "mon-f-client") {
    current.client = event.target.value;
    persist();
    renderFeedList();
  } else if (event.target.id === "mon-f-info") {
    current.info = event.target.checked;
    persist();
    renderFeedList();
  }
}

function onRootInput(event) {
  if (event.target.id !== "mon-f-type") return;
  const value = event.target.value;
  window.clearTimeout(typeDebounce);
  typeDebounce = window.setTimeout(() => {
    current.type = value.trim();
    persist();
    void reloadFeed();
  }, 350);
}

// --- Data loading ------------------------------------------------------

// Each `listPortfolioMonitorChanges` call below passes its own `requestKey`
// (critical strip / blocked-detection / feed) — live reproduction: the
// blocked-detection query and the default feed query are both `state=open`
// with no severity (differing only in `limit`, which isn't part of a
// shape-derived key), so under `Promise.all` the second one aborted the
// first's in-flight fetch and the portfolio page hung on its loading skeleton
// forever. Explicit purpose-scoped keys make the three calls independent
// regardless of overlapping params. The per-client `getMonitorSummary` calls
// use a distinct key per client and join the same fan-out.
async function load({ quiet = false } = {}) {
  const token = ++requestToken;
  const content = root.querySelector("#mon-content");
  if (!quiet) {
    content.setAttribute("aria-busy", "true");
    content.innerHTML = sectionSkeleton({ label: "Site Monitor portfolio", kpis: 3, rows: 8, columns: 6 });
  }
  try {
    const dashboard = await getDashboard({ period: "week", requestKey: "monitor-dashboard-clients" });
    const clients = dashboard.clients || [];
    const feedLimit = current.type || current.client ? 500 : 200;
    const [summaryResults, criticalOpen, openForBlocked, feedRaw] = await Promise.all([
      Promise.allSettled(clients.map((client) => getMonitorSummary(client.client_id))),
      listPortfolioMonitorChanges({ severity: "critical", state: "open", limit: 50, requestKey: "monitor-portfolio-critical-strip" }),
      // Dedicated broad query for "Blocked" detection: `monitor_blocked` is a
      // WARNING-severity change type (app/monitor/severity.py), so the
      // critical-only list above never contains it.
      listPortfolioMonitorChanges({ state: "open", limit: 500, requestKey: "monitor-portfolio-blocked-detect" }),
      listPortfolioMonitorChanges({ severity: current.severity || undefined, state: current.state, limit: feedLimit, requestKey: "monitor-portfolio-feed" }),
    ]);
    if (token !== requestToken) return;

    const summaries = summaryResults.map((result) => (result.status === "fulfilled" ? result.value : null));
    lastData = {
      clients,
      clientsById: new Map(clients.map((client) => [client.client_id, client])),
      summariesById: new Map(clients.map((client, index) => [client.client_id, summaries[index]])),
      criticalOpen, openForBlocked, feedRaw,
    };
    content.setAttribute("aria-busy", "false");
    renderAll(content);
  } catch (error) {
    if (isAbortError(error)) return;
    // Matches dashboard.js's renderError: an honest error replaces the view
    // rather than a page full of zeros standing in for real data.
    content.setAttribute("aria-busy", "false");
    content.innerHTML = `
      <div class="state-panel state-failed">
        ${icon("wifi-off")}
        <p><strong>Could not load Site Monitor.</strong><br>${escapeHtml(error.message)}. Previous values were not replaced with zeros.</p>
        <button type="button" class="button button--primary" data-retry>${icon("refresh")}Retry</button>
      </div>`;
  }
}

// Re-fetches only the change feed (severity/state are server-side filters)
// and re-renders only that section — the strip/critical/table stay put, both
// for speed and so a filter tweak never causes the acknowledge list above it
// to jump around.
async function reloadFeed() {
  if (!lastData) return;
  const token = ++feedToken;
  const feedRoot = root.querySelector("#mon-feed");
  feedRoot?.setAttribute("aria-busy", "true");
  try {
    const feedLimit = current.type || current.client ? 500 : 200;
    const feedRaw = await listPortfolioMonitorChanges({
      severity: current.severity || undefined, state: current.state, limit: feedLimit, requestKey: "monitor-portfolio-feed",
    });
    if (token !== feedToken) return;
    lastData.feedRaw = feedRaw;
    renderFeedSection();
  } catch (error) {
    if (isAbortError(error)) return;
    toast(`Could not refresh the change feed: ${error.message}`);
  } finally {
    feedRoot?.setAttribute("aria-busy", "false");
  }
}

// --- Actions -------------------------------------------------------------

async function acknowledge(button) {
  const id = button.dataset.ack;
  button.disabled = true;
  try {
    const result = await ackMonitorChange(id, null);
    toast("Change acknowledged.");
    // Patch the cached lists in place and re-render just those two sections,
    // rather than a full `load()`: it's faster, and in fixture mode the ack
    // endpoint returns a result without mutating the static JSON bundle, so a
    // refetch would show the change as still open.
    applyAcknowledgement(id, result.acknowledged_at || new Date().toISOString());
  } catch (error) {
    if (isAbortError(error)) return;
    toast(`Could not acknowledge: ${error.message}`);
    button.disabled = false;
  }
}

function applyAcknowledgement(changeId, acknowledgedAt) {
  if (!lastData) return;
  [lastData.criticalOpen, lastData.feedRaw].forEach((list) => {
    const item = (list || []).find((change) => change.id === changeId);
    if (item) item.acknowledged_at = acknowledgedAt;
  });
  renderCriticalSection();
  renderFeedList();
}

async function startCheckAll(trigger) {
  if (!lastData) return;
  const clients = lastData.clients.filter((client) => !isPaused(client));
  if (!clients.length) { toast("No active client to check — every client is paused."); return; }
  trigger.disabled = true;
  const original = trigger.innerHTML;
  trigger.innerHTML = `${icon("loader-2", "icon icon--tiny")}Starting checks…`;
  try {
    const results = await Promise.allSettled(clients.map((client) => startMonitorRun(client.client_id, "light")));
    const started = results.filter((result) => result.status === "fulfilled").length;
    const failed = results.length - started;
    toast(failed
      ? `Started a light check for ${started} of ${results.length} clients — ${failed} failed to start.`
      : `Started a light check for ${started} client${started === 1 ? "" : "s"}.`);
  } finally {
    trigger.disabled = false;
    trigger.innerHTML = original;
  }
  // Light checks run asynchronously on the backend; give them a moment to
  // land before pulling fresh summaries, so the strip/table reflect the new
  // results instead of an unchanged pre-check snapshot.
  window.setTimeout(() => { void load({ quiet: true }); }, CHECK_ALL_SETTLE_MS);
}

// --- Rendering: orchestration ---------------------------------------------

function renderAll(content) {
  content.innerHTML = `
    <div id="mon-strip"></div>
    <section class="monitor-section" id="mon-critical" aria-live="polite">
      <h2>Open critical</h2>
      <div id="mon-critical-body"></div>
    </section>
    <section class="monitor-section" id="mon-table">
      <h2>Clients</h2>
      <div id="mon-table-body"></div>
    </section>
    <section class="monitor-section" id="mon-feed"></section>`;
  renderStrip();
  renderCriticalSection();
  renderTableSection();
  renderFeedSection();
}

// --- Rendering: strip -------------------------------------------------

function renderStrip() {
  const host = root.querySelector("#mon-strip");
  if (!host || !lastData) return;
  let criticalClients = 0;
  let warningClients = 0;
  let lastCheckedMs = null;
  lastData.clients.forEach((client) => {
    const summary = lastData.summariesById.get(client.client_id);
    if (!summary) return; // a failed cell is excluded, never counted as zero
    if (summary.open_critical > 0) criticalClients += 1;
    if (summary.open_warning > 0) warningClients += 1;
    [summary.last_light_run?.finished_at, summary.last_full_run?.finished_at].forEach((value) => {
      if (!value) return;
      const ms = Date.parse(value);
      if (Number.isFinite(ms) && (lastCheckedMs === null || ms > lastCheckedMs)) lastCheckedMs = ms;
    });
  });
  const lastCheckedText = lastCheckedMs === null ? "Never" : formatTimestamp(new Date(lastCheckedMs).toISOString(), "Never");
  host.innerHTML = `
    <div class="monitor-strip">
      <div class="monitor-strip__stat monitor-strip__stat--critical">
        ${icon(MONITOR_SEVERITY_META.critical.icon, "icon")}
        <span class="monitor-strip__value">${criticalClients}</span>
        <span class="monitor-strip__label">${criticalClients === 1 ? "client" : "clients"} with open critical</span>
      </div>
      <div class="monitor-strip__stat monitor-strip__stat--warning">
        ${icon(MONITOR_SEVERITY_META.warning.icon, "icon")}
        <span class="monitor-strip__value">${warningClients}</span>
        <span class="monitor-strip__label">${warningClients === 1 ? "client" : "clients"} with open warning</span>
      </div>
      <div class="monitor-strip__stat">
        <span class="monitor-strip__value">${escapeHtml(lastCheckedText)}</span>
        <span class="monitor-strip__label">Last portfolio check</span>
      </div>
      <button type="button" class="button button--primary monitor-strip__action" data-action="check-all">${icon("refresh")}Check all now</button>
    </div>`;
}

// --- Rendering: open critical -------------------------------------------

function renderCriticalSection() {
  const host = root.querySelector("#mon-critical-body");
  if (!host || !lastData) return;
  // "Open critical" means *still needs action* — an acknowledged item has
  // already been triaged, so it drops out here (the spec's "remove the row"
  // option) while staying visible with an "Acknowledged" chip in the general
  // feed below, where `state=open` intentionally keeps tracking it.
  const changes = (lastData.criticalOpen || []).filter((change) => !change.acknowledged_at);
  if (!changes.length) {
    host.innerHTML = `
      <div class="state-panel">
        ${icon("circle-check")}
        <p><strong>No open critical issues.</strong><br>Nothing here needs immediate attention.</p>
      </div>`;
    return;
  }
  host.innerHTML = `
    <ul class="monitor-change-list monitor-critical-list" aria-label="Open critical changes">
      ${changes.map((change) => renderChangeRow(change)).join("")}
    </ul>`;
}

// --- Rendering: change rows (shared between critical section and feed) ---

function renderChangeRow(change) {
  const client = lastData.clientsById.get(change.client_id);
  const meta = MONITOR_SEVERITY_META[change.severity] || MONITOR_SEVERITY_META.info;
  const label = changeTypeLabel(change.change_type);
  const subject = change.subject || "—";
  const clientName = client ? client.name : change.client_id;
  let action;
  if (change.resolved_at) {
    action = `<span class="monitor-change-row__status">${icon("circle-check", "icon icon--tiny")}Resolved ${escapeHtml(formatTimestamp(change.resolved_at))}</span>`;
  } else if (change.acknowledged_at) {
    action = `<span class="monitor-change-row__status">${icon("check", "icon icon--tiny")}Acknowledged</span>`;
  } else {
    action = `
      <button type="button" class="button button--quiet monitor-change-row__ack" data-ack="${escapeHtml(change.id)}"
        aria-label="${escapeHtml(`Acknowledge: ${label} on ${subject}`)}">${icon("check", "icon icon--tiny")}Acknowledge</button>`;
  }
  return `
    <li class="monitor-change-row monitor-change-row--${escapeHtml(change.severity)}">
      ${icon(meta.icon, "icon monitor-change-row__icon")}
      <span class="monitor-change-row__severity">${escapeHtml(meta.label)}</span>
      <a class="monitor-change-row__client" href="${buildMonitorClientHash(change.client_id)}" translate="no">${escapeHtml(clientName)}</a>
      <span class="monitor-change-row__type">${escapeHtml(label)}</span>
      <span class="monitor-change-row__subject" translate="no">${escapeHtml(subject)}</span>
      <span class="monitor-change-row__time">${escapeHtml(formatTimestamp(change.detected_at))}</span>
      ${action}
    </li>`;
}

// --- Rendering: client table ---------------------------------------------

function blockedClientIds(changes) {
  const ids = new Set();
  (changes || []).forEach((change) => { if (change.change_type === "monitor_blocked") ids.add(change.client_id); });
  return ids;
}

function rowStateOf(client, summary, blocked) {
  if (!summary) return "unavailable";
  if (!summary.last_light_run && !summary.last_full_run) return "never_checked";
  if (isPaused(client)) return "paused";
  if (blocked.has(client.client_id)) return "blocked";
  return "ok";
}

function sortTh(key, label) {
  const active = current.sort === key;
  const ariaSort = active ? (current.dir === "desc" ? "descending" : "ascending") : "none";
  const sortIcon = active ? (current.dir === "desc" ? "sort-descending" : "sort-ascending") : "arrows-sort";
  return `<th aria-sort="${ariaSort}"><button type="button" class="monitor-table-sort" data-sort="${key}">${escapeHtml(label)}${icon(sortIcon, "icon icon--tiny")}</button></th>`;
}

function renderTableSection() {
  const host = root.querySelector("#mon-table-body");
  if (!host || !lastData) return;
  if (!lastData.clients.length) {
    host.innerHTML = `
      <div class="state-panel">
        ${icon("users")}
        <p><strong>No clients in the portfolio yet.</strong><br>Add a client via the dashboard first.</p>
      </div>`;
    return;
  }
  const blocked = blockedClientIds(lastData.openForBlocked);
  const rows = lastData.clients.map((client) => {
    const summary = lastData.summariesById.get(client.client_id);
    return { client, summary, paused: isPaused(client), state: rowStateOf(client, summary, blocked) };
  });

  const dir = current.dir === "desc" ? -1 : 1;
  const compare = (a, b) => {
    if (current.sort === "name") return a.client.name.localeCompare(b.client.name) * dir;
    const field = `open_${current.sort}`;
    const av = a.summary ? a.summary[field] : -1;
    const bv = b.summary ? b.summary[field] : -1;
    return (av - bv) * dir;
  };
  // Paused clients always sink to the bottom regardless of sort column — the
  // same convention the main dashboard uses (task-34).
  const active = rows.filter((row) => !row.paused).sort(compare);
  const parked = rows.filter((row) => row.paused).sort(compare);
  const ordered = [...active, ...parked];

  host.innerHTML = `
    <div class="table-scroll">
      <table class="analytics-table monitor-client-table">
        <thead><tr>
          ${sortTh("name", "Client")}
          <th>Last light check</th>
          <th>Last full check</th>
          ${sortTh("critical", "Critical")}
          ${sortTh("warning", "Warning")}
          ${sortTh("info", "Info")}
          <th>State</th>
        </tr></thead>
        <tbody>${ordered.map((row) => renderTableRow(row)).join("")}</tbody>
      </table>
    </div>`;
}

function renderTableRow({ client, summary, paused, state }) {
  const meta = MONITOR_CLIENT_STATE_META[state];
  // "Never" is a confirmed fact (a summary that legitimately has no runs);
  // "—" is honest ignorance (the summary fetch itself failed) — the two must
  // not collapse into the same word (AGENTS §5, `None` ≠ a definite value).
  const timeFallback = summary ? "Never" : "—";
  return `
    <tr class="${paused ? "monitor-client-row--paused" : ""}">
      <td class="analytics-row__value">
        <a href="${buildMonitorClientHash(client.client_id)}" translate="no">${escapeHtml(client.name)}</a><br>
        <small class="muted" translate="no">${escapeHtml(client.domain)}</small>
      </td>
      <td>${escapeHtml(formatTimestamp(summary?.last_light_run?.finished_at, timeFallback))}</td>
      <td>${escapeHtml(formatTimestamp(summary?.last_full_run?.finished_at, timeFallback))}</td>
      <td class="monitor-count monitor-count--critical">${summary ? summary.open_critical : "—"}</td>
      <td class="monitor-count monitor-count--warning">${summary ? summary.open_warning : "—"}</td>
      <td class="monitor-count monitor-count--info">${summary ? summary.open_info : "—"}</td>
      <td><span class="monitor-client-state monitor-client-state--${state}">${icon(meta.icon, "icon icon--tiny")}${escapeHtml(meta.label)}</span></td>
    </tr>`;
}

// --- Rendering: change feed ------------------------------------------------

// Only relevant when `severity` is unset (viewing "all severities"): Info
// change types are noisy (content_changed fires on every edited page) and
// default to collapsed. Picking "Info" explicitly from the dropdown always
// shows them, independent of the toggle.
function passesInfoToggle(change) {
  if (current.severity === "info") return true;
  if (change.severity !== "info") return true;
  return current.info;
}

function filteredFeed() {
  const needle = current.type.trim().toLocaleLowerCase("en-US");
  return (lastData.feedRaw || []).filter((change) => {
    if (current.client && change.client_id !== current.client) return false;
    if (needle) {
      const haystack = `${change.change_type} ${changeTypeLabel(change.change_type)}`.toLocaleLowerCase("en-US");
      if (!haystack.includes(needle)) return false;
    }
    return passesInfoToggle(change);
  });
}

function feedFiltersActive() {
  return Boolean(current.severity || current.type || current.client || current.state !== "open");
}

function renderFeedSection() {
  const host = root.querySelector("#mon-feed");
  if (!host || !lastData) return;
  const clientOptions = [...lastData.clients]
    .sort((a, b) => a.name.localeCompare(b.name))
    .map((client) => `<option value="${escapeHtml(client.client_id)}" ${client.client_id === current.client ? "selected" : ""}>${escapeHtml(client.name)}</option>`)
    .join("");
  const infoToggleDisabled = current.severity === "critical" || current.severity === "warning";

  host.innerHTML = `
    <h2>Change feed</h2>
    <div class="monitor-feed__filters" role="group" aria-label="Change feed filters">
      <label class="monitor-feed__filter">
        <span class="visually-hidden">Severity</span>
        <select id="mon-f-severity">
          ${SEVERITY_OPTIONS.map((option) => `<option value="${option.key}" ${option.key === current.severity ? "selected" : ""}>${option.label}</option>`).join("")}
        </select>
      </label>
      <label class="monitor-feed__filter">
        <span class="visually-hidden">Change type</span>
        <input type="search" id="mon-f-type" placeholder="Filter by type…" value="${escapeHtml(current.type)}">
      </label>
      <label class="monitor-feed__filter">
        <span class="visually-hidden">Client</span>
        <select id="mon-f-client">
          <option value="">All clients</option>
          ${clientOptions}
        </select>
      </label>
      <div class="monitor-feed__filter period-panel__kinds" role="group" aria-label="State">
        <button type="button" data-feed-state="open" aria-pressed="${current.state === "open"}">Open</button>
        <button type="button" data-feed-state="all" aria-pressed="${current.state === "all"}">All</button>
      </div>
      <label class="monitor-feed__filter monitor-feed__info-toggle">
        <input type="checkbox" id="mon-f-info" ${current.info ? "checked" : ""} ${infoToggleDisabled ? "disabled" : ""}>
        <span>Show info-level changes</span>
      </label>
    </div>
    <div id="mon-feed-list"></div>`;
  renderFeedList();
}

function renderFeedList() {
  const host = root.querySelector("#mon-feed-list");
  if (!host || !lastData) return;
  const visible = filteredFeed();
  if (!visible.length) {
    host.innerHTML = feedFiltersActive()
      ? `
        <div class="state-panel">
          ${icon("filter-off")}
          <p><strong>No changes match these filters.</strong><br>Try widening the severity, state, or client filter.</p>
          <button type="button" class="button button--quiet" data-clear-feed-filters>${icon("filter-x")}Clear filters</button>
        </div>`
      : `
        <div class="state-panel">
          ${icon("cloud-off")}
          <p><strong>No changes recorded yet.</strong><br>Checks haven't found anything to report.</p>
        </div>`;
    return;
  }
  host.innerHTML = `
    <p class="monitor-feed__count">${visible.length} change${visible.length === 1 ? "" : "s"}</p>
    <ul class="monitor-change-list" aria-label="Portfolio change feed">
      ${visible.map((change) => renderChangeRow(change)).join("")}
    </ul>`;
}
