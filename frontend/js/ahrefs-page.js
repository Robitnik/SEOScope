import { createSyncJob, getAhrefsAffectedUrls, getAhrefsSiteAudit, getDashboard, isAbortError } from "./api.js?v=20260803-connect2";
import { clientHeaderSkeleton, sectionSkeleton } from "./skeleton.js?v=20260803-connect2";
import { buildClientHash, replaceAhrefsHash } from "./router.js?v=20260803-connect2";
import { escapeHtml, formatTimestamp, icon, initials, PROVIDERS, STAGE_LABELS, STATE_META, percent, stateClass } from "./formatters.js?v=20260803-connect2";
import { renderAhrefsError, renderAhrefsPanel } from "./ahrefs-panel.js?v=20260803-connect2";
import { watchJob } from "./sse.js?v=20260803-connect2";

const root = document.querySelector("#ahrefs-page-root");
let requestToken = 0;
let affectedRequestToken = 0;
let current = null;
let stopLiveWatching = null;
let liveCollection = null;
let lastAhrefsData = null;

function paramsFromQuery(query) {
  return {
    ahrefsSeverity: query.get("ahseverity") || "",
    ahrefsSearch: query.get("ahq") || "",
    ahrefsSelectedIssue: query.get("ahissue") || "",
    ahrefsUrlSearch: query.get("ahurlq") || "",
    ahrefsUrlOffset: 0,
    ahrefsRevealSelection: query.has("ahissue"),
  };
}

function persist() {
  replaceAhrefsHash(current.clientId, {
    ahseverity: current.ahrefsSeverity,
    ahq: current.ahrefsSearch,
    ahissue: current.ahrefsSelectedIssue,
    ahurlq: current.ahrefsUrlSearch,
  });
}

export async function initAhrefsPage(clientId, query) {
  stopLiveWatching?.();
  stopLiveWatching = null;
  liveCollection = null;
  lastAhrefsData = null;
  affectedRequestToken += 1;
  current = { clientId, ...paramsFromQuery(query) };
  root.innerHTML = shell();
  bindShell();
  await loadClient();
}

function shell() {
  return `
    <a class="back-link" href="${buildClientHash(current.clientId)}">${icon("arrow-left")}<span>Back to client</span></a>
    <header class="client-page__header" id="ap-header" aria-live="polite" aria-busy="true">
      ${clientHeaderSkeleton()}
    </header>
    <section class="period-panel" aria-label="Live Site Audit collection">
      <div class="period-panel__live">
        <button type="button" class="button button--quiet" id="ap-live-refresh">${icon("refresh")}Refresh live data</button>
        <div class="live-collection" id="ap-live-status" aria-live="polite" hidden></div>
      </div>
    </section>
    <div id="ap-content">
      <section class="ahrefs-section" id="ap-ahrefs" aria-label="Ahrefs live Site Audit" aria-live="polite">
        ${sectionSkeleton({ label: "Ahrefs Site Audit", kpis: 3, rows: 6, columns: 3 })}
      </section>
    </div>`;
}

function bindShell() {
  document.querySelector("#ap-live-refresh").addEventListener("click", () => startLiveCollection());
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
    const ahrefs = (client.providers || []).find((item) => item.provider === "ahrefs");
    current.ahrefsConfigured = Boolean(ahrefs && ahrefs.state !== "not_configured");
    current.ahrefsSummary = ahrefs || null;
    if (!current.ahrefsConfigured) {
      renderGate();
      return;
    }
    await loadAhrefs();
  } catch (error) {
    // An aborted request means a newer load superseded this one — never an
    // operator-visible failure (see `isAbortError`).
    if (isAbortError(error)) return;
    const header = document.querySelector("#ap-header");
    header.setAttribute("aria-busy", "false");
    header.innerHTML = `
      <div class="state-panel state-failed">
        ${icon("wifi-off")}
        <p><strong>Could not load this client.</strong><br>${escapeHtml(error.message)}. Nothing was changed — retry once the connection is back.</p>
        <button type="button" class="button button--primary" id="ap-retry">${icon("refresh")}Retry</button>
      </div>`;
    header.querySelector("#ap-retry").addEventListener("click", () => {
      header.setAttribute("aria-busy", "true");
      header.innerHTML = clientHeaderSkeleton();
      loadClient();
    });
  }
}

