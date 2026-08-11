import { discoverIntegration, getDashboard, listPortfolioMonitorChanges, updateIntegration } from "./api.js?v=20260811-monitor1";
import { clientGroup, filterByClientGroup } from "./client-groups.js?v=20260811-monitor1";
import { renderClientCard, renderSkeletons } from "./client-card.js?v=20260811-monitor1";
import { isPaused, openClientStatusDialog } from "./client-status.js?v=20260811-monitor1";
import { createDetailDrawer } from "./detail-drawer.js?v=20260811-monitor1";
import { PROVIDERS, escapeHtml, formatRange, formatTimestamp, icon } from "./formatters.js?v=20260811-monitor1";
import { triggerLogin, watchProviderSession } from "./session-status.js?v=20260811-monitor1";
import { getState, setState } from "./store.js?v=20260811-monitor1";
import { configureJobProgress, getJobProgress, showToast } from "./job-progress.js?v=20260811-monitor1";

const grid = document.querySelector("#client-grid");
const dashboardState = document.querySelector("#dashboard-state");
const search = document.querySelector("#client-search");
const statusFilter = document.querySelector("#status-filter");
const providerFilter = document.querySelector("#provider-filter");
let requestToken = 0;
let searchTimer = null;

// Sort control (PLAN §21.3): the filter bar is owned by index.html
// (frontend/AGENTS.md read-only zone for task-19), so the control is
// mounted into it here rather than added to the markup directly.
const SORT_LABELS = {
  quality: "By data quality",
  health: "By health %",
  name: "By name",
  last_sync: "By last collection",
  biggest_drop: "By biggest drop",
};
const filterBar = document.querySelector(".filter-bar");
const sortField = document.createElement("label");
sortField.innerHTML = `<span class="visually-hidden">Sort clients</span><select id="sort-select">${Object.entries(SORT_LABELS).map(([value, label]) => `<option value="${value}">${label}</option>`).join("")}</select>`;
filterBar.insertBefore(sortField, document.querySelector("#clear-filters"));
const sortSelect = document.querySelector("#sort-select");
sortSelect.addEventListener("change", () => { setState({ sort: sortSelect.value }, { persist: true }); loadDashboard(); });

const detailDrawer = createDetailDrawer({ onError: () => setConnection("offline") });
// The dialog itself is owned by `job-progress.js` (one instance for the whole
// app — see that module); this page only contributes its own callbacks.
configureJobProgress({
  getDashboard: () => getState().dashboard,
  onFinished: async (job) => {
    await loadDashboard({ quiet: true });
    const audit = (job.cells || []).length > 0 && job.cells.every((cell) => cell.provider === "tech_audit");
    toast(audit ? "Technical audit cards updated." : "Client data updated.");
  },
  onToast: toast,
});
const syncModal = getJobProgress();

function setConnection(state) {
  const element = document.querySelector("#connection-status");
  element.dataset.state = state;
  element.querySelector("span:last-child").textContent = state === "online" ? "API online" : "API unavailable";
}

async function loadDashboard({ quiet = false } = {}) {
  const token = ++requestToken;
  const state = getState();
  if (!quiet) {
    setState({ loading: true, error: null });
    dashboardState.innerHTML = "";
    grid.setAttribute("aria-busy", "true");
    grid.innerHTML = renderSkeletons(innerWidth > 1279 ? 4 : innerWidth > 720 ? 2 : 1);
  }
  try {
    // task-36: один портфельний запит критичних відкритих змін монітора,
    // паралельно з `getDashboard` — не по картці (task-36 §3.4). Збій цього
    // запиту не має валити дашборд: бейджі просто не з'являться.
    const [response, criticalChanges] = await Promise.all([
      getDashboard({ period: state.period, query: state.query, status: state.status, provider: state.provider, sort: state.sort }),
      listPortfolioMonitorChanges({ severity: "critical", state: "open", limit: 500 }).catch(() => []),
    ]);
    if (token !== requestToken) return;
    updateGroupCounts(response.clients || []);
    updateClientStatusCounts(response.clients || []);
    const monitorCriticalByClient = new Map();
    criticalChanges.forEach((change) => {
      monitorCriticalByClient.set(change.client_id, (monitorCriticalByClient.get(change.client_id) || 0) + 1);
    });
    // Статус фільтрується тут, а не через `client_status` в запиті (той
    // параметр існує для API/глибоких посилань): інакше відповідь уже не
    // містила б паузованих, і лічильник «Paused» показував би 0 — тобто
    // фільтр брехав би про те, що сам ховає. Той самий підхід, що й у
    // портфельних групах вище.
    const visible = filterByClientStatus(response.clients || [], state.clientStatus);
    const dashboard = {
      ...response,
      clients: filterByClientGroup(visible, state.group),
      // Скільки клієнтів сховав саме статус-фільтр — щоб рядок результатів
      // сказав це вголос, а не лишив оператора гадати, куди подівся клієнт.
      hidden_paused_count: state.clientStatus === "active" ? (response.clients || []).filter(isPaused).length : 0,
      monitor_critical_by_client: monitorCriticalByClient,
    };
    setState({ dashboard, loading: false, error: null });
    setConnection("online");
    render(dashboard);
  } catch (error) {
    if (error.name === "AbortError" || token !== requestToken) return;
    setState({ loading: false, error });
    setConnection("offline");
    renderError(error);
  }
}

