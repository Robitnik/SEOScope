const AHREFS_ROUTE = /^#\/client\/([^/?]+)\/ahrefs/;
const BLOGS_ROUTE = /^#\/client\/([^/?]+)\/blogs/;
// Monthly Performance sub-page (task-54) — same shape as Ahrefs/Blogs: the
// client page only shows a compact summary card, the full sheet-fidelity
// table lives on its own route.
const MONTHLY_ROUTE = /^#\/client\/([^/?]+)\/monthly/;
// Site Monitor client section (task-36) — matched before the generic
// `CLIENT_ROUTE` below, same as `AHREFS_ROUTE`/`BLOGS_ROUTE`.
const MONITOR_CLIENT_ROUTE = /^#\/client\/([^/?]+)\/monitor/;
// Page Inventory sub-page (task-57) — same shape as Monthly Performance:
// the client page only shows a compact summary card, the full table lives
// on its own route.
const PAGES_ROUTE = /^#\/client\/([^/?]+)\/pages/;
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
// Web Archive (task-59): portfolio hub (`#/archive`), one snapshot's replay
// viewer (`#/archive/view?snapshot=<id>`), and the "what changed when" feed
// (`#/archive/changes`) — three top-level pages, same shape as Trends/
// Monitor/Reports. View/Changes matched BEFORE the generic ARCHIVE_ROUTE,
// same reasoning `REPORTS_BATCH_ROUTE` gives for matching before `REPORTS_ROUTE`.
const ARCHIVE_VIEW_ROUTE = /^#\/archive\/view/;
// The replay viewer's own server-backed address (task-63 §1) — a real
// path, not a hash: `/archive/view/snapshot/<id>/<14-digit ts>/<url>`.
// Matched against `location.pathname`, BEFORE any hash parsing at all,
// since a browser navigating directly to this path carries whatever the
// PREVIOUS page's hash happened to be (or none). `url` is greedy (`.+`) —
// the archived url itself may contain further `/`.
const ARCHIVE_VIEW_PATH_RE = /^\/archive\/view\/snapshot\/([^/]+)\/(\d{14})\/(.+)$/;
const ARCHIVE_CHANGES_ROUTE = /^#\/archive\/changes/;
// One capture's live progress + log (task-62 §1.5) — matched BEFORE the
// generic ARCHIVE_ROUTE, same reasoning as VIEW/CHANGES above.
const ARCHIVE_CAPTURE_ROUTE = /^#\/archive\/capture/;
const ARCHIVE_ROUTE = /^#\/archive/;
// URL Inspector (task-61): reached from the header search field on every
// route, not a nav tab — top-level, no client-id segment, same shape as
// Trends/Monitor/Archive above.
//
// Its real address is a server-backed PATH, not a hash (task-88 follow-up,
// same move `ARCHIVE_VIEW_PATH_RE` made): `/url/<the url being inspected>`,
// raw, not percent-encoded. `.+` is greedy because the inspected url has
// slashes of its own. Matched against `location.pathname` BEFORE any hash
// parsing, since a browser landing on this path carries whatever hash the
// PREVIOUS page happened to leave behind.
//
// The two view options (`g` granularity, `w` window) live in the FRAGMENT,
// not the query: a `?` on this address belongs to the INSPECTED url — some
// clients' real pages are query-string pages (LAXTON's `?taxonomy=…`) — and
// one rule with no exceptions beats two rules that look alike.
//
// The old `#/url?u=…` hash still RESOLVES here, so existing links and
// bookmarks keep working; `url-inspector.js` rewrites the address to the
// path form on load, exactly as `archive-viewer.js` canonicalizes its own.
// Nothing WRITES that shape any more, and there is deliberately no builder
// left for it — an exported `buildUrlInspectorHash` would be a standing
// invitation to reintroduce the address we just moved away from.
const URL_INSPECTOR_ROUTE = /^#\/url(?:$|[/?])/;
const URL_INSPECTOR_PATH_RE = /^\/url\/(.+)$/;
// Адмінка користувачів (task-71) — верхньорівнева сторінка, доступна лише
// адміну. Справжня межа безпеки лишається на сервері (403 на
// `/api/v1/admin/*` для будь-кого, хто не адмін, незалежно від фронта) —
// але `activate()` тепер і сама відсилає non-admin назад на `#/` ДО
// рендеру сторінки (не після невдалого виклику API), щоб пряме введення
// хеша в адресний рядок не показувало чужу адмінку навіть на мить.
const ADMIN_USERS_ROUTE = /^#\/admin\/users/;
// Журнал дій (task-72). Приймає `resource_type`/`resource_id` у query — так
// сюди веде посилання «History» з конкретного клієнта.
const ADMIN_AUDIT_ROUTE = /^#\/admin\/audit/;
// MCP catalog page (task-77) — top-level, no client-id segment, read-only
// for every role, same shape as Trends/Monitor above. "How to connect" is
// its own sub-page, matched BEFORE the generic MCP_ROUTE below, same
// reasoning ARCHIVE_VIEW_ROUTE/ARCHIVE_CHANGES_ROUTE give for matching
// before the generic ARCHIVE_ROUTE.
const MCP_CONNECT_ROUTE = /^#\/mcp\/connect/;
const MCP_ROUTE = /^#\/mcp/;
// Особистий кабінет (task-79) — перша ОСОБИСТА сторінка в тулі, доступна
// будь-якій ролі (на відміну від ADMIN_USERS_ROUTE вище): токени тут
// завжди свої власні.
const ACCOUNT_ROUTE = /^#\/account/;
// Екран згоди OAuth (task-78): сюди Claude приводить браузер із /authorize.
const OAUTH_CONSENT_ROUTE = /^#\/oauth\/consent/;

