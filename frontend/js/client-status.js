// task-34 — комерційний статус клієнта (`active` / `paused`).
//
// Один модуль на два входи (картка дашборду і шапка сторінки клієнта), бо це
// одна й та сама дія: показати поточний стан і дати його змінити. Бейдж —
// справжня `<button>`, а не клікабельний `<span>`: він і показує статус, і є
// його контролом (AGENTS §7 — ніяких клікабельних div/span).
import { setClientStatus } from "./api.js?v=20260811-monitor1";
import { escapeHtml, formatDate, icon } from "./formatters.js?v=20260811-monitor1";

export const CLIENT_STATUS_META = {
  // Не колір-онлі: у кожного стану власна іконка + слово (AGENTS §7).
  active: { label: "Active", icon: "circle-dot" },
  paused: { label: "Paused", icon: "player-pause" },
};

export function clientStatusOf(client) {
  return client?.status === "paused" ? "paused" : "active";
}

export function isPaused(client) {
  return clientStatusOf(client) === "paused";
}

// «Paused · since 12 Jul» — дата приходить із `status_changed_at` і є
// справжньою подією. Її немає (статус ніколи не міняли) → пишемо просто
// «Active», а не вигадуємо дату створення клієнта.
export function clientStatusBadge(client) {
  const status = clientStatusOf(client);
  const meta = CLIENT_STATUS_META[status];
  const since = formatDate(client.status_changed_at);
  const sinceText = status === "paused" && since ? `since ${since}` : "";
  const description = [meta.label, sinceText].filter(Boolean).join(" · ");
  return `
    <button class="client-status client-status--${status}" type="button" data-action="client-status"
      aria-label="${escapeHtml(`Client status: ${description}. Change status.`)}"
      title="${escapeHtml(`${description} — change status`)}">
      ${icon(meta.icon, "icon icon--tiny")}
      <span class="client-status__label">${escapeHtml(meta.label)}</span>
      ${sinceText ? `<small class="client-status__since">${escapeHtml(sinceText)}</small>` : ""}
    </button>`;
}

// Нотатка оператора («payment stopped 01.07») — окремим рядком під бейджем,
// а не в `title`: інформація, потрібна щоб не робити зайву роботу, не має
// ховатись за hover (AGENTS §3).
export function clientStatusNote(client) {
  if (!isPaused(client) || !client.status_note) return "";
  return `<p class="client-status-note">${icon("info-circle", "icon icon--tiny")}<span>${escapeHtml(client.status_note)}</span></p>`;
}

const dialog = document.querySelector("#client-status-dialog");
const form = document.querySelector("#client-status-form");
const lead = document.querySelector("#client-status-lead");
const errorLine = document.querySelector("#client-status-error");
const saveButton = document.querySelector("#client-status-save");

let context = null;

export function openClientStatusDialog({ client, trigger, onSaved }) {
  context = { client, trigger, onSaved };
  const status = clientStatusOf(client);
  lead.textContent = `${client.name} — ${client.domain}. Pausing never deletes or hides collected data.`;
  errorLine.textContent = "";
  form.elements["client-status"].value = status;
  form.elements.note.value = client.status_note || "";
  saveButton.disabled = false;
  saveButton.textContent = "Save";
  dialog.showModal();
  // Фокус на поточному стані, а не на першому радіо: діалог відкривають, щоб
  // змінити стан, і починати треба з того, що є зараз.
  form.querySelector(`input[name="client-status"][value="${status}"]`)?.focus();
}

form?.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!context) return;
  const status = form.elements["client-status"].value;
  const note = form.elements.note.value.trim();
  saveButton.disabled = true;
  saveButton.textContent = "Saving…";
  try {
    const updated = await setClientStatus(context.client.client_id || context.client.id, status, note);
    const { onSaved } = context;
    dialog.close();
    onSaved?.(updated);
  } catch (error) {
    errorLine.textContent = `Could not save the status: ${error.message}`;
    saveButton.disabled = false;
    saveButton.textContent = "Save";
  }
});

dialog?.querySelectorAll("[data-close]").forEach((button) =>
  button.addEventListener("click", () => dialog.close()),
);

// Фокус повертається на бейдж, який відкрив діалог (AGENTS §7). Кнопку могло
// вже змести перерендером сітки — тоді `focus()` просто нікуди не веде, і це
// краще, ніж тримати посилання на мертвий вузол.
dialog?.addEventListener("close", () => {
  context?.trigger?.focus?.();
  context = null;
});