function render(dashboard) {
  const clients = dashboard.clients || [];
  grid.setAttribute("aria-busy", "false");
  document.querySelector("#period-copy").textContent = `Current: ${formatRange(dashboard.current_range)} · Comparison: ${formatRange(dashboard.previous_range)}`;
  document.querySelector("#summary-total").textContent = clients.length;
  const hasMetrics = (client) => client.providers.some((provider) => provider.metrics?.length);
  document.querySelector("#summary-ok").textContent = clients.filter(hasMetrics).length;
  document.querySelector("#summary-attention").textContent = clients.filter((client) => !hasMetrics(client)).length;
  const latestDataAt = clients.reduce((latest, client) => {
    const timestamp = client.last_synced_at ? Date.parse(client.last_synced_at) : NaN;
    return Number.isFinite(timestamp) && timestamp > latest ? timestamp : latest;
  }, 0);
  document.querySelector("#summary-updated").textContent = formatTimestamp(latestDataAt ? new Date(latestDataAt).toISOString() : null, "—");
  const hiddenPaused = dashboard.hidden_paused_count || 0;
  document.querySelector("#result-count").textContent = hiddenPaused
    ? `${clients.length} ${clientWord(clients.length)} · ${hiddenPaused} paused hidden`
    : `${clients.length} ${clientWord(clients.length)}`;
  dashboardState.innerHTML = "";
  if (!clients.length) {
    grid.innerHTML = "";
    dashboardState.innerHTML = `<div class="state-panel"><span>${icon("database-off")}</span><p><strong>No clients found.</strong><br>Reset the filters or add a client via the API.</p><button class="button button--quiet" type="button" data-reset>Reset filters</button></div>`;
    dashboardState.querySelector("[data-reset]").addEventListener("click", clearFilters);
    return;
  }
  grid.innerHTML = clients.map((client) => renderClientCard(client, dashboard)).join("");
}

function filterByClientStatus(clients, wanted) {
  if (!wanted || wanted === "all") return clients;
  return clients.filter((client) => (isPaused(client) ? "paused" : "active") === wanted);
}

function updateClientStatusCounts(clients) {
  const paused = clients.filter(isPaused).length;
  document.querySelector("#client-status-count-all").textContent = clients.length;
  document.querySelector("#client-status-count-active").textContent = clients.length - paused;
  document.querySelector("#client-status-count-paused").textContent = paused;
}

function updateGroupCounts(clients) {
  const counts = { all: clients.length, vision: 0, vertex: 0 };
  clients.forEach((client) => {
    const group = clientGroup(client);
    if (group) counts[group] += 1;
  });
  Object.entries(counts).forEach(([group, count]) => {
    document.querySelector(`#group-count-${group}`).textContent = count;
  });
}

function renderError(error) {
  grid.setAttribute("aria-busy", "false");
  grid.innerHTML = "";
  dashboardState.innerHTML = `<div class="state-panel state-failed">${icon("wifi-off")}<p><strong>Failed to load dashboard.</strong><br>${escapeHtml(error.message)}. Previous values were not replaced with zeros.</p><button class="button button--primary" type="button" data-retry>Try again</button></div>`;
  dashboardState.querySelector("[data-retry]").addEventListener("click", () => loadDashboard());
}

