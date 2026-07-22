const API_ROOT = "/api/v1";
const FIXTURE_MODE = new URLSearchParams(location.search).get("fixture");
const controllers = new Map();

async function request(path, options = {}, key = path) {
  controllers.get(key)?.abort();
  const controller = new AbortController();
  controllers.set(key, controller);
  try {
    const response = await fetch(`${API_ROOT}${path}`, {
      ...options,
      signal: options.signal || controller.signal,
      headers: { "Content-Type": "application/json", Accept: "application/json", ...options.headers },
    });
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      const error = new Error(payload.detail?.message || payload.detail || `HTTP ${response.status}`);
      error.status = response.status;
      error.code = payload.detail?.code;
      throw error;
    }
    return response.status === 204 ? null : response.json();
  } finally {
    if (controllers.get(key) === controller) controllers.delete(key);
  }
}

async function fixture(name) {
  const response = await fetch(`/fixtures/${name}.json`);
  if (!response.ok) throw new Error("Не вдалося завантажити тестові дані");
  return response.json();
}

function wait(ms) { return new Promise((resolve) => setTimeout(resolve, ms)); }

export async function getDashboard({ period, query, status, provider }) {
  if (FIXTURE_MODE) {
    if (FIXTURE_MODE === "error") { await wait(500); throw new Error("Тестова помилка з’єднання"); }
    if (FIXTURE_MODE === "loading") await wait(2400);
    const payload = await fixture(FIXTURE_MODE === "empty" ? "dashboard-empty" : "dashboard-all-states");
    const normalizedQuery = query?.toLocaleLowerCase("uk-UA") || "";
    const clients = payload.clients.filter((client) => {
      const matchesQuery = !normalizedQuery || `${client.name} ${client.domain}`.toLocaleLowerCase("uk-UA").includes(normalizedQuery);
      const matchesStatus = !status || client.overall_status === status;
      const matchesProvider = !provider || client.providers.some((item) => item.provider === provider);
      return matchesQuery && matchesStatus && matchesProvider;
    });
    return { ...payload, clients, period_kind: period };
  }
  const params = new URLSearchParams({ period });
  if (query) params.set("q", query);
  if (status) params.set("status", status);
  if (provider) params.set("provider", provider);
  return request(`/dashboard?${params}`, {}, "dashboard");
}

export async function getClientHistory(clientId, period = "week") {
  if (FIXTURE_MODE) {
    const payload = await fixture("history");
    return { ...payload, client_id: clientId, period_kind: period };
  }
  return request(`/clients/${clientId}/metrics?period=${period}&history=12`, {}, "history");
}

function readMockJobs() {
  try { return JSON.parse(localStorage.getItem("theyovth.mock-jobs.v1")) || {}; }
  catch { return {}; }
}

function writeMockJobs(jobs) { localStorage.setItem("theyovth.mock-jobs.v1", JSON.stringify(jobs)); }

function mockCellStatus(cell, elapsed) {
  if (cell.status === "not_configured" || cell.status === "cancelled") return cell;
  const offset = cell.provider_index * 300 + cell.client_index * 1350;
  const progress = Math.max(0, Math.min(100, Math.floor((elapsed - offset) / 38)));
  if (progress <= 0) return { ...cell, status: "queued", progress_percent: 0, current_stage: "queued" };
  const stages = ["preflight", "authenticate", "fetch_current", "fetch_previous", "normalize", "validate", "persist"];
  const stage = stages[Math.min(stages.length - 1, Math.floor(progress / 15))];
  if (progress >= 100) {
    const demoFailure = cell.provider === "semrush" && cell.client_index === 1 && !cell.retried;
    return { ...cell, status: demoFailure ? "failed" : "succeeded", progress_percent: 100, current_stage: "complete", error_code: demoFailure ? "RATE_LIMITED" : null };
  }
  return { ...cell, status: "running", progress_percent: progress, current_stage: stage };
}

