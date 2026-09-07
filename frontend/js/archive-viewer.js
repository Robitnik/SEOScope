// Web Archive replay viewer (task-59 §1.6/§4) — `#/archive/view?snapshot=<id>`.
//
// Everything the archived page itself renders happens INSIDE a sandboxed
// `<iframe>` with no `allow-same-origin` — `app/api/archive_replay.py`'s own
// CSP (no external origin listed at all) is the actual security/truthfulness
// boundary; this page is just the chrome around it: banner, Meta panel,
// prev/next, Compare, and the "disable page scripts" toggle.
//
// Full-bleed by design (fixed, own z-index — see archive.css): the app
// shell has no viewport-height chain to inherit, and an immersive replay
// reads better without the app's own nav competing for space, so this page
// hides it on entry (a full reload on any other route restores it).
import {
  getArchiveSnapshot, getArchiveSnapshotDiff, getArchiveUrlTimeline, isAbortError, lookupArchiveUrl,
  startArchiveAdhocUrlCapture,
} from "./api.js?v=20260821-spin3";
import {
  buildArchiveCaptureHash, buildArchiveHash, pushArchiveViewPath, replaceArchiveViewPath,
} from "./router.js?v=20260905-t63";
import { escapeHtml, formatTimestamp, icon } from "./formatters.js?v=20260821-spin3";
import { panelSkeleton } from "./skeleton.js?v=20260821-spin3";
import {
  calendarMarkup, bindCalendarActions, groupSnapshotsByDay, reasonLabel,
} from "./archive-calendar.js?v=20260907-t80";
import { notArchivedMarkup, bindNotArchivedActions } from "./archive-not-archived.js?v=20260906-t70";

const root = document.querySelector("#archive-view-page-root");

let state = null;

function toast(message) {
  const region = document.querySelector("#toast-region");
  const element = document.createElement("div");
  element.className = "toast-message";
  element.textContent = message;
  region.append(element);
  setTimeout(() => element.remove(), 4200);
}

export async function initArchiveViewPage(query) {
  const snapshotId = query.get("snapshot");
  state = {
    snapshotId,
    snapshot: null,
    loading: true,
    error: null,
    metaOpen: false,
    scriptsEnabled: true,
    diffOpen: false,
    diff: null,
    diffLoading: false,
    diffError: null,
    liveUrl: null, // last URL the replayed frame itself reported via postMessage
    jumping: false,
    // Version-picker calendar (task-63 §4) — lazy-loaded on first open,
    // cached by `archive_url_id` (NOT `snapshot_id`: every version of the
    // same page shares one timeline).
    calendarOpen: false,
    timeline: null,
    timelineForUrlId: null,
    timelineLoading: false,
    timelineError: null,
    calendarYear: null,
    calendarMonth: null,
    selectedDay: null,
    // task-70: set instead of `snapshot` when the address's OWN `url`
    // segment (present only on the new path form — the legacy hash form
    // never carries one) has no captured version at all. While this is
    // set, `render()` shows the shared "not in the archive" panel INSTEAD
    // of the iframe — never a different page silently standing in for the
    // one that was actually requested.
    notArchived: null, // { url, clientName, capturable } | null
    notArchivedStarting: false,
    notArchivedError: null,
  };
  root.innerHTML = "";
  document.querySelector(".masthead")?.setAttribute("hidden", "");
  if (!snapshotId) {
    root.innerHTML = missingIdMarkup();
    return;
  }
  document.addEventListener("keydown", onKeydown);
  window.addEventListener("message", onFrameMessage);
  // Back/Forward through versions (task-63 §1.3/§2.2) — `pushArchiveViewPath`
  // (Previous/Next, calendar pick, an in-replay link click) adds the history
  // entries this reacts to; a plain hash-only entry point never calls it, so
  // `hashchange` (wired globally in this module to a full reload) still
  // covers leaving THIS route for another one entirely.
  window.addEventListener("popstate", onPopState);
  await loadSnapshot(snapshotId, {
    historyMode: "replace", requestedUrl: query.get("url"), requestedTs: query.get("ts"),
  });
}

// Same shape `router.js`'s own `ARCHIVE_VIEW_PATH_RE` matches. Reads
// whichever address form is CURRENTLY live — the new path (which also
// carries its own `url`/`ts`) or the legacy hash (neither) — without
// touching `state`. Used both to react to Back/Forward and, on a
// legacy-hash entry, to canonicalize once data loads.
const VIEW_PATH_RE = /^\/archive\/view\/snapshot\/([^/]+)\/(\d{14})\/(.+)$/;

