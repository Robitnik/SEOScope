import { cancelSyncJob, createSyncJob, getProviderSession, importAISeoToolsHistory, retrySyncJob, startTechAuditBatch } from "./api.js?v=20260731-refresh1";
import { PROVIDERS, PROVIDER_ORDER, STATE_META, STAGE_LABELS, escapeHtml, icon, isTerminal, percent, stateClass } from "./formatters.js?v=20260731-refresh1";
import { triggerLogin } from "./session-status.js?v=20260731-refresh1";
import { loadActiveJob, saveActiveJob } from "./store.js?v=20260731-refresh1";
import { watchJob } from "./sse.js?v=20260731-refresh1";

// task-22 follow-up: providers backed by a warm-Chrome browser session (mirrors
// `BROWSER_PROVIDERS` in `app/integrations/browser_session.py`). `reporting_ninja`
// authenticates via API key and has no browser session to check.
const BROWSER_SESSION_PROVIDERS = ["ahrefs", "semrush"];
const BROWSER_UP_COMMAND = "seo-audit browser-up";
const AUDIT_NUMBER = new Intl.NumberFormat("en-US");

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

const TERMINAL_CELL_STATUSES = new Set([
  "succeeded", "partial", "failed", "failed_interrupted",
  "not_configured", "cancelled", "skipped",
]);

function cellDisplayState(cell) {
  if (["succeeded", "partial"].includes(cell.status) && cell.quality && cell.quality !== "complete") {
    return cell.quality;
  }
  return cell.status;
}

