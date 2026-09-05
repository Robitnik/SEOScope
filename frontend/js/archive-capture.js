// Web Archive — one capture's live progress + log (task-62 §1.5). Reached
// from a row on the hub's "Queue & activity" list; the route carries `id`
// so F5 restores the exact same capture from the API (frontend/AGENTS.md §9)
// instead of losing state.
//
// Polling, not SSE (task-62 §2.6): a capture produces roughly one event per
// URL, and the slowest measured pace is single digits of seconds per URL —
// nowhere near the latency SSE exists to shave off, and it would add the
// `connecting|reconnecting` state machine AGENTS §6 requires for a live
// stream for no real benefit here.
import {
  cancelArchiveCapture, getArchiveCapture, getArchiveCaptureEvents, isAbortError,
} from "./api.js?v=20260905-t62";
import { buildArchiveHash } from "./router.js?v=20260905-t62";
import { escapeHtml, formatTimestamp, icon, statusIcon } from "./formatters.js?v=20260905-t62";
import { sectionSkeleton } from "./skeleton.js?v=20260905-t62";

const root = document.querySelector("#archive-capture-page-root");

const POLL_INTERVAL_MS = 2000;
const TERMINAL_STATUSES = new Set(["ok", "partial", "failed", "blocked", "cancelled"]);

const LOG_LEVEL_META = {
  debug: { label: "Debug", icon: "bug" },
  info: { label: "Info", icon: "info-circle" },
  warning: { label: "Warning", icon: "alert-triangle" },
  error: { label: "Error", icon: "alert-circle" },
};

const STATUS_META = {
  queued: { label: "Queued", icon: "clock" },
  running: { label: "Running", icon: "loader-2" },
  ok: { label: "OK", icon: "circle-check" },
  partial: { label: "Partial", icon: "alert-triangle" },
  failed: { label: "Failed", icon: "circle-x" },
  blocked: { label: "Blocked", icon: "shield-lock" },
  cancelled: { label: "Cancelled", icon: "player-stop" },
};

const logTimeFormatter = new Intl.DateTimeFormat("en-US", {
  hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
});

let state = null;
let pollTimer = null;

function toast(message) {
  const region = document.querySelector("#toast-region");
  const element = document.createElement("div");
  element.className = "toast-message";
  element.textContent = message;
  region.append(element);
  setTimeout(() => element.remove(), 4200);
}

export async function initArchiveCapturePage(query) {
  const id = query.get("id");
  state = {
    id,
    capture: null,
    loading: true,
    error: null,
    events: [],
    lastSeq: 0,
    logPaused: false,
    logLevelFilter: "",
    cancelling: false,
  };
  stopPolling();
  root.innerHTML = shell();
  bindStaticActions();
  if (!id) {
    state.loading = false;
    state.error = new Error("No capture id in the URL.");
    render();
    return;
  }
  await loadAll();
}

function shell() {
  return `
    <header class="archive-hero archive-hero--compact">
      <a class="back-link" href="${buildArchiveHash()}">${icon("arrow-left")}Back to the archive</a>
      <div id="ac-header"></div>
    </header>
    <section id="ac-body" aria-live="polite">
      ${sectionSkeleton({ label: "this capture", rows: 4, columns: 1 })}
    </section>`;
}

function bindStaticActions() {
  root.addEventListener("click", onRootClick);
}

function onRootClick(event) {
  const cancelButton = event.target.closest("#ac-cancel");
  if (cancelButton) { void onCancel(); return; }
  const pauseButton = event.target.closest("#ac-log-pause");
  if (pauseButton) {
    state.logPaused = !state.logPaused;
    renderLog();
    return;
  }
  if (event.target.closest("#ac-log-copy")) { void copyLog(); return; }
  const levelSelect = event.target.closest("#ac-log-level");
  if (levelSelect) return; // handled by its own change listener, bound in renderLog
}

async function loadAll() {
  state.loading = true;
  state.error = null;
  render();
  try {
    const [capture, events] = await Promise.all([
      getArchiveCapture(state.id, { requestKey: `archive-capture-detail-${state.id}` }),
      getArchiveCaptureEvents(state.id, { since: 0, requestKey: `archive-capture-events-${state.id}` }),
    ]);
    state.capture = capture;
    state.events = events;
    state.lastSeq = events.reduce((max, e) => Math.max(max, e.seq), 0);
  } catch (error) {
    if (isAbortError(error)) return;
    state.capture = null;
    state.error = error;
  }
  state.loading = false;
  render();
  syncPolling();
}

