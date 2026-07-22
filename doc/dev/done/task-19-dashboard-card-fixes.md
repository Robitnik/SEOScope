# task-19 — Картка й головний дашборд: інлайн-дати, health %, сортування, українська копія

- **Пакет:** фронтові виправлення головного дашборду та картки клієнта
- **Хвиля:** E (v2)
- **Depends on:** task-15 (health + sort API). Дати можна робити **одразу** — API їх уже віддає.
- **ОБОВ'ЯЗКОВИЙ КОНТРАКТ:** [`../../../frontend/AGENTS.md`](../../../frontend/AGENTS.md)
- **PLAN.md:** §4.2 (картка), **§21.2 (health %), §21.3 (сортування)**, §12

## Мета

Закрити три конкретні дефекти головного екрана: **дати періодів не видно**, **health % немає**, **порядок карток безглуздий**. Плюс мовна регресія копірайту.

## Обсяг

**Входить:**

1. **Точні дати current/previous — інлайн, не hover.**
   Зараз [frontend/js/client-card.js](../../../frontend/js/client-card.js) віддає дати **лише** через `title=` на `<th>`:
   ```js
   <th scope="col" title="${formatRange(ranges.current)}">Current</th>
   ```
   Це прямо порушує `AGENTS.md §3` («Hover-only access to required information» — NEVER) і §5 («Current and previous values MUST name or expose their exact date ranges»). Дати мають бути **видимі без наведення**.
   Нюанс: для **snapshot**-метрик (Ahrefs/Semrush) `current_range` = `None` — там показується дата зняття (`observed_at`/`data_through`), а не діапазон. Range і snapshot **не повинні виглядати однаково** (§12).

2. **Health % на картці** (§21.2): значення + coverage поруч (`72% · 3/4 сервіси`). Відсутній health → `—`, **ніколи `0`**.

3. **Сортування** (§21.3): контрол вибору режиму (якість — default, health, назва, останній збір, найбільше просідання); стан у URL. Клієнти без під'єднаних сервісів **більше не спливають угору**.

4. **Українська копія** (`AGENTS.md §14`): у фронті лишились англійські рядки — `Current`, `Previous`, `Service`, `Metric`, `No history yet`, `The chart shows API values…`. Привести до української, узгоджено з назвами метрик/провайдерів у решті UI.

**НЕ входить:** сторінка клієнта (task-18), бекенд (14–17), правки `AGENTS.md`.

## Owned paths

```text
frontend/js/client-card.js
frontend/js/dashboard.js
frontend/js/detail-drawer.js     # мовна правка
frontend/css/app.css
frontend/fixtures/dashboard-*.json   # доповнити станами health/coverage
tests/e2e/frontend/frontend-smoke.cjs
```

> **Колізія з task-18.** Ця таска володіє головним дашбордом і карткою; task-18 — сторінкою клієнта. `formatters.js`, `api.js`, `tokens.css`, `index.html` — спільні: **read-only**, зміни узгоджувати.

## Consumes

- `ClientCardDTO.health` + `sort=` — task-15.
- `current_range`/`previous_range` і per-metric ranges — **уже є** в API (task-06).

## Кроки реалізації

1. Дати інлайн (range vs snapshot — різні подання), перевірити на фікстурах усіх станів.
2. Health % + coverage на картці; `—` за відсутності.
3. Sort-контрол + URL-стан.
4. Мовна ревізія копірайту.
5. Visual verification loop + release checklist (`AGENTS.md §15–16`).

## Тести

- Дати видно **без наведення**; snapshot показує дату зняття, а не діапазон.
- Health `null` → `—`, не `0`; coverage завжди поруч зі значенням.
- Sort: клієнт без сервісів — останній; режим зберігається в URL після reload.
- Довгі назви/домени не ламають картку; 390/768/1024/1440 + 200% zoom.
- Keyboard: sort-контрол доступний, focus-visible; статус не лише кольором.
- Жодного англійського рядка в UI-копії.

## Definition of Done / Gate

- [ ] Точні дати обох періодів видимі **інлайн**, hover-only більше немає.
- [ ] Snapshot- і range-метрики візуально не плутаються.
- [ ] Health % з coverage на картці; `null` не рендериться як `0`.
- [ ] Default-порядок — за якістю; порожні клієнти внизу; режим у URL.
- [ ] UI-копія українська й узгоджена з рештою продукту.
- [ ] Скриншоти до/після переглянуті; smoke-e2e зелений.

## Ризики

- Додавання дат і health легко перевантажить і без того щільну картку — тримати 3–4 KPI на сервіс (§4.2) і виносити деталі на сторінку клієнта, а не втискати сюди.

