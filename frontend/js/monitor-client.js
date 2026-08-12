// Site Monitor — один клієнт (task-36 §3.2): стан + повна історія моніторингу
// одного клієнта. Структура модуля копіює `blogs-page.js` (client-scoped
// top-level сторінка, власні `paramsFromQuery`/`persist()`, `back-link` на
// сторінку клієнта, "клієнта не знайдено").
//
// Усі динамічні секції живуть під одним стабільним контейнером `#mc-content`.
// Делегування подій (click/change/input/submit) навішується ОДИН раз у
// `bindShell()` на цей контейнер — окремі рендер-функції лише перезаписують
// `innerHTML` дочірніх вузлів і ніколи не додають власні слухачі, інакше
// повторний рендер множив би обробники (грабля, якої немає у сусідніх
// `blogs-panel.js`/`audit-panel.js`, бо ті — листові компоненти з одним
// власником DOM-дерева; тут дерево велике й оновлюється по секціях).
//
// Нові CSS-класи, що очікують на `frontend/css/monitor.css` (усе інше —
// вже існуючі `.state-panel`, `.section-heading`, `.analytics-toolbar`,
// `.search-field`, `.table-scroll`, `.analytics-table`, `.pagination`,
// `.button`, `.icon-button`, `.chip-button`, `.live-collection`,
// `.status-label`, `.visually-hidden`, `.skeleton*`):
//   .monitor-site-changes        — обгортка над стрічкою site-scope критичних подій
//   .monitor-actions-section    — обгортка над рядком Check now / paused-gate
//   .monitor-actions            — сам рядок двох кнопок Check now
//   .monitor-next-due           — рядок "Next light/full check: …"
//   .monitor-live-status        — модифікатор на .live-collection під час поллінгу
//   .monitor-paused-gate        — модифікатор на .state-panel для "Monitoring off"
//   .monitor-never-checked      — модифікатор на .state-panel для "Never checked"
//   .monitor-never-checked__body
//   .monitor-blocks             — grid-обгортка навколо Robots/Sitemap/DNS
//   .monitor-block              — один із трьох блоків у сітці
//   .monitor-conclusion         — рядок висновку в блоці Robots ("Googlebot: allowed")
//   .monitor-changed-at         — дрібний рядок "Last checked: …" у блоках
//   .monitor-chip               — модифікатор на .chip-button для нединамічних чіпів (Truncated)
//   .monitor-sitemap-health     — <dl> лічильників ok/redirects/broken/unavailable
//   .monitor-health-link        — кнопка-число з дрилдауном у Pages
//   .monitor-dns-banner         — модифікатор на .state-panel для "DNS could not be checked"
//   .monitor-dns-verify         — рядок GSC/Analytics верифікації під таблицею DNS
//   .monitor-dns-analytics      — модифікатор для рядка Analytics (пояснення без dev-TODO копірайту)
//   .monitor-dns-show-all       — модифікатор на .chip-button для розгортання списку DNS-записів
//   .monitor-changes-load-more  — обгортка "Showing N of M" + Load more під стрічкою змін
//   .monitor-pages-toolbar      — модифікатор на .analytics-toolbar для Pages
//   .monitor-checkbox           — <label> із чекбоксом-фільтром (hit target)
//   .monitor-changed-note       — пояснення про клієнтський фільтр changed_since
//   .monitor-export-actions     — пара посилань Export CSV/XLSX у шапці Pages
//   .monitor-sort-button        — кнопка сортування всередині <th aria-sort>
//   .monitor-row-open           — кнопка-URL, що відкриває історію версій сторінки
//   .monitor-key-badge          — маленька зірка біля key page
//   .monitor-runs-baseline      — модифікатор на .state-panel для baseline-плашки
//   .monitor-run-error          — дрібний текст помилки під рядком журналу
//   .monitor-feed-toggle        — пара chip-button Open/All над стрічкою змін
//   .monitor-changes            — <ul> рядків зміни (спільний для site-scope change і стрічки)
//   .monitor-change             — <li> одного запису
//   .monitor-severity           — іконка рівня (колір іде inline через var(--danger|warning|ink-dim))
//   .monitor-change__body / __subject / __diff / __old / __new / __meta / __headline
//   .monitor-change-status      — "Acknowledged …"/"Resolved …" замість кнопки
//   .monitor-change-status--resolved — модифікатор тону для "Resolved"
//   .monitor-ack-button         — модифікатор на .button (Acknowledge)
//   .monitor-ack-form           — інлайн-форма нотатки під час Acknowledge
//   .monitor-ack-cancel         — модифікатор на .button (Cancel у формі нотатки)
//   .monitor-pages-section / .monitor-runs-section / .monitor-changes-section
//                                — обгортки-секції (тільки якір для layout/spacing)
//   .monitor-dns-table / .monitor-pages-table / .monitor-runs-table
//                                — модифікатори на .analytics-table для колонок-специфік

import {
  getMonitorSummary, getMonitorSettings, patchMonitorSettings, listMonitorRuns, startMonitorRun,
  listClientMonitorChanges, ackMonitorChange, getMonitorPages, checkMonitorPage, getMonitorDnsRecords,
  monitorExportCsvUrl, monitorExportXlsxUrl, getDashboard, isAbortError,
} from "./api.js?v=20260812-monitor7";
import { clientHeaderSkeleton, sectionSkeleton } from "./skeleton.js?v=20260812-monitor7";
import { buildClientHash, replaceMonitorClientHash } from "./router.js?v=20260812-monitor7";
import {
  escapeHtml, icon, initials, formatTimestamp, formatDate, MONITOR_SEVERITY_META, changeTypeLabel,
  STATE_META, stateClass,
} from "./formatters.js?v=20260812-monitor7";
import { createMonitorDiffViewer } from "./monitor-diff.js?v=20260812-monitor7";
import { createMonitorContentViewer } from "./monitor-content.js?v=20260812-monitor7";

const PAGES_PAGE_SIZE = 25;
const CHANGES_PAGE_SIZE = 25;
const RUNS_LIMIT = 50;
const POLL_INTERVAL_MS = 2000;
// Monitor runs never surface `awaiting_auth` (task-35's engine has no
// interactive-login step, unlike the sync jobs `STATE_META` also covers) —
// this is a smaller, run-specific set rather than a reuse of formatters.js's
// `isTerminal`, which encodes sync-job semantics ("partial" exists there,
// never here; "failed_interrupted" exists here, never there).
const RUN_ACTIVE_STATUSES = new Set(["queued", "running"]);
const RUN_TERMINAL_STATUSES = new Set(["succeeded", "failed", "failed_interrupted"]);

// Only columns a click-to-sort header exposes; boolean/text columns (in
// sitemap, indexable, canonical, title) stay plain `<th>` rather than
// guessing at sort keys the backend may not support for them.
const SORT_FIELDS = new Set(["url", "status", "last_checked_at", "version_seq"]);
const SORT_DEFAULT_DIR = { last_checked_at: "desc", version_seq: "desc", url: "asc", status: "asc" };
const DEFAULT_SORT_FIELD = "last_checked_at";

const root = document.querySelector("#monitor-client-page-root");

let current = null;
let requestToken = 0;
let clientRef = null;
let monitorSummary = null;
let monitorSettings = null;
// `null` after a failed fetch would be indistinguishable from "no open
// changes" in every consumer below, so failures leave the previous array (or
// the initial `[]`) in place and are surfaced separately via the site-scope change
// retry panel — this is a display fallback, not a claim that zero changes
// are open.
let openChanges = [];
let openChangesPromise = null;
let contentMode = "loading"; // "loading" | "never_checked" | "full" | "error"
let diffViewer = null;
let contentViewer = null;
let pollTimer = null;
let pollingKind = null;
let pollingRunId = null;
// The run row the current poll cycle last saw for `pollingRunId` — `null`
// while the "Check now" POST has returned but `list_runs` has not yet
// surfaced the new row as its own #1 result for this kind (task-37 F2).
let livePollRun = null;
let pagesToken = 0;
let dnsShowAll = false;
let dnsRecords = [];
const DNS_COLLAPSE_ROWS = 8;
let changesRaw = [];
let changesVisibleCount = CHANGES_PAGE_SIZE;
let pagesQueryHadFocus = false;
let pagesSearchTimer = null;

function toast(message) {
  const region = document.querySelector("#toast-region");
  const element = document.createElement("div");
  element.className = "toast-message";
  element.textContent = message;
  region.append(element);
  setTimeout(() => element.remove(), 4200);
}

// ---------------------------------------------------------------------------
// URL state
// ---------------------------------------------------------------------------

function parseSortParam(raw) {
  const fallback = { field: DEFAULT_SORT_FIELD, dir: SORT_DEFAULT_DIR[DEFAULT_SORT_FIELD] };
  if (!raw) return fallback;
  const desc = raw.startsWith("-");
  const field = desc ? raw.slice(1) : raw;
  if (!SORT_FIELDS.has(field)) return fallback;
  return { field, dir: desc ? "desc" : "asc" };
}

function composeSortParam() {
  return current.sortDir === "desc" ? `-${current.sortField}` : current.sortField;
}