function renderNotFound() {
  root.querySelector("#ap-header").setAttribute("aria-busy", "false");
  root.querySelector("#ap-header").innerHTML = `
    <div class="state-panel state-failed">
      ${icon("user-x")}
      <p><strong>Client not found.</strong><br>The link may be outdated.</p>
      <a class="button button--primary" href="#/">Back to client list</a>
    </div>`;
  document.querySelector(".period-panel")?.remove();
  document.querySelector("#ap-content")?.remove();
}

function renderGate() {
  document.querySelector("#ap-content").innerHTML = `<div class="state-panel state-not_configured">${icon("plug-off")}<p><strong>Ahrefs is not connected for this client.</strong><br>Connect Ahrefs from the dashboard to enable Live Site Audit.</p><a class="button button--quiet" href="#/">Manage integrations</a></div>`;
  document.querySelector("#ap-live-refresh").disabled = true;
}

function renderHeader(client) {
  const state = STATE_META[client.overall_status] || STATE_META.attention;
  const logo = client.logo_path
    ? `<img class="client-logo" src="${escapeHtml(client.logo_path)}" width="52" height="52" alt="Logo of ${escapeHtml(client.name)}" loading="lazy">`
    : `<span class="client-logo" aria-hidden="true">${escapeHtml(initials(client.name))}</span>`;
  document.querySelector("#ap-header").innerHTML = `
    <div class="client-page__identity">
      ${logo}
      <div>
        <p class="eyebrow">Ahrefs · Live Site Audit</p>
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
  const button = document.querySelector("#ap-live-refresh");
  if (!button) return;
  button.disabled = busy;
  button.innerHTML = busy ? `${icon("loader-2")}Collecting live data…` : `${icon("refresh")}Refresh live data`;
}

function renderLiveStatus(job, { phase = "running", message = "" } = {}) {
  const container = document.querySelector("#ap-live-status");
  if (!container) return;
  container.hidden = false;
  const progress = percent(job?.progress_percent);
  const cells = job?.cells || [];
  const activeCell = cells.find((cell) => !["succeeded", "partial", "failed", "failed_interrupted", "not_configured", "cancelled", "skipped"].includes(cell.status));
  const stage = STAGE_LABELS[activeCell?.current_stage || job?.current_stage] || "Waiting for the collector";
  const failed = phase === "failed" || job?.status === "failed" || job?.status === "partial";
  const complete = phase === "complete";
  const title = complete
    ? "Live data updated"
    : failed
      ? "Live collection finished with issues"
      : phase === "starting" ? "Starting live collection" : `Fetching live data · ${progress}%`;
  const detail = message || (complete
    ? "The latest values have been saved and the page is being refreshed."
    : failed
      ? "Available results were saved. Use Refresh live data to retry."
      : stage);
  container.className = `live-collection ${failed ? "live-collection--failed" : complete ? "live-collection--complete" : "live-collection--running"}`;
  container.innerHTML = `
    <div class="live-collection__copy">
      ${icon(complete ? "circle-check" : failed ? "alert-triangle" : "cloud-download")}
      <p><strong>${escapeHtml(title)}</strong><br><span>${escapeHtml(detail)}</span><small>${PROVIDERS.ahrefs?.label || "Ahrefs"}</small></p>
    </div>
    ${complete || failed ? "" : `<progress max="100" value="${progress}" aria-label="Live collection progress: ${progress}%"></progress>`}`;
}

async function startLiveCollection() {
  if (liveCollection?.active) return;
  if (!current.ahrefsConfigured) return;
  liveCollection = { active: true };
  setLiveButtonBusy(true);
  renderLiveStatus({ status: "queued", progress_percent: 0, cells: [] }, { phase: "starting" });
  try {
    const accepted = await createSyncJob({
      client_ids: [current.clientId],
      providers: ["ahrefs"],
      period_kind: "week",
      comparison_mode: "closed",
      mode: "force_refresh",
      source_mode: "auto",
      idempotency_key: crypto.randomUUID?.() || `${Date.now()}-${Math.random()}`,
    }, {
      period_kind: "week",
      clients: [{ client_id: current.clientId, providers: [{ provider: "ahrefs", state: "complete" }] }],
    });
    const id = accepted.batch_id || accepted.job_id;
    stopLiveWatching?.();
    stopLiveWatching = watchJob({
      id,
      eventsUrl: accepted.events_url,
      onProgress(job) { renderLiveStatus(job); },
      onConnection(state) {
        if (state === "offline" && liveCollection?.active) {
          renderLiveStatus({ status: "running", progress_percent: 0, cells: [] }, { message: "Live connection interrupted; the server collection continues and will reconnect automatically." });
        }
      },
      async onTerminal(job) {
        liveCollection = { active: false };
        stopLiveWatching = null;
        setLiveButtonBusy(false);
        const hasFailure = ["failed", "partial", "cancelled"].includes(job.status)
          || (job.cells || []).some((cell) => ["failed", "failed_interrupted", "not_configured", "cancelled"].includes(cell.status));
        renderLiveStatus(job, { phase: hasFailure ? "failed" : "complete" });
        if (job.status !== "cancelled") await loadClient();
      },
      onError(error) {
        renderLiveStatus({ status: "running", progress_percent: 0, cells: [] }, { message: `Progress temporarily unavailable: ${error.message}` });
      },
    });
  } catch (error) {
    liveCollection = { active: false };
    setLiveButtonBusy(false);
    renderLiveStatus({ status: "failed", progress_percent: 0, cells: [] }, { phase: "failed", message: `Could not start live collection: ${error.message}` });
  }
}

async function loadAhrefsAffected(issueSlug, offset = 0, query = current.ahrefsUrlSearch) {
  if (!issueSlug) return null;
  return getAhrefsAffectedUrls(current.clientId, { issueSlug, query, limit: 100, offset });
}

async function loadAhrefs() {
  const token = ++requestToken;
  const affectedToken = ++affectedRequestToken;
  try {
    const audit = await getAhrefsSiteAudit(current.clientId);
    if (token !== requestToken) return;
    const selectedExists = (audit.issues || []).some((issue) => issue.slug === current.ahrefsSelectedIssue);
    if (!selectedExists) current.ahrefsSelectedIssue = audit.issues?.[0]?.slug || "";
    const affected = await loadAhrefsAffected(current.ahrefsSelectedIssue, 0);
    if (token !== requestToken || affectedToken !== affectedRequestToken) return;
    renderAhrefs({ summary: current.ahrefsSummary, audit, affected });
    if (current.ahrefsRevealSelection) {
      current.ahrefsRevealSelection = false;
      revealAffectedPages({ focus: true });
    }
  } catch (error) {
    if (token !== requestToken || affectedToken !== affectedRequestToken) return;
    renderAhrefsError(document.querySelector("#ap-ahrefs"), error.message);
  }
}

function revealAffectedPages({ focus = false } = {}) {
  requestAnimationFrame(() => {
    const target = document.querySelector("#ahrefs-affected-pages");
    if (!target) return;
    const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    target.scrollIntoView({ behavior: reducedMotion ? "auto" : "smooth", block: "start" });
    if (focus) target.focus({ preventScroll: true });
  });
}

async function updateAffected(issueSlug, offset, query, { reveal = false } = {}) {
  const token = ++affectedRequestToken;
  renderAhrefs({ ...lastAhrefsData, affected: null, affectedError: "" });
  if (reveal) revealAffectedPages();
  try {
    const affected = await loadAhrefsAffected(issueSlug, offset, query);
    if (token !== affectedRequestToken || current.ahrefsSelectedIssue !== issueSlug) return;
    renderAhrefs({ ...lastAhrefsData, affected, affectedError: "" });
    if (reveal) revealAffectedPages({ focus: true });
  } catch (error) {
    if (token !== affectedRequestToken || current.ahrefsSelectedIssue !== issueSlug) return;
    renderAhrefs({ ...lastAhrefsData, affected: null, affectedError: error.message });
    if (reveal) revealAffectedPages({ focus: true });
  }
}

function renderAhrefs(data) {
  lastAhrefsData = data;
  renderAhrefsPanel(document.querySelector("#ap-ahrefs"), {
    ...data,
    state: current,
    onChange(patch) {
      Object.assign(current, patch);
      persist();
      renderAhrefs(lastAhrefsData);
    },
    onSelectIssue(slug) {
      current.ahrefsSelectedIssue = slug;
      current.ahrefsUrlOffset = 0;
      current.ahrefsUrlSearch = "";
      persist();
      void updateAffected(slug, 0, "", { reveal: true });
    },
    onAffectedPage(offset, query = current.ahrefsUrlSearch) {
      current.ahrefsUrlOffset = offset;
      current.ahrefsUrlSearch = query;
      persist();
      void updateAffected(current.ahrefsSelectedIssue, offset, query);
    },
  });
}
