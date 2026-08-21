// Monthly Performance (task-54) — the operator's Google Sheet, reproduced
// from Reporting Ninja instead of hand-kept. Deliberately a SHEET-FIDELITY
// island inside the dark control-room UI (frontend/AGENTS.md §2/§3
// deviation, agreed with the user, task-54 §7): the operator asked for the
// exact colored-header look of the source sheet, not a themed reinterpretation
// of it. `monthly-table.css` owns every literal color used here — none leak
// into `app.css`/`client-page.css`.
//
// A pure render module (AGENTS §12: fetching/normalizing stay in
// `client-page.js`'s loader, this file only turns a `MonthlyTableDTO` into
// markup and wires its own controls) — same shape as
// `reporting-ninja-panel.js::renderReportingNinjaPanel`.
import { escapeHtml, formatTimestamp, icon, statusIcon } from "./formatters.js?v=20260821-spin3";

const _countFormatter = new Intl.NumberFormat("en-US", { useGrouping: false });
const _groupedFormatter = new Intl.NumberFormat("en-US");
const _percentFormatter = new Intl.NumberFormat("en-US", {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

// `Total organic Users`/`Clicks`/`Organic Conversions`/the named-event column
// render WITHOUT digit grouping (`1208`, not `1,208`) — the operator's own
// sheet used that format for every column except `Impressions`, and task-54
// §3.3 keeps it column-by-column rather than forcing one rule on all seven.
function formatCount(value) {
  return _countFormatter.format(value);
}

function formatGroupedCount(value) {
  return _groupedFormatter.format(value);
}

function formatPercent(value) {
  return `${_percentFormatter.format(value * 100)}%`;
}

// PLAN §12 / AGENTS §5: `null` is never rendered as `0` or blank — always
// this exact glyph, with the reason available in the cell's `data-state`.
function formatCell(value, formatter) {
  return value === null || value === undefined ? "—" : formatter(value);
}

// The 7 data columns in the exact order/colors of the source sheet (task-54
// §3.1). `stateKey` says which of `ga4_state`/`gsc_state` governs the muted
// styling and tooltip for that column; `optional` hides the column entirely
// when the client has no conversion event configured yet.
const MONTHLY_COLUMNS = [
  { key: "organic_users", label: "Total organic Users", tone: "green", format: formatCount, stateKey: "ga4_state" },
  { key: "gsc_impressions", label: "Impressions", tone: "yellow", format: formatGroupedCount, stateKey: "gsc_state" },
  { key: "gsc_clicks", label: "Clicks", tone: "blue", format: formatCount, stateKey: "gsc_state" },
  { key: "organic_conversions", label: "Organic Conversions", tone: "red", format: formatCount, stateKey: "ga4_state" },
  { key: "conversion_rate", label: "Conversion rate", tone: "green", format: formatPercent, stateKey: "ga4_state" },
  { key: "key_event_count", label: null, tone: "green", format: formatCount, stateKey: "ga4_state", optional: true },
  { key: "ctr", label: "CTR", tone: "blue", format: formatPercent, stateKey: "gsc_state" },
];

const STATE_NOTE = {
  no_data: "No data for this month.",
  not_configured: "Not connected for this client.",
  failed: "The last collection attempt failed — showing the last known value.",
};

function directionGlyph(direction) {
  if (direction === "up") return "▲";
  if (direction === "down") return "▼";
  return "";
}

function directionWord(direction) {
  if (direction === "up") return "up";
  if (direction === "down") return "down";
  return "unchanged";
}

function resolvedColumns(coverage, keyEventLabel) {
  return MONTHLY_COLUMNS.filter((column) => !column.optional || coverage?.has_key_event_column).map(
    (column) =>
      column.key === "key_event_count"
        ? { ...column, label: `Organic "${keyEventLabel || "Event"}"` }
        : column,
  );
}

function renderHeaderRow(columns) {
  return `<tr>
    <th class="mt-th mt-th--month"><span class="visually-hidden">Month</span></th>
    ${columns.map((column) => `<th class="mt-th mt-th--${column.tone}">${escapeHtml(column.label)}</th>`).join("")}
  </tr>`;
}

function renderCell(column, row) {
  const value = row[column.key];
  const state = row[column.stateKey] || "ok";
  const direction = row.directions?.[column.key];
  const text = formatCell(value, column.format);
  const glyph = directionGlyph(direction);
  const note = state !== "ok" ? STATE_NOTE[state] : "";
  return `<td class="mt-cell mt-cell--${column.tone}${state !== "ok" ? " mt-cell--muted" : ""}" data-state="${state}"${note ? ` title="${escapeHtml(note)}"` : ""}>
    <span class="mt-value">${escapeHtml(text)}</span>${
      glyph
        ? `<span class="mt-direction mt-direction--${direction}" aria-hidden="true">${glyph}</span><span class="visually-hidden">${escapeHtml(column.label)} ${directionWord(direction)} from the previous month</span>`
        : ""
    }
  </td>`;
}

function rowBadges(row) {
  const badges = [];
  if (row.is_partial_period) {
    badges.push(
      `<span class="mt-badge mt-badge--partial" title="${row.gsc_days_with_data} of ${row.days_in_month} days have data — this month is at the edge of Search Console's data window.">partial</span>`,
    );
  }
  if (row.truncated) {
    badges.push(
      `<span class="mt-badge mt-badge--warn" title="This total may be undercounted — the underlying query hit its row limit.">may be incomplete</span>`,
    );
  }
  return badges.join(" ");
}

function renderRow(row, columns) {
  const badges = rowBadges(row);
  return `<tr>
    <th scope="row" class="mt-th mt-th--row">${escapeHtml(row.label)}${badges ? ` ${badges}` : ""}</th>
    ${columns.map((column) => renderCell(column, row)).join("")}
  </tr>`;
}

function coverageNotes(coverage) {
  const notes = [];
  if (!coverage?.has_gsc) {
    notes.push("Search Console isn't connected for this client — Impressions, Clicks and CTR are unavailable.");
  }
  if (!coverage?.has_key_event_column) {
    notes.push('No conversion event is configured yet — open "Configure" below to add one.');
  }
  return notes;
}

function attributionOptions(selected) {
  return `
    <option value="first_user" ${selected === "first_user" ? "selected" : ""}>First-user (who first found the site)</option>
    <option value="session" ${selected === "session" ? "selected" : ""}>Session (this specific visit)</option>`;
}

function settingsForm(settings, keyEventCandidates, open) {
  const namesValue = (settings.key_event_names || []).join(", ");
  const candidates = keyEventCandidates?.length
    ? `<datalist id="mt-event-candidates">${keyEventCandidates
        .map(
          (candidate) =>
            `<option value="${escapeHtml(candidate.event_name)}">${escapeHtml(candidate.event_name)} — ${candidate.key_events} events across ${candidate.months_seen} months</option>`,
        )
        .join("")}</datalist>`
    : "";
  return `
    <details class="mt-settings"${open ? " open" : ""}>
      <summary>${icon("adjustments", "icon icon--tiny")}Configure</summary>
      <form class="mt-settings__form" id="mt-settings-form">
        <div class="mt-settings__row">
          <label>Users attribution<select name="attribution_users">${attributionOptions(settings.attribution_users)}</select></label>
          <label>Conversions &amp; event attribution<select name="attribution_events">${attributionOptions(settings.attribution_events)}</select></label>
        </div>
        <label>Conversion event column label<input type="text" name="key_event_label" value="${escapeHtml(settings.key_event_label || "")}" placeholder="e.g. Book Now"></label>
        <label>Event name(s), comma-separated — a client that splits one conversion across locations needs more than one here
          <input type="text" name="key_event_names" list="mt-event-candidates" value="${escapeHtml(namesValue)}" placeholder="Book Now, Book Now 3rd Avenue, ...">
        </label>
        ${candidates}
        <div class="mt-settings__actions">
          <button type="button" class="button button--quiet" id="mt-discover-events">${icon("search", "icon icon--tiny")}Suggest events from data</button>
          <button type="submit" class="button button--primary" id="mt-settings-save">Save</button>
        </div>
        <p class="mt-settings__note" id="mt-settings-note" aria-live="polite"></p>
      </form>
    </details>`;
}

// `table`/`settings` come straight off `GET .../monthly-table` (task-54
// §2.5). `keyEventCandidates` is `undefined` until `onLoadKeyEvents` has been
// called at least once (the discovery request costs a Reporting Ninja call,
// so it only fires when the operator opens Configure, not on every page load).
export function renderMonthlyTablePanel(
  container,
  {
    table, settings, keyEventCandidates, busy = false, refreshError = "", detailsOpen = false,
    exportUrl = null, onRefresh, onSaveSettings, onLoadKeyEvents,
  },
) {
  const rows = table?.rows || [];
  const coverage = table?.coverage;
  const columns = resolvedColumns(coverage, settings?.key_event_label);

  container.innerHTML = `
    <div class="section-heading">
      <div><p class="eyebrow">Monthly Performance</p><h2>Month-over-month table</h2></div>
      <div class="mt-actions">
        <label class="mt-toggle"><input type="checkbox" id="mt-plain-colors">Sheet colors only</label>
        ${exportUrl && rows.length ? `<a class="button button--quiet" href="${escapeHtml(exportUrl)}" download>${icon("file-spreadsheet", "icon icon--tiny")}Export to Excel</a>` : ""}
        <button type="button" class="button button--quiet" id="mt-refresh" ${busy ? "disabled" : ""}>
          ${busy ? statusIcon("loader-2") : icon("refresh")}${busy ? "Refreshing…" : "Refresh"}
        </button>
      </div>
    </div>
    ${coverageNotes(coverage)
      .map((note) => `<p class="mt-note">${icon("info-circle", "icon icon--tiny")}${escapeHtml(note)}</p>`)
      .join("")}
    ${refreshError ? `<p class="mt-note mt-note--error">${icon("alert-triangle", "icon icon--tiny")}${escapeHtml(refreshError)} The table below still shows the last successful collection.</p>` : ""}
    ${settings ? settingsForm(settings, keyEventCandidates, detailsOpen) : ""}
    ${
      rows.length
        ? `<div class="mt-scroll">
            <table class="mt-table" id="mt-table">
              <thead>${renderHeaderRow(columns)}</thead>
              <tbody>${rows.map((row) => renderRow(row, columns)).join("")}</tbody>
            </table>
          </div>`
        : `<div class="state-panel"><p>No data collected yet — press Refresh to pull the last ${coverage?.requested_months ?? 24} months from Reporting Ninja.</p></div>`
    }
    ${table?.collected_at ? `<p class="mt-collected-at">${icon("clock", "icon icon--tiny")}Last collected ${formatTimestamp(table.collected_at)}</p>` : ""}`;

  container.querySelector("#mt-refresh")?.addEventListener("click", () => onRefresh?.());
  container.querySelector("#mt-plain-colors")?.addEventListener("change", (event) => {
    container.querySelector("#mt-table")?.classList.toggle("mt-table--plain-colors", event.target.checked);
  });

  const details = container.querySelector(".mt-settings");
  let candidatesRequested = keyEventCandidates !== undefined;
  details?.addEventListener("toggle", () => {
    if (details.open && !candidatesRequested) {
      candidatesRequested = true;
      onLoadKeyEvents?.();
    }
  });
  container.querySelector("#mt-discover-events")?.addEventListener("click", () => onLoadKeyEvents?.());
  container.querySelector("#mt-settings-form")?.addEventListener("submit", (event) => {
    event.preventDefault();
    const form = event.target;
    const names = form.key_event_names.value
      .split(",")
      .map((name) => name.trim())
      .filter(Boolean);
    onSaveSettings?.({
      attribution_users: form.attribution_users.value,
      attribution_events: form.attribution_events.value,
      key_event_label: form.key_event_label.value.trim() || null,
      key_event_names: names,
    });
  });
}

export function renderMonthlyTableSettingsNote(container, message, { isError = false } = {}) {
  const note = container.querySelector("#mt-settings-note");
  if (!note) return;
  note.textContent = message;
  note.classList.toggle("mt-settings__note--error", isError);
}

export function renderMonthlyTableError(container, message, { onRetry } = {}) {
  container.innerHTML = `
    <div class="section-heading"><div><p class="eyebrow">Monthly Performance</p><h2>Month-over-month table</h2></div></div>
    <div class="state-panel state-failed">
      ${icon("wifi-off")}
      <p><strong>Could not load the monthly table.</strong><br>${escapeHtml(message)}</p>
      ${onRetry ? `<button type="button" class="button button--quiet" id="mt-retry">${icon("refresh")}Try again</button>` : ""}
    </div>`;
  container.querySelector("#mt-retry")?.addEventListener("click", () => onRetry?.());
}
