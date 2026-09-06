/* MCP catalog page (task-77): "what tools exist, what they do, how to
 * connect, and ready-to-copy prompts" for the MCP server built in task-74.
 *
 * The catalog itself is never hand-written here — every card, arg, prompt
 * and resource on this page comes straight from `GET /api/v1/mcp/catalog`,
 * which in turn reads the live `app.mcp.server.mcp` object. A future tool
 * (task-74 wave 2) shows up here automatically; nothing in this file names
 * a tool by hand.
 */
import { getMcpCatalog } from "./api.js?v=20260906-t77";
import { escapeHtml, icon } from "./formatters.js?v=20260906-t77";

const root = document.querySelector("#mcp-page-root");

export async function initMcpPage() {
  root.innerHTML = loadingShell();
  let catalog;
  try {
    catalog = await getMcpCatalog();
  } catch (error) {
    root.innerHTML = errorShell(error);
    return;
  }
  root.innerHTML = pageShell(catalog);
  wireConnectTabs();
  wireCopyButtons();
  wireToolSearch(catalog);
}

function loadingShell() {
  return `
    <section class="mcp-intro">
      <p class="eyebrow">Operations / AI assistant</p>
      <h1>MCP server</h1>
    </section>
    <div class="state-panel state-running">
      ${icon("loader-2", "icon icon--spin")}
      <p>Reading the live tool catalog…</p>
    </div>`;
}

function errorShell(error) {
  return `
    <section class="mcp-intro">
      <p class="eyebrow">Operations / AI assistant</p>
      <h1>MCP server</h1>
    </section>
    <div class="state-panel state-failed">
      ${icon("alert-triangle")}
      <p><strong>Couldn't load the MCP catalog.</strong><br>${escapeHtml(error?.message || "Unknown error")}</p>
    </div>`;
}

function pageShell(catalog) {
  return `
    ${hero(catalog)}
    ${connectSection(catalog.connect)}
    ${toolsSection(catalog)}
    ${promptsSection(catalog)}
    ${resourcesSection(catalog.resources)}
    ${limitsSection()}
  `;
}

function hero(catalog) {
  return `
    <section class="mcp-intro">
      <p class="eyebrow">Operations / AI assistant</p>
      <h1>MCP server</h1>
      <p class="mcp-lead">
        Every question below already has an answer somewhere in this dashboard — the MCP
        server just lets an AI assistant ask for it directly, instead of you clicking
        through pages to find it. Connect once (below), then just ask.
      </p>
      <dl class="mcp-status">
        <div><dt>Status</dt><dd class="mcp-status__value mcp-status__value--${catalog.enabled ? "on" : "off"}">
          ${catalog.enabled ? "Enabled" : "Disabled"}</dd></div>
        <div><dt>Tools</dt><dd>${catalog.tools.length}</dd></div>
        <div><dt>Prompts</dt><dd>${catalog.prompts.length}</dd></div>
        <div><dt>Server</dt><dd>${escapeHtml(catalog.server_name)} v${escapeHtml(catalog.server_version)}</dd></div>
      </dl>
    </section>`;
}

function connectSection(connect) {
  const tabs = connect
    .map((snippet, index) => `
      <button type="button" class="mcp-tab" data-tab="${index}" ${index === 0 ? 'aria-selected="true"' : ""}>
        ${escapeHtml(snippet.host)}
      </button>`)
    .join("");
  const panels = connect
    .map((snippet, index) => `
      <div class="mcp-tab-panel" data-panel="${index}" ${index === 0 ? "" : "hidden"}>
        <div class="mcp-code-block">
          <div class="mcp-code-block__head">
            <span>${escapeHtml(snippet.title)}</span>
            <button type="button" class="button button--quiet mcp-copy" data-copy-target="connect-${index}">
              ${icon("copy", "icon icon--tiny")}<span>Copy</span>
            </button>
          </div>
          <pre id="connect-${index}"><code>${escapeHtml(snippet.config)}</code></pre>
        </div>
        <p class="mcp-note">${escapeHtml(snippet.note)}</p>
      </div>`)
    .join("");
  return `
    <section class="mcp-section" aria-labelledby="mcp-connect-title">
      <h2 id="mcp-connect-title">Connect</h2>
      <div class="mcp-tabs" role="tablist">${tabs}</div>
      ${panels}
    </section>`;
}