function currentRequestFromLocation() {
  const pathMatch = location.pathname.match(VIEW_PATH_RE);
  if (pathMatch) {
    const [, rawId, ts, rawUrl] = pathMatch;
    const decode = (value) => { try { return decodeURIComponent(value); } catch { return value; } };
    return { snapshotId: decode(rawId), ts, url: decode(rawUrl) };
  }
  const hash = location.hash || "";
  const query = new URLSearchParams(hash.split("?")[1] || "");
  return { snapshotId: query.get("snapshot"), ts: null, url: null };
}

function onPopState() {
  const { snapshotId, url, ts } = currentRequestFromLocation();
  if (!snapshotId) {
    // Back past the first version this session opened — the address bar
    // now points somewhere this module doesn't own (the hub, another
    // route entirely). A full reload matches this app's own convention
    // for cross-route navigation (see `hashchange` below).
    location.reload();
    return;
  }
  if (snapshotId === state.snapshotId && !state.notArchived) return;
  state.snapshotId = snapshotId;
  state.liveUrl = null;
  state.metaOpen = false;
  state.diffOpen = false;
  state.diff = null;
  state.calendarOpen = false;
  loadSnapshot(snapshotId, { historyMode: "none", requestedUrl: url, requestedTs: ts });
}

// Full-bleed loading state (task-63 follow-up: "зроби прелоадер гарний
// анімований") — this page IS the whole viewport while loading (`position:
// fixed; inset: 0`), so a generic gray skeleton row reads as broken rather
// than "opening". `transform`/`opacity` only (frontend/AGENTS.md §10);
// `prefers-reduced-motion` is handled globally (`app.css`'s
// `animation-duration: .01ms !important`), and the icon's own end-of-spin
// frame (`rotate(360deg)`, visually identical to the start) stays coherent
// when that snaps the animation to its last frame.
function loadingMarkup() {
  return `
    <div class="archive-loader" role="status" aria-live="polite">
      <span class="archive-loader__icon">${icon("history", "icon icon--spin")}</span>
      <p class="archive-loader__label">Opening the archived page…</p>
      <div class="bar archive-loader__bar is-indeterminate" data-p="0" role="progressbar"
        aria-valuemin="0" aria-valuemax="100" aria-label="Opening the archived page">
        <span class="bar__fill"></span>
      </div>
    </div>`;
}

function missingIdMarkup() {
  return `
    <div class="state-panel state-failed">
      ${icon("alert-triangle")}
      <p><strong>No snapshot specified.</strong><br>Look up a URL on the Web Archive hub to open a version.</p>
      <a class="button button--primary" href="${buildArchiveHash()}">${icon("arrow-left")}Back to Archive</a>
    </div>`;
}

function onKeydown(event) {
  if (!state?.snapshot) return;
  const iframe = root.querySelector(".archive-iframe");
  if (document.activeElement === iframe) return; // let the archived page have its own keys
  if (event.key === "Escape" && state.calendarOpen) {
    state.calendarOpen = false;
    renderChrome();
    root.querySelector('[data-action="toggle-calendar"]')?.focus();
    return;
  }
  if (event.key === "Escape" && state.metaOpen) {
    state.metaOpen = false;
    render();
    return;
  }
  if (event.key === "ArrowLeft" && state.snapshot.prev_snapshot_id) {
    navigateTo(state.snapshot.prev_snapshot_id);
  } else if (event.key === "ArrowRight" && state.snapshot.next_snapshot_id) {
    navigateTo(state.snapshot.next_snapshot_id);
  }
}

// The replayed frame has an opaque sandbox origin — this page cannot read
// `iframe.contentWindow.location` itself, so `shim.js` posts the real URL
// AND the snapshot id the server resolved for it (on load, and on every
// pushState/replaceState). That is the ONLY way this chrome learns that a
// click inside the archived page moved it to a different archived page.
// `app/api/archive_replay.py`'s page-shaped-miss response posts this from
// its OWN inline button (task-70) — a click on an in-page link that leads
// to an uncaptured page. Unlike `__archiveNav`, the sandbox never actually
// navigated anywhere (a page-shaped miss stays a 404 IN the iframe, never
// escaping the sandbox) — this just tells the chrome around it what to
// show instead. Resolved through `/lookup` here (not trusted from the
// frame) since the frame only knows the raw url/ts, not whether it's ours.
async function onArchiveMissMessage(url, ts) {
  let lookup;
  try {
    lookup = await lookupArchiveUrl(url);
  } catch (error) {
    if (isAbortError(error)) return;
    lookup = { found: false, client_id: null, client_name: null, capturable: false };
  }
  state.notArchived = { url, clientName: lookup.client_name, capturable: lookup.capturable };
  state.notArchivedStarting = false;
  state.notArchivedError = null;
  render();
}

