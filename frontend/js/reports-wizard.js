// Reports — launch wizard (task-43 §3). A 3-step `<dialog>` (Type ->
// Parameters -> Source) bound once to `#reports-wizard-dialog`, the same
// "single shared dialog, opened with context" pattern as
// `client-status.js`/`monitor-diff.js`. `reports-page.js` opens it via
// `openReportsWizard({ clients, trigger, onCreated })`.
//
// The step-2 period preview and the fixture engine both need the exact same
// date math the real engine uses (`app/reports/periods.py`, task-42 §2.4) —
// there is no dry-run endpoint, so `computeReportPeriods` in api.js is the
// one place that logic lives; this file only calls it.

import {
  computeReportPeriods, createReportBatch, createReportRun, getNinjaBindings, getNinjaReportName,
  getReportTypes, isAbortError, putNinjaReportName,
} from "./api.js?v=20260907-rn2";
import { isPaused } from "./client-status.js?v=20260821-spin3";
import { buildClientHash } from "./router.js?v=20260821-spin3";
import {
  REPORT_KIND_META, escapeHtml, formatDateRange, formatDurationSeconds, icon, statusIcon,
} from "./formatters.js?v=20260821-spin3";

const dialog = document.querySelector("#reports-wizard-dialog");
const stepsNav = document.querySelector("#reports-wizard-steps");
const body = document.querySelector("#reports-wizard-body");
const reasonLine = document.querySelector("#reports-wizard-reason");
const actions = document.querySelector("#reports-wizard-actions");
const subtitle = document.querySelector("#reports-wizard-subtitle");

const MONTH_NAMES = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December",
];
const KIND_ICONS = { monthly: "calendar-month", yearly: "calendar-stats", custom_months: "calendar-event", comparator: "columns-3" };

let context = null; // { clients, trigger, onCreated }
let state = null;
let types = null;
let submitting = false;

function pad2(n) { return String(n).padStart(2, "0"); }
function todayYm() { const t = new Date(); return [t.getFullYear(), t.getMonth() + 1]; }

function defaultState() {
  const [year, month] = todayYm();
  return {
    step: 1, scope: null, selectedClientIds: [], clientQuery: "",
    kind: null, clientId: "",
    years: [year, year - 1], span: "full",
    months: [], gridYear: year,
    comparatorMonth: month, comparatorYears: [year, year - 1],
    source: null, forceRefresh: false,
    ninjaState: "unknown", ninjaName: null,
    // task-45 §2.2: the account's reports arrive split into this client's
    // (`matches`) and everyone else's (`others`). Picking from `others` needs
    // `ninjaConfirmed` — binding a foreign report is permanent and makes every
    // later PDF run report another client's numbers under this client's name.
    ninjaMatches: [], ninjaOthers: [], ninjaSelectedCandidate: "", ninjaConfirmed: false,
    ninjaLookupError: null, ninjaSnapshotAgeSeconds: null, ninjaRefreshing: false,
    error: null,
    // task-49 §2.1: `null` until `getNinjaBindings()` resolves — the
    // checklist shows a short loading line rather than treating "not loaded
    // yet" as "not connected". Keyed by `client_id` once loaded.
    bindings: null,
    // task-49 §2.2, moved to the Source step: a client dropped because its
    // Reporting Ninja binding is not usable stays VISIBLE with an `Excluded`
    // mark instead of disappearing — "12 of 14" must never turn into a guess
    // about who is missing. Only the readiness table writes to this; a plain
    // unselect in the Scope step is not a "drop", it is just a selection.
    readinessDropped: new Set(),
    // task-49 §2.2: per-client inline discovery state for the readiness
    // table (a client can be reconnected without leaving the wizard). Keyed
    // by `client_id`, shape mirrors the single-client `ninja*` fields above:
    // `{ open, status: "idle"|"checking"|"known"|"error", name, matches,
    // others, selectedCandidate, confirmed, message }`.
    inlineConnect: {},
  };
}

function bindingFor(clientId) {
  return (state.bindings && state.bindings[clientId]) || {
    connected: false, ninja_report_name: null, state: "not_connected",
  };
}

function ninjaCandidateIsForeign(name) {
  return Boolean(name) && !state.ninjaMatches.some((candidate) => candidate.name === name);
}

function ninjaSaveBlocked() {
  if (!state.ninjaSelectedCandidate) return true;
  return ninjaCandidateIsForeign(state.ninjaSelectedCandidate) && !state.ninjaConfirmed;
}

function selectedClient() {
  return context.clients.find((client) => client.client_id === state.clientId) || null;
}

function clientNinjaConfigured(client) {
  const rn = client?.providers?.find((item) => item.provider === "reporting_ninja");
  return Boolean(rn && rn.state !== "not_configured");
}

// --- Scope step: "All clients" picker (task-47 §3.1) ----------------------
//
// Picking clients is all this step does. It deliberately says NOTHING about
// Reporting Ninja: the source is chosen two steps later, and two of the three
// sources (`api`, `google_api`) do not need a bound Ninja report at all —
// `_run_api` needs only the integration's `connection_key`, `_run_google`
// needs neither (it reads GA4/GSC directly). Gating the client list on a
// binding therefore warned about, and withheld, clients that were perfectly
// fine for the source actually about to run. The binding check still exists
// — see `renderBatchReadiness` in the Source step, where the source is known.

function sortedClients() {
  return [...context.clients].sort((a, b) => a.name.localeCompare(b.name));
}

function filteredScopeClients() {
  const query = state.clientQuery.trim().toLowerCase();
  const all = sortedClients();
  if (!query) return all;
  return all.filter((client) => client.name.toLowerCase().includes(query) || (client.domain || "").toLowerCase().includes(query));
}

function isSelected(clientId) {
  return state.selectedClientIds.includes(clientId);
}

function setSelected(clientId, selected) {
  state.selectedClientIds = selected
    ? [...state.selectedClientIds.filter((id) => id !== clientId), clientId]
    : state.selectedClientIds.filter((id) => id !== clientId);
}

function toggleScopeClient(clientId) {
  const selected = !isSelected(clientId);
  setSelected(clientId, selected);
  // Re-selecting in the Scope step also un-drops the client: the readiness
  // table must not keep showing it as `Excluded` once it is back in.
  if (selected) state.readinessDropped.delete(clientId);
  rerenderScopeStep();
}

// Unchecking a client in the readiness table IS the old `Exclude` button —
// but the row stays on screen, marked `Excluded`, because a client that
// disappears the moment it is dropped turns "12 of 14" into a guess about
// who is missing (task-49 §2.2).
function toggleReadinessClient(clientId) {
  const selected = !isSelected(clientId);
  setSelected(clientId, selected);
  if (selected) state.readinessDropped.delete(clientId);
  else state.readinessDropped.add(clientId);
  rerenderSourceStep(`[data-readiness-client="${cssEscape(clientId)}"]`);
}

function selectAllScopeClients() {
  state.selectedClientIds = context.clients.map((client) => client.client_id);
  state.readinessDropped.clear();
  rerenderScopeStep();
}

function clearScopeClients() {
  state.selectedClientIds = [];
  rerenderScopeStep();
}

// Every in-step action (tick a client, pick a candidate, save) re-renders the
// WHOLE step body, same as every other render path in this file — but unlike
// those, these fire from deep inside a long, scrolled table, and resetting
// scroll to the top on each click reads as the interaction fighting the
// operator. Both scroll positions that matter — the table's own internal
// scroll and the dialog body around it — are captured and restored around the
// re-render, so nothing visibly jumps.
function rerenderPreservingScroll(renderBody, focusSelector) {
  const tableScroll = body.querySelector(".reports-bindings-table-scroll");
  const savedTableScroll = tableScroll ? { top: tableScroll.scrollTop, left: tableScroll.scrollLeft } : null;
  const savedBodyScrollTop = body.scrollTop;
  body.innerHTML = renderBody();
  renderFooter();
  const restoredTableScroll = body.querySelector(".reports-bindings-table-scroll");
  if (restoredTableScroll && savedTableScroll) {
    restoredTableScroll.scrollTop = savedTableScroll.top;
    restoredTableScroll.scrollLeft = savedTableScroll.left;
  }
  body.scrollTop = savedBodyScrollTop;
  if (focusSelector) body.querySelector(focusSelector)?.focus();
}