function clientWord(count) {
  return count === 1 ? "client" : "clients";
}

function clearFilters() {
  search.value = ""; statusFilter.value = ""; providerFilter.value = "";
  setState({ query: "", status: "", provider: "" }, { persist: true });
  loadDashboard();
}

// One implementation, shared with every other page through `job-progress.js`.
function toast(message) {
  showToast(message);
}

// Copies the button's `data-copy` text and confirms without stealing focus:
// the icon flips to a check for a moment and a toast names what was copied.
// `writeText` needs a secure context (https/localhost); if it's unavailable
// or rejected, say so rather than pretending it worked.
async function copyToClipboard(button) {
  const value = button.dataset.copy;
  if (button.dataset.copyState === "done") return;
  try {
    await navigator.clipboard.writeText(value);
  } catch {
    toast("Could not copy — clipboard is unavailable in this browser.");
    return;
  }
  const original = button.innerHTML;
  button.dataset.copyState = "done";
  button.innerHTML = icon("copy-check", "icon icon--tiny");
  toast(`Copied ${value}`);
  setTimeout(() => {
    button.innerHTML = original;
    delete button.dataset.copyState;
  }, 1400);
}

grid.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-action]");
  if (!button) return;
  if (button.dataset.action === "login") {
    if (button.getAttribute("aria-disabled") !== "true") handleProviderLogin(button.dataset.provider, button);
    return;
  }
  if (button.dataset.action === "copy") {
    copyToClipboard(button);
    return;
  }
  const card = button.closest("[data-client-id]");
  const client = getState().dashboard?.clients.find((item) => String(item.client_id) === card.dataset.clientId);
  if (!client) return;
  if (button.dataset.action === "client-status") {
    openClientStatusDialog({ client, trigger: button, onSaved: onClientStatusSaved });
    return;
  }
  if (button.dataset.action === "detail") detailDrawer.open(client, getState().period, button);
  // Збір для паузованого клієнта не заборонений — але й не стається «просто
  // так»: одне підтвердження відділяє свідомий намір від автопілоту.
  if (button.dataset.action === "sync") {
    if (isPaused(client) && !confirm(`${client.name} is paused. Collect data for it anyway?`)) return;
    syncModal.openSelection({ clientId: client.client_id, trigger: button });
  }
  if (button.dataset.action === "sync-provider") syncModal.openSelection({ clientId: client.client_id, provider: button.dataset.provider, trigger: button });
  if (button.dataset.action === "integrate") openIntegration(client, button.dataset.provider, button);
});

// task-22: standalone «Log in» on the client card's auth_required badge —
// runs outside any sync job, so unlike the modal (which leans on the job's
// own SSE stream), this is the one place that actually needs
// `watchProviderSession`'s polling to learn the session came back alive.
async function handleProviderLogin(provider, button) {
  const label = button.querySelector(".source-setup__action");
  const originalLabel = label.textContent;
  button.setAttribute("aria-disabled", "true");
  label.textContent = "Waiting for login…";
  const result = await triggerLogin(provider);
  const providerLabel = PROVIDERS[provider]?.label || provider;
  if (!result.ok) {
    button.removeAttribute("aria-disabled");
    label.textContent = originalLabel;
    toast(result.reason === "no_warm_chrome"
      ? `No access to a warm Chrome for ${providerLabel}.`
      : `Could not start login: ${result.message}`);
    return;
  }
  watchProviderSession(provider, {
    onChange(state) {
      if (state !== "alive") return;
      button.removeAttribute("aria-disabled");
      label.textContent = originalLabel;
      toast(`${providerLabel} session restored. Run a collection to refresh the data.`);
      loadDashboard({ quiet: true });
    },
  });
}

document.querySelector("#sync-all-button").addEventListener("click", (event) => syncModal.openSelection({ trigger: event.currentTarget }));
document.querySelector("#audit-all-button").addEventListener("click", (event) => syncModal.openAuditSelection({ trigger: event.currentTarget }));
document.querySelector("#clear-filters").addEventListener("click", clearFilters);
document.querySelectorAll("[data-period]").forEach((button) => button.addEventListener("click", () => {
  document.querySelectorAll("[data-period]").forEach((item) => item.setAttribute("aria-pressed", String(item === button)));
  setState({ period: button.dataset.period }, { persist: true });
  loadDashboard();
}));
function onClientStatusSaved(updated) {
  const paused = updated?.status === "paused";
  toast(paused
    ? `${updated.name} is paused. Bulk collections will skip it.`
    : `${updated?.name || "Client"} is active again and back in bulk collections.`);
  loadDashboard({ quiet: true });
}