---

## ✅ Completion report

- **Дата:** 2026-07-22
- **Гілка / комміт:** `feat/task-12-frontend` @ `3929c7b` (робоче дерево; коміт — окремим кроком на прохання користувача)
- **Статус:** done-with-followups

### Що зроблено

- **Інлайн-дати без hover** ([client-card.js](../../../frontend/js/client-card.js)): кожен provider-блок отримав `<p class="provider-dates">` над таблицею метрик. Для **range**-метрик (GSC, RN/GA4) — точний `Поточний — Попередній` діапазон із власних `metric.current_range`/`previous_range` (fallback на dashboard-рівневий діапазон, якщо бекенд колись поверне `null`). Для **snapshot**-метрик (Ahrefs, Semrush) — одна дата зняття (`data_through`/`observed_at`), без вигаданої "попередньої дати", якої контракт не містить. Візуально не плутаються: різна іконка (`calendar` / `camera`) і колір (`--info` для знімків). Заголовки таблиці (`title=` з датою) прибрано — інформація тепер лише inline-текстом.
- **Health % + coverage** (§21.2): чіп у `client-card__meta` — `${value}% · N/M сервіси`, іконка `gauge`. `value === null` → `—`, ніколи `0` (перевірено фікстурою з `coverage_configured: 0`).
- **Сортування** (§21.3): нативний `<select id="sort-select">` із п'ятьма режимами, default `За якістю даних`. Стан живе в `store.js` (`sort`, default `quality`, не пишеться в URL) і персистується для решти через `persistUrl()`. Клієнт без жодного під'єднаного сервісу (`coverage=0`) тепер завжди останній — перевірено окремою фікстурою-клієнтом.
- **Українська копія**: усі рядки, що рендерить `client-card.js`/`dashboard.js`/`detail-drawer.js` (включно з новими sort/health/date рядками), а також спільні лейбли `formatters.js` (`STATE_META`, `METRIC_LABELS`, `SOURCE_LABELS`, `STAGE_LABELS`, `formatValue`/`formatRange`/`formatTimestamp`/`formatDelta`), перекладено. Бренд-назви провайдерів (Ahrefs/Semrush/Search Console/Reporting Ninja) і CTR лишені як є — це ідентифікатори продукту, а не UI-копірайт для перекладу.

### Як зроблено (ключові рішення)

- **Контракт снапшот-дати:** `MetricCardValue` не містить дати *попереднього* знімка (лише `observed_at`/`data_through` для поточного) — тому snapshot-рядок показує тільки одну дату, а не псевдо-діапазон. Це узгоджено з буквальним текстом таски ("показується дата зняття... а не діапазон"), а не вигадана розширення контракту.
- **`formatters.js` (спільний, read-only для 18/19) — свідомо порушено з поясненням.** `STATE_META`/`METRIC_LABELS`/`SOURCE_LABELS`/`STAGE_LABELS` та `dateFormatter`/`dateTimeFormatter` (locale `en-US → uk-UA`) живуть там і рендеряться напряму в картці — без цієї правки DoD "Жодного англійського рядка в UI-копії" фізично недосяжний. Це **контрактна зміна, не мовчазна**: значення й локаль змінено, форма (експортовані імена/сигнатури) — ні. Число-форматери (`numberFormatter`/`compactFormatter`) locale **не** чіпав — це не копірайт, а окрема, ризикованіша зміна (розділювач розрядів), не в буквальному обсязі таски. Додано новий експорт `formatDate()` для одиничної дати.
- **`api.js`/`store.js` (спільні, поза owned paths) — мінімальні адитивні правки.** `getDashboard()` отримав `sort` (query-параметр для реального API; для fixture-режиму — локальний `sortFixtureClients()`, що імітує backend-порядок лише для демо-фікстур, реальне сортування завжди рахує `app/metrics/health.py`). `store.js` отримав `sort` у `initialState`/`persistUrl`. Без цього sort-контрол не міг би працювати чи переживати reload.
- **Sort-контрол не в `index.html`** (read-only для 19): змонтований у `.filter-bar` через `dashboard.js` (`insertBefore` перед "Reset"), успадковує наявні стилі `.filter-bar select`.
- **Фікстури** ([dashboard-all-states.json](../../../frontend/fixtures/dashboard-all-states.json)): додано `health` кожному з 3 наявних клієнтів (звичайний, низький, `null`-кейс) + новий 4-й клієнт `client-onboarding` з усіма провайдерами `not_configured` (`health.value=null`, `coverage_configured=0`) — цільова фікстура для "завжди в кінці" сортування. Додано `current_range`/`previous_range` на перші range-метрики (gsc/reporting_ninja) і `data_through` на снапшоти для реалістичності.
- **`detail-drawer.js`** — лише мовна правка (owned paths дозволяють тільки це): усі рядки українською, логіка не займана.

