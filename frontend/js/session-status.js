// task-22: browser-session bridge (task-20) HTTP client, small and
// framework-free. Two jobs:
//
//   * `triggerLogin(provider)` — wraps `POST /providers/{provider}/login`,
//     normalizing task-20's three real outcomes (started / no warm Chrome /
//     other failure) into one result shape both call sites render from.
//   * `watchProviderSession(provider, ...)` — polls the live
//     `GET /providers/{provider}/session` liveness probe. This is only ever
//     needed where there is no job/SSE stream to lean on already: the
//     standalone client-card «Log in» action. Inside the sync modal, the
//     job's own SSE stream is authoritative and already flowing (the lane
//     auto-resumes and emits normal cell progress once it sees the session
//     alive) — polling there is a purely cosmetic head start, never the
//     source of truth.
import { getProviderSession, startProviderLogin } from "./api.js?v=20260723-audit1";

export async function triggerLogin(provider) {
  try {
    const result = await startProviderLogin(provider);
    return { ok: true, state: result.state, loginSessionId: result.login_session_id, timeoutSeconds: result.timeout_seconds };
  } catch (error) {
    if (error.status === 503 || error.code === "no_warm_chrome") {
      return { ok: false, reason: "no_warm_chrome", message: error.message };
    }
    return { ok: false, reason: "error", message: error.message };
  }
}

export function watchProviderSession(provider, { onChange, intervalMs = 2000 } = {}) {
  let stopped = false;
  let lastState = null;
  let timer = null;

  async function poll() {
    if (stopped) return;
    try {
      const { state } = await getProviderSession(provider);
      if (state !== lastState) {
        lastState = state;
        onChange?.(state);
      }
      if (state === "alive") {
        stopped = true;
        return;
      }
    } catch {
      // Transient poll failure — the next tick tries again; nothing to surface mid-wait.
    }
    if (!stopped) timer = setTimeout(poll, intervalMs);
  }

  timer = setTimeout(poll, 0);
  return function stop() {
    stopped = true;
    if (timer) clearTimeout(timer);
  };
}
