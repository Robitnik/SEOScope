import { getSyncJob, isFixtureMode } from "./api.js";
import { isTerminal } from "./formatters.js";

export function watchJob({ id, eventsUrl, lastEventId = "", onProgress, onConnection, onTerminal, onError }) {
  let stopped = false;
  let source = null;
  let timer = null;
  let currentEventId = lastEventId;

  async function refresh() {
    try {
      const job = await getSyncJob(id);
      if (stopped) return;
      onProgress(job, currentEventId);
      if (isTerminal(job.status)) {
        stop();
        onTerminal(job);
      }
    } catch (error) {
      if (!stopped) onError(error);
    }
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
      refresh();
    };
    source.addEventListener("progress", (event) => {
      currentEventId = event.lastEventId || currentEventId;
      refresh();
    });
    source.onerror = () => {
      onConnection("offline");
      refresh();
    };
    refresh();
  }

  function stop() {
    stopped = true;
    source?.close();
    if (timer) clearInterval(timer);
  }

  connect();
  return stop;
}