document.querySelectorAll("[data-client-status]").forEach((button) => button.addEventListener("click", () => {
  document.querySelectorAll("[data-client-status]").forEach((item) => item.setAttribute("aria-pressed", String(item === button)));
  setState({ clientStatus: button.dataset.clientStatus }, { persist: true });
  loadDashboard();
}));
document.querySelectorAll("[data-group]").forEach((button) => button.addEventListener("click", () => {
  document.querySelectorAll("[data-group]").forEach((item) => item.setAttribute("aria-pressed", String(item === button)));
  const group = button.dataset.group;
  document.querySelector("#group-filter-copy").textContent = group === "all" ? "All client accounts" : `${group === "vision" ? "Vision" : "Vertex"} accounts`;
  setState({ group }, { persist: true });
  loadDashboard();
}));
search.addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => { setState({ query: search.value.trim() }, { persist: true }); loadDashboard(); }, 280);
});
statusFilter.addEventListener("change", () => { setState({ status: statusFilter.value }, { persist: true }); loadDashboard(); });
providerFilter.addEventListener("change", () => { setState({ provider: providerFilter.value }, { persist: true }); loadDashboard(); });

const integrationDialog = document.querySelector("#integration-dialog");
const integrationForm = document.querySelector("#integration-form");
let integrationContext = null;

const discoveryBlock = document.querySelector("#integration-discovery");
const discoveryStatus = document.querySelector("#integration-status");
const discoveryDetail = document.querySelector("#integration-detail");
const discoveryChoices = document.querySelector("#integration-choices");
const discoveryChoiceList = document.querySelector("#integration-choice-list");
const discoveryChoiceEmpty = document.querySelector("#integration-choice-empty");
const discoverySearch = document.querySelector("#integration-search");
const discoveryShowAll = document.querySelector("#integration-show-all");
const manualBlock = document.querySelector("#integration-manual");
const manualToggle = document.querySelector("#integration-manual-toggle");
const connectButton = document.querySelector("#integration-connect");
const collectButton = document.querySelector("#integration-collect");
const saveButton = document.querySelector("#integration-save");

// Spelled out rather than left to colour alone (AGENTS §7).
const CONFIDENCE_LABEL = { exact: "Exact", strong: "Likely", weak: "Possible" };

function setDiscoveryState(stateClass, message) {
  discoveryBlock.hidden = false;
  discoveryBlock.className = `discovery ${stateClass}`;
  discoveryStatus.textContent = message;
}

function setIntegrationFooter({ manual = false, connect = false, collect = false, save = false } = {}) {
  manualToggle.hidden = !manual;
  connectButton.hidden = !connect;
  collectButton.hidden = !collect;
  saveButton.hidden = !save;
}

function renderDiscoveryDetail(rows) {
  discoveryDetail.innerHTML = rows.map(([term, value]) => `<dt>${escapeHtml(term)}</dt><dd>${escapeHtml(value)}</dd>`).join("");
  discoveryDetail.hidden = !rows.length;
}

// The provider can expose dozens of accounts. Only plausible ones are listed up
// front; the rest stay behind "show all" and the search box, so a genuine match
// is not buried under twenty unrelated properties.
let candidateView = { all: [], showAll: false, query: "" };

function visibleCandidates() {
  const { all, showAll, query } = candidateView;
  const needle = query.trim().toLowerCase();
  // Typing searches the whole inventory: making the operator press "show all"
  // before they can search for a name they already know would be busywork.
  // `!== false` rather than a truthy check: the DTO defaults `suggested` to
  // true, so an absent field means "show it", not "hide it".
  const pool = showAll || needle ? all : all.filter((candidate) => candidate.suggested !== false);
  if (!needle) return pool;
  return pool.filter((candidate) => `${candidate.label} ${candidate.detail}`.toLowerCase().includes(needle));
}