function onFrameMessage(event) {
  const iframe = root.querySelector(".archive-iframe");
  if (!iframe || event.source !== iframe.contentWindow) return;
  const data = event.data;
  if (data && data.__archiveMiss) {
    onArchiveMissMessage(data.url, data.ts);
    return;
  }
  if (!data || !data.__archiveNav) return;
  const previousUrl = state.liveUrl;
  state.liveUrl = data.url;
  const input = root.querySelector("#archive-viewer-url-input");
  if (input && document.activeElement !== input) input.value = data.url;
  // Only follow the frame to a different snapshot when it also reports a
  // DIFFERENT url — i.e. a real in-page link click, not the frame's own
  // initial load message re-reporting the version the server resolved.
  // Without this guard, a `resolve_snapshot` off-by-one (task-63 §2.1) or
  // any future resolver mismatch drags the chrome (banner, date, prev/next)
  // BACKWARD to whatever the server actually served, silently undoing a
  // Previous/Next click the operator just made — live-confirmed 2026-09-05.
  if (data.snapshot && data.snapshot !== state.snapshotId && data.url !== previousUrl) {
    adoptFrameSnapshot(data.snapshot);
    return;
  }
  if (data.snapshot && data.snapshot !== state.snapshotId) {
    console.debug("archive-viewer: ignoring frame snapshot mismatch with unchanged url", {
      reported: data.snapshot, current: state.snapshotId, url: data.url,
    });
  }
  // Same snapshot, but the archived page's OWN JS moved its in-page address
  // (a client-side filter, a fragment) — keep the address bar truthful
  // without adding a history entry (this is not a version change).
  replaceArchiveViewPath(state.snapshotId, state.snapshot.ts, data.url);
}

// A link click inside the replay already navigated the frame to the right
// archived page — so re-point the chrome (address, date, prev/next, Meta,
// Compare) at that version WITHOUT re-rendering the iframe, which would
// otherwise reload the page the operator just landed on. This IS a real
// version change, so it gets its own history entry (Back returns to the
// page the operator clicked FROM).
async function adoptFrameSnapshot(snapshotId) {
  const previousId = state.snapshotId;
  state.snapshotId = snapshotId;
  try {
    state.snapshot = await getArchiveSnapshot(snapshotId);
  } catch (error) {
    if (!isAbortError(error)) state.snapshotId = previousId; // keep the chrome truthful
    return;
  }
  state.metaOpen = false;
  state.diffOpen = false;
  state.diff = null;
  state.calendarOpen = false;
  pushArchiveViewPath(snapshotId, state.snapshot.ts, state.liveUrl || state.snapshot.url);
  renderChrome();
}

// Previous/Next and the calendar's own version pick — a real navigation,
// re-rendering the iframe from scratch (unlike `adoptFrameSnapshot`, which
// reacts to a frame that already navigated itself).
// "Archive this page now" on the shared not-archived panel (task-70) —
// same endpoint/flow as the hub's own button; navigates to the live
// progress page rather than trying to stay on this now-defunct address.
async function triggerViewerAdhocCapture(url) {
  if (state.notArchivedStarting) return;
  state.notArchivedStarting = true;
  state.notArchivedError = null;
  render();
  try {
    const result = await startArchiveAdhocUrlCapture(url);
    location.hash = buildArchiveCaptureHash({ id: result.capture_id });
  } catch (error) {
    if (isAbortError(error)) return;
    state.notArchivedError =
      error.code === "client_not_found"
        ? "This domain isn't one of our clients — the archive only captures client sites."
        : error.code === "already_running"
          ? "A capture of this exact URL is already queued — try again in a moment."
          : `Could not start the capture: ${error.message}`;
    state.notArchivedStarting = false;
    render();
  }
}

async function navigateTo(snapshotId) {
  state.snapshotId = snapshotId;
  state.liveUrl = null;
  state.metaOpen = false;
  state.diffOpen = false;
  state.diff = null;
  state.calendarOpen = false;
  await loadSnapshot(snapshotId, { historyMode: "push" });
}

