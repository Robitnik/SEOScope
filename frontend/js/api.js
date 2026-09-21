const API_ROOT = "/api/v1";
const FIXTURE_MODE = new URLSearchParams(location.search).get("fixture");
const controllers = new Map();

// `request()` cancels the previous in-flight call under the same key, so an
// abort means "a newer request superseded this one" — never a failure the
// operator should see. Chrome words it "signal is aborted without reason", and
// Safari/Firefox differ again, so match the name first and the wording second.
export function isAbortError(error) {
  return error?.name === "AbortError" || /\baborted\b/i.test(error?.message || "");
}

// FastAPI's own `detail` shape varies by failure kind: our handlers send a
// string or `{message, error_code}` object, but a 422 request-validation
// failure (wrong query param, bad body) never goes through our handlers at
// all — FastAPI renders it as an ARRAY of `{loc, msg, type}` objects. Passing
// any of those straight to `new Error(...)` either loses the message or (for
// the array case) stringifies to the literal text "[object Object]" — this
// normalizer is the one place all three shapes funnel through before
// reaching `Error`, task-37 F3.
export function normalizeErrorDetail(detail, status) {
  if (typeof detail === "string" && detail) return detail;
  if (detail && typeof detail === "object" && !Array.isArray(detail) && detail.message) return detail.message;
  if (Array.isArray(detail) && detail.length) {
    return detail.slice(0, 3).map((item) => {
      if (typeof item === "string") return item;
      const loc = Array.isArray(item?.loc) ? item.loc.filter((part) => part !== "body" && part !== "query").join(".") : "";
      const msg = item?.msg || "Invalid request";
      return loc ? `${loc}: ${msg}` : msg;
    }).join("; ");
  }
  return `HTTP ${status}`;
}

// task-71: double-submit CSRF. Токен лежить у НЕ-HttpOnly cookie саме для
// того, щоб фронт міг покласти його в заголовок; сервер звіряє заголовок із
// тим, що зберігає сесія. Читаємо на кожен виклик, а не кешуємо: після
// повторного логіну в іншій вкладці кешоване значення було б застарілим і
// давало б 403 на кожну дію.
function csrfToken() {
  const match = document.cookie.match(/(?:^|;\s*)yovth_csrf=([^;]+)/);
  return match ? decodeURIComponent(match[1]) : "";
}

// Один шлях виходу для всього додатка: якщо сесія протухла або її відкликали,
// будь-який із ~100 викликів нижче отримає 401 — і замість того, щоб кожна
// панель малювала власну помилку «Failed to load», ведемо людину на форму
// входу і повертаємо її туди, де вона була.
function redirectToLogin() {
  if (location.pathname === "/login.html") return;
  const next = encodeURIComponent(`${location.pathname}${location.search}${location.hash}`);
  location.replace(`/login.html?next=${next}`);
}

