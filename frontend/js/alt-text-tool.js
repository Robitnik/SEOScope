/* Image Alt Text tool (task-27).
 *
 * Робота оператора: завантажити таблицю аудиту → підтвердити вкладку й мапінг
 * колонок → отримати таблицю, де для кожної картинки вже є **фрагмент, за яким
 * медіатека WordPress її знаходить** → копіювати alt (це і є відмітка прогресу)
 * → за потреби перевірити сторінку без кешу.
 *
 * Уся семантика приходить з бекенда (фрагмент, вид URL, вердикт) — тут лише
 * рендер і оптимістичні відмітки прогресу (AGENTS.md §5).
 */
import {
  altTextExportCsvUrl,
  altTextExportXlsxUrl,
  createAltTextReport,
  deleteAltTextReport,
  getAltTextItems,
  getAltTextReport,
  getAltTextReports,
  getDashboard,
  resolveAltTextItemMedia,
  resolveAltTextReportMedia,
  setAltTextItemStatus,
  uploadAltTextSource,
  verifyAltTextItem,
} from "./api.js?v=20260819-spin3";
import { buildToolsHash, replaceToolsHash } from "./router.js?v=20260819-spin3";
import { panelSkeleton } from "./skeleton.js?v=20260819-spin3";
import { escapeHtml, formatTimestamp, icon } from "./formatters.js?v=20260819-spin3";

const PAGE_SIZE = 50;
const TOOL_KEY = "alt-text";

const STATUS_META = {
  pending: { label: "Pending", icon: "circle-dashed" },
  copied: { label: "Alt copied", icon: "clipboard-check" },
  applied: { label: "Applied", icon: "circle-check" },
  skipped: { label: "Skipped", icon: "circle-minus" },
};

const VERIFY_META = {
  match: { label: "Alt is live", icon: "circle-check", tone: "good" },
  mismatch: { label: "Different alt", icon: "alert-triangle", tone: "bad" },
  empty_alt: { label: "Alt is empty", icon: "alert-triangle", tone: "bad" },
  image_not_found_on_page: { label: "Image not in HTML", icon: "help-circle", tone: "warn" },
  fetch_failed: { label: "Page unavailable", icon: "wifi-off", tone: "warn" },
};

const URL_KIND_META = {
  uploads: { label: "Media library", tone: "good" },
  theme_asset: { label: "Theme file", tone: "warn" },
  plugin_asset: { label: "Plugin file", tone: "warn" },
  external: { label: "External host", tone: "warn" },
  unknown: { label: "Not recognized", tone: "bad" },
};

// Стани пошуку точного WordPress media ID (task-39). `null`/`undefined` —
// «ще не шукали» — це не стан із цієї таблиці, він рендериться окремо.
const MEDIA_STATUS_META = {
  resolved: { label: "Found", icon: "circle-check", tone: "good" },
  // "Ambiguous" can mean exactly one unconfirmed candidate as easily as
  // several — this generic label is only a fallback for places without the
  // candidate count (e.g. the batch summary counts rows, not candidates).
  // Anywhere a specific item is on hand, use `mediaMatchLabel()` instead.
  ambiguous: { label: "Needs confirmation", icon: "help-circle", tone: "warn" },
  not_found: { label: "Not found", icon: "search-off", tone: "bad" },
  rest_unavailable: { label: "REST API closed", icon: "lock", tone: "warn" },
  not_searchable: { label: "Not in media library", icon: "circle-off", tone: "bad" },
  failed: { label: "Site unavailable", icon: "wifi-off", tone: "warn" },
};

function mediaMatchLabel(count) {
  return count === 1 ? "1 match to confirm" : `${count} matches to confirm`;
}

const MEDIA_CONFIDENCE_LABEL = {
  exact: "Exact match",
  filename: "Matched by file name",
};

// Машинні попередження деривації → операторська підказка. Показуємо саме
// причину, а не загальне «увага»: оператор має розуміти, що з фрагментом.
const WARNING_COPY = {
  possible_duplicate_counter: "Name ends in a number — if nothing is found, try the shorter key.",
  external_host: "This file lives on another host, not on the client site.",
  not_a_media_library_path: "Not an uploads path — it may not be in the media library at all.",
  resize_query_dropped: "Resize parameters in the URL were ignored.",
  search_key_too_short: "The key is very short and may match many files.",
  search_key_truncated: "The file name was longer than we store and got truncated.",
  no_image_extension: "The URL has no image extension.",
  no_filename_in_url: "The URL has no file name to search for.",
  unsupported_scheme: "Only http(s) image URLs can be looked up.",
  empty_url: "This row had no image URL.",
  photon_proxy_unwrapped: "Unwrapped a Jetpack/Photon CDN URL.",
  statically_proxy_unwrapped: "Unwrapped a Statically CDN URL.",
  cloudflare_resize_unwrapped: "Unwrapped a Cloudflare image-resizing URL.",
  nested_uploads_path_unwrapped: "Unwrapped a WebP-converter path.",
  proxy_url_param_unwrapped: "Unwrapped an image proxy URL.",
};

let root = null;
let state = null;
let requestToken = 0;
let wizard = null;
let clientOptions = null;
// Остання відрендерена сторінка рядків — попап читає дані звідси, а не з DOM.
let currentRows = [];

export async function initTool(mount, route) {
  root = mount;
  requestToken += 1;
  if (route.reportId) {
    state = {
      reportId: route.reportId,
      q: route.query.get("q") || "",
      status: route.query.get("st") || "",
      urlKind: route.query.get("uk") || "",
      verify: route.query.get("vf") || "",
      mediaStatus: route.query.get("md") || "",
      offset: Number(route.query.get("off") || 0) || 0,
    };
    await renderReportView();
    return;
  }
  state = null;
  wizard = { step: "pick", file: null, upload: null, sheet: null, columnMap: {}, busy: false };
  await renderListView();
}