function cellDetail(cell, displayState) {
  if (!TERMINAL_CELL_STATUSES.has(cell.status)) {
    return cell.error_code || STAGE_LABELS[cell.current_stage] || "Waiting for status";
  }
  if (cell.status === "not_configured") return "Integration not connected";
  if (cell.status === "failed" || cell.status === "failed_interrupted") {
    return cell.error_code ? `Collection stopped: ${cell.error_code.replaceAll("_", " ")}` : "Collection failed";
  }
  if (cell.status === "cancelled") return "Collection cancelled";
  if (cell.status === "skipped") return "Fresh data already available";
  if (displayState === "truncated") return "Core metrics complete; detail limited to top results";
  if (displayState === "partial") return "Some requested data was unavailable";
  if (displayState === "stale") return "Collected data is older than requested";
  if (displayState === "unavailable") return "The source returned no usable data";
  return "Collection complete";
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
    resetProgressView();
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
        <fieldset class="selection-group selection-options"><legend>Options</legend>
          <label class="check-row"><input type="checkbox" name="sync-force"><span>Re-collect data<small>By default a cell whose data is still fresh is skipped. Tick this to run every selected cell again — slower, and it spends provider units.</small></span></label>
        </fieldset>
      </div>`;
    footer.innerHTML = `<button class="button button--quiet" type="button" data-close>Cancel</button><button class="button button--primary" type="button" data-start>${icon("player-play")}Start collection</button>`;
    footer.querySelector("[data-close]").addEventListener("click", () => dialog.close());
    footer.querySelector("[data-start]").addEventListener("click", startSelected);
    if (!dialog.open) dialog.showModal();
  }

  function auditSelectionCopy(audit = {}) {
    if (audit.open_errors !== null && audit.open_errors !== undefined) {
      if (Number(audit.open_errors) === 0) {
        return ["partial", "truncated"].includes(audit.state)
          ? "No errors found yet"
          : "No open errors";
      }
      const suffix = ["partial", "truncated"].includes(audit.state) ? "+ errors found" : " open errors";
      return `${AUDIT_NUMBER.format(audit.open_errors)}${suffix}`;
    }
    if (audit.state === "failed") return "Last audit failed";
    if (["queued", "running"].includes(audit.state)) return "Audit in progress";
    return "Not audited";
  }

  function auditSelectionTone(audit = {}) {
    if (Number(audit.open_errors) > 0) return "has-errors";
    return stateClass(audit.state || "no_run");
  }

  function auditPresetIds(preset, dashboard) {
    const clients = dashboard.clients || [];
    if (preset === "never") return clients.filter((client) => client.tech_audit?.state === "no_run").map((client) => client.client_id);
    if (preset === "refresh") {
      return clients
        .filter((client) => ["stale", "failed", "partial", "truncated"].includes(client.tech_audit?.state))
        .map((client) => client.client_id);
    }
    return clients.map((client) => client.client_id);
  }

  function updateAuditSelectionCount() {
    const count = body.querySelectorAll('input[name="audit-client"]:checked').length;
    const copy = body.querySelector("[data-audit-count]");
    if (copy) copy.textContent = `${count} ${count === 1 ? "site" : "sites"} selected`;
  }

  function applyAuditPreset(preset, dashboard) {
    const selected = new Set(auditPresetIds(preset, dashboard).map(String));
    body.querySelectorAll('input[name="audit-client"]').forEach((input) => {
      input.checked = selected.has(input.value);
    });
    body.querySelectorAll("[data-audit-preset]").forEach((button) => {
      button.setAttribute("aria-pressed", String(button.dataset.auditPreset === preset));
    });
    updateAuditSelectionCount();
  }

  function openAuditSelection({ trigger } = {}) {
    opener = trigger || document.activeElement;
    if (active?.kind === "tech_audit" && currentJob && !isTerminal(currentJob.status)) {
      if (!dialog.open) dialog.showModal();
      renderProgress(currentJob);
      return;
    }
    const dashboard = getDashboard();
    if (!dashboard) return;
    resetProgressView();
    title.textContent = "Run technical audits";
    subtitle.textContent = "Our crawler audits one site at a time. Closing this window will not stop the batch.";
    body.innerHTML = `
      <section class="audit-preflight" aria-label="Technical audit selection">
        <div class="audit-preflight__signal" aria-hidden="true">${icon("bug")}</div>
        <div>
          <p class="eyebrow">Site crawl queue</p>
          <h3>Choose the sites to inspect</h3>
          <p>Every selected domain gets an independent crawl. A blocked or failed site will not stop the rest of the queue.</p>
        </div>
      </section>
      <div class="audit-presets" role="group" aria-label="Select audit clients">
        <button type="button" data-audit-preset="all" aria-pressed="true">All active</button>
        <button type="button" data-audit-preset="never" aria-pressed="false">Not audited</button>
        <button type="button" data-audit-preset="refresh" aria-pressed="false">Needs refresh</button>
        <strong data-audit-count></strong>
      </div>
      <fieldset class="selection-group audit-client-selection">
        <legend class="visually-hidden">Clients to audit</legend>
        <div class="audit-client-list">
          ${(dashboard.clients || []).map((client) => `
            <label class="audit-client-row">
              <input type="checkbox" name="audit-client" value="${escapeHtml(client.client_id)}" checked>
              <span class="audit-client-row__index" aria-hidden="true">${String((dashboard.clients || []).indexOf(client) + 1).padStart(2, "0")}</span>
              <span class="audit-client-row__identity"><strong>${escapeHtml(client.name)}</strong><small>${escapeHtml(client.domain)}</small></span>
              <span class="audit-client-row__current ${auditSelectionTone(client.tech_audit)}">${escapeHtml(auditSelectionCopy(client.tech_audit))}</span>
            </label>`).join("")}
        </div>
      </fieldset>
      <p class="audit-capacity-note">${icon("info-circle", "icon icon--tiny")} One site runs at a time because each crawl already scales its own request concurrency.</p>`;
    footer.innerHTML = `<button class="button button--quiet" type="button" data-close>Cancel</button><button class="button button--primary" type="button" data-start-audit>${icon("player-play")}Run audits</button>`;
    body.querySelectorAll("[data-audit-preset]").forEach((button) => button.addEventListener("click", () => applyAuditPreset(button.dataset.auditPreset, dashboard)));
    body.querySelectorAll('input[name="audit-client"]').forEach((input) => input.addEventListener("change", updateAuditSelectionCount));
    footer.querySelector("[data-close]").addEventListener("click", () => dialog.close());
    footer.querySelector("[data-start-audit]").addEventListener("click", startAuditSelected);
    updateAuditSelectionCount();
    if (!dialog.open) dialog.showModal();
  }

  async function checkBrowserAvailability(providers) {
    const toCheck = BROWSER_SESSION_PROVIDERS.filter((provider) => providers.includes(provider));
    const results = await Promise.all(
      toCheck.map(async (provider) => {
        try {
          const { running } = await getProviderSession(provider);
          return { provider, running };
        } catch {
          // Couldn't reach the session probe itself (e.g. API offline) — not what this
          // check is for, and `createSyncJob` right after surfaces a clearer error for
          // that case. Fail open rather than showing a misleading "no browser" screen.
          return { provider, running: true };
        }
      }),
    );
    // Only "Chrome process isn't up at all" blocks the start — a Chrome that's up but
    // logged out is `running: true` and is handled by the existing per-cell "Log in" flow
    // once the job is running (other clients/providers keep collecting meanwhile).
    return results.filter((result) => !result.running);
  }

  function renderBrowserRequired(offline, selection) {
    resetProgressView();
    title.textContent = "Update metrics";
    subtitle.textContent = "Collection has not started yet.";
    body.innerHTML = `
      <div class="state-panel state-error">
        ${icon("alert-circle")}
        <p><strong>No warm browser session found.</strong><br>${offline
          .map((entry) => escapeHtml((PROVIDERS[entry.provider] || { label: entry.provider }).label))
          .join(", ")} need${offline.length === 1 ? "s" : ""} a warm Chrome running on the host before collection can start.</p>
        <p>Run this on the machine with the warm Chrome, then retry:</p>
        <p><code>${escapeHtml(BROWSER_UP_COMMAND)}</code></p>
      </div>`;
    footer.innerHTML = `<button class="button button--quiet" type="button" data-close>Cancel</button><button class="button button--primary" type="button" data-retry-browser>${icon("refresh")}Retry</button>`;
    footer.querySelector("[data-close]").addEventListener("click", () => dialog.close());
    footer.querySelector("[data-retry-browser]").addEventListener("click", (event) => retryBrowserCheck(event, selection));
  }

  async function retryBrowserCheck(event, selection) {
    const button = event.currentTarget;
    button.disabled = true;
    try {
      const offline = await checkBrowserAvailability(selection.providers);
      if (offline.length) {
        renderBrowserRequired(offline, selection);
        return;
      }
      await beginCollection(selection);
    } finally {
      button.disabled = false;
    }
  }

  async function startSelected(event) {
    const clientIds = [...body.querySelectorAll('input[name="sync-client"]:checked')].map((input) => input.value);
    const providers = [...body.querySelectorAll('input[name="sync-provider"]:checked')].map((input) => input.value);
    // Read before the body is replaced by the import panel below — by the time the request is
    // built the checkbox no longer exists in the DOM.
    const forceRefresh = body.querySelector('input[name="sync-force"]')?.checked === true;
    if (!clientIds.length || !providers.length) {
      onToast("Select at least one client and one source.");
      return;
    }
    const selection = { clientIds, providers, forceRefresh };
    // `event.currentTarget` is only valid for the synchronous dispatch — the browser resets it to
    // null once this handler yields at the first `await`, so it must be captured up front.
    const button = event.currentTarget;
    button.disabled = true;
    try {
      const offline = await checkBrowserAvailability(providers);
      if (offline.length) {
        renderBrowserRequired(offline, selection);
        return;
      }
      await beginCollection(selection);
    } finally {
      button.disabled = false;
    }
  }

  async function startAuditSelected(event) {
    const clientIds = [...body.querySelectorAll('input[name="audit-client"]:checked')]
      .map((input) => input.value);
    if (!clientIds.length) {
      onToast("Select at least one site to audit.");
      body.querySelector('input[name="audit-client"]')?.focus();
      return;
    }
    const button = event.currentTarget;
    button.disabled = true;
    try {
      await beginAuditCollection(clientIds);
    } finally {
      button.disabled = false;
    }
  }

  async function beginAuditCollection(clientIds) {
    try {
      const dashboard = getDashboard();
      resetProgressView();
      title.textContent = "Preparing crawl queue";
      subtitle.textContent = "Validating selected clients and reserving the crawler lane.";
      body.innerHTML = `<div class="state-panel state-running">${icon("loader-2", "icon icon--spin")}<p><strong>Building technical audit batch</strong><br>${clientIds.length} ${clientIds.length === 1 ? "site" : "sites"} selected.</p></div>`;
      footer.innerHTML = `<button class="button button--quiet" type="button" disabled>Starting audits…</button>`;
      const accepted = await startTechAuditBatch({
        client_ids: clientIds,
        idempotency_key: crypto.randomUUID?.() || `${Date.now()}-${Math.random()}`,
      }, dashboard);
      active = {
        id: accepted.batch_id || accepted.job_id,
        eventsUrl: accepted.events_url,
        lastEventId: "",
        kind: "tech_audit",
      };
      saveActiveJob(active);
      if (accepted.skipped_active_client_ids?.length) {
        onToast(`${accepted.skipped_active_client_ids.length} active ${accepted.skipped_active_client_ids.length === 1 ? "audit was" : "audits were"} already running.`);
      }
      beginWatch();
      renderProgress({ status: "queued", progress_percent: 0, cells: [] });
    } catch (error) {
      openAuditSelection({ trigger: opener });
      onToast(`Could not start technical audits: ${error.message}`);
    }
  }

  // --- Rolling-window refresh preset (task-30) ------------------------------
  //
  // The routine sync collects CLOSED periods, so the in-progress week is never
  // requested and the day-level GSC/GA4 series the Portfolio Trends page reads
  // stops at the last closed period end. Only a `custom` window ending
  // yesterday can fill it — plus `force_refresh`, or the freshness gate skips
  // every cell that already has a recent snapshot and the run collects nothing
  // (`app/jobs/runner.py`, same reason `yovth-report backfill-daily-history`
  // hardcodes it).

  function isoDay(offsetDays) {
    // UTC on purpose: the worker builds its window in UTC too, so this cannot
    // drift a day against the server around midnight local time.
    return new Date(Date.now() - offsetDays * 86_400_000).toISOString().slice(0, 10);
  }

  function refreshWindow(windowDays) {
    // `end` is yesterday: today is still being written by both sources, and a
    // partial today would land as a real (wrong) low value for that day.
    return { start: isoDay(windowDays), end: isoDay(1), days: windowDays };
  }

  function openMetricsRefresh({ clientIds, providers = ["gsc", "reporting_ninja"], windowDays = 30, trigger } = {}) {
    opener = trigger || document.activeElement;
    // A live run is never replaced by a second one — show it instead.
    if (active?.id && currentJob && !isTerminal(currentJob.status)) {
      if (!dialog.open) dialog.showModal();
      renderProgress(currentJob);
      return;
    }
    resetProgressView();
    const window_ = refreshWindow(windowDays);
    const selection = { clientIds, providers, ...window_ };
    const providerLabels = providers.map((provider) => (PROVIDERS[provider] || { label: provider }).label).join(" and ");
    title.textContent = "Refresh trend data";
    subtitle.textContent = "Collection runs in the background. Closing this window won't stop it.";
    body.innerHTML = `
      <section class="refresh-preflight" aria-label="Refresh scope">
        <div class="refresh-preflight__signal" aria-hidden="true">${icon("cloud-download")}</div>
        <div>
          <p class="eyebrow">Day-level history</p>
          <h3>${escapeHtml(window_.start)} → ${escapeHtml(window_.end)}</h3>
          <p>${clientIds.length} ${clientIds.length === 1 ? "client" : "clients"} · ${escapeHtml(providerLabels)} · re-collects every day in the window.</p>
        </div>
      </section>
      <dl class="refresh-facts">
        <div><dt>Window</dt><dd>${window_.days} days, ending yesterday</dd></div>
        <div><dt>Mode</dt><dd>Re-collect (ignores the freshness skip)</dd></div>
        <div><dt>Source lag</dt><dd>GSC finalizes 3 days, GA4 2 — newest buckets stay provisional</dd></div>
      </dl>
      <p class="refresh-note">${icon("info-circle", "icon icon--tiny")} This is a metered live collection: each client costs about 2 Search Console queries and 12 GA4 queries, whatever the window length.</p>`;
    footer.innerHTML = `<button class="button button--quiet" type="button" data-close>Cancel</button><button class="button button--primary" type="button" data-start-refresh>${icon("refresh")}Refresh data</button>`;
    footer.querySelector("[data-close]").addEventListener("click", () => dialog.close());
    footer.querySelector("[data-start-refresh]").addEventListener("click", (event) => {
      event.currentTarget.disabled = true;
      beginRefresh(selection);
    });
    if (!dialog.open) dialog.showModal();
  }

  async function beginRefresh({ clientIds, providers, start, end }) {
    sessionOverlays.clear();
    resetProgressView();
    try {
      const dashboard = getDashboard();
      const accepted = await createSyncJob({
        client_ids: clientIds,
        providers,
        period_kind: "custom",
        comparison_mode: "closed",
        custom_start: start,
        custom_end: end,
        mode: "force_refresh",
        source_mode: "auto",
        idempotency_key: crypto.randomUUID?.() || `${Date.now()}-${Math.random()}`,
      }, dashboard);
      active = { id: accepted.batch_id || accepted.job_id, eventsUrl: accepted.events_url, lastEventId: "", kind: "metrics" };
      saveActiveJob(active);
      beginWatch();
      title.textContent = "Data collection";
      subtitle.textContent = `Collecting ${start} → ${end}. Closing this window won't stop it.`;
      renderProgress({ status: "queued", progress_percent: 0, cells: [] });
    } catch (error) {
      onToast(`Could not start the refresh: ${error.message}`);
      dialog.close();
    }
  }

  async function beginCollection({ clientIds, providers, forceRefresh }) {
    sessionOverlays.clear();
    resetProgressView();
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
        mode: forceRefresh ? "force_refresh" : "collect_missing",
        source_mode: "auto",
        idempotency_key: crypto.randomUUID?.() || `${Date.now()}-${Math.random()}`,
      }, dashboard);
      active = { id: accepted.batch_id || accepted.job_id, eventsUrl: accepted.events_url, lastEventId: "", kind: "metrics" };
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
        updateDock(job);
        if (dialog.open) renderProgress(job);
        await onFinished(job);
        if (dialog.open) renderProgress(job);
        active = null;
      },
      onError(error) { onToast(`Progress temporarily unavailable: ${error.message}`); },
    });
  }

  function renderOfflineNotice() {
    if (body.querySelector(".job-alert")) return;
    body.insertAdjacentHTML("afterbegin", `<div class="job-alert"><strong>Live connection interrupted.</strong> Status will resume automatically; the collection keeps running on the server.</div>`);
  }

  function isAuditJob(job) {
    const cells = job?.cells || [];
    return active?.kind === "tech_audit"
      || (cells.length > 0 && cells.every((cell) => cell.provider === "tech_audit"));
  }

  function auditResultDetail(cell, dashboard) {
    if (!TERMINAL_CELL_STATUSES.has(cell.status)) {
      return cell.error_code || STAGE_LABELS[cell.current_stage] || "Waiting for crawler";
    }
    if (["failed", "failed_interrupted"].includes(cell.status)) {
      return cell.error_code ? `Crawler stopped: ${cell.error_code.replaceAll("_", " ")}` : "Crawler stopped before a result was saved";
    }
    if (cell.status === "cancelled") return "Stopped by operator";
    const client = (dashboard?.clients || []).find((item) => String(item.client_id) === String(cell.client_id));
    const audit = client?.tech_audit;
    if (audit?.open_errors !== null && audit?.open_errors !== undefined) {
      const pages = audit.pages_crawled === null || audit.pages_crawled === undefined
        ? "pages unavailable"
        : `${AUDIT_NUMBER.format(audit.pages_crawled)} pages`;
      return `${AUDIT_NUMBER.format(audit.open_errors)} open ${audit.open_errors === 1 ? "error" : "errors"} · ${pages}`;
    }
    return cell.status === "partial" ? "Partial crawl saved" : "Audit saved";
  }

  function auditRunRow(cell, index, dashboard) {
    const client = (dashboard?.clients || []).find((item) => String(item.client_id) === String(cell.client_id));
    const displayState = cellDisplayState(cell);
    const meta = STATE_META[displayState] || STATE_META.running;
    const value = TERMINAL_CELL_STATUSES.has(cell.status) ? 100 : percent(cell.progress_percent);
    const spinning = cell.status === "running" ? " icon--spin" : "";
    return `
      <li class="audit-run-row ${stateClass(displayState)}" data-status="${escapeHtml(cell.status)}">
        <span class="audit-run-row__index" aria-hidden="true">${String(index + 1).padStart(2, "0")}</span>
        <span class="audit-run-row__identity"><strong>${escapeHtml(client?.name || cell.client_id)}</strong><small>${escapeHtml(client?.domain || "Client site")}</small></span>
        <span class="audit-run-row__stage"><strong>${icon(meta.icon, `icon${spinning}`)}${escapeHtml(meta.label)}</strong><small>${escapeHtml(auditResultDetail(cell, dashboard))}</small></span>
        <span class="audit-run-row__value">${value}%</span>
        <progress class="mini-progress" max="100" value="${value}" aria-label="${escapeHtml(client?.name || cell.client_id)}: ${value}%"></progress>
      </li>`;
  }

  function renderAuditProgress(job) {
    const dashboard = getDashboard();
    const cells = job.cells || [];
    const progressValue = percent(job.progress_percent);
    const done = cells.filter((cell) => TERMINAL_CELL_STATUSES.has(cell.status)).length;
    const failed = cells.filter((cell) => ["failed", "failed_interrupted"].includes(cell.status)).length;
    const terminal = isTerminal(job.status);
    title.textContent = terminal
      ? (failed ? "Audits finished with issues" : "Technical audits complete")
      : "Technical audits in progress";
    subtitle.textContent = terminal
      ? (failed ? `${failed} ${failed === 1 ? "site needs" : "sites need"} another attempt. Successful audit results are already saved.` : "Every selected site reached a final state.")
      : "The crawler is processing one site at a time; queued sites start automatically.";
    // Same no-flicker rule as the metrics matrix, one size smaller: the audit
    // list has no interactive children, so a string diff is enough to stop the
    // list from being rebuilt (and scrolled back to the top) on every tick.
    structureKey = null;
    const html = `
      <section class="audit-progress-hero" aria-label="Technical audit batch progress">
        <div class="audit-progress-hero__number"><strong>${progressValue}<small>%</small></strong><span>overall</span></div>
        <div class="audit-progress-hero__track">
          <div class="audit-progress-hero__labels"><strong>${done} / ${cells.length || "—"} sites complete</strong><span>${terminal ? "Batch finished" : "Crawler lane active"}</span></div>
          <progress class="progress-track" max="100" value="${progressValue}" aria-label="Overall technical audit progress: ${progressValue}%"></progress>
          <p>Closing this popup will not stop the audits. Progress restores automatically.</p>
        </div>
      </section>
      ${cells.length
        ? `<ol class="audit-run-list">${cells.map((cell, index) => auditRunRow(cell, index, dashboard)).join("")}</ol>`
        : `<div class="state-panel state-running">${icon("loader-2", "icon icon--spin")}<p><strong>Building the crawl queue</strong><br>Selected sites will appear here when the worker claims the batch.</p></div>`}`;
    if (html !== auditHtml) {
      auditHtml = html;
      body.innerHTML = html;
    }
    const nextFooter = footerActions(job);
    if (nextFooter !== footerHtml) {
      footerHtml = nextFooter;
      footer.innerHTML = nextFooter;
      footer.querySelector("[data-close]")?.addEventListener("click", () => dialog.close());
      footer.querySelector("[data-cancel]")?.addEventListener("click", () => cancelJob());
      footer.querySelector("[data-retry]")?.addEventListener("click", () => retryFailed());
    }
  }

  // --- Live progress v2 (task-30) -------------------------------------------
  //
  // This used to rebuild `body.innerHTML` on every SSE tick. With several cells
  // in flight that meant visible flicker, a matrix scrolled back to the top on
  // each update, and focus that had to be restored by hand afterwards.
  //
  // The render is split by what actually changes: a FULL build only when the
  // structure does (which clients/providers exist, audit vs metrics), and cheap
  // per-node patches for values (percent, state, stage text). Two regions that
  // carry their own interactive state — the session panel and the footer — are
  // diffed as strings and replaced only when their markup really changed, which
  // is what keeps the «Log in» button from being torn out from under a click.
  let structureKey = null;
  let sessionPanelHtml = null;
  let footerHtml = null;
  let auditHtml = null;
  let clockTimer = null;

  /** Anything that writes `body`/`footer` outside the progress renderer must
   * call this, or the next `renderProgress` would patch nodes that are no
   * longer in the DOM and silently show nothing. */
  function resetProgressView() {
    structureKey = null;
    sessionPanelHtml = null;
    footerHtml = null;
    auditHtml = null;
    stopClock();
  }

  function structureKeyOf(job, clientIds, providers) {
    return JSON.stringify(["metrics", clientIds, providers, job.cells?.length || 0, laneCount(job.cells || [])]);
  }

  // --- Browser lanes (task-31) ----------------------------------------------
  //
  // A provider may collect several clients at once — one per lane, where a lane
  // is physically one window of the single logged-in warm Chrome. The matrix
  // answers "how is each client doing"; this answers "what are the N browsers
  // doing right now", which is the question an operator watching a long run
  // actually has. Rows appear only when the run really used more than one lane
  // (`slot` is null for single-lane providers), so nothing changes for a run
  // that never parallelised.

  function laneCount(cells) {
    const slots = cells.map((cell) => cell.slot).filter((slot) => Number.isInteger(slot));
    return slots.length ? Math.max(...slots) + 1 : 0;
  }

  // What a lane is doing, or — when it is between clients — the last thing it
  // actually collected. `not_configured`/`skipped` cells pass through a lane in
  // milliseconds without doing any work, so they are never worth reporting as
  // "last": they would bury the result the operator is waiting to see.
  const LANE_REPORTABLE = new Set(["succeeded", "partial", "failed", "failed_interrupted", "cancelled"]);

  function laneRank(cell) {
    if (cell.status === "running") return 3;
    if (LANE_REPORTABLE.has(cell.status)) return 2;
    return 1;
  }

  function laneCells(cells) {
    const lanes = new Map();
    cells.forEach((cell) => {
      if (!Number.isInteger(cell.slot)) return;
      const current = lanes.get(cell.slot);
      if (!current) {
        lanes.set(cell.slot, cell);
        return;
      }
      const rank = laneRank(cell) - laneRank(current);
      if (rank > 0) lanes.set(cell.slot, cell);
      else if (rank === 0 && Date.parse(cell.finished_at || 0) > Date.parse(current.finished_at || 0)) {
        lanes.set(cell.slot, cell);
      }
    });
    return lanes;
  }

  function laneCopy(cell, clients) {
    if (!cell || (cell.status !== "running" && !LANE_REPORTABLE.has(cell.status))) {
      return { state: "idle", label: "Idle", detail: "Waiting for the next client" };
    }
    const name = clients.get(String(cell.client_id)) || cell.client_id;
    const provider = (PROVIDERS[cell.provider] || { label: cell.provider }).label;
    if (cell.status === "running") {
      return {
        state: "running",
        label: `${name} · ${provider}`,
        detail: cell.progress_message || STAGE_LABELS[cell.current_stage] || "Collecting…",
      };
    }
    const meta = STATE_META[cellDisplayState(cell)] || STATE_META.running;
    return { state: cellDisplayState(cell), label: "Idle", detail: `Last: ${name} · ${meta.label}` };
  }

  function laneRowMarkup(index) {
    return `
      <li class="lane-row" data-lane="${index}">
        <span class="lane-row__index" aria-hidden="true">${index + 1}</span>
        <span class="lane-row__body">
          <strong data-lane-label></strong>
          <small data-lane-detail></small>
        </span>
        <span class="lane-row__value" data-lane-value></span>
      </li>`;
  }

  function renderLanePanel(cells) {
    const lanes = laneCount(cells);
    if (lanes < 2) return "";
    return `
      <section class="lane-panel" aria-label="Browser lanes">
        <div class="lane-panel__heading">
          <span>Browser lanes</span>
          <span class="lane-panel__note">${lanes} windows · one shared login</span>
        </div>
        <ul class="lane-list">${Array.from({ length: lanes }, (_, index) => laneRowMarkup(index)).join("")}</ul>
      </section>`;
  }

  function patchLanePanel(cells, clients) {
    const lanes = laneCells(cells);
    body.querySelectorAll("[data-lane]").forEach((row) => {
      const index = Number(row.dataset.lane);
      const cell = lanes.get(index);
      const copy = laneCopy(cell, clients);
      row.className = `lane-row ${stateClass(copy.state)}`;
      const label = row.querySelector("[data-lane-label]");
      if (label.textContent !== copy.label) label.textContent = copy.label;
      const detail = row.querySelector("[data-lane-detail]");
      if (detail.textContent !== copy.detail) detail.textContent = copy.detail;
      const value = row.querySelector("[data-lane-value]");
      const text = cell && cell.status === "running" ? `${percent(cell.progress_percent)}%` : "—";
      if (value.textContent !== text) value.textContent = text;
    });
  }

  function cellKey(cell) {
    return `${cell.client_id}|${cell.provider}`;
  }

  function runStartedAt(cells) {
    const stamps = cells.map((cell) => Date.parse(cell.started_at)).filter((value) => Number.isFinite(value));
    return stamps.length ? Math.min(...stamps) : null;
  }

  function formatDuration(seconds) {
    if (seconds < 60) return `${Math.max(1, Math.round(seconds))}s`;
    const minutes = Math.round(seconds / 60);
    if (minutes < 60) return `${minutes} min`;
    return `${Math.floor(minutes / 60)}h ${String(minutes % 60).padStart(2, "0")}m`;
  }

  function formatClock(ms) {
    const total = Math.max(0, Math.round(ms / 1000));
    return `${String(Math.floor(total / 60)).padStart(2, "0")}:${String(total % 60).padStart(2, "0")}`;
  }

  // Determinate, measured, and honest about not knowing yet (AGENTS §9: never
  // animate a fake percentage). The sample deliberately ignores `skipped` /
  // `not_configured` cells: those finish in milliseconds and would drag the
  // median down until the estimate reads as minutes when it is really an hour.
  function etaCopy(cells) {
    const collected = cells.filter((cell) => ["succeeded", "partial", "failed", "failed_interrupted"].includes(cell.status));
    const remaining = cells.filter((cell) => !TERMINAL_CELL_STATUSES.has(cell.status)).length;
    if (!remaining) return "";
    const durations = collected
      .map((cell) => (Date.parse(cell.finished_at) - Date.parse(cell.started_at)) / 1000)
      .filter((value) => Number.isFinite(value) && value >= 0)
      .sort((a, b) => a - b);
    const running = cells.filter((cell) => cell.status === "running").length;
    const lanes = Math.max(1, running);
    if (durations.length < 3) return `${remaining} ${remaining === 1 ? "cell" : "cells"} left · estimating time…`;
    const median = durations[Math.floor(durations.length / 2)];
    return `${remaining} ${remaining === 1 ? "cell" : "cells"} left · about ${formatDuration((median * remaining) / lanes)} to go`;
  }

  function ensureClockTicking(getCells) {
    if (clockTimer) return;
    clockTimer = setInterval(() => {
      const node = body.querySelector("[data-run-clock]");
      const started = runStartedAt(getCells());
      if (!node || started === null) {
        clearInterval(clockTimer);
        clockTimer = null;
        return;
      }
      node.textContent = formatClock(Date.now() - started);
    }, 1000);
  }

  function stopClock() {
    if (!clockTimer) return;
    clearInterval(clockTimer);
    clockTimer = null;
  }

  function progressHeadings(job, cells) {
    const state = STATE_META[job.status] || STATE_META.running;
    // A lane parked on `awaiting_auth` still aggregates to a job-level
    // `queued` — truthful (it really is waiting), but «Queued» reads as "not
    // started yet" when the run is actually blocked on the operator.
    const awaitingAuth = cells.some((cell) => cell.status === "awaiting_auth");
    const subtitleText = isTerminal(job.status) && hasCollectionIssues(cells)
      ? "Collected data has been saved. Review cells with collection errors."
      : isTerminal(job.status) && hasSetupCells(cells)
        ? "Connected integrations collected successfully. Some clients still need a connection."
      : awaitingAuth
        ? "Collection paused: a service login is required."
        : state.label;
    return { titleText: terminalTitle(job), subtitleText };
  }

  function buildProgress(job, cells, clientIds, providers) {
    const dashboard = getDashboard();
    const clients = new Map((dashboard?.clients || []).map((client) => [String(client.client_id), client.name]));
    sessionPanelHtml = renderSessionStagePanel(job, () => { if (dialog.open) renderProgress(currentJob); });
    body.innerHTML = `
      <div data-session-region>${sessionPanelHtml}</div>
      ${renderLanePanel(cells)}
      <div class="progress-overview">
        <span class="progress-number" data-progress-number>0%</span>
        <div>
          <progress class="progress-track" data-progress-track max="100" value="0" aria-label="Overall progress: 0%"></progress>
          <div class="progress-copy" data-progress-copy></div>
          <p class="progress-eta"><span data-progress-eta></span><span class="progress-eta__clock" data-run-clock-wrap hidden> · running <span data-run-clock>00:00</span></span></p>
        </div>
      </div>
      ${clientIds.length
        ? `<div class="matrix-wrap"><table class="progress-matrix"><thead><tr><th scope="col">Client</th>${providers.map((provider) => `<th scope="col">${PROVIDERS[provider].label}</th>`).join("")}</tr></thead><tbody>${clientIds.map((clientId) => `<tr><td>${escapeHtml(clients.get(clientId) || clientId)}</td>${providers.map((provider) => matrixCell(cells.find((cell) => String(cell.client_id) === clientId && cell.provider === provider), clientId, provider)).join("")}</tr>`).join("")}</tbody></table></div>`
        : `<div class="state-panel"><p><strong>Building the queue.</strong><br>The table will appear once the server responds.</p></div>`}`;
    bindSessionButtons();
  }

  function bindSessionButtons() {
    body.querySelectorAll("[data-session-action]").forEach((button) => {
      button.addEventListener("click", () => {
        if (button.getAttribute("aria-disabled") === "true") return;
        handleLoginClick(button.dataset.sessionAction);
      });
    });
  }

  function patchSessionPanel(job) {
    const region = body.querySelector("[data-session-region]");
    if (!region) return;
    const html = renderSessionStagePanel(job, () => { if (dialog.open) renderProgress(currentJob); });
    if (html === sessionPanelHtml) return;
    const focused = region.contains(document.activeElement) ? document.activeElement.dataset.sessionAction : null;
    sessionPanelHtml = html;
    region.innerHTML = html;
    bindSessionButtons();
    if (focused) region.querySelector(`[data-session-action="${focused}"]`)?.focus();
  }

  function patchCell(cell) {
    const node = body.querySelector(`[data-cell="${CSS.escape(cellKey(cell))}"]`);
    if (!node) return;
    const displayState = cellDisplayState(cell);
    const meta = STATE_META[displayState] || STATE_META.running;
    const value = TERMINAL_CELL_STATUSES.has(cell.status) ? 100 : percent(cell.progress_percent);
    node.className = `matrix-cell ${stateClass(displayState)}`;
    const label = node.querySelector("[data-cell-label]");
    const nextLabel = `${icon(meta.icon, cell.status === "running" ? "icon icon--spin" : "icon")}${meta.label}`;
    if (label.dataset.render !== nextLabel) {
      label.innerHTML = nextLabel;
      label.dataset.render = nextLabel;
    }
    const detail = node.querySelector("[data-cell-detail]");
    const nextDetail = cellDetail(cell, displayState);
    if (detail.textContent !== nextDetail) detail.textContent = nextDetail;
    const bar = node.querySelector("[data-cell-progress]");
    if (Number(bar.value) !== value) {
      bar.value = value;
      bar.setAttribute("aria-label", `${meta.label}: ${value}%`);
    }
  }

  function renderProgress(job) {
    if (isAuditJob(job)) {
      renderAuditProgress(job);
      return;
    }
    const cells = job.cells || [];
    const dashboard = getDashboard();
    const clients = new Map((dashboard?.clients || []).map((client) => [String(client.client_id), client.name]));
    const clientIds = [...new Set(cells.map((cell) => String(cell.client_id)))];
    const providers = PROVIDER_ORDER.filter((provider) => cells.some((cell) => cell.provider === provider));
    const { titleText, subtitleText } = progressHeadings(job, cells);
    if (title.textContent !== titleText) title.textContent = titleText;
    if (subtitle.textContent !== subtitleText) subtitle.textContent = subtitleText;

    const nextKey = structureKeyOf(job, clientIds, providers);
    if (nextKey !== structureKey) {
      structureKey = nextKey;
      buildProgress(job, cells, clientIds, providers);
    } else {
      patchSessionPanel(job);
    }

    const progressValue = percent(job.progress_percent);
    const number = body.querySelector("[data-progress-number]");
    if (number) number.textContent = `${progressValue}%`;
    const track = body.querySelector("[data-progress-track]");
    if (track && Number(track.value) !== progressValue) {
      track.value = progressValue;
      track.setAttribute("aria-label", `Overall progress: ${progressValue}%`);
    }
    patchLanePanel(cells, clients);
    const copy = body.querySelector("[data-progress-copy]");
    if (copy) copy.textContent = progressSummary(cells);
    const eta = body.querySelector("[data-progress-eta]");
    if (eta) eta.textContent = etaCopy(cells);
    const clockWrap = body.querySelector("[data-run-clock-wrap]");
    const started = runStartedAt(cells);
    if (clockWrap) {
      const live = !isTerminal(job.status) && started !== null;
      clockWrap.hidden = !live;
      if (live) {
        body.querySelector("[data-run-clock]").textContent = formatClock(Date.now() - started);
        ensureClockTicking(() => currentJob?.cells || []);
      } else {
        stopClock();
      }
    }
    cells.forEach(patchCell);

    const nextFooter = footerActions(job);
    if (nextFooter !== footerHtml) {
      footerHtml = nextFooter;
      footer.innerHTML = nextFooter;
      footer.querySelector("[data-close]")?.addEventListener("click", () => dialog.close());
      footer.querySelector("[data-cancel]")?.addEventListener("click", () => cancelJob());
      footer.querySelector("[data-retry]")?.addEventListener("click", () => retryFailed());
    }
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

  // `data-cell` is the patch anchor for live progress v2 — every value inside
  // is addressed by its own `data-cell-*` hook so an update never has to
  // re-create the node (see `patchCell`).
  function matrixCell(cell, clientId, provider) {
    if (!cell) return `<td><span class="matrix-cell state-not_configured"><strong>— Not selected</strong></span></td>`;
    const displayState = cellDisplayState(cell);
    const meta = STATE_META[displayState] || STATE_META.running;
    const value = TERMINAL_CELL_STATUSES.has(cell.status) ? 100 : percent(cell.progress_percent);
    const iconMarkup = icon(meta.icon, cell.status === "running" ? "icon icon--spin" : "icon");
    const key = escapeHtml(`${clientId ?? cell.client_id}|${provider ?? cell.provider}`);
    return `<td><div class="matrix-cell ${stateClass(displayState)}" data-cell="${key}"><strong data-cell-label>${iconMarkup}${meta.label}</strong><small data-cell-detail>${escapeHtml(cellDetail(cell, displayState))}</small><progress class="mini-progress" data-cell-progress max="100" value="${value}" aria-label="${meta.label}: ${value}%"></progress></div></td>`;
  }

  function progressSummary(cells) {
    const done = cells.filter((cell) => ["succeeded", "partial", "failed", "failed_interrupted", "not_configured", "cancelled", "skipped"].includes(cell.status)).length;
    return `Completed ${done} of ${cells.length} cells. Closing this window will not stop the collection.`;
  }

  function hasCollectionIssues(cells) {
    return cells.some((cell) => ["partial", "failed", "failed_interrupted"].includes(cell.status));
  }

  function hasSetupCells(cells) {
    return cells.some((cell) => cell.status === "not_configured");
  }

  function terminalTitle(job) {
    if (!isTerminal(job.status)) return "Data collection";
    if (hasCollectionIssues(job.cells || [])) return "Collection finished with issues";
    if (hasSetupCells(job.cells || [])) return "Collection finished — setup needed";
    return "Collection finished";
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
      const sourceId = currentJob.batch_id || currentJob.job_id || currentJob.id || active?.id;
      const retryKind = isAuditJob(currentJob) ? "tech_audit" : "metrics";
      const accepted = await retrySyncJob(sourceId);
      const retryId = accepted.batch_id || accepted.job_id || accepted.id || sourceId;
      active = {
        id: retryId,
        eventsUrl: accepted.events_url || `/api/v1/sync-jobs/${retryId}/events`,
        lastEventId: "",
        kind: retryKind,
      };
      saveActiveJob(active);
      beginWatch();
    } catch (error) { onToast(`Could not start the retry: ${error.message}`); }
  }

  function updateDock(job) {
    const terminal = isTerminal(job.status);
    const audit = isAuditJob(job);
    dock.hidden = false;
    dockTitle.textContent = terminal
      ? (audit ? (hasCollectionIssues(job.cells || []) ? "Audits finished with issues" : "Technical audits complete") : terminalTitle(job))
      : (audit ? "Technical audits running" : "Update in progress");
    dockCopy.textContent = terminal ? "View results" : "View progress";
    dockPercent.textContent = `${percent(job.progress_percent)}%`;
  }

  if (active?.id) beginWatch();
  return { openSelection, openAuditSelection, openMetricsRefresh, hasActiveJob: () => Boolean(active?.id) };
}
