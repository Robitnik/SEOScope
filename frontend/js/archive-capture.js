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

// 1.5s rather than the old 2s: the counters behind this page are themselves
// only flushed server-side every 20 URLs or 5 seconds, and a slower poll on
// top of that is what made a live capture's numbers look frozen.
const POLL_INTERVAL_MS = 1500;
// Redraws the elapsed-time-dependent parts (stall badge, ETA) between polls,
// so a capture that has stopped advancing visibly counts up instead of
// sitting on whatever the last poll happened to render.
const TICK_INTERVAL_MS = 1000;
// A running capture whose counters, log and current URL have all stood still
// for this long is reported as stalled. Deliberately shorter than the
// server's own 90s heartbeat-staleness window, so the operator sees the
// stall BEFORE the scheduler reclaims the capture and marks it failed.
const STALL_AFTER_MS = 45000;

// Why a capture ended the way it did, in the operator's terms. `error_code`
// is a machine token (`interrupted`, `crashed`) and on its own tells nobody
// what to do next; `error_message` is frequently EMPTY for exactly the
// failures that need explaining most.
const ERROR_CODE_HELP = {
  interrupted:
    "The scheduler saw no heartbeat from this capture for 90 seconds and reclaimed it as dead — "
    + "usually a worker restart, or database write contention severe enough that the capture "
    + "could not report in. Pages archived before that point were kept: re-running resumes from "
    + "the frontier, it does not start over.",
  crashed:
    "The capture thread hit an error it could not recover from. The last URL it reached is the "
    + "final line of the log below.",
};
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
let tickTimer = null;

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
    // Client-side liveness and incremental-render bookkeeping — see
    // `noteAdvance` and `appendNewLogRows`.
    progressSignature: "",
    lastAdvanceAt: Date.now(),
    renderedStatus: null,
    renderedCancelRequested: null,
    logRenderedCount: 0,
    logRenderedFilter: "",
    logHasPlaceholder: false,
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
    state.progressSignature = "";  // a fresh load restarts the stall clock
    noteAdvance();
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
  if (!tickTimer) tickTimer = setInterval(onTick, TICK_INTERVAL_MS);
  if (pollTimer) return;
  pollTimer = setInterval(pollQuietly, POLL_INTERVAL_MS);
}

function stopPolling() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = null;
  if (tickTimer) clearInterval(tickTimer);
  tickTimer = null;
}

