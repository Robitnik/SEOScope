const AHREFS_ROUTE = /^#\/client\/([^/?]+)\/ahrefs/;
const BLOGS_ROUTE = /^#\/client\/([^/?]+)\/blogs/;
// Site Monitor client section (task-36) — matched before the generic
// `CLIENT_ROUTE` below, same as `AHREFS_ROUTE`/`BLOGS_ROUTE`.
const MONITOR_CLIENT_ROUTE = /^#\/client\/([^/?]+)\/monitor/;
const CLIENT_ROUTE = /^#\/client\/([^/?]+)/;
// Tools hub and one tool, optionally deep-linked to a single report
// (`#/tools/alt-text/<reportId>`). Added by task-27.
const TOOLS_ROUTE = /^#\/tools(?:\/([^/?]+))?(?:\/([^/?]+))?/;
// Portfolio Trends (task-28): one top-level page, no sub-resource segment.
const TRENDS_ROUTE = /^#\/trends/;
// Site Monitor portfolio page (task-36): same shape as Trends — top-level, no client id.
const MONITOR_ROUTE = /^#\/monitor/;
// Reports (task-43): portfolio-level history (`#/reports`) and one run's
// detail (`#/reports/<runId>`) — same "top-level, optional sub-resource"
// shape TOOLS_ROUTE uses, not a client-scoped section.
const REPORTS_ROUTE = /^#\/reports(?:\/([^/?]+))?/;
// One batch's detail (task-47 §2.4), matched BEFORE the generic
// `REPORTS_ROUTE` above — otherwise `#/reports/batches/<id>` would parse as
// a single run whose id is literally `batches`.
const REPORTS_BATCH_ROUTE = /^#\/reports\/batches\/([^/?]+)/;

export function parseRoute() {
  const hash = location.hash || "";
  const query = new URLSearchParams(hash.split("?")[1] || "");
  const toolsMatch = hash.match(TOOLS_ROUTE);
  if (toolsMatch) {
    return {
      name: "tools",
      toolKey: toolsMatch[1] ? decodeURIComponent(toolsMatch[1]) : null,
      reportId: toolsMatch[2] ? decodeURIComponent(toolsMatch[2]) : null,
      query,
    };
  }
  if (TRENDS_ROUTE.test(hash)) return { name: "trends", query };
  if (MONITOR_ROUTE.test(hash)) return { name: "monitor", query };
  const batchMatch = hash.match(REPORTS_BATCH_ROUTE);
  if (batchMatch) return { name: "reports-batch", batchId: decodeURIComponent(batchMatch[1]), query };
  const reportsMatch = hash.match(REPORTS_ROUTE);
  if (reportsMatch) {
    const runId = reportsMatch[1] ? decodeURIComponent(reportsMatch[1]) : null;
    return { name: runId ? "reports-run" : "reports", runId, query };
  }
  const ahrefsMatch = hash.match(AHREFS_ROUTE);
  if (ahrefsMatch) return { name: "ahrefs", clientId: decodeURIComponent(ahrefsMatch[1]), query };
  const blogsMatch = hash.match(BLOGS_ROUTE);
  if (blogsMatch) return { name: "blogs", clientId: decodeURIComponent(blogsMatch[1]), query };
  const monitorClientMatch = hash.match(MONITOR_CLIENT_ROUTE);
  if (monitorClientMatch) return { name: "monitor-client", clientId: decodeURIComponent(monitorClientMatch[1]), query };
  const match = hash.match(CLIENT_ROUTE);
  if (!match) return { name: "dashboard" };
  return { name: "client", clientId: decodeURIComponent(match[1]), query };
}

function buildHash(path, params = {}) {
  const query = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== "") query.set(key, String(value));
  });
  const qs = query.toString();
  return `${path}${qs ? `?${qs}` : ""}`;
}

export function buildClientHash(clientId, params = {}) {
  return buildHash(`#/client/${encodeURIComponent(clientId)}`, params);
}

export function buildAhrefsHash(clientId, params = {}) {
  return buildHash(`#/client/${encodeURIComponent(clientId)}/ahrefs`, params);
}

export function buildBlogsHash(clientId, params = {}) {
  return buildHash(`#/client/${encodeURIComponent(clientId)}/blogs`, params);
}

export function buildTrendsHash(params = {}) {
  return buildHash("#/trends", params);
}

export function replaceTrendsHash(params) {
  history.replaceState(null, "", `${location.pathname}${location.search}${buildTrendsHash(params)}`);
}

// Site Monitor (task-36) — portfolio page (like Trends) and client section
// (like Blogs/Ahrefs).
export function buildMonitorHash(params = {}) {
  return buildHash("#/monitor", params);
}

export function replaceMonitorHash(params) {
  history.replaceState(null, "", `${location.pathname}${location.search}${buildMonitorHash(params)}`);
}

