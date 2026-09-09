// URL Inspector (task-61) — reached from the header search field on every
// route, not a nav tab. One operator-pasted page URL -> everything the
// system knows about it: GSC/GA4 series scoped to that URL, its Web Archive
// timeline, Site Monitor state/changes, meta + copy QA, and an on-demand
// live micro technical audit. See doc/dev/new/task-61-url-inspector.md.
//
// Calls are split by MEASURED latency, mirroring the backend (task-88 §2.2):
// stored data paints immediately from `getUrlInspector`; the direct Google
// API waves (~0.9s core, ~0.8s breakdowns, ~0.5s realtime) follow and fill
// the panels that need Google; the two genuinely slow round trips — the live
// page fetch (~20s) and Search Console's URL Inspection (~6.6s) — are their
// own opt-in buttons so they never hold up anything else.
//
// task-88 makes Google the PRIMARY source of this page's search analytics.
// The stored Reporting Ninja series does not disappear: it stays as
// "Stored collection history", because beyond Search Console's 16-month
// ceiling it is the only source we have (§4, «чого Google API не дає»).
import {
  getUrlInspector, runUrlInspectorLive, getUrlScreenshots, captureUrlScreenshots,
  getUrlInspectorGoogle, getUrlInspectorGoogleBreakdowns, getUrlInspectorRealtime,
  runUrlIndexStatus, isAbortError,
} from "./api.js?v=20260909-t88";
import { replaceUrlInspectorHash } from "./router.js?v=20260909-t88";
import { escapeHtml, formatDateRange, formatFileSize, formatTimestamp, icon } from "./formatters.js?v=20260821-spin3";
import { sectionSkeleton } from "./skeleton.js?v=20260821-spin3";

const root = document.querySelector("#url-inspector-page-root");

const GRANULARITIES = [
  { key: "week", label: "Weekly" },
  { key: "month", label: "Monthly" },
  { key: "quarter", label: "Quarterly" },
];

// Вікна прямого Google API (task-88 §2.3). Не те саме, що `GRANULARITIES`
// нижче: ті описують гранулярність ЗБЕРЕЖЕНИХ періодів нерівної довжини,
// ці — точний діапазон запиту, бакет якого ми обираємо самі.
const WINDOWS = [
  { key: "28d", label: "28 days" },
  { key: "90d", label: "90 days" },
  { key: "12m", label: "12 months" },
  { key: "16m", label: "16 months" },
];

// Метрики Google-серії. Ключі — рівно ті, що приходять з API, без локальних
// синонімів (CLAUDE.md: одна назва на одну річ скрізь).
const GOOGLE_GSC_SERIES = [
  { key: "clicks", label: "Clicks" },
  { key: "impressions", label: "Impressions" },
  { key: "ctr", label: "CTR" },
  { key: "position", label: "Position" },
];

const GOOGLE_GA4_SERIES = [
  { key: "sessions", label: "Sessions" },
  { key: "totalUsers", label: "Users" },
  { key: "screenPageViews", label: "Views" },
  { key: "engagementRate", label: "Engagement rate" },
  { key: "keyEvents", label: "Conversions" },
];

// Людські назви полів Google API. Усе, чого тут немає, показується власним
// іменем з API — краще технічна назва, ніж вигадана.
const METRIC_LABELS = {
  clicks: "Clicks",
  impressions: "Impressions",
  ctr: "CTR",
  position: "Avg. position",
  sessions: "Sessions",
  totalUsers: "Users",
  newUsers: "New users",
  activeUsers: "Active users",
  engagedSessions: "Engaged sessions",
  engagementRate: "Engagement rate",
  bounceRate: "Bounce rate",
  averageSessionDuration: "Avg. session",
  userEngagementDuration: "Engagement time",
  screenPageViews: "Views",
  screenPageViewsPerSession: "Views / session",
  keyEvents: "Conversions",
  sessionKeyEventRate: "Conversion rate",
  eventCount: "Events",
  scrolledUsers: "Scrolled 90%",
  active1DayUsers: "Active (1d)",
  active7DayUsers: "Active (7d)",
  active28DayUsers: "Active (28d)",
};

// Що показуємо тайлами, а що лишається в повній таблиці — тайли відповідають
// на «як справи», таблиця на «а точно». Не більше 4-5 KPI на блок
// (frontend/AGENTS.md §4).
const GSC_TILE_METRICS = ["clicks", "impressions", "ctr", "position"];
const GA4_LANDING_TILE_METRICS = ["sessions", "totalUsers", "keyEvents", "engagementRate"];
const GA4_PAGE_TILE_METRICS = ["screenPageViews", "activeUsers", "userEngagementDuration", "eventCount"];

const SEARCH_TYPE_LABELS = {
  web: "Web",
  image: "Images",
  video: "Video",
  news: "News",
  discover: "Discover",
  googleNews: "Google News",
};

// Вердикти URL Inspection API. `robotsTxtState`/`indexingState`, що НЕ
// дозволяють індексацію, — це помилка, а не інформація: сторінка не може
// потрапити в пошук.
const INDEX_VERDICT_TONES = {
  PASS: "ui-tone-success",
  PARTIAL: "ui-tone-warning",
  FAIL: "ui-tone-danger",
  NEUTRAL: "ui-tone-dim",
};

const GSC_METRICS = [
  { key: "clicks", label: "Clicks" },
  { key: "impressions", label: "Impressions" },
  { key: "ctr", label: "CTR" },
  { key: "position", label: "Position" },
];

const GA4_METRICS = [
  { key: "sessions", label: "Sessions" },
  { key: "users", label: "Users" },
  { key: "conversions", label: "Conversions" },
  { key: "views", label: "Views" },
  { key: "engagement_rate", label: "Engagement rate" },
];

// This page's own small state vocabulary — distinct from `STATE_META`
// (formatters.js) since none of `has_data`/`no_data`/`unknown_client` exist
// there, and formatters.js already carries unrelated uncommitted changes
// from another session (kept fully self-contained deliberately).
const SOURCE_STATE_META = {
  has_data: { label: "Data available", icon: "circle-check", tone: "ui-tone-success" },
  no_data: { label: "No data for this URL", icon: "cloud-off", tone: "ui-tone-dim" },
  not_configured: { label: "Not connected", icon: "plug-off", tone: "ui-tone-dim" },
  failed: { label: "Collection error", icon: "circle-x", tone: "ui-tone-danger" },
  unknown_client: { label: "Unknown client", icon: "help-circle", tone: "ui-tone-dim" },
};

const SOURCE_LABELS = {
  gsc: "Search Console",
  ga4: "GA4",
  ga4_cross_check: "GSC↔GA4 check",
  url_inspection: "Index status",
  reporting_ninja: "GA4",
  monitor: "Site Monitor",
  archive: "Web Archive",
  page_inventory: "Meta & Copy",
  tech_audit: "Technical Audit",
  keywords: "Keywords",
  site_queries: "Site Queries",
  screenshots: "Screenshots",
  client: "Client",
};

const SHOT_PRESET_LABELS = { desktop: "Desktop", tablet: "Tablet", mobile: "Mobile" };

const SEVERITY_META = {
  critical: { label: "Critical", tone: "ui-tone-danger" },
  error: { label: "Error", tone: "ui-tone-danger" },
  warning: { label: "Warning", tone: "ui-tone-warning" },
  warn: { label: "Warning", tone: "ui-tone-warning" },
  info: { label: "Info", tone: "ui-tone-info" },
};

const numberFormatter = new Intl.NumberFormat("en-US", { maximumFractionDigits: 2 });
// Частка `0..1` -> відсоток. `Intl` множить на 100 сам, тому значення НІКОЛИ
// не множиться в коді — саме так виникає пастка ×100 (task-83 §7.3).
const percentFormatter = new Intl.NumberFormat("en-US", {
  style: "percent", minimumFractionDigits: 1, maximumFractionDigits: 2,
});
const positionFormatter = new Intl.NumberFormat("en-US", {
  minimumFractionDigits: 1, maximumFractionDigits: 1,
});
const shortDay = new Intl.DateTimeFormat("en-US", { day: "numeric", month: "short" });

let current = null; // { url, granularity, window, gscMetric, ga4Metric, gMetricGsc, gMetricGa4 }
let dto = null;
let loadError = null;
let loading = false;
let requestToken = 0;
let gscChart = null;
let ga4Chart = null;
let googleGscChart = null;
let googleGa4Chart = null;
let liveState = { loading: false, error: null, result: null, withSchema: false };
let shotsState = { loading: false, error: null, result: null };
// Кожна Google-хвиля має власний стан, а не спільний прапорець `loading`:
// вони приходять у різний час, і одна невдала не має вдавати, що решта теж
// не завантажилась (task-88 §2.5).
let googleState = emptyAsyncState();
let breakdownsState = emptyAsyncState();
let indexState = emptyAsyncState();
let realtimeState = emptyAsyncState();

function emptyAsyncState() {
  // `unavailable` окремо від `error`: 409 означає «у цього клієнта немає
  // інтеграції з Google», і це стан панелі, а не збій, який треба лагодити.
  return { loading: false, error: null, unavailable: null, result: null };
}

export async function initUrlInspectorPage(query) {
  const url = query.get("u") || "";
  const granularityParam = query.get("g");
  const granularity = GRANULARITIES.some((g) => g.key === granularityParam) ? granularityParam : "week";
  const windowParam = query.get("w");
  const win = WINDOWS.some((w) => w.key === windowParam) ? windowParam : "90d";
  current = {
    url, granularity, window: win,
    gscMetric: "clicks", ga4Metric: "sessions",
    gMetricGsc: "clicks", gMetricGa4: "sessions",
  };
  dto = null;
  loadError = null;
  loading = false;
  liveState = { loading: false, error: null, result: null, withSchema: false };
  shotsState = { loading: false, error: null, result: null };
  googleState = emptyAsyncState();
  breakdownsState = emptyAsyncState();
  indexState = emptyAsyncState();
  realtimeState = emptyAsyncState();
  destroyCharts();

  if (!url) {
    root.innerHTML = emptyShell();
    return;
  }

  root.innerHTML = shell();
  wireControls();
  await load();
  // Runs once per URL, right after the stored data is in — the operator
  // asked for the live check to happen automatically rather than wait for
  // a click. Skips silently if the initial load itself failed (loadError
  // set) or the URL changed again before this resolved (token guard inside
  // `load()` already handles the latter for `dto`/`render()`). Screenshots'
  // cache read runs alongside it (both fast/instant reads) — a genuine
  // capture never runs automatically (task-64 §5: ~20s, opt-in only).
  if (dto) {
    // Усі швидкі хвилі — паралельно. Index status тут НЕМА свідомо: 6.6 с
    // на автозавантаженні означали б, що сторінка «думає» кожного разу,
    // навіть коли оператор прийшов подивитись трафік (task-88 §0.5/§2.2).
    const tasks = [loadCachedScreenshots(), loadGoogleCore(), loadBreakdowns(), loadRealtime()];
    if (!liveState.result && !liveState.loading) {
      tasks.push(runLiveCheck({ withSchema: false }));
    }
    await Promise.all(tasks);
  }
}