function persist() {
  replaceToolsHash(TOOL_KEY, state.reportId, {
    q: state.q,
    st: state.status,
    uk: state.urlKind,
    vf: state.verify,
    md: state.mediaStatus,
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

// --- list view: new report + report history -----------------------------------

async function renderListView() {
  root.innerHTML = `
    <a class="back-link" href="${buildToolsHash()}">${icon("arrow-left")}<span>All tools</span></a>
    <header class="tool-header">
      <p class="eyebrow">Tools / Image Alt Text</p>
      <h1>Image alt text</h1>
      <p class="tool-lead">Upload an image audit sheet. For every row you get the exact fragment the
        WordPress media library actually matches — the file name with resize, <code>-scaled</code> and
        editor suffixes removed — plus the new alt ready to copy.</p>
    </header>
    <section class="tool-panel" id="at-new" aria-labelledby="at-new-title">
      <div class="section-heading"><div><h2 id="at-new-title">New report</h2>
        <p class="tool-panel__hint">Accepted: .xlsx (Excel or Google Sheets export), .csv, .tsv — up to 10 MB.</p></div></div>
      <div class="tool-panel__body" id="at-new-body"></div>
    </section>
    <section class="tool-panel" id="at-reports" aria-labelledby="at-reports-title">
      <div class="section-heading"><div><h2 id="at-reports-title">Reports</h2></div></div>
      <div class="tool-panel__body" id="at-reports-body">
        ${panelSkeleton({ label: "reports", rows: 4, columns: 3 })}
      </div>
    </section>`;
  renderWizard();
  await loadReports();
}

async function loadReports() {
  const token = ++requestToken;
  const body = root.querySelector("#at-reports-body");
  try {
    const reports = await getAltTextReports();
    if (token !== requestToken) return;
    if (!reports.length) {
      body.innerHTML = `
        <p class="tool-empty">${icon("file-off")}<span>No reports yet. Upload an audit sheet above to create the first one.</span></p>`;
      return;
    }
    body.innerHTML = `
      <div class="table-scroll">
        <table class="tool-table">
          <caption class="visually-hidden">Image alt text reports</caption>
          <thead><tr>
            <th scope="col">Report</th><th scope="col">Client</th>
            <th scope="col">Images</th><th scope="col">Progress</th><th scope="col">Imported</th>
            <th scope="col"><span class="visually-hidden">Actions</span></th>
          </tr></thead>
          <tbody>${reports.map(reportRow).join("")}</tbody>
        </table>
      </div>`;
    applyMeters(body);
    body.querySelectorAll("button[data-action='delete']").forEach((button) => {
      button.addEventListener("click", () => void deleteReport(button.dataset.reportId, button.dataset.title));
    });
  } catch (error) {
    if (token !== requestToken) return;
    body.innerHTML = `
      <div class="state-panel state-failed">${icon("wifi-off")}
        <p><strong>Could not load reports.</strong><br>${escapeHtml(error.message)}</p>
        <button class="button button--primary" type="button" id="at-reports-retry">Try again</button></div>`;
    body.querySelector("#at-reports-retry").addEventListener("click", () => void loadReports());
  }
}

function reportRow(report) {
  const counts = report.status_counts || {};
  const done = (counts.copied || 0) + (counts.applied || 0);
  const applied = counts.applied || 0;
  const total = report.row_count || 0;
  // Дві шкали в одній смузі: підтверджене на сторінці (`applied`) — суцільним
  // акцентом, «alt скопійовано» — світлішим. Реальні частки, без анімації.
  const appliedPct = total ? Math.round((applied / total) * 100) : 0;
  const donePct = total ? Math.round((done / total) * 100) : 0;
  const source = report.source_sheet
    ? `${report.source_filename} · ${report.source_sheet}`
    : report.source_filename;
  const href = buildToolsHash(TOOL_KEY, report.id);
  return `
    <tr>
      <td class="tool-table__main">
        <a href="${href}">${escapeHtml(report.title)}</a>
        <span class="report-row__source">${escapeHtml(source)}</span>
      </td>
      <td>${report.client_name
        ? `<span class="report-row__client">${icon("building-store", "icon icon--tiny")}${escapeHtml(report.client_name)}</span>`
        : '<span class="muted">No client</span>'}</td>
      <td class="num">${total}${report.skipped_count
        ? `<small title="Rows in the file with no image URL">+${report.skipped_count} skipped</small>`
        : ""}</td>
      <td class="report-row__progress">
        <span class="meter" role="img" aria-label="${done} of ${total} images handled, ${applied} confirmed on the page">
          <span class="meter__done" data-pct="${donePct}"></span>
          <span class="meter__applied" data-pct="${appliedPct}"></span>
        </span>
        <span class="report-row__counts"><strong>${done}</strong>/${total}${applied ? ` · ${applied} live` : ""}</span>
      </td>
      <td class="report-row__date">${escapeHtml(formatTimestamp(report.created_at, "—"))}</td>
      <td class="tool-table__actions">
        <a class="button button--quiet" href="${href}">Open${icon("arrow-right", "icon icon--tiny")}</a>
        <button class="icon-button" type="button" data-action="delete" data-report-id="${escapeHtml(report.id)}"
          data-title="${escapeHtml(report.title)}" aria-label="${escapeHtml(`Delete report ${report.title}`)}">${icon("trash")}</button>
      </td>
    </tr>`;
}

// Ширину смуги ставимо через CSSOM, а НЕ атрибутом `style="width:…"`:
// застосунок віддає `Content-Security-Policy: default-src 'self'` без
// `unsafe-inline`, тож інлайновий стиль браузер просто блокує — і смуга
// лишалась порожньою. Присвоєння властивості під цю заборону не підпадає.
function applyMeters(container) {
  container.querySelectorAll(".meter [data-pct]").forEach((bar) => {
    bar.style.width = `${Number(bar.dataset.pct) || 0}%`;
  });
}

async function deleteReport(reportId, title) {
  if (!window.confirm(`Delete “${title}” and all of its rows? This cannot be undone.`)) return;
  try {
    await deleteAltTextReport(reportId);
    toast(`Deleted ${title}`);
    await loadReports();
  } catch (error) {
    toast(`Could not delete the report: ${error.message}`);
  }
}

// --- wizard -------------------------------------------------------------------

function renderWizard() {
  const body = root.querySelector("#at-new-body");
  if (wizard.step === "pick") {
    // Нативний `<input type="file">` малює власний, локалізований браузером
    // підпис («Файл не вибрано»), а вся копія в UI мусить бути англійською
    // (AGENTS.md §14). Тому інпут лишається справжнім і фокусованим, але
    // невидимим, а видимий стан малюємо самі — фокус ловить `:focus-within`.
    body.innerHTML = `
      <div class="at-upload">
        <label class="at-file">
          <span class="at-file__label">Audit sheet</span>
          <input id="at-file" class="at-file__input" type="file" accept=".xlsx,.xlsm,.csv,.tsv,.txt">
          <span class="at-file__control">
            <span class="button button--primary" aria-hidden="true">${icon("upload")}Choose file…</span>
            <span class="at-file__name" id="at-file-name">No file selected</span>
          </span>
        </label>
        <p class="field-help" id="at-upload-help">The file stays on our server only until the report is created.</p>
        <p class="form-error" id="at-upload-error" role="alert"></p>
      </div>`;
    body.querySelector("#at-file").addEventListener("change", (event) => {
      const [file] = event.target.files;
      body.querySelector("#at-file-name").textContent = file ? file.name : "No file selected";
      if (file) void startUpload(file);
    });
    return;
  }
  renderMapping(body);
}

async function startUpload(file) {
  const error = root.querySelector("#at-upload-error");
  const input = root.querySelector("#at-file");
  error.textContent = "";
  input.disabled = true;
  root.querySelector("#at-upload-help").textContent = "Reading the file…";
  try {
    const upload = await uploadAltTextSource(file);
    const sheet = upload.sheets.find((item) => item.name === upload.default_sheet) || upload.sheets[0];
    wizard = {
      step: "map",
      file,
      upload,
      sheet: sheet.name,
      columnMap: { ...sheet.suggested_column_map },
      busy: false,
    };
    renderWizard();
    root.querySelector("#at-title")?.focus();
  } catch (exception) {
    input.disabled = false;
    root.querySelector("#at-upload-help").textContent =
      "The file stays on our server only until the report is created.";
    error.textContent = exception.message;
    input.focus();
  }
}

function currentSheet() {
  return wizard.upload.sheets.find((sheet) => sheet.name === wizard.sheet) || wizard.upload.sheets[0];
}

function renderMapping(body) {
  const upload = wizard.upload;
  const sheet = currentSheet();
  const headers = sheet.headers || [];
  const defaultTitle = `${upload.source_filename.replace(/\.[^.]+$/, "")}${upload.sheets.length > 1 ? ` — ${sheet.name}` : ""}`;
  const sheetPicker = upload.sheets.length > 1
    ? `<label class="form-field">Sheet
        <select id="at-sheet">${upload.sheets.map((item) => `
          <option value="${escapeHtml(item.name)}"${item.name === wizard.sheet ? " selected" : ""}>${escapeHtml(item.name)}</option>`).join("")}
        </select></label>`
    : "";

  body.innerHTML = `
    <form class="at-map" id="at-map-form" novalidate>
      <p class="at-map__file">${icon("file-spreadsheet")}<span>${escapeHtml(upload.source_filename)}
        <small>${escapeHtml(upload.source_kind.toUpperCase())}${upload.delimiter ? ` · delimiter “${escapeHtml(upload.delimiter)}”` : ""}${upload.encoding ? ` · ${escapeHtml(upload.encoding)}` : ""}</small></span>
        <button class="button button--quiet" type="button" id="at-map-cancel">Choose another file</button></p>
      ${sheet.warnings?.includes("header_row_not_detected") ? `
        <p class="at-note at-note--warn">${icon("alert-triangle")}<span>No familiar column names were found on this
          sheet — map the columns by hand below.</span></p>` : ""}
      <div class="at-map__grid">
        <label class="form-field">Report title
          <input id="at-title" name="title" value="${escapeHtml(defaultTitle)}" maxlength="200" required>
        </label>
        ${sheetPicker}
        <label class="form-field">Client <small>(optional)</small>
          <select id="at-client"><option value="">No client</option></select>
          <small class="at-client-hint" id="at-client-hint"></small>
        </label>
      </div>
      <fieldset class="at-map__columns">
        <legend>Columns</legend>
        ${columnField("image_url", "Image URL", headers, true)}
        ${columnField("new_alt", "New alt text", headers, true)}
        ${columnField("page_url", "Page where it is used", headers, false)}
        ${columnField("old_alt", "Current alt", headers, false)}
      </fieldset>
      <div class="at-map__preview">
        <h3>First rows</h3>
        <div class="table-scroll">
          <table class="tool-table tool-table--preview">
            <caption class="visually-hidden">Preview of the first rows of ${escapeHtml(sheet.name)}</caption>
            <thead><tr>${headers.map((header, index) => `
              <th scope="col">${escapeHtml(header || `Column ${index + 1}`)}</th>`).join("")}</tr></thead>
            <tbody>${(sheet.sample_rows || []).map((row) => `
              <tr>${row.map((cell) => `<td>${escapeHtml(cell)}</td>`).join("")}</tr>`).join("")}</tbody>
          </table>
        </div>
      </div>
      <p class="form-error" id="at-map-error" role="alert"></p>
      <footer class="at-map__actions">
        <button class="button button--primary" type="submit" id="at-map-submit">Create report</button>
      </footer>
    </form>`;

  body.querySelector("#at-map-cancel").addEventListener("click", () => {
    wizard = { step: "pick", file: null, upload: null, sheet: null, columnMap: {}, busy: false };
    renderWizard();
    root.querySelector("#at-file")?.focus();
  });
  body.querySelector("#at-sheet")?.addEventListener("change", (event) => {
    wizard.sheet = event.target.value;
    const picked = currentSheet();
    wizard.columnMap = { ...picked.suggested_column_map };
    renderWizard();
  });
  body.querySelectorAll("select[data-field]").forEach((select) => {
    select.addEventListener("change", () => {
      const value = select.value;
      if (value === "") delete wizard.columnMap[select.dataset.field];
      else wizard.columnMap[select.dataset.field] = Number(value);
    });
  });
  body.querySelector("#at-map-form").addEventListener("submit", (event) => {
    event.preventDefault();
    void submitReport();
  });
  void fillClientOptions(body.querySelector("#at-client"));
}

function columnField(field, label, headers, required) {
  const selected = wizard.columnMap[field];
  const options = headers
    .map((header, index) => `<option value="${index}"${selected === index ? " selected" : ""}>${escapeHtml(header || `Column ${index + 1}`)}</option>`)
    .join("");
  return `
    <label class="form-field">${escapeHtml(label)}${required ? ' <span class="at-required">required</span>' : ""}
      <select data-field="${field}"${required ? " required" : ""}>
        <option value=""${selected === undefined ? " selected" : ""}>Not mapped</option>
        ${options}
      </select>
    </label>`;
}

async function fillClientOptions(select) {
  if (!select) return;
  try {
    if (clientOptions === null) {
      const dashboard = await getDashboard({ period: "week" });
      clientOptions = (dashboard.clients || []).map((client) => ({
        id: client.client_id,
        name: client.name,
        domain: client.domain || "",
      }));
    }
    clientOptions.forEach((client) => {
      const option = document.createElement("option");
      option.value = client.id;
      option.textContent = client.name;
      select.append(option);
    });
    const guess = guessClient(wizard.upload.source_filename, clientOptions);
    if (guess) {
      select.value = guess.id;
      select.closest(".form-field")?.classList.add("form-field--autofilled");
      const hint = document.querySelector("#at-client-hint");
      if (hint) hint.textContent = `Picked from the file name — change it if that is wrong.`;
    }
  } catch {
    // Привʼязка до клієнта опційна: якщо список недоступний, звіт усе одно
    // створюється — просто без перевірки «свій/чужий хост».
    select.insertAdjacentHTML("beforeend", '<option value="" disabled>Client list unavailable</option>');
  }
}

// Файли аудитів за домовленістю агенції починаються з клієнта
// (`SAPHNY_Ahrefs-Audit_21072026.xlsx`), тож клієнта можна не вибирати руками.
// Порівнюємо нормалізовані токени, а не підрядки: інакше короткі назви
// («19 Cleveland» → «19») чіплялися б за будь-яку дату у назві файлу.
export function guessClient(filename, clients) {
  const normalize = (value) => String(value).toLowerCase().replace(/[^a-z0-9]+/g, "");
  const stem = normalize(String(filename).replace(/\.[^.]+$/, ""));
  if (!stem) return null;
  const candidatesFor = (client) =>
    [client.name, String(client.domain || "").replace(/\.[a-z.]+$/, "")]
      .map(normalize)
      .filter((value) => value.length >= 3);

  let best = null;
  let bestLength = 0;
  clients.forEach((client) => {
    candidatesFor(client).forEach((candidate) => {
      // Саме префікс: клієнт стоїть НА ПОЧАТКУ імені файлу за домовленістю.
      // Найдовший збіг виграє, щоб «Sapphire NY» перемогло «Sapphire».
      if (stem.startsWith(candidate) && candidate.length > bestLength) {
        best = client;
        bestLength = candidate.length;
      }
    });
  });
  if (best) return best;

  // Запасний прохід для файлів, де клієнт не перший (`Ahrefs-Audit_SAPHNY`):
  // збіг лише по ЦІЛОМУ токену імені файлу і лише якщо він однозначний —
  // підрядок тут дав би хибні спрацювання на кшталт «SDS» усередині слова.
  const tokens = new Set(String(filename).replace(/\.[^.]+$/, "").split(/[^a-zA-Z0-9]+/).map(normalize));
  const matches = clients.filter((client) =>
    candidatesFor(client).some((candidate) => tokens.has(candidate)));
  return matches.length === 1 ? matches[0] : null;
}

async function submitReport({ force = false } = {}) {
  const errorField = root.querySelector("#at-map-error");
  const submit = root.querySelector("#at-map-submit");
  const title = root.querySelector("#at-title").value.trim();
  errorField.textContent = "";
  errorField.innerHTML = "";

  const missing = ["image_url", "new_alt"].filter((field) => wizard.columnMap[field] === undefined);
  if (!title) {
    errorField.textContent = "Give the report a title.";
    root.querySelector("#at-title").focus();
    return;
  }
  if (missing.length) {
    errorField.textContent = "Map a column for the image URL and for the new alt text.";
    root.querySelector(`select[data-field="${missing[0]}"]`).focus();
    return;
  }

  submit.disabled = true;
  submit.innerHTML = `${icon("loader-2", "icon icon--spin")}Create report`;
  try {
    const report = await createAltTextReport({
      upload_id: wizard.upload.upload_id,
      title,
      sheet: wizard.upload.source_kind === "xlsx" ? wizard.sheet : null,
      client_id: root.querySelector("#at-client").value || null,
      column_map: wizard.columnMap,
    }, { force });
    location.hash = buildToolsHash(TOOL_KEY, report.id);
  } catch (error) {
    submit.disabled = false;
    submit.textContent = "Create report";
    if (error.code === "report_exists" && error.details?.existing_report_id) {
      errorField.innerHTML = `${escapeHtml(error.message)}
        <a class="button button--quiet" href="${buildToolsHash(TOOL_KEY, error.details.existing_report_id)}">Open existing report</a>
        <button class="button button--quiet" type="button" id="at-force">Import again anyway</button>`;
      errorField.querySelector("#at-force").addEventListener("click", () => void submitReport({ force: true }));
      return;
    }
    errorField.textContent = error.message;
  }
}

// --- report view --------------------------------------------------------------

async function renderReportView() {
  root.innerHTML = `
    <a class="back-link" href="${buildToolsHash(TOOL_KEY)}">${icon("arrow-left")}<span>All reports</span></a>
    <header class="tool-header" id="at-report-header" aria-live="polite">
      ${panelSkeleton({ label: "the report", rows: 2, columns: 2 })}
    </header>
    <section class="tool-panel" id="at-rows" aria-labelledby="at-rows-title">
      <div class="section-heading"><div><h2 id="at-rows-title">Images</h2></div></div>
      <div class="tool-panel__body" id="at-rows-body">
        ${panelSkeleton({ label: "images", rows: 8, columns: 3 })}
      </div>
    </section>`;
  bindDelegatedHandlers(root.querySelector("#at-rows-body"));
  await loadReport();
}

async function loadReport() {
  const token = ++requestToken;
  try {
    const report = await getAltTextReport(state.reportId);
    if (token !== requestToken) return;
    renderReportHeader(report);
    await loadItems();
  } catch (error) {
    if (token !== requestToken) return;
    if (error.status === 404) {
      root.querySelector("#at-report-header").innerHTML = `
        <div class="state-panel state-failed">${icon("file-off")}
          <p><strong>Report not found.</strong><br>It may have been deleted.</p>
          <a class="button button--primary" href="${buildToolsHash(TOOL_KEY)}">Back to reports</a></div>`;
      root.querySelector("#at-rows")?.remove();
      return;
    }
    root.querySelector("#at-report-header").innerHTML = `
      <p class="state-panel state-failed">${icon("wifi-off")}<span><strong>Could not load the report.</strong><br>${escapeHtml(error.message)}</span></p>`;
  }
}

function renderReportHeader(report) {
  const counts = report.status_counts || {};
  const done = (counts.copied || 0) + (counts.applied || 0);
  const source = report.source_sheet
    ? `${report.source_filename} · ${report.source_sheet}`
    : report.source_filename;
  const verified = report.verify_counts?.match || 0;
  root.querySelector("#at-report-header").innerHTML = `
    <p class="eyebrow">Tools / Image Alt Text</p>
    <h1>${escapeHtml(report.title)}</h1>
    <p class="tool-meta">
      <span>${icon("file-spreadsheet", "icon icon--tiny")}${escapeHtml(source)}</span>
      ${report.client_name ? `<span>${icon("building-store", "icon icon--tiny")}${escapeHtml(report.client_name)}</span>` : ""}
      <span>${icon("clock", "icon icon--tiny")}Imported ${escapeHtml(formatTimestamp(report.created_at, "—"))}</span>
    </p>
    <div class="at-progress">
      <p class="at-progress__counts"><strong>${done}</strong> of <strong>${report.row_count}</strong> images handled
        ${counts.skipped ? `<span class="muted">· ${counts.skipped} skipped</span>` : ""}
        ${verified ? `<span class="muted">· ${verified} confirmed on the page</span>` : ""}</p>
      <progress max="${report.row_count}" value="${done}" aria-label="Images handled: ${done} of ${report.row_count}"></progress>
      ${report.skipped_count ? `<p class="at-progress__note">${icon("info-circle", "icon icon--tiny")}${report.skipped_count} row(s) in the file had no image URL and were not imported.</p>` : ""}
      ${report.verifiable_count === 0 ? `<p class="at-progress__note">${icon("info-circle", "icon icon--tiny")}No page column was mapped, so alt checks are unavailable for this report.</p>` : ""}
    </div>
    <div class="at-export">
      <button class="button button--quiet" type="button" id="at-resolve-media-batch">
        ${icon("search")}Resolve media IDs</button>
      <a class="button button--quiet" href="${altTextExportCsvUrl(report.id)}">${icon("download")}Export CSV</a>
      <a class="button button--quiet" href="${altTextExportXlsxUrl(report.id)}">${icon("download")}Export XLSX</a>
    </div>`;
  // Кожен виклик `renderReportHeader` замінює `innerHTML` контейнера, тож
  // нова кнопка — новий елемент; прив'язка тут не дублюється між рендерами.
  root.querySelector("#at-resolve-media-batch")
    ?.addEventListener("click", () => void resolveReportMediaBatch());
}

async function resolveReportMediaBatch() {
  const button = root.querySelector("#at-resolve-media-batch");
  if (!button) return;
  const original = button.innerHTML;
  button.disabled = true;
  button.innerHTML = `${icon("loader-2", "icon icon--spin")}Resolving…`;
  try {
    const summary = await resolveAltTextReportMedia(state.reportId, { limit: 50 });
    const parts = [
      summary.resolved ? `${summary.resolved} found` : null,
      summary.ambiguous ? `${summary.ambiguous} ambiguous` : null,
      summary.not_found ? `${summary.not_found} not found` : null,
      summary.rest_unavailable ? `${summary.rest_unavailable} REST closed` : null,
      summary.failed ? `${summary.failed} failed` : null,
    ].filter(Boolean);
    const tail = summary.remaining
      ? ` — ${summary.remaining} row(s) left, click again to continue`
      : " — every uploads row is checked";
    toast(`${parts.length ? parts.join(", ") : "Nothing to resolve"}${tail}`);
    await loadItems();
  } catch (error) {
    toast(`Could not resolve media IDs: ${error.message}`);
  } finally {
    button.disabled = false;
    button.innerHTML = original;
  }
}

async function loadItems() {
  const token = ++requestToken;
  const body = root.querySelector("#at-rows-body");
  try {
    const page = await getAltTextItems(state.reportId, {
      q: state.q,
      status: state.status,
      urlKind: state.urlKind,
      verify: state.verify,
      mediaStatus: state.mediaStatus,
      limit: PAGE_SIZE,
      offset: state.offset,
    });
    if (token !== requestToken) return;
    renderItems(body, page);
  } catch (error) {
    if (token !== requestToken) return;
    body.innerHTML = `
      <div class="state-panel state-failed">${icon("wifi-off")}
        <p><strong>Could not load the rows.</strong><br>${escapeHtml(error.message)}</p>
        <button class="button button--primary" type="button" id="at-rows-retry">Try again</button></div>`;
    body.querySelector("#at-rows-retry").addEventListener("click", () => void loadItems());
  }
}

function renderItems(body, page) {
  currentRows = page.rows;
  const shown = page.rows.length;
  const from = shown ? state.offset + 1 : 0;
  body.innerHTML = `
    ${toolbar(page)}
    ${shown ? `
      <div class="table-scroll">
        <table class="tool-table at-table">
          <caption class="visually-hidden">Images, search keys and progress</caption>
          <thead><tr>
            <th scope="col">#</th>
            <th scope="col"><span class="visually-hidden">Preview</span></th>
            <th scope="col">Image URL</th>
            <th scope="col">Media library search</th>
            <th scope="col">Media ID</th>
            <th scope="col">New alt text</th>
            <th scope="col">Page</th>
            <th scope="col">Status</th>
          </tr></thead>
          <tbody>${page.rows.map(itemRow).join("")}</tbody>
        </table>
      </div>
      <p class="at-cache-note">${icon("info-circle", "icon icon--tiny")}Alt checks request the page with a random
        cache-busting parameter and no-cache headers. An edge cache can still return older HTML — re-check a minute
        after saving in WordPress.</p>
      <div class="pagination">
        <button class="button button--quiet" type="button" data-page="prev"${state.offset === 0 ? " disabled" : ""}>Previous</button>
        <span>${from}–${state.offset + shown} of ${page.total}</span>
        <button class="button button--quiet" type="button" data-page="next"${state.offset + shown >= page.total ? " disabled" : ""}>Next</button>
      </div>`
    : `<p class="tool-empty">${icon("search-off")}<span>No row matches these filters.
        <button class="link-button" type="button" data-action="clear-filters">Clear filters</button></span></p>`}`;
  bindItemHandlers(body);
}

function toolbar(page) {
  const option = (value, label, selected) =>
    `<option value="${value}"${selected === value ? " selected" : ""}>${label}</option>`;
  return `
    <div class="analytics-toolbar at-toolbar">
      <label class="search-field">
        <span class="visually-hidden">Search images</span>
        ${icon("search")}
        <input id="at-search" type="search" placeholder="URL, search key or alt" value="${escapeHtml(state.q)}" autocomplete="off">
      </label>
      <label class="sort-field">Status
        <select id="at-filter-status">
          ${option("", "All", state.status)}
          ${Object.entries(STATUS_META).map(([key, meta]) => option(key, meta.label, state.status)).join("")}
        </select>
      </label>
      <label class="sort-field">Image source
        <select id="at-filter-kind">
          ${option("", "All", state.urlKind)}
          ${Object.entries(URL_KIND_META).map(([key, meta]) => option(key, meta.label, state.urlKind)).join("")}
        </select>
      </label>
      <label class="sort-field">Alt check
        <select id="at-filter-verify">
          ${option("", "All", state.verify)}
          ${Object.entries(VERIFY_META).map(([key, meta]) => option(key, meta.label, state.verify)).join("")}
        </select>
      </label>
      <label class="sort-field">Media ID
        <select id="at-filter-media">
          ${option("", "All", state.mediaStatus)}
          ${option("unresolved", "Not yet checked", state.mediaStatus)}
          ${Object.entries(MEDIA_STATUS_META).map(([key, meta]) => option(key, meta.label, state.mediaStatus)).join("")}
        </select>
      </label>
      <p class="result-count" aria-live="polite">${page.total} row${page.total === 1 ? "" : "s"}</p>
    </div>`;
}

function itemRow(item) {
  const kind = URL_KIND_META[item.url_kind] || URL_KIND_META.unknown;
  const stripped = (item.stripped || []).length
    ? `<span class="at-stripped" title="Removed from the file name so the media library can match it">${
        item.stripped.map((part) => `<code>${escapeHtml(part)}</code>`).join("")}</span>`
    : "";
  const warnings = (item.derivation_warnings || [])
    .map((code) => WARNING_COPY[code])
    .filter(Boolean);
  const fallback = item.search_key_fallback
    ? `<button class="at-copy at-copy--ghost" type="button" data-action="copy" data-copy="${escapeHtml(item.search_key_fallback)}"
        aria-label="${escapeHtml(`Copy shorter search key ${item.search_key_fallback} for row ${item.row_index}`)}"
        title="Shorter key: ${escapeHtml(item.search_key_fallback)}">${icon("copy", "icon icon--tiny")}<code>${escapeHtml(item.search_key_fallback)}</code></button>`
    : "";
  return `
    <tr data-item-id="${escapeHtml(item.id)}">
      <td class="num at-row-index">${item.row_index}</td>
      <td class="at-preview">
        <button class="at-preview__box" type="button" data-action="preview"
          aria-label="${escapeHtml(`Enlarge the image from row ${item.row_index}`)}">
          <img src="${escapeHtml(item.image_url)}" alt="" loading="lazy" decoding="async" width="52" height="52">
          <span class="at-preview__fallback">No preview</span>
          <span class="at-preview__zoom" aria-hidden="true">${icon("zoom-in", "icon icon--tiny")}</span>
        </button>
      </td>
      <td class="at-url">
        <span class="at-url__text" title="${escapeHtml(item.image_url)}">${escapeHtml(item.image_url)}</span>
        <span class="at-actions">
          <a class="icon-button" href="${escapeHtml(item.image_url)}" target="_blank" rel="noreferrer"
            aria-label="${escapeHtml(`Open image from row ${item.row_index} in a new tab`)}">${icon("external-link", "icon icon--tiny")}</a>
          <button class="icon-button" type="button" data-action="copy" data-copy="${escapeHtml(item.image_url)}"
            aria-label="${escapeHtml(`Copy image URL from row ${item.row_index}`)}">${icon("copy", "icon icon--tiny")}</button>
        </span>
      </td>
      <td class="at-key">
        ${item.search_key
          ? `<button class="at-copy" type="button" data-action="copy" data-copy="${escapeHtml(item.search_key)}"
              aria-label="${escapeHtml(`Copy media library search key ${item.search_key} for row ${item.row_index}`)}">
              ${icon("copy", "icon icon--tiny")}<code>${escapeHtml(item.search_key)}</code></button>`
          : '<span class="muted">No usable file name</span>'}
        ${fallback}
        <span class="at-kind at-kind--${kind.tone}">${escapeHtml(kind.label)}</span>
        ${stripped}
        ${warnings.length ? `<span class="at-warn">${icon("alert-triangle", "icon icon--tiny")}${escapeHtml(warnings.join(" "))}</span>` : ""}
      </td>
      <td class="at-media">${mediaCell(item)}</td>
      <td class="at-alt">
        ${item.new_alt
          ? `<span class="at-alt__text">${escapeHtml(item.new_alt)}</span>
             <button class="button button--quiet at-alt__copy" type="button" data-action="copy-alt" data-copy="${escapeHtml(item.new_alt)}"
               aria-label="${escapeHtml(`Copy new alt text for row ${item.row_index}`)}">${icon("clipboard", "icon icon--tiny")}Copy alt</button>`
          : `<span class="muted">The sheet has no new alt for this row</span>`}
        ${item.old_alt ? `<span class="at-alt__old">Current: ${escapeHtml(item.old_alt)}</span>` : ""}
      </td>
      <td class="at-page">${pageCell(item)}</td>
      <td class="at-status">
        <label class="visually-hidden" for="at-status-${escapeHtml(item.id)}">Status of row ${item.row_index}</label>
        <select class="at-status__select" id="at-status-${escapeHtml(item.id)}" data-action="status" data-status="${escapeHtml(item.status)}">
          ${Object.entries(STATUS_META).map(([key, meta]) =>
            `<option value="${key}"${item.status === key ? " selected" : ""}>${meta.label}</option>`).join("")}
        </select>
        ${item.status_changed_at ? `<small>${escapeHtml(formatTimestamp(item.status_changed_at, ""))}</small>` : ""}
      </td>
    </tr>`;
}

function pageCell(item) {
  const pages = item.page_urls?.length ? item.page_urls : (item.page_url ? [item.page_url] : []);
  if (!pages.length) return '<span class="muted">No page in the sheet</span>';
  const verdict = item.verify_status ? VERIFY_META[item.verify_status] : null;
  const found = item.verify_status === "mismatch" && item.verify_alt
    ? `<small class="at-verdict__found">On page: “${escapeHtml(item.verify_alt)}”</small>`
    : "";
  const detail = item.verify_detail ? `<small class="at-verdict__found">${escapeHtml(item.verify_detail)}</small>` : "";
  // Одна картинка часто стоїть на кількох сторінках (аудити пакують їх в одну
  // клітинку). Показуємо перші три посиланнями, решту — числом, щоб рядок не
  // перетворився на простирадло, але жодна сторінка не зникла з даних.
  const shown = pages.slice(0, 3);
  const rest = pages.length - shown.length;
  const links = shown
    .map((url, index) => `
      <a class="at-page__link" href="${escapeHtml(url)}" target="_blank" rel="noreferrer"
        title="${escapeHtml(url)}"
        aria-label="${escapeHtml(`Open page ${index + 1} using the image from row ${item.row_index}`)}"
        >${icon("external-link", "icon icon--tiny")}<span>${escapeHtml(pageLabel(url))}</span></a>`)
    .join("");
  return `
    <span class="at-page__links">${links}${rest > 0
      ? `<span class="at-page__rest" title="${escapeHtml(pages.slice(3).join("\n"))}">+${rest} more page${rest === 1 ? "" : "s"}</span>`
      : ""}</span>
    <span class="at-actions">
      <button class="button button--quiet" type="button" data-action="verify"
        ${item.new_alt ? "" : "disabled aria-disabled=\"true\" title=\"No new alt to compare against\""}
        aria-label="${escapeHtml(`Check whether the alt is live on the page for row ${item.row_index}`)}">${icon("refresh", "icon icon--tiny")}Verify alt</button>
    </span>
    ${verdict
      ? `<span class="at-verdict at-verdict--${verdict.tone}">${icon(verdict.icon, "icon icon--tiny")}${escapeHtml(verdict.label)}
          <small>${escapeHtml(formatTimestamp(item.verify_checked_at, ""))}</small></span>${found}${detail}`
      : '<span class="at-verdict at-verdict--none">Not checked yet</span>'}`;
}

// WordPress media ID (task-39). Кожен стан — окрема гілка розмітки, не одна
// узагальнена: "resolved" показує справжнє посилання в адмінку (яке ще й
// копіює alt у кліку), решта — чіп причини й фолбек-дію, ніколи порожню
// клітинку без пояснення (PLAN §12).
function mediaCell(item) {
  const status = item.wp_media_status;

  if (!status) {
    if (!item.searchable) {
      return `<span class="at-media-status at-media-status--bad muted">${icon("circle-off", "icon icon--tiny")}Not in media library</span>`;
    }
    return `
      <span class="at-media__pending muted">Not checked</span>
      <button class="button button--quiet at-media__action" type="button" data-action="resolve-media"
        aria-label="${escapeHtml(`Find the WordPress media ID for row ${item.row_index}`)}">
        ${icon("search", "icon icon--tiny")}Find</button>`;
  }

  if (status === "not_searchable") {
    return `<span class="at-media-status at-media-status--bad muted" title="${escapeHtml(item.wp_media_detail || "")}">
      ${icon("circle-off", "icon icon--tiny")}Not in media library</span>`;
  }

  if (status === "resolved") {
    const confidence = MEDIA_CONFIDENCE_LABEL[item.wp_media_confidence] || "";
    return `
      <span class="at-media__id">
        <button class="at-copy" type="button" data-action="copy" data-copy="${escapeHtml(String(item.wp_media_id))}"
          aria-label="${escapeHtml(`Copy media ID ${item.wp_media_id} for row ${item.row_index}`)}">
          ${icon("copy", "icon icon--tiny")}<code>#${escapeHtml(String(item.wp_media_id))}</code></button>
        ${confidence ? `<small class="at-media__confidence">${escapeHtml(confidence)}</small>` : ""}
      </span>
      ${item.wp_media_admin_url
        ? `<a class="button button--quiet at-media__action" data-action="open-media"
            data-copy="${escapeHtml(item.new_alt || "")}" data-media-id="${escapeHtml(String(item.wp_media_id))}"
            href="${escapeHtml(item.wp_media_admin_url)}" target="_blank" rel="noreferrer"
            aria-label="${escapeHtml(`Copy the new alt and open media ${item.wp_media_id} in WordPress for row ${item.row_index}`)}">
            ${icon("external-link", "icon icon--tiny")}Open in WP</a>`
        : ""}`;
  }

  if (status === "ambiguous") {
    const label = mediaMatchLabel((item.wp_media_candidates || []).length);
    return `
      <span class="at-media-status at-media-status--warn">${icon("help-circle", "icon icon--tiny")}${escapeHtml(label)}</span>
      <button class="button button--quiet at-media__action" type="button" data-action="choose-media"
        aria-label="${escapeHtml(`Choose the right media ID for row ${item.row_index}`)}">
        ${icon("list-search", "icon icon--tiny")}Choose…</button>`;
  }

  const meta = MEDIA_STATUS_META[status] || MEDIA_STATUS_META.failed;
  return `
    <span class="at-media-status at-media-status--${meta.tone}" title="${escapeHtml(item.wp_media_detail || "")}">
      ${icon(meta.icon, "icon icon--tiny")}${escapeHtml(meta.label)}</span>
    <span class="at-actions">
      ${item.wp_media_admin_url
        ? `<a class="button button--quiet" href="${escapeHtml(item.wp_media_admin_url)}" target="_blank" rel="noreferrer"
            aria-label="${escapeHtml(`Search the media library for row ${item.row_index}`)}">
            ${icon("search", "icon icon--tiny")}Search in WP</a>`
        : ""}
      <button class="icon-button" type="button" data-action="resolve-media"
        aria-label="${escapeHtml(`Try finding the media ID again for row ${item.row_index}`)}">${icon("refresh", "icon icon--tiny")}</button>
    </span>`;
}

// --- попап із великим прев'ю --------------------------------------------------

// Нативний `<dialog>`: focus trap, Esc і повернення фокуса на кнопку-опener
// дає браузер (AGENTS.md §7), тому власного менеджера фокуса тут не треба.
function previewDialog() {
  let dialog = document.querySelector("#at-preview-dialog");
  if (dialog) return dialog;
  dialog = document.createElement("dialog");
  dialog.id = "at-preview-dialog";
  dialog.className = "at-preview-dialog";
  dialog.setAttribute("aria-labelledby", "at-preview-title");
  dialog.innerHTML = `
    <div class="dialog-shell">
      <header class="dialog-header">
        <div>
          <p class="eyebrow">Image preview</p>
          <h2 id="at-preview-title"></h2>
          <p class="at-preview-dialog__alt"></p>
        </div>
        <button class="icon-button" type="button" data-close aria-label="Close preview">
          ${icon("x")}
        </button>
      </header>
      <div class="at-preview-dialog__stage">
        <img alt="" decoding="async">
        <p class="at-preview-dialog__fallback">The image could not be loaded from the site.</p>
      </div>
      <footer class="dialog-footer at-preview-dialog__actions"></footer>
    </div>`;
  document.body.append(dialog);
  dialog.querySelector("[data-close]").addEventListener("click", () => dialog.close());
  // Клік по підкладці (поза вмістом) теж закриває — звичне очікування.
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) dialog.close();
  });
  dialog.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-action]");
    if (!button) return;
    if (button.dataset.action === "copy") void copyValue(button);
    // Копія alt із попапа — та сама дія, що й у таблиці, тож і прогрес вона
    // рухає так само (інакше два місця робили б різне з однією кнопкою).
    else if (button.dataset.action === "copy-alt-modal") {
      const row = document.querySelector(`tr[data-item-id="${button.dataset.itemId}"]`);
      if (row) void copyAlt(button, row);
      else void copyValue(button);
    }
  });
  return dialog;
}

