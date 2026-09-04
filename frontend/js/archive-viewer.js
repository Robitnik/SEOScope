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
} from "./api.js?v=20260821-spin3";
import { buildArchiveHash, replaceArchiveViewHash } from "./router.js?v=20260821-spin3";
import { escapeHtml, formatTimestamp, icon } from "./formatters.js?v=20260821-spin3";
import { panelSkeleton } from "./skeleton.js?v=20260821-spin3";

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
  };
  root.innerHTML = "";
  document.querySelector(".masthead")?.setAttribute("hidden", "");
  if (!snapshotId) {
    root.innerHTML = missingIdMarkup();
    return;
  }
  document.addEventListener("keydown", onKeydown);
  window.addEventListener("message", onFrameMessage);
  await loadSnapshot(snapshotId);
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
// it is currently showing (on load, and on every pushState/replaceState).
// That is the ONLY way the address input can track in-page navigation.
function onFrameMessage(event) {
  const iframe = root.querySelector(".archive-iframe");
  if (!iframe || event.source !== iframe.contentWindow) return;
  const data = event.data;
  if (!data || !data.__archiveNav) return;
  state.liveUrl = data.url;
  const input = root.querySelector("#archive-viewer-url-input");
  if (input && document.activeElement !== input) input.value = data.url;
}

async function navigateTo(snapshotId) {
  replaceArchiveViewHash({ snapshot: snapshotId, url: state.snapshot?.url || "" });
  state.snapshotId = snapshotId;
  state.liveUrl = null;
  state.metaOpen = false;
  state.diffOpen = false;
  state.diff = null;
  await loadSnapshot(snapshotId);
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

async function loadSnapshot(snapshotId) {
  state.loading = true;
  state.error = null;
  render();
  try {
    state.snapshot = await getArchiveSnapshot(snapshotId);
    replaceArchiveViewHash({ snapshot: snapshotId, url: state.snapshot.url });
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
    root.innerHTML = panelSkeleton({ label: "the archived page", rows: 2, columns: 1 });
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
  if (!state.snapshot) { root.innerHTML = ""; return; }

  const s = state.snapshot;
  const addressValue = state.liveUrl || s.url;
  root.innerHTML = `
    <div class="archive-viewer">
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
      ${state.diffOpen ? diffPanelMarkup() : ""}
      <div class="archive-iframe-wrap">
        <iframe class="archive-iframe" src="${escapeHtml(s.view_url)}"
          sandbox="allow-forms allow-popups${state.scriptsEnabled ? " allow-scripts" : ""}"
          title="Archived copy of ${escapeHtml(s.url)} (${escapeHtml(s.ts)})"></iframe>
      </div>
    </div>`;
  bindActions();
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
        <div><p class="archive-meta-label">Assets</p><p>${s.asset_count} captured${s.assets_missing ? `, ${s.assets_missing} missing` : ""}${s.assets_truncated ? " (truncated)" : ""}</p></div>
        <div><p class="archive-meta-label">Reason stored</p><p>${escapeHtml(s.store_reason)}</p></div>
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
}