function renderCandidateList() {
  const visible = visibleCandidates();
  // Preselect only a candidate we actually believe in. When nothing matched,
  // the list is just the connection's inventory — checking its first row would
  // invite a one-click connection to an unrelated property.
  const preselect = visible[0] && visible[0].confidence !== "weak" ? visible[0].index : -1;
  discoveryChoiceList.innerHTML = visible.map((candidate) => `
    <label class="choice">
      <input type="radio" name="integration-candidate" value="${candidate.index}"${candidate.index === preselect ? " checked" : ""}>
      <span class="choice__body">
        <strong>${escapeHtml(candidate.label)}</strong>
        <small>${escapeHtml(candidate.detail)} · ${escapeHtml(candidate.reason)}</small>
      </span>
      <span class="choice__badge confidence-${candidate.confidence}">${escapeHtml(CONFIDENCE_LABEL[candidate.confidence] || candidate.confidence)}</span>
    </label>`).join("");

  const hiddenCount = candidateView.all.filter((item) => item.suggested === false).length;
  discoveryShowAll.hidden = hiddenCount === 0;
  discoveryShowAll.textContent = candidateView.showAll
    ? "Show likely matches only"
    : `Show all ${candidateView.all.length} accounts`;
  discoveryShowAll.setAttribute("aria-expanded", String(candidateView.showAll));

  discoveryChoiceEmpty.hidden = visible.length > 0;
  if (!visible.length) {
    discoveryChoiceEmpty.textContent = candidateView.query.trim()
      ? `No account matches “${candidateView.query.trim()}”.`
      : "No likely match — search, or show all accounts in this connection.";
  }
}

function renderCandidates(candidates) {
  // The index is carried on the candidate because filtering reorders the list,
  // while the radio value has to keep pointing at the original entry.
  candidateView = {
    all: candidates.map((candidate, index) => ({ ...candidate, index })),
    showAll: false,
    query: "",
  };
  discoverySearch.value = "";
  renderCandidateList();
  discoveryChoices.hidden = false;
}

// The account reference we just stored, plus whether probing it actually
// worked — "saved" and "verified" are different claims and the report must not
// blur them (a reference can be well-formed and still point at nothing).
function connectedDetail(result) {
  const config = result.applied?.integration_config || {};
  const rows = Object.entries(config).map(([key, value]) => [key.replace(/_/g, " "), String(value)]);
  if (result.preflight) {
    rows.push(["verification", result.preflight.ok ? "passed" : (result.preflight.reason_code || "failed")]);
  }
  return rows;
}

function showConnected(result) {
  setDiscoveryState("is-connected", result.message);
  renderDiscoveryDetail(connectedDetail(result));
  discoveryChoices.hidden = true;
  manualBlock.hidden = true;
  setIntegrationFooter({ collect: true });
}

function renderDiscovery(result) {
  integrationContext.candidates = result.candidates || [];
  if (result.status === "connected" || result.status === "already_configured") {
    integrationContext.changed = result.status === "connected";
    showConnected(result);
    return;
  }
  renderDiscoveryDetail([]);
  if (result.status === "needs_choice") {
    setDiscoveryState("is-choice", result.message);
    renderCandidates(integrationContext.candidates);
    setIntegrationFooter({ manual: true, connect: true });
    return;
  }
  // not_found / unavailable — every outcome still offers a way forward.
  discoveryChoices.hidden = true;
  setDiscoveryState(result.status === "unavailable" ? "is-error" : "is-empty", result.message);
  setIntegrationFooter({ manual: true });
}

function showManualEntry() {
  discoveryBlock.hidden = true;
  discoveryChoices.hidden = true;
  manualBlock.hidden = false;
  setIntegrationFooter({ save: true });
  // The `hidden` attribute sits on the `.form-field` label, not the input, so
  // picking the first `input` would silently focus an invisible one and drop
  // the keyboard user out of the dialog entirely.
  const field = [...integrationForm.querySelectorAll(".form-field")].find((item) => !item.hidden);
  field?.querySelector("input")?.focus();
}

