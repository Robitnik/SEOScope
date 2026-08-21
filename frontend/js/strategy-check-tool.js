/* Strategy Check tool (task-53): upload an SEO-strategy workbook, get SEO/
 * anti-slop/brand-voice findings back. Persisted server-side
 * (`content_qa_reports`), so every run gets a real `id` and survives reload
 * — list view (past reports) + detail view (`#/tools/strategy-check/<id>`,
 * shareable), the same two-view shape as `alt-text-tool.js`.
 */
import { checkStrategy, getStrategyReport, getStrategyReports } from "./api.js?v=20260821-spin3";
import { buildToolsHash } from "./router.js?v=20260821-spin3";
import { escapeHtml, icon } from "./formatters.js?v=20260821-spin3";
import { panelSkeleton } from "./skeleton.js?v=20260821-spin3";
import { renderError, renderReport, renderReportsList, wireReport } from "./content-qa-render.js?v=20260821-spin3";

const TOOL_KEY = "strategy-check";
let root = null;
let requestToken = 0;

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
      <div class="cq-fields">
        <label class="form-field">Locale
          <select id="cq-locale">
            <option value="en-US" selected>American English (en-US)</option>
            <option value="en-GB">British English (en-GB)</option>
          </select>
        </label>
        <label class="form-field">Brand name(s)
          <input id="cq-brand" type="text" placeholder="e.g. Acme, Acme Software" autocomplete="off">
        </label>
      </div>
      <p class="field-help">Brand names catch titles padded past the meta limit with " | Brand" — leave
        blank to skip that check.</p>
      <p class="form-error" id="cq-form-error" role="alert"></p>
      <button class="button button--primary" type="submit" id="cq-submit">Run check</button>
    </form>`;
  body.querySelector("#cq-file").addEventListener("change", (event) => {
    const [file] = event.target.files;
    body.querySelector("#cq-file-name").textContent = file ? file.name : "No file selected";
  });
  body.querySelector("#cq-form").addEventListener("submit", (event) => {
    event.preventDefault();
    void runCheck();
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
    locale: root.querySelector("#cq-locale").value,
    brandNames: root.querySelector("#cq-brand").value.split(",").map((s) => s.trim()).filter(Boolean),
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
    wireReport(body);
  } catch (error) {
    if (token !== requestToken) return;
    body.innerHTML = renderError(error);
  }
}
