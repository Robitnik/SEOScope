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
      error.code = payload.detail?.error_code;
      throw error;
    }
    return response.status === 204 ? null : response.json();
  } finally {
    if (controllers.get(key) === controller) controllers.delete(key);
  }
}

async function fixture(name) {
  const response = await fetch(`/fixtures/${name}.json`);
  if (!response.ok) throw new Error("Could not load fixture data");
  return response.json();
}

function wait(ms) { return new Promise((resolve) => setTimeout(resolve, ms)); }

// Mirrors the backend's default `quality` ordering (PLAN §21.3) closely enough
// for fixture/demo mode: coverage first, then status rank, then name. The
// other modes are simple single-key comparisons. Real ordering is always
// computed server-side (app/metrics/health.py); this only serves fixtures,
// which never reach that code.
function sortFixtureClients(clients, sort) {
  const qualityRank = { ok: 3, stale: 2, attention: 1, failed_all: 0 };
  const coverage = (client) => client.providers.filter((item) => item.state !== "not_configured").length;
  const byName = (a, b) => a.name.localeCompare(b.name, "en-US");
  const sorted = [...clients];
  if (sort === "health") {
    sorted.sort((a, b) => Number(b.health?.value ?? -1) - Number(a.health?.value ?? -1) || byName(a, b));
  } else if (sort === "name") {
    sorted.sort(byName);
  } else if (sort === "last_sync") {
    sorted.sort((a, b) => (Date.parse(b.last_synced_at) || 0) - (Date.parse(a.last_synced_at) || 0) || byName(a, b));
  } else if (sort === "biggest_drop") {
    const worst = (client) => Math.min(0, ...client.providers.flatMap((p) => p.metrics.map((m) => Number(m.delta_pct ?? 0))));
    sorted.sort((a, b) => worst(a) - worst(b) || byName(a, b));
  } else {
    sorted.sort((a, b) => coverage(b) - coverage(a) || (qualityRank[b.overall_status] ?? 0) - (qualityRank[a.overall_status] ?? 0) || byName(a, b));
  }
  return sorted;
}