function rerenderScopeStep() {
  rerenderPreservingScroll(renderStep1);
}

// Default selection is every client that isn't paused (task-47 §3.1). A
// paused client stays visible and addable, just unchecked by default,
// matching task-34's own "paused stays visible, drops out of mass operations
// unless named explicitly" rule. Reporting Ninja readiness deliberately does
// NOT narrow this — see the Scope-step note above.
function defaultSelectedClientIds() {
  return context.clients.filter((client) => !isPaused(client)).map((client) => client.client_id);
}

function selectScope(scope) {
  state.scope = scope;
  if (scope === "all" && state.selectedClientIds.length === 0) {
    state.selectedClientIds = defaultSelectedClientIds();
  }
  renderStep();
}

// `focusClientId`/`focusKind` (task-49 §2.3): the batch matrix's `Connect
// Reporting Ninja` action (`reports-batch.js`) jumps straight to this
// client's step-4 discovery block instead of making the operator re-pick
// scope/type/parameters just to fix a binding — reuses the exact single-
// client mechanic task-45 built, only entered at a different step.
export function openReportsWizard({ clients, trigger, onCreated, onBatchCreated, focusClientId, focusKind }) {
  context = { clients, trigger, onCreated, onBatchCreated };
  state = defaultState();
  if (focusClientId) {
    state.scope = "one";
    state.clientId = focusClientId;
    state.kind = focusKind || "monthly";
    state.source = "pdf";
    state.step = 4;
  }
  types = null;
  submitting = false;
  subtitle.textContent = focusClientId
    ? "Connect this client's Reporting Ninja report, then generate."
    : "Choose a report type, its parameters, then how to collect it.";
  render();
  dialog.showModal();
  getReportTypes().then((result) => { types = result; if (state.step === 3) renderStep(); }).catch(() => {});
  // task-49 §2.1: fetched once per wizard open, alongside `types` — cheap
  // (no browser call, see `getNinjaBindings`) and read by the Source step's
  // readiness table, which is the only place a binding can change the answer.
  getNinjaBindings({ requestKey: "reports-wizard-ninja-bindings" }).then((result) => {
    if (!state) return;
    state.bindings = Object.fromEntries(result.rows.map((row) => [row.client_id, row]));
    if (state.step === 4) renderStep();
  }).catch(() => {
    if (state) state.bindings = {};
  });
  if (focusClientId) void checkNinjaReport();
}

dialog?.addEventListener("close", () => {
  context?.trigger?.focus?.();
  context = null;
  state = null;
});

// --- Validation ------------------------------------------------------------

function currentPeriods() {
  if (!state.kind) return [];
  const params = state.kind === "yearly" ? { years: state.years, span: state.span }
    : state.kind === "custom_months" ? { months: state.months }
      : state.kind === "comparator" ? { months: state.comparatorYears.map((y) => `${y}-${pad2(state.comparatorMonth)}`) }
        : {};
  return computeReportPeriods(state.kind, params, new Date());
}

function step1Reason() {
  if (!state.scope) return "Choose who this report is for to continue.";
  if (state.scope === "all" && state.selectedClientIds.length === 0) return "Select at least one client.";
  return "";
}

function step3Reason() {
  if (state.scope === "one") {
    if (!state.clientId) return "Choose a client to continue.";
    const client = selectedClient();
    if (!clientNinjaConfigured(client)) return "Reporting Ninja isn't connected for this client — connect it before generating a report.";
  }
  if (state.kind === "yearly" && (state.years.length < 2 || state.years.length > 4)) return "Choose 2 to 4 years.";
  if (state.kind === "custom_months" && (state.months.length < 1 || state.months.length > 24)) return "Choose at least 1 month (up to 24).";
  if (state.kind === "comparator" && (state.comparatorYears.length < 2 || state.comparatorYears.length > 4)) return "Choose 2 to 4 years for the selected month.";
  return "";
}

function step4Reason() {
  if (!state.source) return "Choose a collection source to continue.";
  // task-84 §1: a comparator column is `PDF | API | Diff %` — it compares the
  // two Reporting Ninja transports, so the direct source has no place in it.
  // Blocked here AND in the runner, never silently sent to a failing run.
  if (state.source === "google_api" && state.kind === "comparator") {
    return "The Google API source can't build a comparator — that report compares the PDF and API transports. Pick API or PDF, or a different report type.";
  }
  // The per-client Reporting Ninja discovery flow (Find/save a report name)
  // only makes sense for one client — a batch's children are validated by
  // `renderBatchReadiness` below, non-blocking, and again at run time.
  if (state.source === "pdf" && state.scope === "one") {
    if (state.ninjaState === "checking") return "Checking the saved Reporting Ninja report name…";
    if (state.ninjaState !== "known" || !state.ninjaName) return "Find and save the Reporting Ninja report name for this client first.";
  }
  return "";
}

function reasonForCurrentStep() {
  if (state.step === 1) return step1Reason();
  if (state.step === 2) return state.kind ? "" : "Choose a report type to continue.";
  if (state.step === 3) return step3Reason();
  return step4Reason();
}

// --- What each source actually needs from Reporting Ninja ------------------
//
// Measured in the runner, not assumed — this is the whole point of moving the
// check here, and the three answers are genuinely different:
//
// - `pdf`   — `_run_pdf` raises `ReportNameMissingError` without a saved
//             `ninja_report_name`, so every non-`ready` state is a real
//             failure ahead (`not_connected`, `no_report_name`,
//             `foreign_binding`).
// - `api`   — `_run_api` needs only `config_json["connection_key"]`; the
//             report NAME is never read. Only `not_connected` matters.
// - `google_api` — `_run_google` never touches Reporting Ninja. Nothing here
//             can predict its outcome, so nothing is claimed.
function ninjaGateStates(source) {
  if (source === "pdf") return ["not_connected", "no_report_name", "foreign_binding"];
  if (source === "api") return ["not_connected"];
  return [];
}

// task-49 §2.2: an at-risk client stays selectable on purpose (the operator's
// own informed choice, not a form bug) — but the Start button still has to say
// exactly what that choice costs. Non-blocking: `renderFooter` never disables
// Start for this. The dropped ones stay in the list so the table can keep
// showing them as `Excluded` rather than silently losing the row.
function readinessClients() {
  const gate = ninjaGateStates(state.source);
  if (state.scope !== "all" || !gate.length || !state.bindings) return [];
  const ids = new Set([...state.selectedClientIds, ...state.readinessDropped]);
  return context.clients
    .filter((client) => ids.has(client.client_id) && gate.includes(bindingFor(client.client_id).state))
    .sort((a, b) => a.name.localeCompare(b.name));
}

function step4Notice() {
  const atRisk = readinessClients().filter((client) => isSelected(client.client_id)).length;
  if (!atRisk) return "";
  const total = state.selectedClientIds.length;
  return `${atRisk} of ${total} client${total === 1 ? "" : "s"} will fail — connect ${atRisk === 1 ? "it" : "them"} or untick ${atRisk === 1 ? "it" : "them"} below.`;
}

// --- Rendering: chrome -------------------------------------------------

function render() {
  renderSteps();
  renderStep();
}

