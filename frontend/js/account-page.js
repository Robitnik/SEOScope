/* Особистий кабінет (task-79): «кожен видає собі сам». Перша ОСОБИСТА
 * сторінка в тулі — досі кожна сторінка була про клієнтів або про
 * адміністрування чужих акаунтів, ця про власника сесії.
 *
 * Головна відповідальність екрана — момент видачі токена: значення
 * показується РІВНО ОДИН раз (той самий патерн, що видача пароля в
 * `admin-users-page.js`), і сторінка робить цей момент неможливим проґавити.
 */
import { createMyToken, listMyTokens, revokeMyToken } from "./api.js?v=20260906-t79";
// `?v=20260906-t72`, NOT this file's own tag: `session.js` is a per-tag
// SEPARATE module instance (see router.js's own long comment on this), and
// the boot IIFE that actually calls `loadSession()` imports it under THIS
// exact tag — importing under any other tag gets a second instance whose
// `currentUser()` was never populated (found live: hero showed "Email —").
import { currentUser } from "./session.js?v=20260906-t72";
import { escapeHtml, formatTimestamp, icon } from "./formatters.js?v=20260906-t79";
import { buildMcpConnectHash } from "./router.js?v=20260906-t79";

const root = document.querySelector("#account-page-root");

// Стеля рівно тут же, у списку опцій — сервер однаково обріже (§3.3
// плану), але показувати "Admin" людині, яка сама viewer, лише щоб
// побачити відмову, безглуздо.
const ROLE_OPTIONS = [
  ["admin", "Administrator"],
  ["member", "Member"],
  ["viewer", "Read-only"],
];
const LIFETIME_OPTIONS = [
  [30, "30 days"],
  [90, "90 days (recommended)"],
  [365, "1 year"],
];
const ROLE_RANK = { viewer: 0, member: 1, admin: 2 };

export async function initAccountPage() {
  root.innerHTML = loadingShell();
  let tokens;
  try {
    tokens = await listMyTokens();
  } catch (error) {
    root.innerHTML = errorShell(error);
    return;
  }
  root.innerHTML = pageShell(tokens);
  wireForm();
  wireRevoke();
}

function loadingShell() {
  return `
    ${hero()}
    <div class="state-panel state-running">
      ${icon("loader-2", "icon icon--spin")}
      <p>Reading your tokens…</p>
    </div>`;
}

function errorShell(error) {
  return `
    ${hero()}
    <div class="state-panel state-failed">
      ${icon("alert-triangle")}
      <p><strong>Couldn't load your tokens.</strong><br>${escapeHtml(error?.message || "Unknown error")}</p>
    </div>`;
}

function hero() {
  const user = currentUser() || {};
  return `
    <section class="account-intro">
      <p class="eyebrow">Your account</p>
      <h1>${escapeHtml(user.name || "Account")}</h1>
      <dl class="account-status">
        <div><dt>Email</dt><dd>${escapeHtml(user.email || "—")}</dd></div>
        <div><dt>Role</dt><dd>${escapeHtml(roleLabel(user.role))}</dd></div>
      </dl>
    </section>`;
}

function roleLabel(role) {
  return ROLE_OPTIONS.find(([value]) => value === role)?.[1] || role || "—";
}

function pageShell(tokens) {
  const ownRole = (currentUser() || {}).role || "viewer";
  const allowedRoleOptions = ROLE_OPTIONS.filter(
    ([value]) => (ROLE_RANK[value] ?? 0) <= (ROLE_RANK[ownRole] ?? 0)
  );
  return `
    ${hero()}
    <section class="account-section" aria-labelledby="account-tokens-title">
      <div class="account-section-head">
        <div>
          <h2 id="account-tokens-title">Personal access tokens</h2>
          <p class="account-lead">
            Use these to call the API from scripts, CI, or an MCP host — see
            <a href="${buildMcpConnectHash()}">how to connect</a> for the MCP setup.
            A token never does more than your OWN role allows, and it stops working
            the moment you're deactivated or demoted.
          </p>
        </div>
      </div>

      <div id="account-secret-panel" class="account-secret-panel" hidden></div>

      <form id="account-token-form" class="account-token-form">
        <label class="account-field">
          <span>Label</span>
          <input type="text" id="account-token-label" placeholder="e.g. claude-code" maxlength="200" required>
        </label>
        <label class="account-field">
          <span>Role</span>
          <select id="account-token-role">
            ${allowedRoleOptions.map(([value, label]) => `<option value="${value}" ${value === ownRole ? "selected" : ""}>${escapeHtml(label)}</option>`).join("")}
          </select>
        </label>
        <label class="account-field">
          <span>Expires</span>
          <select id="account-token-lifetime">
            ${LIFETIME_OPTIONS.map(([value, label]) => `<option value="${value}" ${value === 90 ? "selected" : ""}>${escapeHtml(label)}</option>`).join("")}
          </select>
        </label>
        <button type="submit" class="button button--primary">
          ${icon("plus", "icon icon--tiny")}<span>New token</span>
        </button>
        <p class="account-form-error" id="account-token-error" role="alert" hidden></p>
      </form>

      ${tokensTable(tokens)}
    </section>`;
}