function openPreview(button, row) {
  const item = currentRows.find((candidate) => candidate.id === row?.dataset.itemId);
  if (!item) return;
  const dialog = previewDialog();
  const stage = dialog.querySelector(".at-preview-dialog__stage");
  const image = stage.querySelector("img");
  const filename = item.image_url.split("?")[0].split("/").pop() || item.image_url;

  dialog.querySelector("#at-preview-title").textContent = decodeURIComponent(filename);
  // Показуємо саме НОВИЙ alt: попап існує, щоб звірити картинку з підписом,
  // який оператор зараз проставляє.
  dialog.querySelector(".at-preview-dialog__alt").textContent =
    item.new_alt ? `New alt: ${item.new_alt}` : "This row has no new alt text yet.";
  stage.classList.remove("is-broken");
  image.onerror = () => stage.classList.add("is-broken");
  image.src = item.image_url;
  // Alt порожній навмисно: сама картинка тут — предмет розгляду, а її опис
  // уже надрукований поруч текстом.
  image.alt = "";
  dialog.querySelector(".at-preview-dialog__actions").innerHTML = `
    <a class="button button--quiet" href="${escapeHtml(item.image_url)}" target="_blank" rel="noreferrer">
      ${icon("external-link")}Open original</a>
    <button class="button button--quiet" type="button" data-action="copy" data-copy="${escapeHtml(item.image_url)}">
      ${icon("copy")}Copy URL</button>
    ${item.new_alt
      ? `<button class="button button--primary" type="button" data-action="copy-alt-modal"
          data-item-id="${escapeHtml(item.id)}" data-copy="${escapeHtml(item.new_alt)}">
          ${icon("clipboard")}Copy alt</button>`
      : ""}`;
  dialog.showModal();
}

