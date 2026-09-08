/* Strategy Check tool (task-53, extended task-87): upload an SEO-strategy
 * workbook, get SEO/anti-slop/brand-voice findings back. Persisted server-side
 * (`content_qa_reports`), so every run gets a real `id` and survives reload
 * — list view (past reports) + detail view (`#/tools/strategy-check/<id>`,
 * shareable), the same two-view shape as `alt-text-tool.js`.
 *
 * task-87 adds three things to the form: a Swedish locale option, a sheet
 * picker (a workbook usually holds more than one quarter's strategy — see
 * `inspectStrategyWorkbook()`), and a client picker with domain-based locale
 * autofill (the same heuristic `alt-text-tool.js` already uses for its own
 * client field, duplicated here rather than imported — see
 * `guessContentQaClient()` in content-qa-render.js for why).
 */
import {
  checkStrategy, getDashboard, getStrategyReport, getStrategyReports, inspectStrategyWorkbook,
} from "./api.js?v=20260908-t87";
import { buildToolsHash } from "./router.js?v=20260908-t87";
import { escapeHtml, icon } from "./formatters.js?v=20260908-t87";
import { panelSkeleton } from "./skeleton.js?v=20260908-t87";
import {
  guessContentQaClient, guessLocaleForDomain, renderError, renderReport, renderReportsList,
  wireReport,
} from "./content-qa-render.js?v=20260908-t87";

const TOOL_KEY = "strategy-check";
let root = null;
let requestToken = 0;

// Клієнти для пікера (task-87 §1.6) — кешовані на модуль, той самий патерн,
// що `alt-text-tool.js`'s `clientOptions`: один запит на весь час на сторінці
// tools, не на кожен upload.
let clientOptions = null;

// Стан вибору аркуша (task-87 §1.3) — живе, поки на екрані форма кроку 1;
// скидається щоразу, коли обирають новий файл.
let sheetState = { file: null, inspection: null, selectedSheets: new Set(), crossSheet: false, error: null };

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
  root.innerHTML = `
    <a class="back-link" href="${buildToolsHash()}">${icon("arrow-left")}<span>All tools</span></a>
    <header class="tool-header">
      <p class="eyebrow">Tools / Strategy Check</p>
      <h1>Strategy check</h1>
      <p class="tool-lead">Upload an SEO strategy workbook and get every meta-length, brand-voice,
        slop and cannibalization issue back at once — the same house rules a reviewer would apply by hand.</p>
    </header>
    <section class="tool-panel" aria-labelledby="cq-panel-title">
      <div class="section-heading"><div><h2 id="cq-panel-title">Check a strategy</h2>
        <p class="tool-panel__hint">Accepted: .xlsx (Excel or Google Sheets export) — up to 10 MB.</p></div></div>
      <div class="tool-panel__body" id="cq-form-body"></div>
    </section>
    <section class="tool-panel" id="cq-reports" aria-labelledby="cq-reports-title">
      <div class="section-heading"><div><h2 id="cq-reports-title">Reports</h2></div></div>
      <div class="tool-panel__body" id="cq-reports-body">
        ${panelSkeleton({ label: "reports", rows: 3, columns: 4 })}
      </div>
    </section>`;
  sheetState = { file: null, inspection: null, selectedSheets: new Set(), crossSheet: false, error: null };
  renderForm();
  await loadReports();
}

function renderForm() {
  const body = root.querySelector("#cq-form-body");
  body.innerHTML = `
    <form id="cq-form" novalidate>
      <label class="cq-file">
        <span class="cq-file__label">Strategy workbook</span>
        <input id="cq-file" class="cq-file__input" type="file" accept=".xlsx" required>
        <span class="cq-file__control">
          <span class="button button--primary" aria-hidden="true">${icon("upload")}Choose file…</span>
          <span class="cq-file__name" id="cq-file-name">No file selected</span>
        </span>
      </label>
      <div id="cq-sheet-picker"></div>
      <div class="cq-fields">
        <label class="form-field">Client
          <select id="cq-client"><option value="">No client</option></select>
        </label>
        <label class="form-field">Locale
          <select id="cq-locale">
            <option value="en-US" selected>American English (en-US)</option>
            <option value="en-GB">British English (en-GB)</option>
            <option value="sv-SE">Swedish (sv-SE)</option>
          </select>
        </label>
        <label class="form-field">Brand name(s)
          <input id="cq-brand" type="text" placeholder="e.g. Acme, Acme Software" autocomplete="off">
        </label>
      </div>
      <p class="field-help" id="cq-client-hint">Brand names catch titles padded past the meta limit with " | Brand" — leave
        blank to skip that check.</p>
      <p class="form-error" id="cq-form-error" role="alert"></p>
      <button class="button button--primary" type="submit" id="cq-submit">Run check</button>
    </form>`;

  void fillClientOptions(body.querySelector("#cq-client"));
  wireTouchTracking(body.querySelector("#cq-client"));
  wireTouchTracking(body.querySelector("#cq-locale"));

  body.querySelector("#cq-file").addEventListener("change", (event) => {
    const [file] = event.target.files;
    body.querySelector("#cq-file-name").textContent = file ? file.name : "No file selected";
    if (file) void handleFileSelected(file);
  });
  body.querySelector("#cq-form").addEventListener("submit", (event) => {
    event.preventDefault();
    void runCheck();
  });
}

