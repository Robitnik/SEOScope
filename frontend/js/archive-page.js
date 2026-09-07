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
  startAllArchiveCaptures, startArchiveCapture, startForcedArchiveCapture, startArchiveUrlCapture,
  startArchiveAdhocUrlCapture, isAbortError,
} from "./api.js?v=20260907-t80";
import {
  buildArchiveCaptureHash, buildArchiveViewHash, buildArchiveChangesHash,
} from "./router.js?v=20260905-t63";
import {
  escapeHtml, formatFileSize, formatTimestamp, icon, statusIcon,
} from "./formatters.js?v=20260821-spin3";
import { sectionSkeleton, panelSkeleton } from "./skeleton.js?v=20260821-spin3";
import {
  calendarMarkup, bindCalendarActions, groupSnapshotsByDay, reasonLabel,
} from "./archive-calendar.js?v=20260907-t80";
import { notArchivedMarkup, bindNotArchivedActions } from "./archive-not-archived.js?v=20260906-t70";

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
    lookupCapturable: false, // ArchiveLookupDTO.capturable — host resolves to one of OUR clients
    lookupClientName: null,
    overview: null,
    overviewLoading: true,
    overviewError: null,
    queue: null, // ArchiveQueueDTO — running/queued/recent (task-62 §1.4)
    captureStarting: null, // client_id currently being triggered
    // task-80 — separate from `captureStarting`: sharing one flag would
    // disable BOTH buttons of every OTHER row the moment either fires on
    // this one.
    recaptureStarting: null,
    urlCaptureStarting: false, // "Refresh now" on the currently looked-up URL
    allCapturesStarting: false,
    // Shared by BOTH the hero "Archive now" button and the not-archived
    // panel's own button (task-70) — they act on the SAME `state.urlInput`,
    // so one in-flight request is the correct model, not two independent
    // ones that could race each other.
    adhocCaptureStarting: false,
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
        <button type="button" id="archive-force-capture-btn" class="button button--quiet">${icon("camera")}Archive now</button>
        <p class="archive-lookup-form__error" id="archive-force-capture-error" hidden></p>
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
  root.querySelector("#archive-force-capture-btn").addEventListener("click", () => {
    const value = root.querySelector("#archive-url-input").value.trim();
    triggerAdhocCapture(value);
  });
}