function paramsFromQuery(query) {
  const { field: sortField, dir: sortDir } = parseSortParam(query.get("sort"));
  const diffKind = query.get("diff");
  return {
    status: ["ok", "broken"].includes(query.get("status")) ? query.get("status") : "",
    notIndexable: query.get("not_indexable") === "1",
    inSitemapNot200: query.get("in_sitemap_not200") === "1",
    changedSince: query.get("changed_since") || "",
    q: query.get("q") || "",
    sortField,
    sortDir,
    page: Math.max(1, Number(query.get("page")) || 1),
    changesState: query.get("state") === "all" ? "all" : "open",
    // One-shot deep link (scroll+focus on load), same contract as
    // `client-page.js`'s `focus=audit` — never written back by `persist()`.
    focusPages: query.get("focus") === "pages",
    diff: diffKind ? {
      kind: diffKind,
      pageId: query.get("diff_page") || undefined,
      resourceUrl: query.get("diff_url") || undefined,
      dnsName: query.get("diff_name") || undefined,
      dnsRecordType: query.get("diff_type") || undefined,
      fromVersionId: query.get("diff_from") || undefined,
      toVersionId: query.get("diff_to") || undefined,
    } : null,
  };
}

function persist() {
  const diff = current.diff || {};
  const sortParam = composeSortParam();
  replaceMonitorClientHash(current.clientId, {
    status: current.status,
    not_indexable: current.notIndexable ? "1" : "",
    in_sitemap_not200: current.inSitemapNot200 ? "1" : "",
    changed_since: current.changedSince,
    q: current.q,
    sort: sortParam === `-${DEFAULT_SORT_FIELD}` ? "" : sortParam,
    page: current.page > 1 ? current.page : "",
    state: current.changesState !== "open" ? current.changesState : "",
    diff: diff.kind || "",
    diff_page: diff.pageId || "",
    diff_url: diff.resourceUrl || "",
    diff_name: diff.dnsName || "",
    diff_type: diff.dnsRecordType || "",
    diff_from: diff.fromVersionId || "",
    diff_to: diff.toVersionId || "",
  });
}

// ---------------------------------------------------------------------------
// Formatting helpers
// ---------------------------------------------------------------------------

function formatCount(value) {
  return value === null || value === undefined ? "—" : new Intl.NumberFormat("en-US").format(value);
}

function formatCheckedProgress(run) {
  if (!run) return "Starting…";
  const checked = run.urls_checked ?? 0;
  // `urls_total` is the real target-list size, set once before the crawl
  // loop starts (task-37 F2) — a true `completed/total` model, shown only
  // when known rather than animating a made-up percentage (AGENTS.md §9).
  // `null` on runs started before this field existed.
  if (Number.isFinite(run.urls_total)) {
    return `Checked ${formatCount(checked)} of ${formatCount(run.urls_total)}`;
  }
  return `Checked ${formatCount(checked)} so far`;
}

function formatDuration(run) {
  // `failed_interrupted` means a worker died mid-run; `finished_at` is
  // written later by the reaper that notices, not by real work ending — the
  // gap between them is a detection delay, not a duration (task-38 §3.5,
  // live case: a run that checked 0 pages showing "90m 17s"). Showing that
  // number as "how long this took" would be a lie.
  if (run.status === "failed_interrupted") return "—";
  if (!run.started_at || !run.finished_at) return "—";
  const ms = new Date(run.finished_at) - new Date(run.started_at);
  if (!Number.isFinite(ms) || ms < 0) return "—";
  const seconds = Math.round(ms / 1000);
  if (seconds < 60) return `${seconds}s`;
  return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
}

function formatBytes(value) {
  if (value === null || value === undefined) return "—";
  const n = Number(value);
  if (!Number.isFinite(n)) return "—";
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)} MB`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)} KB`;
  return `${Math.round(n)} B`;
}

function sectionHeadingWithAction(title, subtitle, actionHtml = "") {
  return `<div class="section-heading"><div><p class="eyebrow">Site Monitor</p><h2>${escapeHtml(title)}</h2>${subtitle ? `<p>${escapeHtml(subtitle)}</p>` : ""}</div>${actionHtml}</div>`;
}

// task-38 D2: a cancelled request must never leave a section `aria-busy`
// forever. With intent-named keys (§1.4) a section's own load is now only
// ever aborted by a NEWER call for that SAME purpose — which will render
// its own result momentarily — but "momentarily" is not a guarantee, so this
// is the backstop: clear busy immediately, and if nothing had rendered yet
// (the skeleton this function itself mounted is still up), replace it with a
// neutral state instead of leaving a "Loading…" that will never resolve.
function settleAbortedSection(el, heading, retryAction) {
  el.setAttribute("aria-busy", "false");
  if (el.querySelector(".skeleton")) {
    el.innerHTML = `${sectionHeadingWithAction(heading, "", "")}<div class="state-panel"><p><strong>Loading was interrupted.</strong></p><button type="button" class="button button--primary" data-action="${retryAction}">${icon("refresh-alert")}Retry</button></div>`;
  }
}

function isRunActive(run) {
  return Boolean(run && RUN_ACTIVE_STATUSES.has(run.status));
}

function robotsUrl() {
  return clientRef?.domain ? `https://${clientRef.domain}/robots.txt` : undefined;
}

// ---------------------------------------------------------------------------
// Shell / mount
// ---------------------------------------------------------------------------

export async function initMonitorClientPage(clientId, query) {
  stopPolling();
  requestToken += 1;
  clientRef = null;
  monitorSummary = null;
  monitorSettings = null;
  openChanges = [];
  openChangesPromise = null;
  contentMode = "loading";
  dnsShowAll = false;
  changesVisibleCount = CHANGES_PAGE_SIZE;
  current = { clientId, ...paramsFromQuery(query) };
  root.innerHTML = shell();
  bindShell();
  diffViewer = createMonitorDiffViewer({
    onError(error) { toast(`Could not load version history: ${error.message}`); },
    onStateChange(descriptor) { current.diff = descriptor; persist(); },
  });
  // Deliberately NOT persisted in the URL like `diff` is: this viewer shows the
  // current stored file, so a deep link would promise a specific state it
  // cannot guarantee after the next check.
  contentViewer = createMonitorContentViewer({
    onError(error) { toast(`Could not load stored file: ${error.message}`); },
  });
  // Deep-linked diff survives F5/back — restored without a synthetic
  // `trigger`, matching the contract's "programmatically, without trigger".
  if (current.diff) diffViewer.open({ ...current.diff, clientId: current.clientId });
  await loadClient();
}

function shell() {
  return `
    <a class="back-link" href="${buildClientHash(current.clientId)}">${icon("arrow-left")}<span>Back to client</span></a>
    <header class="client-page__header" id="mc-header" aria-live="polite" aria-busy="true">
      ${clientHeaderSkeleton()}
    </header>
    <div id="mc-content">${contentShellHtml()}</div>`;
}

function contentShellHtml() {
  return `
    <section id="mc-site-changes" class="monitor-site-changes" aria-live="polite" hidden></section>
    <section id="mc-actions" class="monitor-actions-section" aria-live="polite" aria-busy="true">${sectionSkeleton({ label: "actions", rows: 1, columns: 1 })}</section>
    <div class="monitor-blocks" id="mc-blocks">
      <section class="monitor-block" id="mc-robots" aria-label="Robots.txt" aria-busy="true">${sectionSkeleton({ label: "robots.txt", rows: 2, columns: 1 })}</section>
      <section class="monitor-block" id="mc-sitemap" aria-label="Sitemap" aria-busy="true">${sectionSkeleton({ label: "sitemap", rows: 3, columns: 1 })}</section>
    </div>
    <section class="monitor-pages-section" id="mc-pages" aria-label="Pages" aria-busy="true">${sectionSkeleton({ label: "pages", rows: 8, columns: 10 })}</section>
    <section class="monitor-runs-section" id="mc-runs" aria-label="Check history" aria-busy="true">${sectionSkeleton({ label: "check history", rows: 5, columns: 8 })}</section>
    <section class="monitor-changes-section" id="mc-changes" aria-label="Change feed" aria-busy="true">${sectionSkeleton({ label: "change feed", rows: 5, columns: 2 })}</section>
    <!-- DNS last on purpose: A/NS/MX/TXT change a few times a year, while pages
         and the change feed move every check. It stays a full-width reference
         block at the foot of the page rather than competing for the first
         viewport with what actually changed today. -->
    <section class="monitor-block monitor-block--dns" id="mc-dns" aria-label="DNS and verification" aria-busy="true">${sectionSkeleton({ label: "DNS records", rows: 4, columns: 6 })}</section>`;
}

function mountFullContentAndLoadBlocks() {
  if (contentMode !== "full") {
    document.querySelector("#mc-content").innerHTML = contentShellHtml();
    contentMode = "full";
  }
  renderActions();
  renderSitemap();
  renderRobots();
  loadSiteChangesAndDependents();
  loadDns();
  loadPages();
  loadRuns();
  loadChangesFeed();
}

// One delegated listener per event type on the single container that all
// dynamic sections live under — every section's `renderX()` only ever
// reassigns `innerHTML` on its own leaf node, never on `#mc-content` itself
// (except the never-checked/error/full transitions below), so this binding
// stays valid across the page's whole lifetime without ever being re-added.
function bindShell() {
  const content = document.querySelector("#mc-content");
  content.addEventListener("click", handleContentClick);
  content.addEventListener("change", handleContentChange);
  content.addEventListener("input", handleContentInput);
  content.addEventListener("submit", handleContentSubmit);
}