function renderSteps() {
  const labels = ["Scope", "Type", "Parameters", "Source"];
  stepsNav.innerHTML = labels.map((label, index) => {
    const stepNumber = index + 1;
    const state_ = stepNumber < state.step ? "done" : stepNumber === state.step ? "active" : "pending";
    return `
      <span class="reports-wizard-step" data-state="${state_}">
        <span class="reports-wizard-step__index">${state_ === "done" ? icon("check", "icon icon--tiny") : stepNumber}</span>
        ${escapeHtml(label)}
      </span>`;
  }).join("");
}

function renderStep() {
  if (state.step === 1) body.innerHTML = renderStep1();
  else if (state.step === 2) body.innerHTML = renderStep2();
  else if (state.step === 3) body.innerHTML = renderStep3();
  else body.innerHTML = renderStep4();
  renderFooter();
  const firstFocusable = body.querySelector("[data-autofocus]") || body.querySelector("button,select,input");
  firstFocusable?.focus();
}

function renderFooter() {
  const reason = submitting ? "" : reasonForCurrentStep();
  const notice = !reason && state.step === 4 && !submitting ? step4Notice() : "";
  reasonLine.textContent = reason || notice;
  reasonLine.classList.toggle("reports-wizard-reason--warning", !reason && Boolean(notice));
  const backButton = state.step > 1 ? `<button type="button" class="button button--quiet" id="rw-back">${icon("chevron-left")}Back</button>` : "";
  const primaryLabel = state.step < 4 ? "Next" : (submitting ? "Starting…" : startButtonLabel());
  const primaryDisabled = submitting || Boolean(reason);
  actions.innerHTML = `
    <button type="button" class="button button--quiet" id="rw-cancel">Cancel</button>
    ${backButton}
    <button type="button" class="button button--primary" id="rw-primary" ${primaryDisabled ? "disabled" : ""}>
      ${state.step === 4 && submitting ? statusIcon("loader-2") : ""}${primaryLabel}${state.step < 4 ? icon("chevron-right") : ""}
    </button>`;
  document.querySelector("#rw-cancel").addEventListener("click", () => dialog.close());
  document.querySelector("#rw-back")?.addEventListener("click", () => { state.step -= 1; render(); });
  document.querySelector("#rw-primary").addEventListener("click", onPrimaryAction);
}

// The button says exactly what will happen (frontend/AGENTS.md §8 "Action
// copy says what will happen") — a batch names the count, a single run
// names the type, never a generic "Start" once enough is known to be specific.
function startButtonLabel() {
  if (submitting) return "Starting…";
  const kindLabel = REPORT_KIND_META[state.kind]?.label || "report";
  const sourceLabel = state.source === "pdf" ? "Ninja PDFs"
    : state.source === "google_api" ? "Google API" : "API";
  if (state.scope === "all") {
    const n = state.selectedClientIds.length;
    return `Generate ${n} ${kindLabel.toLowerCase()} report${n === 1 ? "" : "s"} (${sourceLabel})`;
  }
  return "Start";
}

async function onPrimaryAction() {
  if (state.step < 4) { state.step += 1; render(); return; }
  await submit();
}

// --- Step 1: scope -------------------------------------------------------

function renderStep1() {
  const showChecklist = state.scope === "all";
  return `
    <div class="reports-scope-grid" role="group" aria-label="Report scope">
      <button type="button" class="reports-scope-card" data-scope="one" aria-pressed="${state.scope === "one"}" data-autofocus>
        ${icon("user")}
        <strong>One client</strong>
        <span>Pick a client, then the type and parameters.</span>
      </button>
      <button type="button" class="reports-scope-card" data-scope="all" aria-pressed="${state.scope === "all"}">
        ${icon("users")}
        <strong>All clients</strong>
        <span>Generate the same report for a whole selection at once.</span>
      </button>
    </div>
    ${showChecklist ? renderScopeChecklist() : ""}`;
}

// task-49 §2.2: labels/icons for the four binding states — named after the
// exact `NinjaBindingState` values so the class is always `state-${value}`,
// the same one-class-per-literal-value convention `.state-cached`/
// `.state-mismatch` already use elsewhere in this stylesheet.
const BINDING_STATE_META = {
  ready: { label: "Ready", icon: "circle-check" },
  no_report_name: { label: "No report name", icon: "alert-triangle" },
  not_connected: { label: "Not connected", icon: "plug-off" },
  foreign_binding: { label: "Foreign binding", icon: "alert-triangle" },
};

function renderScopeChecklist() {
  const clients = filteredScopeClients();
  const selected = new Set(state.selectedClientIds);
  const total = context.clients.length;

  return `
    <div class="reports-step-section reports-scope-checklist">
      <div class="reports-scope-toolbar">
        <label class="search-field">
          <span class="visually-hidden">Search clients</span>${icon("search")}
          <input type="search" id="rw-client-search" placeholder="Search clients…" value="${escapeHtml(state.clientQuery)}">
        </label>
        <button type="button" class="button button--quiet" id="rw-select-all">Select all</button>
        <button type="button" class="button button--quiet" id="rw-clear-clients">Clear</button>
        <span class="reports-scope-count">${state.selectedClientIds.length} of ${total} selected</span>
      </div>
      <div class="reports-bindings-table-scroll"><table class="reports-bindings-table reports-bindings-table--picker">
        <thead>
          <tr>
            <th class="visually-hidden">Select</th>
            <th>Client</th>
            <th>Domain</th>
          </tr>
        </thead>
        <tbody>
          ${clients.map((client) => renderScopeClientRow(client, selected)).join("")
            || `<tr><td colspan="3"><p class="field-help">No client matches "${escapeHtml(state.clientQuery)}".</p></td></tr>`}
        </tbody>
      </table></div>
    </div>`;
}

function renderScopeClientRow(client, selected) {
  return `
    <tr class="reports-binding-row">
      <td>
        <input type="checkbox" data-scope-client="${escapeHtml(client.client_id)}"
          ${selected.has(client.client_id) ? "checked" : ""}
          aria-label="Select ${escapeHtml(client.name)}">
      </td>
      <td>
        <span class="reports-binding-row__name" translate="no">${escapeHtml(client.name)}</span>
        ${isPaused(client) ? `<span class="status-label state-paused">${icon("player-pause", "icon icon--tiny")}Paused</span>` : ""}
      </td>
      <td class="reports-binding-row__report" translate="no">${client.domain ? escapeHtml(client.domain) : "—"}</td>
    </tr>`;
}

// --- Source step: Reporting Ninja readiness (task-49 §2.2, moved here) -----
//
// The same table task-49 built, with the one thing it was missing: the chosen
// source. `ninjaGateStates` decides whether it appears at all, so a Google API
// batch never sees a Reporting Ninja warning — there is nothing for Reporting
// Ninja to be wrong about on that path.
function renderBatchReadiness() {
  if (state.scope !== "all" || !ninjaGateStates(state.source).length) return "";
  if (state.bindings === null) {
    return `<p class="field-help">${statusIcon("loader-2")}Checking Reporting Ninja bindings…</p>`;
  }
  const clients = readinessClients();
  const selected = new Set(state.selectedClientIds);
  if (!clients.length) {
    const n = state.selectedClientIds.length;
    return `<p class="reports-client-hint">${icon("circle-check")}
      All ${n} selected client${n === 1 ? "" : "s"} ${n === 1 ? "is" : "are"} ready for this source.</p>`;
  }
  const explain = state.source === "pdf"
    ? "This source downloads each client's own report from the Reporting Ninja cabinet, so it needs a saved report name. A client below fails on its own — the rest of the batch keeps going."
    : "This source reads Reporting Ninja's API. A client below has no Reporting Ninja connection at all, so it fails on its own — the rest of the batch keeps going.";
  return `
    <div class="reports-step-section reports-scope-checklist">
      <h3>Reporting Ninja readiness</h3>
      <p class="field-help">${escapeHtml(explain)} Connect it, or untick it to leave it out of this batch.</p>
      <div class="reports-bindings-table-scroll"><table class="reports-bindings-table">
        <thead>
          <tr>
            <th class="visually-hidden">Keep in batch</th>
            <th>Client</th>
            <th>Reporting Ninja report</th>
            <th>State</th>
            <th class="visually-hidden">Action</th>
          </tr>
        </thead>
        <tbody>${clients.map((client) => renderReadinessRow(client, selected)).join("")}</tbody>
      </table></div>
    </div>`;
}

