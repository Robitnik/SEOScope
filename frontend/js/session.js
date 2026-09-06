// Поточний користувач у шелі (task-71): меню в хедері + рольове ховання
// навігації. Сесію читаємо ОДИН раз на завантаження сторінки — вона не
// змінюється в межах життя вкладки, а зміна ролі приходить із наступним
// переходом; зайвий запит на кожен рендер був би податком на найгарячіший
// шлях (`AuthMiddleware` уже робить SELECT на кожен запит).

import { getAuthSession, logout } from "./api.js?v=20260906-t72";

let cached = null;

export async function loadSession() {
  if (cached) return cached;
  cached = await getAuthSession();
  return cached;
}

export function currentUser() {
  return cached?.user || null;
}

export function isAdmin() {
  return currentUser()?.role === "admin";
}

export function canMutate() {
  const role = currentUser()?.role;
  return role === "admin" || role === "member";
}

// Слаги інструментів, які бекенд однаково закриє для non-admin. Джерело —
// та сама конфігурація, що й серверна політика (`/auth/session`), тому UI і
// сервер не можуть розійтися: кнопка не показується рівно там, де дія дасть 403.
export function adminOnlyTools() {
  return cached?.admin_only_tools || [];
}

function roleLabel(role) {
  if (role === "admin") return "Administrator";
  if (role === "viewer") return "Read-only";
  return "Member";
}

export function renderUserMenu(session) {
  const root = document.querySelector("#user-menu");
  if (!root || !session?.user) return;

  const user = session.user;
  root.hidden = false;
  root.querySelector("#user-menu-name").textContent = user.name;
  root.querySelector("#user-menu-role").textContent = roleLabel(user.role);
  root.querySelector("#user-menu-email").textContent = user.email;

  const isAdminUser = user.role === "admin";
  root.querySelectorAll("#user-menu-admin, #user-menu-audit").forEach((link) => {
    link.hidden = !isAdminUser;
  });

  const toggle = root.querySelector("#user-menu-toggle");
  const panel = root.querySelector("#user-menu-panel");

  function close() {
    panel.hidden = true;
    toggle.setAttribute("aria-expanded", "false");
  }

  toggle.addEventListener("click", () => {
    const open = panel.hidden;
    panel.hidden = !open;
    toggle.setAttribute("aria-expanded", String(open));
    if (open) panel.querySelector("a, button")?.focus();
  });

  document.addEventListener("click", (event) => {
    if (!root.contains(event.target)) close();
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !panel.hidden) {
      close();
      toggle.focus();
    }
  });

  root.querySelector("#user-menu-signout").addEventListener("click", async () => {
    try {
      await logout();
    } finally {
      // Навіть якщо запит не дійшов, ведемо на логін: cookie все одно
      // недійсний для будь-якої наступної дії, а людина очікує, що вийшла.
      location.replace("/login.html");
    }
  });
}

// Ховає навігацію й дії, недоступні цій ролі. Не «захист» — сервер усе одно
// відмовить; це про те, щоб людина не тицяла в кнопку, яка завжди дає 403.
export function applyRoleVisibility() {
  const admin = isAdmin();
  document.querySelectorAll("[data-requires-role=admin]").forEach((el) => {
    el.hidden = !admin;
  });
  if (!canMutate()) {
    document.body.dataset.readOnly = "true";
    // Дві глобальні дії шела, які read-only роль гарантовано не виконає.
    // Не ховаємо, а вимикаємо з поясненням: зникла кнопка читається як
    // «зламалось», вимкнена з підказкою — як «це не твоя дія».
    ["#sync-all-button", "#audit-all-button"].forEach((selector) => {
      const button = document.querySelector(selector);
      if (!button) return;
      button.disabled = true;
      button.title = "Read-only access — ask an administrator to run collection.";
    });
  }
}
