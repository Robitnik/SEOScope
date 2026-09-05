// Web Archive hub (task-59) — top-level page, no client id, same shape as
// trends-page.js/monitor-page.js: `root.innerHTML = shell()` on every
// `initArchivePage` call, state kept in a module-level object.
//
// The operator's job here: paste a URL, land on its timeline of captured
// versions. Below the input — portfolio coverage, storage, recent capture
// health, and the pages that change most often — so the hub also answers
// "is the archive actually working" without opening a client.
import {
  getArchiveOverview, getArchiveQueue, getArchiveUrlTimeline, lookupArchiveUrl,
  startAllArchiveCaptures, startArchiveCapture, startArchiveUrlCapture,
  startArchiveAdhocUrlCapture, isAbortError,
} from "./api.js?v=20260905-t63";
import {
  buildArchiveCaptureHash, buildArchiveViewHash, buildArchiveChangesHash,
} from "./router.js?v=20260905-t63";
import {
  escapeHtml, formatFileSize, formatTimestamp, icon, statusIcon,
} from "./formatters.js?v=20260821-spin3";
import { sectionSkeleton, panelSkeleton } from "./skeleton.js?v=20260821-spin3";
import {
  calendarMarkup, bindCalendarActions, groupSnapshotsByDay, reasonLabel,
} from "./archive-calendar.js?v=20260905-t63";

const root = document.querySelector("#archive-page-root");

// While any capture is running the panel is a live view, not a snapshot of
// page-load time: without this the counters sit at whatever they were when
// the operator opened the hub, which reads as a stuck capture.
const LIVE_POLL_INTERVAL_MS = 5000;

let state = null;
let livePollTimer = null;

function toast(message) {
  const region = document.querySelector("#toast-region");
  const element = document.createElement("div");
  element.className = "toast-message";
  element.textContent = message;
  region.append(element);
  setTimeout(() => element.remove(), 4200);
}

export async function initArchivePage(query) {
  state = {
    urlInput: query.get("url") || "",
    lookupAttempted: false,
    lookupLoading: false,
    lookupError: null,
    lookupUrl: null, // the ArchiveUrlDTO found
    timeline: null, // { url, snapshots } once a lookup resolves to a tracked URL
    overview: null,
    overviewLoading: true,
    overviewError: null,
    queue: null, // ArchiveQueueDTO — running/queued/recent (task-62 §1.4)
    captureStarting: null, // client_id currently being triggered
    urlCaptureStarting: false, // "Refresh now" on the currently looked-up URL
    allCapturesStarting: false,
    adhocCaptureStarting: false, // "Archive this page now" on a NEVER-seen URL
    adhocCaptureError: null,
  };
  stopLivePolling();
  root.innerHTML = shell();
  bindStaticActions();
  loadOverview();
  if (state.urlInput) await runLookup(state.urlInput);
}

function shell() {
  return `
    <header class="archive-hero">
      <p class="eyebrow">Web Archive</p>
      <h1>Enter a URL</h1>
      <p class="archive-hero__subtitle">
        Every capture of a client page, byte-for-byte — pick a version and walk it like the live site.
      </p>
      <form id="archive-lookup-form" class="archive-lookup-form" role="search">
        <label class="search-field archive-lookup-field">
          <span class="visually-hidden">URL to look up</span>
          ${icon("world-search")}
          <input type="url" id="archive-url-input" placeholder="https://client-site.com/some/page" inputmode="url" autocomplete="off" value="${escapeHtml(state.urlInput)}" required>
        </label>
        <button type="submit" class="button button--primary">${icon("search")}Look up</button>
      </form>
      <a class="archive-hero__changes-link" href="${buildArchiveChangesHash()}">${icon("timeline")}What changed, and when</a>
    </header>
    <section id="archive-lookup-result" aria-live="polite"></section>
    <section id="archive-overview" aria-live="polite" aria-busy="true">
      ${sectionSkeleton({ label: "the archive overview", rows: 6, columns: 5 })}
    </section>`;
}

function bindStaticActions() {
  root.querySelector("#archive-lookup-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const value = root.querySelector("#archive-url-input").value.trim();
    if (value) runLookup(value);
  });
}

// --- lookup --------------------------------------------------------------