// Reflects `adhocCaptureStarting`/`adhocCaptureError` on the hero's OWN
// button — kept separate from `renderLookup()` (which only touches
// `#archive-lookup-result`) since this button exists whether or not a
// lookup has ever run: pasting a brand-new URL and hitting "Archive now"
// straight away, with no "Look up" in between, is the whole point.
function renderHeroForceButton() {
  const button = root.querySelector("#archive-force-capture-btn");
  const errorEl = root.querySelector("#archive-force-capture-error");
  if (!button || !errorEl) return;
  button.disabled = state.adhocCaptureStarting;
  button.innerHTML = `${icon(state.adhocCaptureStarting ? "loader-2" : "camera", state.adhocCaptureStarting ? "icon icon--spin" : "icon")}${state.adhocCaptureStarting ? "Starting…" : "Archive now"}`;
  if (state.adhocCaptureError) {
    errorEl.textContent = state.adhocCaptureError;
    errorEl.hidden = false;
  } else {
    errorEl.hidden = true;
  }
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
  state.lookupCapturable = false;
  state.lookupClientName = null;
  renderLookup();
  try {
    const result = await lookupArchiveUrl(url);
    state.lookupCapturable = result.capturable;
    state.lookupClientName = result.client_name;
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
    // task-70: `capturable`/`clientName` are known BEFORE the click now
    // (`ArchiveLookupDTO`), not only from a 404 after the fact — a
    // non-client domain gets a flat explanation instead of a button that
    // would just fail. Shared markup with the viewer's own "not in the
    // archive" panel (`archive-not-archived.js`) — same wording either way.
    container.innerHTML = notArchivedMarkup({
      url: state.urlInput, clientName: state.lookupClientName, capturable: state.lookupCapturable,
      starting: state.adhocCaptureStarting, error: state.adhocCaptureError,
    });
    bindNotArchivedActions(container, { onCapture: () => triggerAdhocCapture(state.urlInput) });
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
    container.querySelector("#archive-refresh-url").addEventListener("click", () => triggerUrlCapture(url.id, { redirectToProgress: true }));
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
// `redirectToProgress`: true for a URL with NOTHING captured yet (the
// operator has an empty panel to stare at either way, so watching it
// happen live is strictly better — task-63 follow-up, matching
// `triggerAdhocCapture` below); false (default) for "Refresh now" on a URL
// that already has real content on screen, where staying put and toasting
// is the less disruptive choice.
async function triggerUrlCapture(urlId, { redirectToProgress = false } = {}) {
  if (state.urlCaptureStarting) return;
  state.urlCaptureStarting = true;
  renderLookup();
  try {
    const result = await startArchiveUrlCapture(urlId);
    if (redirectToProgress) {
      location.hash = buildArchiveCaptureHash({ id: result.capture_id });
      return;
    }
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
function renderAdhocCaptureUi() {
  renderLookup();
  renderHeroForceButton();
}

// Called from BOTH the not-archived panel's own button and the hero's
// "Archive now" (task-70) — one shared path, one set of messages, so the
// two never drift into saying different things for the identical error.
async function triggerAdhocCapture(url) {
  if (state.adhocCaptureStarting) return;
  if (!isLikelyHttpUrl(url)) {
    state.adhocCaptureError = "Enter a full http(s) URL to archive.";
    renderAdhocCaptureUi();
    return;
  }
  state.adhocCaptureStarting = true;
  state.adhocCaptureError = null;
  renderAdhocCaptureUi();
  try {
    const result = await startArchiveAdhocUrlCapture(url);
    location.hash = buildArchiveCaptureHash({ id: result.capture_id });
  } catch (error) {
    if (isAbortError(error)) return;
    state.adhocCaptureError =
      error.code === "client_not_found"
        ? "This domain isn't one of our clients — the archive only captures client sites."
        : error.code === "already_running"
          ? "A capture of this exact URL is already queued — try again in a moment."
          : `Could not start the capture: ${error.message}`;
    state.adhocCaptureStarting = false;
    renderAdhocCaptureUi();
  }
}

function isLikelyHttpUrl(value) {
  if (!value) return false;
  try {
    return /^https?:$/.test(new URL(value).protocol);
  } catch {
    return false;
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
              <td class="archive-clients-table__actions">
                <button type="button" class="button button--quiet button--small" data-action="capture-now" data-client-id="${escapeHtml(client.client_id)}" ${state.captureStarting === client.client_id ? "disabled" : ""}>
                  ${icon(state.captureStarting === client.client_id ? "loader-2" : "camera", state.captureStarting === client.client_id ? "icon icon--spin" : "icon")}Capture now
                </button>
                <button type="button" class="button button--quiet button--small" data-action="recapture-now" data-client-id="${escapeHtml(client.client_id)}" data-client-name="${escapeHtml(client.client_name)}" data-urls-tracked="${client.urls_tracked}" ${state.recaptureStarting === client.client_id ? "disabled" : ""}>
                  ${icon(state.recaptureStarting === client.client_id ? "loader-2" : "camera-plus", state.recaptureStarting === client.client_id ? "icon icon--spin" : "icon")}Recapture
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
          ${c.force ? `<span class="tag-new">forced</span>` : ""}
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
  container.querySelectorAll('[data-action="recapture-now"]').forEach((button) => {
    button.addEventListener("click", () => triggerRecapture(button.dataset));
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

// task-80 — "Recapture": same queue, same worker, but every URL gets a new
// snapshot even when byte-for-byte unchanged. Confirmed up front — this is
// a full-portfolio-of-one-client obhid (up to a few thousand URLs, PLAN
// §9.1), not a quick refresh — and left in place on this same page
// (same as "Capture now") rather than redirected to the progress page: the
// queue panel below already updates live and links into it on its own.
async function triggerRecapture({ clientId, clientName, urlsTracked }) {
  if (state.recaptureStarting) return;
  const count = Number(urlsTracked) || 0;
  const confirmed = window.confirm(
    `Recapture the whole site for ${clientName}? This stores a new version of `
    + `all ${count.toLocaleString("en-US")} page${count === 1 ? "" : "s"}, even the unchanged ones. `
    + `It can take a while.`,
  );
  if (!confirmed) return;
  state.recaptureStarting = clientId;
  renderOverview();
  try {
    await startForcedArchiveCapture(clientId);
    toast("Recapture queued — the worker will pick it up within a minute.");
  } catch (error) {
    if (!isAbortError(error)) toast(`Could not start the recapture: ${error.message}`);
  } finally {
    state.recaptureStarting = null;
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