function toolsSection(catalog) {
  const groups = catalog.groups
    .map((group) => {
      const tools = catalog.tools.filter((t) => t.group === group);
      if (!tools.length) return "";
      return `
        <div class="mcp-tool-group" data-group="${escapeHtml(group)}">
          <h3>${escapeHtml(group)}</h3>
          <div class="mcp-tool-grid">${tools.map(toolCard).join("")}</div>
        </div>`;
    })
    .join("");
  return `
    <section class="mcp-section" aria-labelledby="mcp-tools-title">
      <div class="mcp-section-head">
        <h2 id="mcp-tools-title">Tools</h2>
        <label class="search-field mcp-tool-search">
          <span class="visually-hidden">Search tools</span>
          ${icon("search")}
          <input id="mcp-tool-search" type="search" placeholder="Tool, argument or keyword" autocomplete="off">
        </label>
      </div>
      <p class="result-count" id="mcp-tool-count" aria-live="polite"></p>
      <div id="mcp-tool-groups">${groups}</div>
      <p class="mcp-empty" id="mcp-tool-empty" hidden>No tool matches that search.</p>
    </section>`;
}

function toolCard(tool) {
  const args = tool.args.length
    ? `<div class="mcp-arg-table-wrap"><table class="mcp-arg-table">
        <thead><tr><th>Arg</th><th>Type</th><th>Required</th><th>Description</th></tr></thead>
        <tbody>${tool.args
          .map(
            (arg) => `
          <tr>
            <td><code>${escapeHtml(arg.name)}</code></td>
            <td>${escapeHtml(arg.type)}</td>
            <td>${arg.required ? "Yes" : "Optional"}</td>
            <td>${escapeHtml(arg.description)}</td>
          </tr>`
          )
          .join("")}</tbody>
      </table></div>`
    : `<p class="mcp-no-args">No arguments.</p>`;
  return `
    <article class="mcp-tool-card" data-search="${escapeHtml(
      `${tool.name} ${tool.title} ${tool.summary} ${tool.args.map((a) => a.name).join(" ")}`.toLowerCase()
    )}">
      <header class="mcp-tool-card__head">
        <strong>${escapeHtml(tool.title)}</strong>
        <span class="mcp-badge mcp-badge--${tool.read_only ? "read" : "write"}">
          ${tool.read_only ? "Read" : "Write"}
        </span>
      </header>
      <code class="mcp-tool-card__name">${escapeHtml(tool.name)}</code>
      <p>${escapeHtml(tool.summary)}</p>
      ${args}
    </article>`;
}

function promptsSection(catalog) {
  const mcpPrompts = catalog.prompts
    .map(
      (prompt) => `
      <article class="mcp-prompt-card">
        <header class="mcp-prompt-card__head">
          <strong>${escapeHtml(prompt.title)}</strong>
          <button type="button" class="button button--quiet mcp-copy" data-copy-target="prompt-${escapeHtml(prompt.name)}">
            ${icon("copy", "icon icon--tiny")}<span>Copy</span>
          </button>
        </header>
        <p>${escapeHtml(prompt.description)}</p>
        <pre id="prompt-${escapeHtml(prompt.name)}"><code>${escapeHtml(prompt.rendered_text)}</code></pre>
      </article>`
    )
    .join("");
  const starters = catalog.starter_prompts
    .map(
      (starter, index) => `
      <li class="mcp-starter">
        <span class="mcp-starter__scenario">${escapeHtml(starter.scenario)}</span>
        <span class="mcp-starter__text">“${escapeHtml(starter.text)}”</span>
        <button type="button" class="button button--quiet mcp-copy" data-copy-text="${escapeHtml(starter.text)}"
                data-copy-target="starter-${index}">
          ${icon("copy", "icon icon--tiny")}<span>Copy</span>
        </button>
      </li>`
    )
    .join("");
  return `
    <section class="mcp-section" aria-labelledby="mcp-prompts-title">
      <h2 id="mcp-prompts-title">Prompts</h2>
      <h3 class="mcp-subhead">Ready-made prompts (once connected)</h3>
      <div class="mcp-prompt-grid">${mcpPrompts}</div>
      <h3 class="mcp-subhead">Starter questions (works even before you connect)</h3>
      <ul class="mcp-starter-list">${starters}</ul>
    </section>`;
}

