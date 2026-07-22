import { cancelSyncJob, createSyncJob, retrySyncJob } from "./api.js";
import { PROVIDERS, PROVIDER_ORDER, STATE_META, STAGE_LABELS, icon, isTerminal, percent, stateClass } from "./formatters.js";
import { loadActiveJob, saveActiveJob } from "./store.js";
import { watchJob } from "./sse.js";

export function createSyncModal({ getDashboard, onFinished, onToast }) {
  const dialog = document.querySelector("#sync-dialog");
  const title = document.querySelector("#sync-title");
  const subtitle = document.querySelector("#sync-subtitle");
  const body = document.querySelector("#sync-body");
  const footer = document.querySelector("#sync-footer");
  const dock = document.querySelector("#job-dock");
  const dockTitle = document.querySelector("#job-dock-title");
  const dockCopy = document.querySelector("#job-dock-copy");
  const dockPercent = document.querySelector("#job-dock-percent");
  const connection = document.querySelector("#connection-status");
  let opener = null;
  let stopWatching = null;
  let active = loadActiveJob();
  let currentJob = null;

  dialog.addEventListener("close", () => opener?.focus());
  document.querySelector("#job-dock-button").addEventListener("click", (event) => {
    opener = event.currentTarget;
    if (!dialog.open) dialog.showModal();
    if (currentJob) renderProgress(currentJob);
  });

  function openSelection({ clientId = null, provider = null, trigger } = {}) {
    opener = trigger || document.activeElement;
    const dashboard = getDashboard();
    if (!dashboard) return;
    title.textContent = clientId ? "Оновити клієнта" : "Оновити метрики";
    subtitle.textContent = "Збір працює у фоні: вікно можна закрити без зупинки.";
    const selectedClients = clientId ? [clientId] : dashboard.clients.map((client) => client.client_id);
    const selectedProviders = provider ? [provider] : PROVIDER_ORDER;
    body.innerHTML = `
      <div class="selection-grid">
        <fieldset class="selection-group"><legend>Клієнти</legend><div class="check-list">
          ${dashboard.clients.map((client) => `<label class="check-row"><input type="checkbox" name="sync-client" value="${client.client_id}" ${selectedClients.includes(client.client_id) ? "checked" : ""}><span>${client.name}<small>${client.domain}</small></span></label>`).join("")}
        </div></fieldset>
        <fieldset class="selection-group"><legend>Джерела</legend><div class="check-list">
          ${PROVIDER_ORDER.map((key) => `<label class="check-row"><input type="checkbox" name="sync-provider" value="${key}" ${selectedProviders.includes(key) ? "checked" : ""}><span>${PROVIDERS[key].label}</span></label>`).join("")}
        </div></fieldset>
      </div>`;
    footer.innerHTML = `<button class="button button--quiet" type="button" data-close>Скасувати</button><button class="button button--primary" type="button" data-start>${icon("player-play")}Запустити збір</button>`;
    footer.querySelector("[data-close]").addEventListener("click", () => dialog.close());
    footer.querySelector("[data-start]").addEventListener("click", startSelected);
    if (!dialog.open) dialog.showModal();
  }

  async function startSelected(event) {
    const clientIds = [...body.querySelectorAll('input[name="sync-client"]:checked')].map((input) => input.value);
    const providers = [...body.querySelectorAll('input[name="sync-provider"]:checked')].map((input) => input.value);
    if (!clientIds.length || !providers.length) {
      onToast("Оберіть хоча б одного клієнта й одне джерело.");
      return;
    }
    event.currentTarget.disabled = true;
    try {
      const dashboard = getDashboard();
      const accepted = await createSyncJob({
        client_ids: clientIds,
        providers,
        period_kind: dashboard.period_kind,
        comparison_mode: "closed",
        mode: "collect_missing",
        source_mode: "auto",
        idempotency_key: crypto.randomUUID?.() || `${Date.now()}-${Math.random()}`,
      }, dashboard);
      active = { id: accepted.batch_id || accepted.job_id, eventsUrl: accepted.events_url, lastEventId: "" };
      saveActiveJob(active);
      beginWatch();
      title.textContent = "Збір даних";
      subtitle.textContent = "Сервіси працюють паралельно, клієнти в кожному сервісі — послідовно.";
      renderProgress({ status: "queued", progress_percent: 0, cells: [] });
    } catch (error) {
      event.currentTarget.disabled = false;
      onToast(`Не вдалося запустити збір: ${error.message}`);
    }
  }

  function beginWatch() {
    stopWatching?.();
    if (!active?.id) return;
    dock.hidden = false;
    stopWatching = watchJob({
      id: active.id,
      eventsUrl: active.eventsUrl || `/api/v1/sync-jobs/${active.id}/events`,
      lastEventId: active.lastEventId,
      onProgress(job, lastEventId) {
        currentJob = job;
        active = { ...active, lastEventId };
        saveActiveJob(active);
        updateDock(job);
        if (dialog.open) renderProgress(job);
      },
      onConnection(state) {
        connection.dataset.state = state === "connected" ? "online" : "offline";
        connection.querySelector("span:last-child").textContent = state === "connected" ? "API онлайн" : "SSE перепідключається";
        if (dialog.open && state !== "connected") renderOfflineNotice();
      },
      async onTerminal(job) {
        currentJob = job;
        saveActiveJob(null);
        active = null;
        updateDock(job);
        if (dialog.open) renderProgress(job);
        await onFinished(job);
      },
      onError(error) { onToast(`Прогрес тимчасово недоступний: ${error.message}`); },
    });
  }

  function renderOfflineNotice() {
    if (body.querySelector(".job-alert")) return;
    body.insertAdjacentHTML("afterbegin", `<div class="job-alert"><strong>Зв’язок із потоком перервано.</strong> Статус відновиться автоматично; сам збір на сервері триває.</div>`);
  }

  function renderProgress(job) {
    const dashboard = getDashboard();
    const clients = new Map((dashboard?.clients || []).map((client) => [String(client.client_id), client.name]));
    const cells = job.cells || [];
    const clientIds = [...new Set(cells.map((cell) => String(cell.client_id)))];
    const providers = PROVIDER_ORDER.filter((provider) => cells.some((cell) => cell.provider === provider));
    const progressValue = percent(job.progress_percent);
    const state = STATE_META[job.status] || STATE_META.running;
    title.textContent = isTerminal(job.status) ? "Збір завершено" : "Збір даних";
    subtitle.textContent = state.label;
    body.innerHTML = `
      <div class="progress-overview">
        <span class="progress-number">${progressValue}%</span>
        <div><progress class="progress-track" max="100" value="${progressValue}" aria-label="Загальний прогрес: ${progressValue}%"></progress><div class="progress-copy">${progressSummary(cells)}</div></div>
      </div>
      ${clientIds.length ? `<div class="matrix-wrap"><table class="progress-matrix"><thead><tr><th scope="col">Клієнт</th>${providers.map((provider) => `<th scope="col">${PROVIDERS[provider].label}</th>`).join("")}</tr></thead><tbody>${clientIds.map((clientId) => `<tr><td>${clients.get(clientId) || clientId}</td>${providers.map((provider) => matrixCell(cells.find((cell) => String(cell.client_id) === clientId && cell.provider === provider))).join("")}</tr>`).join("")}</tbody></table></div>` : `<div class="state-panel"><p><strong>Готуємо чергу.</strong><br>Матриця з’явиться після відповіді сервера.</p></div>`}`;
    footer.innerHTML = footerActions(job);
    footer.querySelector("[data-close]")?.addEventListener("click", () => dialog.close());
    footer.querySelector("[data-cancel]")?.addEventListener("click", () => cancelJob());
    footer.querySelector("[data-retry]")?.addEventListener("click", () => retryFailed());
  }

  function matrixCell(cell) {
    if (!cell) return `<td><span class="matrix-cell state-not_configured"><strong>— Не обрано</strong></span></td>`;
    const meta = STATE_META[cell.status] || STATE_META.running;
    const value = percent(cell.progress_percent);
    return `<td><div class="matrix-cell ${stateClass(cell.status)}"><strong>${icon(meta.icon)}${meta.label}</strong><small>${cell.status === "not_configured" ? "Інтеграцію не додано" : (cell.error_code || STAGE_LABELS[cell.current_stage] || "Очікуємо статус")}</small><progress class="mini-progress" max="100" value="${value}" aria-label="${meta.label}: ${value}%"></progress></div></td>`;
  }

  function progressSummary(cells) {
    const done = cells.filter((cell) => ["succeeded", "failed", "not_configured", "cancelled", "skipped"].includes(cell.status)).length;
    return `${done} з ${cells.length} комірок завершено. Закриття вікна не зупинить процес.`;
  }

  function footerActions(job) {
    const failed = (job.cells || []).some((cell) => ["failed", "failed_interrupted"].includes(cell.status));
    if (isTerminal(job.status)) return `${failed ? `<button class="button button--quiet" type="button" data-retry>${icon("refresh")}Повторити помилки</button>` : ""}<button class="button button--primary" type="button" data-close>Готово</button>`;
    return `<button class="button button--quiet" type="button" data-close>Сховати</button><button class="button button--quiet" type="button" data-cancel>${icon("player-stop")}Зупинити</button>`;
  }

  async function cancelJob() {
    if (!active?.id) return;
    footer.querySelector("[data-cancel]").disabled = true;
    try { currentJob = await cancelSyncJob(active.id); renderProgress(currentJob); }
    catch (error) { onToast(`Не вдалося зупинити: ${error.message}`); }
  }

  async function retryFailed() {
    if (!currentJob) return;
    footer.querySelector("[data-retry]").disabled = true;
    try {
      const accepted = await retrySyncJob(currentJob.batch_id || currentJob.job_id || active?.id);
      active = { id: accepted.batch_id || accepted.job_id, eventsUrl: accepted.events_url, lastEventId: "" };
      saveActiveJob(active);
      beginWatch();
    } catch (error) { onToast(`Повтор не запущено: ${error.message}`); }
  }

  function updateDock(job) {
    const terminal = isTerminal(job.status);
    dock.hidden = false;
    dockTitle.textContent = terminal ? (STATE_META[job.status]?.label || "Збір завершено") : "Оновлення триває";
    dockCopy.textContent = terminal ? "Відкрити результат" : "Відкрити живий прогрес";
    dockPercent.textContent = `${percent(job.progress_percent)}%`;
  }

  if (active?.id) beginWatch();
  return { openSelection };
}