/** Marks a field "touched" the moment a person changes it themselves — set
 * programmatically (autofill) never fires `change`, so this only ever
 * catches a real edit. Autofill checks this flag before overwriting a
 * field (task-87 §1.6: a manual locale choice is never clobbered by a
 * later client guess, in either order). */
function wireTouchTracking(select) {
  select.addEventListener("change", () => {
    select.dataset.touched = "1";
    select.closest(".form-field")?.classList.remove("form-field--autofilled");
  });
}

async function fillClientOptions(select) {
  try {
    if (clientOptions === null) {
      const dashboard = await getDashboard({ period: "week" });
      clientOptions = (dashboard.clients || []).map((client) => ({
        id: client.client_id, name: client.name, domain: client.domain || "",
      }));
    }
    clientOptions.forEach((client) => {
      const option = document.createElement("option");
      option.value = client.id;
      option.textContent = client.name;
      select.append(option);
    });
  } catch {
    // Клієнт — опційне поле: без списку звіт усе одно можна прогнати.
    select.insertAdjacentHTML("beforeend", '<option value="" disabled>Client list unavailable</option>');
  }
}

/** File chosen -> (1) client/locale autofill from the filename, (2)
 * `/inspect` for the sheet picker. Independent of each other — an
 * `/inspect` failure (network hiccup, truly malformed file `/check` will
 * reject anyway) never blocks the client guess or the ability to submit. */
async function handleFileSelected(file) {
  autofillFromFilename(file.name);

  sheetState = { file, inspection: null, selectedSheets: new Set(), crossSheet: false, error: null };
  const mount = root.querySelector("#cq-sheet-picker");
  if (!mount) return;
  mount.innerHTML = `<p class="cq-sheet-picker__loading">${icon("loader-2", "icon icon--spin")}Reading sheets…</p>`;

  const token = ++requestToken;
  try {
    const inspection = await inspectStrategyWorkbook(file);
    if (token !== requestToken || sheetState.file !== file) return;
    sheetState.inspection = inspection;
    sheetState.selectedSheets = new Set(inspection.suggested_sheets);
    renderSheetPicker();
  } catch (error) {
    if (token !== requestToken || sheetState.file !== file) return;
    sheetState.error = error;
    mount.innerHTML = `
      <p class="cq-sheet-picker__error">${icon("alert-triangle", "icon icon--tiny")}
        <span>Could not preview sheets (${escapeHtml(error.message)}) — the check will still run against every sheet.</span></p>`;
  }
}

function autofillFromFilename(filename) {
  if (!clientOptions) return;
  const guess = guessContentQaClient(filename, clientOptions);
  if (!guess) return;

  const clientSelect = root.querySelector("#cq-client");
  if (clientSelect && !clientSelect.dataset.touched) {
    clientSelect.value = guess.id;
    clientSelect.closest(".form-field")?.classList.add("form-field--autofilled");
    const hint = root.querySelector("#cq-client-hint");
    if (hint) hint.textContent = "Client picked from the file name — change it if that is wrong.";
  }

  const localeSelect = root.querySelector("#cq-locale");
  if (localeSelect && !localeSelect.dataset.touched) {
    localeSelect.value = guessLocaleForDomain(guess.domain);
    localeSelect.closest(".form-field")?.classList.add("form-field--autofilled");
  }
}

/** Renders the sheet picker: a single "Sheet: X" line when there is at most
 * one load-bearing sheet to choose from (nothing to pick), otherwise a
 * checkbox per sheet plus the cross-sheet-comparison toggle (task-87 §1.3 —
 * default OFF, unlike the API's own `cross_sheet=true` default kept for
 * backward-compatible callers that omit the param entirely). */