async function runLookup(url) {
  state.urlInput = url;
  state.lookupAttempted = true;
  state.lookupLoading = true;
  state.lookupError = null;
  state.lookupUrl = null;
  state.timeline = null;
  state.adhocCaptureError = null;
  renderLookup();
  try {
    const result = await lookupArchiveUrl(url);
    if (!result.found) {
      state.lookupUrl = null;
    } else {
      state.lookupUrl = result.url;
      state.timeline = await getArchiveUrlTimeline(result.url.id);
      const newest = state.timeline.snapshots[0];
      if (newest) {
        const [year, month] = newest.observed_at.slice(0, 7).split("-").map(Number);
        state.calendarYear = year;
        state.calendarMonth = month - 1; // 0-indexed
        state.selectedDay = newest.observed_at.slice(0, 10);
      }
    }
  } catch (error) {
    if (isAbortError(error)) return;
    state.lookupError = error;
  }
  state.lookupLoading = false;
  renderLookup();
}

function renderLookup() {
  const container = root.querySelector("#archive-lookup-result");
  if (!container) return;

  if (state.lookupLoading) {
    container.innerHTML = panelSkeleton({ label: "this URL's archive", rows: 3, columns: 1 });
    return;
  }
  if (state.lookupError) {
    container.innerHTML = `
      <div class="state-panel state-failed">
        ${icon("wifi-off")}
        <p><strong>Could not look up this URL.</strong><br>${escapeHtml(state.lookupError.message || "")}</p>
        <button type="button" class="button button--primary" id="archive-lookup-retry">${icon("refresh")}Retry</button>
      </div>`;
    container.querySelector("#archive-lookup-retry").addEventListener("click", () => runLookup(state.urlInput));
    return;
  }
  if (!state.lookupAttempted) { container.innerHTML = ""; return; }
  if (state.timeline === null) {
    container.innerHTML = `
      <div class="state-panel state-not_configured archive-not-archived">
        ${icon("history-off")}
        <p><strong>Not archived yet.</strong><br>
        This URL hasn't been captured. If its client has the archive enabled, it will be picked up
        on the next discovery pass — or capture it right now instead of waiting.</p>
        <button type="button" class="button button--primary" id="archive-adhoc-capture" ${state.adhocCaptureStarting ? "disabled" : ""}>
          ${icon(state.adhocCaptureStarting ? "loader-2" : "camera", state.adhocCaptureStarting ? "icon icon--spin" : "icon")}
          ${state.adhocCaptureStarting ? "Starting…" : "Archive this page now"}
        </button>
        ${state.adhocCaptureError ? `<p class="archive-not-archived__error">${icon("alert-triangle")}${escapeHtml(state.adhocCaptureError)}</p>` : ""}
      </div>`;
    container.querySelector("#archive-adhoc-capture").addEventListener("click", () => triggerAdhocCapture(state.urlInput));
    return;
  }

  const { url, snapshots } = state.timeline;
  if (snapshots.length === 0) {
    container.innerHTML = `
      <div class="state-panel state-not_configured">
        ${icon("history-off")}
        <p><strong>${escapeHtml(url.client_name)}</strong> is tracked, but no version has been captured yet.</p>
        <button type="button" class="button button--primary" id="archive-refresh-url" ${state.urlCaptureStarting ? "disabled" : ""}>
          ${icon(state.urlCaptureStarting ? "loader-2" : "camera", state.urlCaptureStarting ? "icon icon--spin" : "icon")}
          ${state.urlCaptureStarting ? "Starting…" : "Archive this page now"}
        </button>
      </div>`;
    container.querySelector("#archive-refresh-url").addEventListener("click", () => triggerUrlCapture(url.id));
    return;
  }

  const groups = groupSnapshotsByDay(snapshots);
  container.innerHTML = `
    <div class="archive-result">
      <div class="section-heading">
        <div>
          <p class="eyebrow">${escapeHtml(url.client_name)}</p>
          <h2>${escapeHtml(url.url)}</h2>
          <p class="archive-result__meta">${snapshots.length} version${snapshots.length === 1 ? "" : "s"} captured
          ${url.gone_since ? `<span class="tag-new" data-tone="warning">gone since ${formatTimestamp(url.gone_since)}</span>` : ""}</p>
        </div>
        <button type="button" class="button button--quiet" id="archive-refresh-url" ${state.urlCaptureStarting ? "disabled" : ""}>
          ${icon(state.urlCaptureStarting ? "loader-2" : "refresh", state.urlCaptureStarting ? "icon icon--spin" : "icon")}Refresh now
        </button>
      </div>
      ${calendarMarkup({ year: state.calendarYear, month: state.calendarMonth, selectedDay: state.selectedDay, groups })}
      ${selectedDayMarkup(groups)}
    </div>`;
  bindCalendarActions(container, {
    onPrevMonth: () => shiftCalendarMonth(-1),
    onNextMonth: () => shiftCalendarMonth(1),
    onSelectDay: (day) => {
      state.selectedDay = day;
      renderLookup();
    },
  });
  container.querySelector("#archive-refresh-url").addEventListener("click", () => triggerUrlCapture(url.id));
}

