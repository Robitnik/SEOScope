import { getBlogsLatest, getBlogsPosts, getDashboard, blogsExportCsvUrl, blogsExportXlsxUrl, startBlogsRun, isAbortError } from "./api.js?v=20260805-status1";
import { clientHeaderSkeleton, sectionSkeleton } from "./skeleton.js?v=20260805-status1";
import { buildClientHash, replaceBlogsHash } from "./router.js?v=20260805-status1";
import { escapeHtml, formatTimestamp, icon, initials, PROVIDERS, STAGE_LABELS, STATE_META, percent, stateClass } from "./formatters.js?v=20260805-status1";
import { renderBlogsError, renderBlogsPanel } from "./blogs-panel.js?v=20260805-status1";
import { watchJob } from "./sse.js?v=20260805-status1";

const root = document.querySelector("#blogs-page-root");
let requestToken = 0;
let current = null;
let stopLiveWatching = null;
let liveCollection = null;

function paramsFromQuery(query) {
  return {
    blogsSearch: query.get("bq") || "",
    blogsSort: query.get("bsort") || "date_desc",
    blogsOffset: Number(query.get("boff") || 0) || 0,
  };
}

function persist() {
  replaceBlogsHash(current.clientId, {
    bq: current.blogsSearch,
    bsort: current.blogsSort !== "date_desc" ? current.blogsSort : "",
    boff: current.blogsOffset || "",
  });
}

export async function initBlogsPage(clientId, query) {
  stopLiveWatching?.();
  stopLiveWatching = null;
  liveCollection = null;
  current = { clientId, ...paramsFromQuery(query) };
  root.innerHTML = shell();
  bindShell();
  await loadClient();
}

function shell() {
  return `
    <a class="back-link" href="${buildClientHash(current.clientId)}">${icon("arrow-left")}<span>Back to client</span></a>
    <header class="client-page__header" id="bp-header" aria-live="polite" aria-busy="true">
      ${clientHeaderSkeleton()}
    </header>
    <section class="period-panel" aria-label="Blog crawl">
      <div class="period-panel__live">
        <button type="button" class="button button--quiet" id="bp-live-refresh">${icon("refresh")}Refresh data</button>
        <div class="live-collection" id="bp-live-status" aria-live="polite" hidden></div>
      </div>
    </section>
    <div id="bp-content">
      <section class="blogs-section" id="bp-blogs" aria-label="Client blog posts" aria-live="polite">
        ${sectionSkeleton({ label: "blog posts", kpis: 1, rows: 8, columns: 3 })}
      </section>
    </div>`;
}

function bindShell() {
  document.querySelector("#bp-live-refresh").addEventListener("click", () => startLiveCollection());
}

async function loadClient() {
  try {
    const dashboard = await getDashboard({ period: "week" });
    const client = (dashboard.clients || []).find((item) => item.client_id === current.clientId);
    if (!client) {
      renderNotFound();
      return;
    }
    renderHeader(client);
    await loadBlogs();
  } catch (error) {
    // An aborted request means a newer load superseded this one — never an
    // operator-visible failure (see `isAbortError`).
    if (isAbortError(error)) return;
    const header = document.querySelector("#bp-header");
    header.setAttribute("aria-busy", "false");
    header.innerHTML = `
      <div class="state-panel state-failed">
        ${icon("wifi-off")}
        <p><strong>Could not load this client.</strong><br>${escapeHtml(error.message)}. Nothing was changed — retry once the connection is back.</p>
        <button type="button" class="button button--primary" id="bp-retry">${icon("refresh")}Retry</button>
      </div>`;
    header.querySelector("#bp-retry").addEventListener("click", () => {
      header.setAttribute("aria-busy", "true");
      header.innerHTML = clientHeaderSkeleton();
      loadClient();
    });
  }
}

function renderNotFound() {
  root.querySelector("#bp-header").setAttribute("aria-busy", "false");
  root.querySelector("#bp-header").innerHTML = `
    <div class="state-panel state-failed">
      ${icon("user-x")}
      <p><strong>Client not found.</strong><br>The link may be outdated.</p>
      <a class="button button--primary" href="#/">Back to client list</a>
    </div>`;
  document.querySelector(".period-panel")?.remove();
  document.querySelector("#bp-content")?.remove();
}

function renderHeader(client) {
  const state = STATE_META[client.overall_status] || STATE_META.attention;
  const logo = client.logo_path
    ? `<img class="client-logo" src="${escapeHtml(client.logo_path)}" width="52" height="52" alt="Logo of ${escapeHtml(client.name)}" loading="lazy">`
    : `<span class="client-logo" aria-hidden="true">${escapeHtml(initials(client.name))}</span>`;
  document.querySelector("#bp-header").innerHTML = `
    <div class="client-page__identity">
      ${logo}
      <div>
        <p class="eyebrow">Client Blogs</p>
        <h1>${escapeHtml(client.name)}</h1>
        <a href="https://${encodeURI(client.domain)}" target="_blank" rel="noreferrer">${escapeHtml(client.domain)}</a>
      </div>
    </div>
    <div class="client-page__status">
      <span class="status-label ${stateClass(client.overall_status)}">${icon(state.icon)}${state.label}</span>
      <span class="client-page__sync">Last collection: ${formatTimestamp(client.last_synced_at, "not yet")}</span>
    </div>`;
}

