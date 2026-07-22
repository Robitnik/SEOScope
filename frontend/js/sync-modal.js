import { cancelSyncJob, createSyncJob, importAISeoToolsHistory, retrySyncJob } from "./api.js";
import { PROVIDERS, PROVIDER_ORDER, STATE_META, STAGE_LABELS, escapeHtml, icon, isTerminal, percent, stateClass } from "./formatters.js";
import { triggerLogin } from "./session-status.js";
import { loadActiveJob, saveActiveJob } from "./store.js";
import { watchJob } from "./sse.js";

// task-22: session-stage panel above the progress matrix. State is derived
// straight from `job.cells` (already streamed over the existing SSE — no new
// backend event type needed): a provider whose cells never left `queued` is
// still being checked; one `awaiting_auth` cell means the lane parked there;
// cells `failed` with `error_code="auth_required"` mean the wait timed out.
// `sessionOverlays` layers the purely local, transient states (button
// clicked, waiting on the operator, no warm Chrome, a launch error) on top —
// those never survive a full page reload, only a close/reopen of the dialog
// within the same page (module state, not localStorage), which is enough:
// the underlying job/cell truth always restores from the SSE replay either way.
const sessionOverlays = new Map();
let countdownTimer = null;

function providerSessionState(provider, cells) {
  const providerCells = cells.filter((cell) => cell.provider === provider);
  if (!providerCells.length) return "checking";
  if (providerCells.every((cell) => cell.status === "not_configured")) return "not_configured";
  if (providerCells.some((cell) => cell.status === "awaiting_auth")) return "auth_required";
  if (providerCells.some((cell) => cell.error_code === "auth_required" && ["failed", "failed_interrupted"].includes(cell.status))) return "auth_timeout";
  const configured = providerCells.filter((cell) => cell.status !== "not_configured");
  if (configured.length && configured.every((cell) => cell.status === "queued")) return "checking";
  return "active";
}

function computeSessionPhase(provider, cells) {
  const derived = providerSessionState(provider, cells);
  const overlay = sessionOverlays.get(provider);
  if (derived === "auth_required") return overlay?.phase || "auth_required";
  if (overlay?.phase === "succeeded") return "succeeded";
  return derived;
}

function reconcileSessionOverlays(cells, rerender) {
  for (const provider of PROVIDER_ORDER) {
    const derived = providerSessionState(provider, cells);
    const overlay = sessionOverlays.get(provider);
    if (!overlay || derived === "auth_required" || overlay.phase === "succeeded") continue;
    if (derived === "active" && ["starting", "waiting"].includes(overlay.phase)) {
      sessionOverlays.set(provider, { phase: "succeeded" });
      setTimeout(() => {
        if (sessionOverlays.get(provider)?.phase !== "succeeded") return;
        sessionOverlays.delete(provider);
        rerender();
      }, 2500);
      continue;
    }
    sessionOverlays.delete(provider);
  }
}

function ensureCountdownTicking(rerender) {
  if (countdownTimer) return;
  countdownTimer = setInterval(() => {
    const anyWaiting = [...sessionOverlays.values()].some((overlay) => overlay.phase === "waiting");
    if (!anyWaiting) {
      clearInterval(countdownTimer);
      countdownTimer = null;
      return;
    }
    rerender();
  }, 1000);
}

function formatCountdown(overlay) {
  if (!overlay?.startedAt || !overlay?.timeoutSeconds) return "";
  const remaining = Math.max(0, Math.round(overlay.timeoutSeconds - (Date.now() - overlay.startedAt) / 1000));
  const mm = String(Math.floor(remaining / 60)).padStart(2, "0");
  const ss = String(remaining % 60).padStart(2, "0");
  return `${mm}:${ss} remaining.`;
}

function sessionPhaseIcon(phase) {
  return {
    checking: "loader-2", starting: "loader-2", waiting: "loader-2",
    active: "circle-check", succeeded: "circle-check",
    not_configured: "plug-off",
    auth_required: "key",
    no_chrome: "alert-circle", error: "alert-circle",
    auth_timeout: "clock-exclamation",
  }[phase] || "circle-help";
}