// One URL, seconds — not the whole client's list (task-62 §1.1/§2.7). The
// operator is already looking at a stale page; the answer is "go fetch
// THIS one", not "re-run everything this client owns".
async function triggerUrlCapture(urlId) {
  if (state.urlCaptureStarting) return;
  state.urlCaptureStarting = true;
  renderLookup();
  try {
    await startArchiveUrlCapture(urlId);
    toast("Refresh queued — check back in a few seconds.");
  } catch (error) {
    if (!isAbortError(error)) toast(`Could not refresh this URL: ${error.message}`);
  } finally {
    state.urlCaptureStarting = false;
    renderLookup();
  }
}

// "Archive this page now" on a URL that has NEVER been seen before —
// `startArchiveUrlCapture` needs an `ArchiveUrl.id` this URL doesn't have
// yet; this resolves a client from the URL's own host and creates that row
// on the spot (task-63 follow-up, live-reported 2026-09-05). Navigates
// straight to the capture's own live-progress page (task-62) instead of
// leaving the operator staring at the same empty lookup panel with nothing
// to watch.
async function triggerAdhocCapture(url) {
  if (state.adhocCaptureStarting) return;
  state.adhocCaptureStarting = true;
  state.adhocCaptureError = null;
  renderLookup();
  try {
    const result = await startArchiveAdhocUrlCapture(url);
    location.hash = buildArchiveCaptureHash({ id: result.capture_id });
  } catch (error) {
    if (isAbortError(error)) return;
    state.adhocCaptureError =
      error.code === "client_not_found"
        ? "Couldn't match this URL to a known client — check the domain, or ask an operator to add it."
        : error.code === "already_running"
          ? "A capture is already running for this client — try again in a moment."
          : `Could not start the capture: ${error.message}`;
    state.adhocCaptureStarting = false;
    renderLookup();
  }
}

function selectedDayMarkup(groups) {
  const points = (state.selectedDay && groups.get(state.selectedDay)) || [];
  if (points.length === 0) {
    return `<p class="archive-empty-note">Pick a highlighted day to see its captured versions.</p>`;
  }
  return `
    <div class="archive-timeline-day">
      <p class="archive-timeline-day__label">${escapeHtml(state.selectedDay)}</p>
      <ul class="archive-timeline-points">
        ${points.map((point) => `
          <li>
            <a class="archive-timeline-point" href="${buildArchiveViewHash({ snapshot: point.id, url: state.lookupUrl.url })}">
              ${icon(point.has_change ? "point-filled" : "point", `icon archive-timeline-point__dot${point.has_change ? " archive-timeline-point__dot--changed" : ""}`)}
              <span class="archive-timeline-point__time">${escapeHtml(point.observed_at.slice(11, 16))} UTC</span>
              <span class="archive-timeline-point__reason">${reasonLabel(point.store_reason)}</span>
              ${point.http_status && point.http_status >= 400 ? `<span class="tag-new" data-tone="danger">${point.http_status}</span>` : ""}
            </a>
          </li>`).join("")}
      </ul>
    </div>`;
}

function shiftCalendarMonth(delta) {
  let month = state.calendarMonth + delta;
  let year = state.calendarYear;
  if (month < 0) { month = 11; year -= 1; }
  if (month > 11) { month = 0; year += 1; }
  state.calendarMonth = month;
  state.calendarYear = year;
  renderLookup();
}

// --- overview --------------------------------------------------------------

