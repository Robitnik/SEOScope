/* Strategy Package Check (task-90): one Google Drive link in, five-part QA
 * out — QA Strategy + QA Frame + QA Copy for every linked framework, plus
 * three cross-checks (Frame vs Strategy, Frame vs live site, Copy vs live
 * site). Persisted server-side (`content_qa_packages`) — same two-view
 * shape as the other Content QA tools (list + shareable detail at
 * `#/tools/strategy-package/<id>`), but the source is always a Drive file,
 * never an upload: step 1 resolves a pasted link or a Drive-browse pick to
 * a file id, step 2 previews sheets/frameworks (stateless `/inspect`), step
 * 3 collects the profile and runs `/check`.
 *
 * The findings panel (`content-qa-render.js`, built for task-53/87) is
 * reused for every findings list in the report view (Strategy tab, each
 * Framework's tab, Cross-checks tab) rather than reinventing filter/sort/
 * group/copy — only one such panel is ever mounted in the DOM at a time
 * (tabs replace `#sp-tabpanel`'s whole innerHTML), so its internal ids
 * (`#cq-groups`, `#cq-controls`, …) never collide.
 */
import {
  browseStrategyPackageDrive, checkStrategyPackage, getDashboard, getStrategyPackageReport,
  getStrategyPackageReports, inspectStrategyPackage, resolveStrategyPackageSource,
  strategyPackageMatrixXlsxUrl,
} from "./api.js?v=20260909-t91";
import { buildToolsHash } from "./router.js?v=20260909-t91";
import { escapeHtml, formatTimestamp, icon } from "./formatters.js?v=20260909-t91";
import { panelSkeleton } from "./skeleton.js?v=20260909-t91";
import { renderError, renderReport, wireReport } from "./content-qa-render.js?v=20260909-t91";

const TOOL_KEY = "strategy-package";
let root = null;
let requestToken = 0;

// Той самий кеш-на-модуль клієнтів, що інші Content QA тули.
let clientOptions = null;

const SOURCE_KIND_LABEL = {
  spreadsheet: "Google Sheet", xlsx_blob: "Excel file on Drive",
  document: "Google Doc", unsupported: "Unsupported file",
};
const SOURCE_KIND_ICON = {
  spreadsheet: "file-spreadsheet", xlsx_blob: "file-spreadsheet",
  document: "file-text", unsupported: "file-off",
};

const FRAMEWORK_STATE_META = {
  checked: { icon: "circle-check", cls: "sp-state--ok", label: "Checked" },
  not_accessible: { icon: "lock", cls: "sp-state--bad", label: "Not accessible" },
  no_recognized_tabs: { icon: "table-off", cls: "sp-state--warn", label: "No Frame/Content tab" },
};

const CROSS_STATE_META = {
  checked: { icon: "circle-check", cls: "sp-cross--ok", label: "Checked" },
  not_checked: { icon: "alert-circle", cls: "sp-cross--warn", label: "Not checked" },
  not_applicable: { icon: "circle-dashed", cls: "sp-cross--dim", label: "N/A" },
};
const CROSS_CHECK_NAMES = {
  frame_strategy: "Frame ↔ Strategy", frame_site: "Frame ↔ Live site", copy_site: "Copy ↔ Live site",
};

const SHEET_STATE_META = {
  checked: { icon: "circle-check", cls: "cq-sheet--checked", label: "Checked" },
  not_selected: { icon: "circle-dashed", cls: "cq-sheet--not-selected", label: "Not selected" },
  no_recognized_columns: { icon: "table-off", cls: "cq-sheet--unrecognized", label: "No title/description columns" },
};

// Стан майстра кроків 1-3 — живе, поки на екрані форма списку; скидається
// при кожному вході в список (новий пошук джерела не має тягнути стару
// вкладку/фреймворки з попереднього прогону).
function freshWizard() {
  return {
    sourceMethod: "link",
    linkValue: "",
    resolving: false,
    resolved: null, // { file, kind }
    resolveError: null,
    inspecting: false,
    inspection: null, // StrategyPackageInspectDTO
    inspectError: null,
    selectedSheets: new Set(),
    selectedFrameworks: new Map(), // document_id -> FrameworkCandidateDTO
    crossSheet: true,
    checkSite: true,
    checkPublished: true,
    browse: { q: "", loading: false, error: null, files: [], nextPageToken: null },
  };
}
let wizard = freshWizard();

export async function initTool(mount, route) {
  root = mount;
  requestToken += 1;
  if (route.reportId) {
    await renderReportView(route.reportId);
    return;
  }
  await renderListView();
}

async function renderListView() {
  wizard = freshWizard();
  root.innerHTML = `
    <a class="back-link" href="${buildToolsHash()}">${icon("arrow-left")}<span>All tools</span></a>
    <header class="tool-header">
      <p class="eyebrow">Tools / Strategy Package Check</p>
      <h1>Strategy package check</h1>
      <p class="tool-lead">Point at a strategy on Google Drive and get QA on the strategy, every linked
        framework and its published copy, plus cross-checks against each other and the live site — all in
        one run.</p>
    </header>
    <section class="tool-panel" aria-labelledby="sp-panel-title">
      <div class="section-heading"><div><h2 id="sp-panel-title">Check a strategy package</h2>
        <p class="tool-panel__hint">The agency Google account needs to see the file — share it first if it lives
          in a personal Drive.</p></div></div>
      <div class="tool-panel__body">
        <div id="sp-source"></div>
        <div id="sp-scope"></div>
        <div id="sp-profile"></div>
      </div>
    </section>
    <section class="tool-panel" id="sp-reports" aria-labelledby="sp-reports-title">
      <div class="section-heading"><div><h2 id="sp-reports-title">Reports</h2></div></div>
      <div class="tool-panel__body" id="sp-reports-body">
        ${panelSkeleton({ label: "reports", rows: 3, columns: 4 })}
      </div>
    </section>`;
  renderSourceStep();
  await loadReports();
}

/* ---------------------------- Step 1: source ---------------------------- */