function renderReadinessRow(client, selected) {
  const binding = bindingFor(client.client_id);
  const dropped = !selected.has(client.client_id);
  const meta = BINDING_STATE_META[binding.state];
  const inline = state.inlineConnect[client.client_id];
  // Inline Connect saves a report NAME, so it is offered only where a name is
  // the missing piece. `not_connected` is a whole missing integration — the
  // wizard cannot create one, and saying "Connect" here would promise a fix
  // this form does not have (AGENTS §6: every failed state names a real next
  // action).
  const canConnectInline = binding.state === "no_report_name" || binding.state === "foreign_binding";
  const action = canConnectInline
    ? `<button type="button" class="button button--quiet" data-scope-connect="${escapeHtml(client.client_id)}" aria-expanded="${Boolean(inline?.open)}">Connect</button>`
    : `<a class="button button--quiet" href="${buildClientHash(client.client_id)}">Open client page</a>`;

  return `
    <tr class="reports-binding-row${dropped ? " reports-binding-row--excluded" : ""}" data-state="${binding.state}">
      <td>
        <input type="checkbox" data-readiness-client="${escapeHtml(client.client_id)}"
          ${dropped ? "" : "checked"}
          aria-label="Keep ${escapeHtml(client.name)} in this batch">
      </td>
      <td>
        <span class="reports-binding-row__name" translate="no">${escapeHtml(client.name)}</span>
        ${isPaused(client) ? `<span class="status-label state-paused">${icon("player-pause", "icon icon--tiny")}Paused</span>` : ""}
        ${dropped ? `<span class="status-label state-not_connected">Excluded</span>` : ""}
      </td>
      <td class="reports-binding-row__report" translate="no">${binding.ninja_report_name ? escapeHtml(binding.ninja_report_name) : "—"}</td>
      <td><span class="status-label state-${binding.state}">${icon(meta.icon, "icon icon--tiny")}${meta.label}</span></td>
      <td class="reports-binding-row__actions">${action}</td>
    </tr>
    ${inline?.open ? `<tr class="reports-binding-row__connect"><td></td><td colspan="4">${renderInlineConnect(client)}</td></tr>` : ""}`;
}

// --- Inline Connect (task-49 §2.2) ------------------------------------------
//
// Reuses the exact discovery/save mechanic task-45 built for the single-
// client step-4 flow (`matches`/`others`/`confirm_mismatch`) — just keyed by
// `client_id` instead of one global slot, so more than one row can be open.

function renderInlineConnect(client) {
  const entry = state.inlineConnect[client.client_id];
  const found = entry.matches.length + entry.others.length;
  if (entry.status === "checking") {
    return `<p class="field-help">${statusIcon("loader-2")}${entry.refreshing
      ? "Re-reading the Reporting Ninja account — about 10 seconds…"
      : "Looking up this client's report in the Reporting Ninja account…"}</p>`;
  }
  if (entry.status === "error") {
    return `<p class="form-error" role="alert">${escapeHtml(entry.message)}</p>`;
  }
  if (entry.status !== "known") return "";
  // Three states used to arrive as one empty list. "The account has no report
  // for this client" is a claim about the account, and it is only safe to
  // make from a cabinet read that actually succeeded — telling the operator
  // to go create a report that already exists earns a duplicate in the
  // account (AGENTS §5: unavailable, truncated and empty are distinct).
  if (entry.lookupError) {
    return `
      <p class="form-error" role="alert">Could not read the Reporting Ninja account: ${escapeHtml(entry.lookupError)}</p>
      <p class="field-help">Any report name saved earlier still applies. A report run in progress holds the Reporting Ninja session — retry once it finishes.</p>
      ${inlineRereadButton(client)}`;
  }
  if (!found) {
    return `
      <p class="form-error" role="alert">No report in the Reporting Ninja account belongs to this client. Create the client's report in Reporting Ninja, then re-read the account.</p>
      ${inlineRereadButton(client)}`;
  }
  const foreign = inlineCandidateIsForeign(entry);
  const saveBlocked = entry.saving || !entry.selectedCandidate || (foreign && !entry.confirmed);
  return `
    ${entry.saveError ? `<p class="form-error" role="alert">${escapeHtml(entry.saveError)}</p>` : ""}
    ${entry.matches.length ? `
      <fieldset class="reports-ninja-candidates">
        <legend>Reports for <span translate="no">${escapeHtml(client.name)}</span></legend>
        ${entry.matches.map((c) => renderInlineCandidateRow(client, c)).join("")}
      </fieldset>`
      : `<p class="reports-ninja-empty">${icon("alert-triangle", "icon icon--tiny")}No report in the account starts with <strong translate="no">${escapeHtml(client.name)}</strong>.</p>`}
    ${entry.others.length ? `
      <details class="reports-ninja-others" ${foreign ? "open" : ""}>
        <summary>Other reports in the account (${entry.others.length}) — belong to different clients</summary>
        <fieldset class="reports-ninja-candidates">
          <legend class="visually-hidden">Reports belonging to other clients</legend>
          ${entry.others.map((c) => renderInlineCandidateRow(client, c)).join("")}
        </fieldset>
      </details>`
      : ""}
    ${foreign ? `
      <label class="reports-force-refresh reports-ninja-confirm" for="rw-inline-confirm-${escapeHtml(client.client_id)}">
        <input type="checkbox" id="rw-inline-confirm-${escapeHtml(client.client_id)}" data-inline-confirm="${escapeHtml(client.client_id)}" ${entry.confirmed ? "checked" : ""}>
        <span><strong>I confirm «<span translate="no">${escapeHtml(entry.selectedCandidate)}</span>» is the report for <span translate="no">${escapeHtml(client.name)}</span></strong><small>Its name belongs to a different client.</small></span>
      </label>` : ""}
    <button type="button" class="button button--primary" data-inline-save="${escapeHtml(client.client_id)}" ${saveBlocked ? "disabled" : ""}>
      ${entry.saving ? statusIcon("loader-2") : ""}${entry.saving ? "Saving…" : "Save this report name"}
    </button>
    ${inlineRereadButton(client)}`;
}

// The account list is one shared snapshot, so this is the only thing in the
// flow that costs a cabinet session — offered explicitly instead of being
// paid for on every Connect click. A quiet meta row, not another boxed
// button: it sits right under the primary Save action and must read as a
// footnote to it, not compete with it (`.button--quiet` at full box size did).
function inlineRereadButton(client) {
  const entry = state.inlineConnect[client.client_id];
  const age = snapshotAgeLabel(entry?.snapshotAgeSeconds);
  return `
    <p class="reports-ninja-reread">
      <span>${age ? `Account list read ${escapeHtml(age)}.` : "Account list not read yet."}</span>
      <button type="button" class="text-button" data-inline-reread="${escapeHtml(client.client_id)}">Re-read the account</button>
    </p>`;
}

function snapshotAgeLabel(seconds) {
  if (typeof seconds !== "number" || !Number.isFinite(seconds)) return "";
  const minutes = Math.round(seconds / 60);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  return hours < 24 ? `${hours} h ago` : `${Math.round(hours / 24)} d ago`;
}

