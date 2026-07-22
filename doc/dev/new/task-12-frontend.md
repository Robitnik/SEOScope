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
- Усі required states з `AGENTS.md §6` (loading/empty/sparse/dense/stale/partial/auth/error/active/completed/cancelled/offline-SSE).

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
4. Sync modal + live progress через `sse.js` (закриття modal НЕ зупиняє job; reconnect відновлює з event history) (§4.3).
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
- [ ] Головна задача очевидна за 5 секунд; мова UI — українська.
- [ ] Немає жодного anti-slop патерну з `AGENTS.md §3`.
- [ ] Усі required states (`AGENTS.md §6`) реалізовані; `null` не як `0`; статус не лише кольором.
- [ ] Live progress переживає reload/reconnect; закриття modal не зупиняє job.
- [ ] Keyboard/focus/dialog-focus-return/SR-names працюють; перевірено на 390–ultrawide + 200% zoom.
- [ ] Немає runtime-CDN; шрифти/іконки/vendor pinned і self-hosted.
- [ ] Скриншоти до/після переглянуті; e2e ключового flow зелений.
- [ ] Бізнес-семантика не рахується в DOM (береться з API).

## Ризики
- AI-slop патерни (`AGENTS.md §3`) — головний ризик якості; проходити критику скриншотів кожної ітерації.
- Layout shift від шрифтів/лого/skeleton — усунути (explicit sizes, mirror-skeletons).
- Stale async update при швидкому перемиканні клієнта/періоду — abort obsolete fetch (`api.js`).