// A hash that starts with `#/` names a route. Every nav link, back-link and
// user-menu item in this app is written that way and is RELATIVE (`#/trends`),
// so clicking one from a server-backed path address (`/url/…`,
// `/archive/view/…`) changes only the hash and leaves the pathname in place.
// Without this check the pathname matched first and the click did nothing at
// all — the whole main menu was dead on those two pages. Reported live on
// `/url/…`; the same defect was already there on `/archive/view/…` since
// task-63, just far less visible because that page is opened far less often.
//
// The URL Inspector's own fragment (`#g=week&w=90d`) deliberately does NOT
// start with `#/`, so view state and navigation intent never collide.
function hashNamesRoute() {
  return (location.hash || "").startsWith("#/");
}

export function parseRoute() {
  const pathMatch = hashNamesRoute() ? null : location.pathname.match(ARCHIVE_VIEW_PATH_RE);
  if (pathMatch) {
    const [, snapshotId, ts, rawUrl] = pathMatch;
    let url = rawUrl;
    try {
      url = decodeURIComponent(rawUrl);
    } catch {
      // Not percent-encoded (the canonical form this app itself writes) —
      // `rawUrl` is already the real URL, use it as-is.
    }
    const query = new URLSearchParams({ snapshot: decodeURIComponent(snapshotId), url, ts });
    return { name: "archive-view", query };
  }
  const urlPathMatch = hashNamesRoute() ? null : location.pathname.match(URL_INSPECTOR_PATH_RE);
  if (urlPathMatch) {
    // `location.pathname` is already percent-DECODED by the browser, so a
    // url written raw arrives raw and one written encoded arrives decoded —
    // both land on the same value without a second decode pass here (a
    // second one would corrupt a url whose own path legitimately contains
    // `%2F`).
    const inspected = `${urlPathMatch[1]}${location.search}`;
    // Fragment carries the view options; it is `#g=week&w=90d`, with no
    // `#/route?` prefix, so it is parsed whole rather than split on `?`.
    const params = new URLSearchParams((location.hash || "").slice(1));
    params.set("u", inspected);
    return { name: "url-inspector", query: params };
  }
  const hash = location.hash || "";
  const query = new URLSearchParams(hash.split("?")[1] || "");
  if (ADMIN_USERS_ROUTE.test(hash)) return { name: "admin-users", query };
  if (ADMIN_AUDIT_ROUTE.test(hash)) return { name: "admin-audit", query };
  if (MCP_CONNECT_ROUTE.test(hash)) return { name: "mcp-connect", query };
  if (MCP_ROUTE.test(hash)) return { name: "mcp", query };
  if (OAUTH_CONSENT_ROUTE.test(hash)) return { name: "oauth-consent", query };
  if (ACCOUNT_ROUTE.test(hash)) return { name: "account", query };
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
  if (ARCHIVE_VIEW_ROUTE.test(hash)) return { name: "archive-view", query };
  if (ARCHIVE_CHANGES_ROUTE.test(hash)) return { name: "archive-changes", query };
  if (ARCHIVE_CAPTURE_ROUTE.test(hash)) return { name: "archive-capture", query };
  if (ARCHIVE_ROUTE.test(hash)) return { name: "archive", query };
  if (URL_INSPECTOR_ROUTE.test(hash)) return { name: "url-inspector", query };
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
  const monthlyMatch = hash.match(MONTHLY_ROUTE);
  if (monthlyMatch) return { name: "monthly", clientId: decodeURIComponent(monthlyMatch[1]), query };
  const monitorClientMatch = hash.match(MONITOR_CLIENT_ROUTE);
  if (monitorClientMatch) return { name: "monitor-client", clientId: decodeURIComponent(monitorClientMatch[1]), query };
  const pagesMatch = hash.match(PAGES_ROUTE);
  if (pagesMatch) return { name: "pages", clientId: decodeURIComponent(pagesMatch[1]), query };
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

export function buildMonthlyPerformanceHash(clientId, params = {}) {
  return buildHash(`#/client/${encodeURIComponent(clientId)}/monthly`, params);
}

export function buildPagesHash(clientId, params = {}) {
  return buildHash(`#/client/${encodeURIComponent(clientId)}/pages`, params);
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

// Web Archive (task-59) — portfolio hub, replay viewer, and change feed.
export function buildArchiveHash(params = {}) {
  return buildHash("#/archive", params);
}

export function replaceArchiveHash(params) {
  history.replaceState(null, "", `${location.pathname}${location.search}${buildArchiveHash(params)}`);
}

export function buildArchiveViewHash(params = {}) {
  return buildHash("#/archive/view", params);
}

export function replaceArchiveViewHash(params) {
  history.replaceState(
    null, "", `${location.pathname}${location.search}${buildArchiveViewHash(params)}`
  );
}

// task-63 §1 — the viewer's canonical, server-backed address. `url` is
// written RAW (not percent-encoded): shorter, and matches the convention
// `/archive/r/<ts>/<url>` already established (task-59) for the exact same
// kind of trailing path segment.
export function buildArchiveViewPath(snapshotId, ts, url) {
  return `/archive/view/snapshot/${encodeURIComponent(snapshotId)}/${ts}/${url}`;
}

// Canonicalizes the address bar to the new path WITHOUT adding a history
// entry — used once a snapshot has actually loaded (so the real `ts`/`url`
// are known) to fix up a legacy hash-form entry point, or to correct a
// `snapshot_id`/`ts` mismatch in a hand-edited address (task-63 §1.1: the
// two are resolved in favor of `snapshot_id`, then the address is silently
// canonicalized to match).
export function replaceArchiveViewPath(snapshotId, ts, url) {
  history.replaceState({ snapshotId }, "", buildArchiveViewPath(snapshotId, ts, url));
}

// A REAL navigation to a different version (Previous/Next, a calendar pick,
// an in-replay link click) — adds a history entry so Back/Forward walk
// versions one at a time, same as clicking through Previous/Next by hand.
export function pushArchiveViewPath(snapshotId, ts, url) {
  history.pushState({ snapshotId }, "", buildArchiveViewPath(snapshotId, ts, url));
}

export function buildArchiveChangesHash(params = {}) {
  return buildHash("#/archive/changes", params);
}

export function replaceArchiveChangesHash(params) {
  history.replaceState(
    null, "", `${location.pathname}${location.search}${buildArchiveChangesHash(params)}`
  );
}

// One capture's live progress + log (task-62 §1.5) — `params` always
// carries `id`, so F5 restores the exact same capture from the API.
export function buildArchiveCaptureHash(params = {}) {
  return buildHash("#/archive/capture", params);
}

// URL Inspector (task-61 + task-88 follow-up). The inspected url identifies
// the page and goes in the PATH, raw; `g`/`w` are view state and go in the
// fragment. See the URL_INSPECTOR_PATH_RE comment for why the split is that
// way round and not the obvious one.
export function buildUrlInspectorPath(url, params = {}) {
  const options = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== "") options.set(key, value);
  }
  const fragment = options.toString();
  // The url is placed verbatim: it already IS a url, and percent-encoding it
  // would only make the address unreadable in a chat message — the one place
  // these links actually get shared.
  return `/url/${url}${fragment ? `#${fragment}` : ""}`;
}