function renderSourceStep() {
  const mount = root.querySelector("#sp-source");
  if (!mount) return;
  mount.innerHTML = `
    <div class="sp-method" role="group" aria-label="How to pick the strategy">
      <button type="button" class="sp-method__option${wizard.sourceMethod === "link" ? " sp-method__option--active" : ""}" data-sp-method="link">
        ${icon("link", "icon icon--tiny")}Paste link
      </button>
      <button type="button" class="sp-method__option${wizard.sourceMethod === "browse" ? " sp-method__option--active" : ""}" data-sp-method="browse">
        ${icon("folder", "icon icon--tiny")}Browse Drive
      </button>
    </div>
    <div class="sp-source-body">
      <div class="sp-link" ${wizard.sourceMethod === "link" ? "" : "hidden"}>
        <label class="form-field">Strategy link or file id
          <input id="sp-link-input" type="text" placeholder="https://docs.google.com/spreadsheets/d/…"
            autocomplete="off" value="${escapeHtml(wizard.linkValue)}">
        </label>
        <button class="button button--primary" type="button" id="sp-link-go">Use this link</button>
      </div>
      <div class="sp-browse" ${wizard.sourceMethod === "browse" ? "" : "hidden"}>
        <label class="sp-search">
          ${icon("search", "icon icon--tiny")}
          <input type="search" id="sp-browse-search" placeholder="Search Drive by file name…" autocomplete="off">
        </label>
        <div id="sp-browse-results"></div>
      </div>
      <p class="form-error" id="sp-source-error" role="alert">${wizard.resolveError ? escapeHtml(wizard.resolveError) : ""}</p>
      <div id="sp-resolved"></div>
    </div>`;

  mount.querySelectorAll("[data-sp-method]").forEach((button) => {
    button.addEventListener("click", () => {
      wizard.sourceMethod = button.dataset.spMethod;
      renderSourceStep();
      if (wizard.sourceMethod === "browse" && !wizard.browse.files.length && !wizard.browse.loading) {
        void runBrowse();
      }
    });
  });
  mount.querySelector("#sp-link-input").addEventListener("input", (event) => {
    wizard.linkValue = event.target.value;
  });
  mount.querySelector("#sp-link-input").addEventListener("keydown", (event) => {
    if (event.key === "Enter") { event.preventDefault(); void handleResolve(wizard.linkValue); }
  });
  mount.querySelector("#sp-link-go").addEventListener("click", () => void handleResolve(wizard.linkValue));

  let searchDebounce = null;
  mount.querySelector("#sp-browse-search").addEventListener("input", (event) => {
    wizard.browse.q = event.target.value;
    window.clearTimeout(searchDebounce);
    searchDebounce = window.setTimeout(() => void runBrowse(), 250);
  });

  renderBrowseResults();
  renderResolvedCard();
}

async function runBrowse() {
  wizard.browse.loading = true;
  wizard.browse.error = null;
  renderBrowseResults();
  const token = ++requestToken;
  try {
    const page = await browseStrategyPackageDrive({ q: wizard.browse.q });
    if (token !== requestToken) return;
    wizard.browse.files = page.files || [];
    wizard.browse.nextPageToken = page.next_page_token || null;
  } catch (error) {
    if (token !== requestToken) return;
    wizard.browse.error = error.message;
  } finally {
    if (token === requestToken) wizard.browse.loading = false;
    renderBrowseResults();
  }
}

function renderBrowseResults() {
  const mount = root.querySelector("#sp-browse-results");
  if (!mount) return;
  if (wizard.browse.loading) {
    mount.innerHTML = `<p class="sp-browse__status">${icon("loader-2", "icon icon--spin")}Searching Drive…</p>`;
    return;
  }
  if (wizard.browse.error) {
    mount.innerHTML = `
      <p class="sp-browse__status sp-browse__status--error">${icon("alert-triangle", "icon icon--tiny")}
        <span>${escapeHtml(wizard.browse.error)}</span></p>`;
    return;
  }
  if (!wizard.browse.files.length) {
    mount.innerHTML = `<p class="sp-browse__status">${icon("folder-open", "icon icon--tiny")}
      <span>No spreadsheets or documents found${wizard.browse.q.trim() ? " for this search." : " — try a search."}</span></p>`;
    return;
  }
  mount.innerHTML = `
    <ul class="sp-browse__list">
      ${wizard.browse.files.map((f) => `
        <li>
          <button type="button" class="sp-browse__item" data-sp-pick="${escapeHtml(f.id)}">
            ${icon(f.mime_type.includes("document") ? "file-text" : "file-spreadsheet", "icon icon--tiny")}
            <span class="sp-browse__name">${escapeHtml(f.name)}</span>
            ${f.is_shared_drive ? `<span class="sp-browse__badge">Shared drive</span>` : ""}
          </button>
        </li>`).join("")}
    </ul>`;
  mount.querySelectorAll("[data-sp-pick]").forEach((button) => {
    button.addEventListener("click", () => void handleResolve(button.dataset.spPick));
  });
}

async function handleResolve(urlOrId) {
  const value = (urlOrId || "").trim();
  const errorField = root.querySelector("#sp-source-error");
  if (!value) {
    if (errorField) errorField.textContent = "Paste a link or pick a file first.";
    return;
  }
  wizard.resolving = true;
  wizard.resolveError = null;
  wizard.resolved = null;
  wizard.inspection = null;
  wizard.selectedSheets = new Set();
  wizard.selectedFrameworks = new Map();
  if (errorField) errorField.textContent = "";
  renderResolvedCard();
  root.querySelector("#sp-scope").innerHTML = "";
  root.querySelector("#sp-profile").innerHTML = "";

  const token = ++requestToken;
  try {
    const resolved = await resolveStrategyPackageSource(value);
    if (token !== requestToken) return;
    wizard.resolved = resolved;
    wizard.resolving = false;
    renderResolvedCard();
    if (resolved.kind === "spreadsheet" || resolved.kind === "xlsx_blob") {
      await handleInspect(resolved.file.id);
    } else {
      renderProfileStep(); // ще дозволяємо профіль/скасування, але без Run — див. renderProfileStep
    }
  } catch (error) {
    if (token !== requestToken) return;
    wizard.resolving = false;
    wizard.resolveError = error.message;
    renderResolvedCard();
    if (errorField) errorField.textContent = error.message;
  }
}

