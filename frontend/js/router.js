const CLIENT_ROUTE = /^#\/client\/([^/?]+)/;

export function parseRoute() {
  const hash = location.hash || "";
  const match = hash.match(CLIENT_ROUTE);
  if (!match) return { name: "dashboard" };
  const query = new URLSearchParams(hash.split("?")[1] || "");
  return { name: "client", clientId: decodeURIComponent(match[1]), query };
}

export function buildClientHash(clientId, params = {}) {
  const query = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== "") query.set(key, String(value));
  });
  const qs = query.toString();
  return `#/client/${encodeURIComponent(clientId)}${qs ? `?${qs}` : ""}`;
}

// Persists client-page state (period/tab/sort/…) without adding a browser
// history entry per keystroke, mirroring store.js's `persistUrl` for the
// dashboard. `replaceState` never fires `hashchange`, so this cannot loop
// back into the listener below.
export function replaceClientHash(clientId, params) {
  history.replaceState(null, "", `${location.pathname}${location.search}${buildClientHash(clientId, params)}`);
}

async function activate() {
  const route = parseRoute();
  const dashboardView = document.querySelector("#dashboard-view");
  const clientRoot = document.querySelector("#client-page-root");
  const dashboardChrome = [
    document.querySelector("#dashboard-header-actions"),
    document.querySelector("#dashboard-command-bar"),
  ];
  if (route.name === "client") {
    dashboardView?.setAttribute("hidden", "");
    dashboardChrome.forEach((el) => el?.setAttribute("hidden", ""));
    clientRoot?.removeAttribute("hidden");
    const { initClientPage } = await import("./client-page.js");
    initClientPage(route.clientId, route.query);
  } else {
    clientRoot?.setAttribute("hidden", "");
    dashboardView?.removeAttribute("hidden");
    dashboardChrome.forEach((el) => el?.removeAttribute("hidden"));
    await import("./dashboard.js");
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