// Address-bar-style navigation: the operator types/pastes a URL and hits
// Enter. Resolves to whichever tracked version was live closest to the
// version currently open, same "nearest" convention the replay route
// itself uses server-side.
async function navigateToUrl(url) {
  if (!url || state.jumping) return;
  state.jumping = true;
  render();
  try {
    const result = await lookupArchiveUrl(url);
    if (!result.found) {
      toast(`Not archived: ${url}`);
      return;
    }
    const timeline = await getArchiveUrlTimeline(result.url.id);
    const target = pickNearestSnapshot(timeline.snapshots, state.snapshot?.ts);
    if (!target) {
      toast(`${result.url.url} is tracked, but has no captured version yet.`);
      return;
    }
    await navigateTo(target.id);
  } catch (error) {
    if (!isAbortError(error)) toast(`Could not open that URL: ${error.message}`);
  } finally {
    state.jumping = false;
    render();
  }
}

// `snapshots` is newest-first (API contract). Mirrors
// `WebArchiveRepository.resolve_snapshot`'s own "nearest" rule.
function pickNearestSnapshot(snapshots, targetTs) {
  if (!snapshots.length) return null;
  if (!targetTs) return snapshots[0];
  const atOrBefore = snapshots.find((s) => s.ts <= targetTs);
  return atOrBefore || snapshots[snapshots.length - 1];
}

// `historyMode`: "replace" (initial load — also canonicalizes a legacy
// hash-form entry to the new path once `ts` is known), "push" (a real
// navigation to a different version), or "none" (already reacting to a
// browser Back/Forward that moved the address itself — touching history
// again here would fight the browser's own stack).
// Loose enough for "is this the same page" (trailing slash only) —
// `find_url_any_client`'s own `normalize_url` is the authority on real
// identity; this is just a cheap check to skip a network round trip in
// the common case where the address's own `url` already matches.
function urlsLooselyMatch(a, b) {
  if (a === b) return true;
  if (!a || !b) return false;
  return a.replace(/\/$/, "") === b.replace(/\/$/, "");
}

// The requested `url` doesn't loosely match the snapshot fetched by id —
// resolve it independently via the SAME `/lookup` + timeline endpoints the
// hub itself uses (never trust `snapshotId` alone to be the right answer:
// task-63 §1.1's old rule was exactly that, and it let an unrelated page
// silently stand in for one that was never captured). Returns the resolved
// `ArchiveSnapshotDetailDTO`, or `null` after setting `state.notArchived`
// for the caller to render instead.
async function resolveRequestedUrl(url, ts) {
  const lookup = await lookupArchiveUrl(url);
  if (!lookup.found) {
    state.notArchived = { url, clientName: lookup.client_name, capturable: lookup.capturable };
    return null;
  }
  const timeline = await getArchiveUrlTimeline(lookup.url.id);
  const nearest = pickNearestSnapshot(timeline.snapshots, ts);
  if (nearest === null) {
    state.notArchived = { url: lookup.url.url, clientName: lookup.client_name, capturable: true };
    return null;
  }
  return await getArchiveSnapshot(nearest.id);
}

async function loadSnapshot(
  snapshotId, { historyMode = "replace", requestedUrl = null, requestedTs = null } = {}
) {
  state.loading = true;
  state.error = null;
  state.notArchived = null;
  render();
  try {
    let snapshot = await getArchiveSnapshot(snapshotId);
    let canonicalize = historyMode === "replace";
    if (requestedUrl && !urlsLooselyMatch(requestedUrl, snapshot.url)) {
      const resolved = await resolveRequestedUrl(requestedUrl, requestedTs);
      snapshot = resolved; // `null` if genuinely not archived — `state.notArchived` is set
      if (resolved) { snapshotId = resolved.id; canonicalize = true; }
    }
    state.snapshot = snapshot;
    state.snapshotId = snapshotId;
    if (snapshot) {
      if (historyMode === "push") pushArchiveViewPath(snapshotId, snapshot.ts, snapshot.url);
      else if (canonicalize) replaceArchiveViewPath(snapshotId, snapshot.ts, snapshot.url);
    }
  } catch (error) {
    if (isAbortError(error)) return;
    state.snapshot = null;
    state.error = error;
  }
  state.loading = false;
  render();
}

