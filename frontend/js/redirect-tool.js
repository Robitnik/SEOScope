/* Redirect Checker (task-29).
 *
 * Робота оператора: вставити посилання → побачити ланцюжок редіректів **підряд,
 * хоп за хопом** (статус → куди повів → що змінилося) → зрозуміти вердикт.
 * Одне посилання трасується одразу й нічого не створює; список стає перевіркою
 * з історією, яку сторінка проганяє по рядку (прогрес у БД, тож F5 не губить).
 *
 * Вердикт, прапорці й розбір хопів приходять із бекенда — тут лише рендер і
 * керування чергою прогону (AGENTS.md §5, §12).
 */
import {
  createRedirectCheck,
  deleteRedirectCheck,
  getDashboard,
  getRedirectCheck,
  getRedirectChecks,
  getRedirectTargets,
  redirectsExportCsvUrl,
  redirectsExportXlsxUrl,
  runRedirectTarget,
  traceRedirect,
} from "./api.js?v=20260727-redirects1";
import { buildToolsHash, replaceToolsHash } from "./router.js?v=20260727-redirects1";
import { panelSkeleton } from "./skeleton.js?v=20260727-redirects1";
import { escapeHtml, formatTimestamp, icon } from "./formatters.js?v=20260727-redirects1";

const PAGE_SIZE = 50;
const TOOL_KEY = "redirects";
// Скільки рядків проганяємо одночасно. Сервер додатково тримає один запит на
// хост, тож це стеля для різних хостів, а не спосіб «прискорити» один сайт.
const CONCURRENCY = 3;

const VERDICT_META = {
  ok_direct: { label: "No redirect", icon: "circle-check", tone: "good" },
  ok_redirect: { label: "One redirect", icon: "arrow-right-circle", tone: "good" },
  chain: { label: "Redirect chain", icon: "stack-2", tone: "warn" },
  loop: { label: "Redirect loop", icon: "refresh-alert", tone: "bad" },
  too_many_hops: { label: "Too many hops", icon: "alert-triangle", tone: "bad" },
  broken: { label: "Dead end", icon: "circle-x", tone: "bad" },
  broken_chain: { label: "No Location header", icon: "unlink", tone: "bad" },
  blocked: { label: "Blocked by the site", icon: "shield-lock", tone: "warn" },
  unsupported_scheme: { label: "Not traceable", icon: "help-circle", tone: "warn" },
  unsafe_url: { label: "Refused: private address", icon: "shield-x", tone: "bad" },
  fetch_failed: { label: "Could not load", icon: "wifi-off", tone: "warn" },
};

// Прапорці — сигнали для людини, тому кожен має пояснення тим, що з ним робити.
const FLAG_COPY = {
  temporary_redirect: {
    label: "Temporary redirect",
    hint: "A 302/307 tells search engines the move is not final. Use 301 for a permanent move.",
  },
  mixed_permanence: {
    label: "Mixed 301 and 302",
    hint: "The chain mixes permanent and temporary hops — usually leftovers from two migrations.",
  },
  protocol_downgrade: {
    label: "HTTPS → HTTP",
    hint: "A hop drops back to plain HTTP. Fix the rule so the chain stays on HTTPS.",
  },
  host_change: { label: "Different host", hint: "The chain leaves the host it started on." },
  www_change: {
    label: "www changes",
    hint: "One extra hop only to add or drop www — point the first rule at the final host.",
  },
  trailing_slash_only: {
    label: "Slash-only hop",
    hint: "The only difference is a trailing slash. Link to the final form to remove the hop.",
  },
  case_changed: { label: "Letter case changes", hint: "The path case differs between hops." },
  query_dropped: {
    label: "Query dropped",
    hint: "Tracking or filter parameters are lost on the way — campaign data will not arrive.",
  },
  redirect_to_homepage: {
    label: "Lands on the homepage",
    hint: "A deep link ends on the homepage: visitors lose the page they asked for.",
  },
  meta_refresh_used: {
    label: "Meta refresh",
    hint: "Part of the chain is an HTML meta refresh, not an HTTP redirect. Slower and weaker for SEO.",
  },
  multiple_location_headers: {
    label: "Several Location headers",
    hint: "The server sent more than one Location; we followed the first one.",
  },
  canonical_mismatch: {
    label: "Canonical points elsewhere",
    hint: "The destination names a different canonical URL, so it may not be the page that ranks.",
  },
  noindex_destination: {
    label: "Destination is noindex",
    hint: "The final page asks search engines not to index it.",
  },
  possible_js_redirect: {
    label: "Possible JS redirect",
    hint: "The HTML assigns location in a script — a browser may end up somewhere else than shown here.",
  },
  normalized_input: {
    label: "Link was normalized",
    hint: "We added the scheme or dropped a fragment before requesting it.",
  },
  cache_buster_used: {
    label: "Cache-buster added",
    hint: "A random parameter was appended, so a query-dependent redirect may differ from a plain visit.",
  },
  cache_buster_broke_the_url: {
    label: "Cache-buster broke the URL",
    hint: "With the cache-busting parameter the site answered 404/403, so the link was re-checked without it. This result is the clean one.",
  },
};

const CHANGE_COPY = {
  scheme_http_to_https: "http → https",
  scheme_https_to_http: "https → http",
  scheme_change: "scheme changed",
  host_change: "different host",
  www_added: "www added",
  www_removed: "www removed",
  port_change: "port changed",
  path_change: "different path",
  trailing_slash_added: "slash added",
  trailing_slash_removed: "slash removed",
  case_changed: "letter case changed",
  query_dropped: "query dropped",
  query_added: "query added",
  query_changed: "query changed",
};

const RUN_STATUS_LABEL = {
  queued: "Not checked",
  running: "Checking…",
  done: "Checked",
  failed: "Check failed",
};

let root = null;
let state = null;
let requestToken = 0;
let clientOptions = null;
let currentRows = [];
// Черга прогону: живе поза рендером, щоб перемальовування таблиці її не рвало.
let runner = { active: 0, stop: false, pending: [], total: 0, done: 0 };

