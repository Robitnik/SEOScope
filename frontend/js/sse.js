import { getSyncJob, isFixtureMode } from "./api.js?v=20260731-refresh1";
import { isTerminal } from "./formatters.js?v=20260731-refresh1";

// One job snapshot poll per this many ms at most (task-30). Every SSE event
// used to trigger its own `getSyncJob`, and `request()` aborts the previous
// call under the same `job-<id>` key — so a run with several cells in flight
// spent most of its polls cancelling each other, and each cancellation had to
// be filtered out of the error path by name. Coalescing makes the stream a
// "something changed" signal and keeps exactly one poll per window.
const REFRESH_INTERVAL_MS = 250;
// Safety net: if an event is ever dropped (proxy buffering, a sleeping tab),
// the view still converges instead of freezing on a stale snapshot. Keepalive
// comments do not fire `onmessage`, so this cannot come from the stream.
const HEARTBEAT_MS = 10_000;

export function watchJob({ id, eventsUrl, lastEventId = "", onProgress, onConnection, onTerminal, onError }) {
  let stopped = false;
  let source = null;
  let timer = null;
  let refreshTimer = null;
  let heartbeat = null;
  let lastRefreshAt = 0;
  let currentEventId = lastEventId;

  async function refresh() {
    lastRefreshAt = Date.now();
    try {
      const job = await getSyncJob(id);
      if (stopped) return;
      onProgress(job, currentEventId);
      if (isTerminal(job.status)) {
        stop();
        onTerminal(job);
      }
    } catch (error) {
      if (stopped) return;
      // Rapid SSE events can still overlap two polls (a slow response plus a
      // fresh trigger); `request()` aborts the superseded one under the shared
      // `job-<id>` key. That is expected supersession — the newer poll delivers
      // the state — not a real failure, so it must NOT surface as a "Progress
      // temporarily unavailable" toast (that spammed the UI).
      if (error?.name === "AbortError" || /\baborted\b/i.test(error?.message || "")) return;
      onError(error);
    }
  }

  /** Collapse a burst of events into one poll, no more often than
   * `REFRESH_INTERVAL_MS`, always with a trailing call so the last event in a
   * burst is never the one that gets dropped. */
  function scheduleRefresh() {
    if (stopped || refreshTimer) return;
    const wait = Math.max(0, REFRESH_INTERVAL_MS - (Date.now() - lastRefreshAt));
    refreshTimer = setTimeout(() => {
      refreshTimer = null;
      refresh();
    }, wait);
  }

  function connect() {
    if (isFixtureMode()) {
      onConnection("connected");
      refresh();
      timer = setInterval(refresh, 650);
      return;
    }
    const url = new URL(eventsUrl, location.origin);
    if (currentEventId) url.searchParams.set("last_event_id", currentEventId);
    source = new EventSource(url);
    source.onopen = () => onConnection("connected");
    source.onmessage = (event) => {
      currentEventId = event.lastEventId || currentEventId;
      scheduleRefresh();
    };
    source.addEventListener("progress", (event) => {
      currentEventId = event.lastEventId || currentEventId;
      scheduleRefresh();
    });
    source.onerror = () => {
      onConnection("offline");
      scheduleRefresh();
    };
    heartbeat = setInterval(scheduleRefresh, HEARTBEAT_MS);
    refresh();
  }

  function stop() {
    stopped = true;
    source?.close();
    if (timer) clearInterval(timer);
    if (heartbeat) clearInterval(heartbeat);
    if (refreshTimer) clearTimeout(refreshTimer);
  }

  connect();
  return stop;
}