function handleContentClick(event) {
  const target = event.target.closest("[data-action]");
  if (!target) return;
  const action = target.dataset.action;
  switch (action) {
    case "check-light": startCheck("light"); break;
    case "check-full": startCheck("full"); break;
    case "monitor-anyway": setMonitorWhenPaused(); break;
    // robots.txt lives at a spec-fixed path (RFC 9309 — no origin may serve it
    // anywhere else), so building the URL here is a fact, not a guess. A
    // sitemap has no such fixed location — it is only ever a URL discovered
    // by the crawler — so `sitemap-history` intentionally omits `resourceUrl`
    // and lets the backend return whatever it already tracked.
    case "robots-history": diffViewer.open({ kind: "robots", clientId: current.clientId, resourceUrl: robotsUrl(), trigger: target }); break;
    case "sitemap-history": diffViewer.open({ kind: "sitemap", clientId: current.clientId, trigger: target }); break;
    // "What does the file actually say right now" — a different question from
    // "what changed between two versions", hence a different viewer.
    case "robots-content": contentViewer.open({ kind: "robots", clientId: current.clientId, trigger: target }); break;
    case "sitemap-content": contentViewer.open({ kind: "sitemap", clientId: current.clientId, trigger: target }); break;
    case "sitemap-jump": jumpToPages({ inSitemapNot200: true }); break;
    case "dns-history":
      diffViewer.open({ kind: "dns", clientId: current.clientId, dnsName: target.dataset.dnsName, dnsRecordType: target.dataset.dnsType, trigger: target });
      break;
    case "page-open": diffViewer.open({ kind: "page", clientId: current.clientId, pageId: target.dataset.pageId, trigger: target }); break;
    case "page-check": checkPageRow(target, target.closest("tr")); break;
    case "sort": toggleSort(target.dataset.sortField); break;
    case "ack": showAckForm(target); break;
    case "ack-cancel": restoreAckButton(target); break;
    case "changes-state": setChangesState(target.dataset.state); break;
    case "changes-load-more": changesVisibleCount += CHANGES_PAGE_SIZE; renderChangesFeed(); break;
    case "pages-clear": clearPagesFilters(); break;
    case "pages-prev": changePage(-1); break;
    case "pages-next": changePage(1); break;
    case "retry-dns": loadDns(); break;
    case "dns-show-all": dnsShowAll = true; renderDns(dnsRecords); break;
    case "retry-pages": loadPages(); break;
    case "retry-runs": loadRuns(); break;
    case "retry-changes": loadChangesFeed(); break;
    case "retry-site-changes": loadSiteChangesAndDependents(); break;
    case "retry-monitor": loadClientMonitor(); break;
    default: break;
  }
}

function handleContentChange(event) {
  const target = event.target;
  if (target.id === "mc-pages-status") { current.status = target.value; current.page = 1; persist(); loadPages(); }
  else if (target.id === "mc-pages-not-indexable") { current.notIndexable = target.checked; current.page = 1; persist(); loadPages(); }
  else if (target.id === "mc-pages-in-sitemap-not200") { current.inSitemapNot200 = target.checked; current.page = 1; persist(); loadPages(); }
  else if (target.id === "mc-pages-changed-since") { current.changedSince = target.value; current.page = 1; persist(); loadPages(); }
}

function handleContentInput(event) {
  if (event.target.id !== "mc-pages-q") return;
  clearTimeout(pagesSearchTimer);
  const value = event.target.value;
  pagesSearchTimer = setTimeout(() => {
    current.q = value;
    current.page = 1;
    persist();
    pagesQueryHadFocus = true;
    loadPages();
  }, 220);
}

function handleContentSubmit(event) {
  const form = event.target.closest(".monitor-ack-form");
  if (!form) return;
  event.preventDefault();
  const note = form.elements.note.value.trim();
  acknowledgeChange(form.dataset.changeId, note, form);
}

// ---------------------------------------------------------------------------
// Client identity
// ---------------------------------------------------------------------------

async function loadClient() {
  try {
    const dashboard = await getDashboard({ period: "week", requestKey: "monitor-client-shell" });
    const client = (dashboard.clients || []).find((item) => item.client_id === current.clientId);
    if (!client) { renderNotFound(); return; }
    clientRef = client;
    renderHeader(client);
    await loadClientMonitor();
    if (current.focusPages) focusPagesBlock();
  } catch (error) {
    if (isAbortError(error)) return;
    renderClientLoadError(error);
  }
}

function renderHeader(client) {
  const state = STATE_META[client.overall_status] || STATE_META.attention;
  const logo = client.logo_path
    ? `<img class="client-logo" src="${escapeHtml(client.logo_path)}" width="52" height="52" alt="Logo of ${escapeHtml(client.name)}" loading="lazy">`
    : `<span class="client-logo" aria-hidden="true">${escapeHtml(initials(client.name))}</span>`;
  const header = document.querySelector("#mc-header");
  header.innerHTML = `
    <div class="client-page__identity">
      ${logo}
      <div>
        <p class="eyebrow">Site Monitor</p>
        <h1>${escapeHtml(client.name)}</h1>
        <a href="https://${encodeURI(client.domain)}" target="_blank" rel="noreferrer">${escapeHtml(client.domain)}</a>
      </div>
    </div>
    <div class="client-page__status">
      <span class="status-label ${stateClass(client.overall_status)}">${icon(state.icon)}${state.label}</span>
      <span class="client-page__sync">Last collection: ${formatTimestamp(client.last_synced_at, "not yet")}</span>
    </div>`;
  header.setAttribute("aria-busy", "false");
}

function renderNotFound() {
  const header = document.querySelector("#mc-header");
  header.setAttribute("aria-busy", "false");
  header.innerHTML = `
    <div class="state-panel state-failed">
      ${icon("user-x")}
      <p><strong>Client not found.</strong><br>The link may be outdated.</p>
      <a class="button button--primary" href="#/">Back to client list</a>
    </div>`;
  document.querySelector("#mc-content")?.remove();
}

function renderClientLoadError(error) {
  const header = document.querySelector("#mc-header");
  if (!header) return;
  header.setAttribute("aria-busy", "false");
  header.innerHTML = `
    <div class="state-panel state-failed">
      ${icon("wifi-off")}
      <p><strong>Could not load this client.</strong><br>${escapeHtml(error.message)}. Nothing was changed — retry once the connection is back.</p>
      <button type="button" class="button button--primary" id="mc-retry-client">${icon("refresh")}Retry</button>
    </div>`;
  header.querySelector("#mc-retry-client").addEventListener("click", () => {
    header.setAttribute("aria-busy", "true");
    header.innerHTML = clientHeaderSkeleton();
    loadClient();
  });
}

function focusPagesBlock() {
  const el = document.querySelector("#mc-pages");
  if (!el) return;
  el.setAttribute("tabindex", "-1");
  requestAnimationFrame(() => {
    el.scrollIntoView({ behavior: "smooth", block: "start" });
    el.focus({ preventScroll: true });
  });
}

// ---------------------------------------------------------------------------
// Orchestration: summary + settings -> never-checked gate or full content
// ---------------------------------------------------------------------------

async function loadClientMonitor() {
  const token = ++requestToken;
  try {
    const [summary, settings] = await Promise.all([
      getMonitorSummary(current.clientId),
      getMonitorSettings(current.clientId),
    ]);
    if (token !== requestToken) return;
    monitorSummary = summary;
    monitorSettings = settings;
  } catch (error) {
    if (isAbortError(error)) return;
    renderMonitorLoadError(error);
    return;
  }
  const activeNow = isRunActive(monitorSummary.last_light_run) || isRunActive(monitorSummary.last_full_run);
  if (!monitorSummary.last_light_run && !monitorSummary.last_full_run && !activeNow) {
    contentMode = "never_checked";
    renderNeverChecked();
    return;
  }
  mountFullContentAndLoadBlocks();
  resumePollingIfActive();
}

function renderMonitorLoadError(error) {
  const el = document.querySelector("#mc-content");
  if (!el) return;
  contentMode = "error";
  el.innerHTML = `
    <div class="state-panel state-failed">
      ${icon("wifi-off")}
      <p><strong>Could not load Site Monitor data.</strong><br>${escapeHtml(error.message)}. Nothing was changed — retry once the connection is back.</p>
      <button type="button" class="button button--primary" data-action="retry-monitor">${icon("refresh")}Retry</button>
    </div>`;
}

function renderNeverChecked() {
  const el = document.querySelector("#mc-content");
  if (!el) return;
  el.innerHTML = `
    <div class="state-panel state-not_configured monitor-never-checked">
      ${icon("radar")}
      <div class="monitor-never-checked__body">
        <p><strong>Never checked.</strong><br>Run a check to start monitoring robots.txt, the sitemap, DNS records and key pages for this client.</p>
        <div class="monitor-actions">
          <button type="button" class="button button--primary" data-action="check-light">${icon("refresh")}Check now · Light</button>
          <button type="button" class="button button--quiet" data-action="check-full">${icon("shield-search")}Check now · Full</button>
        </div>
      </div>
    </div>`;
}

