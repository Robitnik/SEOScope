const ACTIVE_JOB_KEY = "theyovth.active-sync.v1";

function initialState() {
  const params = new URLSearchParams(location.search);
  const group = ["vision", "vertex"].includes(params.get("group")) ? params.get("group") : "all";
  const sortModes = ["quality", "health", "name", "last_sync", "biggest_drop"];
  return {
    period: params.get("period") === "month" ? "month" : "week",
    query: params.get("q") || "",
    status: params.get("status") || "",
    provider: params.get("provider") || "",
    sort: sortModes.includes(params.get("sort")) ? params.get("sort") : "quality",
    group,
    dashboard: null,
    loading: true,
    error: null,
  };
}

let state = initialState();
const listeners = new Set();

export function getState() { return state; }

export function setState(patch, { persist = false } = {}) {
  state = { ...state, ...patch };
  if (persist) persistUrl();
  listeners.forEach((listener) => listener(state));
}

export function subscribe(listener) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function persistUrl() {
  const params = new URLSearchParams(location.search);
  for (const key of ["period", "q", "status", "provider", "sort", "group"]) {
    const value = key === "q" ? state.query : state[key];
    if (value && !(key === "period" && value === "week") && !(key === "sort" && value === "quality") && !(key === "group" && value === "all")) params.set(key, value);
    else params.delete(key);
  }
  history.replaceState(null, "", `${location.pathname}${params.size ? `?${params}` : ""}${location.hash}`);
}

export function saveActiveJob(job) {
  if (job) localStorage.setItem(ACTIVE_JOB_KEY, JSON.stringify(job));
  else localStorage.removeItem(ACTIVE_JOB_KEY);
}

export function loadActiveJob() {
  try { return JSON.parse(localStorage.getItem(ACTIVE_JOB_KEY)); }
  catch { return null; }
}