function setLiveButtonBusy(busy) {
  const button = document.querySelector("#bp-live-refresh");
  if (!button) return;
  button.disabled = busy;
  button.innerHTML = busy ? `${icon("loader-2", "icon icon--spin")}Crawling…` : `${icon("refresh")}Refresh data`;
}

function renderLiveStatus(job, { phase = "running", message = "" } = {}) {
  const container = document.querySelector("#bp-live-status");
  if (!container) return;
  container.hidden = false;
  const progress = percent(job?.progress_percent);
  const cells = job?.cells || [];
  const activeCell = cells.find((cell) => !["succeeded", "partial", "failed", "failed_interrupted", "not_configured", "cancelled", "skipped"].includes(cell.status));
  // `progress_message` is a live, free-form count ("found 148 posts so far")
  // the adapter emits as pages/pagination come in — prefer it over the static
  // per-stage label so the operator sees the count grow in real time, not just
  // "Crawling site pages" the whole run through.
  const stage = activeCell?.progress_message
    || STAGE_LABELS[activeCell?.current_stage || job?.current_stage]
    || "Waiting for the crawler";
  const failed = phase === "failed" || job?.status === "failed" || job?.status === "partial";
  const complete = phase === "complete";
  const title = complete
    ? "Blog data updated"
    : failed
      ? "Crawl finished with issues"
      : phase === "starting" ? "Starting crawl" : `Crawling the site · ${progress}%`;
  const detail = message || (complete
    ? "The latest post list has been saved and the page is being refreshed."
    : failed
      ? "Available results were saved. Use Refresh data to retry."
      : stage);
  container.className = `live-collection ${failed ? "live-collection--failed" : complete ? "live-collection--complete" : "live-collection--running"}`;
  container.innerHTML = `
    <div class="live-collection__copy">
      ${icon(complete ? "circle-check" : failed ? "alert-triangle" : "cloud-download")}
      <p><strong>${escapeHtml(title)}</strong><br><span>${escapeHtml(detail)}</span><small>${PROVIDERS.blogs?.label || "Blogs"}</small></p>
    </div>
    ${complete || failed ? "" : `<progress max="100" value="${progress}" aria-label="Crawl progress: ${progress}%"></progress>`}`;
}

async function startLiveCollection() {
  if (liveCollection?.active) return;
  liveCollection = { active: true };
  setLiveButtonBusy(true);
  renderLiveStatus({ status: "queued", progress_percent: 0, cells: [] }, { phase: "starting" });
  try {
    const accepted = await startBlogsRun(current.clientId);
    const id = accepted.job_id;
    stopLiveWatching?.();
    stopLiveWatching = watchJob({
      id,
      eventsUrl: accepted.events_url,
      onProgress(job) { renderLiveStatus(job); },
      onConnection(state) {
        if (state === "offline" && liveCollection?.active) {
          renderLiveStatus({ status: "running", progress_percent: 0, cells: [] }, { message: "Live connection interrupted; the server crawl continues and will reconnect automatically." });
        }
      },
      async onTerminal(job) {
        liveCollection = { active: false };
        stopLiveWatching = null;
        setLiveButtonBusy(false);
        const hasFailure = ["failed", "partial", "cancelled"].includes(job.status)
          || (job.cells || []).some((cell) => ["failed", "failed_interrupted", "not_configured", "cancelled"].includes(cell.status));
        renderLiveStatus(job, { phase: hasFailure ? "failed" : "complete" });
        if (job.status !== "cancelled") {
          current.blogsOffset = 0;
          await loadBlogs();
        }
      },
      onError(error) {
        renderLiveStatus({ status: "running", progress_percent: 0, cells: [] }, { message: `Progress temporarily unavailable: ${error.message}` });
      },
    });
  } catch (error) {
    liveCollection = { active: false };
    setLiveButtonBusy(false);
    renderLiveStatus({ status: "failed", progress_percent: 0, cells: [] }, { phase: "failed", message: `Could not start the crawl: ${error.message}` });
  }
}

async function loadBlogs() {
  const token = ++requestToken;
  try {
    const summary = await getBlogsLatest(current.clientId);
    if (token !== requestToken) return;
    if (!summary.has_run) {
      renderBlogs({ summary, page: null });
      return;
    }
    const page = await getBlogsPosts(current.clientId, {
      q: current.blogsSearch, sort: current.blogsSort, offset: current.blogsOffset,
    });
    if (token !== requestToken) return;
    renderBlogs({ summary, page });
  } catch (error) {
    if (token !== requestToken) return;
    renderBlogsError(document.querySelector("#bp-blogs"), error.message);
  }
}

function renderBlogs({ summary, page }) {
  renderBlogsPanel(document.querySelector("#bp-blogs"), {
    summary,
    page,
    exportCsvUrl: blogsExportCsvUrl(current.clientId),
    exportXlsxUrl: blogsExportXlsxUrl(current.clientId),
    state: current,
    onChange(patch) {
      Object.assign(current, patch);
      current.blogsOffset = 0;
      persist();
      void loadBlogs();
    },
    onPage(offset) {
      current.blogsOffset = offset;
      persist();
      void loadBlogs();
    },
  });
}