async function request(path, options = {}, key = path) {
  controllers.get(key)?.abort();
  const controller = new AbortController();
  controllers.set(key, controller);
  const method = (options.method || "GET").toUpperCase();
  const authHeaders = method === "GET" || method === "HEAD" ? {} : { "X-CSRF-Token": csrfToken() };
  try {
    const response = await fetch(`${API_ROOT}${path}`, {
      ...options,
      credentials: "same-origin",
      signal: options.signal || controller.signal,
      headers: { "Content-Type": "application/json", Accept: "application/json", ...authHeaders, ...options.headers },
    });
    if (response.status === 401) {
      redirectToLogin();
      const error = new Error("Your session has expired. Sign in again.");
      error.status = 401;
      error.code = "auth_required";
      throw error;
    }
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      const error = new Error(normalizeErrorDetail(payload.detail, response.status));
      error.status = response.status;
      error.code = (payload.detail && typeof payload.detail === "object" && !Array.isArray(payload.detail))
        ? payload.detail.error_code
        : undefined;
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

// `requestKey` exists because two independent callers on the client page both
// read `/dashboard`: the page shell (which client is this?) and the Reporting
// Ninja panel (its per-period provider summary). Under one shared key the
// second call aborted the first, and the shell rendered "Failed to load
// client. signal is aborted without reason" over a page that was loading fine.
// Distinct keys keep each caller cancelling only its own previous request.
export async function getDashboard({ period, query, status, provider, sort = "quality", anchor, clientStatus = "all", requestKey = "dashboard" }) {
  if (FIXTURE_MODE) {
    if (FIXTURE_MODE === "error") { await wait(500); throw new Error("Fixture connection error"); }
    if (FIXTURE_MODE === "loading") await wait(2400);
    const payload = await fixture(FIXTURE_MODE === "empty" ? "dashboard-empty" : "dashboard-all-states");
    const normalizedQuery = query?.toLocaleLowerCase("en-US") || "";
    const clients = sortFixtureClients(payload.clients.filter((client) => {
      const matchesQuery = !normalizedQuery || `${client.name} ${client.domain}`.toLocaleLowerCase("en-US").includes(normalizedQuery);
      const matchesStatus = !status || client.overall_status === status;
      const matchesProvider = !provider || client.providers.some((item) => item.provider === provider);
      const matchesClientStatus = clientStatus === "all" || (client.status || "active") === clientStatus;
      return matchesQuery && matchesStatus && matchesProvider && matchesClientStatus;
    }), sort);
    // Паузовані внизу — те саме, що робить бекенд після `sort_clients`.
    const ordered = [...clients].sort((a, b) => Number(a.status === "paused") - Number(b.status === "paused"));
    return { ...payload, clients: ordered, period_kind: period };
  }
  const params = new URLSearchParams({ period, sort });
  if (query) params.set("q", query);
  if (status) params.set("status", status);
  if (provider) params.set("provider", provider);
  if (anchor) params.set("anchor", anchor);
  if (clientStatus && clientStatus !== "all") params.set("client_status", clientStatus);
  return request(`/dashboard?${params}`, {}, requestKey);
}

// task-34: пауза/поновлення співпраці. Окремий ендпоінт, а не PATCH клієнта —
// саме він проставляє «paused since».
export async function setClientStatus(clientId, status, note) {
  if (FIXTURE_MODE) {
    await wait(300);
    return { id: clientId, status, status_note: note || null, status_changed_at: new Date().toISOString() };
  }
  return request(`/clients/${clientId}/status`, {
    method: "POST",
    body: JSON.stringify({ status, note: note || null }),
  });
}

// task-85 §2.1: the full `ClientDTO` (has `display_name`) — the dashboard's
// `ClientCardDTO` this page otherwise renders from deliberately doesn't
// carry it (internal surfaces stay on `name`), so the edit dialog fetches it
// fresh rather than the dashboard payload growing a field almost nothing
// else on that surface needs.
export async function getClient(clientId) {
  if (FIXTURE_MODE) {
    await wait(200);
    return { id: clientId, display_name: null };
  }
  return request(`/clients/${clientId}`);
}

// task-85 §2.1: the client-facing full name (report document `<h1>`), edited
// from the client page header. A plain PATCH, not a dedicated endpoint like
// `setClientStatus` above — there is no side effect to record, just one field.
export async function updateClientDisplayName(clientId, displayName) {
  if (FIXTURE_MODE) {
    await wait(300);
    return { id: clientId, display_name: displayName || null };
  }
  return request(`/clients/${clientId}`, {
    method: "PATCH",
    body: JSON.stringify({ display_name: displayName || null }),
  });
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

// How many parallel browser lanes fixture mode pretends the worker runs. Mirrors
// `browser_parallel_slots` in `app/core/config.py`; fixtures are for visual QA,
// so the shape has to match what the real run looks like.
const FIXTURE_LANES = 3;

// task-32: fixture mode has to reproduce the SHAPE of the real telemetry, or
// visual QA silently stops covering it. Each provider now walks its own stage
// ladder — including the steps that are genuinely unmeasurable (`export_issues`
// while Ahrefs builds the ZIP), which is what exercises the indeterminate bar.
const FIXTURE_STAGES = {
  ahrefs: ["open_session", "resolve_project", "export_issues", "unpack_export", "parse_affected", "persist"],
  reporting_ninja: ["connect", "discover", "fetch_previous", "datasets", "fetch_current_ga4_landing_pages", "persist"],
  gsc: ["connect", "discover", "fetch_current_pages", "fetch_current_queries", "fetch_current_daily", "persist"],
  semrush: ["preflight", "authenticate", "fetch_current", "fetch_previous", "normalize", "persist"],
};

const FIXTURE_MESSAGES = {
  parse_affected: (step) => `Ahrefs issues · ${step} / 68 · Page has no outgoing links`,
  fetch_current_ga4_landing_pages: (step) => `GA4 landing pages · current · ${step} / 14`,
  export_issues: () => "Ahrefs is building the full issue export",
};

function mockStage(provider, progress) {
  const ladder = FIXTURE_STAGES[provider] || FIXTURE_STAGES.semrush;
  const index = Math.min(ladder.length - 1, Math.floor((progress / 100) * ladder.length));
  const stage = ladder[index];
  const message = FIXTURE_MESSAGES[stage]?.(Math.max(1, Math.round(progress / 100 * 14))) || null;
  return { current_stage: stage, progress_message: message };
}

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
  // task-31: a provider drives `FIXTURE_LANES` windows of its one profile, so
  // clients are dealt out across lanes and only queue behind the client that
  // shares their lane — the same shape the real dispatcher produces.
  const offset = cell.provider_index * 300 + Math.floor(cell.client_index / FIXTURE_LANES) * 1350;
  if (cell.provider === AUTH_DEMO_PROVIDER && cell.client_index === AUTH_DEMO_CLIENT_INDEX) {
    return mockAuthDemoCellStatus(cell, elapsed, offset);
  }
  const progress = Math.max(0, Math.min(100, Math.floor((elapsed - offset) / 38)));
  // Slot appears at claim time, exactly like the worker sets it: a queued cell
  // has not been handed to a lane yet.
  if (progress <= 0) return { ...cell, slot: null, status: "queued", progress_percent: 0, current_stage: "queued" };
  // Real cells carry `started_at`; without it the elapsed clock (task-32) would
  // be invisible in fixture mode, i.e. untested by visual QA.
  const cellWithSlot = {
    ...cell,
    slot: cell.lane,
    started_at: new Date(Date.now() - Math.max(0, elapsed - offset)).toISOString(),
  };
  if (progress >= 100) {
    const demoFailure = cell.provider === "semrush" && cell.client_index === 1 && !cell.retried;
    return { ...cellWithSlot, status: demoFailure ? "failed" : "succeeded", progress_percent: 100, current_stage: "complete", progress_message: null, error_code: demoFailure ? "RATE_LIMITED" : null };
  }
  return { ...cellWithSlot, status: "running", progress_percent: progress, ...mockStage(cell.provider, progress) };
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
    cells.push({ client_id: clientId, provider, provider_index: providerIndex, client_index: clientIndex, lane: provider === "tech_audit" ? null : clientIndex % FIXTURE_LANES, status: configured ? "queued" : "not_configured", progress_percent: 0, current_stage: "queued" });
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

export function updateIntegration(clientId, provider, payload, { sourceMode = "auto" } = {}) {
  if (FIXTURE_MODE) return wait(450).then(() => ({ client_id: clientId, provider, enabled: true, config: payload }));
  return request(`/clients/${clientId}/integrations/${provider}`, { method: "PUT", body: JSON.stringify({ enabled: true, source_mode: sourceMode, integration_config: payload }) });
}

// task-33: ask the server to find this client's account at the provider instead
// of making the operator look the id up and type it in. `apply: false` keeps it
// a dry run — candidates come back, nothing is written.
export function discoverIntegration(clientId, provider, { apply = true } = {}) {
  if (FIXTURE_MODE) return wait(600).then(() => fixtureDiscovery(clientId, provider));
  return request(`/clients/${clientId}/integrations/${provider}/discover`, { method: "POST", body: JSON.stringify({ apply }) }, `discover:${clientId}:${provider}`);
}

function fixtureDiscovery(clientId, provider) {
  if (FIXTURE_MODE === "error") throw new Error("Fixture connection error");
  if (provider === "reporting_ninja") {
    return {
      client_id: clientId, provider, status: "needs_choice",
      message: "Several GA4 accounts could match this domain. Pick the right one.",
      applied: null, preflight: null,
      candidates: [
        { label: "Acme Corp - GA4", detail: "properties/111", confidence: "strong", reason: "name similarity 0.94", suggested: true, source_mode: "auto", integration_config: { connection_key: "seo@example.com", account_id: "properties/111" } },
        { label: "Acme Corp (old)", detail: "properties/222", confidence: "weak", reason: "name similarity 0.71", suggested: true, source_mode: "auto", integration_config: { connection_key: "seo@example.com", account_id: "properties/222" } },
      ],
    };
  }
  return {
    client_id: clientId, provider, status: "connected",
    message: "Matched Search Console account sc-domain:example.com.",
    applied: { integration_config: { connection_key: "seo@example.com", account_id: "sc-domain:example.com" } },
    preflight: { provider, ok: true, reason_code: null, message: "" },
    candidates: [],
  };
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

export async function getMcpCatalog() {
  return request("/mcp/catalog", {}, "mcp-catalog");
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

export async function getAltTextItems(
  reportId, { q, status, urlKind, verify, mediaStatus, limit = 50, offset = 0 } = {}
) {
  if (FIXTURE_MODE) {
    await wait(140);
    const state = await altTextFixture();
    const needle = (q || "").trim().toLocaleLowerCase("en-US");
    const rows = state.rows.filter((row) => {
      const matchesQuery = !needle
        || `${row.image_url} ${row.new_alt} ${row.search_key}`.toLocaleLowerCase("en-US").includes(needle);
      const matchesMedia = !mediaStatus
        || (mediaStatus === "unresolved" ? !row.wp_media_status : row.wp_media_status === mediaStatus);
      return matchesQuery
        && (!status || row.status === status)
        && (!urlKind || row.url_kind === urlKind)
        && (!verify || row.verify_status === verify)
        && matchesMedia;
    });
    return { report_id: reportId, total: rows.length, limit, offset, rows: rows.slice(offset, offset + limit) };
  }
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  if (q) params.set("q", q);
  if (status) params.set("status", status);
  if (urlKind) params.set("url_kind", urlKind);
  if (verify) params.set("verify", verify);
  if (mediaStatus) params.set("media_status", mediaStatus);
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
    const error = new Error(normalizeErrorDetail(payload.detail, response.status));
    error.status = response.status;
    error.code = (payload.detail && typeof payload.detail === "object" && !Array.isArray(payload.detail))
      ? payload.detail.error_code
      : undefined;
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
    const detail = (await response.json().catch(() => ({}))).detail;
    const error = new Error(normalizeErrorDetail(detail, response.status));
    error.status = response.status;
    const detailObj = (detail && typeof detail === "object" && !Array.isArray(detail)) ? detail : {};
    error.code = detailObj.error_code;
    // `report_exists` carries the id of the report already holding this file —
    // the UI offers "Open existing" instead of a dead end.
    error.details = detailObj.details || {};
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

// Deterministic demo outcome for the WordPress media ID lookup (task-39):
// non-uploads rows are `not_searchable`, a row whose name looks like a
// duplicate-counter collision (`bar-counter-2`) demos `ambiguous` with two
// candidates, everything else resolves with a fake ID under the row's own
// site host — so "Find" always opens a plausible-looking admin URL.
function fixtureMediaLookup(row) {
  const checkedAt = new Date().toISOString();
  if (row.url_kind !== "uploads") {
    return {
      wp_media_id: null,
      wp_media_status: "not_searchable",
      wp_media_confidence: null,
      wp_media_alt: null,
      wp_media_admin_url: null,
      wp_media_detail: "This image is not in the uploads folder, so it cannot be in the media library.",
      wp_media_candidates: [],
      wp_media_checked_at: checkedAt,
    };
  }
  const root = new URL(row.image_url, location.origin).origin;
  if ((row.derivation_warnings || []).includes("possible_duplicate_counter")) {
    return {
      wp_media_id: null,
      wp_media_status: "ambiguous",
      wp_media_confidence: null,
      wp_media_alt: null,
      wp_media_admin_url: `${root}/wp-admin/upload.php?mode=list&s=${encodeURIComponent(row.search_key_fallback || row.search_key)}`,
      wp_media_detail: "2 attachment(s) matched — pick the right one.",
      wp_media_candidates: [
        {
          id: 5001,
          file: "2024/02/bar-counter.jpg",
          source_url: `${root}/wp-content/uploads/2024/02/bar-counter.jpg`,
          alt_text: null,
          title: "bar-counter",
        },
        {
          id: 5002,
          file: "2023/09/bar-counter.jpg",
          source_url: `${root}/wp-content/uploads/2023/09/bar-counter.jpg`,
          alt_text: "Old bar photo",
          title: "bar-counter",
        },
      ],
      wp_media_checked_at: checkedAt,
    };
  }
  const fakeId = 4000 + Number(row.row_index || 0);
  return {
    wp_media_id: fakeId,
    wp_media_status: "resolved",
    wp_media_confidence: "exact",
    wp_media_alt: row.old_alt || null,
    wp_media_admin_url: `${root}/wp-admin/upload.php?item=${fakeId}&mode=grid`,
    wp_media_detail: null,
    wp_media_candidates: [],
    wp_media_checked_at: checkedAt,
  };
}

export async function resolveAltTextItemMedia(reportId, itemId) {
  if (FIXTURE_MODE) {
    await wait(500);
    const state = await altTextFixture();
    const row = state.rows.find((item) => item.id === itemId);
    Object.assign(row, fixtureMediaLookup(row));
    saveAltTextFixture();
    return { ...row };
  }
  return request(`/tools/alt-text/reports/${reportId}/items/${itemId}/resolve-media`, {
    method: "POST",
    body: "{}",
  }, `alt-text-resolve-media-${itemId}`);
}

export async function resolveAltTextReportMedia(reportId, { limit = 50 } = {}) {
  if (FIXTURE_MODE) {
    await wait(700);
    const state = await altTextFixture();
    const counts = {
      resolved: 0, ambiguous: 0, not_found: 0, rest_unavailable: 0, failed: 0, skipped: 0, remaining: 0,
    };
    state.rows.forEach((row) => {
      if (row.wp_media_status) return;
      if (row.url_kind !== "uploads") { counts.skipped += 1; return; }
      Object.assign(row, fixtureMediaLookup(row));
      counts[row.wp_media_status] = (counts[row.wp_media_status] || 0) + 1;
    });
    saveAltTextFixture();
    return counts;
  }
  return request(`/tools/alt-text/reports/${reportId}/resolve-media`, {
    method: "POST",
    body: JSON.stringify({ limit }),
  }, `alt-text-resolve-media-batch-${reportId}`);
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

// --- Redirect Checker (task-29) ---------------------------------------------

// Fixture mode keeps a mutable copy of one check so the whole loop (create →
// run row by row → filter → re-run) works without a backend, exactly like the
// alt-text fixture above. Traces come from a fixture keyed by URL shape, so the
// e2e run can assert on concrete verdicts.
let redirectsFixture = null;
const REDIRECTS_FIXTURE_KEY = "yovth:redirects-fixture";

async function redirectFixtureState() {
  if (redirectsFixture === null) {
    const stored = sessionStorage.getItem(REDIRECTS_FIXTURE_KEY);
    if (stored) {
      redirectsFixture = JSON.parse(stored);
    } else {
      const [check, targets] = await Promise.all([
        fixture("redirects-check"),
        fixture("redirects-targets"),
      ]);
      redirectsFixture = { check: structuredClone(check), rows: structuredClone(targets.rows) };
    }
  }
  return redirectsFixture;
}

function saveRedirectsFixture() {
  sessionStorage.setItem(REDIRECTS_FIXTURE_KEY, JSON.stringify(redirectsFixture));
}

function redirectFixtureCounts(rows) {
  const runStatus = { queued: 0, running: 0, done: 0, failed: 0 };
  const verdicts = {};
  rows.forEach((row) => {
    runStatus[row.run_status] = (runStatus[row.run_status] || 0) + 1;
    if (row.verdict) verdicts[row.verdict] = (verdicts[row.verdict] || 0) + 1;
  });
  return { runStatus, verdicts };
}

function withRedirectCounts(check, rows) {
  const counts = redirectFixtureCounts(rows);
  return { ...check, run_status_counts: counts.runStatus, verdict_counts: counts.verdicts };
}

export async function traceRedirect(url, options = null) {
  if (FIXTURE_MODE) {
    await wait(320);
    const traces = await fixture("redirects-traces");
    const key = Object.keys(traces).find((candidate) => url.includes(candidate));
    return { ...(traces[key] || traces.default), input_url: url };
  }
  return request("/tools/redirects/trace", {
    method: "POST",
    body: JSON.stringify(options ? { url, options } : { url }),
  }, "redirects-trace");
}

export async function getRedirectChecks() {
  if (FIXTURE_MODE) {
    await wait(140);
    if (FIXTURE_MODE === "tools-empty") return [];
    const state = await redirectFixtureState();
    return [withRedirectCounts(state.check, state.rows)];
  }
  return request("/tools/redirects/checks", {}, "redirects-checks");
}

export async function getRedirectCheck(checkId) {
  if (FIXTURE_MODE) {
    await wait(140);
    const state = await redirectFixtureState();
    return withRedirectCounts(state.check, state.rows);
  }
  return request(`/tools/redirects/checks/${checkId}`, {}, "redirects-check");
}

export async function getRedirectTargets(checkId, { q, verdict, runStatus, flag, limit = 50, offset = 0 } = {}) {
  if (FIXTURE_MODE) {
    await wait(140);
    const state = await redirectFixtureState();
    const needle = (q || "").trim().toLocaleLowerCase("en-US");
    const rows = state.rows.filter((row) => {
      const haystack = `${row.input_url} ${row.normalized_url} ${row.final_url || ""}`;
      return (!needle || haystack.toLocaleLowerCase("en-US").includes(needle))
        && (!verdict || row.verdict === verdict)
        && (!runStatus || row.run_status === runStatus)
        && (!flag || (row.flags || []).includes(flag));
    });
    return { check_id: checkId, total: rows.length, limit, offset, rows: rows.slice(offset, offset + limit) };
  }
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  if (q) params.set("q", q);
  if (verdict) params.set("verdict", verdict);
  if (runStatus) params.set("run_status", runStatus);
  if (flag) params.set("flag", flag);
  return request(`/tools/redirects/checks/${checkId}/targets?${params}`, {}, "redirects-targets");
}

export async function createRedirectCheck(payload) {
  if (FIXTURE_MODE) {
    await wait(320);
    const state = await redirectFixtureState();
    return withRedirectCounts({ ...state.check, title: payload.title || state.check.title }, state.rows);
  }
  return request("/tools/redirects/checks", {
    method: "POST",
    body: JSON.stringify(payload),
  }, "redirects-create");
}

// Deliberately NOT sharing an abort key with the other calls: the page runs
// several rows concurrently, and `request()` cancels same-key calls — one shared
// key would make every new row abort the previous one.
export async function runRedirectTarget(checkId, targetId) {
  if (FIXTURE_MODE) {
    await wait(360);
    const state = await redirectFixtureState();
    const row = state.rows.find((item) => item.id === targetId);
    const traces = await fixture("redirects-traces");
    const key = Object.keys(traces).find((candidate) => row.normalized_url.includes(candidate));
    Object.assign(row, traces[key] || traces.default, {
      run_status: "done",
      checked_at: new Date().toISOString(),
      input_url: row.input_url,
      normalized_url: row.normalized_url,
      id: row.id,
      row_index: row.row_index,
    });
    saveRedirectsFixture();
    return { ...row };
  }
  return request(`/tools/redirects/checks/${checkId}/targets/${targetId}/run`, {
    method: "POST",
    body: "{}",
  }, `redirects-run-${targetId}`);
}

export async function deleteRedirectCheck(checkId) {
  if (FIXTURE_MODE) { await wait(200); return null; }
  return request(`/tools/redirects/checks/${checkId}`, { method: "DELETE" });
}

export function redirectsExportCsvUrl(checkId) {
  return `${API_ROOT}/tools/redirects/checks/${checkId}/export.csv`;
}

export function redirectsExportXlsxUrl(checkId) {
  return `${API_ROOT}/tools/redirects/checks/${checkId}/export.xlsx`;
}

// Portfolio Trends (task-28): one line per client for a single daily metric.
// `client_ids` is a repeated query param (FastAPI `Query(list[str])`), not CSV.
export async function getPortfolioSeries({ metric, bucket = "week", days = 90, clientIds } = {}) {
  if (FIXTURE_MODE) {
    if (FIXTURE_MODE === "error") { await wait(400); throw new Error("Fixture connection error"); }
    await wait(220);
    const name = FIXTURE_MODE === "trends-empty"
      ? "trends-empty"
      : FIXTURE_MODE === "trends-partial"
        ? "trends-partial"
        : FIXTURE_MODE === "trends-sparse"
          ? "trends-sparse"
          : "trends-normal";
    const bundle = await fixture(name);
    return bundle[metric] || bundle.gsc_clicks;
  }
  const params = new URLSearchParams({ metric, bucket, days: String(days) });
  (clientIds || []).forEach((id) => params.append("client_ids", id));
  return request(`/analytics/portfolio-series?${params}`, {}, `portfolio-series-${metric}`);
}

// --- Site Monitor (task-36) -------------------------------------------------
//
// Fixture dispatch follows the established per-subsystem convention (compare
// `audit-empty`/`blogs-empty` above): most `?fixture=...` values are unrelated
// to the monitor and fall through to the default "populated" bundle, exactly
// like `getTechAuditLatest` ignores `clientId` and returns the same fixture
// regardless of which client page loaded it. A handful of `monitor-*` values
// select one dedicated single-state bundle instead, for states that cannot
// coexist with the populated bundle (never checked, mid-run, baseline, ...).

// "Paused" is deliberately not a scenario flag here: `client-nyc-hearing` is
// already paused in `dashboard-all-states.json`, and `getMonitorSummary` above
// already resolves a real per-client entry from the portfolio bundle — so
// visiting `#/client/client-nyc-hearing/monitor` under the default fixture
// exercises that state without a dedicated bundle.
const MONITOR_SCENARIO_FIXTURES = new Set([
  "monitor-never-checked", "monitor-running", "monitor-baseline",
  "monitor-blocked", "monitor-truncated", "monitor-dns-unavailable",
]);

function monitorClientFixtureName() {
  return MONITOR_SCENARIO_FIXTURES.has(FIXTURE_MODE)
    ? FIXTURE_MODE.replace("monitor-", "monitor-client-")
    : "monitor-client-dense";
}

let monitorClientFixtureCache = null;
let monitorClientFixtureCacheName = null;
async function loadMonitorClientFixture() {
  const name = monitorClientFixtureName();
  if (!monitorClientFixtureCache || monitorClientFixtureCacheName !== name) {
    monitorClientFixtureCacheName = name;
    monitorClientFixtureCache = fixture(name);
  }
  return monitorClientFixtureCache;
}

function loadMonitorPortfolioFixture() {
  return fixture(FIXTURE_MODE === "monitor-empty" ? "monitor-portfolio-empty" : "monitor-portfolio-normal");
}

// "Check now" fixture progression: a `running` row for `MONITOR_FIXTURE_RUN_MS`,
// then `succeeded` — the same shape `getSyncJob`'s elapsed-time simulation uses
// above, scoped to sessionStorage so a refresh doesn't restart it mid-demo.
const MONITOR_FIXTURE_RUN_KEY = "yovth:monitor-fixture-run";
const MONITOR_FIXTURE_RUN_MS = 3200;

function readMonitorFixtureRun(clientId) {
  let stored;
  try { stored = JSON.parse(sessionStorage.getItem(MONITOR_FIXTURE_RUN_KEY) || "null"); }
  catch { return null; }
  if (!stored || stored.clientId !== clientId) return null;
  const elapsed = Date.now() - stored.startedAt;
  if (elapsed > MONITOR_FIXTURE_RUN_MS * 2) return null; // stale demo run, ignore
  const running = elapsed < MONITOR_FIXTURE_RUN_MS;
  return {
    id: stored.id, client_id: clientId, kind: stored.kind, trigger: "manual",
    status: running ? "running" : "succeeded",
    started_at: new Date(stored.startedAt).toISOString(),
    finished_at: running ? null : new Date(stored.startedAt + MONITOR_FIXTURE_RUN_MS).toISOString(),
    urls_total: 42,
    urls_checked: running ? Math.round((elapsed / MONITOR_FIXTURE_RUN_MS) * 42) : 42,
    urls_changed: 0, urls_new: 0, urls_gone: 0, requests_made: running ? Math.round(elapsed / 40) : 210,
    bytes_downloaded: running ? elapsed * 900 : 2_600_000,
    changes_critical: 0, changes_warning: 0, changes_info: 0,
    truncated: false, remaining_count: null, error_code: null, error_message: null,
  };
}

const MONITOR_NEVER_CHECKED_SUMMARY = {
  client_id: "", enabled: true, last_light_run: null, last_full_run: null,
  open_critical: 0, open_warning: 0, open_info: 0,
  sitemap_health: { total: 0, ok: 0, redirects: 0, broken: 0, unavailable: 0 },
  robots_status: null, next_light_due_at: null, next_full_due_at: null,
};

export async function getMonitorSummary(clientId) {
  if (FIXTURE_MODE) {
    if (FIXTURE_MODE === "error") { await wait(400); throw new Error("Fixture connection error"); }
    await wait(160);
    const activeRun = readMonitorFixtureRun(clientId);
    let base;
    // A `monitor-*` scenario flag always wins, regardless of which real
    // client id is in the URL — that is the whole point of the flag (visit
    // any client's monitor page and see the "never checked"/"running"/...
    // scenario). Without this check first, a real id that happens to exist
    // in `monitor-portfolio-normal.json` (e.g. `client-cleveland`) would
    // silently shadow the scenario and always render the dense/populated data.
    if (MONITOR_SCENARIO_FIXTURES.has(FIXTURE_MODE)) {
      const detail = await loadMonitorClientFixture();
      base = { ...(detail.summary || MONITOR_NEVER_CHECKED_SUMMARY), client_id: clientId };
    } else {
      const bundle = await loadMonitorPortfolioFixture();
      base = bundle.summaries?.[clientId]
        || { ...((await loadMonitorClientFixture()).summary || MONITOR_NEVER_CHECKED_SUMMARY), client_id: clientId };
    }
    if (!activeRun) return base;
    const key = activeRun.kind === "full" ? "last_full_run" : "last_light_run";
    return { ...base, [key]: activeRun };
  }
  return request(`/clients/${clientId}/monitor`, {}, `monitor-summary-${clientId}`);
}

// `requestKey` is REQUIRED, not defaulted: this function used to invent its
// own key from `clientId` alone, so every caller — the history table, the
// F5-resume check (fired twice, one per kind), and the live poll tick — sat
// under one shared key and aborted each other. `request()` treats a shared
// key as "supersede the previous call", which is correct WITHIN one purpose
// and wrong ACROSS four different ones (task-38 §1: the "Check history"
// section got stuck in its skeleton forever because its own request was
// aborted by the F5-resume calls, whose `.catch` swallowed the failure
// silently). Making the caller name its own intent is the fix that closes
// the whole class: forgetting the key is now a `TypeError` at the call site,
// not a silent collision discovered live.
export async function listMonitorRuns(clientId, { kind, limit = 50, requestKey } = {}) {
  if (!requestKey) throw new Error("listMonitorRuns requires an explicit requestKey");
  if (FIXTURE_MODE) {
    await wait(160);
    const bundle = await loadMonitorClientFixture();
    const activeRun = readMonitorFixtureRun(clientId);
    let runs = activeRun ? [activeRun, ...(bundle.runs || [])] : (bundle.runs || []);
    if (kind) runs = runs.filter((run) => run.kind === kind);
    return runs.slice(0, limit);
  }
  const params = new URLSearchParams({ limit: String(limit) });
  if (kind) params.set("kind", kind);
  return request(`/clients/${clientId}/monitor/runs?${params}`, {}, requestKey);
}

export async function startMonitorRun(clientId, kind = "light") {
  if (FIXTURE_MODE) {
    await wait(300);
    const id = `fixture-monitor-run-${Date.now()}`;
    sessionStorage.setItem(MONITOR_FIXTURE_RUN_KEY, JSON.stringify({ id, clientId, kind, startedAt: Date.now() }));
    return { run_id: id, status_url: `/api/v1/clients/${clientId}/monitor/runs` };
  }
  return request(
    `/clients/${clientId}/monitor/runs?kind=${kind}`,
    { method: "POST", body: "{}" },
    `monitor-run-start-${clientId}`,
  );
}

export async function listClientMonitorChanges(clientId, { severity, state = "open", requestKey } = {}) {
  if (FIXTURE_MODE) {
    await wait(160);
    const bundle = await loadMonitorClientFixture();
    let changes = bundle.changes || [];
    if (state === "open") changes = changes.filter((change) => !change.resolved_at);
    if (severity) changes = changes.filter((change) => change.severity === severity);
    return changes;
  }
  const params = new URLSearchParams({ state });
  if (severity) params.set("severity", severity);
  // Keyed by the caller's declared purpose when given, else by shape+clientId:
  // `monitor-page.js`/`monitor-client.js` legitimately fire more than one
  // differently-filtered call for the same client concurrently (e.g. the open
  // feed and a separate `severity=critical`/`warning` pull merged into it) —
  // a shape-only key breaks the instant two call sites happen to want the
  // same severity+state (reproduced live for the portfolio equivalent of this
  // function, see `listPortfolioMonitorChanges` — same fix applied here
  // pre-emptively rather than waiting to hit it again).
  return request(`/clients/${clientId}/monitor/changes?${params}`, {}, requestKey || `monitor-client-changes-${clientId}-${severity || "all"}-${state}`);
}

// `type`/`client` are client-side-only filters — `GET /monitor/changes` only
// accepts `severity`/`state`/`limit` (verified against `app/api/monitor.py`).
// Callers filter the returned array further by `change_type`/`client_id`.
export async function listPortfolioMonitorChanges({ clientId, severity, state = "open", limit = 200, requestKey } = {}) {
  if (FIXTURE_MODE) {
    if (FIXTURE_MODE === "error") { await wait(400); throw new Error("Fixture connection error"); }
    await wait(180);
    const bundle = await loadMonitorPortfolioFixture();
    let changes = bundle.changes || [];
    if (state === "open") changes = changes.filter((change) => !change.resolved_at);
    if (severity) changes = changes.filter((change) => change.severity === severity);
    if (clientId) changes = changes.filter((change) => change.client_id === clientId);
    return changes.slice(0, limit);
  }
  const params = new URLSearchParams({ state, limit: String(limit) });
  if (severity) params.set("severity", severity);
  // Narrowing by client belongs on the SERVER: `limit` cuts the newest N rows
  // across the whole portfolio, so filtering a fetched window in JS reports
  // "no changes" for any client whose rows sit past that window (LIVE
  // 2026-08-12 — LAXTON went blank right after a noisy repair run on another
  // client).
  if (clientId) params.set("client_id", clientId);
  // Keyed by the CALLER's declared purpose, not auto-derived from severity/state
  // alone: the portfolio page fires several concurrent calls for different
  // reasons (open critical strip, blocked-state detection, the filterable feed)
  // that can legitimately share identical severity+state (e.g. both the
  // blocked-detection pull and the default feed view are `state=open` with no
  // severity, differing only in `limit`, which two same-shape-but-different-
  // purpose calls firing via `Promise.all` would otherwise abort each other
  // over — reproduced live: the portfolio page hung on its loading skeleton
  // because the feed request was aborted by the blocked-detection request).
  // A caller that omits `requestKey` falls back to the old shape-derived key,
  // which still correctly supersedes its own stale predecessor.
  return request(`/monitor/changes?${params}`, {}, requestKey || `monitor-portfolio-changes-${severity || "all"}-${state}`);
}

export async function ackMonitorChange(changeId, note) {
  if (FIXTURE_MODE) {
    await wait(220);
    return { id: changeId, acknowledged_at: new Date().toISOString(), acknowledged_note: note || null };
  }
  return request(`/monitor/changes/${changeId}/ack`, {
    method: "POST",
    body: JSON.stringify({ note: note || null }),
  }, `monitor-ack-${changeId}`);
}

export async function getMonitorPages(clientId, {
  status, indexable, inSitemap, q, sort = "last_checked_at", limit = 100, offset = 0,
} = {}) {
  if (FIXTURE_MODE) {
    await wait(180);
    const bundle = await loadMonitorClientFixture();
    let rows = bundle.pages?.pages || [];
    if (status === "ok") rows = rows.filter((page) => page.status >= 200 && page.status < 300);
    if (status === "broken") rows = rows.filter((page) => page.status == null || page.status >= 300);
    if (indexable !== undefined) rows = rows.filter((page) => page.indexable === indexable);
    if (inSitemap !== undefined) rows = rows.filter((page) => page.in_sitemap === inSitemap);
    if (q) {
      const needle = q.trim().toLocaleLowerCase("en-US");
      rows = rows.filter((page) => page.url.toLocaleLowerCase("en-US").includes(needle));
    }
    return { pages: rows.slice(offset, offset + limit), total: rows.length, limit, offset };
  }
  const params = new URLSearchParams({ sort, limit: String(limit), offset: String(offset) });
  if (status) params.set("status", status);
  if (indexable !== undefined) params.set("indexable", String(indexable));
  if (inSitemap !== undefined) params.set("in_sitemap", String(inSitemap));
  if (q) params.set("q", q);
  return request(`/clients/${clientId}/monitor/pages?${params}`, {}, `monitor-pages-${clientId}`);
}

// Re-check one URL right now (task-38 §2) — synchronous, unlike `startMonitorRun`:
// one HTTP round trip, the response IS the updated row. `409` means a check for
// this exact page is already running (double-click / another operator).
export async function checkMonitorPage(clientId, pageId) {
  if (FIXTURE_MODE) {
    await wait(700);
    const bundle = await loadMonitorClientFixture();
    const page = (bundle.pages?.pages || []).find((p) => p.id === pageId);
    if (!page) throw new Error("Page not found");
    return { ...page, last_checked_at: new Date().toISOString() };
  }
  return request(
    `/clients/${clientId}/monitor/pages/${pageId}/check`,
    { method: "POST", body: "{}" },
    `monitor-page-check-${pageId}`,
  );
}

export async function getMonitorPageVersions(clientId, pageId) {
  if (FIXTURE_MODE) {
    await wait(160);
    const bundle = await loadMonitorClientFixture();
    return bundle.pageVersions?.[pageId] || [];
  }
  return request(`/clients/${clientId}/monitor/pages/${pageId}/versions`, {}, "monitor-diff");
}

export async function getMonitorPageDiff(clientId, pageId, { fromVersionId, toVersionId } = {}) {
  if (FIXTURE_MODE) {
    await wait(160);
    const bundle = await loadMonitorClientFixture();
    return bundle.pageDiff?.[pageId] || null;
  }
  const params = new URLSearchParams();
  if (fromVersionId) params.set("from_version_id", fromVersionId);
  if (toVersionId) params.set("to_version_id", toVersionId);
  const qs = params.toString();
  return request(`/clients/${clientId}/monitor/pages/${pageId}/diff${qs ? `?${qs}` : ""}`, {}, "monitor-diff");
}

// Every tracked robots.txt / sitemap file of a client. Request key names the
// PURPOSE ("which files exist"), not the resource — a key shared with the
// version viewer would let one abort the other (see task-38 §1).
export async function getMonitorResources(clientId, kind) {
  if (FIXTURE_MODE) {
    await wait(140);
    const bundle = await loadMonitorClientFixture();
    return (bundle.resources?.[kind] || []);
  }
  return request(
    `/clients/${clientId}/monitor/resources/${kind}`,
    {},
    `monitor-resources-${kind}-${clientId}`,
  );
}

// Synchronous re-check of robots.txt (mirrors `checkMonitorPage`) — one HTTP
// round trip, the response IS the refreshed resource list for "robots".
export async function checkMonitorRobots(clientId) {
  if (FIXTURE_MODE) {
    await wait(700);
    return getMonitorResources(clientId, "robots");
  }
  return request(
    `/clients/${clientId}/monitor/robots/check`,
    { method: "POST", body: "{}" },
    `monitor-robots-check-${clientId}`,
  );
}

// Synchronous re-check of the sitemap tree — same rationale as `checkMonitorRobots`.
export async function checkMonitorSitemap(clientId) {
  if (FIXTURE_MODE) {
    await wait(700);
    return getMonitorResources(clientId, "sitemap");
  }
  return request(
    `/clients/${clientId}/monitor/sitemap/check`,
    { method: "POST", body: "{}" },
    `monitor-sitemap-check-${clientId}`,
  );
}

// Stored bytes of one fetched document. Returned as TEXT, not JSON: this is a
// robots.txt / sitemap XML file exactly as the server sent it.
export async function getMonitorDocumentText(sha256) {
  if (FIXTURE_MODE) {
    await wait(140);
    const bundle = await loadMonitorClientFixture();
    return bundle.documents?.[sha256] || "";
  }
  const response = await fetch(`${API_ROOT}/monitor/documents/${sha256}`, {
    headers: { Accept: "text/plain" },
  });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.text();
}

// Direct link for "open in a new tab" — the browser fetches it itself, so this
// is a URL builder, not a request.
export function monitorDocumentUrl(sha256) {
  return `${API_ROOT}/monitor/documents/${sha256}`;
}

export async function getMonitorResourceVersions(clientId, kind, { url } = {}) {
  if (FIXTURE_MODE) {
    await wait(160);
    const bundle = await loadMonitorClientFixture();
    return bundle.resourceVersions?.[kind] || [];
  }
  const params = new URLSearchParams();
  if (url) params.set("url", url);
  const qs = params.toString();
  return request(`/clients/${clientId}/monitor/resources/${kind}/versions${qs ? `?${qs}` : ""}`, {}, "monitor-diff");
}

export async function getMonitorResourceDiff(clientId, kind, { url, fromVersionId, toVersionId } = {}) {
  if (FIXTURE_MODE) {
    await wait(160);
    const bundle = await loadMonitorClientFixture();
    return bundle.resourceDiff?.[kind] || null;
  }
  const params = new URLSearchParams();
  if (url) params.set("url", url);
  if (fromVersionId) params.set("from_version_id", fromVersionId);
  if (toVersionId) params.set("to_version_id", toVersionId);
  const qs = params.toString();
  return request(`/clients/${clientId}/monitor/resources/${kind}/diff${qs ? `?${qs}` : ""}`, {}, "monitor-diff");
}

export async function getMonitorDnsRecords(clientId) {
  if (FIXTURE_MODE) {
    await wait(160);
    const bundle = await loadMonitorClientFixture();
    return bundle.dns || [];
  }
  return request(`/clients/${clientId}/monitor/dns`, {}, `monitor-dns-${clientId}`);
}

// `name`+`recordType` are both required by the real endpoint (no "list every
// record's history" shortcut) — verified against `app/api/monitor.py`.
export async function getMonitorDnsVersions(clientId, { name, recordType }) {
  if (FIXTURE_MODE) {
    await wait(160);
    const bundle = await loadMonitorClientFixture();
    return bundle.dnsVersions?.[`${name}|${recordType}`] || [];
  }
  const params = new URLSearchParams({ name, record_type: recordType });
  return request(`/clients/${clientId}/monitor/dns/versions?${params}`, {}, "monitor-diff");
}

// Raw stored document (robots.txt / page body), not a JSON response — the UI
// links straight to it, same convention as `techAuditExportUrl` below.
export function getMonitorDocumentUrl(sha256) {
  return `${API_ROOT}/monitor/documents/${encodeURIComponent(sha256)}`;
}

// --- redirects (task-51) ----------------------------------------------------

export async function getMonitorRedirects(clientId, { state = "active", q, sort = "-first_seen_at", limit = 100, offset = 0 } = {}) {
  if (FIXTURE_MODE) {
    await wait(180);
    return { redirects: [], total: 0, limit, offset };
  }
  const params = new URLSearchParams({ state, sort, limit: String(limit), offset: String(offset) });
  if (q) params.set("q", q);
  return request(`/clients/${clientId}/monitor/redirects?${params}`, {}, `monitor-redirects-${clientId}`);
}

export async function getMonitorRedirectsStatus(clientId) {
  if (FIXTURE_MODE) {
    await wait(160);
    return {
      client_id: clientId, redirect_source: "auto", effective_source: null,
      credentials_status: "missing", last_checked_at: null,
      rules_total: 0, rules_enabled: 0, rules_disabled: 0, last_unavailable_reason: null,
    };
  }
  return request(`/clients/${clientId}/monitor/redirects/status`, {}, `monitor-redirects-status-${clientId}`);
}

// Synchronous re-check (mirrors `checkMonitorPage`) — one HTTP round trip,
// the response IS the updated status.
export async function checkMonitorRedirects(clientId) {
  if (FIXTURE_MODE) {
    await wait(700);
    return getMonitorRedirectsStatus(clientId);
  }
  return request(
    `/clients/${clientId}/monitor/redirects/check`,
    { method: "POST", body: "{}" },
    `monitor-redirects-check-${clientId}`,
  );
}

export async function getMonitorSettings(clientId) {
  if (FIXTURE_MODE) {
    await wait(160);
    const bundle = await loadMonitorClientFixture();
    return bundle.settings || {
      client_id: clientId, enabled: true, base_url_override: null, canonical_www: null,
      light_interval_hours: null, full_interval_hours: null, max_urls: null,
      monitor_when_paused: false, ignore_patterns: [], muted_change_types: [],
      updated_at: new Date().toISOString(),
    };
  }
  return request(`/clients/${clientId}/monitor/settings`, {}, `monitor-settings-${clientId}`);
}

export async function patchMonitorSettings(clientId, patch) {
  if (FIXTURE_MODE) {
    await wait(260);
    const current = await getMonitorSettings(clientId);
    return { ...current, ...patch, updated_at: new Date().toISOString() };
  }
  return request(`/clients/${clientId}/monitor/settings`, {
    method: "PATCH",
    body: JSON.stringify(patch),
  }, `monitor-settings-patch-${clientId}`);
}

// Binary downloads: deliberately NOT routed through `request()`, which sets
// `Accept: application/json` and parses the body. The UI links straight to them.
export function monitorExportCsvUrl(clientId) {
  return `${API_ROOT}/clients/${clientId}/monitor/export.csv`;
}

export function monitorExportXlsxUrl(clientId) {
  return `${API_ROOT}/clients/${clientId}/monitor/export.xlsx`;
}

// --- Reports (task-43) -------------------------------------------------------
//
// Three distinct intents against the same run id share nothing (task-43 §5.1
// — this already broke Site Monitor twice, tasks 36/38): the history table's
// `reports-history`, the live-row/detail-page snapshot poll's
// `reports-run-poll-<id>`, and the one-shot finished table/file list's
// `reports-run-table-<id>` / `reports-run-files-<id>`. All four are
// independent `request()` keys, never derived from each other.

const REPORT_METRICS = [
  ["users", "Users"], ["organic_users", "Organic Users"], ["direct_users", "Direct Users"],
  ["conversions", "Total Conversions"], ["organic_conversions", "Organic Conversions"],
  ["direct_conversions", "Direct Conversions"], ["clicks", "Clicks"], ["impressions", "Impressions"],
];
const REPORT_GSC_KEYS = new Set(["clicks", "impressions"]);
const MONTHLY_CHANGE_DEFS = [
  ["% M/M", "This Month", "Last Month"],
  ["% Q/Q", "This Quarter", "Last Quarter"],
  ["% M YOY", "This Month", "Month YOY"],
  ["% Q YOY", "This Quarter", "Quarterly YOY"],
];
const MONTH_NAMES_EN = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December",
];

function pad2(n) { return String(n).padStart(2, "0"); }
function isoDate(y, m, d) { return `${y}-${pad2(m)}-${pad2(d)}`; }
function lastDayOfMonth(y, m) { return new Date(y, m, 0).getDate(); }
function monthBack(y, m, n) {
  let year = y;
  let month = m - n;
  while (month <= 0) { month += 12; year -= 1; }
  return [year, month];
}
function monthLabel(y, m) { return `${MONTH_NAMES_EN[m - 1]} ${y}`; }
function monthBoundsFor(year, month, today) {
  const ty = today.getFullYear();
  const tm = today.getMonth() + 1;
  const td = today.getDate();
  if (year > ty || (year === ty && month > tm)) return null; // future month — invalid, mirrors periods.py
  const dateFrom = isoDate(year, month, 1);
  if (year === ty && month === tm) return { date_from: dateFrom, date_to: isoDate(ty, tm, td), is_partial: true };
  return { date_from: dateFrom, date_to: isoDate(year, month, lastDayOfMonth(year, month)), is_partial: false };
}

// Ported from `app/reports/periods.py` (task-42 §2.4/§3.1). There is no
// dry-run/preview endpoint — `POST /reports/runs` is the only place the real
// engine computes periods — so the wizard's step-2 "what will actually run"
// preview (task-43 §3) has to reproduce the same date math client-side. Kept
// as one pure function so drift from the backend is a one-file diff to spot.
export function computeReportPeriods(kind, params, today = new Date()) {
  if (kind === "monthly") {
    let refYear = today.getFullYear();
    let refMonth = today.getMonth(); // 0-based "this month" == 1-based "previous month"
    if (refMonth === 0) { refMonth = 12; refYear -= 1; }
    const refDay = lastDayOfMonth(refYear, refMonth);
    const thisMonthStart = isoDate(refYear, refMonth, 1);
    const thisMonthEnd = isoDate(refYear, refMonth, refDay);

    const [lmY, lmM] = monthBack(refYear, refMonth, 1);
    const lastMonthStart = isoDate(lmY, lmM, 1);
    const lastMonthEnd = isoDate(lmY, lmM, lastDayOfMonth(lmY, lmM));

    const monthYoyStart = isoDate(refYear - 1, refMonth, 1);
    const monthYoyEnd = isoDate(refYear - 1, refMonth, lastDayOfMonth(refYear - 1, refMonth));

    const [tqY, tqM] = monthBack(refYear, refMonth, 2);
    const thisQuarterStart = isoDate(tqY, tqM, 1);
    const thisQuarterEnd = thisMonthEnd;

    const [lqeY, lqeM] = monthBack(tqY, tqM, 1);
    const [lqY, lqM] = monthBack(lqeY, lqeM, 2);
    const lastQuarterStart = isoDate(lqY, lqM, 1);
    const lastQuarterEnd = isoDate(lqeY, lqeM, lastDayOfMonth(lqeY, lqeM));

    const qyoyStart = isoDate(tqY - 1, tqM, 1);
    const qyoyEnd = monthYoyEnd; // same calendar month/year as Month YOY's end, by construction

    return [
      { label: "This Month", date_from: thisMonthStart, date_to: thisMonthEnd, is_partial: false },
      { label: "Last Month", date_from: lastMonthStart, date_to: lastMonthEnd, is_partial: false },
      { label: "Month YOY", date_from: monthYoyStart, date_to: monthYoyEnd, is_partial: false },
      { label: "This Quarter", date_from: thisQuarterStart, date_to: thisQuarterEnd, is_partial: false },
      { label: "Last Quarter", date_from: lastQuarterStart, date_to: lastQuarterEnd, is_partial: false },
      { label: "Quarterly YOY", date_from: qyoyStart, date_to: qyoyEnd, is_partial: false },
    ];
  }

  if (kind === "yearly") {
    const span = params.span === "ytd" ? "ytd" : "full";
    let refYear = today.getFullYear();
    let refMonth = today.getMonth();
    if (refMonth === 0) { refMonth = 12; refYear -= 1; }
    const refIso = isoDate(refYear, refMonth, lastDayOfMonth(refYear, refMonth));
    const years = [...new Set(params.years || [])].filter((year) => year <= refYear).sort((a, b) => b - a);
    return years.map((year) => {
      const dateFrom = isoDate(year, 1, 1);
      if (span === "ytd") {
        return { label: String(year), date_from: dateFrom, date_to: isoDate(year, refMonth, lastDayOfMonth(year, refMonth)), is_partial: false };
      }
      const isCurrent = year === refYear;
      return { label: String(year), date_from: dateFrom, date_to: isCurrent ? refIso : isoDate(year, 12, 31), is_partial: isCurrent };
    });
  }

  if (kind === "custom_months" || kind === "comparator") {
    const parsed = [...new Set(params.months || [])]
      .map((value) => value.split("-").map(Number))
      .sort((a, b) => (b[0] - a[0]) || (b[1] - a[1]));
    return parsed
      .map(([year, month]) => {
        const bounds = monthBoundsFor(year, month, today);
        return bounds ? { label: monthLabel(year, month), ...bounds } : null;
      })
      .filter(Boolean);
  }

  return [];
}

function reportsFixtureName() {
  return FIXTURE_MODE === "reports-empty" ? "reports-empty" : "reports-runs";
}

let reportsHistoryCache = null;
let reportsHistoryCacheName = null;
async function loadReportsHistory() {
  const name = reportsFixtureName();
  if (!reportsHistoryCache || reportsHistoryCacheName !== name) {
    reportsHistoryCacheName = name;
    reportsHistoryCache = fixture(name);
  }
  return reportsHistoryCache;
}

let reportRunBundleCache = null;
async function loadReportRunBundle() {
  if (!reportRunBundleCache) reportRunBundleCache = fixture("reports-run-bundle");
  return reportRunBundleCache;
}

const REPORTS_MOCK_KEY = "yovth:reports-mock-runs";
// Compressed demo pacing — real columns take ~2s (API) / ~55s (PDF) each
// (task-42 §1.2); a wizard-created fixture run instead finishes in a few
// seconds so the live-progress/SSE-log states are actually exercisable in
// visual QA and e2e without a 4-minute wait.
const REPORT_PERIOD_MS = { api: 260, pdf: 900 };
const REPORT_QUEUE_MS = 350;

function readMockReportRuns() {
  try { return JSON.parse(sessionStorage.getItem(REPORTS_MOCK_KEY)) || {}; }
  catch { return {}; }
}
function writeMockReportRuns(runs) { sessionStorage.setItem(REPORTS_MOCK_KEY, JSON.stringify(runs)); }

function hashString(value) {
  let hash = 0;
  for (let i = 0; i < value.length; i += 1) hash = (hash * 31 + value.charCodeAt(i)) >>> 0;
  return hash;
}

function isMockReportRunId(runId) { return runId.startsWith("fixture-report-"); }

function baseMockRunDto(stored, overrides) {
  const dto = {
    id: stored.id, client_id: stored.client_id, kind: stored.kind, source: stored.source,
    period_count: stored.period_count, periods_failed: 0, cross_check_status: null,
    error_code: null, error_message: null, cancel_requested: false, params: stored.params || {},
    ninja_report_name: stored.source === "pdf"
      ? `${stored.client_id.replace("client-", "").toUpperCase()}-Internal report` : null,
    batch_id: stored.batch_id || null,
    queued_at: stored.queued_at, started_at: null, finished_at: null,
    ...overrides,
  };
  dto.periods = mockReportPeriods(stored, dto);
  return dto;
}

// `ReportRunDTO.periods` (task-45 §2.1) — the real engine writes these states;
// the fixture engine has to produce the same shape or the progress list has
// nothing to read (task-43 learned the same lesson about `details.period`).
// `?demo=cache` (task-49 §2.6): every finished period on a PDF run reports
// as reused from a PDF downloaded yesterday — the fixture-mode way to
// exercise the "From cache" chip / freshness banner without a real cache.
function mockCacheDemoDate() {
  if (new URLSearchParams(location.search).get("demo") !== "cache") return null;
  return new Date(Date.now() - 24 * 60 * 60 * 1000).toISOString().slice(0, 10);
}

function mockReportPeriods(stored, dto) {
  const perPeriodMs = REPORT_PERIOD_MS[stored.source] || REPORT_PERIOD_MS.api;
  const terminal = ["succeeded", "partial", "failed", "cancelled"].includes(dto.status);
  const cachedAt = stored.source === "pdf" ? mockCacheDemoDate() : null;
  return (stored.periods || []).map((period, index) => {
    const done = index < dto.periods_done;
    const running = dto.status === "running" && index === dto.periods_done;
    const startedAtMs = stored.startedAtMs + index * perPeriodMs;
    const cached = done && Boolean(cachedAt);
    return {
      label: period.label,
      date_from: period.date_from,
      date_to: period.date_to,
      is_partial: period.is_partial,
      state: cached ? "cached" : done ? "done" : running ? "running" : terminal ? "skipped" : "waiting",
      started_at: done || running ? new Date(startedAtMs).toISOString() : null,
      finished_at: done ? new Date(startedAtMs + perPeriodMs).toISOString() : null,
      duration_seconds: done ? perPeriodMs / 1000 : null,
      from_cache: cached,
      cached_at: cached ? cachedAt : null,
      error: null,
    };
  });
}

// 12 complete months, oldest first, ending at the last complete month — the
// same window `app/reports/history.py` builds. One month is deliberately
// data-less so the `—`/empty-CTR path is exercised in every visual pass
// (task-45 §1.6).
function mockReportHistory(runId, { noGsc = false } = {}) {
  const today = new Date();
  const months = [];
  for (let back = 12; back >= 1; back -= 1) {
    const anchor = new Date(today.getFullYear(), today.getMonth() - back, 1);
    const year = anchor.getFullYear();
    const month = anchor.getMonth() + 1;
    const key = `${year}-${pad2(month)}`;
    const empty = back === 11;
    const clicks = empty || noGsc ? null : 1400 + (hashString(`${runId}-clicks-${key}`) % 1600);
    const impressions = empty || noGsc ? null : 28000 + (hashString(`${runId}-impr-${key}`) % 120000);
    months.push({
      month: key,
      label: `${MONTH_NAMES_EN[month - 1]} ${year}`,
      clicks,
      impressions,
      ctr: clicks === null || !impressions ? null : clicks / impressions,
      organic_conversions: empty ? null : 380 + (hashString(`${runId}-conv-${key}`) % 520),
    });
  }
  return { months, has_gsc: !noGsc, has_ga4: true };
}

function computeMockReportRun(runId) {
  const stored = readMockReportRuns()[runId];
  if (!stored) throw new Error("Report run not found");
  const now = Date.now();
  // A cancel while still `queued` (never even started) must stick — checked
  // before the queued early-return, not after, or the run would ignore the
  // cancellation entirely and quietly run to completion once its start time
  // arrives.
  if (stored.cancelledAtMs && now < stored.startedAtMs) {
    return baseMockRunDto(stored, {
      status: "cancelled", periods_done: 0, cancel_requested: true,
      finished_at: new Date(stored.cancelledAtMs).toISOString(),
    });
  }
  if (now < stored.startedAtMs) return baseMockRunDto(stored, { status: "queued", periods_done: 0 });

  // `?demo=report_name_missing` (task-49 §1.2/§2.3): mirrors the live
  // incident — a PDF run for a client with no saved report name fails in 0
  // seconds, before any period even starts.
  if (
    stored.source === "pdf" && !stored.cancelledAtMs
    && new URLSearchParams(location.search).get("demo") === "report_name_missing"
  ) {
    return baseMockRunDto(stored, {
      status: "failed", periods_done: 0, started_at: new Date(stored.startedAtMs).toISOString(),
      finished_at: new Date(stored.startedAtMs).toISOString(),
      error_code: "report_name_missing",
      error_message: "Reporting Ninja report not set. Open the report wizard and use "
        + "Connect next to this client, or exclude it from the batch.",
    });
  }

  const perPeriodMs = REPORT_PERIOD_MS[stored.source] || REPORT_PERIOD_MS.api;
  const cancelled = Boolean(stored.cancelledAtMs);
  const doneAtCancel = cancelled ? Math.floor((stored.cancelledAtMs - stored.startedAtMs) / perPeriodMs) : Infinity;
  const doneRaw = Math.floor((now - stored.startedAtMs) / perPeriodMs);
  const done = Math.max(0, Math.min(stored.period_count, doneRaw, doneAtCancel));
  const startedIso = new Date(stored.startedAtMs).toISOString();

  if (cancelled && now > stored.cancelledAtMs + 500) {
    return baseMockRunDto(stored, {
      status: "cancelled", periods_done: done, started_at: startedIso,
      finished_at: new Date(stored.cancelledAtMs + 500).toISOString(), cancel_requested: true,
    });
  }
  if (!cancelled && done >= stored.period_count) {
    const mismatchDemo = new URLSearchParams(location.search).get("demo") === "mismatch";
    return baseMockRunDto(stored, {
      status: "succeeded", periods_done: stored.period_count, started_at: startedIso,
      finished_at: new Date(stored.startedAtMs + stored.period_count * perPeriodMs).toISOString(),
      cross_check_status: stored.source === "pdf" ? (mismatchDemo ? "mismatch" : "match") : null,
    });
  }
  return baseMockRunDto(stored, {
    status: "running", periods_done: done, started_at: startedIso, cancel_requested: cancelled,
  });
}

function mkMockEvent(seq, level, stage, message, timestamp, details = {}) {
  return { seq, level, provider: null, stage, percent: null, message, details, timestamp: timestamp || new Date().toISOString() };
}

// `details.period` mirrors the real backend's `emit(level, stage, message,
// {"period": period.label})` contract (`app/reports/runner.py`,
// `app/reports/ninja_browser.py`) — `reports-run.js`'s live progress list
// derives per-period state entirely from this field, so a mock event without
// it is invisible to that UI (found live: the fixture's progress rows never
// left "Waiting" until this was added).
function mockReportRunEvents(runId, lastSeq) {
  const stored = readMockReportRuns()[runId];
  if (!stored) return [];
  const run = computeMockReportRun(runId);
  const events = [];
  let seq = 1;
  events.push(mkMockEvent(seq++, "info", "queued", `Queued — ${stored.period_count} period(s) via ${stored.source === "pdf" ? "PDF" : "API"}.`, stored.queued_at));
  if (run.status !== "queued") {
    events.push(mkMockEvent(seq++, "info", "start", stored.source === "pdf" ? "Signed in to Reporting Ninja — starting downloads." : "Querying Reporting Ninja API.", run.started_at));
    for (let i = 0; i < run.periods_done; i += 1) {
      const period = stored.periods[i];
      events.push(mkMockEvent(seq++, "info", stored.source === "pdf" ? "download" : "fetch", `${period.label} done`, run.started_at, { period: period.label }));
    }
    if (run.status === "running" && run.periods_done < stored.periods.length) {
      const running = stored.periods[run.periods_done];
      events.push(mkMockEvent(seq++, "info", stored.source === "pdf" ? "download" : "fetch", `Downloading ${running.label}`, run.started_at, { period: running.label }));
    }
    if (run.status === "succeeded") {
      if (run.cross_check_status) {
        events.push(mkMockEvent(
          seq++, run.cross_check_status === "mismatch" ? "warning" : "info", "cross_check",
          run.cross_check_status === "mismatch"
            ? "Cross-check found differences — see the Cross-check panel below."
            : "Cross-check passed — API matches the PDF for every value.",
          run.finished_at,
        ));
      }
      events.push(mkMockEvent(seq++, "info", "complete", "Report finished.", run.finished_at));
    } else if (run.status === "cancelled") {
      events.push(mkMockEvent(seq++, "warning", "cancel", "Cancelled by operator — already-downloaded files were kept.", run.finished_at));
    }
  }
  return events.filter((event) => event.seq > lastSeq);
}

function computeMockReportTable(runId) {
  const stored = readMockReportRuns()[runId];
  if (!stored) throw new Error("Report run not found");
  const run = computeMockReportRun(runId);
  const noGsc = new URLSearchParams(location.search).get("demo") === "no-gsc";
  const periods = stored.periods;
  const byLabel = new Map(periods.map((p, i) => [p.label, i]));
  const changeDefs = stored.kind === "monthly"
    ? MONTHLY_CHANGE_DEFS.filter(([, newer, older]) => byLabel.has(newer) && byLabel.has(older))
    : periods.slice(0, -1).map((p, i) => [`${p.label} vs ${periods[i + 1].label}`, p.label, periods[i + 1].label]);

  const rows = REPORT_METRICS.map(([key, label], metricIndex) => {
    const values = periods.map((p, i) => {
      // A period past `periods_done` (e.g. everything after a cancel, or the
      // failed one in a partial run) was never actually collected — `None`,
      // never a fabricated number (PLAN §12), matching how the real engine's
      // `metrics_by_label.get(p.label)` comes back empty for it too.
      if (i >= run.periods_done) return null;
      if (REPORT_GSC_KEYS.has(key) && noGsc) return null;
      const base = 300 + metricIndex * 220 + i * 41;
      return base + (hashString(`${runId}-${key}-${p.label}`) % 190);
    });
    const changes = changeDefs.map(([, newerLabel, olderLabel]) => {
      const newer = values[byLabel.get(newerLabel)];
      const older = values[byLabel.get(olderLabel)];
      if (newer === null || older === null || !older) return null;
      return (newer - older) / older;
    });
    return { key, label, values, changes };
  });

  const sources = stored.source === "pdf"
    ? Object.fromEntries(periods.map((p) => [p.label, `${stored.client_id}__${p.label.replace(/\s+/g, "_")}.pdf`]))
    : {};
  // Shape matches `CrossCheckResult.to_dict()` (app/reports/verify.py)
  // exactly — one cell per metric x period, never a pre-filtered list —
  // real reports.cjs/live gate for how this was found wrong once already.
  let crossCheck = null;
  if (run.cross_check_status) {
    const forcedOffIndex = run.cross_check_status === "mismatch" ? 1 : run.cross_check_status === "within_tolerance" ? 0 : -1;
    const cells = REPORT_METRICS.flatMap(([key, label], metricIndex) => periods.map((p, periodIndex) => {
      const pdfValue = rows[metricIndex].values[periodIndex];
      const isOff = metricIndex === forcedOffIndex && periodIndex === 0;
      const apiValue = isOff ? Math.max(0, (pdfValue || 0) - 140) : pdfValue;
      const verdict = !isOff ? "match" : run.cross_check_status;
      const diffPct = pdfValue === null || apiValue === null || pdfValue === apiValue ? (isOff ? null : 0)
        : Math.abs(pdfValue - apiValue) / (apiValue || pdfValue || 1);
      return { period_label: p.label, metric_key: key, metric_label: label, pdf_value: pdfValue, api_value: apiValue, diff_pct: isOff ? diffPct : 0, verdict };
    }));
    crossCheck = { status: run.cross_check_status, cells };
  }
  return {
    run_id: runId, kind: stored.kind, source: stored.source, periods,
    change_labels: changeDefs.map((c) => c[0]), rows, sources, cross_check: crossCheck,
    history: mockReportHistory(runId, { noGsc }),
  };
}

function computeMockReportFiles(runId) {
  const stored = readMockReportRuns()[runId];
  if (!stored) throw new Error("Report run not found");
  const run = computeMockReportRun(runId);
  const files = [];
  if (stored.source === "pdf") {
    stored.periods.slice(0, run.periods_done).forEach((period, i) => {
      files.push({
        id: `${runId}-pdf-${i}`, run_id: runId, kind: "pdf", period_label: period.label,
        date_from: period.date_from, date_to: period.date_to, is_partial_period: period.is_partial,
        size_bytes: 240_000 + (hashString(`${runId}-${period.label}`) % 500_000), mime: "application/pdf",
        created_at: run.finished_at || new Date().toISOString(),
      });
    });
  }
  if (run.status === "succeeded" || run.status === "partial") {
    files.push({
      id: `${runId}-xlsx`, run_id: runId, kind: "xlsx", period_label: null,
      date_from: null, date_to: null, is_partial_period: false,
      size_bytes: 38_400, mime: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
      created_at: run.finished_at || new Date().toISOString(),
    });
    // Auto-generated on every successful/partial `monthly` run (task-47 §5)
    // — the fixture engine mirrors that so the run detail page's iframe
    // preview + "Client PDF" download button are exercisable without a
    // real backend.
    if (stored.kind === "monthly") {
      files.push({
        id: `${runId}-client-pdf`, run_id: runId, kind: "client_pdf", period_label: null,
        date_from: null, date_to: null, is_partial_period: false,
        size_bytes: 118_000, mime: "application/pdf",
        created_at: run.finished_at || new Date().toISOString(),
      });
    }
  }
  return files;
}

export async function getReportTypes() {
  if (!FIXTURE_MODE) return request("/reports/types", {}, "reports-types");
  await wait(120);
  return fixture("reports-types");
}

export async function listReportRuns({
  clientId, kind, source, status, hasBatch, limit = 50, offset = 0,
} = {}) {
  if (!FIXTURE_MODE) {
    const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
    if (clientId) params.set("client_id", clientId);
    if (kind) params.set("kind", kind);
    if (source) params.set("source", source);
    if (status) params.set("status", status);
    if (hasBatch !== undefined) params.set("has_batch", String(hasBatch));
    return request(`/reports/runs?${params}`, {}, "reports-history");
  }
  if (FIXTURE_MODE === "error") { await wait(400); throw new Error("Fixture connection error"); }
  await wait(FIXTURE_MODE === "loading" ? 2400 : 180);
  const seed = await loadReportsHistory();
  const mockRuns = Object.keys(readMockReportRuns()).map((id) => computeMockReportRun(id));
  let rows = [...mockRuns, ...(seed.rows || [])];
  if (clientId) rows = rows.filter((row) => row.client_id === clientId);
  if (kind) rows = rows.filter((row) => row.kind === kind);
  if (source) rows = rows.filter((row) => row.source === source);
  if (status) rows = rows.filter((row) => row.status === status);
  if (hasBatch !== undefined) rows = rows.filter((row) => Boolean(row.batch_id) === hasBatch);
  rows = [...rows].sort((a, b) => Date.parse(b.queued_at) - Date.parse(a.queued_at));
  return { rows: rows.slice(offset, offset + limit), total: rows.length, limit, offset };
}

const COMPARATOR_MONTH_RE = /^\d{4}-(0[1-9]|1[0-2])$/;

export async function createReportRun(payload) {
  if (!FIXTURE_MODE) return request("/reports/runs", { method: "POST", body: JSON.stringify(payload) }, "reports-create-run");
  await wait(220);
  if (payload.kind === "comparator") {
    const months = payload.months || [];
    if (!months.every((m) => COMPARATOR_MONTH_RE.test(m)) || new Set(months.map((m) => m.slice(5))).size !== 1) {
      const error = new Error("All comparator months must share the same calendar month — for arbitrary months, use Custom months.");
      error.status = 422; error.code = "invalid_comparator_months";
      throw error;
    }
  }
  const periods = computeReportPeriods(payload.kind, payload, new Date());
  if (!periods.length) {
    const error = new Error("Could not resolve any period for this configuration.");
    error.status = 422; error.code = "invalid_period";
    throw error;
  }
  const id = `fixture-report-${Date.now()}`;
  const runs = readMockReportRuns();
  const { client_id: _clientId, kind: _kind, source: _source, ...params } = payload;
  runs[id] = {
    id, client_id: payload.client_id, kind: payload.kind, source: payload.source,
    periods, period_count: periods.length, params,
    queued_at: new Date().toISOString(), startedAtMs: Date.now() + REPORT_QUEUE_MS,
  };
  writeMockReportRuns(runs);
  return { run_id: id, status_url: `/api/v1/reports/runs/${id}`, events_url: `/api/v1/reports/runs/${id}/events` };
}

export async function getReportRun(runId) {
  if (!FIXTURE_MODE) return request(`/reports/runs/${runId}`, {}, `reports-run-poll-${runId}`);
  await wait(100);
  if (isMockReportRunId(runId)) return computeMockReportRun(runId);
  const bundle = await loadReportRunBundle();
  const entry = bundle[runId];
  if (!entry) throw new Error("Report run not found");
  return entry.run;
}

// Real mode never calls this — `reports-run.js` uses a native `EventSource`
// against `reportRunEventsUrl()` instead (browsers auto-resend
// `Last-Event-ID` on reconnect, and the backend already reads it). This is
// fixture mode's substitute, polled on an interval exactly like `sse.js`
// falls back to `setInterval(refresh, ...)` under `isFixtureMode()`.
export async function getReportRunEvents(runId, lastSeq = 0) {
  if (isMockReportRunId(runId)) return mockReportRunEvents(runId, lastSeq);
  const bundle = await loadReportRunBundle();
  const events = bundle[runId]?.events || [];
  return events.filter((event) => event.seq > lastSeq);
}

export async function cancelReportRun(runId) {
  if (!FIXTURE_MODE) return request(`/reports/runs/${runId}/cancel`, { method: "POST", body: "{}" });
  await wait(150);
  if (!isMockReportRunId(runId)) throw new Error("This run has already finished");
  const runs = readMockReportRuns();
  if (runs[runId] && !runs[runId].cancelledAtMs) {
    runs[runId].cancelledAtMs = Date.now();
    writeMockReportRuns(runs);
  }
  return computeMockReportRun(runId);
}

export async function getReportTable(runId) {
  if (!FIXTURE_MODE) return request(`/reports/runs/${runId}/table`, {}, `reports-run-table-${runId}`);
  await wait(150);
  if (isMockReportRunId(runId)) return computeMockReportTable(runId);
  const bundle = await loadReportRunBundle();
  if (!bundle[runId]?.table) throw new Error("Table not available for this run");
  return bundle[runId].table;
}

export async function listReportFiles(runId) {
  if (!FIXTURE_MODE) return request(`/reports/runs/${runId}/files`, {}, `reports-run-files-${runId}`);
  await wait(150);
  if (isMockReportRunId(runId)) return computeMockReportFiles(runId);
  const bundle = await loadReportRunBundle();
  return bundle[runId]?.files || [];
}

// Binary/streaming links: deliberately NOT routed through `request()`, same
// convention as `techAuditExportUrl`/`monitorExportCsvUrl` above.
export function reportFileUrl(fileId, { inline = false } = {}) {
  return `${API_ROOT}/reports/files/${fileId}?disposition=${inline ? "inline" : "attachment"}`;
}

export function reportRunEventsUrl(runId) {
  return `${API_ROOT}/reports/runs/${runId}/events`;
}

// `matches` / `others` mirror the backend split (task-45 §2.2): reports whose
// name starts with the client's name versus the rest of the account. The
// fixture always includes `others`, because that is the case the wizard's
// confirmation gate exists for.
// `refresh: true` re-reads the Reporting Ninja cabinet instead of answering
// from the shared account snapshot — for the operator who has just created
// this client's report and will not wait out the snapshot's TTL.
export async function getNinjaReportName(clientId, { refresh = false } = {}) {
  if (!FIXTURE_MODE) {
    return request(
      `/reports/clients/${clientId}/ninja-report${refresh ? "?refresh=true" : ""}`,
      {},
      `reports-ninja-report-${clientId}`,
    );
  }
  if (FIXTURE_MODE === "error") { await wait(300); throw new Error("Fixture connection error"); }
  await wait(220);
  const configured = clientId === "client-nyc-hearing";
  const slug = clientId.replace("client-", "").toUpperCase();
  return {
    client_id: clientId,
    client_name: slug,
    ninja_report_name: configured ? "NYC-HEARING-Internal report" : null,
    matches: [
      { name: `${slug}-Internal report`, exact_match: true },
      { name: `${slug}-client`, exact_match: true },
    ],
    others: [
      { name: "EFFYS-Internal report", exact_match: false },
      { name: "SAPHNY-client", exact_match: false },
    ],
  };
}

export async function putNinjaReportName(clientId, name, { confirmMismatch = false } = {}) {
  if (!FIXTURE_MODE) {
    return request(`/reports/clients/${clientId}/ninja-report`, {
      method: "PUT",
      body: JSON.stringify({ ninja_report_name: name, confirm_mismatch: confirmMismatch }),
    }, `reports-ninja-report-save-${clientId}`);
  }
  await wait(260);
  return { client_id: clientId, ninja_report_name: name, matches: [], others: [] };
}

// task-49 §2.1: one cheap pass over every client's binding state, read
// BEFORE the batch wizard's Start button — the fixture mirrors the exact
// mix the task doc's own example table shows (one of each state), keyed to
// the four clients `dashboard-all-states.json` already defines, so the
// wizard's client list and this call always agree on which client is which.
const NINJA_BINDINGS_FIXTURE = {
  "client-nyc-hearing": {
    connected: true, ninja_report_name: "NYC-HEARING-Internal report", state: "ready",
  },
  "client-cleveland": { connected: true, ninja_report_name: null, state: "no_report_name" },
  "client-long-name": {
    connected: true, ninja_report_name: "EFFYS-Internal report", state: "foreign_binding",
  },
  "client-onboarding": { connected: false, ninja_report_name: null, state: "not_connected" },
};

export async function getNinjaBindings({ requestKey } = {}) {
  if (!FIXTURE_MODE) {
    return request("/reports/ninja-bindings", {}, requestKey || "reports-ninja-bindings");
  }
  if (FIXTURE_MODE === "error") { await wait(300); throw new Error("Fixture connection error"); }
  await wait(200);
  const payload = await fixture(FIXTURE_MODE === "empty" ? "dashboard-empty" : "dashboard-all-states");
  return {
    rows: payload.clients.map((c) => {
      const binding = NINJA_BINDINGS_FIXTURE[c.client_id] || {
        connected: false, ninja_report_name: null, state: "not_connected",
      };
      return {
        client_id: c.client_id, name: c.name, domain: c.domain,
        connected: binding.connected, ninja_report_name: binding.ninja_report_name,
        state: binding.state,
      };
    }),
  };
}

// --- Client document + PDF (task-47 §2.1/§3.3) ------------------------------

export function reportDocumentUrl(runId) {
  return `${API_ROOT}/reports/runs/${runId}/document`;
}

export async function createClientPdf(runId) {
  if (!FIXTURE_MODE) {
    return request(`/reports/runs/${runId}/client-pdf`, { method: "POST", body: "{}" });
  }
  await wait(400);
  return {
    id: `${runId}-client-pdf`, run_id: runId, kind: "client_pdf", period_label: null,
    date_from: null, date_to: null, is_partial_period: false,
    size_bytes: 118_000, mime: "application/pdf", created_at: new Date().toISOString(),
  };
}

// Manual per-period retry (task-47 follow-up): re-collects ONE failed
// period in place rather than restarting the whole report. Fixture mode has
// no live "failed → retried" state machine for a MOCK run (the fixture
// engine's periods never fail — see `mockReportPeriods`), so this only does
// something useful against the static `reports-run-bundle` fixture, which
// is exactly where the pre-baked "failed"/"partial" demo states live.
export async function retryReportPeriod(runId, label) {
  if (!FIXTURE_MODE) {
    return request(
      `/reports/runs/${runId}/periods/${encodeURIComponent(label)}/retry`,
      { method: "POST", body: "{}" },
    );
  }
  await wait(500);
  const bundle = await loadReportRunBundle();
  const entry = bundle[runId];
  if (!entry) throw new Error("Report run not found");
  const period = (entry.run.periods || []).find((p) => p.label === label);
  if (!period) throw new Error("No such period on this run");
  // task-49 §2.5: `cached`/`done` are retryable too now ("Rebuild"), not
  // only `failed` — mirrors the relaxed backend gate in `period_retry.py`.
  if (!["failed", "cached", "done"].includes(period.state)) {
    throw new Error("This period is not retryable yet");
  }

  period.state = "done";
  period.error = null;
  period.cached_at = null;
  period.finished_at = new Date().toISOString();
  const stillFailed = (entry.run.periods || []).filter((p) => p.state === "failed").length;
  entry.run.periods_failed = stillFailed;
  entry.run.periods_done = (entry.run.periods || []).filter((p) => ["done", "cached"].includes(p.state)).length;
  entry.run.status = stillFailed === 0 ? "succeeded" : "partial";

  if (entry.table) {
    const periodIndex = (entry.table.periods || []).findIndex((p) => p.label === label);
    if (periodIndex !== -1) {
      entry.table.rows.forEach((row) => {
        if (row.values[periodIndex] === null) {
          row.values[periodIndex] = 300 + (hashString(`${runId}-${label}-${row.key}-retry`) % 900);
        }
      });
    }
  }
  return entry.run;
}

// task-49 §2.4/§2.5: clones a mock run with an explicit `force_refresh`,
// preserving its `batch_id` — the fixture-mode mirror of `rebuild_run`
// (`app/reports/batch.py`). Replaces the source run's slot in its parent
// batch's `runIds` (same index) so the matrix shows the new attempt in
// place of the old one, never both.
function rebuildMockRun(sourceRunId, forceRefresh) {
  const runs = readMockReportRuns();
  const source = runs[sourceRunId];
  if (!source) throw new Error("Report run not found");
  const newId = `fixture-report-${Date.now()}-rebuild`;
  runs[newId] = {
    id: newId, client_id: source.client_id, kind: source.kind, source: source.source,
    batch_id: source.batch_id || null, periods: source.periods, period_count: source.period_count,
    params: { ...source.params, force_refresh: forceRefresh },
    queued_at: new Date().toISOString(), startedAtMs: Date.now() + REPORT_QUEUE_MS,
  };
  writeMockReportRuns(runs);
  if (source.batch_id) {
    const batches = readMockReportBatches();
    const stored = batches[source.batch_id];
    if (stored) {
      stored.runIds = stored.runIds.map((id) => (id === sourceRunId ? newId : id));
      writeMockReportBatches(batches);
    }
  }
  return newId;
}

export async function rebuildReportRun(runId, { forceRefresh = true } = {}) {
  if (!FIXTURE_MODE) {
    return request(`/reports/runs/${runId}/rebuild`, {
      method: "POST", body: JSON.stringify({ force_refresh: forceRefresh }),
    }, `reports-rebuild-run-${runId}`);
  }
  await wait(280);
  const newRunId = rebuildMockRun(runId, forceRefresh);
  return {
    run_id: newRunId,
    status_url: `/api/v1/reports/runs/${newRunId}`,
    events_url: `/api/v1/reports/runs/${newRunId}/events`,
  };
}

// --- Batches (task-47 §2.2) --------------------------------------------------
//
// Fixture mode composes on top of the existing single-run mock engine
// (`readMockReportRuns`/`computeMockReportRun`) rather than duplicating it —
// a batch IS "N ordinary runs + an aggregate" on the real backend too
// (task-47 §2.2), so the fixture keeps that same shape: every batch child is
// a perfectly normal mock run, just tagged with `batch_id`.

const REPORTS_BATCH_MOCK_KEY = "yovth:reports-mock-batches";
const REPORT_BATCH_TERMINAL = new Set(["succeeded", "partial", "failed", "cancelled"]);

function readMockReportBatches() {
  try { return JSON.parse(sessionStorage.getItem(REPORTS_BATCH_MOCK_KEY)) || {}; }
  catch { return {}; }
}
function writeMockReportBatches(batches) {
  sessionStorage.setItem(REPORTS_BATCH_MOCK_KEY, JSON.stringify(batches));
}
function isMockReportBatchId(batchId) { return batchId.startsWith("fixture-batch-"); }

function computeMockReportBatch(batchId) {
  const stored = readMockReportBatches()[batchId];
  if (!stored) throw new Error("Report batch not found");
  const children = stored.runIds.map((id) => computeMockReportRun(id));
  const done = children.filter((r) => r.status === "succeeded" || r.status === "partial").length;
  const failed = children.filter((r) => r.status === "failed").length;
  const anyRunning = children.some((r) => !REPORT_BATCH_TERMINAL.has(r.status));

  let status = children.every((r) => r.status === "queued") ? "queued" : "running";
  if (!anyRunning) {
    status = stored.cancelledAtMs ? "cancelled"
      : failed === 0 ? "succeeded"
        : failed >= children.length ? "failed" : "partial";
  }
  const startedTimes = children.map((r) => r.started_at).filter(Boolean).sort();

  return {
    id: batchId, kind: stored.kind, source: stored.source, status,
    client_count: children.length, clients_done: done, clients_failed: failed,
    cancel_requested: Boolean(stored.cancelledAtMs), params: stored.params || {},
    clients: children.map((run) => ({
      run_id: run.id, client_id: run.client_id, client_name: null, status: run.status,
      period_count: run.period_count, periods_done: run.periods_done,
      periods_failed: run.periods_failed, error_message: run.error_message,
      error_code: run.error_code,
      cached_count: (run.periods || []).filter((p) => p.state === "cached").length,
      started_at: run.started_at, finished_at: run.finished_at,
    })),
    queued_at: stored.queued_at,
    started_at: startedTimes[0] || null,
    finished_at: anyRunning ? null : new Date().toISOString(),
  };
}

export async function createReportBatch(payload) {
  if (!FIXTURE_MODE) {
    return request("/reports/batches", { method: "POST", body: JSON.stringify(payload) }, "reports-create-batch");
  }
  await wait(280);
  const { client_ids: clientIds, kind, source, ...params } = payload;
  if (!clientIds || !clientIds.length) {
    const error = new Error("At least one client must be selected");
    error.status = 422; error.code = "no_clients_selected";
    throw error;
  }
  const periods = computeReportPeriods(kind, params, new Date());
  const batchId = `fixture-batch-${Date.now()}`;
  const runs = readMockReportRuns();
  const runIds = clientIds.map((clientId, index) => {
    const id = `fixture-report-${Date.now()}-${index}`;
    runs[id] = {
      id, client_id: clientId, kind, source, batch_id: batchId,
      periods, period_count: periods.length, params,
      queued_at: new Date().toISOString(),
      startedAtMs: Date.now() + REPORT_QUEUE_MS + index * 60,
    };
    return id;
  });
  writeMockReportRuns(runs);
  const batches = readMockReportBatches();
  batches[batchId] = { kind, source, params, runIds, queued_at: new Date().toISOString() };
  writeMockReportBatches(batches);
  return {
    batch_id: batchId,
    status_url: `/api/v1/reports/batches/${batchId}`,
    events_url: `/api/v1/reports/batches/${batchId}/events`,
  };
}

export async function listReportBatches({ clientId, status, limit = 50, offset = 0 } = {}) {
  if (!FIXTURE_MODE) {
    const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
    if (clientId) params.set("client_id", clientId);
    if (status) params.set("status", status);
    return request(`/reports/batches?${params}`, {}, "reports-batches-history");
  }
  await wait(180);
  let rows = Object.keys(readMockReportBatches()).map((id) => computeMockReportBatch(id));
  if (clientId) rows = rows.filter((batch) => batch.clients.some((c) => c.client_id === clientId));
  if (status) rows = rows.filter((batch) => batch.status === status);
  rows = [...rows].sort((a, b) => Date.parse(b.queued_at) - Date.parse(a.queued_at));
  return { rows: rows.slice(offset, offset + limit), total: rows.length, limit, offset };
}

export async function getReportBatch(batchId) {
  if (!FIXTURE_MODE) return request(`/reports/batches/${batchId}`, {}, `reports-batch-poll-${batchId}`);
  await wait(120);
  return computeMockReportBatch(batchId);
}

export async function cancelReportBatch(batchId) {
  if (!FIXTURE_MODE) return request(`/reports/batches/${batchId}/cancel`, { method: "POST", body: "{}" });
  await wait(150);
  if (!isMockReportBatchId(batchId)) throw new Error("This batch has already finished");
  const batches = readMockReportBatches();
  const stored = batches[batchId];
  if (stored && !stored.cancelledAtMs) {
    stored.cancelledAtMs = Date.now();
    writeMockReportBatches(batches);
    const runs = readMockReportRuns();
    stored.runIds.forEach((id) => {
      if (runs[id] && !runs[id].cancelledAtMs) runs[id].cancelledAtMs = Date.now();
    });
    writeMockReportRuns(runs);
  }
  return computeMockReportBatch(batchId);
}

// task-49 §2.4: retries/rebuilds a batch client WITHOUT leaving the batch —
// the old `Retry this client` button called `createReportRun` directly,
// creating a standalone run outside the batch that the matrix never saw
// again (task-49 §1.4). This stays inside the batch (`rebuildMockRun`
// replaces the child's slot in `runIds`) and the caller states
// `force_refresh` explicitly every time, never inheriting it silently.
export async function retryReportBatchClient(batchId, clientId, { forceRefresh = false } = {}) {
  if (!FIXTURE_MODE) {
    return request(`/reports/batches/${batchId}/clients/${clientId}/retry`, {
      method: "POST", body: JSON.stringify({ force_refresh: forceRefresh }),
    }, `reports-batch-retry-${batchId}-${clientId}`);
  }
  await wait(280);
  const batches = readMockReportBatches();
  const stored = batches[batchId];
  if (!stored) throw new Error("Report batch not found");
  const runs = readMockReportRuns();
  const currentRunId = stored.runIds.find((id) => runs[id]?.client_id === clientId);
  if (!currentRunId) throw new Error("This client has no run in this batch");
  rebuildMockRun(currentRunId, forceRefresh);
  return computeMockReportBatch(batchId);
}

export function reportBatchEventsUrl(batchId) {
  return `${API_ROOT}/reports/batches/${batchId}/events`;
}

export function reportBatchPdfArchiveUrl(batchId) {
  return `${API_ROOT}/reports/batches/${batchId}/pdf-archive`;
}

// One combined PDF (all clients, one file) / one XLSX (one sheet per
// client) across the whole batch — task-50, siblings of the ZIP above.
export function reportBatchPortfolioPdfUrl(batchId, { inline = false } = {}) {
  return `${API_ROOT}/reports/batches/${batchId}/portfolio.pdf?disposition=${inline ? "inline" : "attachment"}`;
}

export function reportBatchPortfolioXlsxUrl(batchId) {
  return `${API_ROOT}/reports/batches/${batchId}/portfolio.xlsx`;
}

// On-page table preview (task-50): the XLSX itself has no browser-native
// inline viewer the way the PDF above does, so this is the renderable
// substitute — one `ReportTableDTO` per client, same shape `getReportTable`
// already returns for a single run.
export async function getBatchPortfolioTable(batchId) {
  if (!FIXTURE_MODE) {
    return request(
      `/reports/batches/${batchId}/portfolio-table`, {}, `reports-batch-portfolio-table-${batchId}`
    );
  }
  await wait(150);
  return { clients: [] };
}

// --- Content QA: Strategy Check + Framework Check (task-53) ------------------
//
// The upload mechanics mirror `uploadAltTextSource`: raw bytes, `X-Filename`
// header, no multipart. Profile fields travel as query params since the body
// is the file itself, not JSON. The report is persisted server-side
// (`content_qa_reports`), so it gets a real `id` and a shareable
// `#/tools/<key>/<id>` link — list/get below read that same table.
let contentQaFixtureCounter = 0;

// Raw-byte POST shared by /inspect and /check (task-87): both take the file
// body + `X-Filename`, both return JSON, both throw the same normalized
// error shape. `params` builds the query string — a plain object of
// string|string[]|boolean, arrays repeat the key (`sheet=A&sheet=B`).
async function postContentQaBytes(path, file, params) {
  const qs = new URLSearchParams();
  for (const [key, value] of Object.entries(params || {})) {
    if (value === undefined || value === null || value === "") continue;
    if (Array.isArray(value)) {
      for (const item of value) if (String(item).trim()) qs.append(key, item);
    } else {
      qs.set(key, value);
    }
  }
  const search = qs.toString();
  const response = await fetch(`${API_ROOT}${path}${search ? `?${search}` : ""}`, {
    method: "POST",
    headers: { "Content-Type": "application/octet-stream", "X-Filename": file.name, Accept: "application/json" },
    body: file,
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    const error = new Error(normalizeErrorDetail(payload.detail, response.status));
    error.status = response.status;
    error.code = (payload.detail && typeof payload.detail === "object" && !Array.isArray(payload.detail))
      ? payload.detail.error_code
      : undefined;
    throw error;
  }
  return response.json();
}

async function checkContentQa(route, file, profile, fixtureName, extraParams) {
  if (FIXTURE_MODE) {
    await wait(420);
    const data = await fixture(fixtureName);
    contentQaFixtureCounter += 1;
    return {
      ...data,
      id: `${data.id}-${contentQaFixtureCounter}`,
      source_filename: file.name,
      created_at: new Date().toISOString(),
    };
  }
  return postContentQaBytes(`/tools/${route}`, file, {
    kind: profile?.kind,
    locale: profile?.locale,
    brand: (profile?.brandNames || []).map((name) => name.trim()).filter(Boolean),
    client_id: profile?.clientId || undefined,
    ...extraParams,
  });
}

export async function checkStrategy(file, profile) {
  return checkContentQa("strategy-check/check", file, profile, "strategy-check-report", {
    sheet: profile?.sheets || undefined,
    cross_sheet: profile?.crossSheet === false ? "false" : undefined,
  });
}

export async function checkFramework(file, profile) {
  return checkContentQa("framework-check/check", file, profile, "framework-check-report");
}

// Крок 1 вибору аркуша (task-87 §1.3): сирі байти -> перелік аркушів з
// прев'ю, нічого не персистується. Фронт тримає `file` в пам'яті й
// повторно шле ці ж байти в `checkStrategy` разом з обраним `sheets`.
export async function inspectStrategyWorkbook(file) {
  if (FIXTURE_MODE) {
    await wait(280);
    return fixture("strategy-check-inspect");
  }
  return postContentQaBytes("/tools/strategy-check/inspect", file, {});
}

async function listContentQaReports(route, fixtureName, requestKey) {
  if (FIXTURE_MODE) {
    await wait(140);
    if (FIXTURE_MODE === "tools-empty") return { total: 0, limit: 100, offset: 0, reports: [] };
    const data = await fixture(fixtureName);
    const { findings: _findings, skipped_sheets: _skipped, profile: _profile, ...summary } = data;
    return { total: 1, limit: 100, offset: 0, reports: [summary] };
  }
  return request(`/tools/${route}/reports`, {}, requestKey);
}

async function getContentQaReport(route, reportId, fixtureName, requestKey) {
  if (FIXTURE_MODE) {
    await wait(180);
    const data = await fixture(fixtureName);
    return { ...data, id: reportId };
  }
  return request(`/tools/${route}/reports/${encodeURIComponent(reportId)}`, {}, requestKey);
}

export async function getStrategyReports() {
  return listContentQaReports("strategy-check", "strategy-check-report", "strategy-check-reports");
}

export async function getStrategyReport(reportId) {
  return getContentQaReport(
    "strategy-check", reportId, "strategy-check-report", `strategy-check-report-${reportId}`
  );
}

export async function getFrameworkReports() {
  return listContentQaReports("framework-check", "framework-check-report", "framework-check-reports");
}

export async function getFrameworkReport(reportId) {
  return getContentQaReport(
    "framework-check", reportId, "framework-check-report", `framework-check-report-${reportId}`
  );
}

// --- Strategy Package Check (task-90) ----------------------------------------
//
// One Drive link in, five-part QA out: strategy + every linked Framework's
// Frame/Copy tabs + three cross-checks, one persisted `content_qa_packages`
// row. JSON in/out throughout — unlike Strategy/Framework Check above, this
// tool never receives file bytes from the operator; the source always lives
// on Drive, and `/resolve`/`/inspect`/`/check` read it server-side via the
// Google API. Plain `request()`, not `postContentQaBytes`.

export async function resolveStrategyPackageSource(urlOrId) {
  return request("/tools/strategy-package/resolve", {
    method: "POST", body: JSON.stringify({ url_or_id: urlOrId }),
  });
}

export async function browseStrategyPackageDrive({ q = "", pageToken, pageSize = 25 } = {}) {
  const qs = new URLSearchParams();
  if (q.trim()) qs.set("q", q.trim());
  if (pageToken) qs.set("page_token", pageToken);
  qs.set("page_size", String(pageSize));
  return request(`/tools/strategy-package/drive/files?${qs.toString()}`, {}, "strategy-package-drive-files");
}

export async function inspectStrategyPackage(fileId) {
  return request("/tools/strategy-package/inspect", {
    method: "POST", body: JSON.stringify({ file_id: fileId }),
  });
}

export async function checkStrategyPackage(payload) {
  return request("/tools/strategy-package/check", { method: "POST", body: JSON.stringify(payload) });
}

export async function getStrategyPackageReports() {
  return request("/tools/strategy-package/reports", {}, "strategy-package-reports");
}

export async function getStrategyPackageReport(id) {
  return request(`/tools/strategy-package/reports/${encodeURIComponent(id)}`, {}, `strategy-package-report-${id}`);
}

// task-91 §3.5/§3.6 — plain URL, consumed via `<a href>`; the server sends
// `Content-Disposition: attachment`, the browser handles the download.
export function strategyPackageMatrixXlsxUrl(id) {
  return `${API_ROOT}/tools/strategy-package/reports/${encodeURIComponent(id)}/matrix.xlsx`;
}

// --- Monthly Performance table (task-54) --------------------------------------
//
// A read-mostly register, not a job: `GET` reads whatever is already stored
// (instant, never touches Reporting Ninja) and `POST /refresh` is the only
// call that does — synchronous, 2-4s measured live. `requestKey` is always an
// explicit purpose string, not derived from `clientId` alone, so a settings
// save and a table reload issued back-to-back never abort each other
// (task-36's own request-key lesson, repeated here on purpose).

export async function getClientMonthlyTable(clientId) {
  if (FIXTURE_MODE) {
    await wait(160);
    const data = await fixture("client-monthly-table");
    return data.table;
  }
  return request(`/clients/${clientId}/monthly-table`, {}, `monthly-table:${clientId}`);
}

export async function refreshClientMonthlyTable(clientId, { force } = {}) {
  if (FIXTURE_MODE) {
    await wait(600);
    const data = await fixture("client-monthly-table");
    return { table: data.table, requests_used: 4, months_collected: [] };
  }
  const qs = force ? `?force=${encodeURIComponent(force)}` : "";
  return request(
    `/clients/${clientId}/monthly-table/refresh${qs}`,
    { method: "POST", body: "{}" },
    `monthly-table-refresh:${clientId}`,
  );
}

export async function getClientMonthlyKeyEvents(clientId) {
  if (FIXTURE_MODE) {
    await wait(300);
    const data = await fixture("client-monthly-table");
    return { candidates: data.keyEventCandidates || [] };
  }
  return request(
    `/clients/${clientId}/monthly-table/key-events`, {}, `monthly-table-key-events:${clientId}`
  );
}

export async function putClientMonthlyTableSettings(clientId, payload) {
  if (FIXTURE_MODE) {
    await wait(200);
    const data = await fixture("client-monthly-table");
    return { ...data.settings, ...payload };
  }
  return request(
    `/clients/${clientId}/monthly-table/settings`,
    { method: "PUT", body: JSON.stringify(payload) },
    `monthly-table-settings:${clientId}`,
  );
}

// A binary download: deliberately NOT routed through `request()`, same
// convention as `techAuditExportUrl` above. The UI links straight to it.
export function monthlyTableXlsxUrl(clientId) {
  return `${API_ROOT}/clients/${clientId}/monthly-table/xlsx`;
}

// Schema Markup Checker (task-56): plain JSON POST/GET, same shape as
// `createRedirectCheck`/`getRedirectChecks` — persisted, list+detail with a
// shareable `#/tools/schema-markup/<id>` link.
export async function checkSchemaMarkup(url, clientId) {
  return request("/tools/schema-markup/checks", {
    method: "POST",
    body: JSON.stringify({ url, client_id: clientId || null }),
  }, "schema-markup-create");
}

export async function getSchemaMarkupChecks(clientId) {
  const params = new URLSearchParams({ limit: "50" });
  if (clientId) params.set("client_id", clientId);
  return request(`/tools/schema-markup/checks?${params}`, {}, "schema-markup-checks");
}

export async function getSchemaMarkupCheck(checkId) {
  return request(
    `/tools/schema-markup/checks/${encodeURIComponent(checkId)}`,
    {},
    `schema-markup-check-${checkId}`,
  );
}

// GA4 Events by Month (task-98): every GA4 event for the months the operator
// picks, one tab per month. Request keys are explicit purposes, never derived
// from the params — a derived key silently shares an in-flight request between
// two different questions (hit twice on this project already).
export async function getGa4EventsClients() {
  return request("/tools/ga4-events/clients", {}, "ga4-events-clients");
}

export async function getGa4EventsMonths(clientId) {
  return request(
    `/tools/ga4-events/clients/${encodeURIComponent(clientId)}/months`,
    {},
    "ga4-events-months",
  );
}

export async function runGa4EventsReport(clientId, months) {
  return request(
    "/tools/ga4-events/reports",
    { method: "POST", body: JSON.stringify({ client_id: clientId, months }) },
    "ga4-events-run",
  );
}

export async function getGa4EventsReports(clientId) {
  const params = new URLSearchParams({ limit: "25" });
  if (clientId) params.set("client_id", clientId);
  return request(`/tools/ga4-events/reports?${params}`, {}, "ga4-events-reports");
}

export async function getGa4EventsReport(reportId) {
  return request(
    `/tools/ga4-events/reports/${encodeURIComponent(reportId)}`,
    {},
    `ga4-events-report-${reportId}`,
  );
}

export function ga4EventsExportUrl(reportId) {
  return `/api/v1/tools/ga4-events/reports/${encodeURIComponent(reportId)}/export.xlsx`;
}

// Indexing Planner (task-100): which pages to carry to Search Console by
// hand today, why, and whether it helps. Nothing here submits anything to
// Google — no such API exists; these endpoints plan and record.
// Request keys are explicit purposes, never derived from params.
export async function getIndexingOverview() {
  return request("/tools/indexing/overview", {}, "indexing-overview");
}

export async function getIndexingClients() {
  return request("/tools/indexing/clients", {}, "indexing-clients");
}

export async function getIndexingToday() {
  return request("/tools/indexing/today", {}, "indexing-today");
}

export async function getIndexingCalendar(days = 8) {
  return request(`/tools/indexing/calendar?days=${days}`, {}, "indexing-calendar");
}

export async function getIndexingQueue({ clientId, reasonCode, excluded, offset = 0, limit = 100 } = {}) {
  const params = new URLSearchParams({ offset: String(offset), limit: String(limit) });
  if (clientId) params.set("client_id", clientId);
  if (reasonCode) params.set("reason_code", reasonCode);
  if (excluded) params.set("excluded", "true");
  return request(`/tools/indexing/queue?${params}`, {}, "indexing-queue");
}

export async function getIndexingUrl(urlId) {
  return request(
    `/tools/indexing/urls/${encodeURIComponent(urlId)}`,
    {},
    `indexing-url-${urlId}`,
  );
}

export async function getIndexingAnalytics(windowDays = 180) {
  return request(
    `/tools/indexing/analytics?window_days=${windowDays}`,
    {},
    "indexing-analytics",
  );
}

export async function markIndexingSent(submissionId) {
  return request(
    `/tools/indexing/submissions/${encodeURIComponent(submissionId)}/sent`,
    { method: "POST" },
    `indexing-sent-${submissionId}`,
  );
}

export async function markIndexingQuotaBlocked(submissionId) {
  return request(
    `/tools/indexing/submissions/${encodeURIComponent(submissionId)}/quota-blocked`,
    { method: "POST" },
    `indexing-quota-${submissionId}`,
  );
}

export async function undoIndexingSubmission(submissionId) {
  return request(
    `/tools/indexing/submissions/${encodeURIComponent(submissionId)}/undo`,
    { method: "POST" },
    `indexing-undo-${submissionId}`,
  );
}

export async function setIndexingUrlExcluded(urlId, excluded) {
  const action = excluded ? "exclude" : "include";
  return request(
    `/tools/indexing/urls/${encodeURIComponent(urlId)}/${action}`,
    { method: "POST" },
    `indexing-exclude-${urlId}`,
  );
}

export async function updateIndexingSettings(clientId, payload) {
  return request(
    `/tools/indexing/clients/${encodeURIComponent(clientId)}/settings`,
    { method: "PUT", body: JSON.stringify(payload) },
    `indexing-settings-${clientId}`,
  );
}

export async function startIndexingScan({ clientId = null, full = false } = {}) {
  return request(
    "/tools/indexing/scans",
    { method: "POST", body: JSON.stringify({ client_id: clientId, full }) },
    "indexing-scan-start",
  );
}

export function indexingExportUrl(clientId) {
  const params = new URLSearchParams();
  if (clientId) params.set("client_id", clientId);
  const query = params.toString();
  return `/api/v1/tools/indexing/export.xlsx${query ? `?${query}` : ""}`;
}

// Page Inventory (task-57): architecture table + per-field SEO/slop QA,
// built from monitor data already collected. `Re-crawl site` in the UI
// does NOT call anything here — it drives the existing
// `startMonitorRun`/`listMonitorRuns` above (task-36) and only calls
// `rebuildPageInventory` once that full crawl finishes (task-57 §2.2a).
export async function getPageInventory(clientId, { profile = "calibrated", q, status, field, severity, page, pageSize, requestKey } = {}) {
  const params = new URLSearchParams({ profile });
  if (q) params.set("q", q);
  if (status) params.set("status", status);
  if (field) params.set("field", field);
  if (severity) params.set("severity", severity);
  if (page) params.set("page", String(page));
  if (pageSize) params.set("page_size", String(pageSize));
  return request(
    `/clients/${clientId}/pages?${params}`, {}, requestKey || `page-inventory:${clientId}`,
  );
}

// Compact counts for the client page's summary card — never fetches rows,
// only `PageInventorySummaryDTO` (task-57 §5).
export async function getPageInventorySummary(clientId) {
  return request(
    `/clients/${clientId}/pages/summary`, {}, `page-inventory-summary:${clientId}`,
  );
}

export async function rebuildPageInventory(clientId) {
  const result = await request(
    `/clients/${clientId}/pages/rebuild`,
    { method: "POST", body: "{}" },
    `page-inventory-rebuild:${clientId}`,
  );
  return result.table;
}

export function pageInventoryXlsxUrl(clientId) {
  return `${API_ROOT}/clients/${clientId}/pages/xlsx`;
}

// --- Web Archive (task-59) --------------------------------------------------

export async function getArchiveOverview() {
  return request("/archive/overview", {}, "archive-overview");
}

export async function lookupArchiveUrl(url) {
  return request(`/archive/lookup?url=${encodeURIComponent(url)}`, {}, "archive-lookup");
}

export async function searchArchiveUrls({ clientId, q, page = 1, pageSize = 20, requestKey } = {}) {
  const params = new URLSearchParams({ page: String(page), page_size: String(pageSize) });
  if (clientId) params.set("client_id", clientId);
  if (q) params.set("q", q);
  return request(`/archive/urls?${params}`, {}, requestKey || "archive-urls-search");
}

export async function getArchiveUrlTimeline(urlId) {
  return request(`/archive/urls/${encodeURIComponent(urlId)}/snapshots`, {}, `archive-timeline:${urlId}`);
}

export async function getArchiveSnapshot(snapshotId) {
  return request(`/archive/snapshots/${encodeURIComponent(snapshotId)}`, {}, `archive-snapshot:${snapshotId}`);
}

export async function getArchiveSnapshotSource(snapshotId) {
  const response = await fetch(`${API_ROOT}/archive/snapshots/${encodeURIComponent(snapshotId)}/source`);
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.text();
}

export async function getArchiveSnapshotDiff(snapshotId) {
  return request(
    `/archive/snapshots/${encodeURIComponent(snapshotId)}/diff`, {}, `archive-diff:${snapshotId}`,
  );
}

export async function listArchiveChanges({ clientId, kind, days = 30, page = 1, pageSize = 50, requestKey } = {}) {
  const params = new URLSearchParams({ days: String(days), page: String(page), page_size: String(pageSize) });
  if (clientId) params.set("client_id", clientId);
  if (kind) params.set("kind", kind);
  return request(`/archive/changes?${params}`, {}, requestKey || "archive-changes");
}

export async function listArchiveCaptures({ clientId, limit = 50, requestKey } = {}) {
  const params = new URLSearchParams({ limit: String(limit) });
  if (clientId) params.set("client_id", clientId);
  return request(`/archive/captures?${params}`, {}, requestKey || "archive-captures");
}

export async function startArchiveCapture(clientId) {
  return request(
    `/archive/captures?client_id=${encodeURIComponent(clientId)}`,
    { method: "POST", body: "{}" },
    `archive-capture-start:${clientId}`,
  );
}

// task-80 — "Recapture": forces a new snapshot for every URL, even
// byte-for-byte unchanged ones, instead of the ordinary "confirm and skip".
export async function startForcedArchiveCapture(clientId) {
  return request(
    `/archive/captures/force?client_id=${encodeURIComponent(clientId)}`,
    { method: "POST", body: "{}" },
    `archive-recapture-start:${clientId}`,
  );
}

// task-62 §1.1 — "Refresh now" on one URL, not the whole client.
export async function startArchiveUrlCapture(urlId) {
  return request(
    `/archive/captures/url?url_id=${encodeURIComponent(urlId)}`,
    { method: "POST", body: "{}" },
    `archive-capture-start-url:${urlId}`,
  );
}

// task-62 §1.2 — "Capture all": one queued row per collectable client.
export async function startAllArchiveCaptures() {
  return request("/archive/captures/all", { method: "POST", body: "{}" }, "archive-capture-start-all");
}

// task-63 follow-up — "Archive this page now" on a URL the archive has
// NEVER seen (the hub's lookup only ever finds an already-tracked one).
export async function startArchiveAdhocUrlCapture(url) {
  return request(
    `/archive/captures/url-adhoc?url=${encodeURIComponent(url)}`,
    { method: "POST", body: "{}" },
    `archive-capture-start-adhoc:${url}`,
  );
}

export async function getArchiveCapture(captureId, { requestKey } = {}) {
  if (!requestKey) throw new Error("getArchiveCapture requires an explicit requestKey");
  return request(`/archive/captures/${encodeURIComponent(captureId)}`, {}, requestKey);
}

export async function getArchiveCaptureEvents(captureId, { since = 0, requestKey } = {}) {
  if (!requestKey) throw new Error("getArchiveCaptureEvents requires an explicit requestKey");
  return request(
    `/archive/captures/${encodeURIComponent(captureId)}/events?since=${since}`, {}, requestKey,
  );
}

export async function cancelArchiveCapture(captureId) {
  return request(
    `/archive/captures/${encodeURIComponent(captureId)}/cancel`,
    { method: "POST", body: "{}" },
    `archive-capture-cancel:${captureId}`,
  );
}

export async function getArchiveQueue({ requestKey } = {}) {
  if (!requestKey) throw new Error("getArchiveQueue requires an explicit requestKey");
  return request("/archive/queue", {}, requestKey);
}

// --- URL Inspector (task-61) -------------------------------------------------
//
// Two calls, deliberately not one: stored data (this one) must render
// instantly; `runUrlInspectorLive` is a separate, slow, opt-in fetch so it
// never blocks the five panels that don't need it. Request keys are
// explicit, named purposes — not derived from the URL/granularity params —
// so switching granularity correctly cancels the previous in-flight fetch
// instead of racing it (task-36 UI, hit twice before: derived keys don't
// collide when they should).

export async function getUrlInspector(url, { granularity = "week" } = {}) {
  const params = new URLSearchParams({ url, granularity });
  return request(`/url-inspector?${params}`, {}, "url-inspector");
}

export async function runUrlInspectorLive(url, { withSchema = false } = {}) {
  return request(
    "/url-inspector/live",
    { method: "POST", body: JSON.stringify({ url, with_schema: withSchema }) },
    "url-inspector-live",
  );
}

// Direct Google API panels (task-88) — three more calls, split by MEASURED
// latency rather than by topic, so each group paints as soon as its own
// budget allows instead of every panel waiting for the slowest:
//
//   /url-inspector/google             0.9-1.1s  series, page queries, conversions
//   /url-inspector/google/breakdowns  0.8s      every dimension slice
//   /url-inspector/google/realtime    0.5s      active users right now
//   /url-inspector/index-status       6.8s      URL Inspection API — opt-in only
//
// A 409 here means "this client has no Google integration", not a failure:
// the caller turns it into that panel's own `not_configured` state and keeps
// the rest of the page. Request keys stay explicit named purposes so
// switching the window cancels the previous fetch instead of racing it.

export async function getUrlInspectorGoogle(url, { window: win } = {}) {
  const params = new URLSearchParams({ url });
  if (win) params.set("window", win);
  return request(`/url-inspector/google?${params}`, {}, "url-inspector-google");
}

export async function getUrlInspectorGoogleBreakdowns(url, { window: win } = {}) {
  const params = new URLSearchParams({ url });
  if (win) params.set("window", win);
  return request(
    `/url-inspector/google/breakdowns?${params}`, {}, "url-inspector-google-breakdowns",
  );
}

export async function getUrlInspectorRealtime(url) {
  const params = new URLSearchParams({ url });
  return request(`/url-inspector/google/realtime?${params}`, {}, "url-inspector-realtime");
}

export async function runUrlIndexStatus(url) {
  return request(
    "/url-inspector/index-status",
    { method: "POST", body: JSON.stringify({ url }) },
    "url-inspector-index-status",
  );
}

// Type-ahead while the operator types in the header search field — client
// homepage suggestions before a site resolves, that site's own sitemap
// pages once it does. `requestKey` is per-call (not a fixed constant) so a
// fast typist's stale response can't ever overwrite a newer one out of order.
export async function getUrlInspectorSuggestions(q) {
  return request(
    `/url-inspector/suggest?q=${encodeURIComponent(q)}`, {}, "url-inspector-suggest",
  );
}

// Screenshots (task-64) — same instant-vs-slow split as above:
// `getUrlScreenshots` reads whatever is cached (fast); `captureUrlScreenshots`
// is the opt-in ~20s capture. Separate request keys so a capture in flight
// is never cancelled by an unrelated cached-read poll.
export async function getUrlScreenshots(url) {
  const params = new URLSearchParams({ url });
  return request(`/url-screenshots?${params}`, {}, "url-screenshots");
}

export async function captureUrlScreenshots(url, { presets, force = false } = {}) {
  const body = { url, force };
  if (presets) body.presets = presets;
  return request(
    "/url-screenshots",
    { method: "POST", body: JSON.stringify(body) },
    "url-screenshots-capture",
  );
}


// ── Авторизація та адмінка користувачів (task-71) ─────────────────────────
// `requestKey` у цих викликах — явні іменовані призначення, а не похідні від
// параметрів: спільний ключ скасовував би паралельні виклики один одному.

export async function getAuthSession() {
  // Через `fetch` напряму, повз `request()`: цей ендпоінт публічний і його
  // 401-обробка (редірект на логін) тут була б рекурсією.
  const response = await fetch(`${API_ROOT}/auth/session`, {
    credentials: "same-origin",
    headers: { Accept: "application/json" },
  });
  if (!response.ok) return { authenticated: false, user: null };
  return response.json();
}

export async function logout() {
  return request("/auth/logout", { method: "POST" }, "auth-logout");
}

export async function logoutEverywhere() {
  return request("/auth/logout-all", { method: "POST" }, "auth-logout-all");
}

// Особисті токени (task-79) — «кожен видає собі сам» у `#/account`.
// task-78: екран згоди OAuth. `no-store` тут не потрібен окремо —
// сервер сам віддає його для всього `/api/v1/oauth`.
export async function getPendingAuthorization(requestId) {
  return request(`/oauth/pending/${encodeURIComponent(requestId)}`, {}, "oauth-pending");
}

export async function approveAuthorization(requestId) {
  return request(
    "/oauth/consent",
    { method: "POST", body: JSON.stringify({ request_id: requestId }) },
    "oauth-consent",
  );
}

export async function listMyTokens() {
  return request("/me/tokens", {}, "me-tokens-list");
}

export async function createMyToken({ label, role, expires_in_days: expiresInDays }) {
  return request(
    "/me/tokens",
    { method: "POST", body: JSON.stringify({ label, role, expires_in_days: expiresInDays }) },
    "me-tokens-create"
  );
}

export async function revokeMyToken(tokenId) {
  return request(`/me/tokens/${encodeURIComponent(tokenId)}`, { method: "DELETE" }, "me-tokens-revoke");
}

export async function listUsers() {
  return request("/admin/users", {}, "admin-users-list");
}

export async function createUser({ email, name, role }) {
  return request(
    "/admin/users",
    { method: "POST", body: JSON.stringify({ email, name, role }) },
    "admin-user-create",
  );
}

export async function updateUser(userId, patch) {
  return request(
    `/admin/users/${encodeURIComponent(userId)}`,
    { method: "PATCH", body: JSON.stringify(patch) },
    `admin-user-update-${userId}`,
  );
}

export async function resetUserPassword(userId) {
  return request(
    `/admin/users/${encodeURIComponent(userId)}/reset-password`,
    { method: "POST" },
    `admin-user-reset-${userId}`,
  );
}

export async function revokeUserSessions(userId) {
  return request(
    `/admin/users/${encodeURIComponent(userId)}/revoke-sessions`,
    { method: "POST" },
    `admin-user-revoke-${userId}`,
  );
}


// ── Журнал дій (task-72) ──────────────────────────────────────────────────

export async function listAuditEvents(filters = {}) {
  const params = new URLSearchParams();
  Object.entries(filters).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== "") params.set(key, value);
  });
  return request(`/admin/audit?${params.toString()}`, {}, "admin-audit-list");
}

export async function getAuditFacets() {
  return request("/admin/audit/facets", {}, "admin-audit-facets");
}

// Не через `request()`: це CSV-потік, а не JSON, і його треба віддати
// браузеру як файл, а не розпарсити.
export function auditExportUrl(filters = {}) {
  const params = new URLSearchParams();
  Object.entries(filters).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== "") params.set(key, value);
  });
  return `${API_ROOT}/admin/audit/export.csv?${params.toString()}`;
}

// ── Авторство (task-93) ────────────────────────────────────────────────────
// Admin-only — non-admin отримує 403; кожен викликач (`authorship.js`) сам
// не робить запиту, якщо `!isAdmin()`, тож ці функції не приховують помилку,
// а просто не мають бути дзвінком для не-адміна. Без `requestKey`-скасування:
// сторінка може паралельно питати авторів для кількох ресурсних типів.

export async function getAuthorship(resourceType, ids) {
  const params = new URLSearchParams({ resource_type: resourceType, ids: ids.join(",") });
  return request(`/authorship?${params}`, {}, `authorship-${resourceType}-${ids.join(",")}`);
}

export async function getAuthorshipFacets(resourceTypes) {
  const params = new URLSearchParams({ resource_type: resourceTypes.join(",") });
  return request(`/authorship/facets?${params}`, {}, `authorship-facets-${resourceTypes.join(",")}`);
}

// ── Prompt Launcher (task-94) ────────────────────────────────────────────
// Read-only тул: бек уже рендерить готовий рядок для буфера, фронт нічого
// не склеює сам. `request-key` — явна назва призначення, не похідна від
// параметрів (task-36 gotcha: інакше другий шаблон/клієнт скасовував би
// в польоті запит першого).

export async function getPromptTemplates() {
  return request("/tools/prompts", {}, "prompt-templates");
}

export async function getPromptClients(templateKey) {
  return request(
    `/tools/prompts/${encodeURIComponent(templateKey)}/clients`,
    {},
    "prompt-clients",
  );
}

export async function getPromptSource(templateKey) {
  return request(
    `/tools/prompts/${encodeURIComponent(templateKey)}/source`,
    {},
    "prompt-source",
  );
}