// Between polls nothing NEW is known — but "how long has it been stuck" and
// "how much longer" both move on their own, and a stall is precisely the
// case where no poll will ever bring fresh data to redraw on.
function onTick() {
  if (!state || !state.capture) return;
  if (root.hasAttribute("hidden")) { stopPolling(); return; }
  updateProgressInPlace(state.capture);
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
  noteAdvance();
  renderLive();
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
      ${c.force ? ` · <span class="tag-new">forced</span>` : ""}
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
        <span class="archive-progress-summary__count" id="ac-progress-count">${escapeHtml(progressCountText(c))}</span>
        ${cancellable ? `<button type="button" class="button button--quiet button--small" id="ac-cancel" ${state.cancelling ? "disabled" : ""}>${icon("player-stop", "icon")}Cancel</button>` : ""}
        <span id="ac-progress-flags">${progressFlagsMarkup(c)}</span>
      </div>
      <dl class="archive-capture-counts">
        <div><dt>New</dt><dd id="ac-count-new">${c.urls_new}</dd></div>
        <div><dt>Unchanged</dt><dd id="ac-count-unchanged">${c.urls_unchanged}</dd></div>
        <div><dt>Failed</dt><dd id="ac-count-failed">${c.urls_failed}</dd></div>
        <div><dt>Blocked</dt><dd id="ac-count-blocked">${c.urls_blocked}</dd></div>
        <div><dt>Assets fetched</dt><dd id="ac-count-assets">${c.assets_fetched}</dd></div>
      </dl>
      ${failureMarkup(c)}
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
          <span class="muted" id="ac-log-meta">${escapeHtml(logMetaText())}</span>
        </div>
        <ul class="archive-log-list" id="ac-log-list"></ul>
      </div>
    </div>`;
  state.renderedStatus = c.status;
  state.renderedCancelRequested = c.cancel_requested;
  container.querySelector("#ac-log-level").value = state.logLevelFilter;
  container.querySelector("#ac-log-level").addEventListener("change", (event) => {
    state.logLevelFilter = event.target.value;
    renderLog();
  });
  renderLog();
}

// "Pages visited" is captured + failed + blocked, NOT `urls_captured` alone.
// `urls_captured` counts only the pages whose bytes were stored, so a run
// through a stretch of failing or blocked URLs left the bar and the count
// standing perfectly still while the crawler was in fact working through
// them — indistinguishable, on screen, from the capture being wedged.
function progressStats(c) {
  const total = c.urls_total || 0;
  const done = c.urls_captured + c.urls_failed + c.urls_blocked;
  const startedMs = c.started_at ? Date.parse(c.started_at) : NaN;
  const elapsedSeconds = Number.isFinite(startedMs) ? (Date.now() - startedMs) / 1000 : 0;
  // Below ~5s the rate is dominated by start-up and reads as nonsense
  // ("340 pages/min" for the first two pages), so it simply is not shown.
  const perMinute = elapsedSeconds > 5 && done > 0 ? (done / elapsedSeconds) * 60 : null;
  const remaining = total ? Math.max(total - done, 0) : 0;
  const etaSeconds = perMinute && remaining ? (remaining / perMinute) * 60 : null;
  return { total, done, perMinute, remaining, etaSeconds };
}

function progressBarMarkup(c) {
  if (!c.urls_total) {
    return `<progress class="archive-progress-summary__bar" id="ac-progress-bar" aria-label="Capture starting"></progress>`;
  }
  const { total, done } = progressStats(c);
  return `<progress class="archive-progress-summary__bar" id="ac-progress-bar" max="${total}" value="${done}"
    aria-label="${done} of ${total} pages visited"></progress>`;
}

function progressCountText(c) {
  if (c.status === "queued") return c.queue_position ? `#${c.queue_position} in queue` : "Queued";
  const stats = progressStats(c);
  if (!stats.total) return "Starting…";
  const percent = Math.round((stats.done / stats.total) * 100);
  const parts = [`${stats.done} / ${stats.total} pages · ${percent}%`];
  if (c.status === "running") {
    if (stats.perMinute) parts.push(`${stats.perMinute.toFixed(1)} pages/min`);
    // An ETA extrapolated from a rate that has already stopped applying is
    // worse than no ETA, so a stalled capture shows none.
    if (stats.etaSeconds && !isStalled()) parts.push(`~${formatDuration(stats.etaSeconds)} left`);
  }
  if (c.progress_message) parts.push(c.progress_message);
  return parts.join(" — ");
}

