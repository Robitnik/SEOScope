/* Framework Check tool (task-53): upload a standalone page/blog-framework
 * brief (DOCX, no sibling framework_data.json — that path is already covered
 * by AISeoTools' own `framework-qa`), get the same rule catalog back as
 * Strategy Check. Persisted server-side (`content_qa_reports`) — list view
 * (past reports) + detail view (`#/tools/framework-check/<id>`, shareable),
 * the same two-view shape as `strategy-check-tool.js`/`alt-text-tool.js`.
 */
import { checkFramework, getFrameworkReport, getFrameworkReports } from "./api.js?v=20260821-spin3";
import { buildToolsHash } from "./router.js?v=20260821-spin3";
import { escapeHtml, icon } from "./formatters.js?v=20260821-spin3";
import { panelSkeleton } from "./skeleton.js?v=20260821-spin3";
import { renderError, renderReport, renderReportsList, wireReport } from "./content-qa-render.js?v=20260821-spin3";

const TOOL_KEY = "framework-check";
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
      <p class="eyebrow">Tools / Framework Check</p>
      <h1>Framework check</h1>
      <p class="tool-lead">Upload a page or blog framework brief and get every meta-length, heading,
        FAQ, brand-voice and slop issue back at once — before it goes to the copywriter.</p>
    </header>
    <section class="tool-panel" aria-labelledby="cq-panel-title">
      <div class="section-heading"><div><h2 id="cq-panel-title">Check a framework</h2>
        <p class="tool-panel__hint">Accepted: .docx (page or blog framework brief) — up to 10 MB.</p></div></div>
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
        <span class="cq-file__label">Framework brief</span>
        <input id="cq-file" class="cq-file__input" type="file" accept=".docx" required>
        <span class="cq-file__control">
          <span class="button button--primary" aria-hidden="true">${icon("upload")}Choose file…</span>
          <span class="cq-file__name" id="cq-file-name">No file selected</span>
        </span>
      </label>
      <div class="cq-fields">
        <label class="form-field">Framework type
          <select id="cq-kind">
            <option value="page" selected>Page framework</option>
            <option value="blog">Blog framework</option>
          </select>
        </label>
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
      <p class="field-help">Framework type matters: the FAQ question-count band applies to blog
        frameworks only — a page framework has no FAQ minimum or maximum by design.</p>
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
    error.textContent = "Choose a framework brief first.";
    fileInput.focus();
    return;
  }

  const profile = {
    kind: root.querySelector("#cq-kind").value,
    locale: root.querySelector("#cq-locale").value,
    brandNames: root.querySelector("#cq-brand").value.split(",").map((s) => s.trim()).filter(Boolean),
  };

  submit.disabled = true;
  submit.innerHTML = `${icon("loader-2", "icon icon--spin")}Run check`;
  try {
    const report = await checkFramework(file, profile);
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
    const page = await getFrameworkReports();
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
      <p class="eyebrow">Tools / Framework Check</p>
      <h1>Framework check</h1>
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
    const report = await getFrameworkReport(reportId);
    if (token !== requestToken) return;
    body.innerHTML = renderReport(report, { toolKey: TOOL_KEY });
    wireReport(body);
  } catch (error) {
    if (token !== requestToken) return;
    body.innerHTML = renderError(error);
  }
}