function renderInlineCandidateRow(client, candidate) {
  const entry = state.inlineConnect[client.client_id];
  return `
    <label class="reports-ninja-candidate">
      <input type="radio" name="rw-inline-candidate-${escapeHtml(client.client_id)}" value="${escapeHtml(candidate.name)}" data-inline-candidate="${escapeHtml(client.client_id)}" ${entry.selectedCandidate === candidate.name ? "checked" : ""}>
      <strong translate="no">${escapeHtml(candidate.name)}</strong>
      ${candidate.exact_match ? `<span class="status-label state-succeeded">${icon("check", "icon icon--tiny")}This client</span>` : ""}
    </label>`;
}

function inlineCandidateIsForeign(entry) {
  return Boolean(entry.selectedCandidate) && !entry.matches.some((c) => c.name === entry.selectedCandidate);
}

function openInlineConnect(clientId, { refresh = false } = {}) {
  const existing = state.inlineConnect[clientId];
  if (existing?.open && !refresh) { existing.open = false; rerenderSourceStep(); return; }
  state.inlineConnect[clientId] = {
    open: true, status: "checking", name: null, matches: [], others: [],
    selectedCandidate: "", confirmed: false, message: "", saving: false, saveError: null,
    lookupError: null, snapshotAgeSeconds: null, refreshing: refresh,
  };
  rerenderSourceStep();
  getNinjaReportName(clientId, { refresh }).then((result) => {
    if (!state) return;
    const entry = state.inlineConnect[clientId];
    if (!entry) return;
    entry.status = "known";
    entry.name = result.ninja_report_name || null;
    entry.matches = result.matches || [];
    entry.others = result.others || [];
    entry.lookupError = result.lookup_error || null;
    entry.snapshotAgeSeconds = result.snapshot_age_seconds ?? null;
    entry.selectedCandidate = entry.matches[0]?.name || "";
    rerenderSourceStep();
  }).catch((error) => {
    if (isAbortError(error) || !state) return;
    const entry = state.inlineConnect[clientId];
    if (!entry) return;
    entry.status = "error";
    entry.message = `Could not search Reporting Ninja: ${error.message}`;
    rerenderSourceStep();
  });
}

function saveInlineConnect(clientId) {
  const entry = state.inlineConnect[clientId];
  const name = entry?.selectedCandidate;
  if (!name || entry.saving || (inlineCandidateIsForeign(entry) && !entry.confirmed)) return;
  entry.saving = true;
  entry.saveError = null;
  rerenderSourceStep();
  putNinjaReportName(clientId, name, { confirmMismatch: inlineCandidateIsForeign(entry) }).then(() => {
    if (!state) return;
    // task-49 §2.2: the row flips to `Ready` in place — no wizard reload.
    if (state.bindings) {
      state.bindings[clientId] = { ...bindingFor(clientId), ninja_report_name: name, state: "ready" };
    }
    delete state.inlineConnect[clientId];
    rerenderSourceStep();
  }).catch((error) => {
    if (isAbortError(error) || !state) return;
    const live = state.inlineConnect[clientId];
    // A failed SAVE keeps the candidate list and Save button in place (status
    // stays "known") — losing them meant the only way to try again was to
    // close and reopen Connect, re-running discovery for nothing.
    if (live) { live.saving = false; live.saveError = `Could not save the report name: ${error.message}`; }
    rerenderSourceStep();
  });
}

// --- Step 2: type ----------------------------------------------------------

function renderStep2() {
  return `
    <div class="reports-kind-grid" role="group" aria-label="Report type">
      ${Object.entries(REPORT_KIND_META).map(([key, meta], index) => `
        <button type="button" class="reports-kind-card" data-kind="${key}" aria-pressed="${state.kind === key}" ${index === 0 ? "data-autofocus" : ""}>
          ${icon(KIND_ICONS[key] || "file-report")}
          <strong>${escapeHtml(meta.label)}</strong>
          <span>${escapeHtml(meta.description)}</span>
        </button>`).join("")}
    </div>`;
}

// --- Step 3: parameters --------------------------------------------------

function renderStep3() {
  const client = selectedClient();
  const clientOptions = [...context.clients].sort((a, b) => a.name.localeCompare(b.name)).map((c) => {
    const paused = isPaused(c) ? " — paused" : "";
    return `<option value="${escapeHtml(c.client_id)}" ${c.client_id === state.clientId ? "selected" : ""}>${escapeHtml(c.name)}${paused}</option>`;
  }).join("");
  const hint = state.clientId && !clientNinjaConfigured(client)
    ? `<p class="reports-client-hint">${icon("plug-off")}Reporting Ninja isn't connected for this client — connect it on the client page before generating a report.</p>`
    : "";
  const clientPicker = state.scope === "one" ? `
    <div class="reports-client-picker">
      <label>Client<select id="rw-client" data-autofocus><option value="">Choose a client…</option>${clientOptions}</select></label>
      ${hint}
    </div>` : `
    <p class="field-help" data-autofocus tabindex="-1">${state.selectedClientIds.length} client${state.selectedClientIds.length === 1 ? "" : "s"} selected in the previous step.</p>`;

  return `
    ${clientPicker}
    <div class="reports-step-section">
      <h3>Parameters</h3>
      ${state.kind === "monthly" ? renderMonthlyParams()
        : state.kind === "yearly" ? renderYearlyParams()
          : state.kind === "custom_months" ? renderCustomMonthsParams()
            : renderComparatorParams()}
    </div>
    ${state.scope === "one" ? renderPeriodPreview() : ""}`;
}

function renderMonthlyParams() {
  return `<p class="field-help">This type has no parameters — 6 standard periods anchored to the last complete month.</p>`;
}

function renderYearlyParams() {
  const [thisYear] = todayYm();
  const options = Array.from({ length: 6 }, (_, i) => thisYear - i);
  return `
    <div class="reports-year-picker" role="group" aria-label="Years">
      ${options.map((year) => `
        <button type="button" class="chip-button" data-year="${year}" aria-pressed="${state.years.includes(year)}" ${!state.years.includes(year) && state.years.length >= 4 ? "disabled" : ""}>${year}</button>`).join("")}
    </div>
    <div class="reports-span-toggle" role="group" aria-label="Year span">
      <button type="button" data-span="ytd" aria-pressed="${state.span === "ytd"}">Year to date</button>
      <button type="button" data-span="full" aria-pressed="${state.span === "full"}">Full year</button>
    </div>
    <p class="reports-span-help">${state.span === "ytd"
      ? "Year to date compares the same Jan 1 → last complete month window in every year — apples to apples, never marked partial."
      : "Full year compares Jan 1 → Dec 31. The current year is clipped to the last complete month and marked partial."}</p>`;
}

function renderCustomMonthsParams() {
  return `
    <div class="reports-month-toolbar">
      <div class="reports-year-switch">
        <button type="button" class="icon-button" data-grid-year="prev" aria-label="Previous year">${icon("chevron-left")}</button>
        <strong>${state.gridYear}</strong>
        <button type="button" class="icon-button" data-grid-year="next" aria-label="Next year">${icon("chevron-right")}</button>
      </div>
      <button type="button" class="button button--quiet" id="rw-last12">Last 12 months</button>
      <button type="button" class="button button--quiet" id="rw-clear-months">Clear</button>
      <span class="reports-month-count">${state.months.length} / 24</span>
    </div>
    <div class="reports-month-grid" role="group" aria-label="Months">
      ${monthGridCells(state.gridYear)}
    </div>`;
}

function monthGridCells(year) {
  const [ty, tm] = todayYm();
  return MONTH_NAMES.map((name, index) => {
    const month = index + 1;
    const value = `${year}-${pad2(month)}`;
    const future = year > ty || (year === ty && month > tm);
    const isPartial = year === ty && month === tm;
    const selected = state.months.includes(value);
    return `
      <button type="button" class="reports-month-cell" data-month="${value}" aria-pressed="${selected}"
        data-partial="${isPartial}" ${future ? "disabled" : ""} ${!selected && state.months.length >= 24 ? "disabled" : ""}>
        ${name.slice(0, 3)}
      </button>`;
  }).join("");
}