function renderResolvedCard() {
  const mount = root.querySelector("#sp-resolved");
  if (!mount) return;
  if (wizard.resolving) {
    mount.innerHTML = `<p class="sp-resolved__status">${icon("loader-2", "icon icon--spin")}Looking up the file…</p>`;
    return;
  }
  if (!wizard.resolved) {
    mount.innerHTML = "";
    return;
  }
  const { file, kind } = wizard.resolved;
  const badKind = kind === "document" || kind === "unsupported";
  mount.innerHTML = `
    <div class="sp-resolved${badKind ? " sp-resolved--bad" : ""}">
      ${icon(SOURCE_KIND_ICON[kind] || "file", "icon")}
      <div class="sp-resolved__body">
        <strong>${escapeHtml(file.name)}</strong>
        <span>${SOURCE_KIND_LABEL[kind] || kind}</span>
        ${file.web_view_link ? `<a href="${escapeHtml(file.web_view_link)}" target="_blank" rel="noopener noreferrer">Open in Drive${icon("external-link", "icon icon--tiny")}</a>` : ""}
        ${badKind ? `<p class="sp-resolved__error">${icon("alert-triangle", "icon icon--tiny")}This is not an SEO strategy spreadsheet — pick a Google Sheet or an .xlsx file.</p>` : ""}
      </div>
      <button class="button button--quiet" type="button" id="sp-change-source">Change</button>
    </div>`;
  mount.querySelector("#sp-change-source").addEventListener("click", () => {
    wizard.resolved = null;
    wizard.inspection = null;
    wizard.resolveError = null;
    renderResolvedCard();
    root.querySelector("#sp-scope").innerHTML = "";
    root.querySelector("#sp-profile").innerHTML = "";
  });
}

/* --------------------------- Step 2: scope ------------------------------ */

async function handleInspect(fileId) {
  wizard.inspecting = true;
  wizard.inspectError = null;
  renderScopeStep();
  const token = ++requestToken;
  try {
    const inspection = await inspectStrategyPackage(fileId);
    if (token !== requestToken || wizard.resolved?.file.id !== fileId) return;
    wizard.inspection = inspection;
    wizard.selectedSheets = new Set(inspection.suggested_sheets);
    wizard.selectedFrameworks = new Map(
      inspection.candidates.filter((c) => c.origin === "strategy_link").map((c) => [c.document_id, c])
    );
  } catch (error) {
    if (token !== requestToken || wizard.resolved?.file.id !== fileId) return;
    wizard.inspectError = error.message;
  } finally {
    if (token === requestToken) {
      wizard.inspecting = false;
      renderScopeStep();
      renderProfileStep();
    }
  }
}

function renderScopeStep() {
  const mount = root.querySelector("#sp-scope");
  if (!mount) return;
  if (wizard.inspecting) {
    mount.innerHTML = `<p class="sp-resolved__status">${icon("loader-2", "icon icon--spin")}Reading the strategy…</p>`;
    return;
  }
  if (wizard.inspectError) {
    mount.innerHTML = `
      <p class="cq-sheet-picker__error">${icon("alert-triangle", "icon icon--tiny")}
        <span>Could not preview sheets/frameworks (${escapeHtml(wizard.inspectError)}) — the check will still
          run against the whole strategy, with no frameworks pre-selected.</span></p>`;
    return;
  }
  if (!wizard.inspection) {
    mount.innerHTML = "";
    return;
  }
  const { sheets, candidates } = wizard.inspection;
  mount.innerHTML = `
    ${renderSheetPickerMarkup(sheets)}
    ${renderFrameworkPickerMarkup(candidates)}`;
  wireSheetPicker(mount, sheets);
  wireFrameworkPicker(mount, candidates);
}

function renderSheetPickerMarkup(sheets) {
  const loadBearing = sheets.filter((s) => s.load_bearing);
  if (loadBearing.length <= 1) {
    const only = loadBearing[0];
    return `
      <p class="cq-sheet-single">${icon("table", "icon icon--tiny")}
        <span>Sheet: <strong>${only ? escapeHtml(only.name) : "none recognized"}</strong></span></p>`;
  }
  return `
    <div class="cq-sheet-picker">
      <p class="cq-sheet-picker__label">Sheets to check</p>
      <ul class="cq-sheet-picker__list">
        ${sheets.map((s) => `
          <li class="cq-sheet-picker__item${s.load_bearing ? "" : " cq-sheet-picker__item--unrecognized"}">
            <label>
              <input type="checkbox" data-sp-sheet="${escapeHtml(s.name)}"
                ${wizard.selectedSheets.has(s.name) ? "checked" : ""} ${s.load_bearing ? "" : "disabled"}>
              <span>${escapeHtml(s.name)}</span>
              <span class="cq-sheet-picker__meta">${s.row_count} row${s.row_count === 1 ? "" : "s"}${s.load_bearing ? "" : " — no title/description columns"}</span>
            </label>
          </li>`).join("")}
      </ul>
      <label class="cq-sheet-picker__cross">
        <input type="checkbox" id="sp-cross-sheet" ${wizard.crossSheet ? "checked" : ""}>
        <span>Also compare duplicates/keywords across the selected sheets</span>
      </label>
    </div>`;
}

function wireSheetPicker(mount) {
  mount.querySelectorAll("[data-sp-sheet]").forEach((checkbox) => {
    checkbox.addEventListener("change", () => {
      const name = checkbox.dataset.spSheet;
      if (checkbox.checked) wizard.selectedSheets.add(name); else wizard.selectedSheets.delete(name);
    });
  });
  mount.querySelector("#sp-cross-sheet")?.addEventListener("change", (event) => {
    wizard.crossSheet = event.target.checked;
  });
}

function renderFrameworkPickerMarkup(candidates) {
  if (!candidates.length) {
    return `
      <p class="sp-framework-picker__empty">${icon("file-off", "icon icon--tiny")}
        <span>No framework links found in this strategy — the check will still run against the strategy sheet
          alone. You can re-run once frameworks are linked.</span></p>`;
  }
  return `
    <div class="sp-framework-picker">
      <div class="sp-framework-picker__head">
        <p class="cq-sheet-picker__label">Frameworks to check (${wizard.selectedFrameworks.size} of ${candidates.length} selected)</p>
        <div>
          <button type="button" class="button button--quiet" data-sp-fw-all>All</button>
          <button type="button" class="button button--quiet" data-sp-fw-none>None</button>
        </div>
      </div>
      <ul class="sp-framework-picker__list">
        ${candidates.map((c) => `
          <li>
            <label>
              <input type="checkbox" data-sp-fw="${escapeHtml(c.document_id)}" ${wizard.selectedFrameworks.has(c.document_id) ? "checked" : ""}>
              <span class="sp-framework-picker__name">${escapeHtml(c.document_name || c.document_id)}</span>
              <span class="sp-framework-picker__origin">${c.origin === "strategy_link"
                ? `${icon("link", "icon icon--tiny")}${escapeHtml(c.strategy_ref || "linked from strategy")}`
                : `${icon("search", "icon icon--tiny")}found by name search`}</span>
            </label>
          </li>`).join("")}
      </ul>
    </div>`;
}