export async function initTool(mount, route) {
  root = mount;
  requestToken += 1;
  runner = { active: 0, stop: true, pending: [], total: 0, done: 0 };
  if (route.reportId) {
    state = {
      checkId: route.reportId,
      q: route.query.get("q") || "",
      verdict: route.query.get("vd") || "",
      flag: route.query.get("fl") || "",
      runStatus: route.query.get("rs") || "",
      offset: Number(route.query.get("off") || 0) || 0,
    };
    await renderCheckView();
    return;
  }
  state = null;
  await renderListView();
}

function persist() {
  replaceToolsHash(TOOL_KEY, state.checkId, {
    q: state.q,
    vd: state.verdict,
    fl: state.flag,
    rs: state.runStatus,
    off: state.offset || "",
  });
}

function toast(message) {
  const region = document.querySelector("#toast-region");
  const element = document.createElement("div");
  element.className = "toast-message";
  element.textContent = message;
  region.append(element);
  setTimeout(() => element.remove(), 4200);
}

// --- list view: paste links + history ------------------------------------------

async function renderListView() {
  root.innerHTML = `
    <a class="back-link" href="${buildToolsHash()}">${icon("arrow-left")}<span>All tools</span></a>
    <header class="tool-header">
      <p class="eyebrow">Tools / Redirect Checker</p>
      <h1>Redirect checker</h1>
      <p class="tool-lead">Paste a link to see every hop it goes through — status, destination and what
        changed on the way. A browser hides those middle steps; this does not.</p>
    </header>
    <section class="tool-panel" id="rd-new" aria-labelledby="rd-new-title">
      <div class="section-heading"><div><h2 id="rd-new-title">Check links</h2>
        <p class="tool-panel__hint">One URL per line. A single link is traced right away; a list becomes a
          saved check you can come back to.</p></div></div>
      <div class="tool-panel__body" id="rd-new-body"></div>
    </section>
    <section class="tool-panel" id="rd-checks" aria-labelledby="rd-checks-title">
      <div class="section-heading"><div><h2 id="rd-checks-title">Saved checks</h2></div></div>
      <div class="tool-panel__body" id="rd-checks-body">
        ${panelSkeleton({ label: "checks", rows: 3, columns: 3 })}
      </div>
    </section>`;
  renderForm();
  await loadChecks();
}

function renderForm() {
  const body = root.querySelector("#rd-new-body");
  body.innerHTML = `
    <form class="rd-form" id="rd-form" novalidate>
      <label class="form-field rd-form__links" for="rd-urls">Links
        <textarea id="rd-urls" name="urls" rows="5" spellcheck="false" autocomplete="off"
          placeholder="https://old.example.com/page"></textarea>
      </label>
      <p class="field-help" id="rd-count" aria-live="polite">Paste one URL per line.</p>
      <div class="rd-form__grid">
        <label class="form-field">Title <small>(optional)</small>
          <input id="rd-title" name="title" maxlength="200" placeholder="Named automatically">
        </label>
        <label class="form-field">Client <small>(optional)</small>
          <select id="rd-client"><option value="">No client</option></select>
        </label>
      </div>
      <details class="rd-options">
        <summary>Request options</summary>
        <div class="rd-options__grid">
          <label class="form-field">Stop after
            <input id="rd-max-hops" type="number" inputmode="numeric" min="1" max="25" value="20">
            <small>redirects</small>
          </label>
          <label class="checkbox-field">
            <input id="rd-meta" type="checkbox" checked>
            <span>Follow <code>meta refresh</code> redirects</span>
          </label>
          <label class="checkbox-field">
            <input id="rd-buster" type="checkbox">
            <span>Add a cache-busting parameter
              <small>Off by default: it changes the URL, and some sites redirect based on the query.</small></span>
          </label>
        </div>
      </details>
      <p class="form-error" id="rd-form-error" role="alert"></p>
      <footer class="rd-form__actions">
        <button class="button button--primary" type="submit" id="rd-submit">Trace link</button>
        <p class="rd-form__note">${icon("info-circle", "icon icon--tiny")}Requested from this server with a
          desktop browser user agent and no cookies. Geo- or user-agent-based redirects can differ.</p>
      </footer>
    </form>
    <div id="rd-quick" class="rd-quick" aria-live="polite"></div>`;

  const textarea = body.querySelector("#rd-urls");
  textarea.addEventListener("input", updateFormCounter);
  body.querySelector("#rd-form").addEventListener("submit", (event) => {
    event.preventDefault();
    void submitForm();
  });
  void fillClientOptions(body.querySelector("#rd-client"));
  updateFormCounter();
}

// Легкий клієнтський підрахунок лише для підпису кнопки й лічильника; істину
// (нормалізацію, дедуплікацію, відкинуті рядки) завжди рахує бекенд.
function parseLines(value) {
  return String(value)
    .split(/\n+/)
    .map((line) => line.trim())
    .filter(Boolean);
}

function updateFormCounter() {
  const lines = parseLines(root.querySelector("#rd-urls").value);
  const counter = root.querySelector("#rd-count");
  const submit = root.querySelector("#rd-submit");
  if (!lines.length) {
    counter.textContent = "Paste one URL per line.";
    submit.textContent = "Trace link";
    return;
  }
  counter.textContent = lines.length === 1
    ? "1 link — traced immediately, nothing is saved."
    : `${lines.length} lines — saved as a check so progress survives a reload.`;
  submit.textContent = lines.length === 1 ? "Trace link" : `Check ${lines.length} links`;
}

function readOptions() {
  const hops = Number(root.querySelector("#rd-max-hops").value) || 20;
  return {
    max_hops: Math.max(1, Math.min(25, hops)),
    follow_meta_refresh: root.querySelector("#rd-meta").checked,
    cache_buster: root.querySelector("#rd-buster").checked,
  };
}

async function submitForm() {
  const errorField = root.querySelector("#rd-form-error");
  const submit = root.querySelector("#rd-submit");
  const label = submit.textContent;
  const lines = parseLines(root.querySelector("#rd-urls").value);
  errorField.textContent = "";
  if (!lines.length) {
    errorField.textContent = "Paste at least one link.";
    root.querySelector("#rd-urls").focus();
    return;
  }

  submit.disabled = true;
  submit.innerHTML = `${icon("loader-2", "icon icon--spin")}${escapeHtml(label)}`;
  try {
    if (lines.length === 1) {
      const trace = await traceRedirect(lines[0], readOptions());
      renderQuickTrace(trace);
      return;
    }
    const check = await createRedirectCheck({
      urls: lines,
      title: root.querySelector("#rd-title").value.trim() || null,
      client_id: root.querySelector("#rd-client").value || null,
      options: readOptions(),
    });
    location.hash = buildToolsHash(TOOL_KEY, check.id);
  } catch (error) {
    errorField.textContent = error.message;
  } finally {
    submit.disabled = false;
    submit.textContent = label;
  }
}