function render() {
  if (state.loading && !state.snapshot) {
    root.innerHTML = loadingMarkup();
    return;
  }
  if (state.error) {
    root.innerHTML = `
      <div class="state-panel state-failed">
        ${icon("wifi-off")}
        <p><strong>Could not open this snapshot.</strong><br>${escapeHtml(state.error.message || "")}</p>
        <button type="button" class="button button--primary" id="archive-view-retry">${icon("refresh")}Retry</button>
      </div>`;
    root.querySelector("#archive-view-retry").addEventListener("click", () => loadSnapshot(state.snapshotId));
    return;
  }
  if (state.notArchived) {
    root.innerHTML = `
      <div class="archive-viewer-not-archived">
        ${notArchivedMarkup({
          url: state.notArchived.url, clientName: state.notArchived.clientName,
          capturable: state.notArchived.capturable, starting: state.notArchivedStarting,
          error: state.notArchivedError, idPrefix: "archive-view-not-archived",
        })}
        <a class="button button--quiet" href="${buildArchiveHash()}">${icon("arrow-left")}Back to the archive hub</a>
      </div>`;
    bindNotArchivedActions(root, {
      idPrefix: "archive-view-not-archived", onCapture: () => triggerViewerAdhocCapture(state.notArchived.url),
    });
    return;
  }
  if (!state.snapshot) { root.innerHTML = ""; return; }

  const s = state.snapshot;
  root.innerHTML = `
    <div class="archive-viewer">
      ${chromeMarkup(s)}
      <div class="archive-iframe-wrap">
        ${iframeLoadingMarkup()}
        <iframe class="archive-iframe" src="${escapeHtml(s.view_url)}"
          sandbox="allow-forms allow-popups${state.scriptsEnabled ? " allow-scripts" : ""}"
          title="Archived copy of ${escapeHtml(s.url)} (${escapeHtml(s.ts)})"></iframe>
      </div>
    </div>`;
  bindActions();
  bindIframeLoading();
}

// The metadata call (`getArchiveSnapshot`) that `loadingMarkup()` covers is
// small and fast — the REAL wait an operator sees is the iframe itself
// fetching the archived page's full HTML/CSS/JS/images from `/archive/r/
// ...` afterward, which for a heavy page can run into several seconds with
// nothing on screen to say so (live-reported 2026-09-05: "15 сек я цього не
// розумію"). An `<iframe>` fires a normal `load` event on navigation
// completion regardless of sandboxing/cross-origin content, so this can be
// tracked without reading anything inside the frame.
function iframeLoadingMarkup() {
  return `
    <div class="archive-iframe-loading" aria-hidden="true">
      <span class="archive-iframe-loading__icon">${icon("history", "icon icon--spin")}</span>
      <p class="archive-iframe-loading__label">Loading the archived page…</p>
      <div class="bar archive-iframe-loading__bar is-indeterminate" data-p="0" role="progressbar"
        aria-valuemin="0" aria-valuemax="100" aria-label="Loading the archived page">
        <span class="bar__fill"></span>
      </div>
    </div>`;
}

// A hard cap, not just a `load`-event listener (live-reported 2026-09-05:
// "вічне завантаження" — the overlay never went away). Root cause: a real
// captured page's own LIVE embed (task-60 §2 — a map/video iframe
// deliberately left pointing at its real, external host instead of being
// archived) can keep the outer iframe's native `load` event from ever
// firing at all, since `load` waits on every subresource including nested
// frames. This was directly observed during this task's own e2e work: even
// a plain `page.goto(..., { waitUntil: "load" })` hung indefinitely on a
// real page for the exact same reason. The snapshot's existence is already
// confirmed by the time this runs (this whole branch only renders once
// `state.snapshot` is set from a successful API response), so a fixed
// ceiling is the honest choice — whichever signal (the real `load` event,
// or the timeout) comes first hides the overlay; nothing here can leave it
// stuck forever.
const IFRAME_LOADING_MAX_MS = 3000;

function bindIframeLoading() {
  const iframe = root.querySelector(".archive-iframe");
  const overlay = root.querySelector(".archive-iframe-loading");
  if (!iframe || !overlay) return;
  let hidden = false;
  const hide = () => {
    if (hidden) return;
    hidden = true;
    overlay.classList.add("archive-iframe-loading--done");
  };
  iframe.addEventListener("load", hide, { once: true });
  setTimeout(hide, IFRAME_LOADING_MAX_MS);
}