// task-33: the operator asks for a connection, not for a form. Look the account
// up first and only fall back to manual entry when the lookup cannot answer.
async function openIntegration(client, provider, trigger) {
  integrationContext = { client, provider, trigger, candidates: [] };
  const providerLabel = PROVIDERS[provider]?.label || provider;
  document.querySelector("#integration-title").textContent = `Connect ${providerLabel}`;
  document.querySelector("#integration-lead").textContent = `${client.name} — ${client.domain}. Tokens and passwords are never stored in the browser.`;
  document.querySelector("#integration-error").textContent = "";
  integrationForm.reset();
  integrationForm.elements.domain.value = client.domain;
  const usesDomain = ["ahrefs", "semrush"].includes(provider);
  integrationForm.querySelector("[data-domain-field]").hidden = !usesDomain;
  integrationForm.querySelectorAll("[data-account-field]").forEach((field) => { field.hidden = usesDomain; });

  manualBlock.hidden = true;
  discoveryChoices.hidden = true;
  renderDiscoveryDetail([]);
  setDiscoveryState("is-searching", `Searching ${providerLabel} for ${client.domain}…`);
  setIntegrationFooter();
  integrationDialog.showModal();

  try {
    renderDiscovery(await discoverIntegration(client.client_id, provider));
  } catch (error) {
    setDiscoveryState("is-error", `Could not reach the server: ${error.message}`);
    setIntegrationFooter({ manual: true });
  }
}

manualToggle.addEventListener("click", showManualEntry);

discoverySearch.addEventListener("input", () => {
  candidateView.query = discoverySearch.value;
  renderCandidateList();
});
discoveryShowAll.addEventListener("click", () => {
  candidateView.showAll = !candidateView.showAll;
  renderCandidateList();
});

connectButton.addEventListener("click", async () => {
  const selected = discoveryChoiceList.querySelector("input:checked");
  const candidate = integrationContext.candidates[Number(selected?.value)];
  if (!candidate) {
    document.querySelector("#integration-error").textContent = "Pick an account first.";
    return;
  }
  connectButton.disabled = true;
  try {
    await updateIntegration(integrationContext.client.client_id, integrationContext.provider, candidate.integration_config, { sourceMode: candidate.source_mode });
    integrationContext.changed = true;
    showConnected({ message: `Connected to ${candidate.label}.`, applied: { integration_config: candidate.integration_config } });
  } catch (error) {
    document.querySelector("#integration-error").textContent = error.message;
  } finally { connectButton.disabled = false; }
});

collectButton.addEventListener("click", () => {
  const { client, provider, trigger } = integrationContext;
  integrationDialog.close();
  syncModal.openSelection({ clientId: client.client_id, provider, trigger });
});
integrationDialog.querySelectorAll("[data-close]").forEach((button) => button.addEventListener("click", () => integrationDialog.close()));
integrationDialog.addEventListener("close", () => {
  // Focus goes home first: refreshing the grid replaces the very button we
  // have to return focus to, so the reload waits until the dialog is gone.
  integrationContext?.trigger?.focus();
  if (integrationContext?.changed) loadDashboard({ quiet: true });
});
integrationForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const submit = integrationForm.querySelector('[type="submit"]');
  const data = new FormData(integrationForm);
  const payload = Object.fromEntries([...data.entries()].filter(([, value]) => value));
  const usesDomain = ["ahrefs", "semrush"].includes(integrationContext.provider);
  const invalid = usesDomain ? !payload.domain : (!payload.connection_key || !payload.account_id);
  if (invalid) {
    document.querySelector("#integration-error").textContent = usesDomain ? "Enter the client's domain." : "Enter both the connection key and account ID.";
    return;
  }
  submit.disabled = true;
  try {
    await updateIntegration(integrationContext.client.client_id, integrationContext.provider, payload);
    integrationContext.changed = true;
    integrationDialog.close(); toast("Integration saved.");
  } catch (error) { document.querySelector("#integration-error").textContent = error.message; }
  finally { submit.disabled = false; }
});

const initial = getState();
search.value = initial.query; statusFilter.value = initial.status; providerFilter.value = initial.provider; sortSelect.value = initial.sort;
document.querySelectorAll("[data-period]").forEach((button) => button.setAttribute("aria-pressed", String(button.dataset.period === initial.period)));
document.querySelectorAll("[data-group]").forEach((button) => button.setAttribute("aria-pressed", String(button.dataset.group === initial.group)));
document.querySelectorAll("[data-client-status]").forEach((button) => button.setAttribute("aria-pressed", String(button.dataset.clientStatus === initial.clientStatus)));
document.querySelector("#group-filter-copy").textContent = initial.group === "all" ? "All client accounts" : `${initial.group === "vision" ? "Vision" : "Vertex"} accounts`;
loadDashboard();