function renderQuickTrace(trace) {
  const quick = root.querySelector("#rd-quick");
  quick.innerHTML = `
    <article class="rd-quick__card">
      <header class="rd-quick__head">
        <div>
          <p class="eyebrow">Traced now · not saved</p>
          <h3 class="rd-quick__url">${escapeHtml(trace.input_url)}</h3>
        </div>
        ${verdictBadge(trace.verdict)}
      </header>
      ${chainDetail(trace, { idPrefix: "quick" })}
      <footer class="rd-quick__actions">
        <button class="button button--quiet" type="button" data-action="copy-chain">${icon("copy")}Copy chain</button>
        <button class="button button--quiet" type="button" data-action="save-check">${icon("plus")}Save as check</button>
      </footer>
    </article>`;
  quick.querySelector("[data-action='copy-chain']").addEventListener("click", (event) =>
    void copyChain(event.currentTarget, trace));
  quick.querySelector("[data-action='save-check']").addEventListener("click", () => void saveQuickAsCheck(trace));
  quick.querySelector(".rd-quick__card").scrollIntoView({ block: "nearest", behavior: "smooth" });
}

async function saveQuickAsCheck(trace) {
  try {
    const check = await createRedirectCheck({
      urls: [trace.input_url],
      title: root.querySelector("#rd-title").value.trim() || null,
      client_id: root.querySelector("#rd-client").value || null,
      options: readOptions(),
    });
    location.hash = buildToolsHash(TOOL_KEY, check.id);
  } catch (error) {
    toast(`Could not save the check: ${error.message}`);
  }
}

async function fillClientOptions(select) {
  if (!select) return;
  try {
    if (clientOptions === null) {
      const dashboard = await getDashboard({ period: "week", requestKey: "redirects-clients" });
      clientOptions = (dashboard.clients || []).map((client) => ({
        id: client.client_id,
        name: client.name,
      }));
    }
    clientOptions.forEach((client) => {
      const option = document.createElement("option");
      option.value = client.id;
      option.textContent = client.name;
      select.append(option);
    });
  } catch {
    // Привʼязка до клієнта опційна — перевірка працює й без списку клієнтів.
    select.insertAdjacentHTML("beforeend", '<option value="" disabled>Client list unavailable</option>');
  }
}

async function loadChecks() {
  const token = ++requestToken;
  const body = root.querySelector("#rd-checks-body");
  try {
    const checks = await getRedirectChecks();
    if (token !== requestToken) return;
    if (!checks.length) {
      body.innerHTML = `
        <p class="tool-empty">${icon("file-off")}<span>No saved checks yet. Paste a list of links above to
          create the first one.</span></p>`;
      return;
    }
    body.innerHTML = `
      <div class="table-scroll">
        <table class="tool-table">
          <caption class="visually-hidden">Saved redirect checks</caption>
          <thead><tr>
            <th scope="col">Check</th><th scope="col">Client</th><th scope="col">Links</th>
            <th scope="col">Checked</th><th scope="col">Findings</th><th scope="col">Created</th>
            <th scope="col"><span class="visually-hidden">Actions</span></th>
          </tr></thead>
          <tbody>${checks.map(checkRow).join("")}</tbody>
        </table>
      </div>`;
    applyMeters(body);
    body.querySelectorAll("button[data-action='delete']").forEach((button) => {
      button.addEventListener("click", () => void removeCheck(button.dataset.checkId, button.dataset.title));
    });
  } catch (error) {
    if (token !== requestToken) return;
    body.innerHTML = `
      <div class="state-panel state-failed">${icon("wifi-off")}
        <p><strong>Could not load saved checks.</strong><br>${escapeHtml(error.message)}</p>
        <button class="button button--primary" type="button" id="rd-checks-retry">Try again</button></div>`;
    body.querySelector("#rd-checks-retry").addEventListener("click", () => void loadChecks());
  }
}

function checkRow(check) {
  const runCounts = check.run_status_counts || {};
  const done = (runCounts.done || 0) + (runCounts.failed || 0);
  const total = check.target_count || 0;
  const href = buildToolsHash(TOOL_KEY, check.id);
  return `
    <tr>
      <td class="tool-table__main">
        <a href="${href}">${escapeHtml(check.title)}</a>
        <span class="report-row__source">${escapeHtml(optionsSummary(check.options))}</span>
      </td>
      <td>${check.client_name
        ? `<span class="report-row__client">${icon("building-store", "icon icon--tiny")}${escapeHtml(check.client_name)}</span>`
        : '<span class="muted">No client</span>'}</td>
      <td class="num">${total}${check.duplicate_count
        ? `<small title="Duplicate lines removed before checking">${check.duplicate_count} duplicate${check.duplicate_count === 1 ? "" : "s"} removed</small>`
        : ""}</td>
      <td class="report-row__progress">
        <span class="meter" role="img" aria-label="${done} of ${total} links checked">
          <span class="meter__done" data-pct="${total ? Math.round((done / total) * 100) : 0}"></span>
        </span>
        <span class="report-row__counts"><strong>${done}</strong>/${total}</span>
      </td>
      <td class="rd-findings">${findingChips(check.verdict_counts)}</td>
      <td class="report-row__date">${escapeHtml(formatTimestamp(check.created_at, "—"))}</td>
      <td class="tool-table__actions">
        <a class="button button--quiet" href="${href}">Open${icon("arrow-right", "icon icon--tiny")}</a>
        <a class="icon-button" href="${redirectsExportCsvUrl(check.id)}"
          aria-label="${escapeHtml(`Export ${check.title} as CSV`)}">${icon("download")}</a>
        <button class="icon-button" type="button" data-action="delete" data-check-id="${escapeHtml(check.id)}"
          data-title="${escapeHtml(check.title)}"
          aria-label="${escapeHtml(`Delete check ${check.title}`)}">${icon("trash")}</button>
      </td>
    </tr>`;
}