// Banner + panels only — everything ABOVE the iframe. Rendered separately
// so `adoptFrameSnapshot` can refresh it after an in-frame link click
// without touching (and thereby reloading) the frame itself.
function chromeMarkup(s) {
  const addressValue = state.liveUrl || s.url;
  return `
      <div class="archive-banner">
        <a class="back-link archive-banner__back" href="${buildArchiveHash()}">${icon("arrow-left")}<span class="visually-hidden">Back to Archive</span></a>
        <div class="archive-banner__identity">
          <p class="eyebrow">${escapeHtml(s.client_name)}</p>
          <form id="archive-viewer-url-form" role="search">
            <label class="visually-hidden" for="archive-viewer-url-input">Archived URL</label>
            <input type="text" id="archive-viewer-url-input" class="archive-banner__url" inputmode="url"
              autocomplete="off" spellcheck="false" value="${escapeHtml(addressValue)}"
              title="Current archived URL — edit and press Enter to jump to another captured page">
          </form>
        </div>
        <div class="archive-banner__time">
          <span>${formatTimestamp(s.observed_at)} UTC</span>
          ${s.confirmed_count > 1 ? `<span class="archive-banner__confirmed">live through ${formatTimestamp(s.last_confirmed_at)}</span>` : ""}
        </div>
        <div class="archive-banner__calendar">
          <button type="button" class="button button--quiet button--small" data-action="toggle-calendar"
            aria-expanded="${String(state.calendarOpen)}" aria-haspopup="dialog" aria-controls="archive-calendar-popover">
            ${icon("calendar")}${formatTimestamp(s.observed_at)}
          </button>
          ${calendarPopoverMarkup()}
        </div>
        <nav class="archive-banner__nav" aria-label="Adjacent versions">
          <button type="button" class="button button--quiet button--small" data-action="prev" ${s.prev_snapshot_id ? "" : "disabled"}>${icon("chevron-left")}Previous</button>
          <button type="button" class="button button--quiet button--small" data-action="next" ${s.next_snapshot_id ? "" : "disabled"}>Next${icon("chevron-right")}</button>
        </nav>
        <div class="archive-banner__actions">
          <button type="button" class="button button--quiet button--small" data-action="toggle-meta" aria-expanded="${String(state.metaOpen)}">${icon("info-circle")}Meta</button>
          <a class="button button--quiet button--small" href="/api/v1/archive/snapshots/${encodeURIComponent(s.id)}/source" target="_blank" rel="noreferrer">${icon("code")}View source</a>
          <button type="button" class="button button--quiet button--small" data-action="toggle-diff" aria-expanded="${String(state.diffOpen)}" ${s.prev_snapshot_id ? "" : "disabled"}>${icon("git-compare")}Compare with previous</button>
          <label class="archive-scripts-toggle">
            <input type="checkbox" data-action="toggle-scripts" ${state.scriptsEnabled ? "" : "checked"}>
            <span>Disable page scripts</span>
          </label>
        </div>
      </div>
      ${state.metaOpen ? metaPanelMarkup(s) : ""}
      ${state.diffOpen ? diffPanelMarkup() : ""}`;
}

function renderChrome() {
  const viewer = root.querySelector(".archive-viewer");
  const frameWrap = root.querySelector(".archive-iframe-wrap");
  if (!viewer || !frameWrap || !state.snapshot) {
    render();
    return;
  }
  while (viewer.firstChild !== frameWrap) viewer.removeChild(viewer.firstChild);
  frameWrap.insertAdjacentHTML("beforebegin", chromeMarkup(state.snapshot));
  bindActions();
}

// Version-picker popover (task-63 §4) — same calendar the Web Archive hub
// shows, reused verbatim from `archive-calendar.js`. Lazy: nothing is
// fetched until the operator actually opens it.
function calendarPopoverMarkup() {
  if (!state.calendarOpen) return "";
  if (state.timelineLoading) {
    return `<div class="archive-calendar-popover" id="archive-calendar-popover" role="dialog" aria-label="Pick a version">${panelSkeleton({ label: "versions", rows: 3, columns: 1 })}</div>`;
  }
  if (state.timelineError) {
    return `
      <div class="archive-calendar-popover" id="archive-calendar-popover" role="dialog" aria-label="Pick a version">
        <div class="state-panel state-failed">${icon("wifi-off")}<p>${escapeHtml(state.timelineError.message || "Could not load versions.")}</p></div>
      </div>`;
  }
  if (!state.timeline) return `<div class="archive-calendar-popover" id="archive-calendar-popover" role="dialog" aria-label="Pick a version"></div>`;
  const groups = groupSnapshotsByDay(state.timeline.snapshots);
  return `
    <div class="archive-calendar-popover" id="archive-calendar-popover" role="dialog" aria-label="Pick a version">
      ${calendarMarkup({ year: state.calendarYear, month: state.calendarMonth, selectedDay: state.selectedDay, groups })}
      ${selectedDayVersionsMarkup(groups)}
    </div>`;
}