function wireFrameworkPicker(mount, candidates) {
  const byId = new Map(candidates.map((c) => [c.document_id, c]));
  mount.querySelectorAll("[data-sp-fw]").forEach((checkbox) => {
    checkbox.addEventListener("change", () => {
      const id = checkbox.dataset.spFw;
      if (checkbox.checked) wizard.selectedFrameworks.set(id, byId.get(id));
      else wizard.selectedFrameworks.delete(id);
      const label = mount.querySelector(".sp-framework-picker__head .cq-sheet-picker__label");
      if (label) label.textContent = `Frameworks to check (${wizard.selectedFrameworks.size} of ${candidates.length} selected)`;
    });
  });
  mount.querySelector("[data-sp-fw-all]")?.addEventListener("click", () => {
    wizard.selectedFrameworks = new Map(candidates.map((c) => [c.document_id, c]));
    renderScopeStep();
  });
  mount.querySelector("[data-sp-fw-none]")?.addEventListener("click", () => {
    wizard.selectedFrameworks = new Map();
    renderScopeStep();
  });
}

/* -------------------------- Step 3: profile ----------------------------- */

function renderProfileStep() {
  const mount = root.querySelector("#sp-profile");
  if (!mount || !wizard.resolved) { if (mount) mount.innerHTML = ""; return; }
  const badKind = wizard.resolved.kind === "document" || wizard.resolved.kind === "unsupported";
  if (badKind) { mount.innerHTML = ""; return; }

  mount.innerHTML = `
    <form id="sp-profile-form" novalidate>
      <div class="cq-fields">
        <label class="form-field">Framework type
          <select id="sp-kind">
            <option value="page" selected>Page framework</option>
            <option value="blog">Blog framework</option>
          </select>
        </label>
        <label class="form-field">Client
          <select id="sp-client"><option value="">No client</option></select>
        </label>
        <label class="form-field">Locale
          <select id="sp-locale">
            <option value="en-US" selected>American English (en-US)</option>
            <option value="en-GB">British English (en-GB)</option>
            <option value="sv-SE">Swedish (sv-SE)</option>
          </select>
        </label>
        <label class="form-field">Brand name(s)
          <input id="sp-brand" type="text" placeholder="e.g. Acme, Acme Software" autocomplete="off">
        </label>
      </div>
      <div class="sp-toggles">
        <label class="cq-sheet-picker__cross">
          <input type="checkbox" id="sp-check-site" checked>
          <span>Compare Frame and Copy with the live site</span>
        </label>
        <label class="cq-sheet-picker__cross">
          <input type="checkbox" id="sp-check-published" checked>
          <span>Compare published copy against the site (needs "Compare with the live site")</span>
        </label>
      </div>
      <p class="form-error" id="sp-profile-error" role="alert"></p>
      <button class="button button--primary" type="submit" id="sp-submit">Run check</button>
    </form>`;

  void fillClientOptions(mount.querySelector("#sp-client"));
  mount.querySelector("#sp-check-site").addEventListener("change", (event) => {
    wizard.checkSite = event.target.checked;
    const published = mount.querySelector("#sp-check-published");
    if (!wizard.checkSite) { published.checked = false; wizard.checkPublished = false; published.disabled = true; }
    else published.disabled = false;
  });
  mount.querySelector("#sp-check-published").addEventListener("change", (event) => {
    wizard.checkPublished = event.target.checked;
  });
  mount.querySelector("#sp-profile-form").addEventListener("submit", (event) => {
    event.preventDefault();
    void runCheck();
  });
}

async function fillClientOptions(select) {
  if (!select) return;
  try {
    if (clientOptions === null) {
      const dashboard = await getDashboard({ period: "week" });
      clientOptions = (dashboard.clients || []).map((client) => ({ id: client.client_id, name: client.name }));
    }
    clientOptions.forEach((client) => {
      const option = document.createElement("option");
      option.value = client.id;
      option.textContent = client.name;
      select.append(option);
    });
  } catch {
    select.insertAdjacentHTML("beforeend", '<option value="" disabled>Client list unavailable</option>');
  }
}

async function runCheck() {
  const submit = root.querySelector("#sp-submit");
  const error = root.querySelector("#sp-profile-error");
  error.textContent = "";
  const payload = {
    file_id: wizard.resolved.file.id,
    sheets: wizard.inspection ? [...wizard.selectedSheets] : undefined,
    frameworks: [...wizard.selectedFrameworks.values()],
    cross_sheet: wizard.crossSheet,
    kind: root.querySelector("#sp-kind").value,
    intent: "commercial",
    locale: root.querySelector("#sp-locale").value,
    client_id: root.querySelector("#sp-client").value || null,
    brand: root.querySelector("#sp-brand").value.split(",").map((s) => s.trim()).filter(Boolean),
    check_site: wizard.checkSite,
    check_published: wizard.checkSite && wizard.checkPublished,
  };
  submit.disabled = true;
  submit.innerHTML = `${icon("loader-2", "icon icon--spin")}Run check`;
  try {
    const result = await checkStrategyPackage(payload);
    location.hash = buildToolsHash(TOOL_KEY, result.id);
  } catch (exception) {
    submit.disabled = false;
    submit.textContent = "Run check";
    error.textContent = exception.message;
  }
}

/* ------------------------------ Reports list ----------------------------- */

async function loadReports() {
  const token = ++requestToken;
  const body = root.querySelector("#sp-reports-body");
  try {
    const page = await getStrategyPackageReports();
    if (token !== requestToken) return;
    body.innerHTML = renderPackagesList(page.packages);
  } catch (error) {
    if (token !== requestToken) return;
    body.innerHTML = `
      <div class="state-panel state-failed">${icon("wifi-off")}
        <p><strong>Could not load reports.</strong><br>${escapeHtml(error.message)}</p>
        <button class="button button--primary" type="button" id="sp-reports-retry">Try again</button></div>`;
    body.querySelector("#sp-reports-retry").addEventListener("click", () => void loadReports());
  }
}