// ---------------------------------------------------------------------------
// Check now / polling (task-36 §1.3 — no job engine, no SSE: plain poll of
// `getMonitorSummary` every ~2s until the run we started/resumed goes
// terminal, mirroring `redirect-tool.js`'s per-row run pattern)
// ---------------------------------------------------------------------------

async function startCheck(kind) {
  if (pollingKind) return;
  // Optimistic lock set synchronously, before the `await` — otherwise a
  // rapid double-click fires `startMonitorRun` twice while the first POST is
  // still in flight (`pollingKind` would not be set yet to stop the second).
  pollingKind = kind;
  if (contentMode === "full") renderActions();
  let accepted;
  try {
    accepted = await startMonitorRun(current.clientId, kind);
  } catch (error) {
    pollingKind = null;
    if (contentMode === "full") renderActions();
    toast(`Could not start the ${kind} check: ${error.message}`);
    return;
  }
  mountFullContentAndLoadBlocks();
  beginPolling(kind, accepted.run_id);
}

// Restores the live-polling state after F5 mid-check (AGENTS.md §9 —
// "Reopening or refreshing MUST restore state from the API"). `monitorSummary`
// cannot answer "is a check running right now" — `last_light_run`/
// `last_full_run` only ever reflect the latest FINISHED run (task-37 F2) —
// so this asks the run log directly instead.
function resumePollingIfActive() {
  if (pollingKind) return;
  Promise.all([
    listMonitorRuns(current.clientId, { kind: "full", limit: 1, requestKey: `monitor-runs-resume-full-${current.clientId}` }),
    listMonitorRuns(current.clientId, { kind: "light", limit: 1, requestKey: `monitor-runs-resume-light-${current.clientId}` }),
  ]).then(([fullRuns, lightRuns]) => {
    if (pollingKind) return; // a manual check started while this was in flight
    const full = fullRuns[0];
    const light = lightRuns[0];
    if (isRunActive(full)) beginPolling("full", full.id);
    else if (isRunActive(light)) beginPolling("light", light.id);
  }).catch(() => { /* best-effort resume; a normal poll cycle will pick it up on the next tick if this fails */ });
}

function beginPolling(kind, runId) {
  pollingKind = kind;
  pollingRunId = runId;
  livePollRun = null;
  renderActions();
  pollOnce();
  pollTimer = setInterval(pollOnce, POLL_INTERVAL_MS);
}

function stopPolling() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = null;
  pollingKind = null;
  pollingRunId = null;
  livePollRun = null;
}

async function pollOnce() {
  const kind = pollingKind;
  const runId = pollingRunId;
  if (!kind) return;
  try {
    const runs = await listMonitorRuns(current.clientId, { kind, limit: 1, requestKey: `monitor-runs-poll-${current.clientId}` });
    if (pollingKind !== kind) return;
    const run = runs[0];
    // The freshly queued/claimed row can, for one tick right after the POST,
    // not yet be `list_runs`'s #1 result for this kind — comparing `id`
    // against the `run_id` the POST returned is the only way to avoid
    // mistaking an OLDER finished run for this check's progress or
    // completion (task-37 F2, the exact bug this replaces).
    if (!run || run.id !== runId) {
      livePollRun = null;
      renderLiveStatus(kind, null);
      return;
    }
    livePollRun = run;
    renderLiveStatus(kind, run);
    if (RUN_TERMINAL_STATUSES.has(run.status)) {
      stopPolling();
      toast(run.status === "succeeded"
        ? `${kind === "full" ? "Full" : "Light"} check complete.`
        : `${kind === "full" ? "Full" : "Light"} check finished with issues.`);
      await reloadAfterRun();
    }
  } catch (error) {
    // A transient poll failure does not stop the background check — it keeps
    // running server-side regardless — so this only skips a tick rather than
    // surfacing an error state (isAbortError guards the superseded-request case).
    if (isAbortError(error)) return;
  }
}

async function reloadAfterRun() {
  openChangesPromise = null;
  try { monitorSummary = await getMonitorSummary(current.clientId); } catch { /* keep the last known summary */ }
  renderActions();
  renderSitemap();
  // A finished run can change nearly everything — reset pagination rather
  // than leaving the operator on a now-meaningless page, same precedent as
  // `blogs-page.js`'s `onTerminal` resetting `blogsOffset`.
  current.page = 1;
  changesVisibleCount = CHANGES_PAGE_SIZE;
  persist();
  loadSiteChangesAndDependents();
  loadDns();
  loadPages();
  loadRuns();
  loadChangesFeed();
}

function renderLiveStatus(kind, run) {
  const el = document.querySelector("#mc-live-status");
  if (!el) return;
  el.hidden = false;
  el.className = "live-collection monitor-live-status live-collection--running";
  el.innerHTML = `<div class="live-collection__copy">${icon("cloud-download")}<p><strong>${kind === "full" ? "Full" : "Light"} check running</strong><br><span>${escapeHtml(formatCheckedProgress(run))}</span></p></div>`;
}

async function setMonitorWhenPaused() {
  try {
    monitorSettings = await patchMonitorSettings(current.clientId, { monitor_when_paused: true });
    toast("This client will now be monitored even while paused.");
    renderActions();
  } catch (error) {
    toast(`Could not update settings: ${error.message}`);
  }
}

function renderActions() {
  const el = document.querySelector("#mc-actions");
  if (!el) return;
  el.setAttribute("aria-busy", "false");
  const paused = clientRef?.status === "paused";
  const pausedGate = paused && !monitorSettings?.monitor_when_paused;
  if (pausedGate) {
    el.innerHTML = `
      <div class="state-panel state-not_configured monitor-paused-gate">
        ${icon("player-pause")}
        <div>
          <p><strong>Monitoring off (client paused).</strong><br>Scheduled and manual checks are skipped while this client is paused. Data collected before pausing stays visible below.</p>
          <button type="button" class="button button--quiet" data-action="monitor-anyway">${icon("shield-check")}Monitor anyway</button>
        </div>
      </div>`;
    return;
  }
  const active = pollingKind;
  el.innerHTML = `
    <div class="monitor-actions">
      <button type="button" class="button button--primary" data-action="check-light" ${active ? "disabled" : ""}>${icon("refresh")}Check now · Light</button>
      <button type="button" class="button button--quiet" data-action="check-full" ${active ? "disabled" : ""}>${icon("shield-search")}Check now · Full</button>
      <p class="monitor-next-due">Next light check: ${formatTimestamp(monitorSummary?.next_light_due_at, "not scheduled")} · Next full check: ${formatTimestamp(monitorSummary?.next_full_due_at, "not scheduled")}</p>
    </div>
    <div class="live-collection monitor-live-status" id="mc-live-status" aria-live="polite" ${active ? "" : "hidden"}></div>`;
  if (active) renderLiveStatus(active, livePollRun);
}

// ---------------------------------------------------------------------------
// Site-scope changes (top strip) + open-changes cache shared with Robots/DNS
// ---------------------------------------------------------------------------

function fetchOpenChanges() {
  if (!openChangesPromise) {
    openChangesPromise = listClientMonitorChanges(current.clientId, { state: "open" })
      .then((data) => { openChanges = data; return data; })
      .catch((error) => { openChangesPromise = null; throw error; });
  }
  return openChangesPromise;
}

async function loadSiteChangesAndDependents() {
  const el = document.querySelector("#mc-site-changes");
  try {
    await fetchOpenChanges();
    renderSiteChanges();
  } catch (error) {
    if (isAbortError(error)) return;
    if (el) {
      el.hidden = false;
      el.innerHTML = `<div class="state-panel state-failed">${icon("wifi-off")}<p><strong>Could not load open changes.</strong><br>${escapeHtml(error.message)}</p><button type="button" class="button button--primary" data-action="retry-site-changes">${icon("refresh")}Retry</button></div>`;
    }
  } finally {
    // Robots' "blocked"/"allowed" conclusion depends on `openChanges` — always
    // repaint it once the fetch settles, success or not, so it never sits on
    // its skeleton forever.
    renderRobots();
  }
}

function renderSiteChanges() {
  const el = document.querySelector("#mc-site-changes");
  if (!el) return;
  const siteChanges = (openChanges || []).filter((change) => change.scope === "site" && !change.resolved_at);
  if (!siteChanges.length) { el.hidden = true; el.innerHTML = ""; return; }
  el.hidden = false;
  el.innerHTML = `<ul class="monitor-changes">${siteChanges.map(changeRow).join("")}</ul>`;
}

// ---------------------------------------------------------------------------
// Robots.txt block
// ---------------------------------------------------------------------------