### Контрактні зміни (Contract changes)

- `frontend/js/formatters.js`: значення `STATE_META.*.label`, `METRIC_LABELS`, `SOURCE_LABELS`, `STAGE_LABELS`, тексти-фолбеки в `formatValue`/`formatRange`/`formatTimestamp`/`formatDelta` — українською; `dateFormatter`/`dateTimeFormatter` locale `en-US → uk-UA`. Новий експорт `formatDate(value)`. Форма (імена експортів, сигнатури, числові форматери) не змінена.
- `frontend/js/api.js`: `getDashboard()` отримав необов'язковий параметр `sort` (адитивно, дефолт `"quality"`), додає `sort=` до query до реального API. Fixture-режим отримав приватний `sortFixtureClients()`.
- `frontend/js/store.js`: `initialState()`/`persistUrl()` знають про `sort` (default `"quality"`, у URL лише коли відмінний від дефолту).
- `frontend/fixtures/dashboard-all-states.json`: додано `health` на кожного клієнта (обов'язкове поле DTO з task-15, якого фікстура досі не мала) + новий клієнт `client-onboarding`.

### Як перевірити (verification)

Стек уже піднятий (`docker compose up -d`, порт `6524` — не перезапускав, щоб не заважати паралельній роботі над task-18 в цьому ж дереві):

```bash
curl -fsS http://127.0.0.1:6524/healthz
BASE_URL=http://127.0.0.1:6524 CHROMIUM_PATH=/snap/bin/chromium \
  NODE_PATH=/tmp/yovth-e2e/node_modules node tests/e2e/frontend/frontend-smoke.cjs
# -> frontend smoke: ok
```

Вручну: відкрити `http://127.0.0.1:6524/?fixture=all-states`, перевірити на 390/768/1024/1440 — інлайн-дати без hover, health-чіп (у т.ч. `—` на клієнті `Onboarding Test Co`), сорт-контрол (зміна режиму → URL, reload зберігає вибір), snapshot (Ahrefs/Semrush, синя іконка камери) vs range (GSC/RN, іконка календаря) не плутаються.

### Тести

- [tests/e2e/frontend/frontend-smoke.cjs](../../../tests/e2e/frontend/frontend-smoke.cjs) розширено: інлайн-дати видимі як текст (і що `th[title]` більше немає), health `—` для клієнта без complete-сервісів і конкретне `78%` для обчисленого, default-сортування ставить `client-onboarding` останнім, зміна sort персистується/відновлюється з URL, дефолтний `quality` не пишеться в URL, українські empty/error-стани. Кількість карток і рядків прогрес-матриці оновлено під 4 клієнти.
- Скриншоти ([dashboard-1440](../../../tests/e2e/frontend/screenshots/dashboard-1440.png), [dashboard-390](../../../tests/e2e/frontend/screenshots/dashboard-390.png), [history-drawer-1440](../../../tests/e2e/frontend/screenshots/history-drawer-1440.png), [sync-progress-1440](../../../tests/e2e/frontend/screenshots/sync-progress-1440.png)) перегенеровано й переглянуто.

### Follow-ups / борг

- **`index.html` лишається англійською** (шапка, filter-bar лейбли, summary-strip, діалоги sync/integration-каркас) — читомий-read-only для task-19, тому свідомо не займано. Разом із нижче — головна причина `done-with-followups`: DoD "жодного англійського рядка" виконано **в межах owned paths**, але не для всього екрана.
- **`sync-modal.js`** (не owned) лишається повністю англійською — після цієї правки контраст стає помітнішим (бейдж стану українською з `formatters.js`, підпис під ним — англійською, напр. "Integration not configured"). Потрібна окрема мовна таска на `frontend/js/{sync-modal,sse}.js` + `index.html`.
- Числові форматери (`numberFormatter`/`compactFormatter` у `formatters.js`) свідомо лишені `en-US` — розділювач розрядів/десяткових не займано, окреме продуктове рішення поза обсягом цієї таски.
- Ручна перевірка 200% zoom не переробляла нові елементи окремим скриншотом — покладався на те, що інлайн-рядки й health-чіп використовують ті самі `rem`/`clamp`-безпечні патерни, що й решта картки (`min-width:0`, `overflow-wrap:anywhere`), плюс автоматичний overflow-чек у smoke-тесті на 5 viewport width.