export function buildMonitorClientHash(clientId, params = {}) {
  return buildHash(`#/client/${encodeURIComponent(clientId)}/monitor`, params);
}

// Reports (task-43) — portfolio history (like Trends/Monitor) and one run's
// detail (`#/reports/<runId>`, like `#/tools/<key>/<reportId>`).
export function buildReportsHash(params = {}) {
  return buildHash("#/reports", params);
}

export function replaceReportsHash(params) {
  history.replaceState(null, "", `${location.pathname}${location.search}${buildReportsHash(params)}`);
}

export function buildReportsRunHash(runId, params = {}) {
  return buildHash(`#/reports/${encodeURIComponent(runId)}`, params);
}

export function replaceReportsRunHash(runId, params) {
  history.replaceState(null, "", `${location.pathname}${location.search}${buildReportsRunHash(runId, params)}`);
}

export function buildReportsBatchHash(batchId, params = {}) {
  return buildHash(`#/reports/batches/${encodeURIComponent(batchId)}`, params);
}

export function replaceMonitorClientHash(clientId, params) {
  history.replaceState(null, "", `${location.pathname}${location.search}${buildMonitorClientHash(clientId, params)}`);
}

export function buildToolsHash(toolKey = null, reportId = null, params = {}) {
  const segments = ["#/tools"];
  if (toolKey) segments.push(encodeURIComponent(toolKey));
  if (toolKey && reportId) segments.push(encodeURIComponent(reportId));
  return buildHash(segments.join("/"), params);
}

export function replaceToolsHash(toolKey, reportId, params) {
  const hash = buildToolsHash(toolKey, reportId, params);
  history.replaceState(null, "", `${location.pathname}${location.search}${hash}`);
}

// Persists page state (period/tab/sort/…) without adding a browser history
// entry per keystroke, mirroring store.js's `persistUrl` for the dashboard.
// `replaceState` never fires `hashchange`, so this cannot loop back into the
// listener below.
export function replaceClientHash(clientId, params) {
  history.replaceState(null, "", `${location.pathname}${location.search}${buildClientHash(clientId, params)}`);
}

export function replaceAhrefsHash(clientId, params) {
  history.replaceState(null, "", `${location.pathname}${location.search}${buildAhrefsHash(clientId, params)}`);
}

export function replaceBlogsHash(clientId, params) {
  history.replaceState(null, "", `${location.pathname}${location.search}${buildBlogsHash(clientId, params)}`);
}