function sessionPhaseCopy(provider, phase, overlay) {
  const meta = PROVIDERS[provider] || { label: provider };
  switch (phase) {
    case "checking": return { label: "Checking…" };
    case "active": return { label: "Session active" };
    case "not_configured": return { label: "Not configured" };
    case "auth_required": return { label: "Login required", detail: `${meta.label} session expired — please log in again.` };
    case "starting": return { label: "Starting login…" };
    case "waiting": return { label: "Waiting for login…", detail: `The login window is open on the machine with the warm Chrome. ${formatCountdown(overlay)} Other services keep collecting.` };
    case "no_chrome": return { label: "No warm Chrome available", detail: "Run this on the machine with the warm Chrome:", command: overlay?.message ? overlay.message.replace(/^.*?— /, "") : `seo-audit browser-up --provider ${provider}` };
    case "error": return { label: "Login start failed", detail: overlay?.message || "Please try again." };
    case "succeeded": return { label: "Login succeeded, resuming…" };
    case "auth_timeout": return { label: "Wait timed out", detail: "Login was not completed in time — these cells were closed as «Login required». Other services and clients were not affected." };
    default: return { label: phase };
  }
}

// Buttons stay in the DOM (never `disabled`, only `aria-disabled`) across the
// whole auth_required -> starting -> waiting -> succeeded chain, all sharing
// the same `data-session-action` — a real `disabled` attribute drops out of
// the tab order and can't be refocused by `renderProgress`'s restore step,
// which is exactly the "focus lost on state change" failure the DoD calls out.
const SESSION_BUTTON_BY_PHASE = {
  auth_required: { label: "Log in", busy: false },
  starting: { label: "Starting…", busy: true },
  waiting: { label: "Waiting…", busy: true },
  no_chrome: { label: "Try again", busy: false },
  error: { label: "Try again", busy: false },
  succeeded: { label: "Done", busy: true },
};

function sessionRowMarkup(provider, phase) {
  const meta = PROVIDERS[provider] || { label: provider };
  const overlay = sessionOverlays.get(provider);
  const copy = sessionPhaseCopy(provider, phase, overlay);
  const spinning = ["checking", "starting", "waiting"].includes(phase) ? " icon--spin" : "";
  const button = SESSION_BUTTON_BY_PHASE[phase];
  return `
    <li class="session-row ${stateClass(phase)}" data-session-provider="${provider}">
      <span class="session-row__identity">${meta.icon ? `<img src="${meta.icon}" width="16" height="16" alt="">` : ""}<span>${meta.label}</span></span>
      <span class="session-row__status">${icon(sessionPhaseIcon(phase), `icon${spinning}`)}<span>${copy.label}</span></span>
      ${copy.detail ? `<p class="session-row__detail">${escapeHtml(copy.detail)}</p>` : ""}
      ${copy.command ? `<p class="session-row__detail"><code>${escapeHtml(copy.command)}</code></p>` : ""}
      ${button ? `<button class="button button--quiet session-row__action" type="button" data-session-action="${provider}" aria-disabled="${button.busy}">${button.label}</button>` : ""}
    </li>`;
}

function renderSessionStagePanel(job, rerender) {
  const cells = job.cells || [];
  if (!cells.length || isTerminal(job.status)) return "";
  const providers = PROVIDER_ORDER.filter((provider) => cells.some((cell) => cell.provider === provider));
  if (!providers.length) return "";
  reconcileSessionOverlays(cells, rerender);
  const rows = providers.map((provider) => ({ provider, phase: computeSessionPhase(provider, cells) }));
  if (rows.some((row) => row.phase === "waiting")) ensureCountdownTicking(rerender);
  const resolved = rows.filter((row) => row.phase !== "checking").length;
  const liveSummary = rows
    .map(({ provider, phase }) => `${(PROVIDERS[provider] || { label: provider }).label}: ${sessionPhaseCopy(provider, phase, sessionOverlays.get(provider)).label}`)
    .join(". ");
  return `
    <section class="session-stage" aria-label="Session check">
      <div class="session-stage__heading"><span>Session check</span><span class="session-stage__count">${resolved}/${rows.length}</span></div>
      <progress class="session-stage__progress" max="${rows.length}" value="${resolved}" aria-label="Sessions checked: ${resolved} of ${rows.length}"></progress>
      <ul class="session-row-list">${rows.map(({ provider, phase }) => sessionRowMarkup(provider, phase)).join("")}</ul>
      <p class="visually-hidden" aria-live="polite">${escapeHtml(liveSummary)}</p>
    </section>`;
}

