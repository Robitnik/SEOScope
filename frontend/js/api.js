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

export async function getDashboard({ period, query, status, provider, sort = "quality", anchor }) {
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
  if (anchor) params.set("anchor", anchor);
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

function isoDay(date) {
  return date.toISOString().slice(0, 10);
}

function aggregateGscDaily(payload, period) {
  const rows = payload?.rows || [];
  if (!rows.length) return [];
  const rangeStart = new Date(`${payload.period_start}T12:00:00Z`);
  const rangeEnd = new Date(`${payload.period_end}T12:00:00Z`);
  const rangeDays = Math.round((rangeEnd - rangeStart) / 86400000) + 1;
  const granularity = period === "year" || (period === "custom" && rangeDays > 180)
    ? "month"
    : period === "quarter" || (period === "custom" && rangeDays > 45)
      ? "week"
      : "day";
  const buckets = new Map();
  for (const row of rows) {
    const day = new Date(`${row.dimension_value}T12:00:00Z`);
    let key = row.dimension_value;
    let start = row.dimension_value;
    let end = row.dimension_value;
    if (granularity === "month") {
      key = row.dimension_value.slice(0, 7);
      const year = day.getUTCFullYear();
      const month = day.getUTCMonth();
      start = isoDay(new Date(Date.UTC(year, month, 1, 12)));
      end = isoDay(new Date(Date.UTC(year, month + 1, 0, 12)));
      if (start < payload.period_start) start = payload.period_start;
      if (end > payload.period_end) end = payload.period_end;
    } else if (granularity === "week") {
      const index = Math.floor((day - rangeStart) / 86400000 / 7);
      key = `week-${index}`;
      const bucketStart = new Date(rangeStart.getTime() + index * 7 * 86400000);
      const bucketEnd = new Date(Math.min(rangeEnd.getTime(), bucketStart.getTime() + 6 * 86400000));
      start = isoDay(bucketStart);
      end = isoDay(bucketEnd);
    }
    if (!buckets.has(key)) buckets.set(key, { period_start: start, period_end: end, clicks: 0, impressions: 0, clicksSeen: false, impressionsSeen: false });
    const bucket = buckets.get(key);
    for (const metric of ["clicks", "impressions"]) {
      const value = row.metrics?.[metric];
      if (value !== null && value !== undefined && Number.isFinite(Number(value))) {
        bucket[metric] += Number(value);
        bucket[`${metric}Seen`] = true;
      }
    }
  }
  const points = [...buckets.values()];
  return ["clicks", "impressions"].map((metric) => ({
    metric_key: `gsc_${metric}`,
    points: points.map((bucket) => ({
      period_start: bucket.period_start,
      period_end: bucket.period_end,
      value_numeric: bucket[`${metric}Seen`] ? bucket[metric] : null,
      quality: "complete",
    })),
  }));
}

// Real date-dimension rows, not one aggregate snapshot per selected range.
export async function getClientAnalyticsChart(clientId, period, {
  anchor, customStart, customEnd,
} = {}) {
  if (FIXTURE_MODE) {
    if (FIXTURE_MODE === "error") { await wait(400); throw new Error("Fixture connection error"); }
    const bundle = await loadAnalyticsBundle();
    return bundle.history || null;
  }
  const params = new URLSearchParams({ period, role: "current", limit: "500", offset: "0" });
  if (anchor) params.set("anchor", anchor);
  if (customStart) params.set("custom_start", customStart);
  if (customEnd) params.set("custom_end", customEnd);
  const payload = await request(
    `/clients/${clientId}/analytics/gsc/daily?${params}`,
    {},
    `gsc-daily-${period}`,
  );
  return aggregateGscDaily(payload, period);
}

export async function getReportingNinjaDataset(clientId, datasetKey, {
  period = "week", role = "current", anchor, customStart, customEnd,
  sortBy = "sessions", query, limit = 500, offset = 0,
} = {}) {
  if (FIXTURE_MODE) {
    return {
      client_id: clientId, provider: "reporting_ninja", dataset_key: datasetKey,
      dimension_key: null, period_role: role, period_start: null, period_end: null,
      rows: [], total_rows_available: 0, truncated: false, limit, offset,
    };
  }
  const params = new URLSearchParams({
    period, role, sort_by: sortBy, limit: String(limit), offset: String(offset),
  });
  if (anchor) params.set("anchor", anchor);
  if (customStart) params.set("custom_start", customStart);
  if (customEnd) params.set("custom_end", customEnd);
  if (query) params.set("q", query);
  return request(
    `/clients/${clientId}/analytics/reporting-ninja/${datasetKey}?${params}`,
    {},
    `reporting-ninja-${datasetKey}-${role}`,
  );
}

export async function getAhrefsSiteAudit(clientId) {
  if (FIXTURE_MODE) {
    const empty = FIXTURE_MODE === "empty-analytics";
    return {
      client_id: clientId, observed_at: null, data_through: null, project_id: null,
      issues: empty ? [] : [{
        slug: "404_page", issue_id: "", name: "404 page", severity: "Error",
        priority: "High", description: "Page returns a 404 response.",
        how_to_fix: "Restore the page or redirect it to the best live equivalent.",
        affected_urls: 1, exported_rows: 1,
      }],
      total_issue_types: empty ? 0 : 1, total_affected_rows: empty ? 0 : 1,
      severity_counts: { Error: empty ? 0 : 1, Warning: 0, Notice: 0 },
    };
  }
  return request(`/clients/${clientId}/analytics/ahrefs/site-audit`, {}, "ahrefs-site-audit");
}

export async function getAhrefsAffectedUrls(clientId, {
  issueSlug, query, limit = 100, offset = 0,
} = {}) {
  if (FIXTURE_MODE) {
    const rows = FIXTURE_MODE === "empty-analytics" ? [] : [{
      url: "https://example.com/missing", issue_slug: issueSlug || "404_page",
      issue_name: "404 page", severity: "Error",
      details: { URL: "https://example.com/missing", "HTTP status code": "404" },
    }];
    return { client_id: clientId, issue_slug: issueSlug, rows, total_rows_available: rows.length, limit, offset };
  }
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  if (issueSlug) params.set("issue_slug", issueSlug);
  if (query) params.set("q", query);
  return request(
    `/clients/${clientId}/analytics/ahrefs/affected-urls?${params}`,
    {},
    "ahrefs-affected-urls",
  );
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
  if (cell.provider === "tech_audit") {
    // Technical audits share one crawler lane: the next site starts only after
    // the previous site reaches a terminal state.
    const offset = cell.client_index * 1800;
    const progress = Math.max(0, Math.min(100, Math.floor((elapsed - offset) / 16)));
    if (progress <= 0) return { ...cell, status: "queued", progress_percent: 0, current_stage: "queued" };
    const stages = ["preflight", "crawl", "detect", "persist"];
    const stage = stages[Math.min(stages.length - 1, Math.floor(progress / 25))];
    if (progress >= 100) return { ...cell, status: "succeeded", quality: "complete", progress_percent: 100, current_stage: "complete" };
    return { ...cell, status: "running", progress_percent: progress, current_stage: stage };
  }
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
    // Lets `?fixture=...&demo=no-chrome` preview the pre-start browser check
    // (sync-modal's `checkBrowserAvailability`, keyed off `running`) the same
    // way it already previews the mid-job "no warm Chrome" login failure below.
    if (new URLSearchParams(location.search).get("demo") === "no-chrome") return { state: "expired", running: false };
    if (provider !== AUTH_DEMO_PROVIDER) return { state: "alive", running: true };
    const loggedInAt = readMockLogins()[provider];
    const alive = loggedInAt && Date.now() - loggedInAt > AUTH_LOGIN_GRACE_MS;
    // Chrome is up the whole time in this demo — only the login inside it is
    // stale — so `running` stays true even while `state` is "expired".
    return { state: alive ? "alive" : "expired", running: true };
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

// --- Technical SEO Audit (machine-only findings) ---------------------------

export async function getTechAuditLatest(clientId) {
  if (FIXTURE_MODE) {
    if (FIXTURE_MODE === "error") { await wait(300); throw new Error("Fixture connection error"); }
    await wait(180);
    // `audit-empty` (not `empty`) so the "no audit run yet" state can be shown
    // on a client that otherwise has data — `empty` blanks the whole dashboard.
    return fixture(FIXTURE_MODE === "audit-empty" ? "tech-audit-empty" : "tech-audit-latest");
  }
  return request(`/clients/${clientId}/tech-audit/latest`, {}, "tech-audit-latest");
}

export async function getTechAuditFindings(clientId, { category, q, limit = 25, offset = 0 } = {}) {
  if (FIXTURE_MODE) {
    await wait(180);
    const bundle = await fixture("tech-audit-findings");
    let rows = bundle.rows || [];
    if (category) rows = rows.filter((row) => row.category === category);
    if (q) {
      const needle = q.trim().toLocaleLowerCase("en-US");
      rows = rows.filter((row) => `${row.url} ${row.name}`.toLocaleLowerCase("en-US").includes(needle));
    }
    return { ...bundle, category: category || null, total: rows.length, limit, offset, rows: rows.slice(offset, offset + limit) };
  }
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  if (category) params.set("category", category);
  if (q) params.set("q", q);
  return request(`/clients/${clientId}/tech-audit/findings?${params}`, {}, "tech-audit-findings");
}

export async function startTechAudit(clientId) {
  if (FIXTURE_MODE) {
    await wait(300);
    const id = `fixture-audit-${Date.now()}`;
    return { job_id: id, status_url: `/api/v1/sync-jobs/${id}`, events_url: `/api/v1/sync-jobs/${id}/events` };
  }
  return request(`/clients/${clientId}/tech-audit/runs`, { method: "POST", body: "{}" });
}

export async function startTechAuditBatch(payload, dashboard) {
  if (!FIXTURE_MODE) {
    return request("/tech-audit/runs", { method: "POST", body: JSON.stringify(payload) });
  }
  await wait(260);
  const id = `fixture-audit-batch-${Date.now()}`;
  const clientIds = payload.client_ids === "all"
    ? dashboard.clients.map((client) => client.client_id)
    : payload.client_ids;
  const cells = clientIds.map((clientId, clientIndex) => ({
    client_id: clientId,
    provider: "tech_audit",
    provider_index: 0,
    client_index: clientIndex,
    status: "queued",
    progress_percent: 0,
    current_stage: "queued",
  }));
  const jobs = readMockJobs();
  jobs[id] = {
    id, batch_id: id, job_id: id, status: "queued", started_at: Date.now(),
    cells, client_ids: clientIds, providers: ["tech_audit"],
  };
  writeMockJobs(jobs);
  return {
    batch_id: id,
    job_id: null,
    status_url: `/api/v1/sync-jobs/${id}`,
    events_url: `/api/v1/sync-jobs/${id}/events`,
    queued_count: cells.length,
    skipped_active_client_ids: [],
  };
}

export async function recheckTechAuditFindings(clientId, findings) {
  if (FIXTURE_MODE) {
    await wait(300);
    const id = `fixture-recheck-${Date.now()}`;
    return { job_id: id, status_url: `/api/v1/sync-jobs/${id}`, events_url: `/api/v1/sync-jobs/${id}/events` };
  }
  return request(
    `/clients/${clientId}/tech-audit/findings/recheck`,
    { method: "POST", body: JSON.stringify({ findings }) },
    "tech-audit-recheck",
  );
}

// A binary download: deliberately NOT routed through `request()`, which sets
// `Accept: application/json` and parses the body. The UI links straight to it.
export function techAuditExportUrl(clientId) {
  return `${API_ROOT}/clients/${clientId}/tech-audit/export.xlsx`;
}

// --- Client Blogs (machine-only inventory) ----------------------------------

export async function getBlogsLatest(clientId) {
  if (FIXTURE_MODE) {
    if (FIXTURE_MODE === "error") { await wait(300); throw new Error("Fixture connection error"); }
    await wait(180);
    // `blogs-empty` (not `empty`) so the "no crawl yet" state can be shown on a
    // client that otherwise has data — `empty` blanks the whole dashboard.
    return fixture(FIXTURE_MODE === "blogs-empty" ? "blogs-empty" : "blogs-latest");
  }
  return request(`/clients/${clientId}/blogs/latest`, {}, "blogs-latest");
}

export async function getBlogsPosts(clientId, { q, sort = "date_desc", limit = 25, offset = 0 } = {}) {
  if (FIXTURE_MODE) {
    await wait(180);
    const bundle = await fixture("blogs-posts");
    let rows = bundle.rows || [];
    if (q) {
      const needle = q.trim().toLocaleLowerCase("en-US");
      rows = rows.filter((row) => `${row.url} ${row.title}`.toLocaleLowerCase("en-US").includes(needle));
    }
    const sorted = [...rows];
    if (sort === "date_asc") sorted.sort((a, b) => (a.published_date || "").localeCompare(b.published_date || ""));
    else if (sort === "words_desc") sorted.sort((a, b) => (b.word_count ?? 0) - (a.word_count ?? 0));
    else if (sort === "words_asc") sorted.sort((a, b) => (a.word_count ?? 0) - (b.word_count ?? 0));
    else if (sort === "title") sorted.sort((a, b) => a.title.localeCompare(b.title, "en-US"));
    else sorted.sort((a, b) => (b.published_date || "").localeCompare(a.published_date || ""));
    return { ...bundle, q: q || null, sort, total: sorted.length, limit, offset, rows: sorted.slice(offset, offset + limit) };
  }
  const params = new URLSearchParams({ sort, limit: String(limit), offset: String(offset) });
  if (q) params.set("q", q);
  return request(`/clients/${clientId}/blogs/posts?${params}`, {}, "blogs-posts");
}

export async function startBlogsRun(clientId) {
  if (FIXTURE_MODE) {
    await wait(300);
    const id = `fixture-blogs-${Date.now()}`;
    return { job_id: id, status_url: `/api/v1/sync-jobs/${id}`, events_url: `/api/v1/sync-jobs/${id}/events` };
  }
  return request(`/clients/${clientId}/blogs/runs`, { method: "POST", body: "{}" });
}

// Binary downloads: deliberately NOT routed through `request()`, which sets
// `Accept: application/json` and parses the body. The UI links straight to them.
export function blogsExportCsvUrl(clientId) {
  return `${API_ROOT}/clients/${clientId}/blogs/export.csv`;
}

export function blogsExportXlsxUrl(clientId) {
  return `${API_ROOT}/clients/${clientId}/blogs/export.xlsx`;
}

export function isFixtureMode() { return Boolean(FIXTURE_MODE); }

// --- Tools (task-27) --------------------------------------------------------

// Fixture mode keeps a mutable in-memory copy of the tool's rows so the whole
// operator loop (copy → status, verify → verdict) is exercisable without a
// backend — that is what the e2e run drives.
let toolsFixture = null;
const TOOLS_FIXTURE_KEY = "yovth:alt-text-fixture";

async function altTextFixture() {
  if (toolsFixture === null) {
    // Прогрес у демо-режимі переживає перезавантаження, як і в реальному
    // режимі (де він у БД) — інакше фікстура показувала б поведінку, якої в
    // продукті немає. `sessionStorage`, а не `localStorage`: демо-стан не
    // має жити довше за вкладку.
    const stored = sessionStorage.getItem(TOOLS_FIXTURE_KEY);
    if (stored) {
      toolsFixture = JSON.parse(stored);
    } else {
      const [report, items] = await Promise.all([fixture("alt-text-report"), fixture("alt-text-items")]);
      toolsFixture = { report: structuredClone(report), rows: structuredClone(items.rows) };
    }
  }
  return toolsFixture;
}

function saveAltTextFixture() {
  sessionStorage.setItem(TOOLS_FIXTURE_KEY, JSON.stringify(toolsFixture));
}

function altTextFixtureCounts(rows) {
  const counts = { pending: 0, copied: 0, applied: 0, skipped: 0 };
  rows.forEach((row) => { counts[row.status] = (counts[row.status] || 0) + 1; });
  return counts;
}

export async function getToolsCatalog() {
  if (FIXTURE_MODE) {
    if (FIXTURE_MODE === "error") { await wait(300); throw new Error("Fixture connection error"); }
    await wait(120);
    return fixture(FIXTURE_MODE === "tools-empty" ? "tools-catalog-empty" : "tools-catalog");
  }
  return request("/tools", {}, "tools-catalog");
}

export async function getAltTextReports() {
  if (FIXTURE_MODE) {
    await wait(140);
    if (FIXTURE_MODE === "tools-empty") return [];
    const state = await altTextFixture();
    return [{ ...state.report, status_counts: altTextFixtureCounts(state.rows) }];
  }
  return request("/tools/alt-text/reports", {}, "alt-text-reports");
}

export async function getAltTextReport(reportId) {
  if (FIXTURE_MODE) {
    await wait(140);
    const state = await altTextFixture();
    return { ...state.report, status_counts: altTextFixtureCounts(state.rows) };
  }
  return request(`/tools/alt-text/reports/${reportId}`, {}, "alt-text-report");
}

export async function getAltTextItems(reportId, { q, status, urlKind, verify, limit = 50, offset = 0 } = {}) {
  if (FIXTURE_MODE) {
    await wait(140);
    const state = await altTextFixture();
    const needle = (q || "").trim().toLocaleLowerCase("en-US");
    const rows = state.rows.filter((row) => {
      const matchesQuery = !needle
        || `${row.image_url} ${row.new_alt} ${row.search_key}`.toLocaleLowerCase("en-US").includes(needle);
      return matchesQuery
        && (!status || row.status === status)
        && (!urlKind || row.url_kind === urlKind)
        && (!verify || row.verify_status === verify);
    });
    return { report_id: reportId, total: rows.length, limit, offset, rows: rows.slice(offset, offset + limit) };
  }
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  if (q) params.set("q", q);
  if (status) params.set("status", status);
  if (urlKind) params.set("url_kind", urlKind);
  if (verify) params.set("verify", verify);
  return request(`/tools/alt-text/reports/${reportId}/items?${params}`, {}, "alt-text-items");
}

export async function uploadAltTextSource(file) {
  if (FIXTURE_MODE) {
    await wait(400);
    // Ім'я файлу віддаємо справжнє: на ньому тримається авто-вибір клієнта,
    // тож фікстура з чужою назвою ховала б цю поведінку від тестів.
    return { ...(await fixture("alt-text-upload")), source_filename: file.name };
  }
  // Raw bytes, not multipart: one file, no other form fields — the same choice
  // the logo upload made server-side (`app/api/clients.py`).
  const response = await fetch(`${API_ROOT}/tools/alt-text/uploads`, {
    method: "POST",
    headers: { "Content-Type": "application/octet-stream", "X-Filename": file.name, Accept: "application/json" },
    body: file,
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    const error = new Error(payload.detail?.message || `HTTP ${response.status}`);
    error.status = response.status;
    error.code = payload.detail?.error_code;
    throw error;
  }
  return response.json();
}

export async function createAltTextReport(payload, { force = false } = {}) {
  if (FIXTURE_MODE) {
    await wait(400);
    const state = await altTextFixture();
    return { ...state.report, title: payload.title, status_counts: altTextFixtureCounts(state.rows) };
  }
  const query = force ? "?force=true" : "";
  const response = await fetch(`${API_ROOT}/tools/alt-text/reports${query}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    const detail = (await response.json().catch(() => ({}))).detail || {};
    const error = new Error(detail.message || `HTTP ${response.status}`);
    error.status = response.status;
    error.code = detail.error_code;
    // `report_exists` carries the id of the report already holding this file —
    // the UI offers "Open existing" instead of a dead end.
    error.details = detail.details || {};
    throw error;
  }
  return response.json();
}

export async function setAltTextItemStatus(reportId, itemId, status) {
  if (FIXTURE_MODE) {
    await wait(120);
    const state = await altTextFixture();
    const row = state.rows.find((item) => item.id === itemId);
    row.status = status;
    row.status_changed_at = status === "pending" ? null : new Date().toISOString();
    saveAltTextFixture();
    return { ...row };
  }
  return request(`/tools/alt-text/reports/${reportId}/items/${itemId}`, {
    method: "PATCH",
    body: JSON.stringify({ status }),
  }, `alt-text-status-${itemId}`);
}

export async function verifyAltTextItem(reportId, itemId) {
  if (FIXTURE_MODE) {
    await wait(500);
    const state = await altTextFixture();
    const row = state.rows.find((item) => item.id === itemId);
    // Deterministic demo verdict so the e2e run can assert on it.
    const verdict = row.new_alt === row.old_alt ? "mismatch" : "match";
    row.verify_status = verdict;
    row.verify_alt = verdict === "match" ? row.new_alt : "IMG_0042";
    row.verify_http_status = 200;
    row.verify_checked_at = new Date().toISOString();
    if (verdict === "match") { row.status = "applied"; row.status_changed_at = row.verify_checked_at; }
    saveAltTextFixture();
    return { ...row };
  }
  return request(`/tools/alt-text/reports/${reportId}/items/${itemId}/verify`, {
    method: "POST",
    body: "{}",
  }, `alt-text-verify-${itemId}`);
}

export async function deleteAltTextReport(reportId) {
  if (FIXTURE_MODE) { await wait(200); return null; }
  return request(`/tools/alt-text/reports/${reportId}`, { method: "DELETE" });
}

// Binary downloads: deliberately NOT routed through `request()`, which sets
// `Accept: application/json` and parses the body. The UI links straight to them.
export function altTextExportCsvUrl(reportId) {
  return `${API_ROOT}/tools/alt-text/reports/${reportId}/export.csv`;
}

export function altTextExportXlsxUrl(reportId) {
  return `${API_ROOT}/tools/alt-text/reports/${reportId}/export.xlsx`;
}