// --- Google API waves ---------------------------------------------------------

// Три однакові за формою завантажувачі, один хелпер: кожен тримає власний
// токен запиту, щоб відповідь на старе вікно не перемалювала нове.
async function loadAsync(state, fetcher, setter, { rerender }) {
  const token = requestToken;
  setter({ ...emptyAsyncState(), loading: true, result: state.result });
  rerender();
  try {
    const result = await fetcher();
    if (token !== requestToken) return;
    setter({ ...emptyAsyncState(), result });
  } catch (error) {
    if (isAbortError(error) || token !== requestToken) return;
    // 409 — «Google не підключений для цього клієнта». Це не помилка, яку
    // оператор мусить лагодити, тому окреме поле й інший тон у UI.
    if (error.status === 409) {
      setter({ ...emptyAsyncState(), unavailable: error.message || "Google isn't connected for this client." });
    } else {
      setter({ ...emptyAsyncState(), error: error.message || "Could not reach Google." });
    }
  }
  rerender();
}

async function loadGoogleCore() {
  await loadAsync(
    googleState,
    () => getUrlInspectorGoogle(current.url, { window: current.window }),
    (next) => { googleState = next; },
    { rerender: renderGoogleSections },
  );
}

async function loadBreakdowns() {
  await loadAsync(
    breakdownsState,
    () => getUrlInspectorGoogleBreakdowns(current.url, { window: current.window }),
    (next) => { breakdownsState = next; },
    { rerender: renderBreakdownsInPlace },
  );
}

async function loadRealtime() {
  await loadAsync(
    realtimeState,
    () => getUrlInspectorRealtime(current.url),
    (next) => { realtimeState = next; },
    { rerender: renderRealtimeInPlace },
  );
}

async function loadIndexStatus() {
  await loadAsync(
    indexState,
    () => runUrlIndexStatus(current.url),
    (next) => { indexState = next; },
    { rerender: renderIndexStatusInPlace },
  );
}

function destroyCharts() {
  gscChart?.destroy();
  ga4Chart?.destroy();
  googleGscChart?.destroy();
  googleGa4Chart?.destroy();
  gscChart = null;
  ga4Chart = null;
  googleGscChart = null;
  googleGa4Chart = null;
}

function emptyShell() {
  return `
    <div class="state-panel">
      <div>${icon("search")}</div>
      <div>
        <p><strong>Paste a page URL in the search field above to inspect it.</strong></p>
        <p>Search analytics, Web Archive history, Site Monitor changes, meta/copy QA, and a live technical check — all for one URL.</p>
      </div>
    </div>`;
}

function shell() {
  return `
    <header class="url-inspector__header" id="ui-header" aria-live="polite" aria-busy="true">
      ${sectionSkeleton({ label: "the URL", heading: false, status: true })}
    </header>
    <section class="url-inspector__body" id="ui-body" aria-live="polite" aria-busy="true">
      ${sectionSkeleton({ label: "this URL's data", rows: 6, columns: 3, chart: true })}
    </section>
    <dialog class="url-inspector__shot-dialog" id="ui-shot-dialog">
      <div class="url-inspector__shot-dialog-head">
        <span id="ui-shot-dialog-caption"></span>
        <button type="button" class="button button--quiet button--small" data-action="close-shot-dialog">
          ${icon("x", "icon icon--tiny")}<span>Close</span>
        </button>
      </div>
      <div class="url-inspector__shot-dialog-body">
        <img id="ui-shot-dialog-img" src="" alt="">
      </div>
    </dialog>`;
}

async function load() {
  const token = ++requestToken;
  loading = true;
  loadError = null;
  try {
    const result = await getUrlInspector(current.url, { granularity: current.granularity });
    if (token !== requestToken) return;
    dto = result;
  } catch (error) {
    if (isAbortError(error)) return;
    if (token !== requestToken) return;
    loadError = error.message || "Could not load this URL.";
    dto = null;
  } finally {
    if (token === requestToken) loading = false;
  }
  render();
}

function render() {
  const header = document.querySelector("#ui-header");
  const body = document.querySelector("#ui-body");
  if (!header || !body) return;

  header.removeAttribute("aria-busy");
  body.removeAttribute("aria-busy");

  if (loadError) {
    header.innerHTML = headerShell(null);
    body.innerHTML = `
      <div class="state-panel">
        <div>${icon("alert-triangle")}</div>
        <div><p><strong>Could not load this URL.</strong><br>${escapeHtml(loadError)}</p></div>
      </div>`;
    return;
  }
  if (!dto) return;

  header.innerHTML = headerShell(dto);
  // Two columns from 1200px (url-inspector.css): the main column holds the
  // data-dense/wide panels (charts, tables, image gallery, timelines); the
  // sidebar holds compact reference cards, sticky so they stay visible
  // while the main column scrolls (task-64 §4).
  // Порядок — за питаннями оператора, від найчастішого до найрідшого:
  // Google-панелі попереду, збережена історія збору позаду (вона потрібна
  // або як фолбек, або за межею 16 місяців), локальні джерела далі.
  body.innerHTML = `
    <div class="url-inspector__main">
      ${googleSectionShells()}
      ${renderSearchAnalytics(dto)}
      ${renderKeywords(dto)}
      ${renderScreenshots(dto)}
      ${renderArchive(dto)}
      ${renderMonitor(dto)}
    </div>
    <aside class="url-inspector__sidebar">
      <section class="url-inspector__section" id="ui-index-status" aria-labelledby="ui-index-heading">
        ${renderIndexStatus()}
      </section>
      ${renderLive()}
      ${renderMeta(dto)}
      ${renderTechAudit(dto)}
    </aside>`;

  requestAnimationFrame(() => {
    renderGscChart();
    renderGa4Chart();
  });
  // Google-хвилі могли вже приїхати (перемикання метрики перемальовує все) —
  // заповнюємо їх одразу, інакше готові дані зникли б до наступного запиту.
  renderGoogleSections();
  renderBreakdownsInPlace();
}

// --- header: identity + coverage strip --------------------------------------

function headerShell(data) {
  const client = data?.client;
  const identity = client
    ? `
      <div class="url-inspector__client">
        ${client.logo_path ? `<img class="url-inspector__logo" src="${escapeHtml(client.logo_path)}" alt="" width="32" height="32">` : ""}
        <div>
          <strong>${escapeHtml(client.name)}</strong>
          <small>${client.match === "subdomain" ? "matched by subdomain" : "matched by domain"} · ${escapeHtml(client.domain)}</small>
        </div>
      </div>`
    : `<div class="url-inspector__client url-inspector__client--unknown">${icon("help-circle")}<span>No client matches this URL's host</span></div>`;

  return `
    <a class="back-link" href="#/">${icon("arrow-left")}<span>Back to dashboard</span></a>
    <div class="url-inspector__identity">
      <p class="eyebrow">URL Inspector</p>
      <p class="url-inspector__url" translate="no">${escapeHtml(data ? data.input_url : current.url)}</p>
      ${identity}
    </div>
    ${data ? renderCoverageChips(data.coverage) : ""}`;
}

function renderCoverageChips(coverage) {
  return `
    <ul class="url-inspector__coverage" aria-label="Data sources for this URL">
      ${coverage.map((item) => {
        const meta = SOURCE_STATE_META[item.state] || SOURCE_STATE_META.no_data;
        const label = SOURCE_LABELS[item.source] || item.source;
        return `
          <li class="url-inspector__chip ${meta.tone}" title="${escapeHtml(item.detail || meta.label)}">
            ${icon(meta.icon, "icon icon--tiny")}<span>${escapeHtml(label)}</span>
          </li>`;
      }).join("")}
    </ul>`;
}

// --- search analytics: GSC + GA4 series -------------------------------------

function renderSearchAnalytics(data) {
  const { search_analytics: sa } = data;
  return `
    <section class="url-inspector__section" aria-labelledby="ui-sa-heading">
      <div class="url-inspector__section-head">
        <h2 id="ui-sa-heading">Stored collection history</h2>
        <div class="url-inspector__granularity" role="group" aria-label="Chart granularity">
          ${GRANULARITIES.map((g) => `
            <button type="button" data-action="granularity" data-value="${g.key}" aria-pressed="${current.granularity === g.key}">${g.label}</button>
          `).join("")}
        </div>
      </div>
      <p class="url-inspector__note">What our own collection stored for this URL, period by period. Windows overlap and vary in length because they follow when collection ran, and rows Reporting Ninja truncated to its top-N are marked as unknown rather than zero. Kept because it reaches further back than Search Console's 16 months — for anything inside that window, the Google panels above are exact.</p>
      ${renderSeriesBlock({
        id: "gsc", title: "Search Console", state: sa.gsc, series: sa.gsc_series,
        metrics: GSC_METRICS, selected: current.gscMetric,
      })}
      ${renderSeriesBlock({
        id: "ga4", title: "GA4 (Reporting Ninja)", state: sa.reporting_ninja, series: sa.ga4_series,
        metrics: GA4_METRICS, selected: current.ga4Metric,
      })}
    </section>`;
}

function renderSeriesBlock({ id, title, state, series, metrics, selected }) {
  const meta = SOURCE_STATE_META[state.state] || SOURCE_STATE_META.no_data;
  if (state.state !== "has_data") {
    return `
      <div class="url-inspector__series" data-series="${id}">
        <h3>${escapeHtml(title)}</h3>
        <div class="state-panel state-panel--compact ${meta.tone}">
          ${icon(meta.icon)}<p>${escapeHtml(state.detail || meta.label)}</p>
        </div>
      </div>`;
  }

  const activeSeries = series[selected];
  const points = activeSeries?.points || [];
  const otherLength = activeSeries?.other_length_periods || 0;

  return `
    <div class="url-inspector__series" data-series="${id}">
      <div class="url-inspector__series-head">
        <h3>${escapeHtml(title)}</h3>
        <div class="url-inspector__metric-picker" role="group" aria-label="${escapeHtml(title)} metric">
          ${metrics.map((m) => `
            <button type="button" data-action="metric" data-series="${id}" data-value="${m.key}" aria-pressed="${selected === m.key}">${m.label}</button>
          `).join("")}
        </div>
      </div>
      ${points.length === 0
        ? `<div class="state-panel state-panel--compact"><div>${icon("cloud-off")}</div><p>No ${current.granularity}ly periods stored for this URL yet.</p></div>`
        : `
          <div class="chart-wrap url-inspector__chart-wrap">
            <canvas id="ui-${id}-canvas" role="img" aria-label="Line chart of ${escapeHtml(selected)} for this URL"></canvas>
          </div>
          ${otherLength ? `<p class="url-inspector__note">${otherLength} more period(s) of a different length exist and are not shown here (never averaged across period lengths).</p>` : ""}
          ${renderSeriesTable(points)}
        `}
    </div>`;
}