function renderComparatorParams() {
  const [thisYear] = todayYm();
  const options = Array.from({ length: 6 }, (_, i) => thisYear - i);
  return `
    <label>Month<select id="rw-comparator-month">
      ${MONTH_NAMES.map((name, index) => `<option value="${index + 1}" ${state.comparatorMonth === index + 1 ? "selected" : ""}>${name}</option>`).join("")}
    </select></label>
    <p class="field-help">Every year below uses this same calendar month — mixing different months isn't possible here (use Custom months for that).</p>
    <div class="reports-year-picker reports-year-picker--spaced" role="group" aria-label="Years">
      ${options.map((year) => `
        <button type="button" class="chip-button" data-comparator-year="${year}" aria-pressed="${state.comparatorYears.includes(year)}" ${!state.comparatorYears.includes(year) && state.comparatorYears.length >= 4 ? "disabled" : ""}>${year}</button>`).join("")}
    </div>`;
}

function renderPeriodPreview() {
  if (!state.clientId) return "";
  const periods = currentPeriods();
  if (!periods.length) return `<p class="field-help">Choose parameters to preview the exact date ranges.</p>`;
  return `
    <div class="reports-step-section">
      <h3>What will run</h3>
      <div class="reports-period-preview">
        ${periods.map((p) => `
          <div class="reports-period-preview__row">
            <strong translate="no">${escapeHtml(p.label)}</strong>
            <span>${escapeHtml(formatDateRange(p.date_from, p.date_to))}</span>
            ${p.is_partial ? `<span class="status-label state-partial reports-period-preview__partial">${icon("alert-triangle", "icon icon--tiny")}Partial</span>` : ""}
          </div>`).join("")}
      </div>
    </div>`;
}

// --- Step 4: source ------------------------------------------------------
//
// The estimate multiplies by client count and names the parallelism
// difference between the two sources (task-47 §3.1): the API path runs
// several clients at once (`reports_batch_client_workers`, default 3), the
// PDF path shares ONE browser across the whole batch, so it is an honest
// long queue rather than a number that quietly assumes parallelism it does
// not have.
const BATCH_API_PARALLELISM = 3;

function renderStep4() {
  const periodCount = currentPeriods().length;
  const clientCount = state.scope === "all" ? Math.max(1, state.selectedClientIds.length) : 1;
  const apiPerColumn = types?.types.find((t) => t.kind === state.kind)?.estimate.api_seconds_per_column;
  const pdfPerColumn = types?.types.find((t) => t.kind === state.kind)?.estimate.pdf_seconds_per_column;
  const apiSeconds = apiPerColumn !== undefined ? apiPerColumn * periodCount * clientCount : null;
  const pdfSeconds = pdfPerColumn !== undefined ? pdfPerColumn * periodCount * clientCount : null;
  const parallel = Math.min(clientCount, BATCH_API_PARALLELISM);

  const apiEstimate = apiSeconds === null ? "…"
    : clientCount > 1 ? `~${formatDurationSeconds(apiSeconds / parallel)} (${parallel} clients in parallel)`
      : `~${formatDurationSeconds(apiSeconds)}`;
  const pdfEstimate = pdfSeconds === null ? "…" : `~${formatDurationSeconds(pdfSeconds)}`;

  // The caveat has to stop promising cache reuse once the operator has asked
  // for a full re-download — the estimate is computed as
  // `perColumn × periods × clients` either way, i.e. it always assumed no
  // cache, so with Force re-download on it becomes the honest number rather
  // than a pessimistic ceiling.
  const pdfCaveat = [
    state.forceRefresh
      ? "Force re-download is on — nothing is reused from cache, so expect the full estimate."
      : "Closed periods already downloaded before are reused from cache — actual time can be lower.",
    clientCount > 1
      ? "One browser, shared across the whole batch — clients go one at a time, not in parallel."
      : "",
  ].filter(Boolean).join(" ");

  return `
    <div class="reports-source-grid" role="group" aria-label="Collection source">
      <button type="button" class="reports-source-card" data-source="api" aria-pressed="${state.source === "api"}" data-autofocus>
        <strong>API — fast</strong>
        <span class="reports-source-card__estimate">${apiEstimate}</span>
        <p>Same numbers as the PDF report, verified. Via Reporting Ninja.</p>
      </button>
      <button type="button" class="reports-source-card" data-source="google_api" aria-pressed="${state.source === "google_api"}">
        <strong>Google API</strong>
        <span class="reports-source-card__estimate">${apiEstimate}</span>
        <p>Direct from Google — no Reporting Ninja. Same numbers, one dependency fewer.</p>
      </button>
      <button type="button" class="reports-source-card" data-source="pdf" aria-pressed="${state.source === "pdf"}">
        <strong>PDF — evidence</strong>
        <span class="reports-source-card__estimate">${pdfEstimate}</span>
        <p>Downloads the actual client-facing PDFs and cross-checks them against the API.</p>
        <small class="reports-source-card__caveat">${escapeHtml(pdfCaveat)}</small>
      </button>
    </div>
    ${state.source === "pdf" ? (state.scope === "one" ? renderPdfExtras() : renderBatchPdfNote()) : ""}
    ${state.source === "google_api" ? renderGoogleApiNote() : ""}
    ${renderBatchReadiness()}`;
}

// task-49: a batch had NO force-refresh control at all — `force_refresh`
// silently went out as `false` even though `ReportBatchCreateRequest` accepts
// it, so "run the portfolio, ignoring yesterday's PDFs" was simply not
// expressible. Same control for both scopes now; unchecked stays the default
// (task-49 §2.7: the cache is a correct optimisation, not something to
// disable by default).
function renderForceRefreshToggle() {
  return `
    <label class="reports-force-refresh" for="rw-force-refresh">
      <input type="checkbox" id="rw-force-refresh" ${state.forceRefresh ? "checked" : ""}>
      <span><strong>Force re-download</strong><small>Ignore cached PDFs for closed periods and download every column again.${
        state.scope === "all" ? " The estimate above already assumes a full download." : ""
      }</small></span>
    </label>`;
}

function renderBatchPdfNote() {
  return renderForceRefreshToggle();
}

// task-84 §4.1: the direct source has no per-client setup step — the GA4
// property id and GSC site are already stored, and no Reporting Ninja report
// name is ever read, which is why `ninjaGateStates` shows no readiness table
// for it. The two things worth saying are the missing dependency (the point
// of the source) and the one hard limit the operator can actually hit: GSC's
// 16-month history ceiling.
function renderGoogleApiNote() {
  const historyLimited = state.kind === "yearly" || state.kind === "custom_months";
  return `
    <p class="reports-client-hint">${icon("brand-google")}
      Reads GSC Search Analytics and GA4 directly — Reporting Ninja is never contacted.${
        historyLimited
          ? " Google's Search Analytics API only serves ~16 months of history, so any column older than that shows as “—”, never zero."
          : ""
      }
    </p>`;
}

function renderPdfExtras() {
  return `
    ${renderForceRefreshToggle()}
    ${renderNinjaReportBlock()}`;
}