function optionsSummary(options) {
  if (!options) return "";
  const parts = [`up to ${options.max_hops} hops`];
  if (!options.follow_meta_refresh) parts.push("no meta refresh");
  if (options.cache_buster) parts.push("cache-buster on");
  return parts.join(" · ");
}

// Порожні лічильники — це «ще не перевіряли», а не «нуль проблем» (PLAN §12).
function findingChips(verdictCounts) {
  const counts = verdictCounts || {};
  const entries = Object.entries(counts).filter(([, value]) => value > 0);
  if (!entries.length) return '<span class="muted">Not checked yet</span>';
  const order = ["broken", "broken_chain", "loop", "too_many_hops", "unsafe_url", "blocked",
    "fetch_failed", "chain", "unsupported_scheme", "ok_redirect", "ok_direct"];
  return entries
    .sort((a, b) => order.indexOf(a[0]) - order.indexOf(b[0]))
    .map(([verdict, value]) => {
      const meta = VERDICT_META[verdict] || { label: verdict, tone: "warn", icon: "help-circle" };
      return `<span class="rd-chip rd-chip--${meta.tone}">${icon(meta.icon, "icon icon--tiny")}${value} ${escapeHtml(meta.label.toLocaleLowerCase("en-US"))}</span>`;
    })
    .join("");
}

// Ширину смуги ставимо через CSSOM: інлайновий `style="width:…"` блокує CSP
// (`default-src 'self'` без `unsafe-inline`) — урок task-27.
function applyMeters(container) {
  container.querySelectorAll(".meter [data-pct]").forEach((bar) => {
    bar.style.width = `${Number(bar.dataset.pct) || 0}%`;
  });
}

async function removeCheck(checkId, title) {
  if (!window.confirm(`Delete “${title}” and all of its rows? This cannot be undone.`)) return;
  try {
    await deleteRedirectCheck(checkId);
    toast(`Deleted ${title}`);
    await loadChecks();
  } catch (error) {
    toast(`Could not delete the check: ${error.message}`);
  }
}

// --- check view ---------------------------------------------------------------

async function renderCheckView() {
  root.innerHTML = `
    <a class="back-link" href="${buildToolsHash(TOOL_KEY)}">${icon("arrow-left")}<span>All checks</span></a>
    <header class="tool-header" id="rd-header" aria-live="polite">
      ${panelSkeleton({ label: "the check", rows: 2, columns: 2 })}
    </header>
    <section class="tool-panel" id="rd-rows" aria-labelledby="rd-rows-title">
      <div class="section-heading"><div><h2 id="rd-rows-title">Links</h2></div></div>
      <div class="tool-panel__body" id="rd-rows-body">
        ${panelSkeleton({ label: "links", rows: 6, columns: 4 })}
      </div>
    </section>`;
  bindDelegatedHandlers(root.querySelector("#rd-rows-body"));
  await loadCheck();
}

// Свідомо БЕЗ авто-старту прогону при відкритті: кожен рядок — це реальний
// запит до чужого сайту, тож він починається з кліку оператора, а не з того,
// що хтось відкрив сторінку (або натиснув F5).
async function loadCheck() {
  const token = ++requestToken;
  try {
    const check = await getRedirectCheck(state.checkId);
    if (token !== requestToken) return;
    renderCheckHeader(check);
    await loadTargets();
  } catch (error) {
    if (token !== requestToken) return;
    if (error.status === 404) {
      root.querySelector("#rd-header").innerHTML = `
        <div class="state-panel state-failed">${icon("file-off")}
          <p><strong>Check not found.</strong><br>It may have been deleted.</p>
          <a class="button button--primary" href="${buildToolsHash(TOOL_KEY)}">Back to checks</a></div>`;
      root.querySelector("#rd-rows")?.remove();
      return;
    }
    root.querySelector("#rd-header").innerHTML = `
      <p class="state-panel state-failed">${icon("wifi-off")}<span><strong>Could not load the check.</strong><br>${escapeHtml(error.message)}</span></p>`;
  }
}

function renderCheckHeader(check) {
  const runCounts = check.run_status_counts || {};
  const checked = (runCounts.done || 0) + (runCounts.failed || 0);
  const total = check.target_count || 0;
  const queued = runCounts.queued || 0;
  const busy = runner.active > 0;
  root.querySelector("#rd-header").innerHTML = `
    <p class="eyebrow">Tools / Redirect Checker</p>
    <h1>${escapeHtml(check.title)}</h1>
    <p class="tool-meta">
      ${check.client_name ? `<span>${icon("building-store", "icon icon--tiny")}${escapeHtml(check.client_name)}</span>` : ""}
      <span>${icon("clock", "icon icon--tiny")}Created ${escapeHtml(formatTimestamp(check.created_at, "—"))}</span>
      <span>${icon("route", "icon icon--tiny")}${escapeHtml(optionsSummary(check.options))}</span>
    </p>
    <div class="at-progress">
      <p class="at-progress__counts"><strong>${checked}</strong> of <strong>${total}</strong> links checked
        ${queued ? `<span class="muted">· ${queued} waiting</span>` : ""}
        ${runCounts.failed ? `<span class="muted">· ${runCounts.failed} could not run</span>` : ""}</p>
      <progress max="${total}" value="${checked}" aria-label="Links checked: ${checked} of ${total}"></progress>
      <p class="rd-findings rd-findings--header">${findingChips(check.verdict_counts)}</p>
      ${check.duplicate_count ? `<p class="at-progress__note">${icon("info-circle", "icon icon--tiny")}${check.duplicate_count} duplicate line(s) were removed before checking.</p>` : ""}
      ${check.truncated ? `<p class="at-progress__note">${icon("alert-triangle", "icon icon--tiny")}The pasted list was longer than 200 links, so only the first 200 were kept.</p>` : ""}
      ${(check.rejected || []).length ? `<details class="rd-rejected">
        <summary>${check.rejected.length} line(s) were not links</summary>
        <ul>${check.rejected.map((row) => `<li><code>${escapeHtml(row.input_url)}</code> — ${escapeHtml(row.message)}</li>`).join("")}</ul>
      </details>` : ""}
    </div>
    <div class="at-export rd-actions">
      <button class="button button--primary" type="button" id="rd-run"${queued || busy ? "" : " disabled"}
        title="Checks the unchecked links on this page, three at a time">
        ${busy ? `${icon("loader-2", "icon icon--spin")}Checking ${runner.done}/${runner.total}…` : `${icon("player-play")}Check remaining`}
      </button>
      <button class="button button--quiet" type="button" id="rd-stop"${busy ? "" : " disabled"}>${icon("player-stop")}Stop</button>
      <button class="button button--quiet" type="button" id="rd-rerun-failed">${icon("refresh")}Re-check problems</button>
      <a class="button button--quiet" href="${redirectsExportCsvUrl(check.id)}">${icon("download")}Export CSV</a>
      <a class="button button--quiet" href="${redirectsExportXlsxUrl(check.id)}">${icon("download")}Export XLSX</a>
    </div>`;
  root.querySelector("#rd-run").addEventListener("click", () => startRun());
  root.querySelector("#rd-stop").addEventListener("click", () => stopRun());
  root.querySelector("#rd-rerun-failed").addEventListener("click", () => void rerunProblems());
}