function renderSeriesTable(points) {
  const latest = points.at(-1);
  const previous = points.at(-2);
  if (!latest) return "";
  const rows = [latest, previous].filter(Boolean);
  return `
    <div class="url-inspector__table-scroll">
      <table class="metric-table url-inspector__series-table">
        <thead><tr><th>Period</th><th>Value</th></tr></thead>
        <tbody>
          ${rows.map((point) => `
            <tr>
              <td>${escapeHtml(formatDateRange(point.period_start, point.period_end))}</td>
              <td class="${point.value === null ? "metric-null" : ""}">${formatSeriesValue(point)}</td>
            </tr>`).join("")}
        </tbody>
      </table>
    </div>`;
}

function formatSeriesValue(point) {
  if (point.value === null) {
    return point.absence_reason === "truncated"
      ? "Unknown (outside top-N)"
      : "No data";
  }
  return numberFormatter.format(Number(point.value));
}

function chartDataset(points) {
  return {
    borderColor: "#ff4a17",
    backgroundColor: "#ff4a17",
    borderWidth: 2,
    pointRadius: 3,
    pointHoverRadius: 5,
    spanGaps: false,
    data: points.map((p) => (p.value === null ? null : Number(p.value))),
  };
}

function chartLabels(points) {
  return points.map((p) => {
    const start = new Date(`${p.period_start}T12:00:00`);
    const end = new Date(`${p.period_end}T12:00:00`);
    return p.period_start === p.period_end ? shortDay.format(start) : `${start.getDate()}–${shortDay.format(end)}`;
  });
}

function chartOptions(points) {
  return {
    responsive: true,
    maintainAspectRatio: false,
    animation: window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ? false : undefined,
    interaction: { intersect: false, mode: "index" },
    plugins: {
      legend: { display: false },
      tooltip: {
        callbacks: {
          label: (ctx) => {
            const point = points[ctx.dataIndex];
            if (point.value === null) {
              return point.absence_reason === "truncated" ? "Unknown (outside top-N)" : "No data";
            }
            return numberFormatter.format(Number(point.value));
          },
        },
      },
    },
    scales: {
      x: { grid: { color: "#2a2a2a" }, ticks: { color: "#aaa6a0", maxRotation: 0, autoSkip: true, maxTicksLimit: 8 } },
      y: { grid: { color: "#2a2a2a" }, ticks: { color: "#aaa6a0" } },
    },
  };
}

function renderGscChart() {
  gscChart?.destroy();
  gscChart = null;
  const canvas = document.querySelector("#ui-gsc-canvas");
  const points = dto?.search_analytics?.gsc_series?.[current.gscMetric]?.points;
  if (!canvas || !points?.length || !window.Chart) return;
  gscChart = new window.Chart(canvas, {
    type: "line",
    data: { labels: chartLabels(points), datasets: [chartDataset(points)] },
    options: chartOptions(points),
  });
}

function renderGa4Chart() {
  ga4Chart?.destroy();
  ga4Chart = null;
  const canvas = document.querySelector("#ui-ga4-canvas");
  const points = dto?.search_analytics?.ga4_series?.[current.ga4Metric]?.points;
  if (!canvas || !points?.length || !window.Chart) return;
  ga4Chart = new window.Chart(canvas, {
    type: "line",
    data: { labels: chartLabels(points), datasets: [chartDataset(points)] },
    options: chartOptions(points),
  });
}


// --- Google API panels (task-88) ----------------------------------------------
//
// Порядок секцій відповідає порядку питань оператора, а не порядку джерел:
// «скільки трафіку» -> «за якими запитами» -> «що люди робили» -> «хто вони».
// Стан індексації живе в сайдбарі поряд із живою перевіркою — це довідка про
// сторінку, а не метрика.

function googleSectionShells() {
  return `
    <section class="url-inspector__section" id="ui-google-search" aria-labelledby="ui-gsearch-heading" aria-busy="true">
      ${sectionSkeleton({ label: "Search Console data for this URL", rows: 2, columns: 4, chart: true })}
    </section>
    <section class="url-inspector__section" id="ui-google-queries" aria-labelledby="ui-gqueries-heading" aria-busy="true">
      ${sectionSkeleton({ label: "this page's search queries", rows: 4, columns: 4 })}
    </section>
    <section class="url-inspector__section" id="ui-google-traffic" aria-labelledby="ui-gtraffic-heading" aria-busy="true">
      ${sectionSkeleton({ label: "GA4 data for this URL", rows: 2, columns: 4, chart: true })}
    </section>
    <section class="url-inspector__section" id="ui-google-breakdowns" aria-labelledby="ui-gbreak-heading" aria-busy="true">
      ${sectionSkeleton({ label: "audience breakdowns", rows: 4, columns: 3 })}
    </section>`;
}

function renderGoogleSections() {
  const searchEl = root?.querySelector("#ui-google-search");
  const queriesEl = root?.querySelector("#ui-google-queries");
  const trafficEl = root?.querySelector("#ui-google-traffic");
  if (!searchEl || !queriesEl || !trafficEl) return;

  if (googleState.loading && !googleState.result) return;
  for (const el of [searchEl, queriesEl, trafficEl]) el.removeAttribute("aria-busy");

  searchEl.innerHTML = renderGoogleSearch();
  queriesEl.innerHTML = renderGoogleQueries();
  trafficEl.innerHTML = renderGoogleTraffic();
  requestAnimationFrame(() => {
    renderGoogleGscChart();
    renderGoogleGa4Chart();
  });
}

// Спільна плашка для всіх Google-панелей: розрізняє «не підключено»,
// «не завантажилось» і «ще вантажиться» — три різні відповіді, які легко
// злити в одне безлике «немає даних» (frontend/AGENTS.md §6).
function asyncPanel(state, { loadingText }) {
  if (state.loading && !state.result) {
    return `<div class="state-panel state-panel--compact">${icon("loader-2", "icon icon--spin")}<p>${escapeHtml(loadingText)}</p></div>`;
  }
  if (state.unavailable) {
    return `<div class="state-panel state-panel--compact ui-tone-dim">${icon("plug-off")}<p>${escapeHtml(state.unavailable)}</p></div>`;
  }
  if (state.error) {
    return `<div class="state-panel state-panel--compact ui-tone-danger">${icon("alert-triangle")}<p>${escapeHtml(state.error)}</p></div>`;
  }
  return null;
}

function sourceBadge(transport) {
  return transport === "google_api"
    ? `<span class="url-inspector__badge ui-tone-success" title="This client's transport is set to the direct Google API">${icon("brand-google", "icon icon--tiny")}<span>Direct from Google</span></span>`
    : `<span class="url-inspector__badge ui-tone-dim" title="Collection for this client still runs through Reporting Ninja; the panels below are read live from Google either way">${icon("cloud", "icon icon--tiny")}<span>Collection via Reporting Ninja</span></span>`;
}

function windowPicker() {
  return `
    <div class="url-inspector__granularity" role="group" aria-label="Google API window">
      ${WINDOWS.map((w) => `
        <button type="button" data-action="window" data-value="${w.key}" aria-pressed="${current.window === w.key}">${w.label}</button>
      `).join("")}
    </div>`;
}

function windowNote(win) {
  if (!win) return "";
  const range = formatDateRange(win.start, win.end);
  const edge = win.at_gsc_history_edge
    ? ` Search Console keeps about 16 months, so an empty left edge here may be that limit rather than no traffic.`
    : "";
  const clamped = win.clamped_to_gsc_history
    ? ` Asked from ${escapeHtml(win.requested_start)}, Search Console only serves from ${escapeHtml(win.start)}.`
    : "";
  return `<p class="url-inspector__note">${escapeHtml(range)} · ${win.days} days · ${escapeHtml(win.bucket)} buckets.${edge}${clamped}</p>`;
}

// --- Google: Search Console ----------------------------------------------------

function renderGoogleSearch() {
  const head = `
    <div class="url-inspector__section-head">
      <h2 id="ui-gsearch-heading">Search performance</h2>
      ${windowPicker()}
    </div>`;
  const panel = asyncPanel(googleState, { loadingText: "Asking Search Console about this URL…" });
  if (panel) return `${head}${panel}`;

  const data = googleState.result;
  if (!data) return `${head}`;
  const gsc = data.gsc;
  const stateMeta = SOURCE_STATE_META[gsc.state.state] || SOURCE_STATE_META.no_data;

  if (gsc.state.state !== "has_data") {
    return `
      ${head}
      <div class="url-inspector__source-line">${sourceBadge(data.transport)}</div>
      ${windowNote(data.window)}
      <div class="state-panel state-panel--compact ${stateMeta.tone}">
        ${icon(stateMeta.icon)}<p>${escapeHtml(gsc.state.detail || stateMeta.label)}</p>
      </div>`;
  }

  const series = gsc.series[current.gMetricGsc];
  const provisional = (series?.points || []).some((point) => point.provisional);

  return `
    ${head}
    <div class="url-inspector__source-line">
      ${sourceBadge(data.transport)}
      ${renderCrossCheck(data.cross_check)}
    </div>
    ${windowNote(data.window)}
    ${renderMetricTiles(gsc.totals, GSC_TILE_METRICS)}
    <div class="url-inspector__series-head">
      <h3>Trend</h3>
      <div class="url-inspector__metric-picker" role="group" aria-label="Search Console metric">
        ${GOOGLE_GSC_SERIES.map((m) => `
          <button type="button" data-action="g-metric" data-series="gsc" data-value="${m.key}" aria-pressed="${current.gMetricGsc === m.key}">${m.label}</button>
        `).join("")}
      </div>
    </div>
    ${(series?.points || []).length === 0
      ? `<div class="state-panel state-panel--compact"><div>${icon("cloud-off")}</div><p>No days with impressions in this window.</p></div>`
      : `
        <div class="chart-wrap url-inspector__chart-wrap">
          <canvas id="ui-g-gsc-canvas" role="img" aria-label="Chart of ${escapeHtml(metricLabel(current.gMetricGsc))} for this URL"></canvas>
        </div>
        ${provisional ? `<p class="url-inspector__note">Buckets marked provisional include days Google has not finished counting${gsc.first_incomplete_date ? ` (from ${escapeHtml(gsc.first_incomplete_date)})` : ""}.</p>` : ""}`}
`;
}

