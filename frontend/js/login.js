// Сторінка логіну (task-71). Свідомо самодостатня: не імпортує `api.js`
// (той тягне пів-додатка й ще й редіректить на цю саму сторінку при 401),
// лише один fetch.

const form = document.querySelector("#login-form");
const emailInput = document.querySelector("#login-email");
const passwordInput = document.querySelector("#login-password");
const errorBox = document.querySelector("#login-error");
const submit = document.querySelector("#login-submit");
const submitLabel = document.querySelector("#login-submit-label");

// `next` приходить від серверного редіректу (`AuthMiddleware`). Приймаємо
// ЛИШЕ відносний шлях: інакше посилання
// `…/login.html?next=https://evil.example` перетворило б власну форму входу
// на open redirect.
function safeNext() {
  const raw = new URLSearchParams(location.search).get("next") || "/";
  if (!raw.startsWith("/") || raw.startsWith("//")) return "/";
  // Фрагмент серверний редірект не бачить узагалі (він не їде на сервер),
  // але браузер переносить його на сторінку логіну сам. Без цього рядка
  // вхід із глибокого посилання завжди висаджував би людину на дашборді:
  // саме так губився `#/oauth/consent?request=…` (task-78), і людина
  // поверталась у Claude ні з чим, не зрозумівши, що пішло не так.
  const fragment = location.hash;
  if (fragment && !raw.includes("#")) return `${raw}${fragment}`;
  return raw;
}

function showError(message) {
  errorBox.textContent = message;
  errorBox.hidden = false;
}

function clearError() {
  errorBox.hidden = true;
  errorBox.textContent = "";
}

function setBusy(busy) {
  submit.disabled = busy;
  submitLabel.textContent = busy ? "Signing in…" : "Sign in";
  const existing = submit.querySelector(".login-spinner");
  if (busy && !existing) {
    const spinner = document.createElement("span");
    spinner.className = "login-spinner";
    spinner.setAttribute("aria-hidden", "true");
    submit.append(spinner);
  } else if (!busy && existing) {
    existing.remove();
  }
}

form?.addEventListener("submit", async (event) => {
  event.preventDefault();
  clearError();

  const email = emailInput.value.trim();
  const password = passwordInput.value;
  if (!email) {
    showError("Enter your work email.");
    emailInput.focus();
    return;
  }
  if (!password) {
    showError("Enter your password.");
    passwordInput.focus();
    return;
  }

  setBusy(true);
  try {
    const response = await fetch("/api/v1/auth/login", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({ email, password }),
    });
    if (response.status === 401) {
      showError("Invalid email or password. After 10 failed attempts the account locks for 15 minutes.");
      passwordInput.value = "";
      passwordInput.focus();
      return;
    }
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      showError(payload?.detail?.message || `Sign-in failed (HTTP ${response.status}).`);
      return;
    }
    // Повна навігація, не history API: після входу треба, щоб додаток
    // стартував з нуля вже із сесією (шел читає її на завантаженні).
    location.replace(safeNext());
  } catch {
    showError("Cannot reach the server. Check your connection and try again.");
  } finally {
    setBusy(false);
  }
});