// --- модалка кандидатів при ambiguous media ID --------------------------------

// `wp_media_admin_url` при `ambiguous` — це посилання на пошук в адмінці
// (`upload.php?mode=list&s=…`), не на конкретне вкладення; корінь інсталяції
// з нього все одно можна витягти — і зібрати точні `item=<id>` посилання на
// кожного кандидата, не питаючи бекенд ще раз.
function adminRootFrom(url) {
  if (!url) return null;
  const marker = "/wp-admin/";
  const index = url.indexOf(marker);
  return index === -1 ? null : url.slice(0, index);
}

function mediaCandidatesDialog() {
  let dialog = document.querySelector("#at-media-dialog");
  if (dialog) return dialog;
  dialog = document.createElement("dialog");
  dialog.id = "at-media-dialog";
  dialog.className = "at-media-dialog";
  dialog.setAttribute("aria-labelledby", "at-media-dialog-title");
  dialog.innerHTML = `
    <div class="dialog-shell">
      <header class="dialog-header">
        <div>
          <p class="eyebrow">Media library</p>
          <h2 id="at-media-dialog-title"></h2>
          <p class="at-media-dialog__hint">The search is not exact. Each link opens that attachment in
            WordPress so you can confirm it visually before using its ID.</p>
        </div>
        <button class="icon-button" type="button" data-close aria-label="Close">${icon("x")}</button>
      </header>
      <div class="at-media-dialog__list" id="at-media-dialog-list"></div>
    </div>`;
  document.body.append(dialog);
  dialog.querySelector("[data-close]").addEventListener("click", () => dialog.close());
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog || event.target.closest(".at-media-dialog__item")) dialog.close();
  });
  return dialog;
}