function renderSheetPicker() {
  const mount = root.querySelector("#cq-sheet-picker");
  if (!mount) return;
  const { inspection, selectedSheets, crossSheet } = sheetState;
  const loadBearing = inspection.sheets.filter((s) => s.load_bearing);

  if (loadBearing.length <= 1) {
    const only = loadBearing[0];
    mount.innerHTML = `
      <p class="cq-sheet-single">${icon("table", "icon icon--tiny")}
        <span>Sheet: <strong>${only ? escapeHtml(only.name) : "none recognized"}</strong></span></p>`;
    return;
  }

  mount.innerHTML = `
    <div class="cq-sheet-picker">
      <p class="cq-sheet-picker__label">Sheets to check</p>
      <ul class="cq-sheet-picker__list">
        ${inspection.sheets.map((s) => `
          <li class="cq-sheet-picker__item${s.load_bearing ? "" : " cq-sheet-picker__item--unrecognized"}">
            <label>
              <input type="checkbox" data-cq-sheet-toggle="${escapeHtml(s.name)}"
                ${selectedSheets.has(s.name) ? "checked" : ""} ${s.load_bearing ? "" : "disabled"}>
              <span>${escapeHtml(s.name)}</span>
              <span class="cq-sheet-picker__meta">${s.row_count} row${s.row_count === 1 ? "" : "s"}${s.load_bearing ? "" : " — no title/description columns"}</span>
            </label>
          </li>`).join("")}
      </ul>
      <label class="cq-sheet-picker__cross">
        <input type="checkbox" id="cq-cross-sheet" ${crossSheet ? "checked" : ""}>
        <span>Also compare duplicates/keywords across the selected sheets (off checks each sheet on its own)</span>
      </label>
    </div>`;

  mount.querySelectorAll("[data-cq-sheet-toggle]").forEach((checkbox) => {
    checkbox.addEventListener("change", () => {
      const name = checkbox.dataset.cqSheetToggle;
      if (checkbox.checked) sheetState.selectedSheets.add(name); else sheetState.selectedSheets.delete(name);
    });
  });
  mount.querySelector("#cq-cross-sheet").addEventListener("change", (event) => {
    sheetState.crossSheet = event.target.checked;
  });
}

async function runCheck() {
  const fileInput = root.querySelector("#cq-file");
  const error = root.querySelector("#cq-form-error");
  const submit = root.querySelector("#cq-submit");
  const [file] = fileInput.files;
  error.textContent = "";
  if (!file) {
    error.textContent = "Choose a strategy workbook first.";
    fileInput.focus();
    return;
  }

  const profile = {
    clientId: root.querySelector("#cq-client").value || null,
    locale: root.querySelector("#cq-locale").value,
    brandNames: root.querySelector("#cq-brand").value.split(",").map((s) => s.trim()).filter(Boolean),
    // `sheetState.inspection` — null дає `sheets: undefined` -> сервер
    // перевіряє все, той самий дефолт, що до task-87 (наприклад, /inspect
    // не встиг відповісти, а оператор уже тисне Run check).
    sheets: sheetState.inspection && sheetState.file === file
      ? [...sheetState.selectedSheets] : undefined,
    crossSheet: sheetState.inspection && sheetState.file === file ? sheetState.crossSheet : undefined,
  };

  submit.disabled = true;
  submit.innerHTML = `${icon("loader-2", "icon icon--spin")}Run check`;
  try {
    const report = await checkStrategy(file, profile);
    location.hash = buildToolsHash(TOOL_KEY, report.id);
  } catch (exception) {
    submit.disabled = false;
    submit.textContent = "Run check";
    error.textContent = exception.message;
  }
}

async function loadReports() {
  const token = ++requestToken;
  const body = root.querySelector("#cq-reports-body");
  try {
    const page = await getStrategyReports();
    if (token !== requestToken) return;
    body.innerHTML = renderReportsList(page.reports, TOOL_KEY);
  } catch (error) {
    if (token !== requestToken) return;
    body.innerHTML = `
      <div class="state-panel state-failed">${icon("wifi-off")}
        <p><strong>Could not load reports.</strong><br>${escapeHtml(error.message)}</p>
        <button class="button button--primary" type="button" id="cq-reports-retry">Try again</button></div>`;
    body.querySelector("#cq-reports-retry").addEventListener("click", () => void loadReports());
  }
}

async function renderReportView(reportId) {
  root.innerHTML = `
    <a class="back-link" href="${buildToolsHash(TOOL_KEY)}">${icon("arrow-left")}<span>All reports</span></a>
    <header class="tool-header">
      <p class="eyebrow">Tools / Strategy Check</p>
      <h1>Strategy check</h1>
    </header>
    <section class="tool-panel" aria-labelledby="cq-results-title">
      <div class="section-heading"><div><h2 id="cq-results-title">Findings</h2></div></div>
      <div class="tool-panel__body" id="cq-results-body" aria-live="polite">
        ${panelSkeleton({ label: "report", rows: 5, columns: 1 })}
      </div>
    </section>`;
  const token = ++requestToken;
  const body = root.querySelector("#cq-results-body");
  try {
    const report = await getStrategyReport(reportId);
    if (token !== requestToken) return;
    body.innerHTML = renderReport(report, { toolKey: TOOL_KEY });
    wireReport(body, report);
  } catch (error) {
    if (token !== requestToken) return;
    body.innerHTML = renderError(error);
  }
}