function renderRobots() {
  const el = document.querySelector("#mc-robots");
  if (!el) return;
  el.setAttribute("aria-busy", "false");
  const status = monitorSummary?.robots_status;
  const blocked = (openChanges || []).some((change) => change.change_type === "robots_blocks_site" && !change.resolved_at);
  const lastChecked = monitorSummary?.last_full_run?.finished_at || monitorSummary?.last_light_run?.finished_at;
  const readable = typeof status === "number" && status >= 200 && status < 300;
  // A non-2xx status (WAF challenge, 404, ...) means the rules themselves
  // were never actually read — that is a data-availability gap, not a real
  // "allowed" conclusion, so it gets its own honest state instead of
  // defaulting to the green case.
  // Tone drives a `.monitor-conclusion--<tone>` modifier class (monitor.css),
  // never an inline `style` — the app's CSP (`default-src 'self'`, no
  // `style-src 'unsafe-inline'`) blocks inline styles outright.
  const conclusion = status === null || status === undefined
    ? { text: "Not checked yet", tone: "help-circle", modifier: "unknown" }
    : !readable
      ? { text: "Could not read robots.txt", tone: "alert-triangle", modifier: "warning" }
      : blocked
        ? { text: "Site blocked for Googlebot", tone: "alert-triangle", modifier: "critical" }
        : { text: "Googlebot: allowed", tone: "circle-check", modifier: "ok" };
  el.innerHTML = `
    ${sectionHeadingWithAction("Robots.txt", status !== null && status !== undefined ? `HTTP ${status}` : "", `<button type="button" class="button button--quiet" data-action="robots-content">${icon("file-text")}View file</button><button type="button" class="button button--quiet" data-action="robots-history">${icon("history")}History</button>`)}
    <p class="monitor-conclusion monitor-conclusion--${conclusion.modifier}">${icon(conclusion.tone)}<span>${conclusion.text}</span></p>
    <p class="monitor-changed-at">Last checked: ${formatTimestamp(lastChecked, "not checked yet")}</p>`;
}

// ---------------------------------------------------------------------------
// Sitemap block
// ---------------------------------------------------------------------------

function sitemapHealthCell(value) {
  if (value === null || value === undefined) return "—";
  if (!value) return "0"; // a real, earned zero — never hidden as "—" (None ≠ 0)
  // Redirects/broken/unavailable all collapse to the same Pages drill-down:
  // the `status` filter this page exposes only distinguishes ok (2xx) vs.
  // broken (everything else, including redirects and null) — see
  // `getMonitorPages`'s fixture filter in api.js. A finer split would need a
  // Pages-side status enum this task does not define, so all three numbers
  // open the same `in_sitemap_not200` view; the breakdown itself stays
  // visible in these four counters.
  return `<button type="button" class="monitor-health-link" data-action="sitemap-jump">${formatCount(value)}</button>`;
}

function renderSitemap() {
  const el = document.querySelector("#mc-sitemap");
  if (!el) return;
  el.setAttribute("aria-busy", "false");
  const health = monitorSummary?.sitemap_health;
  const lastRun = monitorSummary?.last_full_run;
  const lastChecked = lastRun?.finished_at || monitorSummary?.last_light_run?.finished_at;
  const truncatedChip = lastRun?.truncated
    ? `<p class="chip-button monitor-chip">${icon("cut")}Truncated · showing first ${formatCount(lastRun.urls_checked)} of ${formatCount(lastRun.urls_checked + (lastRun.remaining_count ?? 0))}</p>`
    : "";
  el.innerHTML = `
    ${sectionHeadingWithAction("Sitemap", health ? `${formatCount(health.total)} URLs tracked` : "", `<button type="button" class="button button--quiet" data-action="sitemap-content">${icon("list-details")}View URLs</button><button type="button" class="button button--quiet" data-action="sitemap-history">${icon("history")}History</button>`)}
    ${truncatedChip}
    <dl class="monitor-sitemap-health">
      <div><dt>OK</dt><dd>${formatCount(health?.ok)}</dd></div>
      <div><dt>Redirects</dt><dd>${sitemapHealthCell(health?.redirects)}</dd></div>
      <div><dt>Broken</dt><dd>${sitemapHealthCell(health?.broken)}</dd></div>
      <div><dt>Unavailable</dt><dd>${sitemapHealthCell(health?.unavailable)}</dd></div>
    </dl>
    <p class="monitor-changed-at">Last checked: ${formatTimestamp(lastChecked, "not checked yet")}</p>`;
}

function jumpToPages(patch) {
  Object.assign(current, patch, { page: 1 });
  persist();
  loadPages();
  const el = document.querySelector("#mc-pages");
  if (!el) return;
  el.setAttribute("tabindex", "-1");
  requestAnimationFrame(() => {
    el.scrollIntoView({ behavior: "smooth", block: "start" });
    el.focus({ preventScroll: true });
  });
}

// ---------------------------------------------------------------------------
// DNS & verification block
// ---------------------------------------------------------------------------

function findGscVerification(records) {
  for (const record of records) {
    if (record.record_type !== "TXT") continue;
    const match = (record.values || []).find((value) => value.startsWith("google-site-verification="));
    if (match) return match.slice("google-site-verification=".length);
  }
  return null;
}

async function loadDns() {
  const el = document.querySelector("#mc-dns");
  if (!el) return;
  el.setAttribute("aria-busy", "true");
  el.innerHTML = sectionSkeleton({ label: "DNS records", rows: 4, columns: 6 });
  try {
    const records = await getMonitorDnsRecords(current.clientId);
    dnsRecords = records;
    // Best-effort: the "could not be checked" flag needs `openChanges`, but a
    // failure there must not block rendering DNS records that loaded fine.
    await fetchOpenChanges().catch(() => null);
    renderDns(records);
  } catch (error) {
    if (isAbortError(error)) { settleAbortedSection(el, "DNS & verification", "retry-dns"); return; }
    el.setAttribute("aria-busy", "false");
    el.innerHTML = `${sectionHeadingWithAction("DNS & verification", "", "")}<div class="state-panel state-failed">${icon("wifi-off")}<p><strong>Could not load DNS records.</strong><br>${escapeHtml(error.message)}</p><button type="button" class="button button--primary" data-action="retry-dns">${icon("refresh")}Retry</button></div>`;
  }
}

function dnsRow(record) {
  const values = Array.isArray(record.values) && record.values.length
    ? record.values.map((value) => escapeHtml(value)).join("<br>")
    : "—";
  return `<tr>
    <td class="analytics-row__value">${escapeHtml(record.name)}</td>
    <td>${escapeHtml(record.record_type)}</td>
    <td>${values}</td>
    <td class="num">${record.ttl ?? "—"}</td>
    <td>${formatTimestamp(record.last_checked_at)}</td>
    <td><button type="button" class="button button--quiet" data-action="dns-history" data-dns-name="${escapeHtml(record.name)}" data-dns-type="${escapeHtml(record.record_type)}">${icon("history")}History</button></td>
  </tr>`;
}

function renderDns(records) {
  const el = document.querySelector("#mc-dns");
  if (!el) return;
  el.setAttribute("aria-busy", "false");
  const unavailable = (openChanges || []).some((change) => change.change_type === "dns_unavailable" && !change.resolved_at);
  const gsc = findGscVerification(records);
  const banner = unavailable
    ? `<div class="state-panel state-attention monitor-dns-banner">${icon("cloud-off")}<p><strong>DNS could not be checked.</strong><br>Showing the last known values below — this is not the same as "no records".</p></div>`
    : "";
  // Collapsed by default (task-37 F5): a busy domain's full DNS record set
  // can run well past a screen's height; showing that many is never hidden
  // silently — the count above the table and the "Show all" chip both say
  // exactly how many more rows exist.
  const visibleRecords = dnsShowAll ? records : records.slice(0, DNS_COLLAPSE_ROWS);
  const showAllChip = !dnsShowAll && records.length > DNS_COLLAPSE_ROWS
    ? `<button type="button" class="chip-button monitor-dns-show-all" data-action="dns-show-all">${icon("chevron-down")}Show all ${records.length}</button>`
    : "";
  const table = records.length
    ? `<div class="table-scroll"><table class="analytics-table monitor-dns-table">
        <thead><tr><th>Name</th><th>Type</th><th>Values</th><th class="num">TTL</th><th>Last checked</th><th></th></tr></thead>
        <tbody>${visibleRecords.map(dnsRow).join("")}</tbody>
      </table></div>${showAllChip}`
    : `<div class="state-panel state-not_configured"><p><strong>No DNS records recorded yet.</strong></p></div>`;
  el.innerHTML = `
    ${sectionHeadingWithAction("DNS & verification", records.length ? `${records.length} records` : "", "")}
    ${banner}
    <p class="monitor-dns-verify">${icon(gsc ? "circle-check" : "circle-x")}<span>Search Console verification: ${gsc ? `found (<code>${escapeHtml(gsc)}</code>)` : "not found"}</span></p>
    <p class="monitor-dns-verify monitor-dns-analytics">${icon("info-circle")}<span>Analytics tags (GA4/GTM) are not read from DNS — open a key page's history in Pages above and check its <code>analytics_ids</code> signal to confirm one is present.</span></p>
    ${table}`;
}

// ---------------------------------------------------------------------------
// Pages block
// ---------------------------------------------------------------------------

function sortableTh(field, label) {
  const active = current.sortField === field;
  const ariaSort = active ? (current.sortDir === "asc" ? "ascending" : "descending") : "none";
  const arrow = active ? icon(current.sortDir === "asc" ? "chevron-up" : "chevron-down", "icon icon--tiny") : "";
  return `<th aria-sort="${ariaSort}"><button type="button" class="monitor-sort-button" data-action="sort" data-sort-field="${field}">${escapeHtml(label)}${arrow}</button></th>`;
}