async function loadTargets() {
  const token = ++requestToken;
  const body = root.querySelector("#rd-rows-body");
  try {
    const page = await getRedirectTargets(state.checkId, {
      q: state.q,
      verdict: state.verdict,
      flag: state.flag,
      runStatus: state.runStatus,
      limit: PAGE_SIZE,
      offset: state.offset,
    });
    if (token !== requestToken) return;
    renderTargets(body, page);
  } catch (error) {
    if (token !== requestToken) return;
    body.innerHTML = `
      <div class="state-panel state-failed">${icon("wifi-off")}
        <p><strong>Could not load the links.</strong><br>${escapeHtml(error.message)}</p>
        <button class="button button--primary" type="button" id="rd-rows-retry">Try again</button></div>`;
    body.querySelector("#rd-rows-retry").addEventListener("click", () => void loadTargets());
  }
}

function renderTargets(body, page) {
  currentRows = page.rows;
  const shown = page.rows.length;
  const from = shown ? state.offset + 1 : 0;
  body.innerHTML = `
    ${toolbar(page)}
    ${shown ? `
      <div class="table-scroll">
        <table class="tool-table rd-table">
          <caption class="visually-hidden">Links, redirect chains and verdicts</caption>
          <thead><tr>
            <th scope="col">#</th>
            <th scope="col">Link</th>
            <th scope="col">Chain</th>
            <th scope="col">Destination</th>
            <th scope="col">Verdict</th>
            <th scope="col"><span class="visually-hidden">Actions</span></th>
          </tr></thead>
          <tbody>${page.rows.map(targetRow).join("")}</tbody>
        </table>
      </div>
      <div class="pagination">
        <button class="button button--quiet" type="button" data-page="prev"${state.offset === 0 ? " disabled" : ""}>Previous</button>
        <span>${from}–${state.offset + shown} of ${page.total}</span>
        <button class="button button--quiet" type="button" data-page="next"${state.offset + shown >= page.total ? " disabled" : ""}>Next</button>
      </div>`
    : `<p class="tool-empty">${icon("search-off")}<span>No link matches these filters.
        <button class="link-button" type="button" data-action="clear-filters">Clear filters</button></span></p>`}`;
  bindRowControls(body);
}

function toolbar(page) {
  const option = (value, label, selected) =>
    `<option value="${escapeHtml(value)}"${selected === value ? " selected" : ""}>${escapeHtml(label)}</option>`;
  const usedFlags = [...new Set(currentRows.flatMap((row) => row.flags || []))];
  const flagChoices = state.flag && !usedFlags.includes(state.flag) ? [...usedFlags, state.flag] : usedFlags;
  return `
    <div class="analytics-toolbar rd-toolbar">
      <label class="search-field">
        <span class="visually-hidden">Search links</span>
        ${icon("search")}
        <input id="rd-search" type="search" placeholder="Link or destination" value="${escapeHtml(state.q)}" autocomplete="off">
      </label>
      <label class="sort-field">Verdict
        <select id="rd-filter-verdict">
          ${option("", "All", state.verdict)}
          ${Object.entries(VERDICT_META).map(([key, meta]) => option(key, meta.label, state.verdict)).join("")}
        </select>
      </label>
      <label class="sort-field">Signal
        <select id="rd-filter-flag">
          ${option("", "All", state.flag)}
          ${flagChoices.map((flag) => option(flag, FLAG_COPY[flag]?.label || flag, state.flag)).join("")}
        </select>
      </label>
      <label class="sort-field">Progress
        <select id="rd-filter-run">
          ${option("", "All", state.runStatus)}
          ${Object.entries(RUN_STATUS_LABEL).map(([key, label]) => option(key, label, state.runStatus)).join("")}
        </select>
      </label>
      <p class="result-count" aria-live="polite">${page.total} link${page.total === 1 ? "" : "s"}</p>
    </div>`;
}

function targetRow(target) {
  const detailId = `rd-detail-${target.id}`;
  const checked = Boolean(target.checked_at);
  return `
    <tr data-target-id="${escapeHtml(target.id)}" class="rd-row">
      <td class="num at-row-index">${target.row_index}</td>
      <td class="rd-link">
        <span class="rd-link__text" title="${escapeHtml(target.input_url)}">${escapeHtml(target.input_url)}</span>
        <span class="at-actions">
          <a class="icon-button" href="${escapeHtml(target.normalized_url)}" target="_blank" rel="noreferrer"
            aria-label="${escapeHtml(`Open the link from row ${target.row_index} in a new tab`)}">${icon("external-link", "icon icon--tiny")}</a>
          <button class="icon-button" type="button" data-action="copy" data-copy="${escapeHtml(target.normalized_url)}"
            aria-label="${escapeHtml(`Copy the link from row ${target.row_index}`)}">${icon("copy", "icon icon--tiny")}</button>
        </span>
        ${(target.input_notes || []).length
          ? `<span class="rd-note">${icon("info-circle", "icon icon--tiny")}${escapeHtml(noteCopy(target.input_notes))}</span>`
          : ""}
      </td>
      <td class="rd-chain">${chainStrip(target)}</td>
      <td class="rd-dest">${destinationCell(target)}</td>
      <td class="rd-verdict">${verdictBadge(target.verdict, target)}${flagChips(target.flags)}</td>
      <td class="tool-table__actions">
        <button class="button button--quiet" type="button" data-action="toggle"
          aria-expanded="false" aria-controls="${detailId}"
          ${checked ? "" : "disabled aria-disabled=\"true\" title=\"Check the link first\""}>
          ${icon("list-details", "icon icon--tiny")}Details</button>
        <button class="icon-button" type="button" data-action="run"
          aria-label="${escapeHtml(`${checked ? "Re-check" : "Check"} row ${target.row_index}`)}">${icon("refresh")}</button>
      </td>
    </tr>
    <tr class="rd-detail-row" id="${detailId}" data-target-id="${escapeHtml(target.id)}" hidden>
      <td colspan="6">${checked ? chainDetail(target, { idPrefix: target.id }) : ""}</td>
    </tr>`;
}

