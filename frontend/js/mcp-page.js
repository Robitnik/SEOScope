/* MCP catalog page (task-77): "what tools exist, what they do, and
 * ready-to-copy prompts" for the MCP server built in task-74. "How to
 * connect" lives on its own page (`#/mcp/connect`) — it's read once, not
 * referred back to, and doesn't deserve the scroll distance it was costing
 * the Tools/Prompts sections below it.
 *
 * The catalog itself is never hand-written here — every card, arg, prompt
 * and resource on this page comes straight from `GET /api/v1/mcp/catalog`,
 * which in turn reads the live `app.mcp.server.mcp` object. A future tool
 * (task-74 wave 2) shows up here automatically; nothing in this file names
 * a tool by hand.
 */
import { getMcpCatalog } from "./api.js?v=20260906-t77";
import { escapeHtml, icon } from "./formatters.js?v=20260906-t77";
import { buildAccountHash, buildMcpHash, buildMcpConnectHash } from "./router.js?v=20260906-t77";

const root = document.querySelector("#mcp-page-root");
const connectRoot = document.querySelector("#mcp-connect-page-root");

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
  wireCopyButtons(root);
  wireToolSearch(catalog);
}

export async function initMcpConnectPage() {
  connectRoot.innerHTML = loadingShell();
  let catalog;
  try {
    catalog = await getMcpCatalog();
  } catch (error) {
    connectRoot.innerHTML = errorShell(error);
    return;
  }
  connectRoot.innerHTML = connectPageShell(catalog);
  wireStepChecks();
  wireCopyButtons(connectRoot);
  wireDownloadButtons(connectRoot);
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
    ${promptsSection(catalog)}
    ${toolsSection(catalog)}
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
        through pages to find it.
      </p>
      <dl class="mcp-status">
        <div><dt>Status</dt><dd class="mcp-status__value mcp-status__value--${catalog.enabled ? "on" : "off"}">
          ${catalog.enabled ? "Enabled" : "Disabled"}</dd></div>
        <div><dt>Tools</dt><dd>${catalog.tools.length}</dd></div>
        <div><dt>Prompts</dt><dd>${catalog.prompts.length}</dd></div>
        <div><dt>Server</dt><dd>${escapeHtml(catalog.server_name)} v${escapeHtml(catalog.server_version)}</dd></div>
      </dl>
      <a class="button button--primary mcp-connect-cta" href="${buildMcpConnectHash()}">
        ${icon("plug", "icon icon--tiny")}<span>How to connect</span>
      </a>
    </section>`;
}

// Конект — це чекліст, а не довідка: людина проходить його рівно один
// раз, з чужого пристрою, і найчастіше губиться саме на тому, "а що я
// вже зробив". Тому кожен крок має власний чекбокс, а стан живе в
// localStorage — щоб перезавантаження сторінки (або похід у Your
// account по токен і назад) не скидав прогрес.
function connectPageShell(catalog) {
  const steps = [
    connectStep(1, "Open Connectors on claude.ai", openSettingsStepBody()),
    connectStep(2, "Add this URL as a custom connector", urlStepBody(catalog)),
    connectStep(3, "Sign in here and approve", approveStepBody()),
    connectStep(4, "Brief the assistant", briefStepBody(catalog)),
    connectStep(5, "Ask your first question", askStepBody(catalog)),
  ].join("");
  return `
    <a class="back-link" href="${buildMcpHash()}">${icon("arrow-left")}<span>MCP server</span></a>
    <section class="mcp-intro">
      <p class="eyebrow">Operations / AI assistant</p>
      <h1>How to connect</h1>
      <p class="mcp-lead">
        Five steps, about two minutes, done once. Nothing to install — the tools
        appear in your normal chats on claude.ai. Tick each step off as you go;
        this page remembers where you stopped.
      </p>
    </section>
    <ol class="mcp-steps">${steps}</ol>`;
}

function connectStep(number, title, body) {
  return `
    <li class="mcp-step" data-step="${number}">
      <label class="mcp-step__head">
        <input type="checkbox" class="mcp-step__check" data-step-key="${number}">
        <span class="mcp-step__num">${number}</span>
        <span class="mcp-step__title">${escapeHtml(title)}</span>
      </label>
      <div class="mcp-step__body">${body}</div>
    </li>`;
}

function openSettingsStepBody() {
  return `
    <p>
      On <strong>claude.ai</strong>, open <strong>Settings → Connectors</strong>, then
      <strong>Add custom connector</strong>. Same place in the desktop app and on mobile —
      it's one account, so connecting once covers all of them.
    </p>`;
}

function urlStepBody(catalog) {
  return `
    <p>Paste this as the connector URL. That's the whole configuration — there is no file to edit.</p>
    <div class="mcp-code-block">
      <div class="mcp-code-block__head">
        <span>Connector URL</span>
        <button type="button" class="button button--quiet mcp-copy" data-copy-target="connect-url">
          ${icon("copy", "icon icon--tiny")}<span>Copy</span>
        </button>
      </div>
      <pre id="connect-url"><code>${escapeHtml(catalog.mcp_url)}</code></pre>
    </div>
    <p class="mcp-note">
      Leave the OAuth Client ID and Secret fields empty — this server registers Claude
      by itself.
    </p>`;
}

function approveStepBody() {
  return `
    <p>
      Claude opens this dashboard and asks you to sign in — your normal email and
      password. Then you get one screen naming what's connecting; press
      <strong>Approve</strong> and Claude takes it from there.
    </p>
    <p class="mcp-note">
      The connector acts <strong>as you</strong>: it sees exactly what your role lets you
      see, every action lands in the journal under your name, and it appears in
      <a href="${buildAccountHash()}">Your account</a> where you can revoke it any time.
    </p>`;
}

function briefStepBody(catalog) {
  return `
    <p>
      Open a new chat and paste this first, so the assistant knows every tool it now
      has. Without it, it has the tools but no idea when to reach for them.
    </p>
    <article class="mcp-prompt-card mcp-prompt-card--all">
      <header class="mcp-prompt-card__head">
        <div>
          <strong>Brief an assistant on every tool</strong>
          <p class="mcp-prompt-card__sub">One paste covers all ${catalog.tools.length} tools.</p>
        </div>
        <div class="mcp-prompt-card__actions">
          <button type="button" class="button button--primary mcp-copy" data-copy-target="prompt-all-tools">
            ${icon("copy", "icon icon--tiny")}<span>Copy</span>
          </button>
          <button type="button" class="button button--quiet mcp-download"
                  data-download-target="prompt-all-tools" data-download-name="yovth-mcp-tools-briefing.md">
            ${icon("download", "icon icon--tiny")}<span>Download .md</span>
          </button>
        </div>
      </header>
      <pre id="prompt-all-tools"><code>${escapeHtml(catalog.all_tools_prompt)}</code></pre>
    </article>`;
}

function askStepBody(catalog) {
  const example = catalog.starter_prompts?.[0]?.text
    || "List every client and flag anyone whose health looks off right now.";
  return `
    <p>Type your real question straight under the briefing. Something like:</p>
    <div class="mcp-code-block">
      <div class="mcp-code-block__head">
        <span>Example</span>
        <button type="button" class="button button--quiet mcp-copy" data-copy-text="${escapeHtml(example)}">
          ${icon("copy", "icon icon--tiny")}<span>Copy</span>
        </button>
      </div>
      <pre id="connect-example"><code>${escapeHtml(example)}</code></pre>
    </div>
    <p class="mcp-note">
      Stuck? The <a href="${buildMcpHash()}">tool catalog</a> lists every question
      the server can answer, with the exact arguments each tool takes.
    </p>`;
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
  // All 12 cards share ONE fixed height (mcp.css) regardless of how many
  // arguments a tool has — `run_client_report`'s 7-row table scrolls
  // inside `.mcp-tool-card__body` instead of stretching its card (and
  // dragging every sibling in its grid row along with it).
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
      <div class="mcp-tool-card__body">${args}</div>
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
      <p class="mcp-lead">
        New here? The <a href="${buildMcpConnectHash()}">connect page</a> has a one-paste briefing
        that covers every tool below in a single prompt.
      </p>
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

const STEPS_STORAGE_KEY = "yovth.mcp.connect.steps";

// Прогрес чекліста — суто клієнтський: це нотатка "де я зупинився" для
// цього браузера, а не стан акаунта. Тому localStorage, а не API: інакше
// довелося б заводити ендпоінт і рядок у БД заради галочки, яку людина
// ставить один раз у житті.
function wireStepChecks() {
  let done = [];
  try {
    done = JSON.parse(window.localStorage.getItem(STEPS_STORAGE_KEY) || "[]");
  } catch {
    done = [];
  }
  const boxes = connectRoot.querySelectorAll(".mcp-step__check");
  boxes.forEach((box) => {
    box.checked = done.includes(box.dataset.stepKey);
    box.closest(".mcp-step").classList.toggle("is-done", box.checked);
    box.addEventListener("change", () => {
      box.closest(".mcp-step").classList.toggle("is-done", box.checked);
      const checked = [...connectRoot.querySelectorAll(".mcp-step__check")]
        .filter((b) => b.checked)
        .map((b) => b.dataset.stepKey);
      try {
        window.localStorage.setItem(STEPS_STORAGE_KEY, JSON.stringify(checked));
      } catch {
        /* приватний режим — галочки просто не переживуть перезавантаження */
      }
    });
  });
}

function wireCopyButtons(scopeEl) {
  scopeEl.querySelectorAll(".mcp-copy").forEach((button) => {
    button.addEventListener("click", async () => {
      const explicitText = button.dataset.copyText;
      const targetId = button.dataset.copyTarget;
      const text = explicitText || scopeEl.querySelector(`#${CSS.escape(targetId)}`)?.textContent || "";
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

function wireDownloadButtons(scopeEl) {
  scopeEl.querySelectorAll(".mcp-download").forEach((button) => {
    button.addEventListener("click", () => {
      const targetId = button.dataset.downloadTarget;
      const filename = button.dataset.downloadName || "download.md";
      const text = scopeEl.querySelector(`#${CSS.escape(targetId)}`)?.textContent || "";
      const heading = filename.replace(/\.md$/, "").replace(/[-_]+/g, " ");
      const markdown = `# ${heading}\n\n\`\`\`\n${text}\n\`\`\`\n`;
      const blobUrl = URL.createObjectURL(new Blob([markdown], { type: "text/markdown" }));
      const link = document.createElement("a");
      link.href = blobUrl;
      link.download = filename;
      document.body.append(link);
      link.click();
      // Both deferred, never synchronous: tearing the anchor out of the DOM
      // (or revoking the blob) in the same tick as `.click()` aborts the
      // download in Chromium before it has read a byte.
      setTimeout(() => {
        link.remove();
        URL.revokeObjectURL(blobUrl);
      }, 1000);
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