function renderPackagesList(packages) {
  if (!packages.length) {
    return `<p class="tool-empty">${icon("file-off")}<span>No reports yet. Run a check above to create the first one.</span></p>`;
  }
  return `
    <div class="table-scroll">
      <table class="tool-table">
        <caption class="visually-hidden">Past reports</caption>
        <thead><tr>
          <th scope="col">Strategy</th><th scope="col">Client</th><th scope="col">Frameworks</th>
          <th scope="col">Findings</th><th scope="col">Checked</th>
          <th scope="col"><span class="visually-hidden">Open</span></th>
        </tr></thead>
        <tbody>${packages.map(renderPackageRow).join("")}</tbody>
      </table>
    </div>`;
}

function renderPackageRow(pkg) {
  const counts = pkg.severity_counts || {};
  const href = buildToolsHash(TOOL_KEY, pkg.id);
  const badges = ["error", "warn", "info"].filter((sev) => counts[sev]).map((sev) => `
    <span class="cq-row-badge cq-sev--${sev}">${counts[sev]} ${sev === "warn" ? "warning" : sev}${counts[sev] === 1 ? "" : "s"}</span>`
  ).join("") || `<span class="muted">No issues</span>`;
  return `
    <tr>
      <td class="tool-table__main"><a href="${href}">${escapeHtml(pkg.source_name)}</a></td>
      <td>${pkg.client_name
        ? `<span class="report-row__client">${icon("building-store", "icon icon--tiny")}${escapeHtml(pkg.client_name)}</span>`
        : '<span class="muted">No client</span>'}</td>
      <td>${pkg.framework_count}</td>
      <td>${badges}</td>
      <td class="report-row__date">${escapeHtml(formatTimestamp(pkg.created_at, "—"))}</td>
      <td class="tool-table__actions"><a class="button button--quiet" href="${href}">Open${icon("arrow-right", "icon icon--tiny")}</a></td>
    </tr>`;
}

/* ------------------------------- Report view ----------------------------- */

// task-91 §2.5: Comparison — the literal Strategy/Framework/Site/Verdict
// table the operator asked for — is the default landing tab; Overview and
// the findings-centric tabs (Strategy/Frameworks/Cross-checks) stay
// alongside it, since the matrix shows six fields while the engine checks
// dozens of rules the matrix cannot represent.
let activeTab = "comparison";
let activeFrameworkId = null;
let currentPackage = null;
let comparisonMismatchesOnly = false;

async function renderReportView(packageId) {
  activeTab = "comparison";
  activeFrameworkId = null;
  comparisonMismatchesOnly = false;
  root.innerHTML = `
    <a class="back-link" href="${buildToolsHash(TOOL_KEY)}">${icon("arrow-left")}<span>All reports</span></a>
    <header class="tool-header">
      <p class="eyebrow">Tools / Strategy Package Check</p>
      <h1>Strategy package check</h1>
    </header>
    <section class="tool-panel" aria-labelledby="sp-results-title">
      <div class="section-heading"><div><h2 id="sp-results-title" class="visually-hidden">Findings</h2></div></div>
      <div class="tool-panel__body" id="sp-report-body" aria-live="polite">
        ${panelSkeleton({ label: "report", rows: 5, columns: 1 })}
      </div>
    </section>`;
  const token = ++requestToken;
  const body = root.querySelector("#sp-report-body");
  try {
    const pkg = await getStrategyPackageReport(packageId);
    if (token !== requestToken) return;
    currentPackage = pkg;
    body.innerHTML = renderPackageShell(pkg);
    wirePackageTabs(body, pkg);
  } catch (error) {
    if (token !== requestToken) return;
    body.innerHTML = renderError(error);
  }
}

function renderPackageShell(pkg) {
  const chips = [
    pkg.client_name || null,
    SOURCE_KIND_LABEL[pkg.source_kind] || pkg.source_kind,
    `${pkg.framework_count} framework${pkg.framework_count === 1 ? "" : "s"}`,
  ].filter(Boolean);
  return `
    <div class="cq-report" data-testid="sp-report">
      <div class="cq-report__meta">
        <span class="cq-report__file">${icon("file-stack", "icon icon--tiny")}${escapeHtml(pkg.source_name)}</span>
        ${pkg.created_at ? `<span class="cq-report__date">${escapeHtml(formatTimestamp(pkg.created_at))}</span>` : ""}
        <span class="cq-report__profile">${chips.map(escapeHtml).join(" · ")}</span>
        <button class="button button--quiet cq-report__link" type="button" id="sp-copy-link">
          ${icon("link", "icon icon--tiny")}Copy link
        </button>
      </div>
      <ul class="cq-summary">${renderPackageSummary(pkg)}</ul>
      <div class="tablist sp-tablist" role="tablist" aria-label="Package report sections">
        <button type="button" role="tab" id="sp-tab-comparison" data-sp-tab="comparison" aria-selected="true">${icon("table", "icon icon--tiny")}Comparison</button>
        <button type="button" role="tab" id="sp-tab-overview" data-sp-tab="overview" aria-selected="false">${icon("layout-grid", "icon icon--tiny")}Overview</button>
        <button type="button" role="tab" id="sp-tab-strategy" data-sp-tab="strategy" aria-selected="false">${icon("file-spreadsheet", "icon icon--tiny")}Strategy</button>
        <button type="button" role="tab" id="sp-tab-frameworks" data-sp-tab="frameworks" aria-selected="false">${icon("file-text", "icon icon--tiny")}Frameworks</button>
        <button type="button" role="tab" id="sp-tab-cross" data-sp-tab="cross" aria-selected="false">${icon("arrows-cross", "icon icon--tiny")}Cross-checks</button>
      </div>
      <div id="sp-tabpanel" role="tabpanel"></div>
    </div>`;
}

function renderPackageSummary(pkg) {
  const counts = pkg.severity_counts || {};
  const meta = {
    error: { label: "Error", icon: "alert-triangle", cls: "cq-sev--error" },
    warn: { label: "Warning", icon: "alert-circle", cls: "cq-sev--warn" },
    info: { label: "Info", icon: "info-circle", cls: "cq-sev--info" },
  };
  return ["error", "warn", "info"].map((sev) => {
    const m = meta[sev];
    const n = counts[sev] || 0;
    return `
      <li class="cq-summary__item ${m.cls}">
        ${icon(m.icon, "icon icon--tiny")}<strong>${n}</strong><span>${m.label}${n === 1 ? "" : "s"}</span>
      </li>`;
  }).join("");
}

