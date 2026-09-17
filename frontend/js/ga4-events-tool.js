/* GA4 Events by Month (task-98): pick a client and the months you care about,
 * get every GA4 event with its numbers — one tab per month, one Excel sheet
 * per month.
 *
 * Two data rules this module must not break (both measured live, plan §1.6):
 * - the Users column is NEVER summed. The property total comes from the API's
 *   own `totals`, computed by a separate GA4 query; summing the column
 *   overstated users by 3.9x on real data.
 * - a month with no data, a failed month and a partial month are three
 *   different states with three different messages — none of them is "0".
 */
import {
  getGa4EventsClients,
  getGa4EventsMonths,
  getGa4EventsReport,
  getGa4EventsReports,
  ga4EventsExportUrl,
  runGa4EventsReport,
} from "./api.js?v=20260917-t98";
import { buildToolsHash } from "./router.js?v=20260917-t98";
import { escapeHtml, formatTimestamp, icon } from "./formatters.js?v=20260917-t98";
import { panelSkeleton } from "./skeleton.js?v=20260917-t98";

const MAX_MONTHS = 24;

// Ті самі три порізи, що в GA4-таблиці оператора, у тому самому порядку.
// Визначення (`sessionDefaultChannelGroup` CONTAINS) живе на беку — фронт лише
// показує те, що прийшло, і ніколи не рахує часток сам.
const SEGMENTS = [
  { key: "all", label: "All Users" },
  { key: "direct", label: "Direct traffic" },
  { key: "organic", label: "Organic traffic" },
];

let root = null;
let clients = [];
let selectedClientId = null;
let availableMonths = [];
let selectedMonths = new Set();
let report = null;
let activeMonth = null;
let searchTerm = "";
let keyEventsOnly = false;