export function createSyncModal({ getDashboard, onFinished, onToast }) {
  const dialog = document.querySelector("#sync-dialog");
  const title = document.querySelector("#sync-title");
  const subtitle = document.querySelector("#sync-subtitle");
  const body = document.querySelector("#sync-body");
  const footer = document.querySelector("#sync-footer");
  const dock = document.querySelector("#job-dock");
  const dockTitle = document.querySelector("#job-dock-title");
  const dockCopy = document.querySelector("#job-dock-copy");
  const dockPercent = document.querySelector("#job-dock-percent");
  const connection = document.querySelector("#connection-status");
  let opener = null;
  let stopWatching = null;
  let active = loadActiveJob();
  let currentJob = null;

  dialog.addEventListener("close", () => opener?.focus());
  document.querySelector("#job-dock-button").addEventListener("click", (event) => {
    opener = event.currentTarget;
    if (!dialog.open) dialog.showModal();
    if (currentJob) renderProgress(currentJob);
  });

  function openSelection({ clientId = null, provider = null, trigger } = {}) {
    opener = trigger || document.activeElement;
    const dashboard = getDashboard();
    if (!dashboard) return;
    title.textContent = clientId ? "Update client" : "Update metrics";
    subtitle.textContent = "Collection runs in the background. Closing this window won't stop it.";
    const selectedClients = clientId ? [clientId] : dashboard.clients.map((client) => client.client_id);
    const selectedProviders = provider ? [provider] : PROVIDER_ORDER;
    body.innerHTML = `
      <div class="selection-grid">
        <fieldset class="selection-group"><legend>Clients</legend><div class="check-list">
          ${dashboard.clients.map((client) => `<label class="check-row"><input type="checkbox" name="sync-client" value="${client.client_id}" ${selectedClients.includes(client.client_id) ? "checked" : ""}><span>${client.name}<small>${client.domain}</small></span></label>`).join("")}
        </div></fieldset>
        <fieldset class="selection-group"><legend>Sources</legend><div class="check-list">
          ${PROVIDER_ORDER.map((key) => `<label class="check-row"><input type="checkbox" name="sync-provider" value="${key}" ${selectedProviders.includes(key) ? "checked" : ""}><span>${PROVIDERS[key].label}</span></label>`).join("")}
        </div></fieldset>
      </div>`;
    footer.innerHTML = `<button class="button button--quiet" type="button" data-close>Cancel</button><button class="button button--primary" type="button" data-start>${icon("player-play")}Start collection</button>`;
    footer.querySelector("[data-close]").addEventListener("click", () => dialog.close());
    footer.querySelector("[data-start]").addEventListener("click", startSelected);
    if (!dialog.open) dialog.showModal();
  }

  async function startSelected(event) {
    const clientIds = [...body.querySelectorAll('input[name="sync-client"]:checked')].map((input) => input.value);
    const providers = [...body.querySelectorAll('input[name="sync-provider"]:checked')].map((input) => input.value);
    if (!clientIds.length || !providers.length) {
      onToast("Select at least one client and one source.");
      return;
    }
    sessionOverlays.clear();
    event.currentTarget.disabled = true;
    try {
      const dashboard = getDashboard();
      title.textContent = "History import";
      subtitle.textContent = "Reading existing AISeoTools reports before the live collection.";
      body.innerHTML = `<div class="state-panel state-running">${icon("database-import")}<p><strong>Syncing AISeoTools</strong><br>Importing new artifacts and applying stored client matches.</p></div>`;
      footer.innerHTML = `<button class="button button--quiet" type="button" disabled>Preparing collection…</button>`;
      const imported = await importAISeoToolsHistory(clientIds);
      if (imported.imported_artifacts > 0) {
        onToast(`Imported ${imported.imported_artifacts} new AISeoTools artifacts.`);
      }
      const accepted = await createSyncJob({
        client_ids: clientIds,
        providers,
        period_kind: dashboard.period_kind,
        comparison_mode: "closed",
        mode: "collect_missing",
        source_mode: "auto",
        idempotency_key: crypto.randomUUID?.() || `${Date.now()}-${Math.random()}`,
      }, dashboard);
      active = { id: accepted.batch_id || accepted.job_id, eventsUrl: accepted.events_url, lastEventId: "" };
      saveActiveJob(active);
      beginWatch();
      title.textContent = "Data collection";
      subtitle.textContent = "Services run in parallel; clients within a service run sequentially.";
      renderProgress({ status: "queued", progress_percent: 0, cells: [] });
    } catch (error) {
      openSelection({ trigger: opener });
      onToast(`Could not start collection: ${error.message}`);
    }
  }

  function beginWatch() {
    stopWatching?.();
    if (!active?.id) return;
    dock.hidden = false;
    stopWatching = watchJob({
      id: active.id,
      eventsUrl: active.eventsUrl || `/api/v1/sync-jobs/${active.id}/events`,
      lastEventId: active.lastEventId,
      onProgress(job, lastEventId) {
        currentJob = job;
        active = { ...active, lastEventId };
        saveActiveJob(active);
        updateDock(job);
        if (dialog.open) renderProgress(job);
      },
      onConnection(state) {
        connection.dataset.state = state === "connected" ? "online" : "offline";
        connection.querySelector("span:last-child").textContent = state === "connected" ? "API online" : "Reconnecting SSE";
        if (dialog.open && state !== "connected") renderOfflineNotice();
      },
      async onTerminal(job) {
        currentJob = job;
        saveActiveJob(null);
        active = null;
        updateDock(job);
        if (dialog.open) renderProgress(job);
        await onFinished(job);
      },
      onError(error) { onToast(`Progress temporarily unavailable: ${error.message}`); },
    });
  }

  function renderOfflineNotice() {
    if (body.querySelector(".job-alert")) return;
    body.insertAdjacentHTML("afterbegin", `<div class="job-alert"><strong>Live connection interrupted.</strong> Status will resume automatically; the collection keeps running on the server.</div>`);
  }

  function renderProgress(job) {
    const focused = body.contains(document.activeElement) ? document.activeElement.dataset.sessionAction : null;
    const dashboard = getDashboard();
    const clients = new Map((dashboard?.clients || []).map((client) => [String(client.client_id), client.name]));
    const cells = job.cells || [];
    const clientIds = [...new Set(cells.map((cell) => String(cell.client_id)))];
    const providers = PROVIDER_ORDER.filter((provider) => cells.some((cell) => cell.provider === provider));
    const progressValue = percent(job.progress_percent);
    const state = STATE_META[job.status] || STATE_META.running;
    title.textContent = terminalTitle(job);
    // A lane parked on `awaiting_auth` still aggregates to a job-level
    // `queued` — truthful (it really is waiting), but «Queued» reads as "not
    // started yet" when the run is actually blocked on the operator.
    const awaitingAuth = cells.some((cell) => cell.status === "awaiting_auth");
    subtitle.textContent = isTerminal(job.status) && hasAttentionCells(cells)
      ? "Collected data has been saved. Review cells that need attention."
      : awaitingAuth
        ? "Collection paused: a service login is required."
        : state.label;
    body.innerHTML = `
      ${renderSessionStagePanel(job, () => { if (dialog.open) renderProgress(currentJob); })}
      <div class="progress-overview">
        <span class="progress-number">${progressValue}%</span>
        <div><progress class="progress-track" max="100" value="${progressValue}" aria-label="Overall progress: ${progressValue}%"></progress><div class="progress-copy">${progressSummary(cells)}</div></div>
      </div>
      ${clientIds.length ? `<div class="matrix-wrap"><table class="progress-matrix"><thead><tr><th scope="col">Client</th>${providers.map((provider) => `<th scope="col">${PROVIDERS[provider].label}</th>`).join("")}</tr></thead><tbody>${clientIds.map((clientId) => `<tr><td>${clients.get(clientId) || clientId}</td>${providers.map((provider) => matrixCell(cells.find((cell) => String(cell.client_id) === clientId && cell.provider === provider))).join("")}</tr>`).join("")}</tbody></table></div>` : `<div class="state-panel"><p><strong>Building the queue.</strong><br>The table will appear once the server responds.</p></div>`}`;
    footer.innerHTML = footerActions(job);
    footer.querySelector("[data-close]")?.addEventListener("click", () => dialog.close());
    footer.querySelector("[data-cancel]")?.addEventListener("click", () => cancelJob());
    footer.querySelector("[data-retry]")?.addEventListener("click", () => retryFailed());
    body.querySelectorAll("[data-session-action]").forEach((button) => {
      button.addEventListener("click", () => {
        if (button.getAttribute("aria-disabled") === "true") return;
        handleLoginClick(button.dataset.sessionAction);
      });
    });
    if (focused) body.querySelector(`[data-session-action="${focused}"]`)?.focus();
  }

  async function handleLoginClick(provider) {
    sessionOverlays.set(provider, { phase: "starting" });
    if (dialog.open) renderProgress(currentJob);
    const result = await triggerLogin(provider);
    if (!result.ok) {
      sessionOverlays.set(provider, { phase: result.reason === "no_warm_chrome" ? "no_chrome" : "error", message: result.message });
      if (dialog.open) renderProgress(currentJob);
      return;
    }
    sessionOverlays.set(provider, { phase: "waiting", startedAt: Date.now(), timeoutSeconds: result.timeoutSeconds });
    if (dialog.open) renderProgress(currentJob);
  }

  function matrixCell(cell) {
    if (!cell) return `<td><span class="matrix-cell state-not_configured"><strong>— Not selected</strong></span></td>`;
    const meta = STATE_META[cell.status] || STATE_META.running;
    const value = percent(cell.progress_percent);
    return `<td><div class="matrix-cell ${stateClass(cell.status)}"><strong>${icon(meta.icon)}${meta.label}</strong><small>${cell.status === "not_configured" ? "Integration not connected" : (cell.error_code || STAGE_LABELS[cell.current_stage] || "Waiting for status")}</small><progress class="mini-progress" max="100" value="${value}" aria-label="${meta.label}: ${value}%"></progress></div></td>`;
  }

  function progressSummary(cells) {
    const done = cells.filter((cell) => ["succeeded", "partial", "failed", "failed_interrupted", "not_configured", "cancelled", "skipped"].includes(cell.status)).length;
    return `Completed ${done} of ${cells.length} cells. Closing this window will not stop the collection.`;
  }

  function hasAttentionCells(cells) {
    return cells.some((cell) => ["partial", "failed", "failed_interrupted", "not_configured"].includes(cell.status));
  }

  function terminalTitle(job) {
    if (!isTerminal(job.status)) return "Data collection";
    return hasAttentionCells(job.cells || []) ? "Collection finished with issues" : "Collection finished";
  }

  function footerActions(job) {
    const failed = (job.cells || []).some((cell) => ["failed", "failed_interrupted"].includes(cell.status));
    if (isTerminal(job.status)) return `${failed ? `<button class="button button--quiet" type="button" data-retry>${icon("refresh")}Retry failed</button>` : ""}<button class="button button--primary" type="button" data-close>Done</button>`;
    return `<button class="button button--quiet" type="button" data-close>Hide</button><button class="button button--quiet" type="button" data-cancel>${icon("player-stop")}Stop</button>`;
  }

  async function cancelJob() {
    if (!active?.id) return;
    footer.querySelector("[data-cancel]").disabled = true;
    try { currentJob = await cancelSyncJob(active.id); renderProgress(currentJob); }
    catch (error) { onToast(`Could not stop the collection: ${error.message}`); }
  }

  async function retryFailed() {
    if (!currentJob) return;
    footer.querySelector("[data-retry]").disabled = true;
    try {
      const accepted = await retrySyncJob(currentJob.batch_id || currentJob.job_id || active?.id);
      active = { id: accepted.batch_id || accepted.job_id, eventsUrl: accepted.events_url, lastEventId: "" };
      saveActiveJob(active);
      beginWatch();
    } catch (error) { onToast(`Could not start the retry: ${error.message}`); }
  }

  function updateDock(job) {
    const terminal = isTerminal(job.status);
    dock.hidden = false;
    dockTitle.textContent = terminal ? terminalTitle(job) : "Update in progress";
    dockCopy.textContent = terminal ? "View results" : "View progress";
    dockPercent.textContent = `${percent(job.progress_percent)}%`;
  }

  if (active?.id) beginWatch();
  return { openSelection };
}
