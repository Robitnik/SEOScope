// Переглядач збереженого вмісту (robots.txt / файли sitemap).
//
// Окремий діалог і окремий модуль від `monitor-diff.js`: той володіє ПАРОЮ
// версій і їхнім дифом, цей — ОДНИМ файлом і фільтром по ньому. Спільний стан
// зробив би обидва важчими для читання, а не легшими.
//
// Що показуємо — саме те, що монітор реально завантажив і поклав у сховище
// (content-addressed blob), а не те, що сайт віддає прямо зараз: у цьому вся
// суть моніторингу. Посилання «Open live» поруч дає другий бік — живий файл.

import { getMonitorDocumentText, getMonitorResources, isAbortError, monitorDocumentUrl } from "./api.js?v=20260819-spin3";
import { escapeHtml, icon } from "./formatters.js?v=20260819-spin3";

const MAX_RENDERED_LINES = 5000;

function loadingMarkup(label) {
  return `<p class="monitor-content-state" aria-live="polite">Loading ${escapeHtml(label)}…</p>`;
}

function emptyMarkup(text) {
  return `<div class="state-panel state-not_configured"><p><strong>${escapeHtml(text)}</strong></p></div>`;
}

function failureMarkup(error, label) {
  return `<div class="state-panel state-failed">${icon("wifi-off")}<p><strong>Could not load ${escapeHtml(label)}.</strong><br>${escapeHtml(error?.message || "")}</p></div>`;
}

// XML entities inside `<loc>`. A sitemap MUST escape `&` as `&amp;`, so a URL
// with query parameters arrives as `?a=1&amp;b=2`; printing that verbatim shows
// the operator a URL that does not exist, and putting it in `href` builds a
// link that 404s. Only the five predefined XML entities — no HTML entity table,
// because this is XML, not HTML.
export function unescapeXml(value) {
  return value
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&apos;/g, "'")
    .replace(/&amp;/g, "&"); // last: otherwise `&amp;lt;` would collapse twice
}

// `<loc>` values of a sitemap. Deliberately a regex over the stored text and
// not DOMParser: the point is to list what the file declares, byte for byte,
// including entries a strict XML parse would reject on a malformed document.
export function extractSitemapLocs(text) {
  const out = [];
  const pattern = /<loc>\s*([^<\s][^<]*?)\s*<\/loc>/gi;
  let match = pattern.exec(text);
  while (match) {
    out.push(unescapeXml(match[1]));
    match = pattern.exec(text);
  }
  return out;
}

export function filterLines(lines, needle) {
  const query = (needle || "").trim().toLocaleLowerCase("en-US");
  if (!query) return lines;
  return lines.filter((line) => line.toLocaleLowerCase("en-US").includes(query));
}

