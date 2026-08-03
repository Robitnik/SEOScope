// Single owner of the sync/progress dialog (task-30).
//
// Why this module exists: `createSyncModal` used to be instantiated in
// `dashboard.js` module scope, and `dashboard.js` is only imported on the
// dashboard route (`router.js`). Every other page — `#/trends` above all —
// therefore had no way to start a collection, and an already-running job
// restored from `localStorage` stayed invisible there even though the job dock
// markup is global (`index.html`).
//
// The dialog is a singleton on purpose: two instances would both call
// `watchJob` on the same job id, doubling SSE connections and fighting over
// `#sync-dialog`. Pages contribute only their own callbacks through
// `configureJobProgress` — the instance itself is created once, lazily, on the
// first `getJobProgress()`.
import { createSyncModal } from "./sync-modal.js?v=20260803-progress2";

let instance = null;

const hooks = {
  // What the modal needs to name clients and (for the metrics preset) read the
  // dashboard's period. `null` is valid: the selection view is simply not used
  // on pages that only start a preset run.
  getDashboard: () => null,
  onFinished: async () => {},
  onToast: (message) => showToast(message),
};

/** Toast in the app-wide live region (`index.html`). Shared so every page
 * reports job outcomes the same way instead of re-implementing it. */
export function showToast(message) {
  const region = document.querySelector("#toast-region");
  if (!region) return;
  const element = document.createElement("div");
  element.className = "toast-message";
  element.textContent = message;
  region.append(element);
  setTimeout(() => element.remove(), 4200);
}

/** Register the current page's callbacks. Call before `getJobProgress()`;
 * calling it again replaces only the keys provided. */
export function configureJobProgress(next = {}) {
  Object.assign(hooks, next);
}

export function getJobProgress() {
  if (instance) return instance;
  instance = createSyncModal({
    // Indirection through `hooks` (not the values themselves) so a page can
    // configure itself after the instance already exists — e.g. a job restored
    // from localStorage starts being watched at import time, before the trends
    // payload that supplies client names has loaded.
    getDashboard: () => hooks.getDashboard(),
    onFinished: (job) => hooks.onFinished(job),
    onToast: (message) => hooks.onToast(message),
  });
  return instance;
}
