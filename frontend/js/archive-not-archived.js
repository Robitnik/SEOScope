// Shared "Not in the archive" panel (task-70) — one markup, one message,
// one "Archive this page now" action, used from THREE places that must
// never say different things about the exact same condition:
//   1. the hub's own lookup result (`archive-page.js`) when a URL has
//      never been captured,
//   2. the viewer (`archive-viewer.js`) when a `/archive/view/snapshot/
//      <id>/<ts>/<url>` address's own `url` segment doesn't resolve to a
//      captured version — the case that used to silently show a DIFFERENT
//      page under the requested address instead (task-63 §1.1, reversed
//      here),
//   3. the same viewer's chrome, reacting to a `postMessage` from a replay
//      iframe whose OWN in-page link pointed at an uncaptured page
//      (`app/api/archive_replay.py`'s page-shaped miss).
import { escapeHtml, icon } from "./formatters.js?v=20260821-spin3";

// `capturable` (`ArchiveLookupDTO.capturable`) is known BEFORE any click —
// resolved server-side from the URL's own host — so a non-client domain
// gets a flat explanation instead of a button that would only 404.
export function notArchivedMarkup({
  url, clientName, capturable, starting = false, error = null, idPrefix = "archive-not-archived",
}) {
  return `
    <div class="state-panel state-not_configured archive-not-archived">
      ${icon("history-off")}
      <p><strong>Not in the archive.</strong><br>
      ${capturable
        ? `This page${clientName ? ` (<strong>${escapeHtml(clientName)}</strong>)` : ""} hasn't been captured at this address yet. Capture it now instead of waiting for the next pass.`
        : "Only client sites can be archived, and this domain doesn't match any of ours."}</p>
      ${capturable ? `
      <button type="button" class="button button--primary" id="${idPrefix}-capture" ${starting ? "disabled" : ""}>
        ${icon(starting ? "loader-2" : "camera", starting ? "icon icon--spin" : "icon")}
        ${starting ? "Starting…" : "Archive this page now"}
      </button>` : ""}
      ${error ? `<p class="archive-not-archived__error">${icon("alert-triangle")}${escapeHtml(error)}</p>` : ""}
    </div>`;
}

export function bindNotArchivedActions(container, { onCapture, idPrefix = "archive-not-archived" }) {
  container.querySelector(`#${idPrefix}-capture`)?.addEventListener("click", onCapture);
}