function selectedDayVersionsMarkup(groups) {
  const points = (state.selectedDay && groups.get(state.selectedDay)) || [];
  if (points.length === 0) {
    return `<p class="archive-empty-note">Pick a highlighted day to see its captured versions.</p>`;
  }
  return `
    <ul class="archive-timeline-points">
      ${points.map((point) => `
        <li>
          <button type="button" class="archive-timeline-point" data-goto-snapshot="${escapeHtml(point.id)}"
            ${point.id === state.snapshotId ? 'aria-current="true"' : ""}>
            ${icon(point.has_change ? "point-filled" : "point", `icon archive-timeline-point__dot${point.has_change ? " archive-timeline-point__dot--changed" : ""}`)}
            <span class="archive-timeline-point__time">${escapeHtml(point.observed_at.slice(11, 16))} UTC</span>
            <span class="archive-timeline-point__reason">${reasonLabel(point.store_reason)}</span>
            ${point.http_status && point.http_status >= 400 ? `<span class="tag-new" data-tone="danger">${point.http_status}</span>` : ""}
          </button>
        </li>`).join("")}
    </ul>`;
}

// Fetched once per `archive_url_id` (every version of the same page shares
// one timeline), not once per `snapshot_id` — switching to a version of the
// SAME page never re-fetches; switching to a DIFFERENT page (an in-replay
// link click) does.
async function ensureTimelineLoaded() {
  if (state.timeline && state.timelineForUrlId === state.snapshot.archive_url_id) return;
  state.timelineLoading = true;
  state.timelineError = null;
  state.timeline = null;
  renderChrome();
  try {
    state.timeline = await getArchiveUrlTimeline(state.snapshot.archive_url_id);
    state.timelineForUrlId = state.snapshot.archive_url_id;
    const day = state.snapshot.observed_at.slice(0, 10);
    const [year, month] = day.split("-").map(Number);
    state.calendarYear = year;
    state.calendarMonth = month - 1;
    state.selectedDay = day;
  } catch (error) {
    if (!isAbortError(error)) state.timelineError = error;
  }
  state.timelineLoading = false;
  renderChrome();
}

function shiftViewerCalendarMonth(delta) {
  let month = state.calendarMonth + delta;
  let year = state.calendarYear;
  if (month < 0) { month = 11; year -= 1; }
  if (month > 11) { month = 0; year += 1; }
  state.calendarMonth = month;
  state.calendarYear = year;
  renderChrome();
}

function metaPanelMarkup(s) {
  const headerRows = Object.entries(s.headers || {})
    .map(([key, value]) => `<tr><th>${escapeHtml(key)}</th><td>${escapeHtml(value)}</td></tr>`)
    .join("");
  return `
    <div class="archive-meta-panel" role="region" aria-label="Snapshot metadata">
      <div class="archive-meta-grid">
        <div><p class="archive-meta-label">HTTP status</p><p>${s.http_status ?? "—"}</p></div>
        <div><p class="archive-meta-label">Final URL</p><p class="archive-meta-value">${s.final_url ? escapeHtml(s.final_url) : "—"}</p></div>
        <div><p class="archive-meta-label">Title</p><p class="archive-meta-value">${s.title ? escapeHtml(s.title) : "—"}</p></div>
        <div><p class="archive-meta-label">Description</p><p class="archive-meta-value">${s.description ? escapeHtml(s.description) : "—"}</p></div>
        <div><p class="archive-meta-label">Canonical</p><p class="archive-meta-value">${s.canonical ? escapeHtml(s.canonical) : "—"}</p></div>
        <div><p class="archive-meta-label">Meta robots</p><p class="archive-meta-value">${s.meta_robots ? escapeHtml(s.meta_robots) : "—"}</p></div>
        <div><p class="archive-meta-label">Word count</p><p>${s.word_count ?? "—"}</p></div>
        <div>
          <p class="archive-meta-label">Assets</p>
          <p>${s.asset_count} captured
            ${s.assets_missing ? `<span class="tag-new" data-tone="warning">${s.assets_missing} missing</span>` : ""}
            ${s.assets_truncated ? `<span class="tag-new" data-tone="warning">truncated</span>` : ""}
          </p>
        </div>
        <div><p class="archive-meta-label">Reason stored</p><p>${escapeHtml(s.store_reason)}</p></div>
        <div>
          <p class="archive-meta-label">Replay misses (this host)</p>
          <p>${s.replay_misses_for_host}${s.replay_misses_for_host ? ` <span class="tag-new" data-tone="warning">not archived</span>` : ""}</p>
        </div>
        ${s.live_iframe_hosts && s.live_iframe_hosts.length ? `
        <div>
          <p class="archive-meta-label">Live embeds</p>
          <p>${s.live_iframe_hosts.length} (${s.live_iframe_hosts.map(escapeHtml).join(", ")})</p>
          <p class="archive-meta-note">Embedded third-party frames are loaded live, not archived.</p>
        </div>` : ""}
      </div>
      <details class="archive-meta-headers">
        <summary>Response headers</summary>
        <table class="analytics-table"><tbody>${headerRows || "<tr><td>No headers recorded.</td></tr>"}</tbody></table>
      </details>
    </div>`;
}