export async function getDashboard({ period, query, status, provider, sort = "quality" }) {
  if (FIXTURE_MODE) {
    if (FIXTURE_MODE === "error") { await wait(500); throw new Error("Fixture connection error"); }
    if (FIXTURE_MODE === "loading") await wait(2400);
    const payload = await fixture(FIXTURE_MODE === "empty" ? "dashboard-empty" : "dashboard-all-states");
    const normalizedQuery = query?.toLocaleLowerCase("en-US") || "";
    const clients = sortFixtureClients(payload.clients.filter((client) => {
      const matchesQuery = !normalizedQuery || `${client.name} ${client.domain}`.toLocaleLowerCase("en-US").includes(normalizedQuery);
      const matchesStatus = !status || client.overall_status === status;
      const matchesProvider = !provider || client.providers.some((item) => item.provider === provider);
      return matchesQuery && matchesStatus && matchesProvider;
    }), sort);
    return { ...payload, clients, period_kind: period };
  }
  const params = new URLSearchParams({ period, sort });
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

// `fixture=empty` is already claimed by `getDashboard` (zero clients system-wide,
// so a client page can never even be reached) — dimensional data being empty
// for an *existing* client is a different scenario and needs its own flag to
// stay reachable in a `?fixture=` URL.
function analyticsFixtureName() {
  return FIXTURE_MODE === "empty-analytics" ? "client-analytics-empty" : "client-analytics-normal";
}

let analyticsBundleCache = null;
let analyticsBundleFixtureName = null;
async function loadAnalyticsBundle() {
  const name = analyticsFixtureName();
  if (!analyticsBundleCache || analyticsBundleFixtureName !== name) {
    analyticsBundleFixtureName = name;
    analyticsBundleCache = fixture(name);
  }
  return analyticsBundleCache;
}

// task-16 dimensional analytics (PLAN §21.4/§21.6): one page of rows for a
// single stored period. `variant` only steers the fixture lookup (current vs
// previous side of a comparison) — the real API resolves "current" from
// whatever period/anchor/custom_start/custom_end it is given, so callers
// fetch the previous side by passing the previous period's own exact dates
// as period=custom (see client-page.js).
export async function getClientAnalytics(clientId, { dimension, variant = "current", period, anchor, comparisonMode, customStart, customEnd, sortBy = "clicks", limit = 500, offset = 0 } = {}) {
  if (FIXTURE_MODE) {
    if (FIXTURE_MODE === "error") { await wait(400); throw new Error("Fixture connection error"); }
    if (FIXTURE_MODE === "loading") await wait(1600);
    const bundle = await loadAnalyticsBundle();
    return bundle.analytics[dimension][variant];
  }
  const params = new URLSearchParams({ period, sort_by: sortBy, limit: String(limit), offset: String(offset) });
  if (anchor) params.set("anchor", anchor);
  if (comparisonMode) params.set("comparison_mode", comparisonMode);
  if (customStart) params.set("custom_start", customStart);
  if (customEnd) params.set("custom_end", customEnd);
  const segment = dimension === "page" ? "pages" : "queries";
  return request(`/clients/${clientId}/analytics/${segment}?${params}`, {}, `analytics-${dimension}-${variant}`);
}

// task-17 alerts (PLAN §21.5): declining/rising/new pages or queries between
// two adjacent periods, plus the exact resolved dates of both sides — reused
// by client-page.js to derive the previous period's exact range for
// `getClientAnalytics`.
export async function getClientAlerts(clientId, { dimension = "page", period, anchor, comparisonMode, customStart, customEnd } = {}) {
  if (FIXTURE_MODE) {
    if (FIXTURE_MODE === "error") { await wait(400); throw new Error("Fixture connection error"); }
    if (FIXTURE_MODE === "loading") await wait(1600);
    const bundle = await loadAnalyticsBundle();
    return bundle.alerts[dimension];
  }
  const params = new URLSearchParams({ period, dimension });
  if (anchor) params.set("anchor", anchor);
  if (comparisonMode) params.set("comparison_mode", comparisonMode);
  if (customStart) params.set("custom_start", customStart);
  if (customEnd) params.set("custom_end", customEnd);
  return request(`/clients/${clientId}/alerts?${params}`, {}, `alerts-${dimension}`);
}

// Clicks/impressions dynamics for the client-page chart. Reuses the existing
// `/clients/{id}/metrics` history endpoint (task-06), which is only ever
// collected on the week/month cadence — quarter/year/custom intentionally
// return `null` here rather than mislabel that same weekly history as an
// aggregate it isn't (PLAN §12honest-empty over fabricated aggregation;
// see task-18 Completion report follow-ups).
export async function getClientAnalyticsChart(clientId, period) {
  if (period !== "week" && period !== "month") return null;
  if (FIXTURE_MODE) {
    if (FIXTURE_MODE === "error") { await wait(400); throw new Error("Fixture connection error"); }
    const bundle = await loadAnalyticsBundle();
    return bundle.history || null;
  }
  const payload = await getClientHistory(clientId, period);
  return (payload.series || []).filter((item) => item.metric_key === "gsc_clicks" || item.metric_key === "gsc_impressions");
}

function readMockJobs() {
  try { return JSON.parse(localStorage.getItem("theyovth.mock-jobs.v1")) || {}; }
  catch { return {}; }
}

function writeMockJobs(jobs) { localStorage.setItem("theyovth.mock-jobs.v1", JSON.stringify(jobs)); }

// task-22 fixture-mode auth demo: one cell (Semrush, first client) parks at
// `awaiting_auth` instead of always resolving, so the whole "start ->
// check -> Log in -> auto-continue" loop is demoable/e2e-testable
// without Docker or a real warm Chrome. `startProviderLogin`/`getProviderSession`
// below read/write the same login-trigger timestamp this reads.
const MOCK_LOGINS_KEY = "theyovth.mock-logins.v1";
const AUTH_DEMO_PROVIDER = "semrush";
const AUTH_DEMO_CLIENT_INDEX = 0;
const AUTH_FREEZE_PROGRESS = 40;
const AUTH_LOGIN_GRACE_MS = 1500; // how long the operator "reads the login page" before the mock counts it as signed in

function readMockLogins() {
  try { return JSON.parse(localStorage.getItem(MOCK_LOGINS_KEY)) || {}; }
  catch { return {}; }
}

function writeMockLogins(logins) { localStorage.setItem(MOCK_LOGINS_KEY, JSON.stringify(logins)); }

function mockCellStatus(cell, elapsed) {
  if (cell.status === "not_configured" || cell.status === "cancelled") return cell;
  const offset = cell.provider_index * 300 + cell.client_index * 1350;
  if (cell.provider === AUTH_DEMO_PROVIDER && cell.client_index === AUTH_DEMO_CLIENT_INDEX) {
    return mockAuthDemoCellStatus(cell, elapsed, offset);
  }
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

function mockAuthDemoCellStatus(cell, elapsed, offset) {
  const loggedInAt = readMockLogins()[cell.provider];
  const signedIn = loggedInAt && Date.now() - loggedInAt > AUTH_LOGIN_GRACE_MS;
  if (!signedIn) {
    const progress = Math.max(0, Math.min(AUTH_FREEZE_PROGRESS, Math.floor((elapsed - offset) / 38)));
    if (progress <= 0) return { ...cell, status: "queued", progress_percent: 0, current_stage: "queued" };
    if (progress < AUTH_FREEZE_PROGRESS) return { ...cell, status: "running", progress_percent: progress, current_stage: "authenticate" };
    return { ...cell, status: "awaiting_auth", progress_percent: progress, current_stage: "authenticate", error_code: "auth_required" };
  }
  // Signed in: resume from the freeze point using time-since-login, exactly
  // like the real lane requeues the parked cell and continues — not a
  // restart from zero.
  const resumeElapsed = Date.now() - loggedInAt - AUTH_LOGIN_GRACE_MS;
  const progress = Math.min(100, AUTH_FREEZE_PROGRESS + Math.floor(resumeElapsed / 30));
  if (progress >= 100) return { ...cell, status: "succeeded", progress_percent: 100, current_stage: "complete", error_code: null };
  const stages = ["preflight", "authenticate", "fetch_current", "fetch_previous", "normalize", "validate", "persist"];
  const stage = stages[Math.min(stages.length - 1, Math.floor(progress / 15))];
  return { ...cell, status: "running", progress_percent: progress, current_stage: stage };
}

export async function createSyncJob(payload, dashboard) {
  if (!FIXTURE_MODE) return request("/sync-jobs", { method: "POST", body: JSON.stringify(payload) });
  const id = `fixture-${Date.now()}`;
  if (FIXTURE_MODE.startsWith("sessions-")) {
    // Static, frozen session-stage scenarios (task-22 visual QA) — rendered
    // exactly as recorded, never progressed by `mockCellStatus`.
    const frozen = await fixture(FIXTURE_MODE);
    const jobs = readMockJobs();
    jobs[id] = { ...frozen, id, batch_id: id, job_id: id, frozen: true };
    writeMockJobs(jobs);
    return { batch_id: id, job_id: id, status_url: `/api/v1/sync-jobs/${id}`, events_url: `/api/v1/sync-jobs/${id}/events` };
  }
  writeMockLogins({});
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

export async function importAISeoToolsHistory(clientIds) {
  if (FIXTURE_MODE) {
    await wait(120);
    return { source_available: true, imported_artifacts: 0, scanned_artifacts: 0, outcomes: {} };
  }
  return request("/imports/ai-seo-tools", {
    method: "POST",
    body: JSON.stringify({ client_ids: clientIds, dry_run: false }),
  }, "aiseotools-import");
}

export async function getSyncJob(id) {
  if (!FIXTURE_MODE) return request(`/sync-jobs/${id}`, {}, `job-${id}`);
  const jobs = readMockJobs();
  const job = jobs[id];
  if (!job) throw new Error("Refresh job not found");
  if (job.frozen || ["cancelled", "succeeded", "partial", "failed"].includes(job.status)) return job;
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

// task-22 HTTP surface for task-20's `check_session`/`start_login`
// (`app/api/providers.py`) — the modal's «Log in» button and the standalone
// client-card badge both go through these two.
const AUTH_PAUSE_TIMEOUT_SECONDS = 15 * 60; // mirrors `app.jobs.lane.DEFAULT_AUTH_PAUSE_TIMEOUT_SECONDS`

export async function startProviderLogin(provider) {
  if (FIXTURE_MODE) {
    await wait(300);
    if (new URLSearchParams(location.search).get("demo") === "no-chrome") {
      const error = new Error(`${provider}: no warm Chrome reachable — on the machine running the warm Chrome: seo-audit browser-up --provider ${provider}`);
      error.status = 503;
      error.code = "no_warm_chrome";
      throw error;
    }
    const logins = readMockLogins();
    logins[provider] = Date.now();
    writeMockLogins(logins);
    return { login_session_id: `fixture-login-${provider}`, state: "expired", timeout_seconds: AUTH_PAUSE_TIMEOUT_SECONDS };
  }
  return request(`/providers/${provider}/login`, { method: "POST" }, `login-${provider}`);
}

export async function getProviderSession(provider) {
  if (FIXTURE_MODE) {
    await wait(150);
    const loggedInAt = readMockLogins()[provider];
    const alive = loggedInAt && Date.now() - loggedInAt > AUTH_LOGIN_GRACE_MS;
    return { state: alive ? "alive" : "expired" };
  }
  return request(`/providers/${provider}/session`, {}, `session-${provider}`);
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