// Persists view state without a history entry per click, and — when the page
// was reached through the legacy hash — upgrades the address to the path form
// in the same call. `replaceState` never fires `hashchange`, so this cannot
// loop back into the listener that reloads on navigation.
export function replaceUrlInspectorAddress({ u, ...options }) {
  history.replaceState(null, "", buildUrlInspectorPath(u, options));
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

export function buildMcpHash(params = {}) {
  return buildHash("#/mcp", params);
}

export function buildMcpConnectHash(params = {}) {
  return buildHash("#/mcp/connect", params);
}

export function buildAccountHash(params = {}) {
  return buildHash("#/account", params);
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
  // task-71/72: `#/admin/users` and `#/admin/audit` are admin-only (see the
  // ADMIN_USERS_ROUTE comment above) — the user-menu links to them are
  // already hidden for non-admins (session.js applyRoleVisibility()), but
  // that only stops discovery, not someone typing/bookmarking the hash
  // directly. Without this, the branches below rendered the full page
  // shell (title, "Add person" button, table container) before the first
  // API call 403'd — not the "don't draw an empty screen" the comment
  // above promises. Checked here, before any DOM work, so a non-admin
  // never sees the shell and the client never even attempts the call the
  // server would refuse anyway.
  if (route.name === "admin-users" || route.name === "admin-audit") {
    const { isAdmin } = await import("./session.js?v=20260906-t72");
    if (!isAdmin()) {
      location.replace("#/");
      return;
    }
  }
  const dashboardView = document.querySelector("#dashboard-view");
  const clientRoot = document.querySelector("#client-page-root");
  const ahrefsRoot = document.querySelector("#ahrefs-page-root");
  const blogsRoot = document.querySelector("#blogs-page-root");
  const monthlyRoot = document.querySelector("#monthly-performance-page-root");
  const toolsRoot = document.querySelector("#tools-page-root");
  const trendsRoot = document.querySelector("#trends-page-root");
  const monitorRoot = document.querySelector("#monitor-page-root");
  const monitorClientRoot = document.querySelector("#monitor-client-page-root");
  const reportsRoot = document.querySelector("#reports-page-root");
  const reportsRunRoot = document.querySelector("#reports-run-page-root");
  const reportsBatchRoot = document.querySelector("#reports-batch-page-root");
  const pagesRoot = document.querySelector("#page-inventory-page-root");
  const archiveRoot = document.querySelector("#archive-page-root");
  const archiveViewRoot = document.querySelector("#archive-view-page-root");
  const archiveChangesRoot = document.querySelector("#archive-changes-page-root");
  const archiveCaptureRoot = document.querySelector("#archive-capture-page-root");
  const urlInspectorRoot = document.querySelector("#url-inspector-page-root");
  const adminUsersRoot = document.querySelector("#admin-users-page-root");
  const adminAuditRoot = document.querySelector("#admin-audit-page-root");
  const mcpRoot = document.querySelector("#mcp-page-root");
  const mcpConnectRoot = document.querySelector("#mcp-connect-page-root");
  const accountRoot = document.querySelector("#account-page-root");
  const oauthConsentRoot = document.querySelector("#oauth-consent-page-root");
  const dashboardChrome = [
    document.querySelector("#dashboard-header-actions"),
    document.querySelector("#dashboard-command-bar"),
  ];
  const allRoots = [
    clientRoot, ahrefsRoot, blogsRoot, monthlyRoot, toolsRoot, trendsRoot, monitorRoot, monitorClientRoot,
    reportsRoot, reportsRunRoot, reportsBatchRoot, pagesRoot, archiveRoot, archiveViewRoot, archiveChangesRoot,
    archiveCaptureRoot, urlInspectorRoot, adminUsersRoot, adminAuditRoot, mcpRoot, mcpConnectRoot, accountRoot, oauthConsentRoot,
  ];
  // Global nav marks the active destination for assistive tech and for the eye
  // (`aria-current`), since a cross-route move is a full reload here.
  const NAV_ROUTES = new Set([
    "tools", "trends", "monitor", "reports", "reports-run", "reports-batch", "archive", "admin",
  ]);
  document.querySelectorAll("[data-nav]").forEach((link) => {
    const navName = (route.name === "reports-run" || route.name === "reports-batch")
      ? "reports"
      : (route.name === "admin-users" || route.name === "admin-audit")
      ? "admin"
      : (route.name === "archive-view" || route.name === "archive-changes" || route.name === "archive-capture")
        ? "archive" : route.name;
    const isActive = link.dataset.nav === (NAV_ROUTES.has(navName) ? navName : "dashboard");
    if (isActive) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  });
  if (route.name === "oauth-consent") {
    dashboardView?.setAttribute("hidden", "");
    allRoots.filter((el) => el !== oauthConsentRoot).forEach((el) => el?.setAttribute("hidden", ""));
    dashboardChrome.forEach((el) => el?.setAttribute("hidden", ""));
    oauthConsentRoot?.removeAttribute("hidden");
    const { initOauthConsentPage } = await import("./oauth-consent-page.js?v=20260906-t78");
    await initOauthConsentPage(route.query.get("request") || "");
    return;
  }

  if (route.name === "account") {
    dashboardView?.setAttribute("hidden", "");
    allRoots.filter((el) => el !== accountRoot).forEach((el) => el?.setAttribute("hidden", ""));
    dashboardChrome.forEach((el) => el?.setAttribute("hidden", ""));
    accountRoot?.removeAttribute("hidden");
    const { initAccountPage } = await import("./account-page.js?v=20260906-t79");
    await initAccountPage();
  } else if (route.name === "admin-audit") {
    dashboardView?.setAttribute("hidden", "");
    allRoots.filter((el) => el !== adminAuditRoot).forEach((el) => el?.setAttribute("hidden", ""));
    dashboardChrome.forEach((el) => el?.setAttribute("hidden", ""));
    adminAuditRoot?.removeAttribute("hidden");
    const { initAdminAuditPage } = await import("./admin-audit-page.js?v=20260914-t93");
    await initAdminAuditPage(route.query);
  } else if (route.name === "mcp-connect") {
    dashboardView?.setAttribute("hidden", "");
    allRoots.filter((el) => el !== mcpConnectRoot).forEach((el) => el?.setAttribute("hidden", ""));
    dashboardChrome.forEach((el) => el?.setAttribute("hidden", ""));
    mcpConnectRoot?.removeAttribute("hidden");
    const { initMcpConnectPage } = await import("./mcp-page.js?v=20260906-t78x");
    await initMcpConnectPage();
  } else if (route.name === "mcp") {
    dashboardView?.setAttribute("hidden", "");
    allRoots.filter((el) => el !== mcpRoot).forEach((el) => el?.setAttribute("hidden", ""));
    dashboardChrome.forEach((el) => el?.setAttribute("hidden", ""));
    mcpRoot?.removeAttribute("hidden");
    const { initMcpPage } = await import("./mcp-page.js?v=20260906-t78x");
    await initMcpPage();
  } else if (route.name === "admin-users") {
    dashboardView?.setAttribute("hidden", "");
    allRoots.filter((el) => el !== adminUsersRoot).forEach((el) => el?.setAttribute("hidden", ""));
    dashboardChrome.forEach((el) => el?.setAttribute("hidden", ""));
    adminUsersRoot?.removeAttribute("hidden");
    const { initAdminUsersPage } = await import("./admin-users-page.js?v=20260906-t71");
    await initAdminUsersPage();
  } else if (route.name === "tools") {
    dashboardView?.setAttribute("hidden", "");
    allRoots.filter((el) => el !== toolsRoot).forEach((el) => el?.setAttribute("hidden", ""));
    dashboardChrome.forEach((el) => el?.setAttribute("hidden", ""));
    toolsRoot?.removeAttribute("hidden");
    const { initToolsRoute } = await import("./tools-page.js?v=20260914-t93");
    await initToolsRoute(route);
  } else if (route.name === "trends") {
    dashboardView?.setAttribute("hidden", "");
    allRoots.filter((el) => el !== trendsRoot).forEach((el) => el?.setAttribute("hidden", ""));
    dashboardChrome.forEach((el) => el?.setAttribute("hidden", ""));
    trendsRoot?.removeAttribute("hidden");
    const { initTrendsPage } = await import("./trends-page.js?v=20260821-spin3");
    await initTrendsPage(route.query);
  } else if (route.name === "monitor") {
    dashboardView?.setAttribute("hidden", "");
    allRoots.filter((el) => el !== monitorRoot).forEach((el) => el?.setAttribute("hidden", ""));
    dashboardChrome.forEach((el) => el?.setAttribute("hidden", ""));
    monitorRoot?.removeAttribute("hidden");
    const { initMonitorPage } = await import("./monitor-page.js?v=20260821-spin3");
    await initMonitorPage(route.query);
  } else if (route.name === "reports") {
    dashboardView?.setAttribute("hidden", "");
    allRoots.filter((el) => el !== reportsRoot).forEach((el) => el?.setAttribute("hidden", ""));
    dashboardChrome.forEach((el) => el?.setAttribute("hidden", ""));
    reportsRoot?.removeAttribute("hidden");
    const { initReportsPage } = await import("./reports-page.js?v=20260914-t93");
    await initReportsPage(route.query);
  } else if (route.name === "reports-run") {
    dashboardView?.setAttribute("hidden", "");
    allRoots.filter((el) => el !== reportsRunRoot).forEach((el) => el?.setAttribute("hidden", ""));
    dashboardChrome.forEach((el) => el?.setAttribute("hidden", ""));
    reportsRunRoot?.removeAttribute("hidden");
    const { initReportsRunPage } = await import("./reports-run.js?v=20260914-t93");
    await initReportsRunPage(route.runId, route.query);
  } else if (route.name === "reports-batch") {
    dashboardView?.setAttribute("hidden", "");
    allRoots.filter((el) => el !== reportsBatchRoot).forEach((el) => el?.setAttribute("hidden", ""));
    dashboardChrome.forEach((el) => el?.setAttribute("hidden", ""));
    reportsBatchRoot?.removeAttribute("hidden");
    const { initReportsBatchPage } = await import("./reports-batch.js?v=20260914-t93");
    await initReportsBatchPage(route.batchId);
  } else if (route.name === "ahrefs") {
    dashboardView?.setAttribute("hidden", "");
    allRoots.filter((el) => el !== ahrefsRoot).forEach((el) => el?.setAttribute("hidden", ""));
    dashboardChrome.forEach((el) => el?.setAttribute("hidden", ""));
    ahrefsRoot?.removeAttribute("hidden");
    const { initAhrefsPage } = await import("./ahrefs-page.js?v=20260821-spin3");
    initAhrefsPage(route.clientId, route.query);
  } else if (route.name === "blogs") {
    dashboardView?.setAttribute("hidden", "");
    allRoots.filter((el) => el !== blogsRoot).forEach((el) => el?.setAttribute("hidden", ""));
    dashboardChrome.forEach((el) => el?.setAttribute("hidden", ""));
    blogsRoot?.removeAttribute("hidden");
    const { initBlogsPage } = await import("./blogs-page.js?v=20260821-spin3");
    initBlogsPage(route.clientId, route.query);
  } else if (route.name === "monthly") {
    dashboardView?.setAttribute("hidden", "");
    allRoots.filter((el) => el !== monthlyRoot).forEach((el) => el?.setAttribute("hidden", ""));
    dashboardChrome.forEach((el) => el?.setAttribute("hidden", ""));
    monthlyRoot?.removeAttribute("hidden");
    const { initMonthlyPerformancePage } = await import("./monthly-performance-page.js?v=20260821-spin3");
    initMonthlyPerformancePage(route.clientId, route.query);
  } else if (route.name === "monitor-client") {
    dashboardView?.setAttribute("hidden", "");
    allRoots.filter((el) => el !== monitorClientRoot).forEach((el) => el?.setAttribute("hidden", ""));
    dashboardChrome.forEach((el) => el?.setAttribute("hidden", ""));
    monitorClientRoot?.removeAttribute("hidden");
    const { initMonitorClientPage } = await import("./monitor-client.js?v=20260821-spin3");
    initMonitorClientPage(route.clientId, route.query);
  } else if (route.name === "pages") {
    dashboardView?.setAttribute("hidden", "");
    allRoots.filter((el) => el !== pagesRoot).forEach((el) => el?.setAttribute("hidden", ""));
    dashboardChrome.forEach((el) => el?.setAttribute("hidden", ""));
    pagesRoot?.removeAttribute("hidden");
    const { initPageInventoryPage } = await import("./page-inventory-page.js?v=20260904-t57");
    initPageInventoryPage(route.clientId, route.query);
  } else if (route.name === "archive") {
    dashboardView?.setAttribute("hidden", "");
    allRoots.filter((el) => el !== archiveRoot).forEach((el) => el?.setAttribute("hidden", ""));
    dashboardChrome.forEach((el) => el?.setAttribute("hidden", ""));
    archiveRoot?.removeAttribute("hidden");
    const { initArchivePage } = await import("./archive-page.js?v=20260907-t80");
    await initArchivePage(route.query);
  } else if (route.name === "archive-view") {
    dashboardView?.setAttribute("hidden", "");
    allRoots.filter((el) => el !== archiveViewRoot).forEach((el) => el?.setAttribute("hidden", ""));
    dashboardChrome.forEach((el) => el?.setAttribute("hidden", ""));
    archiveViewRoot?.removeAttribute("hidden");
    const { initArchiveViewPage } = await import("./archive-viewer.js?v=20260907-t80");
    await initArchiveViewPage(route.query);
  } else if (route.name === "archive-changes") {
    dashboardView?.setAttribute("hidden", "");
    allRoots.filter((el) => el !== archiveChangesRoot).forEach((el) => el?.setAttribute("hidden", ""));
    dashboardChrome.forEach((el) => el?.setAttribute("hidden", ""));
    archiveChangesRoot?.removeAttribute("hidden");
    const { initArchiveChangesPage } = await import("./archive-changes.js?v=20260904-t59");
    await initArchiveChangesPage(route.query);
  } else if (route.name === "archive-capture") {
    dashboardView?.setAttribute("hidden", "");
    allRoots.filter((el) => el !== archiveCaptureRoot).forEach((el) => el?.setAttribute("hidden", ""));
    dashboardChrome.forEach((el) => el?.setAttribute("hidden", ""));
    archiveCaptureRoot?.removeAttribute("hidden");
    const { initArchiveCapturePage } = await import("./archive-capture.js?v=20260907-t80");
    await initArchiveCapturePage(route.query);
  } else if (route.name === "url-inspector") {
    dashboardView?.setAttribute("hidden", "");
    allRoots.filter((el) => el !== urlInspectorRoot).forEach((el) => el?.setAttribute("hidden", ""));
    dashboardChrome.forEach((el) => el?.setAttribute("hidden", ""));
    urlInspectorRoot?.removeAttribute("hidden");
    const { initUrlInspectorPage } = await import("./url-inspector.js?v=20260909-t88d");
    await initUrlInspectorPage(route.query);
  } else if (route.name === "client") {
    dashboardView?.setAttribute("hidden", "");
    allRoots.filter((el) => el !== clientRoot).forEach((el) => el?.setAttribute("hidden", ""));
    dashboardChrome.forEach((el) => el?.setAttribute("hidden", ""));
    clientRoot?.removeAttribute("hidden");
    const { initClientPage } = await import("./client-page.js?v=20260909-t89c");
    initClientPage(route.clientId, route.query);
  } else {
    allRoots.forEach((el) => el?.setAttribute("hidden", ""));
    dashboardView?.removeAttribute("hidden");
    dashboardChrome.forEach((el) => el?.removeAttribute("hidden"));
    await import("./dashboard.js?v=20260821-spin3");
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
//
// Guarded by `window.__yovthRouterBooted` (task-63 follow-up, live-reported
// 2026-09-05: "сторінки інколи не рендеряться, треба перезавантажити").
// Root cause: dozens of page modules import `router.js` under THEIR OWN
// cache-busting `?v=...` tag (`router.js?v=20260821-spin3` here,
// `router.js?v=20260905-t63` there, and so on) — each DISTINCT query
// string is a DISTINCT ES module specifier, so the browser fetches and
// evaluates a SEPARATE copy of this entire file for each one, running
// every top-level side effect (this listener, `initUrlSearch()`,
// `activate()` at the bottom) MULTIPLE times on a single page load. Two
// `activate()` calls racing each other's `root.innerHTML = ...` writes is
// exactly what intermittently left a page half-rendered or blank until a
// manual reload — `window` is the one thing every one of those separate
// module instances actually shares (they're separate module records, not
// separate realms), so a flag on it is the only place a cross-instance
// guard can live. The flag is set once, at the LAST side effect below
// (not here) — this check only ever READS it, so the first module
// instance to load still runs everything in order, top to bottom.
if (!window.__yovthRouterBooted) {
  window.addEventListener("hashchange", () => {
    // Leaving a server-backed path address for a hash route: go to the hash
    // route's OWN address (`/#/trends`) instead of reloading in place, so the
    // address bar stops naming a page we are no longer on. Costs exactly one
    // load, the same as the plain reload below — `location.replace` also
    // avoids leaving the abandoned path in the back stack.
    if (hashNamesRoute() && !isAppRootPath()) {
      location.replace(`/${location.hash}`);
      return;
    }
    location.reload();
  });
}

function isAppRootPath() {
  return location.pathname === "/" || location.pathname === "/index.html";
}

// URL Inspector header search (task-61) — global chrome, visible on every
// route, so it's wired here rather than inside any per-route module. Runs
// once per full page load (this app reloads on every hash change, see
// above), so no re-binding is needed across in-app navigation.
// Recently-inspected URLs, kept client-side only (task-61 follow-up) — the
// backend deliberately never persists a "history" (see app/urlinspector/
// live.py's own reasoning: a 7th table of page state wouldn't make the
// other six more true). `localStorage` is per-viewer/per-browser, which is
// exactly the right scope for "what did I just look at".
const URL_SEARCH_HISTORY_KEY = "url-inspector:history";
const URL_SEARCH_HISTORY_LIMIT = 12;
const URL_SEARCH_SUGGESTION_LIMIT = 8;
const URL_SEARCH_DEBOUNCE_MS = 200;

function readUrlSearchHistory() {
  try {
    const raw = localStorage.getItem(URL_SEARCH_HISTORY_KEY);
    const list = raw ? JSON.parse(raw) : [];
    return Array.isArray(list) ? list.filter((item) => typeof item === "string") : [];
  } catch {
    return []; // private window / storage blocked — degrade to no history, never throw
  }
}

function pushUrlSearchHistory(url) {
  try {
    const list = readUrlSearchHistory().filter((item) => item !== url);
    list.unshift(url);
    localStorage.setItem(URL_SEARCH_HISTORY_KEY, JSON.stringify(list.slice(0, URL_SEARCH_HISTORY_LIMIT)));
  } catch {
    // ignore — a lost history entry is not worth surfacing an error for
  }
}

function initUrlSearch() {
  const form = document.querySelector("#url-search-form");
  const input = document.querySelector("#url-search-input");
  const error = document.querySelector("#url-search-error");
  const list = document.querySelector("#url-search-suggestions");
  if (!form || !input || !list) return;

  // `.masthead` (app.css) clips overflow for the brand ribbon's own edges —
  // load-bearing there, but it also hard-clips this dropdown the instant it
  // grows taller than the header (caught live: rows past the masthead's own
  // bottom edge were never rendered at all, not merely covered). Toggling a
  // more-specific class (`classList`, never inline style — CSP has no
  // `style-src` allowance) lifts the clip for exactly as long as the
  // dropdown is open, and only there.
  const masthead = document.querySelector(".masthead");

  let items = []; // current suggestion strings, in the order rendered
  let activeIndex = -1;
  let debounceTimer = null;
  let requestToken = 0;

  function closeList() {
    list.hidden = true;
    list.innerHTML = "";
    items = [];
    activeIndex = -1;
    input.setAttribute("aria-expanded", "false");
    input.removeAttribute("aria-activedescendant");
    masthead?.classList.remove("masthead--search-open");
  }

  function renderList(nextItems, { historyMode } = {}) {
    items = nextItems;
    activeIndex = -1;
    input.removeAttribute("aria-activedescendant");
    if (!items.length) {
      closeList();
      return;
    }
    list.innerHTML = items.map((url, index) => `
      <li id="url-search-option-${index}" role="option" data-index="${index}">
        <svg class="icon icon--tiny" aria-hidden="true"><use href="/assets/icons/tabler-sprite.svg#tabler-${historyMode ? "history" : "link"}"></use></svg>
        <span>${url.replace(/^https?:\/\//, "")}</span>
      </li>`).join("");
    list.hidden = false;
    input.setAttribute("aria-expanded", "true");
    masthead?.classList.add("masthead--search-open");
  }

  function setActive(index) {
    const options = list.querySelectorAll("[role=option]");
    options.forEach((el) => el.removeAttribute("aria-selected"));
    activeIndex = index;
    if (index >= 0 && options[index]) {
      options[index].setAttribute("aria-selected", "true");
      options[index].scrollIntoView({ block: "nearest" });
      input.setAttribute("aria-activedescendant", options[index].id);
    } else {
      input.removeAttribute("aria-activedescendant");
    }
  }

  function showHistory() {
    const typed = input.value.trim().toLowerCase();
    const history = readUrlSearchHistory();
    const filtered = typed ? history.filter((url) => url.toLowerCase().includes(typed)) : history;
    renderList(filtered.slice(0, URL_SEARCH_SUGGESTION_LIMIT), { historyMode: true });
  }

  async function fetchSuggestions(q) {
    // A monotonic local token, not `api.js`'s own abort-per-key cancellation
    // (`request()` already cancels a same-key in-flight call, but a stale
    // response can still resolve after a newer one under `Promise` ordering
    // quirks) — this is the belt-and-braces guard against a fast typist
    // seeing an older keystroke's suggestions render last.
    const token = ++requestToken;
    try {
      const { getUrlInspectorSuggestions } = await import("./api.js?v=20260905-t64");
      const result = await getUrlInspectorSuggestions(q);
      if (token !== requestToken) return; // a newer keystroke already superseded this
      const suggestions = (result.suggestions || []).slice(0, URL_SEARCH_SUGGESTION_LIMIT);
      if (suggestions.length) renderList(suggestions, { historyMode: false });
      else showHistory();
    } catch {
      if (token === requestToken) showHistory();
    }
  }

  function submitUrl(raw) {
    if (!raw) {
      if (error) error.textContent = "Paste a page URL to inspect.";
      return;
    }
    if (error) error.textContent = "";
    pushUrlSearchHistory(raw);
    closeList();
    // A real path change, so a real navigation — which costs exactly what
    // today's hash navigation already costs: this app reloads on every
    // `hashchange` anyway (see the listener above).
    location.assign(buildUrlInspectorPath(raw));
  }

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    submitUrl(input.value.trim());
  });

  input.addEventListener("focus", showHistory);

  input.addEventListener("input", () => {
    if (error) error.textContent = "";
    const q = input.value.trim();
    clearTimeout(debounceTimer);
    if (q.length < 2) {
      showHistory();
      return;
    }
    debounceTimer = setTimeout(() => fetchSuggestions(q), URL_SEARCH_DEBOUNCE_MS);
  });

  input.addEventListener("keydown", (event) => {
    // A closed list still opens on Down/Up, same as a native <select> or any
    // WAI-ARIA combobox — the operator shouldn't need to type first just to
    // see their own history.
    if (list.hidden) {
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        showHistory();
        if (items.length) setActive(event.key === "ArrowDown" ? 0 : items.length - 1);
      }
      return;
    }
    if (event.key === "ArrowDown") {
      event.preventDefault();
      // Wraps around both ends — the list is short (<=8 rows) and always
      // fully visible, so "past the last item" landing back on the first
      // one reads as a loop, not a jump to somewhere unseen.
      setActive(activeIndex + 1 >= items.length ? 0 : activeIndex + 1);
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setActive(activeIndex - 1 < 0 ? items.length - 1 : activeIndex - 1);
    } else if (event.key === "Home") {
      event.preventDefault();
      setActive(0);
    } else if (event.key === "End") {
      event.preventDefault();
      setActive(items.length - 1);
    } else if (event.key === "Enter" && activeIndex >= 0) {
      event.preventDefault();
      submitUrl(items[activeIndex]);
    } else if (event.key === "Escape") {
      event.preventDefault();
      closeList();
    } else if (event.key === "Tab") {
      closeList();
    }
  });

  // Blur closes the dropdown (Tab away, click outside a focusable element,
  // etc.) — safe alongside mouse selection: the list's own `mousedown`
  // handler below calls `preventDefault()`, which stops that click from
  // ever moving focus off the input in the first place, so `blur` never
  // fires for a suggestion pick.
  input.addEventListener("blur", closeList);

  list.addEventListener("mousedown", (event) => {
    // `mousedown`, not `click`: fires before the input's `blur`, so the list
    // is still open (and its option still in the DOM) when the selection is read.
    const option = event.target.closest("[role=option]");
    if (!option) return;
    event.preventDefault();
    submitUrl(items[Number(option.dataset.index)]);
  });

  document.addEventListener("click", (event) => {
    if (!form.contains(event.target)) closeList();
  });
}

// The actual guard (see the long comment by the `hashchange` listener
// above) — set exactly once, by whichever module instance's synchronous
// top-level execution reaches this line FIRST. A second, duplicate
// instance (same file, different `?v=` cache-bust tag) finds this already
// `true` and skips both calls entirely.
if (!window.__yovthRouterBooted) {
  window.__yovthRouterBooted = true;
  // task-71: сесія резолвиться ПЕРЕД першим рендером — інакше меню
  // користувача і пункт «People» блимали б на місці вже намальованої
  // сторінки. Один запит на завантаження вкладки, не на кожен маршрут.
  (async () => {
    try {
      const { loadSession, renderUserMenu, applyRoleVisibility } = await import("./session.js?v=20260906-t72");
      const session = await loadSession();
      if (!session?.authenticated) {
        const next = encodeURIComponent(`${location.pathname}${location.search}${location.hash}`);
        location.replace(`/login.html?next=${next}`);
        return;
      }
      renderUserMenu(session);
      applyRoleVisibility();
    } catch {
      // Сесію не вдалось прочитати (мережа) — додаток усе одно вантажимо:
      // перший же API-виклик отримає 401 і сам поведе на форму входу.
    }
    initUrlSearch();
    activate();
  })();
}