function resourcesSection(resources) {
  const rows = resources
    .map(
      (resource) => `
      <li>
        <code>${escapeHtml(resource.uri)}</code>
        <p>${escapeHtml(resource.description)}</p>
      </li>`
    )
    .join("");
  return `
    <section class="mcp-section" aria-labelledby="mcp-resources-title">
      <h2 id="mcp-resources-title">Resources</h2>
      <ul class="mcp-resource-list">${rows}</ul>
    </section>`;
}

function limitsSection() {
  return `
    <section class="mcp-section" aria-labelledby="mcp-limits-title">
      <h2 id="mcp-limits-title">What this doesn't do</h2>
      <ul class="mcp-limits-list">
        <li>Never writes to an external service (Reporting Ninja, Ahrefs, Semrush, GA4/GSC)
          — the only path to a provider is still the existing job engine.</li>
        <li>Has no direct database access — every tool is a normal client of our own REST API,
          same as your browser.</li>
        <li>Can start a report run, but never a batch — batches stay admin-only, same as in the UI.</li>
        <li>Write access depends on the token's role, exactly like a logged-in person's role.</li>
      </ul>
    </section>`;
}

function wireConnectTabs() {
  const tabs = root.querySelectorAll(".mcp-tab");
  tabs.forEach((tab) => {
    tab.addEventListener("click", () => {
      tabs.forEach((t) => t.removeAttribute("aria-selected"));
      tab.setAttribute("aria-selected", "true");
      root.querySelectorAll(".mcp-tab-panel").forEach((panel) => {
        panel.hidden = panel.dataset.panel !== tab.dataset.tab;
      });
    });
  });
}

function wireCopyButtons() {
  root.querySelectorAll(".mcp-copy").forEach((button) => {
    button.addEventListener("click", async () => {
      const explicitText = button.dataset.copyText;
      const targetId = button.dataset.copyTarget;
      const text = explicitText || root.querySelector(`#${CSS.escape(targetId)}`)?.textContent || "";
      const label = button.querySelector("span");
      const originalLabel = label.textContent;
      try {
        await navigator.clipboard.writeText(text);
        label.textContent = "Copied";
      } catch {
        label.textContent = "Copy failed";
      }
      setTimeout(() => {
        label.textContent = originalLabel;
      }, 1500);
    });
  });
}

function wireToolSearch(catalog) {
  const input = root.querySelector("#mcp-tool-search");
  const count = root.querySelector("#mcp-tool-count");
  const empty = root.querySelector("#mcp-tool-empty");
  const cards = Array.from(root.querySelectorAll(".mcp-tool-card"));
  const groups = Array.from(root.querySelectorAll(".mcp-tool-group"));
  const total = catalog.tools.length;

  function apply() {
    const query = input.value.trim().toLowerCase();
    let visible = 0;
    cards.forEach((card) => {
      const match = !query || card.dataset.search.includes(query);
      card.hidden = !match;
      if (match) visible += 1;
    });
    groups.forEach((group) => {
      const anyVisible = Array.from(group.querySelectorAll(".mcp-tool-card")).some((c) => !c.hidden);
      group.hidden = !anyVisible;
    });
    count.textContent = query ? `${visible} of ${total} tools` : `${total} tools`;
    empty.hidden = visible !== 0;
  }

  input.addEventListener("input", apply);
  apply();
}