function renderNinjaReportBlock() {
  const found = state.ninjaMatches.length + state.ninjaOthers.length;
  // The saved binding is always visible (task-45 §2.2) — a wrong one has to be
  // spottable here, not only by reading the database.
  const savedLine = state.ninjaName
    ? `<p class="field-help">${icon("circle-check", "icon icon--tiny")} Reporting Ninja report: <strong translate="no">${escapeHtml(state.ninjaName)}</strong> <button type="button" class="button button--quiet" id="rw-ninja-recheck">Change or re-check</button></p>`
    : "";
  if (state.ninjaName && !found && state.ninjaState !== "checking") return savedLine;
  return `
    ${savedLine}
    <div class="reports-ninja-discovery">
      <p>${state.ninjaState === "checking"
        ? (state.ninjaRefreshing
          ? "Re-reading the Reporting Ninja account — about 10 seconds."
          : "Looking up this client's report in the Reporting Ninja account.")
        : state.ninjaName
          ? "Pick a different report only if this client's PDFs really come from it."
          : "No saved Reporting Ninja report name for this client yet."}</p>
      <button type="button" class="button button--primary" id="rw-ninja-find" ${state.ninjaState === "checking" ? "disabled" : ""}>
        ${state.ninjaState === "checking" ? statusIcon("loader-2") : icon("search", "icon icon--tiny")}
        ${state.ninjaState === "checking" ? "Looking up…" : "Find report"}
      </button>
      ${state.ninjaState === "known" && found ? renderNinjaCandidates() : ""}
      ${state.ninjaState === "known" && !found ? renderNinjaNoCandidates() : ""}
      ${state.ninjaState === "known" ? renderNinjaRereadLine() : ""}
    </div>`;
}

// "The account has no report for this client" is a claim about the account,
// safe to make only from a cabinet read that succeeded. Told wrongly, the
// operator creates a report that already exists (AGENTS §5: unavailable and
// empty are distinct states, and every failed state names its next action).
function renderNinjaNoCandidates() {
  if (state.ninjaLookupError) {
    return `
      <p class="form-error" role="alert">Could not read the Reporting Ninja account: ${escapeHtml(state.ninjaLookupError)}</p>
      <p class="field-help">Any report name saved earlier still applies. A report run in progress holds the Reporting Ninja session — retry once it finishes.</p>`;
  }
  return `<p class="form-error" role="alert">No report in the Reporting Ninja account belongs to this client. Create the client's report in Reporting Ninja, then re-read the account.</p>`;
}

// The account list is one shared snapshot, so a re-read is the only step in
// this flow that costs a cabinet session — offered, not charged for on every
// lookup. Same quiet meta row as `inlineRereadButton`.
function renderNinjaRereadLine() {
  const age = snapshotAgeLabel(state.ninjaSnapshotAgeSeconds);
  return `
    <p class="reports-ninja-reread">
      <span>${age ? `Account list read ${escapeHtml(age)}.` : "Account list not read yet."}</span>
      <button type="button" class="text-button" id="rw-ninja-reread">Re-read the account</button>
    </p>`;
}

function renderNinjaCandidateRow(candidate) {
  return `
    <label class="reports-ninja-candidate">
      <input type="radio" name="rw-ninja-candidate" value="${escapeHtml(candidate.name)}" ${state.ninjaSelectedCandidate === candidate.name ? "checked" : ""}>
      <strong translate="no">${escapeHtml(candidate.name)}</strong>
      ${candidate.exact_match ? `<span class="status-label state-succeeded">${icon("check", "icon icon--tiny")}This client</span>` : ""}
    </label>`;
}

// This client's reports first and on their own; the rest of the account is a
// separate, collapsed block, so a foreign report is never one stray click away
// from a permanent binding (task-45 §1.2/§2.2).
function renderNinjaCandidates() {
  const clientName = selectedClient()?.name || "this client";
  const foreign = ninjaCandidateIsForeign(state.ninjaSelectedCandidate);
  return `
    ${state.ninjaMatches.length ? `
      <fieldset class="reports-ninja-candidates">
        <legend>Reports for <span translate="no">${escapeHtml(clientName)}</span></legend>
        ${state.ninjaMatches.map(renderNinjaCandidateRow).join("")}
      </fieldset>`
      : `<p class="reports-ninja-empty">${icon("alert-triangle", "icon icon--tiny")}No report in the account starts with <strong translate="no">${escapeHtml(clientName)}</strong>. Create one in Reporting Ninja, or bind another report below and confirm it explicitly.</p>`}
    ${state.ninjaOthers.length ? `
      <details class="reports-ninja-others" ${foreign ? "open" : ""}>
        <summary>Other reports in the account (${state.ninjaOthers.length}) — belong to different clients</summary>
        <fieldset class="reports-ninja-candidates">
          <legend class="visually-hidden">Reports belonging to other clients</legend>
          ${state.ninjaOthers.map(renderNinjaCandidateRow).join("")}
        </fieldset>
      </details>`
      : ""}
    ${foreign ? `
      <label class="reports-force-refresh reports-ninja-confirm" for="rw-ninja-confirm">
        <input type="checkbox" id="rw-ninja-confirm" ${state.ninjaConfirmed ? "checked" : ""}>
        <span><strong>I confirm «<span translate="no">${escapeHtml(state.ninjaSelectedCandidate)}</span>» is the report for <span translate="no">${escapeHtml(clientName)}</span></strong><small>Its name belongs to a different client. Every PDF run for this client will read that report from now on.</small></span>
      </label>` : ""}
    <button type="button" class="button button--primary" id="rw-ninja-save" ${ninjaSaveBlocked() ? "disabled" : ""}>Save this report name</button>
    ${state.ninjaSelectedCandidate && ninjaSaveBlocked() ? `<p class="field-help">Confirm the binding above to enable Save.</p>` : ""}
    ${!state.ninjaSelectedCandidate ? `<p class="field-help">Choose the report this client's PDFs come from.</p>` : ""}`;
}

// --- Events ----------------------------------------------------------------

body.addEventListener("click", onBodyClick);
body.addEventListener("change", onBodyChange);
body.addEventListener("input", onBodyInput);

function onBodyClick(event) {
  if (!state) return;
  const scopeCard = event.target.closest("[data-scope]");
  if (scopeCard) { selectScope(scopeCard.dataset.scope); return; }

  const scopeClient = event.target.closest("[data-scope-client]");
  if (scopeClient) { toggleScopeClient(scopeClient.dataset.scopeClient); return; }

  const readinessClient = event.target.closest("[data-readiness-client]");
  if (readinessClient) { toggleReadinessClient(readinessClient.dataset.readinessClient); return; }

  if (event.target.closest("#rw-select-all")) { selectAllScopeClients(); return; }
  if (event.target.closest("#rw-clear-clients")) { clearScopeClients(); return; }

  const connectButton = event.target.closest("[data-scope-connect]");
  if (connectButton) { openInlineConnect(connectButton.dataset.scopeConnect); return; }

  const rereadButton = event.target.closest("[data-inline-reread]");
  if (rereadButton) {
    openInlineConnect(rereadButton.dataset.inlineReread, { refresh: true });
    return;
  }

  // "Open client page" navigates away — close the modal so it doesn't sit
  // open behind/in front of the page the operator just asked to see.
  if (event.target.closest(".reports-binding-row__actions a")) { dialog.close(); return; }

  const inlineSave = event.target.closest("[data-inline-save]");
  if (inlineSave) { saveInlineConnect(inlineSave.dataset.inlineSave); return; }

  const kindCard = event.target.closest("[data-kind]");
  if (kindCard) { state.kind = kindCard.dataset.kind; renderStep(); return; }

  const yearChip = event.target.closest("[data-year]");
  if (yearChip) { toggleYear(Number(yearChip.dataset.year)); return; }

  const spanButton = event.target.closest("[data-span]");
  if (spanButton) { state.span = spanButton.dataset.span; renderStep(); return; }

  const gridNav = event.target.closest("[data-grid-year]");
  if (gridNav) { state.gridYear += gridNav.dataset.gridYear === "prev" ? -1 : 1; renderStep(); return; }

  const monthCell = event.target.closest("[data-month]");
  if (monthCell && !monthCell.disabled) { toggleMonth(monthCell.dataset.month); return; }

  if (event.target.closest("#rw-last12")) { fillLast12Months(); return; }
  if (event.target.closest("#rw-clear-months")) { state.months = []; renderStep(); return; }

  const comparatorYear = event.target.closest("[data-comparator-year]");
  if (comparatorYear) { toggleComparatorYear(Number(comparatorYear.dataset.comparatorYear)); return; }

  const sourceCard = event.target.closest("[data-source]");
  if (sourceCard) { selectSource(sourceCard.dataset.source); return; }

  if (event.target.closest("#rw-ninja-reread")) { void checkNinjaReport({ refresh: true }); return; }
  if (event.target.closest("#rw-ninja-find") || event.target.closest("#rw-ninja-recheck")) { void checkNinjaReport(); return; }
  if (event.target.closest("#rw-ninja-save")) { void saveNinjaReport(); }
}

