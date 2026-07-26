const AHREFS_ROUTE = /^#\/client\/([^/?]+)\/ahrefs/;
const BLOGS_ROUTE = /^#\/client\/([^/?]+)\/blogs/;
const CLIENT_ROUTE = /^#\/client\/([^/?]+)/;
// Tools hub and one tool, optionally deep-linked to a single report
// (`#/tools/alt-text/<reportId>`). Added by task-27.
const TOOLS_ROUTE = /^#\/tools(?:\/([^/?]+))?(?:\/([^/?]+))?/;

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
  const ahrefsMatch = hash.match(AHREFS_ROUTE);
  if (ahrefsMatch) return { name: "ahrefs", clientId: decodeURIComponent(ahrefsMatch[1]), query };
  const blogsMatch = hash.match(BLOGS_ROUTE);
  if (blogsMatch) return { name: "blogs", clientId: decodeURIComponent(blogsMatch[1]), query };
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
  const dashboardChrome = [
    document.querySelector("#dashboard-header-actions"),
    document.querySelector("#dashboard-command-bar"),
  ];
  // Global nav marks the active destination for assistive tech and for the eye
  // (`aria-current`), since a cross-route move is a full reload here.
  document.querySelectorAll("[data-nav]").forEach((link) => {
    const isActive = link.dataset.nav === (route.name === "tools" ? "tools" : "dashboard");
    if (isActive) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  });
  if (route.name === "tools") {
    dashboardView?.setAttribute("hidden", "");
    clientRoot?.setAttribute("hidden", "");
    ahrefsRoot?.setAttribute("hidden", "");
    blogsRoot?.setAttribute("hidden", "");
    dashboardChrome.forEach((el) => el?.setAttribute("hidden", ""));
    toolsRoot?.removeAttribute("hidden");
    const { initToolsRoute } = await import("./tools-page.js?v=20260726-tools1");
    await initToolsRoute(route);
  } else if (route.name === "ahrefs") {
    dashboardView?.setAttribute("hidden", "");
    clientRoot?.setAttribute("hidden", "");
    blogsRoot?.setAttribute("hidden", "");
    toolsRoot?.setAttribute("hidden", "");
    dashboardChrome.forEach((el) => el?.setAttribute("hidden", ""));
    ahrefsRoot?.removeAttribute("hidden");
    const { initAhrefsPage } = await import("./ahrefs-page.js?v=20260724-blogs1");
    initAhrefsPage(route.clientId, route.query);
  } else if (route.name === "blogs") {
    dashboardView?.setAttribute("hidden", "");
    clientRoot?.setAttribute("hidden", "");
    ahrefsRoot?.setAttribute("hidden", "");
    toolsRoot?.setAttribute("hidden", "");
    dashboardChrome.forEach((el) => el?.setAttribute("hidden", ""));
    blogsRoot?.removeAttribute("hidden");
    const { initBlogsPage } = await import("./blogs-page.js?v=20260724-blogs1");
    initBlogsPage(route.clientId, route.query);
  } else if (route.name === "client") {
    dashboardView?.setAttribute("hidden", "");
    ahrefsRoot?.setAttribute("hidden", "");
    blogsRoot?.setAttribute("hidden", "");
    toolsRoot?.setAttribute("hidden", "");
    dashboardChrome.forEach((el) => el?.setAttribute("hidden", ""));
    clientRoot?.removeAttribute("hidden");
    const { initClientPage } = await import("./client-page.js?v=20260724-bulkaudit2");
    initClientPage(route.clientId, route.query);
  } else {
    clientRoot?.setAttribute("hidden", "");
    ahrefsRoot?.setAttribute("hidden", "");
    blogsRoot?.setAttribute("hidden", "");
    toolsRoot?.setAttribute("hidden", "");
    dashboardView?.removeAttribute("hidden");
    dashboardChrome.forEach((el) => el?.removeAttribute("hidden"));
    await import("./dashboard.js?v=20260724-bulkaudit2");
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