function syncPolling() {
  if (!state.capture || TERMINAL_STATUSES.has(state.capture.status)) {
    stopPolling();
    return;
  }
  if (pollTimer) return;
  pollTimer = setInterval(pollQuietly, POLL_INTERVAL_MS);
}

function stopPolling() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = null;
}

// No skeleton, no `loading` flip — a poll must never flash the page the
// operator is reading back to placeholders every two seconds.
async function pollQuietly() {
  // The router hides pages with `hidden` rather than removing them, so that
  // attribute — not `isConnected` — is what says "the operator left"
  // (same convention `archive-page.js` uses for its own live poll).
  if (root.hasAttribute("hidden")) {
    stopPolling();
    return;
  }
  try {
    const [capture, newEvents] = await Promise.all([
      getArchiveCapture(state.id, { requestKey: `archive-capture-detail-${state.id}` }),
      getArchiveCaptureEvents(
        state.id, { since: state.lastSeq, requestKey: `archive-capture-events-${state.id}` },
      ),
    ]);
    state.capture = capture;
    if (newEvents.length) {
      state.events = state.events.concat(newEvents);
      state.lastSeq = newEvents.reduce((max, e) => Math.max(max, e.seq), state.lastSeq);
    }
  } catch (error) {
    if (isAbortError(error)) return;
    return; // a failed poll keeps the last good state on screen
  }
  render();
  syncPolling();
}

function render() {
  renderHeader();
  renderBody();
}

function renderHeader() {
  const host = root.querySelector("#ac-header");
  if (!host) return;
  if (!state.capture) { host.innerHTML = ""; return; }
  const c = state.capture;
  const meta = STATUS_META[c.status] || { label: c.status, icon: "circle" };
  const targetLabel = c.scope === "url" && c.target_url
    ? escapeHtml(c.target_url)
    : "whole client";
  host.innerHTML = `
    <p class="eyebrow">${escapeHtml(c.client_name)}</p>
    <h1>${statusIcon(meta.icon, "icon")}${escapeHtml(meta.label)}</h1>
    <p class="archive-hero__subtitle">
      ${targetLabel} · ${escapeHtml(c.trigger)}
      ${c.status === "queued" && c.queue_position ? ` · #${c.queue_position} in queue` : ""}
    </p>`;
}

function renderBody() {
  const container = root.querySelector("#ac-body");
  if (!container) return;

  if (state.error && !state.capture) {
    container.innerHTML = `
      <div class="state-panel state-failed">
        ${icon("wifi-off")}
        <p><strong>Could not load this capture.</strong><br>${escapeHtml(state.error.message || "")}</p>
        <button type="button" class="button button--primary" id="ac-retry">${icon("refresh")}Retry</button>
      </div>`;
    container.querySelector("#ac-retry").addEventListener("click", loadAll);
    return;
  }
  if (state.loading && !state.capture) {
    container.innerHTML = sectionSkeleton({ label: "this capture", rows: 4, columns: 1 });
    return;
  }
  if (!state.capture) {
    container.innerHTML = `<div class="state-panel state-not_configured">${icon("history-off")}<p>Capture not found.</p></div>`;
    return;
  }

  const c = state.capture;
  const cancellable = c.status === "queued" || (c.status === "running" && !c.cancel_requested);
  container.innerHTML = `
    <div class="archive-capture-detail">
      <div class="archive-progress-summary">
        ${progressBarMarkup(c)}
        <span class="archive-progress-summary__count">${progressCountText(c)}</span>
        ${cancellable ? `<button type="button" class="button button--quiet button--small" id="ac-cancel" ${state.cancelling ? "disabled" : ""}>${icon("player-stop", "icon")}Cancel</button>` : ""}
        ${c.cancel_requested && c.status === "running" ? `<span class="tag-new" data-tone="warning">Stopping…</span>` : ""}
      </div>
      <dl class="archive-capture-counts">
        <div><dt>New</dt><dd>${c.urls_new}</dd></div>
        <div><dt>Unchanged</dt><dd>${c.urls_unchanged}</dd></div>
        <div><dt>Failed</dt><dd>${c.urls_failed}</dd></div>
        <div><dt>Blocked</dt><dd>${c.urls_blocked}</dd></div>
        <div><dt>Assets fetched</dt><dd>${c.assets_fetched}</dd></div>
      </dl>
      ${c.error_message ? `<div class="state-panel state-failed">${icon("alert-circle")}<p>${escapeHtml(c.error_message)}</p></div>` : ""}
      <div class="section-heading"><div><p class="eyebrow">Log</p><h2>What happened</h2></div></div>
      <div class="archive-log-panel">
        <div class="archive-log-toolbar">
          <select id="ac-log-level" aria-label="Filter by level">
            <option value="">All levels</option>
            <option value="debug">Debug</option>
            <option value="info">Info</option>
            <option value="warning">Warning</option>
            <option value="error">Error</option>
          </select>
          <button type="button" class="button button--quiet button--small" id="ac-log-pause" aria-pressed="${state.logPaused}">
            ${icon(state.logPaused ? "player-play" : "player-pause", "icon icon--tiny")}${state.logPaused ? "Resume" : "Pause"}
          </button>
          <button type="button" class="button button--quiet button--small" id="ac-log-copy">${icon("copy", "icon icon--tiny")}Copy</button>
        </div>
        <ul class="archive-log-list" id="ac-log-list"></ul>
      </div>
    </div>`;
  container.querySelector("#ac-log-level").value = state.logLevelFilter;
  container.querySelector("#ac-log-level").addEventListener("change", (event) => {
    state.logLevelFilter = event.target.value;
    renderLog();
  });
  renderLog();
}

function progressBarMarkup(c) {
  if (!c.urls_total) {
    return `<progress class="archive-progress-summary__bar" aria-label="Capture starting"></progress>`;
  }
  return `<progress class="archive-progress-summary__bar" max="${c.urls_total}" value="${c.urls_captured}"
    aria-label="${c.urls_captured} of ${c.urls_total} URLs captured"></progress>`;
}

function progressCountText(c) {
  if (c.status === "queued") return c.queue_position ? `#${c.queue_position} in queue` : "Queued";
  if (!c.urls_total) return "Starting…";
  const parts = [`${c.urls_captured} / ${c.urls_total} URLs`];
  if (c.progress_message) parts.push(c.progress_message);
  return parts.join(" — ");
}

