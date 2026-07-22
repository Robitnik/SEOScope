# task-12 — Frontend (vanilla-JS dashboard)

- **Пакет:** увесь `frontend/` — brand shell, dashboard, картки клієнтів, sync modal + live progress, detail drawer
- **Хвиля:** D (стартує з першого дня на фікстурах; синхронізується по OpenAPI/SSE)
- **Depends on:** task-06 (OpenAPI — форма даних), task-05 (SSE — live progress). До їх готовності — працювати на фікстурах.
- **ОБОВ'ЯЗКОВИЙ КОНТРАКТ:** [`../../../frontend/AGENTS.md`](../../../frontend/AGENTS.md) — це нормативний документ для **кожного** файлу під `frontend/`. Читати повністю перед кодом; він головніший за загальні звички.
- **PLAN.md:** §4 (UX/UI — повністю), §4.5 (frontend stack + модулі), §10 (форма API), Етапи 1/3/7 (frontend-частини)

## Мета

Побудувати самобутній, production-grade внутрішній інструмент «The Yovth operations control room»: щільна інформація, темна індустріальна точність, один контрольований помаранчевий сигнал. Vanilla JS + CSS + ES-модулі (без фреймворків), Bootstrap лише як layout/a11y-фундація, кастомна тема The Yovth. **Уся робота підпорядкована `frontend/AGENTS.md`.**

## Обсяг

**Входить (модулі PLAN §4.5):**
- `frontend/index.html` — семантичний shell, skip-link, заголовки, static-mount сумісний із task-01.
- `frontend/css/` — токени бренду (canvas `#0E0E0E`, surface `#1E1E1E`, accent `#FF4A17`, League Spartan 900 uppercase, Archivo 400/700, tabular-nums), responsive grid.
- `frontend/js/`: `api.js` (fetch, abort obsolete), `store.js` (стан + URL-persist), `dashboard.js`, `client-card.js`, `sync-modal.js`, `sse.js` (EventSource + reconnect + `Last-Event-ID`), `formatters.js` (`Intl.NumberFormat`/`DateTimeFormat`).
- `frontend/assets/` — self-hosted шрифти, офіційний логотип (`logo-3.svg`), Tabler Icons як локальний SVG-sprite; `frontend/vendor/` — pinned Bootstrap 5.3.x, Chart.js (лише detail history). Без runtime CDN.
- Екрани/стани: головний dashboard (§4.1), картка клієнта (§4.2), sync modal + live progress (§4.3), loading/empty/error (§4.4), detail drawer + history chart (Етап 7).
- Усі required states з `AGENTS.md §6` (loading/empty/sparse/dense/stale/partial/auth/error/active/completed/cancelled/offline-SSE) + `not_configured` (сервіс не заведено для клієнта → CTA додати інтеграцію, не нуль/помилка).

**НЕ входить:** бекенд-логіка, обрахунок бізнес-семантики в DOM (API вже віддає delta/quality/ranges — task-06), правки `frontend/AGENTS.md` (read-only контракт).

## Owned paths
```
frontend/index.html
frontend/css/**
frontend/js/**
frontend/assets/**
frontend/vendor/**
frontend/fixtures/**          # фікстури для fixture-driven dev усіх станів
# frontend/AGENTS.md — READ-ONLY (не редагувати)
tests/e2e/frontend/**         # координувати з task-13 harness
```

## Consumes
- OpenAPI/DTO — task-06 (форма dashboard/clients/jobs). До готовності — локальні фікстури, що дзеркалять DTO task-02.
- SSE event stream + стадії/статуси — task-05.

## Provides
- Робочий UI усіх станів; screenshot-набір ключових станів для регресії (task-13).

## Кроки реалізації (за `AGENTS.md §1` — «Read before designing»)
1. Прочитати `AGENTS.md` + PLAN §4; визначити єдину задачу оператора у кожному екрані.
2. Brand shell + токени + шрифти/лого/іконки (self-hosted, pinned, vendored).
3. Fixture-driven картка клієнта (усі стани), потім dashboard-грід (§4.1/4.2).
4. Sync modal + live progress через `sse.js`: **матриця клієнт × сервіс** — паралельні доріжки сервісів, послідовні клієнти всередині сервісу (PLAN §9.1); `not_configured`-клітинки позначені окремо (не помилка); закриття modal НЕ зупиняє job; reconnect відновлює з event history; запуск sync одного клієнта і batch/усіх (§4.3).
5. loading/empty/error states + detail drawer + Chart.js history (§4.4, Етап 7).
6. Синхронізувати з реальним OpenAPI/SSE, коли task-06/05 готові.
7. Visual verification loop + release checklist (`AGENTS.md §15–16`) — обов'язково зі скриншотами до/після.