function wirePackageTabs(container, pkg) {
  container.querySelector("#sp-copy-link").addEventListener("click", () => {
    void navigator.clipboard?.writeText(`${location.origin}${location.pathname}${buildToolsHash(TOOL_KEY, pkg.id)}`)
      .then(() => toastLink());
  });
  container.querySelectorAll("[data-sp-tab]").forEach((button) => {
    button.addEventListener("click", () => {
      activeTab = button.dataset.spTab;
      container.querySelectorAll("[data-sp-tab]").forEach((b) => b.setAttribute("aria-selected", String(b === button)));
      renderTabPanel(container, pkg);
    });
  });
  renderTabPanel(container, pkg);
}

function toastLink() {
  const region = document.querySelector("#toast-region");
  if (!region) return;
  const element = document.createElement("div");
  element.className = "toast-message";
  element.textContent = "Link copied";
  region.append(element);
  setTimeout(() => element.remove(), 4200);
}

function renderTabPanel(container, pkg) {
  const panel = container.querySelector("#sp-tabpanel");
  if (activeTab === "comparison") { renderComparisonTab(panel, pkg); return; }
  if (activeTab === "overview") { panel.innerHTML = renderOverviewTab(pkg); wireOverviewTab(panel); return; }
  if (activeTab === "strategy") { renderStrategyTab(panel, pkg); return; }
  if (activeTab === "frameworks") { renderFrameworksTab(panel, pkg); return; }
  renderCrossTab(panel, pkg);
}

/* ----------------------------- Comparison tab ---------------------------- */
/* Grouped by PAGE, not by source (task-91 §3.6, corrected live per the
 * operator's direct feedback): for one URL, three tables run one after
 * another — Strategy, Framework, Site — so all three sit in the same
 * screenful and the eye compares down the column without scrolling past
 * every other page first. 10 pages -> 10 groups of 3 tables (30 total),
 * not 3 giant tables of 10 rows each (the first cut, corrected). Verdict
 * is not a fourth table — it rides as inline badges on the group header,
 * so the count stays exactly 3 tables per page as confirmed live. */

const MATRIX_FIELDS = ["url", "title", "description", "h1", "keywords", "links"];
const MATRIX_FIELD_LABELS = { url: "URL", title: "Title", description: "Description", h1: "H1", keywords: "Keywords", links: "Outgoing links" };
const MATRIX_BLOCKS = [
  { side: "strategy", title: "Strategy", icon: "file-spreadsheet" },
  { side: "framework", title: "Framework", icon: "file-text" },
  { side: "site", title: "Site", icon: "world" },
];
const MATRIX_VERDICT_META = {
  match: { icon: "circle-check", cls: "sp-matrix-verdict--match", label: "match" },
  mismatch: { icon: "alert-triangle", cls: "sp-matrix-verdict--mismatch", label: "mismatch" },
  not_comparable: { icon: "circle-dashed", cls: "sp-matrix-verdict--na", label: "n/a" },
};

function matrixRowHasMismatch(row) {
  return MATRIX_FIELDS.some((f) => row.verdicts[f].state === "mismatch");
}

function renderComparisonTab(panel, pkg) {
  const allRows = pkg.matrix || [];
  if (!allRows.length) {
    panel.innerHTML = `
      <p class="sp-browse__status">${icon("table-off", "icon icon--tiny")}
        <span>This report predates the comparison table, or the strategy had no pages to compare.
          Run the check again to get one.</span></p>`;
    return;
  }
  const rows = comparisonMismatchesOnly ? allRows.filter(matrixRowHasMismatch) : allRows;
  const mismatchCount = allRows.filter(matrixRowHasMismatch).length;

  panel.innerHTML = `
    <div class="sp-matrix-toolbar">
      <label class="cq-sheet-picker__cross sp-matrix-toolbar__filter">
        <input type="checkbox" id="sp-matrix-mismatches-only" ${comparisonMismatchesOnly ? "checked" : ""}>
        <span>Mismatches only (${mismatchCount} of ${allRows.length} page${allRows.length === 1 ? "" : "s"})</span>
      </label>
      <a class="button button--quiet" href="${strategyPackageMatrixXlsxUrl(pkg.id)}">
        ${icon("file-spreadsheet", "icon icon--tiny")}Export to Excel
      </a>
    </div>
    ${rows.length ? rows.map(renderMatrixPageGroup).join("") : `
      <p class="sp-browse__status">${icon("filter-x", "icon icon--tiny")}<span>No mismatches in this run.</span></p>`}`;

  panel.querySelector("#sp-matrix-mismatches-only")?.addEventListener("change", (event) => {
    comparisonMismatchesOnly = event.target.checked;
    renderComparisonTab(panel, pkg);
  });
}

function renderMatrixCell(cell) {
  if (cell.state === "absent") return `<span class="sp-matrix-cell sp-matrix-cell--absent">— no such column</span>`;
  if (cell.state === "empty") return `<span class="sp-matrix-cell sp-matrix-cell--empty">(empty)</span>`;
  if (cell.text !== null && cell.text !== undefined) return escapeHtml(cell.text);
  const items = cell.items || [];
  const shown = items.slice(0, 4).map(escapeHtml).join("; ");
  return items.length > 4 ? `${shown} <span class="sp-matrix-cell--more">(+${items.length - 4} more)</span>` : shown;
}

function renderMatrixVerdictBadges(verdicts) {
  return MATRIX_FIELDS.map((f) => {
    const v = verdicts[f];
    const meta = MATRIX_VERDICT_META[v.state];
    const title = v.detail ? `${MATRIX_FIELD_LABELS[f]}: ${v.detail}` : `${MATRIX_FIELD_LABELS[f]}: ${meta.label}`;
    return `
      <span class="sp-matrix-verdict-badge ${meta.cls}" title="${escapeHtml(title)}">
        ${icon(meta.icon, "icon icon--tiny")}<span>${MATRIX_FIELD_LABELS[f]}</span>
      </span>`;
  }).join("");
}