// Найдешевша й найсильніша перевірка здоров'я на цій сторінці: ті самі
// кліки/покази двома незалежними шляхами Google (task-88 §0.10). Розбіжність
// означає, що зламався НАШ матчинг URL, а не що розійшлись дані.
function renderCrossCheck(check) {
  if (!check || check.matches === null || check.matches === undefined) return "";
  return check.matches
    ? `<span class="url-inspector__badge ui-tone-success" title="Search Console API and GA4's own Search Console metrics return the same clicks and impressions for this URL">${icon("circle-check", "icon icon--tiny")}<span>GSC ↔ GA4 agree</span></span>`
    : `<span class="url-inspector__badge ui-tone-warning" title="Search Console API: ${escapeHtml(String(check.gsc_clicks ?? "—"))} clicks / ${escapeHtml(String(check.gsc_impressions ?? "—"))} impressions. GA4's own Search Console metrics: ${escapeHtml(String(check.ga4_clicks ?? "—"))} / ${escapeHtml(String(check.ga4_impressions ?? "—"))}. A gap points at URL matching, not at the data.">${icon("alert-triangle", "icon icon--tiny")}<span>GSC ↔ GA4 disagree</span></span>`;
}

// --- Google: page queries ------------------------------------------------------

function renderGoogleQueries() {
  const head = `
    <div class="url-inspector__section-head">
      <h2 id="ui-gqueries-heading">Page queries</h2>
      ${googleState.result?.gsc?.queries_total
        ? `<span class="url-inspector__count">${googleState.result.gsc.queries_total} ${googleState.result.gsc.queries_total === 1 ? "query" : "queries"}</span>`
        : ""}
    </div>
    <p class="url-inspector__note">Search Console's own queries <strong>for this page</strong> — clicks, impressions, CTR and average position. Not a text match against the page's content.</p>`;
  const panel = asyncPanel(googleState, { loadingText: "Asking Search Console for this page's queries…" });
  if (panel) return `${head}${panel}`;

  const gsc = googleState.result?.gsc;
  const rows = gsc?.queries || [];
  if (!rows.length) {
    const meta = SOURCE_STATE_META[gsc?.state?.state || "no_data"] || SOURCE_STATE_META.no_data;
    return `
      ${head}
      <div class="state-panel state-panel--compact ${meta.tone}">
        ${icon(meta.icon)}<p>Search Console recorded no queries for this page in this window.</p>
      </div>`;
  }

  return `
    ${head}
    <div class="url-inspector__query-filter">
      <label class="url-inspector__field">
        <span class="url-inspector__field-label">Filter queries</span>
        <input type="search" id="ui-query-filter" placeholder="Type to filter…" autocomplete="off">
      </label>
    </div>
    <div class="url-inspector__table-scroll">
      <table class="metric-table url-inspector__series-table" id="ui-query-table">
        <thead>
          <tr><th>Query</th><th>Clicks</th><th>Impressions</th><th>CTR</th><th>Position</th></tr>
        </thead>
        <tbody>
          ${rows.map((row) => `
            <tr data-query="${escapeHtml(row.query.toLowerCase())}">
              <td class="url-inspector__wrap">${escapeHtml(row.query)}</td>
              <td>${formatMetricValue("count", row.clicks)}</td>
              <td>${formatMetricValue("count", row.impressions)}</td>
              <td>${formatMetricValue("ratio", row.ctr)}</td>
              <td>${formatMetricValue("position", row.position)}</td>
            </tr>`).join("")}
        </tbody>
      </table>
    </div>
    ${gsc.queries_truncated
      ? `<p class="url-inspector__note">Showing the top ${rows.length} of ${gsc.queries_total} queries by clicks.</p>`
      : ""}`;
}

// --- Google: GA4 traffic -------------------------------------------------------

function renderGoogleTraffic() {
  const head = `
    <div class="url-inspector__section-head">
      <h2 id="ui-gtraffic-heading">Traffic &amp; behaviour</h2>
      <span id="ui-realtime">${renderRealtime()}</span>
    </div>`;
  const panel = asyncPanel(googleState, { loadingText: "Asking GA4 about this URL…" });
  if (panel) return `${head}${panel}`;

  const ga4 = googleState.result?.ga4;
  if (!ga4) return head;
  const meta = SOURCE_STATE_META[ga4.state.state] || SOURCE_STATE_META.no_data;
  if (ga4.state.state !== "has_data") {
    return `
      ${head}
      <div class="state-panel state-panel--compact ${meta.tone}">
        ${icon(meta.icon)}<p>${escapeHtml(ga4.state.detail || meta.label)}</p>
      </div>`;
  }

  const series = ga4.series[current.gMetricGa4];
  return `
    ${head}
    <h3>Sessions that entered here</h3>
    <p class="url-inspector__note">GA4's <code>landingPage</code> scope: visits that <strong>started</strong> on this page. Conversions are counted here, because a conversion belongs to a session, not to a page.</p>
    ${renderMetricTiles(ga4.landing_totals, GA4_LANDING_TILE_METRICS)}
    <h3>Views of this page</h3>
    <p class="url-inspector__note">GA4's <code>pagePath</code> scope: everything that happened on this page, wherever the visit began. Different question, different number — neither is more correct.</p>
    ${renderMetricTiles(ga4.page_totals, GA4_PAGE_TILE_METRICS)}
    ${renderKeyEvents(ga4)}
    <div class="url-inspector__series-head">
      <h3>Trend</h3>
      <div class="url-inspector__metric-picker" role="group" aria-label="GA4 metric">
        ${GOOGLE_GA4_SERIES.map((m) => `
          <button type="button" data-action="g-metric" data-series="ga4" data-value="${m.key}" aria-pressed="${current.gMetricGa4 === m.key}">${m.label}</button>
        `).join("")}
      </div>
    </div>
    ${(series?.points || []).length === 0
      ? `<div class="state-panel state-panel--compact"><div>${icon("cloud-off")}</div><p>No days with GA4 data in this window.</p></div>`
      : `<div class="chart-wrap url-inspector__chart-wrap">
          <canvas id="ui-g-ga4-canvas" role="img" aria-label="Chart of ${escapeHtml(metricLabel(current.gMetricGa4))} for this URL"></canvas>
        </div>`}
    ${renderGa4FullTotals(ga4)}`;
}

function renderKeyEvents(ga4) {
  const rows = ga4.key_events || [];
  const configured = ga4.key_event_names || [];
  const configuredNote = configured.length
    ? `<p class="url-inspector__note">Conversions configured in this GA4 property: ${configured.map(escapeHtml).join(", ")}. Only the ones that actually fired on this page are listed — GA4 returns no row for an event that never happened, so a missing name is "no data", not a zero.</p>`
    : "";
  if (!rows.length) {
    return `
      <h3>Conversions</h3>
      ${configuredNote}
      <div class="state-panel state-panel--compact ui-tone-dim">${icon("cloud-off")}<p>No conversions recorded for this page in this window.</p></div>`;
  }
  return `
    <h3>Conversions</h3>
    ${configuredNote}
    <div class="url-inspector__table-scroll">
      <table class="metric-table url-inspector__series-table">
        <thead><tr><th>Event</th><th>Conversions</th><th>Events</th></tr></thead>
        <tbody>
          ${rows.map((row) => `
            <tr>
              <td class="url-inspector__wrap">${escapeHtml(row.name)}</td>
              <td>${formatMetricValue("count", row.key_events)}</td>
              <td>${formatMetricValue("count", row.event_count)}</td>
            </tr>`).join("")}
        </tbody>
      </table>
    </div>`;
}

// Повний набір landing-метрик таблицею — тайлів на 15 полів було б забагато
// (frontend/AGENTS.md §4: 3-4 KPI на блок), а викидати їх шкода: саме тут
// живуть bounce rate, тривалість сесії й активні за 1/7/28 днів.
function renderGa4FullTotals(ga4) {
  const rows = (ga4.landing_totals || []).filter((m) => !GA4_LANDING_TILE_METRICS.includes(m.metric));
  if (!rows.length) return "";
  return `
    <h3>All session metrics</h3>
    <div class="url-inspector__table-scroll">
      <table class="metric-table url-inspector__series-table">
        <thead><tr><th>Metric</th><th>Value</th></tr></thead>
        <tbody>
          ${rows.map((m) => `
            <tr>
              <td class="url-inspector__wrap">${escapeHtml(metricLabel(m.metric))}</td>
              <td class="${m.value === null ? "metric-null" : ""}">${formatMetricValue(m.unit, m.value)}</td>
            </tr>`).join("")}
        </tbody>
      </table>
    </div>`;
}

function renderRealtime() {
  if (realtimeState.loading && !realtimeState.result) return "";
  const result = realtimeState.result;
  if (!result || result.active_users === null || result.active_users === undefined) return "";
  const count = numberFormatter.format(Number(result.active_users));
  return `<span class="url-inspector__badge ui-tone-accent" title="GA4 Realtime has no page filter, so this counts the whole property, not this page">${icon("activity", "icon icon--tiny")}<span>${count} active on site now</span></span>`;
}

function renderRealtimeInPlace() {
  const el = root?.querySelector("#ui-realtime");
  if (el) el.innerHTML = renderRealtime();
}

// --- Google: breakdowns --------------------------------------------------------

function renderBreakdownsInPlace() {
  const el = root?.querySelector("#ui-google-breakdowns");
  if (!el) return;
  if (breakdownsState.loading && !breakdownsState.result) return;
  el.removeAttribute("aria-busy");
  el.innerHTML = renderBreakdowns();
}

function renderBreakdowns() {
  const head = `
    <div class="url-inspector__section-head">
      <h2 id="ui-gbreak-heading">Audience &amp; discovery</h2>
    </div>`;
  const panel = asyncPanel(breakdownsState, { loadingText: "Asking Google for this URL's breakdowns…" });
  if (panel) return `${head}${panel}`;

  const data = breakdownsState.result;
  if (!data) return head;
  const cards = (data.breakdowns || []).map(renderBreakdownCard).join("");
  return `
    ${head}
    ${renderSearchTypeStrip(data.search_types || [])}
    <h3>Who arrived and from where</h3>
    <div class="url-inspector__breakdowns">${cards}</div>`;
}

