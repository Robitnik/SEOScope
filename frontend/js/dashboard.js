import { getDashboard, updateIntegration } from "./api.js";
import { clientGroup, filterByClientGroup } from "./client-groups.js";
import { renderClientCard, renderSkeletons } from "./client-card.js";
import { createDetailDrawer } from "./detail-drawer.js";
import { PROVIDERS, escapeHtml, formatRange, formatTimestamp, icon } from "./formatters.js";
import { triggerLogin, watchProviderSession } from "./session-status.js";
import { getState, setState } from "./store.js";
import { createSyncModal } from "./sync-modal.js";

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
const syncModal = createSyncModal({
  getDashboard: () => getState().dashboard,
  onFinished: async () => { await loadDashboard({ quiet: true }); toast("Client data updated."); },
  onToast: toast,
});

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
    const response = await getDashboard({ period: state.period, query: state.query, status: state.status, provider: state.provider, sort: state.sort });
    if (token !== requestToken) return;
    updateGroupCounts(response.clients || []);
    const dashboard = { ...response, clients: filterByClientGroup(response.clients || [], state.group) };
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
  document.querySelector("#result-count").textContent = `${clients.length} ${clientWord(clients.length)}`;
  dashboardState.innerHTML = "";
  if (!clients.length) {
    grid.innerHTML = "";
    dashboardState.innerHTML = `<div class="state-panel"><span>${icon("database-off")}</span><p><strong>No clients found.</strong><br>Reset the filters or add a client via the API.</p><button class="button button--quiet" type="button" data-reset>Reset filters</button></div>`;
    dashboardState.querySelector("[data-reset]").addEventListener("click", clearFilters);
    return;
  }
  grid.innerHTML = clients.map((client) => renderClientCard(client, dashboard)).join("");
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

function toast(message) {
  const region = document.querySelector("#toast-region");
  const element = document.createElement("div");
  element.className = "toast-message";
  element.textContent = message;
  region.append(element);
  setTimeout(() => element.remove(), 4200);
}

grid.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-action]");
  if (!button) return;
  if (button.dataset.action === "login") {
    if (button.getAttribute("aria-disabled") !== "true") handleProviderLogin(button.dataset.provider, button);
    return;
  }
  const card = button.closest("[data-client-id]");
  const client = getState().dashboard?.clients.find((item) => String(item.client_id) === card.dataset.clientId);
  if (!client) return;
  if (button.dataset.action === "detail") detailDrawer.open(client, getState().period, button);
  if (button.dataset.action === "sync") syncModal.openSelection({ clientId: client.client_id, trigger: button });
  if (button.dataset.action === "sync-provider") syncModal.openSelection({ clientId: client.client_id, provider: button.dataset.provider, trigger: button });
  if (button.dataset.action === "integrate") openIntegration(client, button.dataset.provider, button);
});

// task-22: standalone «Увійти» on the client card's auth_required badge —
// runs outside any sync job, so unlike the modal (which leans on the job's
// own SSE stream), this is the one place that actually needs
// `watchProviderSession`'s polling to learn the session came back alive.
async function handleProviderLogin(provider, button) {
  const label = button.querySelector(".source-setup__action");
  const originalLabel = label.textContent;
  button.setAttribute("aria-disabled", "true");
  label.textContent = "Очікуємо входу…";
  const result = await triggerLogin(provider);
  const providerLabel = PROVIDERS[provider]?.label || provider;
  if (!result.ok) {
    button.removeAttribute("aria-disabled");
    label.textContent = originalLabel;
    toast(result.reason === "no_warm_chrome"
      ? `Немає доступу до теплого Chrome для ${providerLabel}.`
      : `Не вдалося запустити вхід: ${result.message}`);
    return;
  }
  watchProviderSession(provider, {
    onChange(state) {
      if (state !== "alive") return;
      button.removeAttribute("aria-disabled");
      label.textContent = originalLabel;
      toast(`Сесію ${providerLabel} відновлено. Запустіть збір, щоб оновити дані.`);
      loadDashboard({ quiet: true });
    },
  });
}

document.querySelector("#sync-all-button").addEventListener("click", (event) => syncModal.openSelection({ trigger: event.currentTarget }));
document.querySelector("#clear-filters").addEventListener("click", clearFilters);
document.querySelectorAll("[data-period]").forEach((button) => button.addEventListener("click", () => {
  document.querySelectorAll("[data-period]").forEach((item) => item.setAttribute("aria-pressed", String(item === button)));
  setState({ period: button.dataset.period }, { persist: true });
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

function openIntegration(client, provider, trigger) {
  integrationContext = { client, provider, trigger };
  document.querySelector("#integration-title").textContent = `Connect ${PROVIDERS[provider]?.label || provider}`;
  document.querySelector("#integration-lead").textContent = `${client.name}: provide a reference to a server-side connection. Tokens and passwords are never stored in the browser.`;
  document.querySelector("#integration-error").textContent = "";
  integrationForm.reset();
  integrationForm.elements.domain.value = client.domain;
  const usesDomain = ["ahrefs", "semrush"].includes(provider);
  integrationForm.querySelector("[data-domain-field]").hidden = !usesDomain;
  integrationForm.querySelectorAll("[data-account-field]").forEach((field) => { field.hidden = usesDomain; });
  integrationDialog.showModal();
}
integrationDialog.querySelectorAll("[data-close]").forEach((button) => button.addEventListener("click", () => integrationDialog.close()));
integrationDialog.addEventListener("close", () => integrationContext?.trigger?.focus());
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
    integrationDialog.close(); toast("Integration saved."); await loadDashboard({ quiet: true });
  } catch (error) { document.querySelector("#integration-error").textContent = error.message; }
  finally { submit.disabled = false; }
});

const initial = getState();
search.value = initial.query; statusFilter.value = initial.status; providerFilter.value = initial.provider; sortSelect.value = initial.sort;
document.querySelectorAll("[data-period]").forEach((button) => button.setAttribute("aria-pressed", String(button.dataset.period === initial.period)));
document.querySelectorAll("[data-group]").forEach((button) => button.setAttribute("aria-pressed", String(button.dataset.group === initial.group)));
document.querySelector("#group-filter-copy").textContent = initial.group === "all" ? "All client accounts" : `${initial.group === "vision" ? "Vision" : "Vertex"} accounts`;
loadDashboard();