function renderMatrixPageGroup(row) {
  return `
    <div class="sp-matrix-page">
      <div class="sp-matrix-page__header">
        <h3 class="sp-matrix-page__title">${escapeHtml(row.label)}</h3>
        <div class="sp-matrix-page__verdicts">${renderMatrixVerdictBadges(row.verdicts)}</div>
      </div>
      ${MATRIX_BLOCKS.map((b) => renderMatrixMiniTable(b, row)).join("")}
    </div>`;
}

// Live feedback: a mismatch must flag only the source that actually
// drifted, not every source that happens to carry a value. When Strategy
// and Framework agree and only Site differs, Strategy/Framework read
// green (they agree with each other/each other's source), only Site red.
// `outlier_sources` (task-91 §3.6) carries which source(s) are the odd
// one out for scalar fields (url/title/description/h1); keywords/links
// verdicts don't compute per-source outliers (different claim-vs-confirm
// shape), so a mismatch there still flags every source, same as before.
function matrixCellTone(verdict, side) {
  if (verdict.state === "match") return "match";
  if (verdict.state === "mismatch") {
    const outliers = verdict.outlier_sources || [];
    if (outliers.length > 0) return outliers.includes(side) ? "mismatch" : "match";
    return "mismatch";
  }
  return null;
}

function renderMatrixMiniTable(block, row) {
  const cells = row[block.side];
  // Only a cell that actually HAS a value can be the agreeing/outlier side
  // of a comparison — an empty/absent cell contributed nothing to the
  // verdict, coloring it red or green would claim an opinion it never had.
  const toneFor = (f) => (cells && cells[f].state === "value" ? matrixCellTone(row.verdicts[f], block.side) : null);
  return `
    <div class="sp-matrix-mini">
      <p class="sp-matrix-mini__title">${icon(block.icon, "icon icon--tiny")}${block.title}</p>
      <div class="table-scroll sp-matrix-scroll">
        <table class="tool-table sp-matrix-table">
          <caption class="visually-hidden">${block.title} values for ${escapeHtml(row.label)}</caption>
          <thead><tr>
            ${MATRIX_FIELDS.map((f) => {
              const tone = toneFor(f);
              return `<th scope="col" class="${tone ? `sp-matrix-th--${tone}` : ""}">${MATRIX_FIELD_LABELS[f]}</th>`;
            }).join("")}
          </tr></thead>
          <tbody>
            <tr>
              ${cells
                ? MATRIX_FIELDS.map((f) => {
                    const tone = toneFor(f);
                    return `<td class="${tone ? `sp-matrix-cell--${tone}` : ""}">${renderMatrixCell(cells[f])}</td>`;
                  }).join("")
                : `<td colspan="${MATRIX_FIELDS.length}" class="sp-matrix-cell sp-matrix-cell--absent">— not in this run</td>`}
            </tr>
          </tbody>
        </table>
      </div>
    </div>`;
}

function renderOverviewTab(pkg) {
  const sheetBlock = pkg.sheets && pkg.sheets.length ? renderSheetScope(pkg.sheets) : "";
  return `
    ${sheetBlock}
    <div class="sp-fw-overview">
      <p class="cq-sheet-picker__label">Frameworks in this run</p>
      ${pkg.frameworks.length ? `
        <ul class="sp-fw-overview__list">
          ${pkg.frameworks.map((o, i) => renderFrameworkOverviewRow(o, i)).join("")}
        </ul>` : `
        <p class="sp-browse__status">${icon("file-off", "icon icon--tiny")}<span>No frameworks were checked in this run.</span></p>`}
    </div>
    ${pkg.aggregate_findings.length ? `
      <p class="sp-cross-teaser">${icon("arrows-cross", "icon icon--tiny")}
        <span>${pkg.aggregate_findings.length} strategy row${pkg.aggregate_findings.length === 1 ? "" : "s"} with no
          matching framework — see <button type="button" class="link-button" data-sp-goto-cross>Cross-checks</button>.</span></p>` : ""}`;
}

function renderSheetScope(sheets) {
  return `
    <div class="cq-scope">
      <p class="cq-scope__label">${icon("list-check", "icon icon--tiny")}<span>Sheets in this run</span></p>
      <ul class="cq-scope__list">
        ${sheets.map((s) => {
          const meta = SHEET_STATE_META[s.state] || SHEET_STATE_META.no_recognized_columns;
          return `
            <li class="cq-scope__item ${meta.cls}">
              ${icon(meta.icon, "icon icon--tiny")}
              <span class="cq-scope__name">${escapeHtml(s.name)}</span>
              <span class="cq-scope__detail">${s.item_count} row${s.item_count === 1 ? "" : "s"} — ${meta.label}</span>
            </li>`;
        }).join("")}
      </ul>
    </div>`;
}

function renderFrameworkOverviewRow(outcome, index) {
  const state = FRAMEWORK_STATE_META[outcome.state] || FRAMEWORK_STATE_META.not_accessible;
  const name = outcome.source.document_name || outcome.source.document_id;
  const crossBadges = ["frame_strategy_info", "frame_site_info", "copy_site_info"]
    .map((key) => renderCrossBadge(key, outcome[key])).join("");
  return `
    <li class="sp-fw-row ${state.cls}">
      <div class="sp-fw-row__head">
        ${icon(state.icon, "icon icon--tiny")}
        <strong>${escapeHtml(name)}</strong>
        <span class="sp-fw-row__state">${state.label}</span>
        <button type="button" class="link-button" data-sp-view-fw="${index}">View findings${icon("arrow-right", "icon icon--tiny")}</button>
      </div>
      ${outcome.reason ? `<p class="sp-fw-row__reason">${escapeHtml(outcome.reason)}</p>` : ""}
      <div class="sp-fw-row__cross">${crossBadges}</div>
    </li>`;
}

function renderCrossBadge(key, info) {
  const name = CROSS_CHECK_NAMES[key.replace("_info", "")];
  const meta = info ? (CROSS_STATE_META[info.state] || CROSS_STATE_META.not_applicable) : CROSS_STATE_META.not_applicable;
  const reason = info?.reason ? ` — ${info.reason}` : "";
  return `
    <span class="sp-cross-badge ${meta.cls}" title="${escapeHtml(`${name}: ${meta.label}${reason}`)}">
      ${icon(meta.icon, "icon icon--tiny")}<span>${name}</span>
    </span>`;
}

