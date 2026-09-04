// Web Archive hub (task-59) — top-level page, no client id, same shape as
// trends-page.js/monitor-page.js: `root.innerHTML = shell()` on every
// `initArchivePage` call, state kept in a module-level object.
//
// The operator's job here: paste a URL, land on its timeline of captured
// versions. Below the input — portfolio coverage, storage, recent capture
// health, and the pages that change most often — so the hub also answers
// "is the archive actually working" without opening a client.
import {
  getArchiveOverview, getArchiveUrlTimeline, lookupArchiveUrl, startArchiveCapture, isAbortError,
} from "./api.js?v=20260821-spin3";
import { buildArchiveViewHash, buildArchiveChangesHash } from "./router.js?v=20260821-spin3";
import { escapeHtml, formatFileSize, formatTimestamp, icon } from "./formatters.js?v=20260821-spin3";
import { sectionSkeleton, panelSkeleton } from "./skeleton.js?v=20260821-spin3";

const root = document.querySelector("#archive-page-root");

let state = null;

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
    captureStarting: null, // client_id currently being triggered
  };
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

const WEEKDAY_LABELS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MONTH_LABELS = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December",
];

// One cell per day of `year`/`month` (0-indexed), padded with `null`s so the
// grid always starts on the correct weekday column — same shape the real
// Wayback Machine calendar uses.
function buildCalendarCells(year, month, groupsByDay) {
  const firstWeekday = new Date(Date.UTC(year, month, 1)).getUTCDay();
  const daysInMonth = new Date(Date.UTC(year, month + 1, 0)).getUTCDate();
  const cells = new Array(firstWeekday).fill(null);
  for (let day = 1; day <= daysInMonth; day += 1) {
    const iso = `${year}-${String(month + 1).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
    cells.push({ iso, day, points: groupsByDay.get(iso) || [] });
  }
  return cells;
}

function groupSnapshotsByDay(snapshots) {
  const groups = new Map();
  for (const snapshot of snapshots) {
    const day = snapshot.observed_at.slice(0, 10);
    if (!groups.has(day)) groups.set(day, []);
    groups.get(day).push(snapshot);
  }
  return groups;
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
      <div class="state-panel state-not_configured">
        ${icon("history-off")}
        <p><strong>Not archived (yet).</strong><br>
        This URL hasn't been captured. If its client has the archive enabled, it will be picked up
        on the next discovery pass — or ask an operator to enable it for that client.</p>
      </div>`;
    return;
  }

  const { url, snapshots } = state.timeline;
  if (snapshots.length === 0) {
    container.innerHTML = `
      <div class="state-panel state-not_configured">
        ${icon("history-off")}
        <p><strong>${escapeHtml(url.client_name)}</strong> is tracked, but no version has been captured yet.</p>
      </div>`;
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
      </div>
      ${calendarMarkup(groups)}
      ${selectedDayMarkup(groups)}
    </div>`;
  bindCalendarActions(groups);
}

function calendarMarkup(groups) {
  const cells = buildCalendarCells(state.calendarYear, state.calendarMonth, groups);
  return `
    <div class="archive-calendar">
      <div class="archive-calendar__header">
        <button type="button" class="icon-button" data-action="cal-prev" aria-label="Previous month">${icon("chevron-left")}</button>
        <p class="archive-calendar__month">${MONTH_LABELS[state.calendarMonth]} ${state.calendarYear}</p>
        <button type="button" class="icon-button" data-action="cal-next" aria-label="Next month">${icon("chevron-right")}</button>
      </div>
      <div class="archive-calendar__weekdays">
        ${WEEKDAY_LABELS.map((label) => `<span>${label}</span>`).join("")}
      </div>
      <div class="archive-calendar__grid">
        ${cells.map((cell) => {
          if (!cell) return `<span class="archive-calendar__day archive-calendar__day--empty"></span>`;
          const hasCapture = cell.points.length > 0;
          const changed = cell.points.some((p) => p.has_change);
          const isSelected = cell.iso === state.selectedDay;
          return `
            <button type="button" class="archive-calendar__day${hasCapture ? " archive-calendar__day--has-capture" : ""}${isSelected ? " archive-calendar__day--selected" : ""}${changed ? " archive-calendar__day--changed" : ""}"
              data-day="${cell.iso}" ${hasCapture ? "" : "disabled"}
              aria-current="${isSelected ? "date" : "false"}" aria-label="${cell.iso}${hasCapture ? `, ${cell.points.length} version${cell.points.length === 1 ? "" : "s"}` : ", no capture"}">
              ${cell.day}
            </button>`;
        }).join("")}
      </div>
    </div>`;
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

function bindCalendarActions(groups) {
  const container = root.querySelector("#archive-lookup-result");
  container.querySelector('[data-action="cal-prev"]')?.addEventListener("click", () => shiftCalendarMonth(-1));
  container.querySelector('[data-action="cal-next"]')?.addEventListener("click", () => shiftCalendarMonth(1));
  container.querySelectorAll(".archive-calendar__day[data-day]").forEach((button) => {
    button.addEventListener("click", () => {
      state.selectedDay = button.dataset.day;
      renderLookup();
    });
  });
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

function reasonLabel(reason) {
  return { first: "First capture", changed: "Changed", weekly_refresh: "Confirmed unchanged", status_changed: "Status changed" }[reason] || reason;
}

// --- overview --------------------------------------------------------------

async function loadOverview() {
  state.overviewLoading = true;
  state.overviewError = null;
  renderOverview();
  try {
    state.overview = await getArchiveOverview();
  } catch (error) {
    if (isAbortError(error)) return;
    state.overview = null;
    state.overviewError = error;
  }
  state.overviewLoading = false;
  renderOverview();
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
      <div class="archive-stat"><p class="archive-stat__value">${formatFileSize(o.bytes_local)}</p><p class="archive-stat__label">Stored locally</p></div>
      <div class="archive-stat"><p class="archive-stat__value">${o.cloud_enabled ? formatFileSize(o.bytes_uploaded) : "—"}</p><p class="archive-stat__label">${o.cloud_enabled ? "In cloud storage" : "Cloud storage off"}</p></div>
      <div class="archive-stat"><p class="archive-stat__value">${escapeHtml(o.capture_times.join(" · "))}</p><p class="archive-stat__label">Daily captures (${escapeHtml(o.timezone)})</p></div>
    </div>
    <div class="section-heading"><div><p class="eyebrow">Coverage</p><h2>Clients</h2></div></div>
    ${clientsTableMarkup(o.clients)}
    <div class="archive-columns">
      <div>
        <div class="section-heading"><div><p class="eyebrow">Health</p><h2>Recent captures</h2></div></div>
        ${capturesListMarkup(o.recent_captures)}
      </div>
      <div>
        <div class="section-heading"><div><p class="eyebrow">Attention</p><h2>Changes most often</h2></div></div>
        ${topChangingMarkup(o.top_changing_urls)}
      </div>
    </div>`;
  bindOverviewActions(container);
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
  return { ok: "OK", partial: "Partial", failed: "Failed", blocked: "Blocked", running: "Running", queued: "Queued" }[status] || "—";
}

function capturesListMarkup(captures) {
  if (captures.length === 0) return `<p class="archive-empty-note">No captures have run yet.</p>`;
  return `<ul class="archive-capture-list">
    ${captures.map((c) => `
      <li class="archive-capture-row state-${escapeHtml(c.status)}">
        ${icon(captureIcon(c.status))}
        <span class="analytics-row__value">${escapeHtml(c.client_name)}</span>
        <span class="archive-capture-row__counts">${c.urls_new} new · ${c.urls_unchanged} unchanged${c.urls_failed ? ` · ${c.urls_failed} failed` : ""}${c.urls_blocked ? ` · ${c.urls_blocked} blocked` : ""}</span>
        <span class="archive-capture-row__time">${c.finished_at ? formatTimestamp(c.finished_at) : "in progress…"}</span>
      </li>`).join("")}
  </ul>`;
}

function captureIcon(status) {
  return { ok: "circle-check", partial: "alert-triangle", failed: "circle-x", blocked: "shield-lock", running: "loader-2", queued: "clock" }[status] || "circle";
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