const integers = new Intl.NumberFormat("en-US");
const decimals = new Intl.NumberFormat("en-US", {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});
const ratios = new Intl.NumberFormat("en-US", {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

function num(value) {
  // `null` is not zero (AGENTS.md §5): an em dash says "GA4 returned nothing
  // for this cell", which is a different fact from a measured zero.
  return value === null || value === undefined ? "—" : integers.format(value);
}

function monthLabel(month) {
  const [year, index] = month.split("-");
  const date = new Date(Date.UTC(Number(year), Number(index) - 1, 1));
  return new Intl.DateTimeFormat("en-US", {
    month: "short",
    year: "numeric",
    timeZone: "UTC",
  }).format(date);
}

export async function initTool(mount, route) {
  root = mount;
  if (route.reportId) {
    await renderReportView(route.reportId);
    return;
  }
  await renderFormView();
}

function shell() {
  return `
    <a class="back-link" href="${buildToolsHash()}">${icon("arrow-left")}<span>All tools</span></a>
    <header class="tool-header">
      <p class="eyebrow">Tools / GA4 Events by Month</p>
      <h1>GA4 events by month</h1>
      <p class="tool-lead">Pick a client and the months you need. Every GA4 event comes back in
        three comparisons — All Users, Direct traffic and Organic traffic — with its count, share,
        users and events per user. One tab per month, and an Excel file with one sheet per month.</p>
    </header>
    <section class="tool-panel" aria-labelledby="ge-form-title">
      <div class="section-heading"><div><h2 id="ge-form-title">Build a report</h2>
        <p class="tool-panel__hint">Read straight from the Google Analytics Data API — no
          Reporting Ninja quota is spent.</p></div></div>
      <div class="tool-panel__body" id="ge-form-body"></div>
    </section>
    <section class="tool-panel" id="ge-result-panel" hidden aria-labelledby="ge-result-title">
      <div class="section-heading">
        <div><h2 id="ge-result-title">Report</h2><p class="tool-panel__hint" id="ge-result-hint"></p></div>
        <div id="ge-result-actions"></div>
      </div>
      <div class="tool-panel__body" id="ge-result-body"></div>
    </section>
    <section class="tool-panel" aria-labelledby="ge-recent-title">
      <div class="section-heading"><div><h2 id="ge-recent-title">Recent reports</h2></div></div>
      <div class="tool-panel__body" id="ge-recent-body">
        ${panelSkeleton({ label: "reports", rows: 3, columns: 4 })}
      </div>
    </section>`;
}

async function renderFormView() {
  root.innerHTML = shell();
  await loadClients();
  await loadRecent();
}

async function loadClients() {
  const body = root.querySelector("#ge-form-body");
  body.innerHTML = panelSkeleton({ label: "clients", rows: 2, columns: 2 });
  try {
    const payload = await getGa4EventsClients();
    clients = payload.clients || [];
  } catch (error) {
    body.innerHTML = failurePanel("Could not load the client list.", error.message, "ge-retry-clients");
    body.querySelector("#ge-retry-clients").addEventListener("click", () => void loadClients());
    return;
  }
  renderForm();
}

function failurePanel(title, detail, retryId) {
  return `
    <div class="state-panel state-failed">
      ${icon("wifi-off")}
      <p><strong>${escapeHtml(title)}</strong><br>${escapeHtml(detail || "")}</p>
      <button class="button button--primary" type="button" id="${retryId}">Try again</button>
    </div>`;
}

function renderForm() {
  const body = root.querySelector("#ge-form-body");
  const options = clients
    .map(
      (item) =>
        `<option value="${escapeHtml(item.id)}"${item.id === selectedClientId ? " selected" : ""}${
          item.configured ? "" : " disabled"
        }>${escapeHtml(item.name)}${item.configured ? "" : " — GA4 not connected"}</option>`,
    )
    .join("");
  body.innerHTML = `
    <form id="ge-form" class="ge-form" novalidate>
      <label class="form-field" for="ge-client">Client
        <select id="ge-client" name="client">
          <option value="">Choose a client…</option>
          ${options}
        </select>
      </label>
      <div class="ge-months" id="ge-months" aria-live="polite"></div>
      <div class="ge-submit-row">
        <button class="button button--primary" type="submit" id="ge-submit" disabled>
          ${icon("table")}Build the report
        </button>
        <p class="ge-submit-hint" id="ge-submit-hint">Pick a client first.</p>
      </div>
    </form>`;
  body.querySelector("#ge-client").addEventListener("change", (event) => {
    void selectClient(event.target.value);
  });
  body.querySelector("#ge-form").addEventListener("submit", (event) => {
    event.preventDefault();
    void submit();
  });
  renderMonths();
}

async function selectClient(clientId) {
  selectedClientId = clientId || null;
  selectedMonths = new Set();
  availableMonths = [];
  renderMonths();
  if (!selectedClientId) return;
  const holder = root.querySelector("#ge-months");
  holder.innerHTML = `<p class="ge-months-loading">${icon("loader")}<span>Reading which months GA4 has…</span></p>`;
  try {
    const payload = await getGa4EventsMonths(selectedClientId);
    availableMonths = payload.months || [];
  } catch (error) {
    holder.innerHTML = `
      <div class="state-panel state-not_configured">
        ${icon("plug-connected-x")}
        <p><strong>No months to show.</strong><br>${escapeHtml(error.message)}</p>
      </div>`;
    updateSubmit();
    return;
  }
  // Найсвіжіший повний місяць — розумний перший вибір, але не мовчазний:
  // оператор бачить його відміченим і може зняти.
  const preset = availableMonths.slice(0, 3);
  preset.forEach((month) => selectedMonths.add(month));
  renderMonths();
}

function renderMonths() {
  const holder = root.querySelector("#ge-months");
  if (!holder) return;
  if (!selectedClientId) {
    holder.innerHTML = `<p class="ge-months-empty">Choose a client to see which months GA4 has data for.</p>`;
    updateSubmit();
    return;
  }
  if (!availableMonths.length) {
    holder.innerHTML = `<p class="ge-months-empty">GA4 returned no months with events for this client.</p>`;
    updateSubmit();
    return;
  }
  const byYear = new Map();
  availableMonths.forEach((month) => {
    const year = month.slice(0, 4);
    if (!byYear.has(year)) byYear.set(year, []);
    byYear.get(year).push(month);
  });
  const years = [...byYear.entries()]
    .map(
      ([year, months]) => `
      <div class="ge-year">
        <p class="ge-year__label" translate="no">${escapeHtml(year)}</p>
        <div class="ge-year__months">
          ${months
            .map(
              (month) => `
            <label class="ge-month">
              <input type="checkbox" value="${escapeHtml(month)}"${
                selectedMonths.has(month) ? " checked" : ""
              }>
              <span>${escapeHtml(monthLabel(month))}</span>
            </label>`,
            )
            .join("")}
        </div>
      </div>`,
    )
    .join("");
  holder.innerHTML = `
    <fieldset class="ge-fieldset">
      <legend>Months<span class="ge-count" id="ge-month-count"></span></legend>
      <div class="ge-year-list">${years}</div>
    </fieldset>`;
  holder.querySelectorAll(".ge-month input").forEach((input) => {
    input.addEventListener("change", () => {
      if (input.checked) {
        if (selectedMonths.size >= MAX_MONTHS) {
          input.checked = false;
          return;
        }
        selectedMonths.add(input.value);
      } else {
        selectedMonths.delete(input.value);
      }
      updateSubmit();
    });
  });
  updateSubmit();
}

function updateSubmit() {
  const button = root.querySelector("#ge-submit");
  const hint = root.querySelector("#ge-submit-hint");
  const counter = root.querySelector("#ge-month-count");
  if (!button || !hint) return;
  const count = selectedMonths.size;
  if (counter) counter.textContent = count ? ` · ${count} of ${MAX_MONTHS} selected` : "";
  button.disabled = !selectedClientId || count === 0;
  if (!selectedClientId) hint.textContent = "Pick a client first.";
  else if (!count) hint.textContent = "Pick at least one month.";
  else hint.textContent = `${count} ${count === 1 ? "month" : "months"} — ${count} ${
    count === 1 ? "tab" : "tabs"
  } and ${count} Excel ${count === 1 ? "sheet" : "sheets"}.`;
}

async function submit() {
  const button = root.querySelector("#ge-submit");
  const hint = root.querySelector("#ge-submit-hint");
  button.disabled = true;
  button.innerHTML = `${icon("loader")}Building the report…`;
  hint.textContent = "Querying GA4, one request per month.";
  try {
    report = await runGa4EventsReport(selectedClientId, [...selectedMonths].sort().reverse());
  } catch (error) {
    button.disabled = false;
    button.innerHTML = `${icon("table")}Build the report`;
    hint.textContent = "";
    const holder = root.querySelector("#ge-result-panel");
    holder.hidden = false;
    root.querySelector("#ge-result-body").innerHTML = `
      <div class="state-panel state-not_configured">
        ${icon("plug-connected-x")}
        <p><strong>No report.</strong><br>${escapeHtml(error.message)}</p>
      </div>`;
    return;
  }
  button.disabled = false;
  button.innerHTML = `${icon("table")}Build the report`;
  updateSubmit();
  activeMonth = report.months_data?.[0]?.month || null;
  renderReport();
  await loadRecent();
}

async function renderReportView(reportId) {
  root.innerHTML = shell();
  root.querySelector("#ge-result-panel").hidden = false;
  root.querySelector("#ge-result-body").innerHTML = panelSkeleton({
    label: "report",
    rows: 6,
    columns: 6,
  });
  try {
    report = await getGa4EventsReport(reportId);
  } catch (error) {
    root.querySelector("#ge-result-body").innerHTML = `
      <div class="state-panel state-failed">
        ${icon("alert-triangle")}
        <p><strong>Could not open this report.</strong><br>${escapeHtml(error.message)}</p>
        <a class="button button--primary" href="#/tools/ga4-events">Build a new one</a>
      </div>`;
    return;
  }
  activeMonth = report.months_data?.[0]?.month || null;
  await loadClients();
  renderReport();
  await loadRecent();
}

function renderReport() {
  const panel = root.querySelector("#ge-result-panel");
  const hint = root.querySelector("#ge-result-hint");
  const actions = root.querySelector("#ge-result-actions");
  const body = root.querySelector("#ge-result-body");
  panel.hidden = false;
  hint.textContent = `${report.client_name || "Client"} · ${report.property_id} · generated ${formatTimestamp(
    report.created_at,
  )}`;
  actions.innerHTML = `
    <a class="button" href="${ga4EventsExportUrl(report.id)}" download>${icon("file-spreadsheet")}Export .xlsx</a>
    <a class="button button--ghost" href="#/tools/ga4-events/${encodeURIComponent(report.id)}">${icon(
      "link",
    )}Shareable link</a>`;

  const months = report.months_data || [];
  if (!months.length) {
    body.innerHTML = `<p class="ge-months-empty">This report has no months.</p>`;
    return;
  }
  body.innerHTML = `
    <div class="ge-tabs" role="tablist" aria-label="Months in this report">
      ${months
        .map(
          (month) => `
        <button type="button" role="tab" class="ge-tab" id="ge-tab-${escapeHtml(month.month)}"
          aria-controls="ge-panel-${escapeHtml(month.month)}"
          aria-selected="${month.month === activeMonth}"
          tabindex="${month.month === activeMonth ? "0" : "-1"}"
          data-month="${escapeHtml(month.month)}">
          <span class="ge-tab__label">${escapeHtml(month.label)}</span>
          ${month.is_partial ? `<span class="ge-chip ge-chip--partial">Partial</span>` : ""}
          ${month.state === "failed" ? `<span class="ge-chip ge-chip--failed">Failed</span>` : ""}
          ${month.state === "no_data" ? `<span class="ge-chip ge-chip--empty">No data</span>` : ""}
        </button>`,
        )
        .join("")}
    </div>
    <div class="ge-filters" id="ge-filters"></div>
    <div id="ge-panels"></div>`;

  const tabs = [...body.querySelectorAll(".ge-tab")];
  tabs.forEach((tab) => {
    tab.addEventListener("click", () => {
      activeMonth = tab.dataset.month;
      renderReport();
    });
    tab.addEventListener("keydown", (event) => {
      if (event.key !== "ArrowRight" && event.key !== "ArrowLeft") return;
      event.preventDefault();
      const index = tabs.indexOf(tab);
      const next = tabs[(index + (event.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length];
      activeMonth = next.dataset.month;
      renderReport();
      root.querySelector(`#ge-tab-${CSS.escape(activeMonth)}`)?.focus();
    });
  });
  renderFilters();
  renderMonthPanel();
}

function renderFilters() {
  const month = currentMonth();
  const holder = root.querySelector("#ge-filters");
  if (!holder || !month || month.state !== "ok") {
    if (holder) holder.innerHTML = "";
    return;
  }
  holder.innerHTML = `
    <label class="search-field ge-search">
      <span class="visually-hidden">Search events</span>
      ${icon("search")}
      <input id="ge-search" type="search" placeholder="Event name" autocomplete="off"
        value="${escapeHtml(searchTerm)}">
    </label>
    <label class="ge-toggle">
      <input type="checkbox" id="ge-key-only"${keyEventsOnly ? " checked" : ""}>
      <span>Key events only</span>
    </label>`;
  holder.querySelector("#ge-search").addEventListener("input", (event) => {
    searchTerm = event.target.value;
    renderMonthPanel();
  });
  holder.querySelector("#ge-key-only").addEventListener("change", (event) => {
    keyEventsOnly = event.target.checked;
    renderMonthPanel();
  });
}

function currentMonth() {
  return (report.months_data || []).find((month) => month.month === activeMonth) || null;
}

function renderMonthPanel() {
  const month = currentMonth();
  const holder = root.querySelector("#ge-panels");
  if (!holder || !month) return;
  const attrs = `role="tabpanel" id="ge-panel-${escapeHtml(month.month)}" aria-labelledby="ge-tab-${escapeHtml(
    month.month,
  )}" tabindex="0"`;

  if (month.state === "failed") {
    holder.innerHTML = `
      <div ${attrs}>
        <div class="state-panel state-failed">
          ${icon("alert-triangle")}
          <p><strong>${escapeHtml(month.label)} could not be read.</strong><br>${escapeHtml(
            month.error || "GA4 did not answer.",
          )}</p>
          <p class="ge-note">The other months in this report are unaffected — switch tabs to see them.</p>
        </div>
      </div>`;
    return;
  }
  if (month.state === "no_data") {
    holder.innerHTML = `
      <div ${attrs}>
        <div class="state-panel state-not_configured">
          ${icon("database-off")}
          <p><strong>No events in ${escapeHtml(month.label)}.</strong><br>
            GA4 returned no rows for this property and month — that is an empty month, not a zero.</p>
        </div>
      </div>`;
    return;
  }

  const needle = searchTerm.trim().toLocaleLowerCase("en-US");
  const rows = (month.rows || []).filter(
    (row) =>
      (!needle || row.event_name.toLocaleLowerCase("en-US").includes(needle)) &&
      (!keyEventsOnly || row.is_key_event),
  );
  const totals = month.totals || {};
  holder.innerHTML = `
    <div ${attrs}>
      ${month.is_partial
        ? `<p class="ge-partial-note">${icon(
            "alert-triangle",
          )}<span>${escapeHtml(month.label)} is still settling — GA4 keeps revising these numbers for about 48 hours, and the month is not over yet.</span></p>`
        : ""}
      <div class="ge-totals">
        ${SEGMENTS.map((segment) => segmentTotalTile(segment, totals[segment.key])).join("")}
      </div>
      <p class="ge-note">Each comparison's totals come from its own GA4 query. Total Users is never
        the sum of the column below — one user triggers many events — and every percentage is a
        share of that comparison's own totals.</p>
      <div class="table-scroll ge-table-wrap">
        <table class="analytics-table ge-table">
          <caption class="visually-hidden">GA4 events for ${escapeHtml(month.label)}, by comparison</caption>
          <thead>
            <tr>
              <th scope="col">Event name</th>
              <th scope="col">Comparison</th>
              <th scope="col" class="num">Event count</th>
              <th scope="col" class="num">Total users</th>
              <th scope="col" class="num">Event count per user</th>
            </tr>
          </thead>
          <tbody>${rows.map(eventGroup).join("") || emptyRow()}</tbody>
        </table>
      </div>
    </div>`;
}

function segmentTotalTile(segment, totals) {
  return `
    <div class="ge-total">
      <p class="ge-total__label">${escapeHtml(segment.label)}</p>
      <p class="ge-total__value" translate="no">${num(totals?.event_count)}</p>
      <p class="ge-total__sub">${num(totals?.users)} users</p>
    </div>`;
}

function emptyRow() {
  return `<tr><td colspan="5" class="ge-empty-cell">No event matches this filter.</td></tr>`;
}

/* One event = three rows (All Users / Direct traffic / Organic traffic), the
 * shape of the operator's own GA4 table. The event name is written once, on
 * the first row of the group, and spans all three — repeating it three times
 * would read as three different events. */
function eventGroup(row) {
  return SEGMENTS.map((segment, index) => {
    const values = row.segments?.[segment.key];
    const nameCell =
      index === 0
        ? `<th scope="rowgroup" rowspan="${SEGMENTS.length}" class="ge-name-cell">
             <span class="ge-event" translate="no">${escapeHtml(row.event_name)}</span>
             ${row.is_key_event ? `<span class="ge-chip ge-chip--key">${icon("target", "icon icon--tiny")}Key</span>` : ""}
           </th>`
        : "";
    return `
      <tr class="${row.is_key_event ? "ge-row--key" : ""} ${index === 0 ? "ge-row--group-start" : ""}">
        ${nameCell}
        <td class="ge-comparison">${escapeHtml(segment.label)}</td>
        <td class="num">${withShare(values?.event_count, values?.event_share_pct)}</td>
        <td class="num">${withShare(values?.users, values?.user_share_pct)}</td>
        <td class="num">${ratio(values?.per_user)}</td>
      </tr>`;
  }).join("");
}

function withShare(value, share) {
  if (value === null || value === undefined) return "—";
  const percent =
    share === null || share === undefined
      ? ""
      : ` <span class="ge-share">(${decimals.format(Number(share))}%)</span>`;
  return `${integers.format(value)}${percent}`;
}

function ratio(value) {
  return value === null || value === undefined ? "—" : ratios.format(Number(value));
}

async function loadRecent() {
  const body = root.querySelector("#ge-recent-body");
  if (!body) return;
  let payload;
  try {
    payload = await getGa4EventsReports();
  } catch (error) {
    body.innerHTML = failurePanel("Could not load recent reports.", error.message, "ge-retry-recent");
    body.querySelector("#ge-retry-recent").addEventListener("click", () => void loadRecent());
    return;
  }
  const reports = payload.reports || [];
  if (!reports.length) {
    body.innerHTML = `<p class="ge-months-empty">No report has been built yet.</p>`;
    return;
  }
  body.innerHTML = `
    <div class="table-scroll ge-table-wrap">
      <table class="analytics-table">
        <caption class="visually-hidden">Recent GA4 event reports</caption>
        <thead>
          <tr><th scope="col">Client</th><th scope="col">Months</th><th scope="col">State</th>
            <th scope="col">Built</th><th scope="col"><span class="visually-hidden">Open</span></th></tr>
        </thead>
        <tbody>
          ${reports
            .map(
              (item) => `
            <tr>
              <th scope="row" translate="no">${escapeHtml(item.client_name || "Unknown client")}</th>
              <td>${item.months.length} (${escapeHtml(monthLabel(item.months[item.months.length - 1] || item.months[0]))}
                – ${escapeHtml(monthLabel(item.months[0]))})</td>
              <td>${stateChip(item.state)}</td>
              <td>${escapeHtml(formatTimestamp(item.created_at))}</td>
              <td class="ge-open-cell"><a class="link-button" href="#/tools/ga4-events/${encodeURIComponent(
                item.id,
              )}">Open</a></td>
            </tr>`,
            )
            .join("")}
        </tbody>
      </table>
    </div>`;
}

function stateChip(state) {
  if (state === "partial") {
    return `<span class="ge-chip ge-chip--partial">${icon(
      "alert-triangle",
      "icon icon--tiny",
    )}Some months failed</span>`;
  }
  if (state === "failed") {
    return `<span class="ge-chip ge-chip--failed">${icon("circle-x", "icon icon--tiny")}Failed</span>`;
  }
  return `<span class="ge-chip ge-chip--ok">${icon("circle-check", "icon icon--tiny")}Complete</span>`;
}