function openMediaCandidates(row) {
  const item = currentRows.find((candidate) => candidate.id === row?.dataset.itemId);
  if (!item) return;
  const dialog = mediaCandidatesDialog();
  const list = dialog.querySelector("#at-media-dialog-list");
  const root = adminRootFrom(item.wp_media_admin_url);
  const candidates = item.wp_media_candidates || [];
  dialog.querySelector("#at-media-dialog-title").textContent =
    `${mediaMatchLabel(candidates.length)} — pick the right one`;
  list.innerHTML = candidates.length
    ? candidates.map((candidate) => {
        const label = candidate.file
          || (candidate.source_url ? candidate.source_url.split("/").pop() : `Attachment ${candidate.id}`);
        // No `&mode=grid`: WordPress's own grid router matches `item=:slug`
        // with a regex that stops at `/` or `?` but not `&`, so anything
        // appended after `item=<id>` gets swallowed into the id and silently
        // breaks the deep link whenever the item isn't already on the
        // pre-loaded grid page (found live — see `build_admin_url` docstring).
        const href = root
          ? `${root}/wp-admin/upload.php?item=${candidate.id}`
          : (candidate.source_url || "#");
        return `
          <a class="at-media-dialog__item" href="${escapeHtml(href)}" target="_blank" rel="noreferrer"
            aria-label="${escapeHtml(`Open candidate ${label} in WordPress`)}">
            <code>#${candidate.id}</code>
            <span class="at-media-dialog__file">${escapeHtml(label)}</span>
            ${candidate.alt_text
              ? `<small>Current alt: “${escapeHtml(candidate.alt_text)}”</small>`
              : '<small class="muted">No alt set</small>'}
            ${icon("external-link", "icon icon--tiny")}
          </a>`;
      }).join("")
    : `<p class="tool-empty">${icon("search-off")}<span>No candidates left to show.</span></p>`;
  dialog.showModal();
}