function wireOverviewTab(panel) {
  panel.querySelectorAll("[data-sp-view-fw]").forEach((button) => {
    button.addEventListener("click", () => {
      const outcome = currentPackage.frameworks[Number(button.dataset.spViewFw)];
      activeFrameworkId = outcome.source.document_id;
      activeTab = "frameworks";
      const container = panel.closest(".cq-report");
      container.querySelectorAll("[data-sp-tab]").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.spTab === "frameworks")));
      renderTabPanel(container, currentPackage);
    });
  });
  panel.querySelector("[data-sp-goto-cross]")?.addEventListener("click", () => {
    activeTab = "cross";
    const container = panel.closest(".cq-report");
    container.querySelectorAll("[data-sp-tab]").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.spTab === "cross")));
    renderTabPanel(container, currentPackage);
  });
}

function strategySeverityCounts(findings) {
  const counts = { error: 0, warn: 0, info: 0 };
  findings.forEach((f) => { counts[f.severity] = (counts[f.severity] || 0) + 1; });
  return counts;
}

function renderStrategyTab(panel, pkg) {
  const itemCount = (pkg.sheets || [])
    .filter((s) => s.state === "checked")
    .reduce((sum, s) => sum + (s.item_count || 0), 0);
  const report = {
    source_filename: pkg.source_name, item_count: itemCount, client_name: pkg.client_name,
    created_at: pkg.created_at, sheets: pkg.sheets || [], findings: pkg.strategy_findings,
    severity_counts: strategySeverityCounts(pkg.strategy_findings),
  };
  panel.innerHTML = renderReport(report);
  wireReport(panel, report);
}

function renderFrameworksTab(panel, pkg) {
  if (!pkg.frameworks.length) {
    panel.innerHTML = `<p class="sp-browse__status">${icon("file-off", "icon icon--tiny")}<span>No frameworks were checked in this run.</span></p>`;
    return;
  }
  if (!activeFrameworkId || !pkg.frameworks.some((o) => o.source.document_id === activeFrameworkId)) {
    activeFrameworkId = pkg.frameworks[0].source.document_id;
  }
  panel.innerHTML = `
    <div class="sp-fw-picker" role="group" aria-label="Choose a framework">
      ${pkg.frameworks.map((o) => {
        const name = o.source.document_name || o.source.document_id;
        const state = FRAMEWORK_STATE_META[o.state] || FRAMEWORK_STATE_META.not_accessible;
        const active = o.source.document_id === activeFrameworkId;
        return `
          <button type="button" class="cq-chip sp-fw-chip ${state.cls}${active ? " cq-chip--active" : ""}" data-sp-fw-pick="${escapeHtml(o.source.document_id)}">
            ${icon(state.icon, "icon icon--tiny")}<span class="cq-chip__label">${escapeHtml(name)}</span>
          </button>`;
      }).join("")}
    </div>
    <div id="sp-fw-detail"></div>`;
  panel.querySelectorAll("[data-sp-fw-pick]").forEach((button) => {
    button.addEventListener("click", () => {
      activeFrameworkId = button.dataset.spFwPick;
      renderFrameworksTab(panel, pkg);
    });
  });
  renderFrameworkDetail(panel.querySelector("#sp-fw-detail"), pkg.frameworks.find((o) => o.source.document_id === activeFrameworkId));
}

function renderFrameworkDetail(mount, outcome) {
  const state = FRAMEWORK_STATE_META[outcome.state] || FRAMEWORK_STATE_META.not_accessible;
  const tabs = [
    outcome.frame_tab_title ? `Frame: “${outcome.frame_tab_title}”` : null,
    outcome.copy_tab_title ? `Content: “${outcome.copy_tab_title}”` : null,
  ].filter(Boolean).join(" · ");
  const crossBadges = ["frame_strategy_info", "frame_site_info", "copy_site_info"]
    .map((key) => renderCrossBadge(key, outcome[key])).join("");

  if (outcome.state !== "checked") {
    mount.innerHTML = `
      <div class="state-panel ${outcome.state === "not_accessible" ? "state-failed" : ""}">
        ${icon(state.icon)}
        <p><strong>${escapeHtml(state.label)}.</strong><br>${escapeHtml(outcome.reason || "")}</p>
      </div>`;
    return;
  }

  const tabCount = (outcome.frame_tab_title ? 1 : 0) + (outcome.copy_tab_title ? 1 : 0);
  const report = {
    source_filename: outcome.source.document_name || outcome.source.document_id,
    item_count: tabCount, findings: outcome.findings, sheets: [],
    severity_counts: strategySeverityCounts(outcome.findings),
  };
  mount.innerHTML = `
    ${tabs ? `<p class="sp-fw-detail__tabs">${icon("layout-list", "icon icon--tiny")}${escapeHtml(tabs)}</p>` : ""}
    <div class="sp-fw-row__cross sp-fw-detail__cross">${crossBadges}</div>
    <div id="sp-fw-report">${renderReport(report)}</div>`;
  wireReport(mount.querySelector("#sp-fw-report"), report);
}

function renderCrossTab(panel, pkg) {
  const report = {
    source_filename: "Cross-checks", item_count: pkg.frameworks.length,
    findings: pkg.aggregate_findings, sheets: [],
    severity_counts: strategySeverityCounts(pkg.aggregate_findings),
  };
  panel.innerHTML = `
    ${renderCrossStateTable(pkg)}
    <div id="sp-cross-report">${renderReport(report)}</div>`;
  wireReport(panel.querySelector("#sp-cross-report"), report);
}

function renderCrossStateTable(pkg) {
  if (!pkg.frameworks.length) return "";
  return `
    <div class="table-scroll sp-cross-table-wrap">
      <table class="tool-table sp-cross-table">
        <caption class="visually-hidden">Cross-check coverage per framework</caption>
        <thead><tr>
          <th scope="col">Framework</th><th scope="col">Frame ↔ Strategy</th>
          <th scope="col">Frame ↔ Live site</th><th scope="col">Copy ↔ Live site</th>
        </tr></thead>
        <tbody>
          ${pkg.frameworks.map((o) => `
            <tr>
              <td class="tool-table__main">${escapeHtml(o.source.document_name || o.source.document_id)}</td>
              <td>${renderCrossBadge("frame_strategy_info", o.frame_strategy_info)}</td>
              <td>${renderCrossBadge("frame_site_info", o.frame_site_info)}</td>
              <td>${renderCrossBadge("copy_site_info", o.copy_site_info)}</td>
            </tr>`).join("")}
        </tbody>
      </table>
    </div>`;
}