function pagesTableHead() {
  return `<tr>
    ${sortableTh("url", "URL")}
    ${sortableTh("status", "Status")}
    <th>In sitemap</th>
    <th>Indexable</th>
    <th>Meta robots</th>
    <th>Canonical</th>
    <th>Title</th>
    ${sortableTh("last_checked_at", "Last checked")}
    ${sortableTh("version_seq", "Versions")}
    <th><span class="visually-hidden">Actions</span></th>
  </tr>`;
}

// Directives that actually change what Google does. WordPress ships
// `max-image-preview:large, max-snippet:-1, max-video-preview:-1` on virtually
// every page, and printing that in full would make the column unreadable on
// 600 rows — so the policy tokens are shown, the rest is counted (`+3`) and the
// complete string stays available in the cell's `title`. Nothing is dropped
// silently (PLAN §12).
const ROBOTS_POLICY_TOKENS = new Set([
  "index", "noindex", "follow", "nofollow", "none",
  "noarchive", "nosnippet", "noimageindex", "notranslate",
]);

function metaRobotsCell(page) {
  const raw = page.meta_robots || page.x_robots_tag;
  if (!raw) return "—";
  const tokens = raw.split(",").map((token) => token.trim()).filter(Boolean);
  const policy = tokens.filter((token) => ROBOTS_POLICY_TOKENS.has(token.toLowerCase()));
  const shown = (policy.length ? policy : tokens).join(", ");
  const hiddenCount = policy.length ? tokens.length - policy.length : 0;
  // `noindex` and `none` both remove the page from the index — the single most
  // important thing this whole monitor watches for, so it is never just text.
  const isNoindex = tokens.some((token) => ["noindex", "none"].includes(token.toLowerCase()));
  const extra = hiddenCount > 0 ? `<span class="monitor-robots__extra">+${hiddenCount}</span>` : "";
  // The header carries the same weight as the meta tag but is invisible in the
  // page source, so where the directive came from is part of the answer.
  const source = !page.meta_robots && page.x_robots_tag
    ? `<span class="monitor-robots__extra">X-Robots-Tag</span>`
    : "";
  // `aria-label` carries the full string independently of `title` (task-38
  // §3.6): a tooltip alone never reaches a screen reader, so "+3" used to be
  // read as literally "plus three" with no way to learn what the other three
  // tokens were.
  return `<span class="monitor-robots${isNoindex ? " monitor-robots--noindex" : ""}" title="${escapeHtml(raw)}" aria-label="${escapeHtml(raw)}">${escapeHtml(shown)}${extra}${source}</span>`;
}

// task-38 §3.1: canonical duplicates the row's own URL in ~90% of rows — the
// widest column in the table spent on a repeat of the first one. `self`
// (muted) makes the common case take a glance, and the anomaly (a REAL
// cross-URL canonical, which is what an operator is actually scanning for)
// is the only time this column prints a full URL at all.
function canonicalCell(page) {
  if (!page.canonical) return "—";
  if (page.canonical === page.url) {
    return `<span class="monitor-canonical-self" title="${escapeHtml(page.canonical)}">self</span>`;
  }
  return wrapUrlPath(page.canonical);
}