// Коротка, але однозначна підпис-мітка сторінки: шлях без домену (домен той
// самий у межах звіту), а корінь — як «/».
function pageLabel(url) {
  try {
    const parsed = new URL(url, location.origin);
    return `${parsed.pathname}${parsed.search}` || "/";
  } catch {
    return url;
  }
}

function bindItemHandlers(body) {
  const search = body.querySelector("#at-search");
  if (search) {
    let timer = null;
    search.addEventListener("input", () => {
      clearTimeout(timer);
      timer = setTimeout(() => applyFilters({ q: search.value }), 300);
    });
  }
  body.querySelector("#at-filter-status")?.addEventListener("change", (event) =>
    applyFilters({ status: event.target.value }));
  body.querySelector("#at-filter-kind")?.addEventListener("change", (event) =>
    applyFilters({ urlKind: event.target.value }));
  body.querySelector("#at-filter-verify")?.addEventListener("change", (event) =>
    applyFilters({ verify: event.target.value }));
  body.querySelector("#at-filter-media")?.addEventListener("change", (event) =>
    applyFilters({ mediaStatus: event.target.value }));
  body.querySelector("[data-action='clear-filters']")?.addEventListener("click", () =>
    applyFilters({ q: "", status: "", urlKind: "", verify: "", mediaStatus: "" }));
  body.querySelectorAll("button[data-page]").forEach((button) => {
    button.addEventListener("click", () => {
      state.offset = button.dataset.page === "next"
        ? state.offset + PAGE_SIZE
        : Math.max(0, state.offset - PAGE_SIZE);
      persist();
      void loadItems();
    });
  });
}

