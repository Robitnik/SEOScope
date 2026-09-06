/* Екран згоди OAuth (task-78).
 *
 * Сюди браузер потрапляє з `/authorize` — тобто людина щойно натиснула
 * Connect у Claude і чекає, що її спитають. Сторінка робить рівно одну
 * річ: показує, ХТО просить доступ і КУДИ полетить код, і дає дві
 * кнопки. Жодних налаштувань, жодного вибору scope — обсяг доступу тут
 * визначає роль людини, і вона не змінюється цим екраном.
 *
 * `session.js` імпортується під тим самим тегом, що використовує
 * boot-IIFE у router.js. Інакший `?v=` створив би ДРУГИЙ екземпляр
 * модуля з порожнім кешем сесії — і сторінка вважала б реально
 * залогінену людину анонімом.
 */
import { currentUser } from "./session.js?v=20260906-t72";
import { getPendingAuthorization, approveAuthorization } from "./api.js?v=20260906-t78";
import { escapeHtml, icon } from "./formatters.js?v=20260906-t79";

const root = document.querySelector("#oauth-consent-page-root");

export async function initOauthConsentPage(requestId) {
  if (!requestId) {
    root.innerHTML = failure(
      "Nothing to approve",
      "This link is missing its request id. Start the connection again from Claude.",
    );
    return;
  }
  root.innerHTML = loading();
  let pending;
  try {
    pending = await getPendingAuthorization(requestId);
  } catch (error) {
    root.innerHTML = failure(
      "This request expired",
      error?.message || "Start the connection again from Claude.",
    );
    return;
  }
  root.innerHTML = shell(pending);
  wire(pending);
}

function loading() {
  return `
    <section class="consent-card">
      <div class="state-panel state-running">
        ${icon("loader-2", "icon icon--spin")}
        <p>Checking the request…</p>
      </div>
    </section>`;
}

function failure(title, message) {
  return `
    <section class="consent-card">
      <h1>${escapeHtml(title)}</h1>
      <p class="consent-lead">${escapeHtml(message)}</p>
      <a class="button button--quiet" href="#/mcp/connect">
        ${icon("arrow-left", "icon icon--tiny")}<span>Back to How to connect</span>
      </a>
    </section>`;
}

function shell(pending) {
  const user = currentUser();
  const host = hostOf(pending.redirect_host);
  return `
    <section class="consent-card">
      <p class="eyebrow">Connection request</p>
      <h1>Connect ${escapeHtml(pending.client_name)}?</h1>
      <p class="consent-lead">
        It will be able to read and act in this dashboard <strong>as you</strong> — the
        same clients, the same reports, nothing you can't already see yourself.
      </p>
      <dl class="consent-facts">
        <div>
          <dt>Signed in as</dt>
          <dd>${escapeHtml(user?.email || "—")}${
            user?.role ? ` <span class="consent-role">${escapeHtml(user.role)}</span>` : ""
          }</dd>
        </div>
        <div>
          <dt>Sends you back to</dt>
          <dd><code>${escapeHtml(host)}</code></dd>
        </div>
        <div>
          <dt>Access expires</dt>
          <dd>in 30 days, then renews itself while you keep using it</dd>
        </div>
      </dl>
      <p class="consent-note">
        Every action it takes is recorded in the journal under your name, and you can
        revoke it any time in <a href="#/account">Your account</a>.
      </p>
      <div class="consent-actions">
        <button type="button" class="button button--primary" id="consent-approve">
          ${icon("check", "icon icon--tiny")}<span>Approve</span>
        </button>
        <a class="button button--quiet" href="#/account">Cancel</a>
      </div>
      <p class="consent-error" id="consent-error" hidden></p>
    </section>`;
}

/* Хост, а не повний URL: MCP-специфікація вимагає показати саме його —
 * назву клієнта пише сам клієнт (їй вірити не можна), а хост редіректу
 * зареєстрований і підмінити його непомітно не вийде. */
function hostOf(rawUrl) {
  try {
    return new URL(rawUrl).host;
  } catch {
    return rawUrl;
  }
}

function wire(pending) {
  const button = root.querySelector("#consent-approve");
  const errorEl = root.querySelector("#consent-error");
  button?.addEventListener("click", async () => {
    button.disabled = true;
    errorEl.hidden = true;
    try {
      const { redirect_to: redirectTo } = await approveAuthorization(pending.request_id);
      // Повний перехід, не hash-навігація: ми свідомо ЙДЕМО З застосунку
      // назад у Claude, і код авторизації має поїхати саме туди.
      window.location.href = redirectTo;
    } catch (error) {
      button.disabled = false;
      errorEl.textContent = error?.message || "Couldn't approve this request.";
      errorEl.hidden = false;
    }
  });
}
