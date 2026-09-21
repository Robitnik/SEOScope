/* Indexing Planner (task-100): which pages to carry to Search Console by hand
 * today, why, and whether it actually helps.
 *
 * The tool submits nothing. There is no API to request indexing — Search
 * Console exposes no such method and the Indexing API is JobPosting/
 * BroadcastEvent only — so the operator clicks "Request indexing" in GSC and
 * marks the row here. That makes `Mark sent` the most dangerous control on
 * the page: it arms a 90-day cooldown, which is why Undo stays available for
 * 24 hours and `Quota blocked` is a separate button that deliberately does
 * NOT arm it.
 *
 * Three data rules this module must not break:
 * - an empty day is "queue exhausted", not an error and not a zero. Measured
 *   live: seven of ten clients have a backlog of three URLs or fewer.
 * - `is_indexed: null` means "not inspected yet", never "not indexed".
 * - `Crawled - currently not indexed` gets "Review content, then submit",
 *   not "Submit": Google already looked and passed.
 */
import {
  getIndexingOverview,
  getIndexingToday,
  indexingExportUrl,
  markIndexingQuotaBlocked,
  markIndexingSent,
  startIndexingScan,
  undoIndexingSubmission,
} from "./api.js?v=20260921-t100";
import { buildToolsHash } from "./router.js?v=20260921-t100";
import { escapeHtml, formatTimestamp, icon } from "./formatters.js?v=20260921-t100";
import { panelSkeleton } from "./skeleton.js?v=20260921-t100";

let root = null;
let activeTab = "today";
let overview = null;
let today = null;
let busy = new Set();

const TABS = [
  { key: "today", label: "Today" },
  { key: "calendar", label: "Calendar" },
  { key: "backlog", label: "Backlog" },
  { key: "analytics", label: "Analytics" },
];

// Той самий текст, що в `app/schemas/indexing.py` — назва причини не
// перекладається і не вигадується на фронті.
const REASON_LABELS = {
  dropped_out: "Dropped out of the index",
  never_indexed: "Google doesn't know this page",
  discovered_not_indexed: "Discovered, not crawled yet",
  crawled_not_indexed: "Crawled and not taken",
  new_page: "New page",
  stale_crawl: "Changed since Google's last crawl",
  traffic_lost: "Traffic dropped",
  no_traffic: "No impressions",
  periodic_refresh: "Periodic refresh",
};

const EXCLUDED_LABELS = {
  noindex: "noindex",
  robots_blocked: "Blocked by robots.txt",
  non_canonical: "Google picked another canonical",
  redirect: "Redirects elsewhere",
  http_error: "HTTP error",
  manual: "Excluded by hand",
};

export function reasonLabel(code) {
  return REASON_LABELS[code] || code || "—";
}

export function excludedLabel(code) {
  return EXCLUDED_LABELS[code] || code || "—";
}

/* Одне речення з доказів. Текст не зберігається в БД — там лежать самі
 * числа, тож формулювання можна виправити, не переписуючи історію. */
export function evidenceSentence(reasonCode, evidence) {
  const data = evidence || {};
  switch (reasonCode) {
    case "traffic_lost":
      return `${data.clicks_previous ?? "—"} → ${data.clicks_recent ?? "—"} clicks over the last 90 days`;
    case "no_traffic":
      return `No impressions in ${data.window_days ?? 90} days`;
    case "stale_crawl":
      return `Changed ${shortDate(data.changed_at)}, last crawled ${shortDate(data.last_crawl_time)}`;
    case "crawled_not_indexed":
      return `Last crawled ${shortDate(data.last_crawl_time)} — Google looked and passed`;
    case "dropped_out":
      return `Left the index ${shortDate(data.left_index_at)}`;
    case "new_page":
      return `First seen ${shortDate(data.first_seen_at)}`;
    default:
      return data.coverage_state ? String(data.coverage_state) : "";
  }
}