function diffPanelMarkup() {
  if (state.diffLoading) return `<div class="archive-diff-panel">${panelSkeleton({ label: "the diff", rows: 4, columns: 1 })}</div>`;
  if (state.diffError) {
    return `<div class="archive-diff-panel"><div class="state-panel state-failed">${icon("wifi-off")}<p>${escapeHtml(state.diffError.message || "Could not load the diff.")}</p></div></div>`;
  }
  if (!state.diff) return "";
  const lines = state.diff.diff_lines;
  if (lines.length === 0) {
    return `<div class="archive-diff-panel"><p class="archive-empty-note">No textual differences from the previous version.</p></div>`;
  }
  return `
    <div class="archive-diff-panel">
      <pre class="archive-diff">${lines.map(diffLineMarkup).join("\n")}</pre>
    </div>`;
}

function diffLineMarkup(line) {
  const cls = line.startsWith("+") ? "archive-diff-line--add" : line.startsWith("-") ? "archive-diff-line--del" : "";
  return `<span class="${cls}">${escapeHtml(line)}</span>`;
}

function bindActions() {
  root.querySelector('[data-action="prev"]')?.addEventListener("click", () => navigateTo(state.snapshot.prev_snapshot_id));
  root.querySelector('[data-action="next"]')?.addEventListener("click", () => navigateTo(state.snapshot.next_snapshot_id));
  root.querySelector('[data-action="toggle-meta"]')?.addEventListener("click", () => {
    state.metaOpen = !state.metaOpen;
    render();
  });
  root.querySelector('[data-action="toggle-scripts"]')?.addEventListener("change", (event) => {
    state.scriptsEnabled = !event.target.checked;
    render();
  });
  root.querySelector('[data-action="toggle-diff"]')?.addEventListener("click", async () => {
    state.diffOpen = !state.diffOpen;
    if (state.diffOpen && !state.diff) {
      state.diffLoading = true;
      state.diffError = null;
      render();
      try {
        state.diff = await getArchiveSnapshotDiff(state.snapshot.id);
      } catch (error) {
        if (!isAbortError(error)) state.diffError = error;
      }
      state.diffLoading = false;
    }
    render();
  });
  root.querySelector("#archive-viewer-url-form")?.addEventListener("submit", (event) => {
    event.preventDefault();
    navigateToUrl(root.querySelector("#archive-viewer-url-input").value.trim());
  });
  root.querySelector('[data-action="toggle-calendar"]')?.addEventListener("click", () => {
    state.calendarOpen = !state.calendarOpen;
    if (state.calendarOpen) ensureTimelineLoaded();
    renderChrome();
  });
  const popover = root.querySelector(".archive-calendar-popover");
  if (popover) {
    bindCalendarActions(popover, {
      onPrevMonth: () => shiftViewerCalendarMonth(-1),
      onNextMonth: () => shiftViewerCalendarMonth(1),
      onSelectDay: (day) => {
        state.selectedDay = day;
        renderChrome();
      },
    });
    popover.querySelectorAll("[data-goto-snapshot]").forEach((button) => {
      button.addEventListener("click", () => {
        const targetId = button.dataset.gotoSnapshot;
        state.calendarOpen = false;
        if (targetId !== state.snapshotId) navigateTo(targetId);
        else renderChrome();
      });
    });
  }
}