function noteCopy(notes) {
  const copy = {
    scheme_added: "we added https://",
    wrapper_removed: "quotes/brackets removed",
    trailing_punctuation_removed: "trailing punctuation removed",
    fragment_dropped: "#fragment dropped (never sent to the server)",
    idn_encoded: "host encoded as punycode",
  };
  return notes.map((note) => copy[note] || note).join(" · ");
}

// Компактна стрічка ланцюжка: `301 → 302 → 200`, статуси текстом і кольором.
function chainStrip(target) {
  if (!target.checked_at) {
    return `<span class="rd-strip rd-strip--pending">${icon(target.run_status === "running" ? "loader-2" : "circle-dashed",
      `icon icon--tiny${target.run_status === "running" ? " icon--spin" : ""}`)}${escapeHtml(RUN_STATUS_LABEL[target.run_status] || "Not checked")}</span>`;
  }
  if (!(target.hops || []).length) {
    return `<span class="rd-strip rd-strip--pending">${icon("wifi-off", "icon icon--tiny")}No response</span>`;
  }
  const hops = target.hops
    .map((hop) => `<span class="rd-status rd-status--${statusTone(hop.status)}"${hop.kind === "meta_refresh"
      ? ' title="HTML meta refresh, not an HTTP redirect"'
      : ""}>${hop.status}${hop.kind === "meta_refresh" ? "*" : ""}</span>`)
    .join(`<span class="rd-arrow" aria-hidden="true">${icon("arrow-narrow-right", "icon icon--tiny")}</span>`);
  const count = target.hop_count === null || target.hop_count === undefined ? "" : target.hop_count;
  return `
    <span class="rd-strip" role="img" aria-label="${escapeHtml(chainAria(target))}">${hops}</span>
    <span class="rd-strip__count">${count} redirect${count === 1 ? "" : "s"}${target.elapsed_ms ? ` · ${target.elapsed_ms} ms` : ""}</span>`;
}

function chainAria(target) {
  const statuses = (target.hops || []).map((hop) => hop.status).join(", then ");
  return `Chain: ${statuses}`;
}

function statusTone(status) {
  if (status >= 500) return "bad";
  if (status === 401 || status === 403 || status === 407 || status === 429) return "warn";
  if (status >= 400) return "bad";
  if (status >= 300) return "info";
  return "good";
}

function destinationCell(target) {
  if (!target.checked_at) return '<span class="muted">—</span>';
  if (!target.final_url) {
    return `<span class="muted">Never reached</span>${target.error_detail
      ? `<small class="rd-error">${escapeHtml(target.error_detail)}</small>` : ""}`;
  }
  return `
    <span class="rd-dest__url" title="${escapeHtml(target.final_url)}">${escapeHtml(target.final_url)}</span>
    <span class="at-actions">
      <a class="icon-button" href="${escapeHtml(target.final_url)}" target="_blank" rel="noreferrer"
        aria-label="${escapeHtml(`Open the destination of row ${target.row_index} in a new tab`)}">${icon("external-link", "icon icon--tiny")}</a>
      <button class="icon-button" type="button" data-action="copy" data-copy="${escapeHtml(target.final_url)}"
        aria-label="${escapeHtml(`Copy the destination of row ${target.row_index}`)}">${icon("copy", "icon icon--tiny")}</button>
    </span>
    ${target.final_status ? `<small class="rd-dest__status">HTTP ${target.final_status}</small>` : ""}`;
}

function verdictBadge(verdict, target = null) {
  if (!verdict) return '<span class="rd-badge rd-badge--none">Not checked yet</span>';
  const meta = VERDICT_META[verdict] || { label: verdict, icon: "help-circle", tone: "warn" };
  const time = target?.checked_at
    ? `<small>${escapeHtml(formatTimestamp(target.checked_at, ""))}</small>`
    : "";
  return `<span class="rd-badge rd-badge--${meta.tone}">${icon(meta.icon, "icon icon--tiny")}${escapeHtml(meta.label)}${time}</span>`;
}

function flagChips(flags) {
  const list = flags || [];
  if (!list.length) return "";
  const shown = list.slice(0, 2);
  const rest = list.length - shown.length;
  return `<span class="rd-flags">${shown
    .map((flag) => `<span class="rd-flag" title="${escapeHtml(FLAG_COPY[flag]?.hint || flag)}">${escapeHtml(FLAG_COPY[flag]?.label || flag)}</span>`)
    .join("")}${rest > 0 ? `<span class="rd-flag rd-flag--rest">+${rest}</span>` : ""}</span>`;
}

// «Звідки → куди» одним рядком, до самих кроків: на це питання оператор
// відповідає першим, і воно мусить читатися без розбору списку хопів.
// Відсутність редіректів — теж явне твердження, а не порожнє місце.
function routeSummary(target, hops) {
  const redirects = hops.filter((hop) => hop.location).length;
  const from = target.normalized_url || hops[0]?.url || "";
  const to = target.final_url || hops[hops.length - 1]?.url || "";
  if (!redirects) {
    if (!to) return "";
    return `
      <p class="rd-route rd-route--direct">${icon("circle-check", "icon icon--tiny")}
        <span>No redirects — this URL answered directly.</span></p>`;
  }
  return `
    <p class="rd-route">
      <code translate="no">${escapeHtml(from)}</code>
      <span class="rd-route__arrow" aria-hidden="true">${icon("arrow-narrow-right", "icon icon--tiny")}</span>
      <span class="visually-hidden">redirects to</span>
      <code translate="no">${escapeHtml(to)}</code>
      <span class="rd-route__count">via ${redirects} redirect${redirects === 1 ? "" : "s"}</span>
    </p>`;
}