export async function createSyncJob(payload, dashboard) {
  if (!FIXTURE_MODE) return request("/sync-jobs", { method: "POST", body: JSON.stringify(payload) });
  const id = `fixture-${Date.now()}`;
  const clientIds = payload.client_ids === "all" ? dashboard.clients.map((client) => client.client_id) : payload.client_ids;
  const providers = payload.providers === "all" ? ["ahrefs", "semrush", "gsc", "reporting_ninja"] : payload.providers;
  const cells = [];
  providers.forEach((provider, providerIndex) => clientIds.forEach((clientId, clientIndex) => {
    const client = dashboard.clients.find((item) => item.client_id === clientId);
    const configured = client?.providers?.find((item) => item.provider === provider)?.state !== "not_configured";
    cells.push({ client_id: clientId, provider, provider_index: providerIndex, client_index: clientIndex, status: configured ? "queued" : "not_configured", progress_percent: 0, current_stage: "queued" });
  }));
  const jobs = readMockJobs();
  jobs[id] = { id, batch_id: id, job_id: id, status: "queued", started_at: Date.now(), cells, client_ids: clientIds, providers };
  writeMockJobs(jobs);
  return { batch_id: id, job_id: id, status_url: `/api/v1/sync-jobs/${id}`, events_url: `/api/v1/sync-jobs/${id}/events` };
}

export async function getSyncJob(id) {
  if (!FIXTURE_MODE) return request(`/sync-jobs/${id}`, {}, `job-${id}`);
  const jobs = readMockJobs();
  const job = jobs[id];
  if (!job) throw new Error("Оновлення не знайдено");
  if (["cancelled", "succeeded", "partial", "failed"].includes(job.status)) return job;
  const elapsed = Date.now() - job.started_at;
  const cells = job.cells.map((cell) => mockCellStatus(cell, elapsed));
  const actionable = cells.filter((cell) => cell.status !== "not_configured");
  const completed = actionable.filter((cell) => ["succeeded", "failed"].includes(cell.status));
  const progress = actionable.length ? Math.round(actionable.reduce((sum, cell) => sum + cell.progress_percent, 0) / actionable.length) : 100;
  const status = completed.length === actionable.length ? (completed.some((cell) => cell.status === "failed") ? "partial" : "succeeded") : "running";
  jobs[id] = { ...job, cells, status, progress_percent: progress };
  writeMockJobs(jobs);
  return jobs[id];
}

export async function cancelSyncJob(id) {
  if (!FIXTURE_MODE) return request(`/sync-jobs/${id}/cancel`, { method: "POST", body: "{}" });
  const jobs = readMockJobs();
  const job = jobs[id];
  jobs[id] = { ...job, status: "cancelled", cells: job.cells.map((cell) => ["queued", "running"].includes(cell.status) ? { ...cell, status: "cancelled" } : cell) };
  writeMockJobs(jobs);
  return jobs[id];
}

export async function retrySyncJob(id) {
  if (!FIXTURE_MODE) return request(`/sync-jobs/${id}/retry`, { method: "POST", body: "{}" });
  const jobs = readMockJobs();
  const previous = jobs[id];
  const retryId = `fixture-retry-${Date.now()}`;
  const failed = previous.cells.filter((cell) => cell.status === "failed").map((cell) => ({ ...cell, status: "queued", progress_percent: 0, retried: true }));
  jobs[retryId] = { ...previous, id: retryId, batch_id: retryId, job_id: retryId, status: "queued", started_at: Date.now(), cells: failed };
  writeMockJobs(jobs);
  return { batch_id: retryId, job_id: retryId, status_url: `/api/v1/sync-jobs/${retryId}`, events_url: `/api/v1/sync-jobs/${retryId}/events` };
}

export function updateIntegration(clientId, provider, payload) {
  if (FIXTURE_MODE) return wait(450).then(() => ({ client_id: clientId, provider, enabled: true, config: payload }));
  return request(`/clients/${clientId}/integrations/${provider}`, { method: "PUT", body: JSON.stringify({ enabled: true, source_mode: "auto", integration_config: payload }) });
}

export function isFixtureMode() { return Boolean(FIXTURE_MODE); }