async function loadOverview() {
  state.overviewLoading = true;
  state.overviewError = null;
  renderOverview();
  try {
    const [overview, queue] = await Promise.all([
      getArchiveOverview(),
      getArchiveQueue({ requestKey: "archive-queue" }),
    ]);
    state.overview = overview;
    state.queue = queue;
  } catch (error) {
    if (isAbortError(error)) return;
    state.overview = null;
    state.overviewError = error;
  }
  state.overviewLoading = false;
  renderOverview();
  syncLivePolling();
}

// Polls only while something is actually running, and stops itself the tick
// after the last capture finishes — an idle hub costs no requests.
function syncLivePolling() {
  const running = (state.queue?.running.length || 0) > 0 || (state.queue?.queued.length || 0) > 0;
  if (!running) {
    stopLivePolling();
    return;
  }
  if (livePollTimer) return;
  livePollTimer = setInterval(refreshOverviewQuietly, LIVE_POLL_INTERVAL_MS);
}

function stopLivePolling() {
  if (livePollTimer) clearInterval(livePollTimer);
  livePollTimer = null;
}

// No skeleton, no `overviewLoading` flip: a poll must never make the panel
// the operator is reading flash back to placeholders every five seconds.
async function refreshOverviewQuietly() {
  // The router hides pages with `hidden` rather than removing them, so
  // that attribute — not `isConnected` — is what says "the operator left".
  if (root.hasAttribute("hidden") || !root.querySelector("#archive-overview")) {
    stopLivePolling();
    return;
  }
  try {
    const [overview, queue] = await Promise.all([
      getArchiveOverview(),
      getArchiveQueue({ requestKey: "archive-queue" }),
    ]);
    state.overview = overview;
    state.queue = queue;
  } catch (error) {
    if (isAbortError(error)) return;
    return; // a failed poll keeps the last good numbers on screen
  }
  renderOverview();
  syncLivePolling();
}

