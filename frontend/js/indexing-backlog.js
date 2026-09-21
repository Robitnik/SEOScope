/* Indexing Planner — Backlog (task-100 §2.5): the whole queue, and the
 * excluded list next to it.
 *
 * Excluded pages are shown, not hidden. Measured live, 36 of them across the
 * portfolio (noindex, redirects, robots, foreign canonicals, one 404) — if
 * they silently disappeared, nobody would ever notice a page was excluded by
 * mistake.
 */
import {
  getIndexingClients,
  getIndexingQueue,
  setIndexingUrlExcluded,
  updateIndexingSettings,
} from "./api.js?v=20260921-t100";
import { escapeHtml, icon } from "./formatters.js?v=20260921-t100";
import { evidenceSentence, excludedLabel, reasonLabel } from "./indexing-tool.js?v=20260921-t100";

let state = { clientId: "", excluded: false };

export async function renderBacklog(host) {
  const [clients, queue] = await Promise.all([
    getIndexingClients(),
    getIndexingQueue({ clientId: state.clientId || undefined, excluded: state.excluded }),
  ]);
  host.innerHTML = `
    <div class="indexing-filters">
      <label class="field">
        <span>Client</span>
        <select id="indexing-filter-client">
          <option value="">All clients</option>
          ${clients.clients
            .map(
              (client) =>
                `<option value="${escapeHtml(client.id)}"${client.id === state.clientId ? " selected" : ""}>${escapeHtml(client.name)}</option>`,
            )
            .join("")}
        </select>
      </label>
      <div class="indexing-toggle" role="group" aria-label="Queue or excluded">
        <button type="button" class="${state.excluded ? "" : "is-active"}" data-excluded="false">Queue</button>
        <button type="button" class="${state.excluded ? "is-active" : ""}" data-excluded="true">Excluded</button>
      </div>
      <p class="result-count">${queue.total} ${queue.total === 1 ? "page" : "pages"}</p>
    </div>
    ${clientSettingsBlock(clients)}
    ${queue.urls.length ? tableFor(queue) : emptyBlock()}`;

  host.querySelector("#indexing-filter-client").addEventListener("change", (event) => {
    state.clientId = event.target.value;
    void renderBacklog(host);
  });
  host.querySelectorAll("[data-excluded]").forEach((button) => {
    button.addEventListener("click", () => {
      state.excluded = button.dataset.excluded === "true";
      void renderBacklog(host);
    });
  });
  host.querySelectorAll(".indexing-exclude").forEach((button) => {
    button.addEventListener("click", async () => {
      button.disabled = true;
      await setIndexingUrlExcluded(button.dataset.id, button.dataset.action === "exclude");
      await renderBacklog(host);
    });
  });
  bindSettings(host);
}

function clientSettingsBlock(clients) {
  if (!state.clientId) return "";
  const client = clients.clients.find((row) => row.id === state.clientId);
  if (!client) return "";
  return `
    <form class="indexing-settings" id="indexing-settings" data-client="${escapeHtml(client.id)}">
      <label class="field field--inline">
        <input type="checkbox" name="enabled"${client.settings.enabled ? " checked" : ""}>
        <span>Plan for this client</span>
      </label>
      <label class="field">
        <span>Pages per day</span>
        <input type="number" name="daily_quota" min="1" max="50" value="${client.settings.daily_quota}">
      </label>
      <label class="field">
        <span>Re-submit after (days)</span>
        <input type="number" name="resubmit_after_days" min="1" max="365" value="${client.settings.resubmit_after_days}">
      </label>
      <button class="button button--primary" type="submit">Save</button>
      ${client.configured ? "" : `<p class="indexing-settings__warn">${icon("alert-triangle")} Search Console isn't connected for this client.</p>`}
    </form>`;
}

function bindSettings(host) {
  const form = host.querySelector("#indexing-settings");
  if (!form) return;
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = new FormData(form);
    await updateIndexingSettings(form.dataset.client, {
      enabled: form.querySelector("[name=enabled]").checked,
      daily_quota: Number(data.get("daily_quota")),
      resubmit_after_days: Number(data.get("resubmit_after_days")),
    });
    await renderBacklog(host);
  });
}

function emptyBlock() {
  return `
    <div class="state-panel">
      ${icon("check")}
      <p><strong>${state.excluded ? "Nothing is excluded." : "The queue is empty."}</strong><br>
        ${state.excluded ? "Every page in the sitemap is either indexed or waiting in the queue." : "Every page Google can index is already in the index. That is the goal state, not an error."}</p>
    </div>`;
}

function tableFor(queue) {
  const rows = queue.urls
    .map((row) => {
      const evidence = evidenceSentence(row.reason_code, row.reason);
      const label = row.excluded
        ? `<span class="indexing-reason indexing-reason--excluded">${escapeHtml(excludedLabel(row.excluded_reason))}</span>`
        : `<span class="indexing-reason indexing-reason--${escapeHtml(row.reason_code)}">${escapeHtml(reasonLabel(row.reason_code))}</span>`;
      const cooldown = row.cooldown_days_left
        ? `<span class="indexing-cooldown">${icon("clock")} ${row.cooldown_days_left}d cooldown</span>`
        : "";
      return `
        <tr>
          <td>${escapeHtml(row.client_name)}</td>
          <td class="indexing-cell-url">
            <a href="${escapeHtml(row.url)}" target="_blank" rel="noopener">${escapeHtml(row.url)}</a>
            ${row.title ? `<p class="indexing-cell-title">${escapeHtml(row.title)}</p>` : ""}
          </td>
          <td>${label}${evidence ? `<p class="indexing-evidence">${escapeHtml(evidence)}</p>` : ""}</td>
          <td class="indexing-num">${row.priority_score === null || row.priority_score === undefined ? "—" : Math.round(Number(row.priority_score))}</td>
          <td>${cooldown || (row.submissions_count ? `${row.submissions_count} sent` : "—")}</td>
          <td class="indexing-table__actions">
            <button class="button button--ghost indexing-exclude" type="button"
              data-id="${escapeHtml(row.id)}" data-action="${row.excluded ? "include" : "exclude"}">
              ${row.excluded ? `${icon("plus")}<span>Put back</span>` : `${icon("ban")}<span>Exclude</span>`}
            </button>
          </td>
        </tr>`;
    })
    .join("");
  return `
    <div class="indexing-table-wrap">
      <table class="indexing-table">
        <thead>
          <tr>
            <th scope="col">Client</th>
            <th scope="col">Page</th>
            <th scope="col">Why</th>
            <th scope="col" class="indexing-num">Score</th>
            <th scope="col">History</th>
            <th scope="col" class="indexing-table__actions">Actions</th>
          </tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;
}