function renderBreakdownCard(breakdown) {
  const meta = SOURCE_STATE_META[breakdown.state.state] || SOURCE_STATE_META.no_data;
  const sourceTag = breakdown.source === "gsc" ? "Search Console" : "GA4";
  const scopeTag = breakdown.scope ? ` · <code>${escapeHtml(breakdown.scope)}</code>` : "";
  if (breakdown.state.state !== "has_data") {
    return `
      <div class="url-inspector__breakdown">
        <h3>${escapeHtml(breakdown.label)}</h3>
        <p class="url-inspector__breakdown-meta">${escapeHtml(sourceTag)}${scopeTag}</p>
        <div class="state-panel state-panel--compact ${meta.tone}">
          ${icon(meta.icon, "icon icon--tiny")}<p>${escapeHtml(breakdown.state.detail || meta.label)}</p>
        </div>
        ${breakdown.note ? `<p class="url-inspector__note">${escapeHtml(breakdown.note)}</p>` : ""}
      </div>`;
  }
  const metrics = breakdown.rows[0]?.metrics || [];
  return `
    <div class="url-inspector__breakdown">
      <h3>${escapeHtml(breakdown.label)}</h3>
      <p class="url-inspector__breakdown-meta">${escapeHtml(sourceTag)}${scopeTag}</p>
      <div class="url-inspector__table-scroll">
        <table class="metric-table url-inspector__series-table">
          <thead>
            <tr>
              <th>${breakdown.dimensions.map((d) => escapeHtml(dimensionLabel(d))).join(" / ")}</th>
              ${metrics.map((m) => `<th>${escapeHtml(metricLabel(m.metric))}</th>`).join("")}
            </tr>
          </thead>
          <tbody>
            ${breakdown.rows.map((row) => `
              <tr>
                <td class="url-inspector__wrap">${escapeHtml(row.keys.map((k) => k || "(not set)").join(" / "))}</td>
                ${row.metrics.map((m) => `<td class="${m.value === null ? "metric-null" : ""}">${formatMetricValue(m.unit, m.value)}</td>`).join("")}
              </tr>`).join("")}
          </tbody>
        </table>
      </div>
      ${breakdown.truncated ? `<p class="url-inspector__note">Truncated at ${breakdown.row_limit} rows.</p>` : ""}
      ${breakdown.note ? `<p class="url-inspector__note">${escapeHtml(breakdown.note)}</p>` : ""}
    </div>`;
}

// Типи пошуку одним рядком, а не карткою на кожен: п'ять із шести майже
// завжди порожні, і п'ять порожніх карток витіснили б із екрана те, що є.
// Порожній тип лишається видимим — «сторінки немає в Картинках» це відповідь.
function renderSearchTypeStrip(types) {
  if (!types.length) return "";
  return `
    <h3>Where this page appears in Google</h3>
    <p class="url-inspector__note">Search Console counts each surface separately. A surface with no rows means this page does not appear there — that is an answer, so it stays visible instead of disappearing.</p>
    <div class="url-inspector__types" aria-label="Impressions by search type">
      ${types.map((type) => {
        const values = Object.fromEntries(type.metrics.map((m) => [m.metric, m.value]));
        const empty = values.impressions === null || values.impressions === undefined;
        return `
          <div class="url-inspector__type ${empty ? "url-inspector__type--empty" : ""}">
            <span class="url-inspector__type-label">${escapeHtml(SEARCH_TYPE_LABELS[type.search_type] || type.search_type)}</span>
            <span class="url-inspector__type-value">${empty ? "—" : `${formatMetricValue("count", values.impressions)} impr.`}</span>
            <span class="url-inspector__type-sub">${empty ? "not shown here" : `${formatMetricValue("count", values.clicks)} clicks`}</span>
          </div>`;
      }).join("")}
    </div>`;
}

// --- Google: index status (sidebar) --------------------------------------------

function renderIndexStatus() {
  const busy = indexState.loading;
  const head = `
    <div class="url-inspector__section-head">
      <h2 id="ui-index-heading">Index status</h2>
      <button type="button" class="button button--quiet button--small" data-action="check-index" ${busy ? "disabled" : ""}>
        ${icon(busy ? "loader-2" : "search", busy ? "icon icon--tiny icon--spin" : "icon icon--tiny")}
        <span>${busy ? "Checking…" : indexState.result ? "Recheck" : "Check now"}</span>
      </button>
    </div>
    <div id="ui-index-body">${renderIndexBody()}</div>`;
  return head;
}

function renderIndexBody() {
  if (indexState.loading) {
    return `<p class="url-inspector__note">Asking Search Console's URL Inspection API — this one takes about 7 seconds.</p>`;
  }
  const panel = asyncPanel(indexState, { loadingText: "Checking…" });
  if (panel) return panel;
  const data = indexState.result;
  if (!data) {
    return `<p class="url-inspector__note">Search Console's own answer to "is this page indexed": coverage state, last crawl, robots.txt, the canonical Google picked, and rich results. Not fetched automatically — it costs ~7s per URL and Google's quota for it is daily.</p>`;
  }

  const rows = [
    ["Verdict", data.verdict, INDEX_VERDICT_TONES[data.verdict || ""] || ""],
    ["Coverage", data.coverage_state, ""],
    ["Indexing", data.indexing_state, data.indexing_state && data.indexing_state !== "INDEXING_ALLOWED" ? "ui-tone-danger" : ""],
    ["robots.txt", data.robots_txt_state, data.robots_txt_state && data.robots_txt_state !== "ALLOWED" ? "ui-tone-danger" : ""],
    ["Fetch", data.page_fetch_state, data.page_fetch_state && data.page_fetch_state !== "SUCCESSFUL" ? "ui-tone-warning" : ""],
    ["Crawled as", data.crawled_as, ""],
    ["Last crawl", data.last_crawl_time ? formatTimestamp(data.last_crawl_time) : null, ""],
    ["Mobile usability", data.mobile_usability_verdict, ""],
    ["Rich results", data.rich_results_verdict, ""],
  ];

  return `
    <dl class="url-inspector__kv">
      ${rows.map(([label, value, tone]) => `
        <dt>${escapeHtml(label)}</dt>
        <dd class="${value === null || value === undefined ? "metric-null" : escapeHtml(tone)}">${value === null || value === undefined ? "Google didn't say" : escapeHtml(String(value))}</dd>
      `).join("")}
    </dl>
    ${renderCanonicalBlock(data)}
    ${data.rich_result_types?.length
      ? `<p class="url-inspector__note"><strong>Detected:</strong> ${data.rich_result_types.map((r) => `${escapeHtml(r.rich_result_type)} (${r.items})`).join(", ")}</p>`
      : ""}
    ${data.mobile_usability_issues?.length
      ? `<ul class="url-inspector__issues">${data.mobile_usability_issues.map((issue) => `<li>${escapeHtml(issue)}</li>`).join("")}</ul>`
      : ""}
    ${data.referring_urls?.length
      ? `<h3>Internal links Google knows</h3>
         <ul class="url-inspector__links">${data.referring_urls.map((link) => `<li><a href="${escapeHtml(link)}" target="_blank" rel="noopener noreferrer">${escapeHtml(link)}</a></li>`).join("")}</ul>`
      : ""}
    ${renderSitemapsBlock(data)}
    ${data.inspection_result_link
      ? `<p class="url-inspector__note"><a href="${escapeHtml(data.inspection_result_link)}" target="_blank" rel="noopener noreferrer">Open in Search Console</a></p>`
      : ""}`;
}

function renderCanonicalBlock(data) {
  if (data.canonical_mismatch === null || data.canonical_mismatch === undefined) {
    return `<p class="url-inspector__note metric-null">Canonical: Google didn't report both sides, so this is unknown — not a match.</p>`;
  }
  if (!data.canonical_mismatch) {
    return `<p class="url-inspector__note"><span class="url-inspector__badge ui-tone-success">${icon("circle-check", "icon icon--tiny")}<span>Canonical agrees</span></span></p>`;
  }
  return `
    <div class="state-panel state-panel--compact ui-tone-warning">
      ${icon("alert-triangle")}
      <div>
        <p><strong>Google picked a different canonical.</strong></p>
        <p>Google: <span translate="no">${escapeHtml(data.google_canonical || "—")}</span></p>
        <p>Page declares: <span translate="no">${escapeHtml(data.user_canonical || "—")}</span></p>
      </div>
    </div>`;
}

function renderSitemapsBlock(data) {
  const state = data.sitemaps_state;
  if (!state) return "";
  if (state.state !== "has_data") {
    const meta = SOURCE_STATE_META[state.state] || SOURCE_STATE_META.no_data;
    return `<p class="url-inspector__note ${escapeHtml(meta.tone)}">Sitemaps: ${escapeHtml(state.detail || meta.label)}</p>`;
  }
  return `
    <h3>Sitemaps</h3>
    ${data.sitemap_membership?.length
      ? `<p class="url-inspector__note">This URL is listed in: ${data.sitemap_membership.map(escapeHtml).join(", ")}</p>`
      : ""}
    <ul class="url-inspector__links">
      ${data.sitemaps.map((sitemap) => `
        <li>
          <span translate="no">${escapeHtml(sitemap.path)}</span>
          <small>${sitemap.contents.map((c) => `${escapeHtml(c.type)}: ${c.submitted === null ? "—" : numberFormatter.format(c.submitted)} submitted`).join(" · ")}
          ${sitemap.errors ? ` · <span class="ui-tone-danger">${sitemap.errors} errors</span>` : ""}
          ${sitemap.warnings ? ` · <span class="ui-tone-warning">${sitemap.warnings} warnings</span>` : ""}
          ${sitemap.last_downloaded ? ` · read ${escapeHtml(formatTimestamp(sitemap.last_downloaded))}` : ""}</small>
        </li>`).join("")}
    </ul>
    <p class="url-inspector__note">Search Console's API no longer reports how many of those were indexed, so that number is deliberately absent rather than shown as zero.</p>`;
}

function renderIndexStatusInPlace() {
  const button = root?.querySelector('[data-action="check-index"]');
  if (button) {
    button.disabled = indexState.loading;
    button.innerHTML = `
      ${icon(indexState.loading ? "loader-2" : "search", indexState.loading ? "icon icon--tiny icon--spin" : "icon icon--tiny")}
      <span>${indexState.loading ? "Checking…" : indexState.result ? "Recheck" : "Check now"}</span>`;
  }
  const body = root?.querySelector("#ui-index-body");
  if (body) body.innerHTML = renderIndexBody();
}

// --- Google: shared bits -------------------------------------------------------

function renderMetricTiles(metrics, keys) {
  const byKey = Object.fromEntries((metrics || []).map((m) => [m.metric, m]));
  const tiles = keys.map((key) => byKey[key]).filter(Boolean);
  if (!tiles.length) return "";
  return `
    <div class="url-inspector__tiles">
      ${tiles.map((m) => `
        <div class="url-inspector__tile">
          <span class="url-inspector__tile-label">${escapeHtml(metricLabel(m.metric))}</span>
          <strong class="url-inspector__tile-value ${m.value === null ? "metric-null" : ""}">${formatMetricValue(m.unit, m.value)}</strong>
        </div>`).join("")}
    </div>`;
}

function metricLabel(key) {
  return METRIC_LABELS[key] || key;
}