// Делеговані обробники вішаються РІВНО ОДИН РАЗ на контейнер, який переживає
// перерендери (ми міняємо лише його `innerHTML`). Раніше вони навішувалися в
// `bindItemHandlers` на кожен рендер, і після N перемальовувань один клік
// давав N тостів і N однакових запитів.
function bindDelegatedHandlers(body) {
  if (!body || body.dataset.delegated === "true") return;
  body.dataset.delegated = "true";

  body.addEventListener("click", (event) => {
    // `[data-action]`, не `button[data-action]`: "Open in WP"/"Search in WP"
    // are real `<a target="_blank">`, not buttons — a real link lets the
    // browser open the new tab in the same click gesture instead of after an
    // `await`, which popup blockers would otherwise kill.
    const target = event.target.closest("[data-action]");
    if (!target) return;
    const row = target.closest("tr[data-item-id]");
    if (target.dataset.action === "copy") void copyValue(target);
    else if (target.dataset.action === "copy-alt") void copyAlt(target, row);
    else if (target.dataset.action === "verify") void verifyRow(target, row);
    else if (target.dataset.action === "preview") openPreview(target, row);
    else if (target.dataset.action === "resolve-media") void resolveMediaRow(target, row);
    else if (target.dataset.action === "open-media") void openMediaLink(target);
    else if (target.dataset.action === "choose-media") openMediaCandidates(row);
  });
  body.addEventListener("change", (event) => {
    const select = event.target.closest("select[data-action='status']");
    if (!select) return;
    void changeStatus(select, select.closest("tr[data-item-id]"));
  });
  // `error` не булькає, тому слухаємо у фазі захоплення: битий прев'ю-URL має
  // дати текстовий стан, а не «зламану картинку».
  body.addEventListener("error", (event) => {
    const image = event.target;
    if (image.tagName === "IMG") image.closest(".at-preview__box")?.classList.add("is-broken");
  }, true);
}