// Головний екран інструмента: ланцюжок вертикально, крок за кроком.
function chainDetail(target, { idPrefix }) {
  const hops = target.hops || [];
  const steps = hops.map((hop, index) => {
    const isFinal = index === hops.length - 1 && !hop.location;
    const changes = (hop.changes || []).map((change) => CHANGE_COPY[change] || change);
    return `
      <li class="rd-hop${isFinal ? " rd-hop--final" : ""}">
        <span class="rd-hop__index" aria-hidden="true">${hop.index}</span>
        <span class="rd-hop__body">
          <span class="rd-hop__url">
            <code translate="no">${escapeHtml(hop.url)}</code>
            <button class="icon-button" type="button" data-action="copy" data-copy="${escapeHtml(hop.url)}"
              aria-label="${escapeHtml(`Copy hop ${hop.index} URL`)}">${icon("copy", "icon icon--tiny")}</button>
          </span>
          <span class="rd-hop__meta">
            <span class="rd-status rd-status--${statusTone(hop.status)}">${hop.status}</span>
            <span class="rd-hop__text">${escapeHtml(hop.status_text || "")}</span>
            ${hop.kind === "meta_refresh" ? `<span class="rd-flag">meta refresh</span>` : ""}
            ${hop.permanence ? `<span class="rd-flag">${hop.permanence === "permanent" ? "permanent" : "temporary"}</span>` : ""}
            ${hop.elapsed_ms ? `<span class="rd-hop__time">${hop.elapsed_ms} ms</span>` : ""}
            ${isFinal ? '<span class="rd-flag rd-flag--final">destination</span>' : ""}
          </span>
          ${hop.location ? `
            <span class="rd-hop__to">
              ${icon("corner-down-right", "icon icon--tiny")}
              <span class="rd-hop__to-label">redirects to</span>
              <code translate="no">${escapeHtml(hop.location)}</code>
            </span>` : ""}
          ${changes.length ? `<span class="rd-hop__changes">${icon("arrow-narrow-right", "icon icon--tiny")}${escapeHtml(changes.join(" · "))}</span>` : ""}
        </span>
      </li>`;
  }).join("");

  const advice = (target.flags || [])
    .map((flag) => FLAG_COPY[flag])
    .filter(Boolean);
  const page = target.final_page;
  return `
    <div class="rd-detail" data-detail="${escapeHtml(idPrefix)}">
      ${routeSummary(target, hops)}
      ${hops.length
        ? `<ol class="rd-hops">${steps}</ol>`
        : `<p class="rd-detail__empty">${icon("wifi-off")}<span>The server never answered, so there is no chain to show.</span></p>`}
      ${target.error_detail ? `<p class="rd-detail__error">${icon("alert-triangle", "icon icon--tiny")}${escapeHtml(target.error_detail)}</p>` : ""}
      ${page ? `
        <dl class="rd-page-facts">
          ${page.title ? `<div><dt>Destination title</dt><dd>${escapeHtml(page.title)}</dd></div>` : ""}
          ${page.canonical ? `<div><dt>Canonical</dt><dd><code translate="no">${escapeHtml(page.canonical)}</code></dd></div>` : ""}
          ${page.meta_robots ? `<div><dt>Meta robots</dt><dd><code translate="no">${escapeHtml(page.meta_robots)}</code></dd></div>` : ""}
        </dl>` : ""}
      ${advice.length ? `
        <div class="rd-advice">
          <h4>What this means</h4>
          <ul>${advice.map((flag) => `<li><strong>${escapeHtml(flag.label)}.</strong> ${escapeHtml(flag.hint)}</li>`).join("")}</ul>
        </div>` : ""}
      ${hops.length && idPrefix !== "quick" ? `
        <p class="rd-detail__actions">
          <button class="button button--quiet" type="button" data-action="copy-chain-row">${icon("copy", "icon icon--tiny")}Copy chain as text</button>
        </p>` : ""}
    </div>`;
}

// --- прогін ------------------------------------------------------------------

function startRun() {
  const queued = currentRows.filter((row) => row.run_status === "queued").map((row) => row.id);
  if (!queued.length) {
    toast("Nothing left to check on this page.");
    return;
  }
  runner = { active: 0, stop: false, pending: queued, total: queued.length, done: 0 };
  pump();
  updateRunControls();
}

function stopRun() {
  runner.stop = true;
  runner.pending = [];
  updateRunControls();
  toast("Stopped. Rows already in flight will finish.");
}

async function rerunProblems() {
  const problems = currentRows
    .filter((row) => row.run_status === "failed"
      || ["fetch_failed", "blocked", "broken", "broken_chain", "loop", "too_many_hops"].includes(row.verdict))
    .map((row) => row.id);
  if (!problems.length) {
    toast("No problem rows on this page.");
    return;
  }
  runner = { active: 0, stop: false, pending: problems, total: problems.length, done: 0 };
  pump();
  updateRunControls();
}

function pump() {
  while (!runner.stop && runner.active < CONCURRENCY && runner.pending.length) {
    const targetId = runner.pending.shift();
    runner.active += 1;
    void runOne(targetId).finally(() => {
      runner.active -= 1;
      runner.done += 1;
      updateRunControls();
      if (runner.pending.length && !runner.stop) pump();
      else if (runner.active === 0) void finishRun();
    });
  }
}

// Один рядок на кнопку: після нього одразу перечитуємо лічильники, бо це
// завершена дія оператора, а не крок черги.
async function runSingle(targetId) {
  await runOne(targetId);
  await finishRun();
}

async function runOne(targetId) {
  markRunning(targetId);
  try {
    const target = await runRedirectTarget(state.checkId, targetId);
    replaceRow(target);
  } catch (error) {
    // 409 означає «рядок уже виконується» — це не помилка оператора.
    if (error.status !== 409) {
      const row = rowElement(targetId);
      if (row) row.querySelector(".rd-chain").innerHTML =
        `<span class="rd-strip rd-strip--pending">${icon("alert-triangle", "icon icon--tiny")}${escapeHtml(error.message)}</span>`;
    }
  }
}

