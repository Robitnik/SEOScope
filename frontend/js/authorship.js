// Хто створив звіт/пакет (task-93) — admin-only, скрізь однаковий формат.
//
// Автор ніколи не блокує рендер списку: список звітів/пакетів малюється
// одразу з `[data-author-id]`-плейсхолдерами (порожня клітинка для
// не-адміна — цей модуль узагалі не викликає API), а `loadAuthors()`
// довантажує справжніх авторів одним батч-запитом і патчить DOM. Помилка
// цього запиту (роль змінили посеред вкладки, мережа) лишає плейсхолдери
// як є — колонка автора другорядна, список звітів важливіший (план §2 D9).
import { getAuthorship, isAbortError } from "./api.js?v=20260914-t93";
import { escapeHtml } from "./formatters.js?v=20260914-t93";
import { isAdmin } from "./session.js?v=20260906-t72";

const MAX_IDS_PER_CALL = 200;

// task-93 план §2 D9 — той самий словник, що `AuditEvent.actor_kind` +
// `"unknown"`. Кожна функція отримує вже резолвлений `author` (`{kind,
// user}` з `GET /api/v1/authorship`).
function authorLabel(author) {
  const user = author.user;
  switch (author.kind) {
    case "user":
      return user ? nameWithStatus(user) : "Unknown";
    case "agent":
      return user ? `${nameWithStatus(user)} · API token` : "Dev agent token";
    case "local":
      return "Local operator";
    case "cli":
      return "CLI";
    case "system":
      return "System";
    default:
      return "Unknown";
  }
}

function nameWithStatus(user) {
  return user.is_active ? user.name : `${user.name} (inactive)`;
}

function authorTitle(author) {
  if (author.user?.email) return author.user.email;
  if (author.kind === "local") return "Signed in via localhost bypass";
  if (author.kind === "unknown") return "Created before author tracking";
  return "";
}

/** Порожня клітинка/чип, поки батч-запит ще не повернувся — сама розмітка
 * DOM-точки, яку `patchAuthorCells` знайде за `data-author-id`. */
export function authorPlaceholder(resourceId) {
  return `<span class="author-chip author-chip--loading" data-author-id="${escapeHtml(resourceId)}">···</span>`;
}

/** Готовий чип для вже резолвленого автора — використовується і плейсхолдером
 * заміняється, і напряму (де автор уже відомий синхронно, напр. в тестах). */
export function renderAuthorChip(author) {
  if (!author) {
    return `<span class="author-chip author-chip--unknown">Unknown</span>`;
  }
  const label = escapeHtml(authorLabel(author));
  const title = authorTitle(author);
  const titleAttr = title ? ` title="${escapeHtml(title)}"` : "";
  const toneClass = author.kind === "unknown" || !author.kind ? " author-chip--unknown" : "";
  if (author.user) {
    return (
      `<a class="author-chip${toneClass}" href="#/admin/audit?actor_id=${encodeURIComponent(author.user.id)}"` +
      `${titleAttr} translate="no">${label}</a>`
    );
  }
  return `<span class="author-chip${toneClass}"${titleAttr} translate="no">${label}</span>`;
}

/** Батчить `ids` (≤200 за виклик), тягне авторів одним+N запитом, повертає
 * `Map<resource_id, author>`. Не-адмін і порожній список — миттєво `Map()`
 * без мережевого виклику. Мережева/рольова помилка — теж `Map()`: колонка
 * автора другорядна відносно самого списку (план §2 D9). */
export async function loadAuthors(resourceType, ids) {
  const unique = [...new Set(ids)].filter(Boolean);
  if (!isAdmin() || !unique.length) return new Map();

  const chunks = [];
  for (let i = 0; i < unique.length; i += MAX_IDS_PER_CALL) {
    chunks.push(unique.slice(i, i + MAX_IDS_PER_CALL));
  }
  try {
    const pages = await Promise.all(chunks.map((chunk) => getAuthorship(resourceType, chunk)));
    const authors = new Map();
    for (const page of pages) {
      for (const item of page.items || []) authors.set(item.resource_id, item.author);
    }
    return authors;
  } catch (error) {
    if (!isAbortError(error)) {
      // eslint-disable-next-line no-console
      console.warn(`authorship: could not load ${resourceType} authors`, error);
    }
    return new Map();
  }
}

/** Знаходить кожен `[data-author-id]` під `root` і замінює його на готовий
 * чип за `authors` (`Map` від `loadAuthors`). Елемент без відповідного id в
 * мапі (запит не повернувся, або цей ресурс уже видалили) лишається
 * плейсхолдером-заглушкою — ніколи не зникає й не ламає розмітку рядка. */
export function patchAuthorCells(root, authors) {
  if (!root || !authors.size) return;
  root.querySelectorAll("[data-author-id]").forEach((element) => {
    const id = element.getAttribute("data-author-id");
    if (!authors.has(id)) return;
    element.outerHTML = renderAuthorChip(authors.get(id));
  });
}