function renderOverview() {
  const container = root.querySelector("#archive-overview");
  if (!container) return;
  container.setAttribute("aria-busy", state.overviewLoading ? "true" : "false");

  if (state.overviewError && !state.overview) {
    container.innerHTML = `
      <div class="state-panel state-failed">
        ${icon("wifi-off")}
        <p><strong>Could not load the archive overview.</strong><br>${escapeHtml(state.overviewError.message || "")}</p>
        <button type="button" class="button button--primary" id="archive-overview-retry">${icon("refresh")}Retry</button>
      </div>`;
    container.querySelector("#archive-overview-retry").addEventListener("click", loadOverview);
    return;
  }
  if (state.overviewLoading && !state.overview) {
    container.innerHTML = sectionSkeleton({ label: "the archive overview", rows: 6, columns: 5 });
    return;
  }
  if (!state.overview) { container.innerHTML = ""; return; }

  const o = state.overview;
  container.innerHTML = `
    ${o.archive_enabled ? "" : `<div class="state-panel state-not_configured">${icon("power")}<p><strong>The Web Archive is turned off.</strong> No captures will run until it's enabled in configuration.</p></div>`}
    <div class="archive-stat-row">
      <div class="archive-stat"><p class="archive-stat__value">${o.urls_total.toLocaleString("en-US")}</p><p class="archive-stat__label">URLs tracked</p></div>
      <div class="archive-stat"><p class="archive-stat__value">${o.snapshots_total.toLocaleString("en-US")}</p><p class="archive-stat__label">Versions stored</p></div>
      <div class="archive-stat"><p class="archive-stat__value">${o.blobs_total.toLocaleString("en-US")}</p><p class="archive-stat__label">Unique files</p></div>
      <div class="archive-stat"><p class="archive-stat__value">${o.assets_captured.toLocaleString("en-US")} / ${o.assets_referenced.toLocaleString("en-US")}</p><p class="archive-stat__label">Asset coverage</p></div>
      <div class="archive-stat"><p class="archive-stat__value">${formatFileSize(o.bytes_local)}</p><p class="archive-stat__label">Stored locally</p></div>
      <div class="archive-stat"><p class="archive-stat__value">${o.cloud_enabled ? formatFileSize(o.bytes_uploaded) : "—"}</p><p class="archive-stat__label">${o.cloud_enabled ? "In cloud storage" : "Cloud storage off"}</p></div>
      <div class="archive-stat"><p class="archive-stat__value">${escapeHtml(o.capture_times.join(" · "))}</p><p class="archive-stat__label">Daily captures (${escapeHtml(o.timezone)})</p></div>
    </div>
    <div class="section-heading">
      <div><p class="eyebrow">Coverage</p><h2>Clients</h2></div>
      <button type="button" class="button button--quiet" id="archive-capture-all" ${state.allCapturesStarting ? "disabled" : ""}>
        ${icon(state.allCapturesStarting ? "loader-2" : "player-play", state.allCapturesStarting ? "icon icon--spin" : "icon")}Capture all
      </button>
    </div>
    ${clientsTableMarkup(o.clients)}
    <div class="archive-columns">
      <div>
        <div class="section-heading"><div><p class="eyebrow">Health</p><h2>Queue &amp; activity</h2></div></div>
        ${capturesListMarkup(combinedQueueRows())}
      </div>
      <div>
        <div class="section-heading"><div><p class="eyebrow">Attention</p><h2>Changes most often</h2></div></div>
        ${topChangingMarkup(o.top_changing_urls)}
      </div>
    </div>`;
  bindOverviewActions(container);
}

// `running` + `queued` (in FIFO order) + `recent` — deduped by id, active
// rows first, so a capture that is BOTH running AND among the 10 most
// recent overall (the common case) shows once, at the top (task-62 §1.4).
function combinedQueueRows() {
  if (!state.queue) return [];
  const seen = new Set();
  const rows = [];
  for (const c of [...state.queue.running, ...state.queue.queued, ...state.queue.recent]) {
    if (seen.has(c.id)) continue;
    seen.add(c.id);
    rows.push(c);
  }
  return rows;
}

function clientsTableMarkup(clients) {
  if (clients.length === 0) {
    return `<div class="state-panel state-not_configured">${icon("users")}<p>No clients yet.</p></div>`;
  }
  return `
    <div class="table-scroll">
      <table class="analytics-table archive-clients-table">
        <thead><tr>
          <th>Client</th><th>URLs</th><th>vs. Monitor</th><th>Versions</th><th>Last capture</th><th></th>
        </tr></thead>
        <tbody>
          ${clients.map((client) => `
            <tr class="${client.enabled ? "" : "archive-row--disabled"}">
              <td class="analytics-row__value">${escapeHtml(client.client_name)}${client.enabled ? "" : ` <span class="tag-new">off</span>`}</td>
              <td>${client.urls_tracked.toLocaleString("en-US")}</td>
              <td>${client.urls_known_to_monitor.toLocaleString("en-US")}</td>
              <td>${client.snapshots_total.toLocaleString("en-US")}</td>
              <td class="analytics-row__value">${client.last_capture_at ? `${formatTimestamp(client.last_capture_at)} — ${captureStatusLabel(client.last_capture_status)}` : "Never"}</td>
              <td>
                <button type="button" class="button button--quiet button--small" data-action="capture-now" data-client-id="${escapeHtml(client.client_id)}" ${state.captureStarting === client.client_id ? "disabled" : ""}>
                  ${icon(state.captureStarting === client.client_id ? "loader-2" : "camera", state.captureStarting === client.client_id ? "icon icon--spin" : "icon")}Capture now
                </button>
              </td>
            </tr>`).join("")}
        </tbody>
      </table>
    </div>`;
}

function captureStatusLabel(status) {
  return {
    ok: "OK", partial: "Partial", failed: "Failed", blocked: "Blocked",
    running: "Running", queued: "Queued", cancelled: "Cancelled",
  }[status] || "—";
}

function capturesListMarkup(captures) {
  if (captures.length === 0) return `<p class="archive-empty-note">No captures have run yet.</p>`;
  return `<ul class="archive-capture-list">
    ${captures.map(captureRowMarkup).join("")}
  </ul>`;
}

function captureRowMarkup(c) {
  // A bar only once the capture is actually working: `queued` gets the clock
  // glyph and nothing else, because a bar there would claim progress that
  // has not started.
  const running = c.status === "running";
  const label = c.scope === "url" && c.target_url
    ? `${escapeHtml(c.client_name)}: ${escapeHtml(c.target_url)}`
    : escapeHtml(c.client_name);
  return `
      <li class="archive-capture-row state-${escapeHtml(c.status)}">
        <a class="archive-capture-row__link" href="${buildArchiveCaptureHash({ id: c.id })}">
          ${statusIcon(captureIcon(c.status), "icon")}
          <span class="analytics-row__value">${label}</span>
          <span class="archive-capture-row__counts">${captureCountsText(c)}</span>
          <span class="archive-capture-row__time">${c.finished_at ? formatTimestamp(c.finished_at) : captureProgressText(c)}</span>
        </a>
        ${running ? captureProgressBarMarkup(c) : ""}
      </li>`;
}

function captureCountsText(c) {
  const parts = [`${c.urls_new} new`, `${c.urls_unchanged} unchanged`];
  if (c.urls_failed) parts.push(`${c.urls_failed} failed`);
  if (c.urls_blocked) parts.push(`${c.urls_blocked} blocked`);
  return parts.join(" · ");
}

// "142 / 441 URLs" beats a bare "in progress…": a big client legitimately
// takes the better part of an hour, and only a moving number tells that
// apart from a capture that died.
function captureProgressText(c) {
  if (c.status === "queued") return c.queue_position ? `#${c.queue_position} in queue` : "queued…";
  if (!c.urls_total) return "starting…";
  return `${c.urls_captured} / ${c.urls_total} URLs`;
}