function formatDuration(seconds) {
  const total = Math.max(Math.round(seconds), 0);
  if (total < 60) return `${total}s`;
  const minutes = Math.floor(total / 60);
  if (minutes < 60) return total % 60 ? `${minutes}m ${total % 60}s` : `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  return minutes % 60 ? `${hours}h ${minutes % 60}m` : `${hours}h`;
}

function isStalled() {
  if (!state.capture || state.capture.status !== "running") return false;
  return Date.now() - state.lastAdvanceAt >= STALL_AFTER_MS;
}

function progressFlagsMarkup(c) {
  const flags = [];
  if (c.cancel_requested && c.status === "running") {
    flags.push(`<span class="tag-new" data-tone="warning">Stopping…</span>`);
  }
  if (isStalled()) {
    const stalledFor = formatDuration((Date.now() - state.lastAdvanceAt) / 1000);
    flags.push(`<span class="tag-new" data-tone="warning">No progress for ${escapeHtml(stalledFor)}</span>`);
  }
  return flags.join("");
}

// Everything this page can observe about whether the capture is still
// moving, in one comparable string.
function progressSignature() {
  const c = state.capture;
  if (!c) return "";
  return [
    c.status, c.urls_captured, c.urls_failed, c.urls_blocked,
    state.lastSeq, c.progress_message || "",
  ].join("|");
}

// Stall detection is done HERE, on the client, rather than read off the
// capture row's own `heartbeat_at`: that heartbeat is written through the
// same serialized write queue as the capture's commits, so under the write
// contention that actually causes a stall it is the first thing to stop
// being written — it cannot report on the condition it is a casualty of.
// What this page can always tell is that nothing it polls has changed.
function noteAdvance() {
  const signature = progressSignature();
  if (signature === state.progressSignature) return;
  state.progressSignature = signature;
  state.lastAdvanceAt = Date.now();
}

function failureMarkup(c) {
  // `error_message` is an empty STRING (not null) on a reclaimed capture, so
  // the old `c.error_message ? … : ""` test rendered nothing at all for it —
  // which is how a run showed a bare "FAILED" stating no reason whatsoever
  // (live: ALPHA, 2026-09-06, `error_code=interrupted`).
  const parts = [];
  if (c.error_code) parts.push(`<strong>${escapeHtml(c.error_code)}</strong>`);
  if (c.error_message) parts.push(escapeHtml(c.error_message));
  const help = c.error_code ? ERROR_CODE_HELP[c.error_code] : null;
  if (help) parts.push(escapeHtml(help));
  if (!parts.length) return "";
  return `<div class="state-panel state-failed">${icon("alert-circle")}<p>${parts.join("<br>")}</p></div>`;
}

// A poll that brought no structural change must not rebuild the body: doing
// so drops the operator's log scroll position and any focus they held in the
// toolbar, every 1.5 seconds. Only a status (or cancel-state) change alters
// the body's actual shape, and only that rebuilds.
function renderLive() {
  const c = state.capture;
  if (!c) { render(); return; }
  if (c.status !== state.renderedStatus || c.cancel_requested !== state.renderedCancelRequested) {
    render();
    return;
  }
  renderHeader();
  updateProgressInPlace(c);
  appendNewLogRows();
}

function updateProgressInPlace(c) {
  const bar = root.querySelector("#ac-progress-bar");
  const stats = progressStats(c);
  if (bar && stats.total) {
    bar.max = stats.total;
    bar.value = stats.done;
    bar.setAttribute("aria-label", `${stats.done} of ${stats.total} pages visited`);
  }
  const count = root.querySelector("#ac-progress-count");
  if (count) count.textContent = progressCountText(c);
  const flags = root.querySelector("#ac-progress-flags");
  if (flags) flags.innerHTML = progressFlagsMarkup(c);
  const cells = {
    new: c.urls_new, unchanged: c.urls_unchanged, failed: c.urls_failed,
    blocked: c.urls_blocked, assets: c.assets_fetched,
  };
  for (const [key, value] of Object.entries(cells)) {
    const cell = root.querySelector(`#ac-count-${key}`);
    if (cell) cell.textContent = String(value);
  }
}

function logMetaText() {
  const total = state.events.length;
  if (!total) return "No lines yet";
  const shown = visibleLogs().length;
  return shown === total ? `${total} lines` : `${shown} of ${total} lines`;
}

function updateLogMeta() {
  const meta = root.querySelector("#ac-log-meta");
  if (meta) meta.textContent = logMetaText();
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
  state.logHasPlaceholder = rows.length === 0;
  host.innerHTML = rows.length
    ? rows.map(renderLogRow).join("")
    : `<li class="archive-log-row"><span></span><span class="muted">No log lines yet.</span></li>`;
  state.logRenderedCount = state.events.length;
  state.logRenderedFilter = state.logLevelFilter;
  updateLogMeta();
  if (!state.logPaused) host.scrollTop = host.scrollHeight;
}

// Appends only the lines that arrived since the last render rather than
// re-serializing the whole list on every poll. A whole-client capture writes
// one line per URL (1565 of them for ALPHA), and rebuilding all of them
// every 1.5s both cost real time on screen and threw away the scroll
// position of an operator reading back through the log.
function appendNewLogRows() {
  const host = root.querySelector("#ac-log-list");
  if (!host) return;
  if (state.logRenderedFilter !== state.logLevelFilter) { renderLog(); return; }
  const fresh = state.events.slice(state.logRenderedCount);
  state.logRenderedCount = state.events.length;
  const rows = state.logLevelFilter
    ? fresh.filter((event) => event.level === state.logLevelFilter)
    : fresh;
  if (rows.length) {
    if (state.logHasPlaceholder) {
      host.innerHTML = "";
      state.logHasPlaceholder = false;
    }
    host.insertAdjacentHTML("beforeend", rows.map(renderLogRow).join(""));
  }
  updateLogMeta();
  // Paused means "hold my place", so a paused log gains rows without being
  // scrolled away from what the operator stopped on.
  if (rows.length && !state.logPaused) host.scrollTop = host.scrollHeight;
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