function visibleLogs() {
  return state.logLevelFilter
    ? state.events.filter((event) => event.level === state.logLevelFilter)
    : state.events;
}

function renderLog() {
  const host = root.querySelector("#ac-log-list");
  if (!host) return;
  const rows = visibleLogs();
  host.innerHTML = rows.length
    ? rows.map(renderLogRow).join("")
    : `<li class="archive-log-row"><span></span><span class="muted">No log lines yet.</span></li>`;
  if (!state.logPaused) host.scrollTop = host.scrollHeight;
}

function renderLogRow(event) {
  const meta = LOG_LEVEL_META[event.level] || LOG_LEVEL_META.info;
  const parsed = Date.parse(event.created_at);
  const time = Number.isFinite(parsed) ? logTimeFormatter.format(new Date(parsed)) : "";
  return `
    <li class="archive-log-row archive-log-row--${escapeHtml(event.level)}">
      <time datetime="${escapeHtml(event.created_at)}">${escapeHtml(time)}</time>
      <span class="archive-log-row__level">${icon(meta.icon, "icon icon--tiny")}${escapeHtml(meta.label)}</span>
      <span class="archive-log-row__message">${escapeHtml(event.message)}</span>
    </li>`;
}

async function copyLog() {
  const text = visibleLogs().map((event) => `${event.created_at}\t${event.level}\t${event.message}`).join("\n");
  try {
    await navigator.clipboard.writeText(text);
    toast("Log copied.");
  } catch {
    toast("Could not copy — clipboard is unavailable in this browser.");
  }
}

async function onCancel() {
  if (state.cancelling) return;
  state.cancelling = true;
  renderBody();
  try {
    const result = await cancelArchiveCapture(state.id);
    toast(result.status === "cancelled" ? "Capture cancelled." : "Cancellation requested — it will stop shortly.");
    await loadAll();
  } catch (error) {
    if (!isAbortError(error)) toast(`Could not cancel: ${error.message}`);
  } finally {
    state.cancelling = false;
    renderBody();
  }
}