// Native `<progress>`, like every other live bar in the app: the page CSP is
// `default-src 'self'` with no `unsafe-inline`, so a width-driven `<span>`
// (inline style, or a JS-set `.style.width`) would simply not paint.
// No `value` attribute = the browser's own indeterminate animation, which is
// exactly the right thing before the URL count is known.
function captureProgressBarMarkup(c) {
  if (!c.urls_total) {
    return `<progress class="archive-capture-row__bar" aria-label="Capture starting"></progress>`;
  }
  return `<progress class="archive-capture-row__bar" max="${c.urls_total}" value="${c.urls_captured}"
    aria-label="${c.urls_captured} of ${c.urls_total} URLs captured"></progress>`;
}

function captureIcon(status) {
  return {
    ok: "circle-check", partial: "alert-triangle", failed: "circle-x", blocked: "shield-lock",
    running: "loader-2", queued: "clock", cancelled: "player-stop",
  }[status] || "circle";
}

function topChangingMarkup(urls) {
  if (urls.length === 0) return `<p class="archive-empty-note">Nothing has changed more than once yet.</p>`;
  return `<ul class="archive-top-changing-list">
    ${urls.map((u) => `
      <li>
        <a href="${buildArchiveHashForUrl(u)}">${escapeHtml(u.client_name)}: ${escapeHtml(u.url)}</a>
        <span class="tag-new">${u.snapshot_count} versions</span>
      </li>`).join("")}
  </ul>`;
}

function buildArchiveHashForUrl(urlDto) {
  const params = new URLSearchParams({ url: urlDto.url }).toString();
  return `#/archive?${params}`;
}

function bindOverviewActions(container) {
  container.querySelectorAll('[data-action="capture-now"]').forEach((button) => {
    button.addEventListener("click", () => triggerCapture(button.dataset.clientId));
  });
  container.querySelector("#archive-capture-all")?.addEventListener("click", triggerAllCaptures);
}

async function triggerCapture(clientId) {
  if (state.captureStarting) return;
  state.captureStarting = clientId;
  renderOverview();
  try {
    await startArchiveCapture(clientId);
    toast("Capture queued — the worker will pick it up within a minute.");
  } catch (error) {
    if (!isAbortError(error)) toast(`Could not start the capture: ${error.message}`);
  } finally {
    state.captureStarting = null;
    renderOverview();
  }
}

// task-62 §1.2/§0.6: safe precisely BECAUSE the queue is now real — this
// used to be either a crash-prone `archive-capture --all` run or a
// multi-hour foreground operation; now it's just N rows joining the tail.
async function triggerAllCaptures() {
  if (state.allCapturesStarting) return;
  state.allCapturesStarting = true;
  renderOverview();
  try {
    const result = await startAllArchiveCaptures();
    const skipped = result.skipped_client_ids.length;
    toast(
      `Queued ${result.capture_ids.length} client${result.capture_ids.length === 1 ? "" : "s"}`
      + (skipped ? ` — ${skipped} already active, left alone.` : "."),
    );
    await loadOverview();
  } catch (error) {
    if (!isAbortError(error)) toast(`Could not start captures: ${error.message}`);
  } finally {
    state.allCapturesStarting = false;
    renderOverview();
  }
}