function shortDate(value) {
  if (!value) return "—";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return "—";
  return parsed.toISOString().slice(0, 10);
}

function ratio(value) {
  // `null` — це «ще не сканували», а не 0%.
  if (value === null || value === undefined) return "—";
  return `${(Number(value) * 100).toFixed(1)}%`;
}

function count(value) {
  return value === null || value === undefined ? "—" : String(value);
}

export async function initTool(mount) {
  root = mount;
  activeTab = readTabFromHash();
  render();
  await loadActiveTab();
}

function readTabFromHash() {
  const match = window.location.hash.match(/#\/tools\/indexing\/([a-z]+)/);
  const key = match ? match[1] : "today";
  return TABS.some((tab) => tab.key === key) ? key : "today";
}

function render() {
  root.innerHTML = `
    <section class="indexing-page">
      <a class="back-link" href="${buildToolsHash()}">${icon("arrow-left")}<span>All tools</span></a>
      <header class="indexing-head">
        <p class="eyebrow">Operations / Indexing</p>
        <h1>Indexing Planner</h1>
        <p class="indexing-lead">Google has no API to request indexing — this plans what to submit
          by hand, keeps the reason, and measures whether it helps.</p>
      </header>
      <div class="indexing-summary" id="indexing-summary"></div>
      <nav class="indexing-tabs" role="tablist" aria-label="Indexing planner views">
        ${TABS.map(
          (tab) => `
          <button type="button" role="tab" class="indexing-tab${tab.key === activeTab ? " is-active" : ""}"
            aria-selected="${tab.key === activeTab}" data-tab="${tab.key}">${escapeHtml(tab.label)}</button>`,
        ).join("")}
        <span class="indexing-tabs__spacer"></span>
        <button type="button" class="button button--ghost" id="indexing-scan">${icon("refresh")}<span>Scan now</span></button>
        <a class="button button--ghost" id="indexing-export" href="${indexingExportUrl()}">${icon("file-spreadsheet")}<span>Excel</span></a>
      </nav>
      <div class="indexing-body" id="indexing-body" role="tabpanel"></div>
    </section>`;

  root.querySelectorAll("[data-tab]").forEach((button) => {
    button.addEventListener("click", () => {
      activeTab = button.dataset.tab;
      window.location.hash = `#/tools/indexing/${activeTab}`;
      render();
      void loadActiveTab();
    });
  });
  root.querySelector("#indexing-scan").addEventListener("click", onScan);
  renderSummary();
}

function renderSummary() {
  const host = root.querySelector("#indexing-summary");
  if (!host) return;
  if (!overview) {
    host.innerHTML = panelSkeleton ? panelSkeleton(1) : "";
    return;
  }
  const cycle =
    overview.cycle_holds === null || overview.cycle_holds === undefined
      ? "—"
      : overview.cycle_holds
        ? "holds"
        : "at risk";
  host.innerHTML = `
    <dl class="indexing-stats">
      ${stat("Index coverage", ratio(overview.coverage_ratio), `${count(overview.indexed)} of ${count(overview.urls_total)} pages`)}
      ${stat("In the queue", count(overview.candidates), overview.days_to_clear_queue ? `${overview.days_to_clear_queue} working days at the current pace` : "Nothing waiting")}
      ${stat("Today", `${overview.sent_today}/${overview.planned_today}`, "submitted / planned")}
      ${stat("Excluded", count(overview.excluded), "Google won't index these")}
      ${stat("90-day cycle", cycle, `${overview.daily_capacity} slots a day`)}
    </dl>`;
}

function stat(label, value, hint) {
  return `
    <div class="indexing-stat">
      <dt>${escapeHtml(label)}</dt>
      <dd>${escapeHtml(String(value))}</dd>
      <p class="indexing-stat__hint">${escapeHtml(hint)}</p>
    </div>`;
}

async function loadActiveTab() {
  const body = root.querySelector("#indexing-body");
  body.innerHTML = panelSkeleton ? panelSkeleton(3) : "";
  try {
    if (!overview) {
      overview = await getIndexingOverview();
      renderSummary();
    }
    if (activeTab === "today") {
      today = await getIndexingToday();
      renderToday();
      return;
    }
    if (activeTab === "calendar") {
      const module = await import("./indexing-calendar.js?v=20260921-t100");
      await module.renderCalendar(body);
      return;
    }
    if (activeTab === "backlog") {
      const module = await import("./indexing-backlog.js?v=20260921-t100");
      await module.renderBacklog(body);
      return;
    }
    const module = await import("./indexing-analytics.js?v=20260921-t100");
    await module.renderAnalytics(body);
  } catch (error) {
    renderError(body, error);
  }
}

function renderError(host, error) {
  host.innerHTML = `
    <div class="state-panel state-failed">
      ${icon("wifi-off")}
      <p><strong>Could not load the planner.</strong><br>${escapeHtml(error.message || String(error))}</p>
      <button class="button button--primary" type="button" id="indexing-retry">Try again</button>
    </div>`;
  host.querySelector("#indexing-retry").addEventListener("click", () => void loadActiveTab());
}

function renderToday() {
  const body = root.querySelector("#indexing-body");
  if (!today || !today.groups.length) {
    body.innerHTML = `
      <div class="state-panel">
        ${icon("calendar-check")}
        <p><strong>Nothing planned for today.</strong><br>
          Either no client has the planner switched on yet, or every queue is empty.</p>
        <a class="button button--primary" href="#/tools/indexing/backlog">Open the backlog</a>
      </div>`;
    return;
  }
  body.innerHTML = today.groups.map(groupBlock).join("");
  bindTodayActions();
}

function groupBlock(group) {
  const rows = group.submissions.map(submissionRow).join("");
  const exhausted = group.queue_exhausted
    ? `<p class="indexing-group__note">${icon("check")} Queue exhausted — nothing else is waiting for this client.</p>`
    : "";
  const urls = group.submissions.map((item) => item.url).join("\n");
  return `
    <section class="indexing-group" data-client="${escapeHtml(group.client_id)}">
      <header class="indexing-group__head">
        <h2>${escapeHtml(group.client_name)}</h2>
        <p class="indexing-group__progress">${group.sent} of ${group.submissions.length} sent
          <span class="indexing-group__quota">· quota ${group.daily_quota}/day</span></p>
        <button type="button" class="button button--ghost indexing-copy-all"
          data-urls="${escapeHtml(urls)}">${icon("copy")}<span>Copy all</span></button>
      </header>
      ${exhausted}
      <table class="indexing-table">
        <thead>
          <tr>
            <th scope="col">Page</th>
            <th scope="col">Why</th>
            <th scope="col">Status</th>
            <th scope="col" class="indexing-table__actions">Actions</th>
          </tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>
    </section>`;
}

function submissionRow(item) {
  const evidence = evidenceSentence(item.reason_code, item.reason);
  const actionHint =
    item.action === "review_then_submit"
      ? `<span class="indexing-action-hint">${icon("alert-triangle")} Review content, then submit</span>`
      : "";
  const status = statusCell(item);
  const gsc = item.gsc_link
    ? `<a class="button button--ghost" href="${escapeHtml(item.gsc_link)}" target="_blank" rel="noopener">${icon("external-link")}<span>GSC</span></a>`
    : `<button class="button button--ghost" type="button" disabled title="Not inspected yet — no Search Console link">${icon("external-link")}<span>GSC</span></button>`;
  return `
    <tr data-submission="${escapeHtml(item.id)}">
      <td class="indexing-cell-url">
        <a href="${escapeHtml(item.url)}" target="_blank" rel="noopener">${escapeHtml(item.url)}</a>
        ${item.title ? `<p class="indexing-cell-title">${escapeHtml(item.title)}</p>` : ""}
      </td>
      <td>
        <span class="indexing-reason indexing-reason--${escapeHtml(item.reason_code)}">${escapeHtml(reasonLabel(item.reason_code))}</span>
        ${evidence ? `<p class="indexing-evidence">${escapeHtml(evidence)}</p>` : ""}
        ${actionHint}
      </td>
      <td>${status}</td>
      <td class="indexing-table__actions">
        <button class="button button--ghost indexing-copy" type="button" data-url="${escapeHtml(item.url)}">${icon("copy")}<span>Copy</span></button>
        ${gsc}
        ${actionButtons(item)}
      </td>
    </tr>`;
}

function statusCell(item) {
  if (item.status === "sent") {
    return `<span class="indexing-status indexing-status--sent">${icon("check")} Sent ${escapeHtml(formatTimestamp(item.submitted_at) || "")}</span>`;
  }
  if (item.status === "quota_blocked") {
    return `<span class="indexing-status indexing-status--blocked">${icon("alert-circle")} Quota blocked</span>`;
  }
  return `<span class="indexing-status indexing-status--planned">Planned</span>`;
}

function actionButtons(item) {
  if (item.status === "sent") {
    return item.undoable
      ? `<button class="button button--ghost indexing-undo" type="button" data-id="${escapeHtml(item.id)}">${icon("arrow-back-up")}<span>Undo</span></button>`
      : "";
  }
  return `
    <button class="button button--primary indexing-sent" type="button" data-id="${escapeHtml(item.id)}">${icon("check")}<span>Mark sent</span></button>
    <button class="button button--ghost indexing-blocked" type="button" data-id="${escapeHtml(item.id)}"
      title="GSC refused over its daily quota — this does not start the 90-day cooldown">${icon("ban")}<span>Quota blocked</span></button>`;
}

function bindTodayActions() {
  root.querySelectorAll(".indexing-copy").forEach((button) => {
    button.addEventListener("click", () => void copyText(button.dataset.url, button));
  });
  root.querySelectorAll(".indexing-copy-all").forEach((button) => {
    button.addEventListener("click", () => void copyText(button.dataset.urls, button));
  });
  root.querySelectorAll(".indexing-sent").forEach((button) => {
    button.addEventListener("click", () => void transition(button, markIndexingSent));
  });
  root.querySelectorAll(".indexing-blocked").forEach((button) => {
    button.addEventListener("click", () => void transition(button, markIndexingQuotaBlocked));
  });
  root.querySelectorAll(".indexing-undo").forEach((button) => {
    button.addEventListener("click", () => void transition(button, undoIndexingSubmission));
  });
}

async function copyText(text, button) {
  if (!text) return;
  try {
    await navigator.clipboard.writeText(text);
    flash(button, "Copied");
  } catch {
    flash(button, "Copy failed");
  }
}

function flash(button, message) {
  const label = button.querySelector("span");
  if (!label) return;
  const original = label.textContent;
  label.textContent = message;
  window.setTimeout(() => {
    label.textContent = original;
  }, 1500);
}

async function transition(button, action) {
  const id = button.dataset.id;
  // Подвійний клік по `Mark sent` не має ставати другою відправкою: бек
  // ідемпотентний, але блокування тут прибирає і миготіння, і гонку.
  if (!id || busy.has(id)) return;
  busy.add(id);
  button.disabled = true;
  try {
    await action(id);
    today = await getIndexingToday();
    overview = await getIndexingOverview();
    renderSummary();
    renderToday();
  } catch (error) {
    button.disabled = false;
    window.alert(error.message || String(error));
  } finally {
    busy.delete(id);
  }
}

async function onScan(event) {
  const button = event.currentTarget;
  button.disabled = true;
  const label = button.querySelector("span");
  const original = label.textContent;
  label.textContent = "Scanning…";
  try {
    await startIndexingScan({});
    overview = await getIndexingOverview();
    renderSummary();
    await loadActiveTab();
  } catch (error) {
    window.alert(error.message || String(error));
  } finally {
    button.disabled = false;
    label.textContent = original;
  }
}