function onBodyChange(event) {
  if (event.target.id === "rw-client") {
    state.clientId = event.target.value;
    state.ninjaState = "unknown"; state.ninjaName = null; state.ninjaCandidates = []; state.ninjaSelectedCandidate = "";
    renderStep();
  } else if (event.target.id === "rw-comparator-month") {
    state.comparatorMonth = Number(event.target.value);
    body.innerHTML = renderStep3();
    renderFooter();
  } else if (event.target.id === "rw-force-refresh") {
    state.forceRefresh = event.target.checked;
    // Re-render so the PDF card's caveat stops/starts promising cache reuse
    // — `rerenderSourceStep` keeps focus on the checkbox that was just
    // toggled instead of jumping to the top of the step.
    rerenderSourceStep("#rw-force-refresh");
  } else if (event.target.name === "rw-ninja-candidate") {
    state.ninjaSelectedCandidate = event.target.value;
    // A fresh choice always re-arms the guard: confirming one foreign report
    // must never carry over to the next one selected.
    state.ninjaConfirmed = false;
    rerenderSourceStep(`input[name="rw-ninja-candidate"][value="${cssEscape(event.target.value)}"]`);
  } else if (event.target.id === "rw-ninja-confirm") {
    state.ninjaConfirmed = event.target.checked;
    rerenderSourceStep("#rw-ninja-confirm");
  } else if (event.target.dataset.inlineCandidate) {
    const entry = state.inlineConnect[event.target.dataset.inlineCandidate];
    if (!entry) return;
    entry.selectedCandidate = event.target.value;
    entry.confirmed = false; // a fresh choice always re-arms the foreign-binding guard
    entry.saveError = null; // and clears a stale error from a previous, different attempt
    rerenderSourceStep();
  } else if (event.target.dataset.inlineConfirm) {
    const entry = state.inlineConnect[event.target.dataset.inlineConfirm];
    if (!entry) return;
    entry.confirmed = event.target.checked;
    rerenderSourceStep();
  }
}

function cssEscape(value) {
  return window.CSS?.escape ? window.CSS.escape(value) : value.replace(/["\\]/g, "\\$&");
}

function onBodyInput(event) {
  if (event.target.id !== "rw-client-search") return;
  state.clientQuery = event.target.value;
  const container = body.querySelector(".reports-scope-checklist");
  if (!container) return;
  container.outerHTML = renderScopeChecklist();
  const input = body.querySelector("#rw-client-search");
  if (!input) return;
  input.focus();
  input.setSelectionRange(input.value.length, input.value.length);
}

// `renderStep()` moves focus to the step's first control, which is right when
// a step opens and wrong when a control inside it merely changed state — it
// would throw the operator back to the top of the dialog on every radio click.
// Here the control that caused the change keeps focus and stays in view.
function rerenderSourceStep(focusSelector) {
  rerenderPreservingScroll(renderStep4, focusSelector);
}

function toggleYear(year) {
  state.years = state.years.includes(year) ? state.years.filter((y) => y !== year) : [...state.years, year];
  body.innerHTML = renderStep3();
  renderFooter();
}

function toggleMonth(value) {
  state.months = state.months.includes(value) ? state.months.filter((m) => m !== value) : [...state.months, value];
  body.innerHTML = renderStep3();
  renderFooter();
}

function fillLast12Months() {
  const [ty, tm] = todayYm();
  const months = [];
  for (let i = 0; i < 12; i += 1) {
    let year = ty; let month = tm - i;
    while (month <= 0) { month += 12; year -= 1; }
    months.push(`${year}-${pad2(month)}`);
  }
  state.months = months;
  body.innerHTML = renderStep3();
  renderFooter();
}

function toggleComparatorYear(year) {
  state.comparatorYears = state.comparatorYears.includes(year)
    ? state.comparatorYears.filter((y) => y !== year)
    : [...state.comparatorYears, year];
  body.innerHTML = renderStep3();
  renderFooter();
}

function selectSource(source) {
  state.source = source;
  if (source === "pdf" && state.scope === "one" && state.ninjaState === "unknown") {
    renderStep(); void checkNinjaReport(); return;
  }
  renderStep();
}

async function checkNinjaReport({ refresh = false } = {}) {
  state.ninjaState = "checking";
  state.ninjaRefreshing = refresh;
  renderStep();
  try {
    const result = await getNinjaReportName(state.clientId, { refresh });
    if (!state) return;
    state.ninjaState = "known";
    state.ninjaName = result.ninja_report_name || null;
    state.ninjaMatches = result.matches || [];
    state.ninjaOthers = result.others || [];
    state.ninjaLookupError = result.lookup_error || null;
    state.ninjaSnapshotAgeSeconds = result.snapshot_age_seconds ?? null;
    state.ninjaConfirmed = false;
    // Pre-selects only a report that actually belongs to this client. When
    // there is none, nothing is selected — the operator has to choose, which
    // is exactly the decision task-42 made for them by sorting.
    state.ninjaSelectedCandidate = state.ninjaMatches[0]?.name || "";
  } catch (error) {
    if (isAbortError(error) || !state) return;
    state.ninjaState = "unknown";
    reasonLine.textContent = `Could not search Reporting Ninja: ${error.message}`;
  }
  renderStep();
}

async function saveNinjaReport() {
  const name = state.ninjaSelectedCandidate;
  if (!name || ninjaSaveBlocked()) return;
  try {
    // The server re-validates both rules (name in the account, prefix vs
    // `confirm_mismatch`) — this flag tells it the operator confirmed, it does
    // not replace the check (task-45 §2.2.3).
    await putNinjaReportName(state.clientId, name, { confirmMismatch: ninjaCandidateIsForeign(name) });
    if (!state) return;
    state.ninjaName = name;
  } catch (error) {
    if (isAbortError(error) || !state) return;
    reasonLine.textContent = `Could not save the report name: ${error.message}`;
    return;
  }
  renderStep();
}

// --- Submit ----------------------------------------------------------------

function sharedParams() {
  if (state.kind === "yearly") return { years: state.years, span: state.span, force_refresh: state.forceRefresh };
  if (state.kind === "custom_months") return { months: state.months, force_refresh: state.forceRefresh };
  if (state.kind === "comparator") {
    return { months: state.comparatorYears.map((y) => `${y}-${pad2(state.comparatorMonth)}`), force_refresh: state.forceRefresh };
  }
  return { force_refresh: state.forceRefresh };
}

function payload() {
  const base = { kind: state.kind, source: state.source, ...sharedParams() };
  if (state.scope === "all") return { ...base, client_ids: state.selectedClientIds };
  return { ...base, client_id: state.clientId };
}

async function submit() {
  submitting = true;
  renderFooter();
  try {
    if (state.scope === "all") {
      const result = await createReportBatch(payload());
      const { onBatchCreated } = context;
      dialog.close();
      onBatchCreated(result.batch_id);
      return;
    }
    const result = await createReportRun(payload());
    const { onCreated } = context;
    dialog.close();
    onCreated(result.run_id);
  } catch (error) {
    if (isAbortError(error) || !state) return;
    submitting = false;
    reasonLine.textContent = `Could not start the report: ${error.message}`;
    renderFooter();
  }
}
