import { getDashboard, updateIntegration } from "./api.js";
import { renderClientCard, renderSkeletons } from "./client-card.js";
import { createDetailDrawer } from "./detail-drawer.js";
import { PROVIDERS, formatRange, formatTimestamp, icon } from "./formatters.js";
import { getState, setState } from "./store.js";
import { createSyncModal } from "./sync-modal.js";

const grid = document.querySelector("#client-grid");
const dashboardState = document.querySelector("#dashboard-state");
const search = document.querySelector("#client-search");
const statusFilter = document.querySelector("#status-filter");
const providerFilter = document.querySelector("#provider-filter");
let requestToken = 0;
let searchTimer = null;

const detailDrawer = createDetailDrawer({ onError: () => setConnection("offline") });
const syncModal = createSyncModal({
  getDashboard: () => getState().dashboard,
  onFinished: async () => { await loadDashboard({ quiet: true }); toast("Дані клієнтів оновлено."); },
  onToast: toast,
});

function setConnection(state) {
  const element = document.querySelector("#connection-status");
  element.dataset.state = state;
  element.querySelector("span:last-child").textContent = state === "online" ? "API онлайн" : "API недоступний";
}

async function loadDashboard({ quiet = false } = {}) {
  const token = ++requestToken;
  const state = getState();
  if (!quiet) {
    setState({ loading: true, error: null });
    dashboardState.innerHTML = "";
    grid.setAttribute("aria-busy", "true");
    grid.innerHTML = renderSkeletons(innerWidth > 1050 ? 2 : 1);
  }
  try {
    const dashboard = await getDashboard({ period: state.period, query: state.query, status: state.status, provider: state.provider });
    if (token !== requestToken) return;
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
  document.querySelector("#period-copy").textContent = `Поточний: ${formatRange(dashboard.current_range)} · Порівняння: ${formatRange(dashboard.previous_range)}`;
  document.querySelector("#summary-total").textContent = clients.length;
  document.querySelector("#summary-ok").textContent = clients.filter((client) => client.overall_status === "ok").length;
  document.querySelector("#summary-attention").textContent = clients.filter((client) => client.overall_status !== "ok").length;
  document.querySelector("#summary-updated").textContent = formatTimestamp(dashboard.generated_at, "—");
  document.querySelector("#result-count").textContent = `${clients.length} ${clientWord(clients.length)}`;
  dashboardState.innerHTML = "";
  if (!clients.length) {
    grid.innerHTML = "";
    dashboardState.innerHTML = `<div class="state-panel"><span>${icon("database-off")}</span><p><strong>Клієнтів за цим запитом немає.</strong><br>Скиньте фільтри або додайте клієнта через API.</p><button class="button button--quiet" type="button" data-reset>Скинути фільтри</button></div>`;
    dashboardState.querySelector("[data-reset]").addEventListener("click", clearFilters);
    return;
  }
  grid.innerHTML = clients.map((client) => renderClientCard(client, dashboard)).join("");
}

function renderError(error) {
  grid.setAttribute("aria-busy", "false");
  grid.innerHTML = "";
  dashboardState.innerHTML = `<div class="state-panel state-failed">${icon("wifi-off")}<p><strong>Dashboard не завантажено.</strong><br>${error.message}. Попередні значення не підмінено нулями.</p><button class="button button--primary" type="button" data-retry>Спробувати ще</button></div>`;
  dashboardState.querySelector("[data-retry]").addEventListener("click", () => loadDashboard());
}

function clientWord(count) {
  const mod10 = count % 10, mod100 = count % 100;
  if (mod10 === 1 && mod100 !== 11) return "клієнт";
  if ([2, 3, 4].includes(mod10) && ![12, 13, 14].includes(mod100)) return "клієнти";
  return "клієнтів";
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
  const card = button.closest("[data-client-id]");
  const client = getState().dashboard?.clients.find((item) => String(item.client_id) === card.dataset.clientId);
  if (!client) return;
  if (button.dataset.action === "detail") detailDrawer.open(client, getState().period, button);
  if (button.dataset.action === "sync") syncModal.openSelection({ clientId: client.client_id, trigger: button });
  if (button.dataset.action === "sync-provider") syncModal.openSelection({ clientId: client.client_id, provider: button.dataset.provider, trigger: button });
  if (button.dataset.action === "integrate") openIntegration(client, button.dataset.provider, button);
});

document.querySelector("#sync-all-button").addEventListener("click", (event) => syncModal.openSelection({ trigger: event.currentTarget }));
document.querySelector("#clear-filters").addEventListener("click", clearFilters);
document.querySelectorAll("[data-period]").forEach((button) => button.addEventListener("click", () => {
  document.querySelectorAll("[data-period]").forEach((item) => item.setAttribute("aria-pressed", String(item === button)));
  setState({ period: button.dataset.period }, { persist: true });
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
  document.querySelector("#integration-title").textContent = `Підключити ${PROVIDERS[provider]?.label || provider}`;
  document.querySelector("#integration-lead").textContent = `${client.name}: вкажіть посилання на серверне з’єднання. Токени та паролі у браузері не зберігаються.`;
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
    document.querySelector("#integration-error").textContent = usesDomain ? "Вкажіть домен клієнта." : "Вкажіть ключ з’єднання та ID акаунта.";
    return;
  }
  submit.disabled = true;
  try {
    await updateIntegration(integrationContext.client.client_id, integrationContext.provider, payload);
    integrationDialog.close(); toast("Інтеграцію збережено."); await loadDashboard({ quiet: true });
  } catch (error) { document.querySelector("#integration-error").textContent = error.message; }
  finally { submit.disabled = false; }
});

const initial = getState();
search.value = initial.query; statusFilter.value = initial.status; providerFilter.value = initial.provider;
document.querySelectorAll("[data-period]").forEach((button) => button.setAttribute("aria-pressed", String(button.dataset.period === initial.period)));
loadDashboard();