function rowElement(targetId) {
  return root.querySelector(`tr[data-target-id="${cssEscape(targetId)}"]`);
}

// `CSS.escape` є не в кожному engine, з яким тестуємо; id — це UUID, тож
// достатньо відкинути все, крім безпечних символів.
function cssEscape(value) {
  return String(value).replace(/["\\]/g, "");
}

function markRunning(targetId) {
  const index = currentRows.findIndex((row) => row.id === targetId);
  if (index >= 0) currentRows[index] = { ...currentRows[index], run_status: "running" };
  const row = rowElement(targetId);
  if (row) row.querySelector(".rd-chain").innerHTML = chainStrip({ run_status: "running", hops: [] });
}

function replaceRow(target) {
  const index = currentRows.findIndex((row) => row.id === target.id);
  if (index >= 0) currentRows[index] = target;
  const row = rowElement(target.id);
  if (!row) return;
  const detail = row.nextElementSibling;
  const wasOpen = detail?.classList.contains("rd-detail-row") && !detail.hidden;
  // `targetRow` віддає ДВА `<tr>` (рядок + його деталі), тож старий detail-рядок
  // прибираємо до заміни — інакше в DOM лишилися б два вузли з тим самим id.
  if (detail?.classList.contains("rd-detail-row")) detail.remove();
  row.outerHTML = targetRow(target);
  const fresh = rowElement(target.id);
  if (wasOpen && fresh) toggleDetail(fresh.querySelector("button[data-action='toggle']"), fresh, true);
}

async function finishRun() {
  updateRunControls();
  try {
    renderCheckHeader(await getRedirectCheck(state.checkId));
  } catch {
    // Лічильники — вторинні: самі результати вже збережені на сервері.
  }
}

function updateRunControls() {
  const run = root.querySelector("#rd-run");
  const stop = root.querySelector("#rd-stop");
  if (!run || !stop) return;
  const busy = runner.active > 0;
  stop.disabled = !busy;
  run.disabled = busy || !currentRows.some((row) => row.run_status === "queued");
  run.innerHTML = busy
    ? `${icon("loader-2", "icon icon--spin")}Checking ${runner.done}/${runner.total}…`
    : `${icon("player-play")}Check remaining`;
}

// --- events ------------------------------------------------------------------

// Делегація вішається РІВНО ОДИН РАЗ на контейнер, який переживає перерендери
// (міняється лише його `innerHTML`) — інакше після N перемальовувань один клік
// давав би N запитів (дефект, знайдений у task-27).
function bindDelegatedHandlers(body) {
  if (!body || body.dataset.delegated === "true") return;
  body.dataset.delegated = "true";
  body.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-action]");
    if (!button) return;
    const row = button.closest("tr[data-target-id]");
    const action = button.dataset.action;
    if (action === "copy") void copyValue(button);
    else if (action === "toggle") toggleDetail(button, row);
    else if (action === "run") void runSingle(row.dataset.targetId);
    else if (action === "copy-chain-row") {
      const target = currentRows.find((candidate) => candidate.id === row.dataset.targetId);
      if (target) void copyChain(button, target);
    } else if (action === "clear-filters") {
      applyFilters({ q: "", verdict: "", flag: "", runStatus: "" });
    }
  });
}

function bindRowControls(scope) {
  const body = root.querySelector("#rd-rows-body");
  const search = body.querySelector("#rd-search");
  if (search && !search.dataset.bound) {
    search.dataset.bound = "true";
    let timer = null;
    search.addEventListener("input", () => {
      clearTimeout(timer);
      timer = setTimeout(() => applyFilters({ q: search.value }), 300);
    });
  }
  // `bindRowControls` викликається і після заміни окремого рядка, тож кожен
  // слухач вішаємо один раз — інакше один вибір фільтра дав би N запитів.
  const onceChange = (selector, patch) => {
    const control = body.querySelector(selector);
    if (!control || control.dataset.bound) return;
    control.dataset.bound = "true";
    control.addEventListener("change", (event) => applyFilters(patch(event.target.value)));
  };
  onceChange("#rd-filter-verdict", (value) => ({ verdict: value }));
  onceChange("#rd-filter-flag", (value) => ({ flag: value }));
  onceChange("#rd-filter-run", (value) => ({ runStatus: value }));
  body.querySelectorAll("button[data-page]").forEach((button) => {
    if (button.dataset.bound) return;
    button.dataset.bound = "true";
    button.addEventListener("click", () => {
      state.offset = button.dataset.page === "next"
        ? state.offset + PAGE_SIZE
        : Math.max(0, state.offset - PAGE_SIZE);
      persist();
      void loadTargets();
    });
  });
  if (scope) applyMeters(scope);
}

function toggleDetail(button, row, force = null) {
  const detail = row.nextElementSibling;
  if (!detail || !detail.classList.contains("rd-detail-row")) return;
  const open = force === null ? detail.hidden : force;
  detail.hidden = !open;
  button.setAttribute("aria-expanded", String(open));
  button.innerHTML = `${icon("list-details", "icon icon--tiny")}${open ? "Hide" : "Details"}`;
}

function applyFilters(patch) {
  Object.assign(state, patch);
  state.offset = 0;
  persist();
  void loadTargets();
}

async function copyText(value) {
  try {
    await navigator.clipboard.writeText(value);
    return true;
  } catch {
    toast("Could not copy — the clipboard is unavailable in this browser.");
    return false;
  }
}

async function copyValue(button) {
  if (!(await copyText(button.dataset.copy))) return;
  flashCopied(button);
  toast(`Copied ${button.dataset.copy}`);
}

async function copyChain(button, target) {
  const lines = (target.hops || []).map((hop) =>
    `${hop.index}. ${hop.status}${hop.status_text ? ` ${hop.status_text}` : ""} — ${hop.url}`);
  if (target.final_url) lines.push(`→ ${target.final_url}`);
  if (!(await copyText(lines.join("\n")))) return;
  flashCopied(button, "Copy chain");
  toast("Copied the chain as text");
}

function flashCopied(button, label = null) {
  const original = button.innerHTML;
  button.innerHTML = label
    ? `${icon("clipboard-check", "icon icon--tiny")}${label}`
    : icon("copy-check", "icon icon--tiny");
  setTimeout(() => { button.innerHTML = original; }, 1400);
}