// Breaks only at path separators (`<wbr>`), never mid-token: the default
// break behavior was cutting words in half ("…/shop/utru" / "stning/teddy/",
// task-38 §3.2). `<wbr>` is a zero-width, invisible break opportunity — it
// changes nothing when the URL fits on one line.
function wrapUrlPath(url) {
  return escapeHtml(url).replace(/\//g, "/<wbr>");
}

// Same four-state vocabulary as `sitemap_health` (`app/monitor/severity.py`
// `classify_sitemap_status` — task-38 §3.3: "той самий словник"), so a page's
// row-level status and the sitemap block's aggregate counts never disagree
// about what counts as broken.
function pageStatusKind(status) {
  if (status === null || status === undefined || status >= 500) return "unavailable";
  if (status >= 400) return "broken";
  if (status >= 300) return "redirect";
  return "ok";
}

const PAGE_STATUS_LABEL = { ok: "OK", redirect: "Redirect", broken: "Broken", unavailable: "Unavailable" };

function pageStatusCell(page) {
  if (page.status === null || page.status === undefined) return "—";
  const kind = pageStatusKind(page.status);
  // Color is never the only signal (AGENTS §7) — the word and the number
  // both travel with the chip, not just its class.
  return `<span class="status-label ${stateClass(kind)}">${escapeHtml(PAGE_STATUS_LABEL[kind])} ${page.status}</span>`;
}

function pageRow(page) {
  // Indexable is a real 3-state field: `null` means "not checked yet", which
  // is not the same claim as `false` ("checked, and it is not indexable") —
  // collapsing the two would misreport a page nobody has looked at yet.
  const indexableText = page.indexable === true ? "Yes" : page.indexable === false ? "No" : "Not checked yet";
  return `<tr data-page-id="${escapeHtml(page.id)}">
    <td class="analytics-row__value">
      <button type="button" class="monitor-row-open" data-action="page-open" data-page-id="${escapeHtml(page.id)}" aria-label="Open version history: ${escapeHtml(page.url)}">${icon("history", "icon icon--tiny")}<span>${wrapUrlPath(page.url)}</span></button>
      ${page.is_key_page ? `<span class="monitor-key-badge" title="Key page">${icon("star")}</span>` : ""}
    </td>
    <td>${pageStatusCell(page)}</td>
    <td>${page.in_sitemap ? "Yes" : "No"}</td>
    <td>${indexableText}</td>
    <td>${metaRobotsCell(page)}</td>
    <td>${canonicalCell(page)}</td>
    <td>${page.title ? escapeHtml(page.title) : "—"}</td>
    <td>${formatTimestamp(page.last_checked_at)}</td>
    <td class="num">${page.version_seq ?? "—"}</td>
    <td><button type="button" class="button button--quiet monitor-page-check" data-action="page-check" data-page-id="${escapeHtml(page.id)}" aria-label="Check this page now: ${escapeHtml(page.url)}">${icon("refresh", "icon icon--tiny")}Check</button></td>
  </tr>`;
}

async function checkPageRow(button, row) {
  if (!row || button.disabled) return;
  const original = button.innerHTML;
  button.disabled = true;
  button.innerHTML = `${icon("loader-2", "icon icon--spin icon--tiny")}Checking…`;
  try {
    const page = await checkMonitorPage(current.clientId, button.dataset.pageId);
    const fresh = document.createElement("tbody");
    fresh.innerHTML = pageRow(page);
    row.replaceWith(fresh.firstElementChild);
    toast(`Checked ${page.url} — HTTP ${page.status ?? "no response"}.`);
  } catch (error) {
    button.disabled = false;
    button.innerHTML = original;
    toast(`Could not check this page: ${error.message}`);
  }
}

function pagesToolbar() {
  const forced = current.inSitemapNot200;
  const filtersActive = Boolean(current.status || current.notIndexable || current.inSitemapNot200 || current.changedSince || current.q);
  return `<div class="analytics-toolbar monitor-pages-toolbar">
    <label class="search-field"><span class="visually-hidden">Search pages by URL</span>${icon("search")}<input type="search" id="mc-pages-q" placeholder="Search URL" value="${escapeHtml(current.q)}"></label>
    <label>Status<select id="mc-pages-status" ${forced ? "disabled" : ""}>
      <option value="" ${!current.status ? "selected" : ""}>All</option>
      <option value="ok" ${current.status === "ok" ? "selected" : ""}>OK (2xx)</option>
      <option value="broken" ${current.status === "broken" || forced ? "selected" : ""}>Broken / redirect / unavailable</option>
    </select></label>
    <label class="monitor-checkbox"><input type="checkbox" id="mc-pages-not-indexable" ${current.notIndexable ? "checked" : ""}><span>Not indexable only</span></label>
    <label class="monitor-checkbox"><input type="checkbox" id="mc-pages-in-sitemap-not200" ${current.inSitemapNot200 ? "checked" : ""}><span>In sitemap &amp; not 200</span></label>
    <label>Changed since <span class="monitor-field-hint">(YYYY-MM-DD)</span><input type="date" id="mc-pages-changed-since" lang="en-GB" value="${escapeHtml(current.changedSince)}"></label>
    ${filtersActive ? `<button type="button" class="button button--quiet" data-action="pages-clear">${icon("x")}Clear filters</button>` : ""}
  </div>`;
}

function paginationMarkup(result) {
  const { total, limit, offset } = result;
  if (!total) return "";
  const start = offset + 1;
  const end = Math.min(offset + limit, total);
  return `<div class="pagination">
    <button type="button" class="button button--quiet" data-action="pages-prev" ${offset > 0 ? "" : "disabled"}>${icon("chevron-left")}Previous</button>
    <span>${formatCount(start)}–${formatCount(end)} of ${formatCount(total)}</span>
    <button type="button" class="button button--quiet" data-action="pages-next" ${end < total ? "" : "disabled"}>Next${icon("chevron-right")}</button>
  </div>`;
}

// `changed_since` has no backend filter (verified against the DTO contract
// given for this task) — this pulls the full change list and filters the
// already-loaded page of rows client-side. Consequence flagged in the UI
// copy: the server's `total`/pagination numbers are computed BEFORE this
// filter, so "N of M" and the rows shown can legitimately disagree with the
// count on this page. TODO(task-36-followup): a real `changed_since` query
// param on `GET /monitor/pages` would remove this caveat entirely.
async function loadChangedPageUrls(sinceDate) {
  const changes = await listClientMonitorChanges(current.clientId, { state: "all" });
  const since = new Date(`${sinceDate}T00:00:00Z`);
  const urls = new Set();
  for (const change of changes) {
    if (change.scope !== "page" || !change.detected_at) continue;
    if (new Date(change.detected_at) >= since) urls.add(change.subject);
  }
  return urls;
}

async function loadPages() {
  const token = ++pagesToken;
  const el = document.querySelector("#mc-pages");
  if (!el) return;
  el.setAttribute("aria-busy", "true");
  el.innerHTML = sectionSkeleton({ label: "pages", rows: 8, columns: 10 });
  try {
    const effectiveStatus = current.inSitemapNot200 ? "broken" : (current.status || undefined);
    const effectiveInSitemap = current.inSitemapNot200 ? true : undefined;
    const result = await getMonitorPages(current.clientId, {
      status: effectiveStatus,
      indexable: current.notIndexable ? false : undefined,
      inSitemap: effectiveInSitemap,
      q: current.q || undefined,
      sort: composeSortParam(),
      limit: PAGES_PAGE_SIZE,
      offset: (current.page - 1) * PAGES_PAGE_SIZE,
    });
    if (token !== pagesToken) return;
    let rows = result.pages || [];
    let changedNote = "";
    if (current.changedSince) {
      const changedUrls = await loadChangedPageUrls(current.changedSince);
      if (token !== pagesToken) return;
      const loadedCount = rows.length;
      rows = rows.filter((page) => changedUrls.has(page.url));
      changedNote = `Changed-since filter applies only within this loaded page: ${rows.length} of ${loadedCount} loaded rows changed on or after ${formatDate(current.changedSince)}. Narrow with search or other filters for a complete match across all pages.`;
    }
    renderPages({ ...result, pages: rows }, changedNote);
  } catch (error) {
    if (isAbortError(error)) { settleAbortedSection(el, "Pages", "retry-pages"); return; }
    el.setAttribute("aria-busy", "false");
    el.innerHTML = `${sectionHeadingWithAction("Pages", "", "")}<div class="state-panel state-failed">${icon("wifi-off")}<p><strong>Could not load pages.</strong><br>${escapeHtml(error.message)}</p><button type="button" class="button button--primary" data-action="retry-pages">${icon("refresh")}Retry</button></div>`;
  }
}

function renderPages(result, changedNote) {
  const el = document.querySelector("#mc-pages");
  if (!el) return;
  el.setAttribute("aria-busy", "false");
  const filtersActive = Boolean(current.status || current.notIndexable || current.inSitemapNot200 || current.changedSince || current.q);
  const rows = result.pages || [];
  const exportActions = `<div class="monitor-export-actions"><a class="button button--quiet" href="${escapeHtml(monitorExportCsvUrl(current.clientId))}" download>${icon("download")}Export CSV</a><a class="button button--quiet" href="${escapeHtml(monitorExportXlsxUrl(current.clientId))}" download>${icon("download")}Export XLSX</a></div>`;
  const body = rows.length
    ? `<div class="table-scroll"><table class="analytics-table monitor-pages-table"><thead>${pagesTableHead()}</thead><tbody>${rows.map(pageRow).join("")}</tbody></table></div>${paginationMarkup(result)}`
    : `<div class="state-panel state-not_configured"><p><strong>${filtersActive ? "No pages match these filters." : "No pages recorded yet."}</strong></p></div>`;
  el.innerHTML = `
    ${sectionHeadingWithAction("Pages", result.total !== null && result.total !== undefined ? `${formatCount(result.total)} tracked URLs` : "", exportActions)}
    ${pagesToolbar()}
    ${changedNote ? `<p class="monitor-changed-note">${icon("info-circle")}${escapeHtml(changedNote)}</p>` : ""}
    ${body}`;
  if (pagesQueryHadFocus) {
    pagesQueryHadFocus = false;
    const input = el.querySelector("#mc-pages-q");
    if (input) { input.focus(); input.setSelectionRange(input.value.length, input.value.length); }
  }
}

function toggleSort(field) {
  if (!SORT_FIELDS.has(field)) return;
  if (current.sortField === field) {
    current.sortDir = current.sortDir === "asc" ? "desc" : "asc";
  } else {
    current.sortField = field;
    current.sortDir = SORT_DEFAULT_DIR[field] || "asc";
  }
  current.page = 1;
  persist();
  loadPages();
}

function clearPagesFilters() {
  current.status = "";
  current.notIndexable = false;
  current.inSitemapNot200 = false;
  current.changedSince = "";
  current.q = "";
  current.page = 1;
  persist();
  loadPages();
}

function changePage(delta) {
  current.page = Math.max(1, current.page + delta);
  persist();
  loadPages();
}

// ---------------------------------------------------------------------------
// Check history (run log)
// ---------------------------------------------------------------------------

function runRow(run) {
  const state = STATE_META[run.status] || { label: run.status || "Unknown", icon: "help-circle" };
  const errorNote = run.error_message ? `<br><small class="monitor-run-error">${escapeHtml(run.error_message)}</small>` : "";
  return `<tr class="${stateClass(run.status)}">
    <td>${run.kind === "full" ? "Full" : "Light"}</td>
    <td><span class="status-label ${stateClass(run.status)}">${icon(state.icon)}${escapeHtml(state.label)}</span>${errorNote}</td>
    <td>${escapeHtml(run.trigger || "—")}</td>
    <td>${formatTimestamp(run.started_at)}</td>
    <td>${formatDuration(run)}</td>
    <td class="num">${run.urls_checked ?? "—"}</td>
    <td class="num">${run.urls_changed ?? "—"}</td>
    <td class="num">${formatBytes(run.bytes_downloaded)}</td>
  </tr>`;
}

async function loadRuns() {
  const el = document.querySelector("#mc-runs");
  if (!el) return;
  el.setAttribute("aria-busy", "true");
  el.innerHTML = sectionSkeleton({ label: "check history", rows: 5, columns: 8 });
  try {
    const runs = await listMonitorRuns(current.clientId, { limit: RUNS_LIMIT, requestKey: `monitor-runs-history-${current.clientId}` });
    renderRuns(runs);
  } catch (error) {
    if (isAbortError(error)) { settleAbortedSection(el, "Check history", "retry-runs"); return; }
    el.setAttribute("aria-busy", "false");
    el.innerHTML = `${sectionHeadingWithAction("Check history", "", "")}<div class="state-panel state-failed">${icon("wifi-off")}<p><strong>Could not load check history.</strong><br>${escapeHtml(error.message)}</p><button type="button" class="button button--primary" data-action="retry-runs">${icon("refresh")}Retry</button></div>`;
  }
}

function renderRuns(allRuns) {
  const el = document.querySelector("#mc-runs");
  if (!el) return;
  el.setAttribute("aria-busy", "false");
  // task-38 §2: this table is the client-wide check log (light/full sweeps).
  // Per-page re-checks fired from the Pages table share the same `MonitorRun`
  // log for data integrity (every change still has a real run behind it) but
  // would flood this history with dozens of one-URL rows if shown here too —
  // they stay visible on their own row in the Pages table instead.
  const runs = allRuns.filter((run) => run.kind !== "page");
  if (!runs.length) {
    el.innerHTML = `${sectionHeadingWithAction("Check history", "", "")}<div class="state-panel state-not_configured">${icon("history")}<p><strong>No checks recorded yet.</strong></p></div>`;
    return;
  }
  // The API has no explicit `is_baseline` flag (verified against the DTO
  // contract given for this task) — a single row in the whole history is
  // the only available signal that this was the very first check, so a
  // history of exactly one run gets the baseline banner instead of a table
  // with nothing to compare it to.
  if (runs.length === 1) {
    const run = runs[0];
    el.innerHTML = `${sectionHeadingWithAction("Check history", "", "")}
      <div class="state-panel state-ok monitor-runs-baseline">
        ${icon("flag")}
        <p><strong>Baseline recorded — changes will be tracked from the next check.</strong><br>${run.kind === "full" ? "Full" : "Light"} check on ${formatTimestamp(run.started_at)} · ${run.urls_checked ?? "—"} URL${run.urls_checked === 1 ? "" : "s"} checked.</p>
      </div>`;
    return;
  }
  el.innerHTML = `${sectionHeadingWithAction("Check history", `${runs.length} checks`, "")}<div class="table-scroll"><table class="analytics-table monitor-runs-table">
    <thead><tr><th>Kind</th><th>Status</th><th>Trigger</th><th>Started</th><th>Duration</th><th class="num">Checked</th><th class="num">Changed</th><th class="num">Traffic</th></tr></thead>
    <tbody>${runs.map(runRow).join("")}</tbody>
  </table></div>`;
}

// ---------------------------------------------------------------------------
// Change feed (shared row renderer with the site-scope change strip above)
// ---------------------------------------------------------------------------

function changeRow(change) {
  const meta = MONITOR_SEVERITY_META[change.severity] || MONITOR_SEVERITY_META.info;
  const label = changeTypeLabel(change.change_type);
  const subject = change.subject || "";
  const ackLabel = `Acknowledge: ${label} on ${subject || "this change"}`;
  let statusMarkup;
  if (change.resolved_at) {
    statusMarkup = `<span class="monitor-change-status monitor-change-status--resolved">${icon("circle-check")}Resolved ${formatTimestamp(change.resolved_at)}</span>`;
  } else if (change.acknowledged_at) {
    statusMarkup = `<span class="monitor-change-status">${icon("check")}Acknowledged ${formatTimestamp(change.acknowledged_at)}${change.acknowledged_note ? `<br><em>${escapeHtml(change.acknowledged_note)}</em>` : ""}</span>`;
  } else {
    statusMarkup = `<button type="button" class="button button--quiet monitor-ack-button" data-action="ack" data-change-id="${escapeHtml(change.id)}" data-subject="${escapeHtml(subject)}" data-type-label="${escapeHtml(label)}" aria-label="${escapeHtml(ackLabel)}">${icon("check")}Acknowledge</button>`;
  }
  const hasDiffValues = (change.old_value !== null && change.old_value !== undefined) || (change.new_value !== null && change.new_value !== undefined);
  const diffLine = hasDiffValues
    ? `<p class="monitor-change__diff"><span class="monitor-change__old">${escapeHtml(change.old_value ?? "—")}</span> <span aria-hidden="true">→</span> <span class="monitor-change__new">${escapeHtml(change.new_value ?? "—")}</span></p>`
    : "";
  return `<li class="monitor-change">
    <span class="monitor-severity monitor-severity--${escapeHtml(change.severity)}" aria-hidden="true">${icon(meta.icon)}</span>
    <div class="monitor-change__body">
      <p class="monitor-change__headline"><span class="visually-hidden">${meta.label}: </span><strong>${escapeHtml(label)}</strong>${subject ? ` <span class="monitor-change__subject">${escapeHtml(subject)}</span>` : ""}</p>
      ${diffLine}
      <p class="monitor-change__meta">${meta.label} · ${formatTimestamp(change.detected_at)}</p>
    </div>
    ${statusMarkup}
  </li>`;
}

function setChangesState(state) {
  current.changesState = state === "all" ? "all" : "open";
  changesVisibleCount = CHANGES_PAGE_SIZE;
  persist();
  loadChangesFeed();
}

async function loadChangesFeed() {
  const el = document.querySelector("#mc-changes");
  if (!el) return;
  el.setAttribute("aria-busy", "true");
  el.innerHTML = sectionSkeleton({ label: "change feed", rows: 5, columns: 2 });
  try {
    let changes;
    if (current.changesState === "open") {
      // The unfiltered "open" list (shared with the site-scope strip/Robots/DNS
      // blocks) is capped at the backend's default page size — on a client with
      // heavy info-level noise (task-35 §1.4: thousands of `content_changed`
      // rows possible per full check), a critical or warning row is not
      // guaranteed to survive that cap purely by chance of insertion order.
      // `severity=critical`/`warning` are always small sets on their own, so
      // fetching them explicitly and merging in guarantees they are never
      // silently truncated away — this is the row the whole feature exists to
      // surface (task-36 DoD: "critical видно з головного екрана", verified
      // live against laxton.se's `page_noindex_appeared`).
      const [general, critical, warning] = await Promise.all([
        fetchOpenChanges(),
        listClientMonitorChanges(current.clientId, { state: "open", severity: "critical", requestKey: `monitor-client-changes-${current.clientId}-critical-strip` }),
        listClientMonitorChanges(current.clientId, { state: "open", severity: "warning", requestKey: `monitor-client-changes-${current.clientId}-warning-strip` }),
      ]);
      const byId = new Map();
      [...critical, ...warning, ...general].forEach((change) => byId.set(change.id, change));
      changes = [...byId.values()];
    } else {
      changes = await listClientMonitorChanges(current.clientId, { state: "all" });
    }
    changesRaw = changes;
    renderChangesFeed();
  } catch (error) {
    if (isAbortError(error)) { settleAbortedSection(el, "Change feed", "retry-changes"); return; }
    el.setAttribute("aria-busy", "false");
    el.innerHTML = `${sectionHeadingWithAction("Change feed", "", "")}<div class="state-panel state-failed">${icon("wifi-off")}<p><strong>Could not load changes.</strong><br>${escapeHtml(error.message)}</p><button type="button" class="button button--primary" data-action="retry-changes">${icon("refresh")}Retry</button></div>`;
  }
}

const CHANGE_FEED_SEVERITY_RANK = { critical: 0, warning: 1, info: 2 };

function renderChangesFeed() {
  const el = document.querySelector("#mc-changes");
  if (!el) return;
  el.setAttribute("aria-busy", "false");
  // Severity first (critical always on top, regardless of timestamp), then
  // most recent within a severity — the DoD's "видно за 5 секунд без
  // прокрутки" only holds if a real critical row cannot end up buried under a
  // page of routine info-level rows sharing the same `detected_at`.
  const ordered = [...changesRaw].sort((a, b) => {
    const rank = (CHANGE_FEED_SEVERITY_RANK[a.severity] ?? 3) - (CHANGE_FEED_SEVERITY_RANK[b.severity] ?? 3);
    return rank !== 0 ? rank : new Date(b.detected_at) - new Date(a.detected_at);
  });
  const toggle = `<div class="monitor-feed-toggle" role="group" aria-label="Change list filter">
    <button type="button" class="chip-button" data-action="changes-state" data-state="open" aria-pressed="${String(current.changesState === "open")}">Open</button>
    <button type="button" class="chip-button" data-action="changes-state" data-state="all" aria-pressed="${String(current.changesState === "all")}">All</button>
  </div>`;
  // Client-side "Load more" (task-37 F6) — a client with hundreds of open
  // `info`-level rows (task-35 §1.4) rendered the whole list in one go,
  // ballooning the page past 11,000px. Same pattern as Pages' pagination:
  // never hide the true count, just cap what is painted at once.
  const visible = ordered.slice(0, changesVisibleCount);
  const loadMore = ordered.length > visible.length
    ? `<div class="monitor-changes-load-more"><p>Showing ${formatCount(visible.length)} of ${formatCount(ordered.length)}</p><button type="button" class="button button--quiet" data-action="changes-load-more">${icon("chevron-down")}Load more</button></div>`
    : "";
  const list = visible.length
    ? `<ul class="monitor-changes" aria-live="polite">${visible.map(changeRow).join("")}</ul>`
    : `<div class="state-panel state-ok"><p><strong>${current.changesState === "open" ? "No open changes." : "No changes recorded."}</strong></p></div>`;
  el.innerHTML = `${sectionHeadingWithAction("Change feed", ordered.length ? `${ordered.length} ${current.changesState === "open" ? "open" : "total"}` : "", "")}${toggle}${list}${loadMore}`;
}

// ---------------------------------------------------------------------------
// Acknowledge (inline note form — no modal, kept local to this file's own
// generated markup rather than a shared dialog)
// ---------------------------------------------------------------------------

function showAckForm(button) {
  const { changeId, subject, typeLabel } = button.dataset;
  const form = document.createElement("form");
  form.className = "monitor-ack-form";
  form.dataset.changeId = changeId;
  form.dataset.subject = subject || "";
  form.dataset.typeLabel = typeLabel || "";
  form.innerHTML = `
    <label class="visually-hidden" for="ack-note-${escapeHtml(changeId)}">Note (optional)</label>
    <input type="text" id="ack-note-${escapeHtml(changeId)}" name="note" placeholder="Optional note" maxlength="500" autocomplete="off">
    <button type="submit" class="button button--quiet">${icon("check")}Confirm</button>
    <button type="button" class="button button--quiet monitor-ack-cancel" data-action="ack-cancel">${icon("x")}Cancel</button>`;
  button.replaceWith(form);
  form.querySelector("input").focus();
}

function restoreAckButton(cancelButton) {
  const form = cancelButton.closest(".monitor-ack-form");
  if (!form) return;
  const { changeId, subject, typeLabel } = form.dataset;
  const label = typeLabel || "";
  const ackLabel = `Acknowledge: ${label} on ${subject || "this change"}`;
  const button = document.createElement("button");
  button.type = "button";
  button.className = "button button--quiet monitor-ack-button";
  button.dataset.action = "ack";
  button.dataset.changeId = changeId;
  button.dataset.subject = subject || "";
  button.dataset.typeLabel = label;
  button.setAttribute("aria-label", ackLabel);
  button.innerHTML = `${icon("check")}Acknowledge`;
  form.replaceWith(button);
}

async function acknowledgeChange(id, note, form) {
  const submitButton = form.querySelector("button[type=submit]");
  if (submitButton) submitButton.disabled = true;
  try {
    await ackMonitorChange(id, note || null);
    toast("Change acknowledged.");
    openChangesPromise = null;
    await loadSiteChangesAndDependents();
    loadDns();
    loadChangesFeed();
  } catch (error) {
    toast(`Could not acknowledge: ${error.message}`);
    if (submitButton) submitButton.disabled = false;
  }
}
