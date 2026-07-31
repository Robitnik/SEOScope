/* Tools hub (task-27): полиця внутрішніх інструментів агенції.
 *
 * Сторінка проєктована як каталог, що росте: блоки приходять з `GET /tools`,
 * тож наступний інструмент — це один запис у реєстрі на бекенді, а не
 * переверстка. Пошук тут робочий (фільтрує назву/опис/теги), а не
 * декоративний, і статистика в блоці — справжня.
 */
import { getToolsCatalog } from "./api.js?v=20260731-refresh1";
import { buildToolsHash } from "./router.js?v=20260731-refresh1";
import { escapeHtml, icon } from "./formatters.js?v=20260731-refresh1";

const root = document.querySelector("#tools-page-root");

// Реєстр модулів інструментів: ключ із каталогу → динамічний імпорт сторінки.
const TOOL_MODULES = {
  "alt-text": () => import("./alt-text-tool.js?v=20260731-refresh1"),
  "redirects": () => import("./redirect-tool.js?v=20260731-refresh1"),
};

export async function initToolsRoute(route) {
  if (route.toolKey) {
    const load = TOOL_MODULES[route.toolKey];
    if (!load) {
      renderUnknownTool(route.toolKey);
      return;
    }
    const module = await load();
    await module.initTool(root, route);
    return;
  }
  await renderHub();
}

function renderUnknownTool(toolKey) {
  root.innerHTML = `
    <a class="back-link" href="${buildToolsHash()}">${icon("arrow-left")}<span>All tools</span></a>
    <div class="state-panel state-failed">
      ${icon("tool")}
      <p><strong>No tool named “${escapeHtml(toolKey)}”.</strong><br>The link may be outdated.</p>
      <a class="button button--primary" href="${buildToolsHash()}">Open the tools list</a>
    </div>`;
}

async function renderHub() {
  root.innerHTML = shell();
  const grid = root.querySelector("#tools-grid");
  const search = root.querySelector("#tools-search");
  const count = root.querySelector("#tools-count");

  let tools = [];
  try {
    const catalog = await getToolsCatalog();
    tools = catalog.tools || [];
  } catch (error) {
    grid.innerHTML = `
      <div class="state-panel state-failed">
        ${icon("wifi-off")}
        <p><strong>Could not load the tools list.</strong><br>${escapeHtml(error.message)}</p>
        <button class="button button--primary" type="button" id="tools-retry">Try again</button>
      </div>`;
    grid.querySelector("#tools-retry").addEventListener("click", () => void renderHub());
    count.textContent = "";
    return;
  }

  const apply = () => {
    const needle = search.value.trim().toLocaleLowerCase("en-US");
    const matches = tools.filter((tool) => !needle || haystack(tool).includes(needle));
    grid.innerHTML = matches.length
      ? matches.map(card).join("")
      : `<p class="tools-empty">${icon("search-off")}<span>No tool matches “${escapeHtml(search.value.trim())}”.
           <button class="link-button" type="button" id="tools-clear">Clear the search</button></span></p>`;
    grid.querySelector("#tools-clear")?.addEventListener("click", () => {
      search.value = "";
      apply();
      search.focus();
    });
    count.textContent = needle
      ? `${matches.length} of ${tools.length} ${tools.length === 1 ? "tool" : "tools"}`
      : `${tools.length} ${tools.length === 1 ? "tool" : "tools"}`;
  };

  if (!tools.length) {
    grid.innerHTML = `
      <p class="tools-empty">${icon("tool")}<span>No tools are available in this build yet.</span></p>`;
    count.textContent = "";
    return;
  }

  search.addEventListener("input", apply);
  apply();
}

function haystack(tool) {
  return `${tool.name} ${tool.summary} ${(tool.tags || []).join(" ")}`.toLocaleLowerCase("en-US");
}

function shell() {
  return `
    <section class="tools-intro">
      <p class="eyebrow">Operations / Tools</p>
      <h1>Internal tools</h1>
      <p class="tools-lead">Small, focused utilities for repetitive client work. Each one keeps its own
        history, so progress survives a reload.</p>
      <div class="tools-search">
        <label class="search-field">
          <span class="visually-hidden">Search tools</span>
          ${icon("search")}
          <input id="tools-search" type="search" placeholder="Tool, task or keyword" autocomplete="off">
        </label>
        <p class="result-count" id="tools-count" aria-live="polite"></p>
      </div>
    </section>
    <section class="tools-grid" id="tools-grid" aria-label="Available tools"></section>`;
}

function card(tool) {
  const stat = tool.stat_value
    ? `<span class="tool-card__stat">${escapeHtml(tool.stat_value)} ${escapeHtml(tool.stat_label || "")}</span>`
    // Свідомо не «0 reports»: відсутність історії — це не нуль-метрика.
    : `<span class="tool-card__stat tool-card__stat--none">Not used yet</span>`;
  const tags = (tool.tags || [])
    .slice(0, 4)
    .map((tag) => `<li>${escapeHtml(tag)}</li>`)
    .join("");
  return `
    <a class="tool-card" href="${escapeHtml(tool.route)}">
      <span class="tool-card__mark" aria-hidden="true">${icon(tool.icon || "tool", "icon")}</span>
      <span class="tool-card__body">
        <strong class="tool-card__name">${escapeHtml(tool.name)}</strong>
        <span class="tool-card__summary">${escapeHtml(tool.summary)}</span>
        <ul class="tool-card__tags">${tags}</ul>
      </span>
      <span class="tool-card__foot">
        ${stat}
        <span class="tool-card__open">Open${icon("arrow-right", "icon icon--tiny")}</span>
      </span>
    </a>`;
}