function applyFilters(patch) {
  Object.assign(state, patch);
  state.offset = 0;
  persist();
  void loadItems();
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

// Копіювання нового alt — і є відмітка «цю картинку взяли в роботу»
// (копіювання пошукового фрагмента статус не рухає: це проміжний крок).
async function copyAlt(button, row) {
  if (!(await copyText(button.dataset.copy))) return;
  flashCopied(button, "Copy alt");
  toast("Copied the new alt text");
  const select = row.querySelector("select[data-action='status']");
  if (select.value === "pending") {
    select.value = "copied";
    await changeStatus(select, row, { silent: true });
  }
}

function flashCopied(button, label = null) {
  const original = button.innerHTML;
  button.innerHTML = label
    ? `${icon("clipboard-check", "icon icon--tiny")}${label}`
    : icon("copy-check", "icon icon--tiny");
  setTimeout(() => { button.innerHTML = original; }, 1400);
}

async function changeStatus(select, row, { silent = false } = {}) {
  const previous = select.dataset.status;
  const next = select.value;
  if (previous === next) return;
  select.disabled = true;
  try {
    const item = await setAltTextItemStatus(state.reportId, row.dataset.itemId, next);
    select.dataset.status = item.status;
    select.value = item.status;
    if (!silent) toast(`Row ${item.row_index} marked “${STATUS_META[item.status].label}”`);
    await refreshProgress();
  } catch (error) {
    select.value = previous;
    toast(`Could not save the status: ${error.message}`);
  } finally {
    select.disabled = false;
  }
}

async function verifyRow(button, row) {
  const original = button.innerHTML;
  button.disabled = true;
  button.innerHTML = `${icon("loader-2", "icon icon--spin")}Checking…`;
  try {
    const item = await verifyAltTextItem(state.reportId, row.dataset.itemId);
    row.querySelector(".at-page").innerHTML = pageCell(item);
    const select = row.querySelector("select[data-action='status']");
    select.value = item.status;
    select.dataset.status = item.status;
    const meta = VERIFY_META[item.verify_status];
    toast(meta ? `Row ${item.row_index}: ${meta.label}` : `Row ${item.row_index} checked`);
    await refreshProgress();
  } catch (error) {
    button.disabled = false;
    button.innerHTML = original;
    toast(`Could not check the page: ${error.message}`);
  }
}

async function resolveMediaRow(button, row) {
  const original = button.innerHTML;
  button.disabled = true;
  button.innerHTML = `${icon("loader-2", "icon icon--spin")}Finding…`;
  try {
    const item = await resolveAltTextItemMedia(state.reportId, row.dataset.itemId);
    row.querySelector(".at-media").innerHTML = mediaCell(item);
    const index = currentRows.findIndex((candidate) => candidate.id === item.id);
    if (index !== -1) currentRows[index] = item;
    const label = item.wp_media_status === "ambiguous"
      ? mediaMatchLabel((item.wp_media_candidates || []).length)
      : MEDIA_STATUS_META[item.wp_media_status]?.label;
    toast(label ? `Row ${item.row_index}: ${label}` : `Row ${item.row_index} checked`);
  } catch (error) {
    button.disabled = false;
    button.innerHTML = original;
    toast(`Could not find the media ID: ${error.message}`);
  }
}

// Справжнє посилання (не `button`), тож клік відкриває WordPress у тому ж
// жесті користувача — копіювання йде паралельно, а не перед навігацією, щоб
// `await navigator.clipboard.writeText` не встиг дати попап-блокеру привід
// вважати відкриття вкладки не викликаним дією людини.
async function openMediaLink(link) {
  const alt = link.dataset.copy;
  if (!alt) return;
  if (await copyText(alt)) toast(`Alt copied — media #${link.dataset.mediaId} opened in WordPress`);
}

// Лічильники в шапці — єдине джерело правди про прогрес, тож після кожної
// зміни перечитуємо їх із сервера, а не рахуємо в DOM.
async function refreshProgress() {
  try {
    renderReportHeader(await getAltTextReport(state.reportId));
  } catch {
    // Прогрес — вторинний показник; сама дія вже збережена.
  }
}