export function createMonitorContentViewer({ onError } = {}) {
  const dialog = document.querySelector("#monitor-content-dialog");
  if (!dialog) return { open: () => {}, close: () => {} };
  const title = dialog.querySelector("#monitor-content-title");
  const subtitle = dialog.querySelector("#monitor-content-subtitle");
  const body = dialog.querySelector("#monitor-content-body");
  const filterInput = dialog.querySelector("#monitor-content-filter");
  const fileLabel = dialog.querySelector("#monitor-content-file-label");
  const fileSelect = dialog.querySelector("#monitor-content-file");
  const count = dialog.querySelector("#monitor-content-count");
  const liveLink = dialog.querySelector("#monitor-content-live");
  const rawLink = dialog.querySelector("#monitor-content-raw");

  let opener = null;
  let resources = [];
  let selected = null; // поточний ресурс
  let lines = []; // рядки або URL поточного файлу
  let mode = "lines"; // "lines" | "urls"
  let token = 0;

  dialog.addEventListener("close", () => {
    token += 1; // будь-який запит у польоті стає нецікавим
    resources = [];
    selected = null;
    lines = [];
    const toFocus = opener;
    opener = null;
    toFocus?.focus();
  });

  dialog.addEventListener("click", (event) => {
    if (event.target.closest('[data-action="content-close"]')) dialog.close();
  });

  filterInput.addEventListener("input", () => renderBody());

  fileSelect.addEventListener("change", () => {
    if (fileSelect.value === ALL_FILES) {
      loadAllFiles();
      return;
    }
    const next = resources.find((resource) => resource.id === fileSelect.value);
    if (next) loadResource(next);
  });

  // Sitemap-дерево — це 5–18 файлів, і оператор шукає «де ця сторінка?», а не
  // «що в page-sitemap.xml». Пошук лише всередині обраного файлу відповідав би
  // на друге питання, тож режим «усі файли» стоїть першим і за замовчуванням.
  const ALL_FILES = "__all__";

  function urlsetResources() {
    return resources.filter((resource) => resource.structure_kind === "urlset" && resource.content_sha256);
  }

  function setLinks(resource) {
    // Живий файл — завжди доступний; збережена копія — лише коли байти справді
    // збережені (у файлу, що впав у 404, `content_sha256` порожній, і посилання
    // на неіснуючий документ було б брехнею).
    liveLink.href = resource?.url || "#";
    liveLink.hidden = !resource?.url;
    if (resource?.content_sha256) {
      rawLink.href = monitorDocumentUrl(resource.content_sha256);
      rawLink.hidden = false;
    } else {
      rawLink.removeAttribute("href");
      rawLink.hidden = true;
    }
  }

  function renderBody() {
    const visible = filterLines(lines, filterInput.value);
    const total = lines.length;
    const noun = mode === "urls" ? "URLs" : "lines";
    count.textContent = total
      ? (visible.length === total ? `${total} ${noun}` : `${visible.length} of ${total} ${noun}`)
      : "";
    if (!total) {
      body.innerHTML = emptyMarkup("This file is empty or was never stored.");
      return;
    }
    if (!visible.length) {
      body.innerHTML = emptyMarkup("Nothing matches this filter.");
      return;
    }
    const shown = visible.slice(0, MAX_RENDERED_LINES);
    const cut = visible.length > shown.length
      ? `<p class="monitor-content-cut">${icon("cut")}Showing the first ${MAX_RENDERED_LINES} of ${visible.length} — narrow the filter, or open the stored copy for everything.</p>`
      : "";
    if (mode === "urls") {
      body.innerHTML = `${cut}<ol class="monitor-content-urls">${shown
        .map((url) => `<li><a href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(url)}</a></li>`)
        .join("")}</ol>`;
    } else {
      body.innerHTML = `${cut}<pre class="monitor-content-text"><code>${shown.map((line) => escapeHtml(line)).join("\n")}</code></pre>`;
    }
  }

  async function loadResource(resource) {
    selected = resource;
    setLinks(resource);
    subtitle.textContent = resource.url;
    body.innerHTML = loadingMarkup("stored file");
    count.textContent = "";
    if (!resource.content_sha256) {
      lines = [];
      body.innerHTML = emptyMarkup("No stored copy of this file yet.");
      return;
    }
    const localToken = (token += 1);
    try {
      const text = await getMonitorDocumentText(resource.content_sha256);
      if (localToken !== token) return;
      const locs = extractSitemapLocs(text);
      // Sitemap → клікабельний список URL (це те, з чим працює оператор);
      // robots.txt та все інше → текст як є.
      mode = locs.length ? "urls" : "lines";
      lines = locs.length ? locs : text.replace(/\r\n/g, "\n").split("\n");
      renderBody();
    } catch (error) {
      if (localToken !== token || isAbortError(error)) return;
      lines = [];
      body.innerHTML = failureMarkup(error, "stored file");
      onError?.(error);
    }
  }

  async function loadAllFiles() {
    const files = urlsetResources();
    selected = null;
    // Немає одного «збереженого файлу» для об'єднаного списку, але живий вхід у
    // дерево є завжди — це індекс (або перший файл, якщо індексу немає).
    const entry = resources.find((resource) => resource.structure_kind === "index") || resources[0];
    setLinks({ url: entry?.url, content_sha256: null });
    subtitle.textContent = `${files.length} sitemap files`;
    body.innerHTML = loadingMarkup("every sitemap file");
    count.textContent = "";
    const localToken = (token += 1);
    try {
      const texts = await Promise.all(files.map((file) => getMonitorDocumentText(file.content_sha256)));
      if (localToken !== token) return;
      mode = "urls";
      // Дедуплікація: один URL цілком законно лежить у двох файлах, і
      // показувати його двічі — це шум, а не факт про сайт.
      lines = [...new Set(texts.flatMap((text) => extractSitemapLocs(text)))];
      renderBody();
    } catch (error) {
      if (localToken !== token || isAbortError(error)) return;
      lines = [];
      body.innerHTML = failureMarkup(error, "sitemap files");
      onError?.(error);
    }
  }

  async function open({ kind, clientId, trigger } = {}) {
    opener = trigger || null;
    title.textContent = kind === "sitemap" ? "Sitemap content" : "Robots.txt content";
    subtitle.textContent = "";
    filterInput.value = "";
    filterInput.placeholder = kind === "sitemap" ? "Filter URLs" : "Filter lines";
    count.textContent = "";
    body.innerHTML = loadingMarkup("files");
    fileLabel.hidden = true;
    setLinks(null);
    if (!dialog.open) dialog.showModal();

    const localToken = (token += 1);
    try {
      const fetched = await getMonitorResources(clientId, kind);
      if (localToken !== token) return;
      resources = Array.isArray(fetched) ? fetched : [];
      if (!resources.length) {
        body.innerHTML = emptyMarkup("Nothing tracked for this client yet.");
        return;
      }
      // Файловий селектор має сенс лише коли файлів справді кілька.
      fileLabel.hidden = resources.length < 2;
      const files = urlsetResources();
      // Без числа: сума `entry_count` по файлах — це кількість ЗАПИСІВ, а
      // об'єднаний список дедуплікований, тож підпис «173 URLs» суперечив би
      // лічильнику «172 URLs» під ним. Точне число дає лічильник, який рахує
      // реальні дані, а не заголовки файлів.
      const allOption = files.length > 1 ? `<option value="${ALL_FILES}">All files</option>` : "";
      fileSelect.innerHTML = allOption + resources
        .map((resource) => {
          const shape = resource.structure_kind === "index" ? "index" : `${resource.entry_count ?? 0} URLs`;
          return `<option value="${escapeHtml(resource.id)}">${escapeHtml(resource.url)} · ${escapeHtml(shape)}</option>`;
        })
        .join("");
      if (allOption) {
        fileSelect.value = ALL_FILES;
        await loadAllFiles();
        return;
      }
      // Один urlset (або robots) — показуємо його одразу; в індексі самих URL
      // немає, тож він ніколи не є кращим стартовим вибором.
      const first = files[0] || resources[0];
      fileSelect.value = first.id;
      await loadResource(first);
    } catch (error) {
      if (localToken !== token || isAbortError(error)) return;
      body.innerHTML = failureMarkup(error, "files");
      onError?.(error);
    }
  }

  return { open, close: () => dialog.close() };
}