async function activate() {
  const route = parseRoute();
  const dashboardView = document.querySelector("#dashboard-view");
  const clientRoot = document.querySelector("#client-page-root");
  const ahrefsRoot = document.querySelector("#ahrefs-page-root");
  const blogsRoot = document.querySelector("#blogs-page-root");
  const toolsRoot = document.querySelector("#tools-page-root");
  const trendsRoot = document.querySelector("#trends-page-root");
  const monitorRoot = document.querySelector("#monitor-page-root");
  const monitorClientRoot = document.querySelector("#monitor-client-page-root");
  const reportsRoot = document.querySelector("#reports-page-root");
  const reportsRunRoot = document.querySelector("#reports-run-page-root");
  const reportsBatchRoot = document.querySelector("#reports-batch-page-root");
  const dashboardChrome = [
    document.querySelector("#dashboard-header-actions"),
    document.querySelector("#dashboard-command-bar"),
  ];
  const allRoots = [
    clientRoot, ahrefsRoot, blogsRoot, toolsRoot, trendsRoot, monitorRoot, monitorClientRoot,
    reportsRoot, reportsRunRoot, reportsBatchRoot,
  ];
  // Global nav marks the active destination for assistive tech and for the eye
  // (`aria-current`), since a cross-route move is a full reload here.
  const NAV_ROUTES = new Set(["tools", "trends", "monitor", "reports", "reports-run", "reports-batch"]);
  document.querySelectorAll("[data-nav]").forEach((link) => {
    const navName = (route.name === "reports-run" || route.name === "reports-batch") ? "reports" : route.name;
    const isActive = link.dataset.nav === (NAV_ROUTES.has(navName) ? navName : "dashboard");
    if (isActive) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  });
  if (route.name === "tools") {
    dashboardView?.setAttribute("hidden", "");
    allRoots.filter((el) => el !== toolsRoot).forEach((el) => el?.setAttribute("hidden", ""));
    dashboardChrome.forEach((el) => el?.setAttribute("hidden", ""));
    toolsRoot?.removeAttribute("hidden");
    const { initToolsRoute } = await import("./tools-page.js?v=20260819-spin3");
    await initToolsRoute(route);
  } else if (route.name === "trends") {
    dashboardView?.setAttribute("hidden", "");
    allRoots.filter((el) => el !== trendsRoot).forEach((el) => el?.setAttribute("hidden", ""));
    dashboardChrome.forEach((el) => el?.setAttribute("hidden", ""));
    trendsRoot?.removeAttribute("hidden");
    const { initTrendsPage } = await import("./trends-page.js?v=20260819-spin3");
    await initTrendsPage(route.query);
  } else if (route.name === "monitor") {
    dashboardView?.setAttribute("hidden", "");
    allRoots.filter((el) => el !== monitorRoot).forEach((el) => el?.setAttribute("hidden", ""));
    dashboardChrome.forEach((el) => el?.setAttribute("hidden", ""));
    monitorRoot?.removeAttribute("hidden");
    const { initMonitorPage } = await import("./monitor-page.js?v=20260819-spin3");
    await initMonitorPage(route.query);
  } else if (route.name === "reports") {
    dashboardView?.setAttribute("hidden", "");
    allRoots.filter((el) => el !== reportsRoot).forEach((el) => el?.setAttribute("hidden", ""));
    dashboardChrome.forEach((el) => el?.setAttribute("hidden", ""));
    reportsRoot?.removeAttribute("hidden");
    const { initReportsPage } = await import("./reports-page.js?v=20260819-spin3");
    await initReportsPage(route.query);
  } else if (route.name === "reports-run") {
    dashboardView?.setAttribute("hidden", "");
    allRoots.filter((el) => el !== reportsRunRoot).forEach((el) => el?.setAttribute("hidden", ""));
    dashboardChrome.forEach((el) => el?.setAttribute("hidden", ""));
    reportsRunRoot?.removeAttribute("hidden");
    const { initReportsRunPage } = await import("./reports-run.js?v=20260819-spin3");
    await initReportsRunPage(route.runId, route.query);
  } else if (route.name === "reports-batch") {
    dashboardView?.setAttribute("hidden", "");
    allRoots.filter((el) => el !== reportsBatchRoot).forEach((el) => el?.setAttribute("hidden", ""));
    dashboardChrome.forEach((el) => el?.setAttribute("hidden", ""));
    reportsBatchRoot?.removeAttribute("hidden");
    const { initReportsBatchPage } = await import("./reports-batch.js?v=20260819-spin3");
    await initReportsBatchPage(route.batchId);
  } else if (route.name === "ahrefs") {
    dashboardView?.setAttribute("hidden", "");
    allRoots.filter((el) => el !== ahrefsRoot).forEach((el) => el?.setAttribute("hidden", ""));
    dashboardChrome.forEach((el) => el?.setAttribute("hidden", ""));
    ahrefsRoot?.removeAttribute("hidden");
    const { initAhrefsPage } = await import("./ahrefs-page.js?v=20260819-spin3");
    initAhrefsPage(route.clientId, route.query);
  } else if (route.name === "blogs") {
    dashboardView?.setAttribute("hidden", "");
    allRoots.filter((el) => el !== blogsRoot).forEach((el) => el?.setAttribute("hidden", ""));
    dashboardChrome.forEach((el) => el?.setAttribute("hidden", ""));
    blogsRoot?.removeAttribute("hidden");
    const { initBlogsPage } = await import("./blogs-page.js?v=20260819-spin3");
    initBlogsPage(route.clientId, route.query);
  } else if (route.name === "monitor-client") {
    dashboardView?.setAttribute("hidden", "");
    allRoots.filter((el) => el !== monitorClientRoot).forEach((el) => el?.setAttribute("hidden", ""));
    dashboardChrome.forEach((el) => el?.setAttribute("hidden", ""));
    monitorClientRoot?.removeAttribute("hidden");
    const { initMonitorClientPage } = await import("./monitor-client.js?v=20260819-spin3");
    initMonitorClientPage(route.clientId, route.query);
  } else if (route.name === "client") {
    dashboardView?.setAttribute("hidden", "");
    allRoots.filter((el) => el !== clientRoot).forEach((el) => el?.setAttribute("hidden", ""));
    dashboardChrome.forEach((el) => el?.setAttribute("hidden", ""));
    clientRoot?.removeAttribute("hidden");
    const { initClientPage } = await import("./client-page.js?v=20260819-spin3");
    initClientPage(route.clientId, route.query);
  } else {
    allRoots.forEach((el) => el?.setAttribute("hidden", ""));
    dashboardView?.removeAttribute("hidden");
    dashboardChrome.forEach((el) => el?.removeAttribute("hidden"));
    await import("./dashboard.js?v=20260819-spin3");
  }
}

// Cross-route navigation (dashboard <-> client page, or browser back/forward
// landing on a different client) reloads rather than tearing down/reinitializing
// modules in place. `dashboard.js` runs its setup once at import time with no
// exported re-init entry point, so a live SPA swap back to it would show a
// stale, unwired view. A full reload is simple, always correct, and still
// satisfies "state survives back/forward" since it re-derives everything from
// the URL (AGENTS.md §12). In-page changes within the client page itself
// (period/tab/sort) never hit this path — they go through `replaceClientHash`.
window.addEventListener("hashchange", () => location.reload());

activate();