## Тести (PLAN §15 Frontend/E2E; `AGENTS.md §15–16`)
- skeleton → data → refresh; фільтри й перемикання періоду.
- sync modal: reconnect, close/reopen, retry failed.
- keyboard-only, focus trap, escape, screen-reader labels.
- responsive 390/768/1024/1440/ultra-wide + 200% zoom; довгі імена клієнтів.
- reduced motion + contrast; `null`≠`0`; статус не лише кольором.
- screenshot regression ключових станів (спільний harness — task-13).

## Definition of Done / Gate (додатково до `AGENTS.md §16` release checklist)
- [x] Головна задача очевидна за 5 секунд; мова UI — українська.
- [x] Немає жодного anti-slop патерну з `AGENTS.md §3`.
- [x] Усі required states (`AGENTS.md §6`) реалізовані; `null` не як `0`; статус не лише кольором.
- [x] Live progress переживає reload/reconnect; закриття modal не зупиняє job.
- [x] Прогрес показує матрицю клієнт × сервіс (паралельні доріжки); можна запустити sync одного клієнта і batch/усіх; `not_configured` — окремий стан (CTA, не нуль/помилка).
- [x] Keyboard/focus/dialog-focus-return/SR-names працюють; перевірено на 390–ultrawide + 200% zoom.
- [x] Немає runtime-CDN; шрифти/іконки/vendor pinned і self-hosted.
- [x] Скриншоти до/після переглянуті; e2e ключового flow зелений.
- [x] Бізнес-семантика не рахується в DOM (береться з API).

## Ризики
- AI-slop патерни (`AGENTS.md §3`) — головний ризик якості; проходити критику скриншотів кожної ітерації.
- Layout shift від шрифтів/лого/skeleton — усунути (explicit sizes, mirror-skeletons).
- Stale async update при швидкому перемиканні клієнта/періоду — abort obsolete fetch (`api.js`).

---

## ✅ Completion report

- **Дата:** 2026-07-22
- **Гілка:** `feat/task-12-frontend`
- **Статус:** done

### Що зроблено

- Побудовано production-ready vanilla-JS dashboard українською: brand shell The Yovth, точні закриті періоди, summary, URL-персистентні фільтри, адаптивна сітка та щільні картки клієнтів.
- Картки відображають максимум 4 canonical KPI на сервіс, поточне/попереднє значення, API-дельту, quality/state і source mode. `null` показується як «Немає даних», реальний `0` лишається нулем.
- Реалізовано всі degraded states: stale, partial, truncated, unavailable, failed, auth_required і not_configured з робочим CTA налаштування інтеграції без вводу секретів у браузері.
- Реалізовано batch/one-client/one-provider sync, матрицю `client × provider`, background job dock, cancel, retry failed only, SSE reconnect з `last_event_id` та відновлення job після reload через `localStorage`.
- Реалізовано side drawer історії з Chart.js і збереженням пропусків у рядах, а також loading skeleton, empty/error стани й fixture-режими для розробки.
- Bootstrap 5.3.8, Chart.js 4.5.1, Tabler Icons 3.45.0, Simple Icons 16.27.0, шрифти та logo vendored локально; runtime CDN-запитів немає.

### Контрактні зміни

- Backend/OpenAPI/ORM не змінювались. Fixture payload-и валідовані безпосередньо через `DashboardResponse` і `ClientMetricsHistoryDTO`.
- UI локалізує canonical metric keys українською, але не обчислює delta/quality/period semantics — ці значення читаються з API DTO.

### Verification

```bash
for file in frontend/js/*.js tests/e2e/frontend/frontend-smoke.cjs; do node --check "$file"; done
NODE_PATH=/tmp/yovth-frontend-e2e/node_modules \
  CHROMIUM_PATH=/snap/bin/chromium \
  BASE_URL=http://127.0.0.1:4173 \
  node tests/e2e/frontend/frontend-smoke.cjs
# frontend smoke: ok

docker compose run --rm --no-deps api python -c \
  "... DashboardResponse.model_validate(...); ClientMetricsHistoryDTO.model_validate(...)"
# frontend DTO fixtures: ok
```

Browser smoke перевіряє 390/768/1024/1440/1920 px, reduced motion, відсутність horizontal overflow, search, `null ≠ 0`, точні періоди, sync matrix, close/reopen без cancel, focus return, history gap, empty/error та console errors. Переглянуті скриншоти dashboard desktop/mobile, live progress і history drawer лежать у `tests/e2e/frontend/screenshots/`.