function tokensTable(tokens) {
  if (!tokens.length) {
    return `
      <p class="account-empty" id="account-tokens-empty">
        No tokens yet. Create one above, or read
        <a href="${buildMcpConnectHash()}">how to connect</a> an MCP host.
      </p>`;
  }
  const rows = tokens
    .map((token) => {
      const active = !token.revoked_at;
      return `
        <tr data-token-id="${escapeHtml(token.id)}" class="${active ? "" : "account-row--revoked"}">
          <td>
            <strong>${escapeHtml(token.label)}</strong>
            <code class="account-token-prefix">yvth_${escapeHtml(token.prefix)}…</code>
          </td>
          <td>${escapeHtml(roleLabel(token.role))}</td>
          <td>${escapeHtml(formatTimestamp(token.created_at))}</td>
          <td>${escapeHtml(formatTimestamp(token.expires_at, "—"))}</td>
          <td>${escapeHtml(formatTimestamp(token.last_used_at, "Never used"))}</td>
          <td>
            ${
              active
                ? `<button type="button" class="button button--quiet account-revoke" data-token-id="${escapeHtml(token.id)}">Revoke</button>`
                : `<span class="account-badge account-badge--revoked">Revoked</span>`
            }
          </td>
        </tr>`;
    })
    .join("");
  return `
    <div class="account-table-wrap">
      <table class="account-table" id="account-tokens-table">
        <thead>
          <tr><th>Token</th><th>Role</th><th>Created</th><th>Expires</th><th>Last used</th><th></th></tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;
}

function showSecret({ label, token }) {
  const panel = root.querySelector("#account-secret-panel");
  panel.hidden = false;
  panel.innerHTML = `
    <p class="eyebrow">New token — shown once</p>
    <h3>${escapeHtml(label)}</h3>
    <div class="account-secret-row">
      <code id="account-secret-value">${escapeHtml(token)}</code>
      <button type="button" class="button button--primary" id="account-secret-copy">
        ${icon("copy", "icon icon--tiny")}<span>Copy</span>
      </button>
    </div>
    <p class="account-note">
      This value is shown exactly once and cannot be recovered — only revoked and
      reissued. Paste it into your host's config now.
    </p>
    <button type="button" class="button button--quiet" id="account-secret-dismiss">I've saved it</button>`;

  panel.querySelector("#account-secret-copy").addEventListener("click", async (event) => {
    const button = event.currentTarget;
    const label = button.querySelector("span");
    try {
      await navigator.clipboard.writeText(token);
      label.textContent = "Copied";
    } catch {
      const range = document.createRange();
      range.selectNodeContents(panel.querySelector("#account-secret-value"));
      const selection = window.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
      label.textContent = "Press Ctrl+C";
    }
  });
  panel.querySelector("#account-secret-dismiss").addEventListener("click", () => {
    panel.hidden = true;
    panel.innerHTML = "";
  });
  panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function wireForm() {
  const form = root.querySelector("#account-token-form");
  const errorEl = root.querySelector("#account-token-error");
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    errorEl.hidden = true;
    const label = root.querySelector("#account-token-label").value.trim();
    const role = root.querySelector("#account-token-role").value;
    const expiresInDays = Number(root.querySelector("#account-token-lifetime").value);
    if (!label) {
      errorEl.textContent = "Give the token a label so you recognize it later.";
      errorEl.hidden = false;
      return;
    }
    const submit = form.querySelector("button[type=submit]");
    submit.disabled = true;
    try {
      const created = await createMyToken({ label, role, expires_in_days: expiresInDays });
      form.reset();
      showSecret({ label: created.label, token: created.token });
      await refreshTable();
    } catch (error) {
      errorEl.textContent = error?.message || "Could not create the token.";
      errorEl.hidden = false;
    } finally {
      submit.disabled = false;
    }
  });
}

function wireRevoke() {
  root.addEventListener("click", async (event) => {
    const button = event.target.closest(".account-revoke");
    if (!button) return;
    const tokenId = button.dataset.tokenId;
    const row = root.querySelector(`tr[data-token-id="${CSS.escape(tokenId)}"]`);
    const label = row?.querySelector("strong")?.textContent || "this token";
    if (!window.confirm(`Revoke "${label}"? Anything using it stops working immediately.`)) {
      return;
    }
    button.disabled = true;
    try {
      await revokeMyToken(tokenId);
      await refreshTable();
    } catch (error) {
      button.disabled = false;
      window.alert(error?.message || "Could not revoke the token.");
    }
  });
}

async function refreshTable() {
  const tokens = await listMyTokens();
  const container = root.querySelector("#account-tokens-table")?.closest(".account-table-wrap")
    || root.querySelector("#account-tokens-empty");
  container.outerHTML = tokensTable(tokens);
}