function dimensionLabel(key) {
  const labels = {
    device: "Device",
    country: "Country",
    query: "Query",
    searchAppearance: "Appearance",
    date: "Date",
    sessionDefaultChannelGroup: "Channel",
    firstUserDefaultChannelGroup: "First-touch channel",
    sessionSourceMedium: "Source / medium",
    deviceCategory: "Device",
    city: "City",
    languageCode: "Language",
    newVsReturning: "Visitor",
    pageTitle: "Title",
    eventName: "Event",
    pagePath: "Page",
  };
  return labels[key] || key;
}

// Одиниця вирішує формат, а не сама метрика: частка `0..1` під `count`
// намалювала б «CTR 0.06», а секунди — «694» без одиниці (task-88 units.py).
function formatMetricValue(unit, value) {
  if (value === null || value === undefined) return "—";
  const num = Number(value);
  if (!Number.isFinite(num)) return "—";
  if (unit === "ratio") return `${percentFormatter.format(num)}`;
  if (unit === "position") return positionFormatter.format(num);
  if (unit === "seconds") return formatDuration(num);
  return numberFormatter.format(num);
}

function formatDuration(seconds) {
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const minutes = Math.floor(seconds / 60);
  const rest = Math.round(seconds % 60);
  if (minutes < 60) return rest ? `${minutes}m ${rest}s` : `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  return `${hours}h ${minutes % 60}m`;
}

function googleChartLabels(points) {
  return points.map((point) => {
    const start = new Date(`${point.period_start}T12:00:00`);
    const end = new Date(`${point.period_end}T12:00:00`);
    if (point.period_start === point.period_end) return shortDay.format(start);
    return `${start.getDate()}–${shortDay.format(end)}`;
  });
}

function googleChartOptions(points, unit) {
  return {
    responsive: true,
    maintainAspectRatio: false,
    animation: window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ? false : undefined,
    interaction: { intersect: false, mode: "index" },
    plugins: {
      legend: { display: false },
      tooltip: {
        callbacks: {
          label: (ctx) => {
            const point = points[ctx.dataIndex];
            const text = formatMetricValue(unit, point.value);
            return point.provisional ? `${text} (provisional)` : text;
          },
          afterLabel: (ctx) => formatDateRange(points[ctx.dataIndex].period_start, points[ctx.dataIndex].period_end),
        },
      },
    },
    scales: {
      x: { grid: { color: "#2a2a2a" }, ticks: { color: "#aaa6a0", maxRotation: 0, autoSkip: true, maxTicksLimit: 8 } },
      // Позиція: менша = краща, тому вісь перевернута. Без цього графік
      // «зростає» саме тоді, коли сторінка падає (PLAN §12: напрямок
      // «краще» задає метрика, не знак числа).
      y: {
        reverse: unit === "position",
        grid: { color: "#2a2a2a" },
        ticks: {
          color: "#aaa6a0",
          // Chart.js за замовчуванням форматує підписи локаллю БРАУЗЕРА, і
          // на не-англійській машині вісь малює «0,9» посеред англомовного
          // UI. Формат мусить бути той самий, що в тайлах і таблицях —
          // одна метрика форматується однаково скрізь (AGENTS.md §5).
          callback: (value) => formatMetricValue(unit, value),
        },
      },
    },
  };
}

function googleChartDataset(points) {
  return {
    borderColor: "#ff4a17",
    backgroundColor: "#ff4a17",
    borderWidth: 2,
    pointRadius: 3,
    pointHoverRadius: 5,
    // Провізорні точки — порожнім колом: значення справжнє, але доба ще не
    // закрита, і малювати його як повноцінне означало б показати «падіння»,
    // якого нема.
    pointStyle: points.map((point) => (point.provisional ? "circle" : "circle")),
    pointBackgroundColor: points.map((point) => (point.provisional ? "#0e0e0e" : "#ff4a17")),
    spanGaps: false,
    data: points.map((point) => (point.value === null ? null : Number(point.value))),
  };
}

function renderGoogleGscChart() {
  googleGscChart?.destroy();
  googleGscChart = null;
  const canvas = root?.querySelector("#ui-g-gsc-canvas");
  const series = googleState.result?.gsc?.series?.[current.gMetricGsc];
  if (!canvas || !series?.points?.length || !window.Chart) return;
  googleGscChart = new window.Chart(canvas, {
    type: "line",
    data: { labels: googleChartLabels(series.points), datasets: [googleChartDataset(series.points)] },
    options: googleChartOptions(series.points, series.unit),
  });
}

function renderGoogleGa4Chart() {
  googleGa4Chart?.destroy();
  googleGa4Chart = null;
  const canvas = root?.querySelector("#ui-g-ga4-canvas");
  const series = googleState.result?.ga4?.series?.[current.gMetricGa4];
  if (!canvas || !series?.points?.length || !window.Chart) return;
  googleGa4Chart = new window.Chart(canvas, {
    type: "line",
    data: { labels: googleChartLabels(series.points), datasets: [googleChartDataset(series.points)] },
    options: googleChartOptions(series.points, series.unit),
  });
}

// --- keywords & queries -------------------------------------------------------

function renderKeywords(data) {
  const { keywords } = data;
  const onPageMeta = SOURCE_STATE_META[keywords.state.state] || SOURCE_STATE_META.no_data;
  return `
    <section class="url-inspector__section" aria-labelledby="ui-keywords-heading">
      <h2 id="ui-keywords-heading">Keywords &amp; queries</h2>
      <h3>On-page terms</h3>
      ${keywords.state.state !== "has_data"
        ? `<div class="state-panel state-panel--compact ${onPageMeta.tone}">${icon(onPageMeta.icon)}<p>${escapeHtml(keywords.state.detail || onPageMeta.label)}</p></div>`
        : renderOnPageTerms(keywords)}
      <h3>Site queries matched to this page</h3>
      <p class="url-inspector__note">Site-level Search Console queries whose text fully matches this page's content — NOT this page's own search queries. Google does not expose per-page queries through this integration.</p>
      ${renderSiteQueries(keywords)}
    </section>`;
}

function renderOnPageTerms(keywords) {
  const rows = keywords.on_page;
  const badges = (t) => [t.in_title && "Title", t.in_h1 && "H1", t.in_description && "Desc"].filter(Boolean).join(", ");
  return `
    ${keywords.meta_keywords.length ? `<p class="url-inspector__note"><strong>Meta keywords:</strong> ${keywords.meta_keywords.map(escapeHtml).join(", ")}</p>` : ""}
    ${rows.length === 0
      ? `<p class="url-inspector__note">No extractable terms.</p>`
      : `<div class="url-inspector__table-scroll">
          <table class="metric-table url-inspector__series-table">
            <thead><tr><th>Term</th><th>Count</th><th>Where</th></tr></thead>
            <tbody>
              ${rows.map((t) => `
                <tr>
                  <td class="url-inspector__wrap">${escapeHtml(t.term)}${t.is_phrase ? " <small>(phrase)</small>" : ""}</td>
                  <td>${t.count}</td>
                  <td>${escapeHtml(badges(t) || "—")}</td>
                </tr>`).join("")}
            </tbody>
          </table>
        </div>`}`;
}

function renderSiteQueries(keywords) {
  const meta = SOURCE_STATE_META[keywords.queries_state.state] || SOURCE_STATE_META.no_data;
  if (keywords.queries_state.state !== "has_data") {
    return `<div class="state-panel state-panel--compact ${meta.tone}">${icon(meta.icon)}<p>${escapeHtml(keywords.queries_state.detail || meta.label)}</p></div>`;
  }
  return `
    <div class="url-inspector__table-scroll">
      <table class="metric-table url-inspector__series-table">
        <thead><tr><th>Query</th><th>Clicks</th><th>Impressions</th><th></th></tr></thead>
        <tbody>
          ${keywords.site_queries.map((q) => `
            <tr>
              <td class="url-inspector__wrap">${escapeHtml(q.query)}</td>
              <td>${q.clicks ?? "—"}</td>
              <td>${q.impressions ?? "—"}</td>
              <td>${q.is_brand ? `<span class="url-inspector__badge ui-tone-accent">Brand</span>` : ""}</td>
            </tr>`).join("")}
        </tbody>
      </table>
    </div>`;
}

// --- screenshots ---------------------------------------------------------------

function renderScreenshots(data) {
  const { screenshots } = data;
  const hasCache = screenshots.state.state === "has_data";
  return `
    <section class="url-inspector__section" aria-labelledby="ui-shots-heading">
      <div class="url-inspector__section-head">
        <h2 id="ui-shots-heading">Screenshots</h2>
        <button type="button" class="button button--quiet button--small" data-action="capture-shots" ${shotsState.loading ? "disabled" : ""}>
          ${icon(shotsState.loading ? "loader-2" : "camera", shotsState.loading ? "icon icon--tiny icon--spin" : "icon icon--tiny")}
          <span>${shotsState.loading ? "Capturing…" : hasCache ? "Recapture" : "Capture screenshots"}</span>
        </button>
      </div>
      <div id="ui-shots-body">${renderShotsBody(data)}</div>
    </section>`;
}

function renderShotsBody(data) {
  if (shotsState.loading) {
    return `<p class="url-inspector__note">Rendering desktop, tablet, and mobile screenshots — this can take up to 20s.</p>`;
  }
  if (shotsState.error) {
    return `<div class="state-panel state-panel--compact ui-tone-danger">${icon("alert-triangle")}<p>${escapeHtml(shotsState.error)}</p></div>`;
  }
  const shots = shotsState.result?.screenshots || [];
  if (!shots.length) {
    const meta = SOURCE_STATE_META[data.screenshots.state.state] || SOURCE_STATE_META.no_data;
    return `<div class="state-panel state-panel--compact ${meta.tone}">${icon(meta.icon)}<p>${escapeHtml(data.screenshots.state.detail || "No screenshots captured for this URL yet.")}</p></div>`;
  }
  return `<div class="url-inspector__shots">${shots.map(renderShotCard).join("")}</div>`;
}

function renderShotCard(shot) {
  const label = SHOT_PRESET_LABELS[shot.preset] || shot.preset;
  if (shot.state !== "ok") {
    return `
      <figure class="url-inspector__shot url-inspector__shot--failed">
        <figcaption>${escapeHtml(label)}</figcaption>
        <div class="state-panel state-panel--compact ui-tone-danger">${icon("alert-triangle")}<p>${escapeHtml(shot.error || "Capture failed.")}</p></div>
      </figure>`;
  }
  return `
    <figure class="url-inspector__shot" data-action="open-shot" data-image="${escapeHtml(shot.image_url || "")}" data-caption="${escapeHtml(label)}" tabindex="0" role="button" aria-label="Open full-size ${escapeHtml(label)} screenshot">
      <div class="url-inspector__shot-frame">
        <img src="${escapeHtml(shot.thumb_url || shot.image_url || "")}" alt="${escapeHtml(label)} screenshot of this page" loading="lazy">
      </div>
      <figcaption>
        <span>${escapeHtml(label)}</span>
        <small>${shot.width}×${shot.height}px${shot.truncated ? " (clipped)" : ""} · ${formatFileSize(shot.byte_size || 0)} · fold at ${shot.fold_height}px</small>
      </figcaption>
    </figure>`;
}

// --- Web Archive -------------------------------------------------------------

function renderArchive(data) {
  const { archive } = data;
  const meta = SOURCE_STATE_META[archive.state.state] || SOURCE_STATE_META.no_data;
  return `
    <section class="url-inspector__section" aria-labelledby="ui-archive-heading">
      <h2 id="ui-archive-heading">Web Archive</h2>
      ${archive.state.state !== "has_data"
        ? `<div class="state-panel state-panel--compact ${meta.tone}">${icon(meta.icon)}<p>${escapeHtml(archive.state.detail || meta.label)}</p></div>`
        : archive.snapshots.length === 0
          ? `<div class="state-panel state-panel--compact"><div>${icon("cloud-off")}</div><p>Tracked, but no snapshot has been captured yet.</p></div>`
          : `
            <ul class="url-inspector__list">
              ${archive.snapshots.map((s) => `
                <li class="url-inspector__row">
                  <span class="url-inspector__row-time">${escapeHtml(formatTimestamp(s.observed_at))}</span>
                  <span class="url-inspector__row-main">
                    ${s.title ? escapeHtml(s.title) : "<em>Untitled</em>"}
                    <small>${s.http_status ?? "—"} · ${escapeHtml(s.store_reason)}</small>
                  </span>
                  ${s.has_change ? `<span class="url-inspector__badge ui-tone-accent">${icon("history", "icon icon--tiny")} Changed</span>` : ""}
                  <a class="button button--quiet button--small" href="#/archive/view?snapshot=${encodeURIComponent(s.id)}">${icon("external-link", "icon icon--tiny")} View</a>
                </li>`).join("")}
            </ul>
            ${archive.snapshot_count > archive.snapshots.length ? `<p class="url-inspector__note">Showing ${archive.snapshots.length} of ${archive.snapshot_count} snapshots.</p>` : ""}
          `}
    </section>`;
}

// --- Site Monitor ------------------------------------------------------------

function renderMonitor(data) {
  const { monitor } = data;
  const meta = SOURCE_STATE_META[monitor.state.state] || SOURCE_STATE_META.no_data;
  if (monitor.state.state !== "has_data") {
    return `
      <section class="url-inspector__section" aria-labelledby="ui-monitor-heading">
        <h2 id="ui-monitor-heading">Site Monitor</h2>
        <div class="state-panel state-panel--compact ${meta.tone}">${icon(meta.icon)}<p>${escapeHtml(monitor.state.detail || meta.label)}</p></div>
      </section>`;
  }

  return `
    <section class="url-inspector__section" aria-labelledby="ui-monitor-heading">
      <h2 id="ui-monitor-heading">Site Monitor</h2>
      <div class="url-inspector__table-scroll">
        <table class="metric-table url-inspector__facts">
          <tbody>
            <tr><td>Status</td><td>${monitor.status ?? "—"}</td></tr>
            <tr><td>Indexable</td><td>${formatBool(monitor.indexable)}</td></tr>
            <tr><td>Canonical</td><td class="url-inspector__wrap">${monitor.canonical ? escapeHtml(monitor.canonical) : "—"}</td></tr>
            <tr><td>Last checked</td><td>${escapeHtml(formatTimestamp(monitor.last_checked_at))}</td></tr>
            ${monitor.gone_since ? `<tr><td>Gone since</td><td>${escapeHtml(formatTimestamp(monitor.gone_since))}</td></tr>` : ""}
          </tbody>
        </table>
      </div>
      <h3>Changes ${monitor.changes_total ? `(${monitor.changes.length} of ${monitor.changes_total})` : ""}</h3>
      ${monitor.changes.length === 0
        ? `<p class="url-inspector__note">No changes recorded for this URL.</p>`
        : `<ul class="url-inspector__list">
            ${monitor.changes.map((c) => renderChangeRow(c)).join("")}
          </ul>`}
      <h3>Versions ${monitor.versions_total ? `(${monitor.versions.length} of ${monitor.versions_total})` : ""}</h3>
      ${monitor.versions.length === 0
        ? `<p class="url-inspector__note">No versions recorded.</p>`
        : `<ul class="url-inspector__list url-inspector__list--compact">
            ${monitor.versions.map((v) => `
              <li class="url-inspector__row">
                <span class="url-inspector__row-time">${escapeHtml(formatTimestamp(v.observed_at))}</span>
                <span class="url-inspector__row-main">HTTP ${v.http_status ?? "—"}${v.changed_fields.length ? ` · ${v.changed_fields.map(escapeHtml).join(", ")}` : ""}</span>
              </li>`).join("")}
          </ul>`}
    </section>`;
}

function renderChangeRow(change) {
  const severity = SEVERITY_META[change.severity] || SEVERITY_META.info;
  return `
    <li class="url-inspector__row ${severity.tone}">
      <span class="url-inspector__badge">${escapeHtml(severity.label)}</span>
      <span class="url-inspector__row-main">
        ${escapeHtml(change.change_type)}
        ${change.old_value || change.new_value
          ? `<small>${change.old_value ? `<span class="url-inspector__old">${escapeHtml(change.old_value)}</span> → ` : ""}${change.new_value ? escapeHtml(change.new_value) : ""}</small>`
          : ""}
      </span>
      <span class="url-inspector__row-time">${escapeHtml(formatTimestamp(change.detected_at))}</span>
    </li>`;
}

// --- Meta & copy -------------------------------------------------------------

// Довжина title/description у символах — те, за чим оператор тягнеться
// найчастіше, дивлячись на ці два поля. Показуємо ЛИШЕ число: чи вона
// завелика, вже каже блок findings нижче, який проганяє ті самі правила
// `app/pageinventory/qa.py`, що й портфельний Page Inventory (метаqa.py) — з
// порогами з `app/contentqa/policy_bridge.py`. Прошити свої 60/160 тут
// означало б завести другу, розбіжну думку про ту саму метрику.
//
// Рахуємо `Array.from(...)`, а не `.length`: у рядку `.length` рахує UTF-16
// code units, тож емодзі чи пара сурогатів дали б завищене число саме там,
// де оператор дивиться на межу.
function withLength(value) {
  if (!value) return "<em>Missing</em>";
  const chars = Array.from(String(value)).length;
  return `${escapeHtml(value)} <small class="url-inspector__len">${chars} chars</small>`;
}

function renderMeta(data) {
  const { meta: metaData } = data;
  const meta = SOURCE_STATE_META[metaData.state.state] || SOURCE_STATE_META.no_data;
  if (metaData.state.state !== "has_data") {
    return `
      <section class="url-inspector__section" aria-labelledby="ui-meta-heading">
        <h2 id="ui-meta-heading">Meta &amp; copy</h2>
        <div class="state-panel state-panel--compact ${meta.tone}">${icon(meta.icon)}<p>${escapeHtml(metaData.state.detail || meta.label)}</p></div>
      </section>`;
  }

  return `
    <section class="url-inspector__section" aria-labelledby="ui-meta-heading">
      <h2 id="ui-meta-heading">Meta &amp; copy <small class="url-inspector__source-tag">from ${metaData.meta_source === "page_inventory" ? "Page Inventory" : "Site Monitor"}</small></h2>
      <div class="url-inspector__table-scroll">
        <table class="metric-table url-inspector__facts">
          <tbody>
            <tr><td>Title</td><td class="url-inspector__wrap">${withLength(metaData.title)}</td></tr>
            <tr><td>Description</td><td class="url-inspector__wrap">${withLength(metaData.description)}</td></tr>
            <tr><td>H1</td><td class="url-inspector__wrap">${metaData.h1 ? escapeHtml(metaData.h1) : "<em>Missing</em>"}</td></tr>
            ${metaData.keywords.length ? `<tr><td>Keywords</td><td class="url-inspector__wrap">${metaData.keywords.map(escapeHtml).join(", ")}</td></tr>` : ""}
          </tbody>
        </table>
      </div>
      ${renderFindings(metaData.findings)}
    </section>`;
}

function renderFindings(findings) {
  if (!findings.length) return `<p class="url-inspector__note">No QA findings.</p>`;
  return `
    <ul class="url-inspector__list url-inspector__list--compact">
      ${findings.map((f) => {
        const severity = SEVERITY_META[f.severity] || SEVERITY_META.info;
        return `
          <li class="url-inspector__row ${severity.tone}">
            <span class="url-inspector__badge">${escapeHtml(severity.label)}</span>
            <span class="url-inspector__row-main">${escapeHtml(f.message)}<small>${escapeHtml(f.field)} · ${escapeHtml(f.check)}</small></span>
          </li>`;
      }).join("")}
    </ul>`;
}

// --- Technical audit ---------------------------------------------------------

function renderTechAudit(data) {
  const { tech_audit: audit } = data;
  const meta = SOURCE_STATE_META[audit.state.state] || SOURCE_STATE_META.no_data;
  if (audit.state.state !== "has_data") {
    return `
      <section class="url-inspector__section" aria-labelledby="ui-audit-heading">
        <h2 id="ui-audit-heading">Technical audit</h2>
        <div class="state-panel state-panel--compact ${meta.tone}">${icon(meta.icon)}<p>${escapeHtml(audit.state.detail || meta.label)}</p></div>
      </section>`;
  }
  return `
    <section class="url-inspector__section" aria-labelledby="ui-audit-heading">
      <h2 id="ui-audit-heading">Technical audit${audit.audit_date ? ` <small class="url-inspector__source-tag">as of ${escapeHtml(audit.audit_date)}</small>` : ""}</h2>
      ${audit.findings.length === 0
        ? `<p class="url-inspector__note">No open findings for this URL in the latest crawl.</p>`
        : `<ul class="url-inspector__list url-inspector__list--compact">
            ${audit.findings.map((f) => {
              const severity = SEVERITY_META[(f.severity || "").toLowerCase()] || SEVERITY_META.info;
              return `
                <li class="url-inspector__row ${severity.tone}">
                  <span class="url-inspector__badge">${escapeHtml(f.severity || "—")}</span>
                  <span class="url-inspector__row-main">
                    ${f.category ? escapeHtml(f.category) : escapeHtml(f.check)}
                    ${f.detail ? `<small>${escapeHtml(f.detail)}</small>` : ""}
                  </span>
                </li>`;
            }).join("")}
          </ul>`}
    </section>`;
}

// --- live micro technical audit ----------------------------------------------

function renderLive() {
  return `
    <section class="url-inspector__section url-inspector__live" aria-labelledby="ui-live-heading">
      <div class="url-inspector__section-head">
        <h2 id="ui-live-heading">Live audit</h2>
        <div class="url-inspector__live-controls">
          <label class="url-inspector__checkbox">
            <input type="checkbox" id="ui-live-schema" ${liveState.withSchema ? "checked" : ""}>
            <span>Also check schema markup</span>
          </label>
          <button type="button" class="button button--quiet button--small" data-action="run-live" ${liveState.loading ? "disabled" : ""}>
            ${icon(liveState.loading ? "loader-2" : "refresh", liveState.loading ? "icon icon--tiny icon--spin" : "icon icon--tiny")}
            ${liveState.loading ? "Checking…" : "Recheck"}
          </button>
        </div>
      </div>
      <div id="ui-live-result">${renderLiveResult()}</div>
    </section>`;
}

function renderLiveResult() {
  if (liveState.loading) {
    return `<p class="url-inspector__note">Fetching the live page — this can take up to 20s on a slow or protected site.</p>`;
  }
  if (liveState.error) {
    return `<div class="state-panel state-panel--compact ui-tone-danger">${icon("alert-triangle")}<p>${escapeHtml(liveState.error)}</p></div>`;
  }
  const result = liveState.result;
  if (!result) return `<p class="url-inspector__note">Not run yet.</p>`;

  if (result.verdict === "blocked") {
    return `
      <div class="state-panel state-panel--compact ui-tone-warning">
        ${icon("shield-off")}
        <p><strong>Blocked by the site's bot protection.</strong>${result.error ? ` ${escapeHtml(result.error)}` : ""}</p>
      </div>
      ${renderHopChain(result.redirect_chain)}`;
  }
  if (result.verdict !== "ok") {
    return `
      <div class="state-panel state-panel--compact ui-tone-danger">
        ${icon("circle-x")}
        <p><strong>Live check failed (${escapeHtml(result.verdict)}).</strong>${result.error ? ` ${escapeHtml(result.error)}` : ""}</p>
      </div>`;
  }

  const s = result.signals;
  return `
    ${renderHopChain(result.redirect_chain)}
    <div class="url-inspector__table-scroll">
      <table class="metric-table url-inspector__facts">
        <tbody>
          <tr><td>HTTP status</td><td>${result.http_status ?? "—"}</td></tr>
          <tr><td>Indexable</td><td>${formatBool(result.indexable)}</td></tr>
          <tr><td>Title</td><td class="url-inspector__wrap">${withLength(s?.title)}</td></tr>
          <tr><td>Description</td><td class="url-inspector__wrap">${withLength(s?.description)}</td></tr>
          <tr><td>H1</td><td class="url-inspector__wrap">${s?.h1 ? escapeHtml(s.h1) : "<em>Missing</em>"}</td></tr>
          <tr><td>Canonical</td><td class="url-inspector__wrap">${s?.canonical ? escapeHtml(s.canonical) : "—"}</td></tr>
          <tr><td>Meta robots</td><td>${s?.meta_robots ? escapeHtml(s.meta_robots) : "—"}</td></tr>
          <tr><td>Word count</td><td>${s?.word_count ?? "—"}</td></tr>
          ${s?.json_ld_types?.length ? `<tr><td>JSON-LD types</td><td class="url-inspector__wrap">${s.json_ld_types.map(escapeHtml).join(", ")}</td></tr>` : ""}
        </tbody>
      </table>
    </div>
    ${renderFindings(result.findings)}
    ${result.schema_check_id
      ? `<p class="url-inspector__note">${icon("shield-check", "icon icon--tiny")} Schema markup check saved — <a href="#/tools/schema-markup/${encodeURIComponent(result.schema_check_id)}">view full result</a>.</p>`
      : ""}`;
}

function renderHopChain(hops) {
  if (!hops?.length) return "";
  return `
    <ol class="url-inspector__hops">
      ${hops.map((h) => `<li><span>${h.status}</span><span class="url-inspector__wrap" translate="no">${escapeHtml(h.url)}</span></li>`).join("")}
    </ol>`;
}

function formatBool(value) {
  if (value === null || value === undefined) return "<em>Unknown</em>";
  return value ? "Yes" : "No";
}

// --- wiring -------------------------------------------------------------

async function loadCachedScreenshots() {
  shotsState = { ...shotsState, loading: true, error: null };
  renderShotsInPlace();
  try {
    const result = await getUrlScreenshots(current.url);
    shotsState = { loading: false, error: null, result };
  } catch (error) {
    if (isAbortError(error)) return;
    shotsState = { loading: false, error: error.message || "Could not load screenshots.", result: null };
  }
  renderShotsInPlace();
}

async function captureScreenshots() {
  shotsState = { ...shotsState, loading: true, error: null };
  renderShotsInPlace();
  try {
    const result = await captureUrlScreenshots(current.url, { force: true });
    shotsState = { loading: false, error: null, result };
  } catch (error) {
    if (isAbortError(error)) return;
    shotsState = { ...shotsState, loading: false, error: error.message || "Screenshot capture failed." };
  }
  renderShotsInPlace();
}

function renderShotsInPlace() {
  const button = root.querySelector('[data-action="capture-shots"]');
  if (button) {
    const hasCache = !!shotsState.result?.screenshots?.length;
    button.disabled = shotsState.loading;
    button.innerHTML = `
      ${icon(shotsState.loading ? "loader-2" : "camera", shotsState.loading ? "icon icon--tiny icon--spin" : "icon icon--tiny")}
      <span>${shotsState.loading ? "Capturing…" : hasCache ? "Recapture" : "Capture screenshots"}</span>`;
  }
  const bodyEl = root.querySelector("#ui-shots-body");
  if (bodyEl && dto) bodyEl.innerHTML = renderShotsBody(dto);
}

async function runLiveCheck({ withSchema }) {
  liveState = { ...liveState, loading: true, error: null, withSchema };
  renderLiveInPlace();
  try {
    const result = await runUrlInspectorLive(current.url, { withSchema });
    liveState = { ...liveState, loading: false, result };
  } catch (error) {
    if (isAbortError(error)) return;
    liveState = { ...liveState, loading: false, error: error.message || "Live check failed." };
  }
  renderLiveInPlace();
}

function wireControls() {
  root.addEventListener("click", async (event) => {
    const granularityButton = event.target.closest('[data-action="granularity"]');
    if (granularityButton) {
      current.granularity = granularityButton.dataset.value;
      replaceUrlInspectorHash({ u: current.url, g: current.granularity });
      root.querySelector("#ui-header")?.setAttribute("aria-busy", "true");
      root.querySelector("#ui-body")?.setAttribute("aria-busy", "true");
      await load();
      return;
    }

    const windowButton = event.target.closest('[data-action="window"]');
    if (windowButton) {
      current.window = windowButton.dataset.value;
      replaceUrlInspectorHash({ u: current.url, g: current.granularity, w: current.window });
      // Перемальовуємо ті самі секції в стан завантаження й тягнемо обидві
      // швидкі хвилі заново — вікно змінює і серію, і кожен зріз.
      await Promise.all([loadGoogleCore(), loadBreakdowns()]);
      return;
    }

    const googleMetricButton = event.target.closest('[data-action="g-metric"]');
    if (googleMetricButton) {
      const seriesId = googleMetricButton.dataset.series;
      if (seriesId === "gsc") current.gMetricGsc = googleMetricButton.dataset.value;
      if (seriesId === "ga4") current.gMetricGa4 = googleMetricButton.dataset.value;
      renderGoogleSections();
      return;
    }

    if (event.target.closest('[data-action="check-index"]')) {
      await loadIndexStatus();
      return;
    }

    const metricButton = event.target.closest('[data-action="metric"]');
    if (metricButton) {
      const seriesId = metricButton.dataset.series;
      if (seriesId === "gsc") current.gscMetric = metricButton.dataset.value;
      if (seriesId === "ga4") current.ga4Metric = metricButton.dataset.value;
      render();
      return;
    }

    const liveButton = event.target.closest('[data-action="run-live"]');
    if (liveButton) {
      await runLiveCheck({ withSchema: !!root.querySelector("#ui-live-schema")?.checked });
      return;
    }

    const captureButton = event.target.closest('[data-action="capture-shots"]');
    if (captureButton) {
      await captureScreenshots();
      return;
    }

    const shotCard = event.target.closest('[data-action="open-shot"]');
    if (shotCard) {
      openShotDialog(shotCard.dataset.image, shotCard.dataset.caption);
      return;
    }

    if (event.target.closest('[data-action="close-shot-dialog"]')) {
      root.querySelector("#ui-shot-dialog")?.close();
      return;
    }

    // Native `<dialog>` backdrop click: only the dialog element itself (not
    // its content, which is `event.target` when a child was clicked) is the
    // backdrop area — closing on it is the expected click-outside-to-close.
    if (event.target.id === "ui-shot-dialog") {
      event.target.close();
    }
  });

  // Фільтр таблиці запитів — чисто клієнтський: усі рядки вже в DOM, і
  // ходити за ними в Google на кожну літеру означало б палити квоту заради
  // того, що браузер робить безкоштовно.
  root.addEventListener("input", (event) => {
    const input = event.target.closest("#ui-query-filter");
    if (!input) return;
    const needle = input.value.trim().toLowerCase();
    const rows = root.querySelectorAll("#ui-query-table tbody tr");
    for (const row of rows) {
      row.hidden = needle !== "" && !(row.dataset.query || "").includes(needle);
    }
  });

  root.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" && event.key !== " ") return;
    const shotCard = event.target.closest('[data-action="open-shot"]');
    if (!shotCard) return;
    event.preventDefault();
    openShotDialog(shotCard.dataset.image, shotCard.dataset.caption);
  });
}

function openShotDialog(imageUrl, caption) {
  if (!imageUrl) return;
  const dialog = root.querySelector("#ui-shot-dialog");
  const img = root.querySelector("#ui-shot-dialog-img");
  const captionEl = root.querySelector("#ui-shot-dialog-caption");
  if (!dialog || !img) return;
  img.src = imageUrl;
  img.alt = caption ? `${caption} screenshot, full size` : "Screenshot, full size";
  if (captionEl) captionEl.textContent = caption || "";
  dialog.showModal();
}

function renderLiveInPlace() {
  const button = root.querySelector('[data-action="run-live"]');
  if (button) {
    button.disabled = liveState.loading;
    button.innerHTML = `${icon(liveState.loading ? "loader-2" : "refresh", liveState.loading ? "icon icon--tiny icon--spin" : "icon icon--tiny")} ${liveState.loading ? "Checking…" : "Recheck"}`;
  }
  const resultEl = root.querySelector("#ui-live-result");
  if (resultEl) resultEl.innerHTML = renderLiveResult();
}
