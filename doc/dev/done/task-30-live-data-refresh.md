# task-30 — Оновлення даних з UI: rolling-window refresh + живий прогрес

- **Пакет:** кнопка **«Refresh data»** на `#/trends` (і на дашборді) яка ставить **rolling-window** джобу (`period_kind=custom`, вікно «останні N днів до вчора», `force_refresh`), чесний **freshness-індикатор** (`data through … · N days behind`), доступ до модалки прогресу **з будь-якої сторінки**, і «стабільний живий прогрес» (коалесований refetch, no-flicker rerender, ETA, анімації за `AGENTS §10`).
- **Хвиля:** L (розширення після MVP; усі попередні таски закриті)
- **Depends on:** — (адитивно розширює закриті зони task-03/05/06/09/12/28)
- **PLAN.md:** §3.1 (періоди), §9.1–9.3 (джоб-рушій, live progress, SSE), §10 (REST-конвенції), §12 (`None` ≠ `0`, quality/provisional), §21.6 (взірець сторінки)
- **frontend/AGENTS.md:** обов'язковий (особливо §5 «Data truth», §9 «Sync modal and progress UX», §10 «Motion», §14 «Copy rules»)
- **AISeoTools:** **змін не потребує** (обґрунтування — §1.6). Паралельні браузери — окрема [task-31](task-31-parallel-collection-slots.md).

---

## Мета (робота оператора, яку закриваємо)

Оператор відкриває `#/trends` 31.07 і бачить лінії, що обриваються **26.07**. Він не може ні зрозуміти чому, ні
щось із цим зробити з UI: єдиний спосіб дотягнути денну історію — CLI у контейнері
(`yovth-report backfill-daily-history`), про який сторінка чесно пише лише в порожньому стані
([trends-page.js:198](../../../frontend/js/trends-page.js#L198)).

Закриваємо: **побачити свіжість → натиснути одну кнопку → дивитись живий прогрес → графік сам перемалювався**.

---

## 1. Дослідження (факти, на яких стоїть план)

### 1.1 Дані стоять на 26.07 **не** через мертвий worker

Живий стан (запит до БД у контейнері, 2026-07-31):

| Факт | Значення |
|---|---|
| Останній батч | `2026-07-29 09:02:40` → `09:05:53`, статус `succeeded` |
| Параметри тих джоб | `period_kind=week`, `comparison_mode=closed`, `mode=collect_missing` |
| `gsc_daily` максимальний день | `2026-07-26` (384 різних днів у БД) |
| `ga4_daily` максимальний день | `2026-07-26` (938 днів) |
| Єдиний запуск із `period_kind=custom` | `force_refresh`, `custom_end = 2026-07-26` (той самий CLI-бекфіл, запущений 27.07) |

Тобто збір працює. Кінець рядів — це рівно **кінець закритого тижня 20–26.07**.

### 1.2 Чому звичайний sync **ніколи** не заповнить поточний тиждень

`resolve_periods(week, closed)` дає `current` = останній **закритий** тиждень, а денні датасети пишуться лише
за `context.current`/`context.previous` ([gsc.py:324](../../../app/integrations/gsc.py#L324),
[reporting_ninja.py:54](../../../app/integrations/reporting_ninja.py#L54)). Хоч скільки разів тисни «Update
metrics» 31.07 — рядків за 27–30.07 не з'явиться: їх ніхто не просив. Дотягнути «до вчора» може **тільки**
`period_kind=custom` з `custom_end = today-1` — це те, що робить CLI
([cli.py:164](../../../app/cli.py#L164)).

### 1.3 Freshness-гейт: без `force_refresh` оновлення дало б нуль рядків

`_run_cell_unguarded` пропускає клітинку (`skipped`), якщо є свіжий снапшот
([runner.py:392](../../../app/jobs/runner.py#L392)) — незалежно від того, який період попросили. Це вже
задокументовано в CLI ([cli.py:182](../../../app/cli.py#L182)) і **обов'язково** для нашої кнопки.

### 1.4 «Актуально» ≠ «сьогодні»: лаг джерел

`GSC_LAG_DAYS = 3`, `GA4_LAG_DAYS = 2` ([portfolio.py:30](../../../app/metrics/portfolio.py#L30)). Тобто
максимум досяжного 31.07 — дані **по 30.07**, де 28–30.07 ще **provisional** (джерело їх переписує). Кнопка не
має обіцяти «today»; UI має показати `data through` + позначку provisional (бакетна логіка вже є).

### 1.5 Повторні перетинні вікна безпечні

`AnalyticsDatasetRepository.daily_series` бере **найсвіжіший рядок на день** (сортування `created_at desc`,
`setdefault`) — [analytics_datasets.py:170](../../../app/db/repositories/analytics_datasets.py#L170). Отже
щоденний refresh з 30-денним вікном не дублює точки, а **ревізує** provisional-дні. Immutability снапшотів не
порушується (нові снапшоти, старі лишаються).

### 1.6 Для трендів браузер **не потрібен** узагалі

`gsc` і `reporting_ninja` — це Reporting Ninja API ([gsc.py:1](../../../app/integrations/gsc.py#L1) «Uses
Reporting Ninja exclusively»), жодного вікна Chrome. Тому «оновити тренди» — це чистий API-збір, який можна
робити без теплого Chrome і без ризику зачепити логін. «3 паралельні браузери» стосується `ahrefs`/`semrush` і
живе в [task-31](task-31-parallel-collection-slots.md).

### 1.7 Модалка прогресу недоступна з `#/trends`

`createSyncModal(...)` викликається у module-scope `dashboard.js`
([dashboard.js:36](../../../frontend/js/dashboard.js#L36)); `trends-page.js` її не бачить. Джоб-док
(`#job-dock`) у розмітці спільний ([index.html:143](../../../frontend/index.html#L143)) — тобто розмітка вже
глобальна, а власник інстансу — ні.

### 1.8 Живий прогрес зараз «шумний»

`watchJob` на **кожну** SSE-подію робить повний `getSyncJob(id)`, а `request()` абортить попередній політ по
спільному ключу `job-<id>` ([sse.js:11](../../../frontend/js/sse.js#L11)) — при кількох активних клітинках це
пачка марних запитів і серія `AbortError`, які код мусить окремо гасити. Плюс `renderProgress` перебудовує
**весь** `body.innerHTML` на кожен апдейт ([sync-modal.js:607](../../../frontend/js/sync-modal.js#L607)) — звідси
блимання, втрата фокусу (є ручний воркераунд на `data-session-action`) і скидання скролу матриці.

---

## 2. Обсяг (scope)

**Входить:**
1. Rolling-window refresh із UI — без нових ендпоінтів (наявний `POST /sync-jobs` уже приймає `custom` + `force_refresh`, [api.py:303](../../../app/schemas/api.py#L303)).
2. Чесний freshness у API трендів (`data_through` на клієнта і на портфель) + індикатор в UI.
3. Спільний модуль модалки прогресу (`job-progress.js`) + preset «metrics refresh».
4. Живий прогрес v2: коалесований refetch, no-flicker rerender, ETA, анімації за контрактом.
5. Після завершення — інвалідація й перемалювання графіка без втрати фільтрів/скролу.

**Не входить (свідомо):**
- Паралельні браузерні слоти — [task-31](task-31-parallel-collection-slots.md).
- Планувальник/нічний авто-refresh (потрібен новий процес-компонент; PLAN §16 етап 8 «поза MVP») — §8.
- Зміна семантики періодів дашборда (`closed` лишається `closed`).

---

## 3. Рішення

### 3.1 Одна джоба, нуль нових ендпоінтів

```
POST /api/v1/sync-jobs
{
  "client_ids": [...],                 // видимі на сторінці клієнти (після фільтра Portfolio)
  "providers": ["gsc", "reporting_ninja"],
  "period_kind": "custom",
  "comparison_mode": "closed",
  "custom_start": "<today-N+1>",
  "custom_end":   "<today-1>",         // «вчора»: сьогоднішній день у джерелі ще пишеться
  "mode": "force_refresh",             // §1.3 — інакше нуль нових рядків
  "source_mode": "auto",
  "idempotency_key": "<uuid>"
}
```

`custom_end = today-1` у **таймзоні клієнта** брати не можемо (джоба одна на батч) — беремо UTC-«вчора», як CLI;
різниця ≤1 день і повністю накривається вікном.

### 3.2 Вікно: 14 / 30 / 90, дефолт **30**

Вартість майже не залежить від довжини вікна (GSC ~2 запити, GA4 ~12 запитів **на клієнта**, незалежно від
діапазону — див. попередження в [cli.py:174](../../../app/cli.py#L174)), а 30 днів гарантовано накривають і
розрив, і всі provisional-дні. 14 — «швидко долити хвіст», 90 — «перезібрати квартал».

### 3.3 Freshness в API (адитивно)

- `PortfolioClientSeriesDTO.data_through: date | None` — максимальний день із **не-`null`** значенням для цього клієнта.
- `PortfolioSeriesDTO.data_through: date | None` + `stale_client_ids: list[str]` — портфельний максимум і хто відстає більш ніж на `lag_days + 1`.
- `None` означає «не збирали», **не** «нуль» (PLAN §12). Рахує чиста функція в `app/metrics/portfolio.py`, роутер лише мапить.

### 3.4 Спільний власник модалки: `frontend/js/job-progress.js`

Витягуємо створення інстансу з `dashboard.js` у новий модуль-синглтон:

```js
export function getJobProgress()            // lazy singleton, один на застосунок
// нові presets поверх наявного createSyncModal:
openMetricsRefresh({ clientIds, providers, windowDays, label })
```

`dashboard.js` починає імпортувати той самий синглтон (замість власного інстансу) — його поведінка не
змінюється, доки preset не викликано. `trends-page.js` імпортує його ж. Це прибирає й наявний баг «зайшов
одразу на `#/trends`, а активна джоба з localStorage не показується» — синглтон піднімає `watchJob` на будь-якій
сторінці, як і зараз на дашборді ([sync-modal.js:737](../../../frontend/js/sync-modal.js#L737)).

### 3.5 Живий прогрес v2 (стабільність — головний критерій)

| Проблема (§1.8) | Рішення |
|---|---|
| Refetch на кожну подію + `AbortError`-шум | trailing-debounce 250 мс + не більше 1 запиту/с на джобу; подія лише «позначає брудним» |
| `innerHTML` на весь body → блимання, втрата фокусу/скролу | перший рендер будує DOM, далі **точкові** апдейти (`textContent`, `value`, `dataset.state`) по `data-cell="<client>:<provider>"`; повний rebuild лише коли змінився **набір** клітинок |
| Немає відчуття руху при довгих клітинках | `progress_message` (уже є в DTO, [api.py:349](../../../app/schemas/api.py#L349)) як основний рядок + elapsed-таймер на активній клітинці |
| Немає ETA | ETA = медіана тривалості завершених клітинок × залишок / кількість активних; показувати **тільки** коли завершено ≥3 клітинки, інакше «Estimating…» (AGENTS §9: «Do not animate a fake percentage») |
| Дрібне | `overscroll-behavior: contain` вже є; додати збереження скролу матриці між апдейтами |

Анімації (AGENTS §10): лише `transform`/`opacity`; прогрес-бар — `transform: scaleX()` з `transition` 200 мс;
поява рядка — `opacity`+`translateY(2px)`; ніякого `transition: all`, ніяких вічних ambient-анімацій навколо
живих чисел; повний `prefers-reduced-motion` шлях (стани міняються без переходів).

### 3.6 Завершення → графік

`onFinished` для preset-а «metrics refresh» на сторінці трендів: скинути `lastPayload`, перезапросити ряди,
перемалювати, **зберігши** metric/bucket/range/group/view/вибір клієнтів (усе вже в URL) і позицію скролу.
Тост: `Refreshed N clients · data through Jul 30`.

### 3.7 UI-копірайт (англійська, AGENTS §14)

- Чип свіжості: `Data through Jul 26 · 4 days behind` (`aria-label` розгорнуто); коли в межах лагу — `Data through Jul 30 · up to date`.
- Кнопка: `Refresh data`; поряд селектор вікна `Last 14 / 30 / 90 days`.
- Підпис під кнопкою: `Collects day-level GSC and GA4 history. GSC finalizes the last 3 days, GA4 the last 2 — the newest buckets stay provisional.`
- Помилка: `Could not start the refresh: <reason>. The chart above still shows the last collected data.`

---

## 4. Owned paths

| Шлях | Що робимо |
|---|---|
| `frontend/js/job-progress.js` | **новий** — синглтон модалки + presets |
| `frontend/js/sync-modal.js` | preset `openMetricsRefresh`, живий прогрес v2 (точкові апдейти, ETA, debounce-хук) |
| `frontend/js/sse.js` | коалесування refetch |
| `frontend/js/trends-page.js` | freshness-чип, кнопка/селектор вікна, ре-лоад після завершення |
| `frontend/js/dashboard.js` | перехід на спільний синглтон (без зміни поведінки) |
| `frontend/css/trends.css`, `frontend/css/app.css` | стилі чипа/кнопки + `scaleX`-прогрес, reduced-motion |
| `app/metrics/portfolio.py` | обчислення `data_through`/`stale_client_ids` (чисті функції) |
| `app/api/portfolio.py` | мапінг нових полів у DTO |
| `app/schemas/api.py` | **адитивні** поля DTO (сумісні, `None` за замовчуванням) |
| `tests/unit/metrics/`, `tests/unit/api/`, `tests/e2e/frontend/` | §6 |

Read-only: `app/jobs/**`, `app/api/jobs.py`, `app/db/**`, `app/integrations/**`, `app/cli.py`, `frontend/AGENTS.md`.

---

## 5. Definition of Done / Gate

1. На `#/trends` видно чесний `data through` (сходиться з БД) і `N days behind`.
2. «Refresh data» ставить джобу з правильними `custom_start`/`custom_end`/`mode` — перевірено по `sync_jobs` у БД.
3. Прогрес відкривається з `#/trends`, закриття модалки **не** скасовує збір, F5 відновлює стан із SSE-реплею.
4. Після завершення графік дотягнутий до `today-1` (з поправкою на лаг), фільтри/скрол/вибір клієнтів збережені.
5. Живий прогрес: ≤1 refetch/с на джобу, немає блимання, фокус і скрол матриці не губляться, `AbortError`-тостів немає.
6. `prefers-reduced-motion: reduce` — усі стани читаються без анімацій.
7. Нуль українських рядків у UI; нуль нових залежностей; нуль правок у read-only зонах.
8. `ruff` / `mypy app` / `pytest` — зелені **в Docker**.

---

## 6. Тести

- `tests/unit/metrics/test_portfolio.py` (додатки): `data_through` = останній **не-null** день; `None` коли ряд порожній; `stale_client_ids` враховує `lag_days`.
- `tests/unit/api/test_portfolio_api.py` (додатки): нові поля в DTO; ендпоінт **не** створює джоб (наявний тест `test_endpoint_never_creates_a_sync_job` лишається зеленим).
- `tests/e2e/frontend/trends-refresh.cjs` (fixture-mode): чип → клік → мок-джоба → прогрес → перемалювання; окремий прогін із `prefers-reduced-motion`.
- Ручний живий прогін: `POST /sync-jobs` із вікном 14 днів на 2 клієнтах, перевірка нових днів у `analytics_dataset_rows` і відсутності дублів у `daily_series`.

---

## 7. Ризики

| Ризик | Мітигація |
|---|---|
| `force_refresh` на 14 клієнтів × 2 провайдери = ~14×14 запитів до Ninja | вікно за замовчуванням 30 днів, але **кнопка не автоматична**; у підписі під кнопкою прямо сказано, що це metered; за замовчуванням оновлюються лише **видимі** (відфільтровані) клієнти |
| Оператор чекає «дані за сьогодні» | чип і підпис явно кажуть про лаг 3/2 дні; provisional-бакети вже підсвічені |
| Refresh конкурує з рутинним синком | той самий джоб-рушій і ті самі lane-и: клітинки просто стають у чергу; активна джоба показується в доці |
| Синглтон модалки змінює поведінку дашборда | `dashboard.js` викликає ті самі методи; e2e `sync-auth-flow.cjs` мусить лишитись зеленим **без правок** |

---

## 8. Follow-ups / борг

- `TODO(task-32)`: нічний авто-refresh (планувальник + «last refreshed» в UI + правило «не частіше 1×/добу на клієнта»). Потрібен окремий процес-компонент — свідомо не тут.
- `TODO(task-31)`: паралельні браузерні слоти; після нього прогрес-панель отримує ще й розріз «per browser lane».
- Розгляд `comparison_mode=to_date` для дашборда (щоб «поточний тиждень» був видимий і в картках) — окремою таскою, бо змінює семантику порівнянь (PLAN §12).

---

## ✅ Completion report

- **Дата:** 2026-07-31
- **Гілка / комміт:** `task-30-live-data-refresh` @ (див. комміт нижче)
- **Статус:** done

### Що зроблено

- На `#/trends` з'явився рядок свіжості: чип **`Data through Jul 29 · up to date` / `· N days behind`**, селектор вікна (14/30/90, дефолт 30) і кнопка **«Refresh data»**.
- Кнопка ставить **rolling-window** джобу через наявний `POST /sync-jobs` (`period_kind=custom`, `custom_end` = вчора, `mode=force_refresh`, провайдери `gsc`+`reporting_ninja`) — те, що досі вміла лише CLI `backfill-daily-history`. Перед стартом — preflight-панель із точним діапазоном, режимом, лагом джерел і попередженням про metered-збір.
- Діалог прогресу став **спільним синглтоном** (`frontend/js/job-progress.js`): він більше не належить дашборду, тож працює з будь-якої сторінки, а активна джоба з `localStorage` підхоплюється скрізь.
- **Живий прогрес v2:** SSE-події коалесуються в ≤1 полінг снапшота на 250 мс (+10 с heartbeat як запобіжник), рендер розділений на «повна побудова при зміні структури» / «точкові патчі значень», з'явився вимірюваний ETA (`N cells left · about 47s to go`) і годинник прогону.
- Живий прогін на реальних даних: 22 клітинки (11 клієнтів × 2 провайдери) за ~3 хв, `gsc_daily` дотягнувся з **26.07 → 29.07**, `ga4_daily` → **30.07**, чип змінився з «1 day behind» на «up to date».

### Як зроблено (ключові рішення)

- **Нових ендпоінтів немає.** `SyncJobRequest` уже приймав `custom` + `custom_start/custom_end` + `force_refresh`; єдина серверна зміна — адитивне `PortfolioMetricDTO.lag_days`, бо «up to date vs N days behind» інакше не порахувати чесно (`buckets[].provisional` дає лише булеву межу).
- **`data_through` на бекенді не додавався** — на відміну від плану §3.3. `PortfolioClientSeriesDTO.last_day` уже означає рівно «останній день із даними», тож портфельний максимум і `days_behind` рахує фронт із наявних полів. Менше дубльованої логіки; тест `test_series_last_day_is_the_last_day_with_data` фіксує цю семантику як контракт.
- **`custom_end = today-1` в UTC** — так само, як робить CLI: сьогоднішній день у GSC/GA4 ще пишеться, і частковий «сьогодні» ліг би як реальне (занижене) значення дня.
- **Свіжість міряється проти лагу джерела, а не проти «сьогодні»**: `expected = вчора − lag_days`. Інакше здоровий ряд назавжди світився б «1 day behind», бо GSC фіналізує 3 останні дні.
- **Патч-рендер замість `innerHTML`**: анкер `data-cell="<client>|<provider>"` + `data-cell-label/detail/progress`; сесійна панель і футер діфаються як рядки й перемальовуються лише при реальній зміні (це те, що зберігає фокус на кнопці «Log in» і скрол матриці). `resetProgressView()` викликається скрізь, де `body` перезаписується поза рендером прогресу.
- **Єдиний cache-buster** `?v=20260731-refresh1` по всьому фронту: різні теги давали б **дві копії** модуля модалки (dashboard і trends), тобто два інстанси діалогу й два SSE-з'єднання на ту саму джобу.
- Головні файли: `frontend/js/job-progress.js` — власник діалогу; `frontend/js/sync-modal.js` — preset `openMetricsRefresh` + рендер v2; `frontend/js/sse.js` — коалесування; `frontend/js/trends-page.js` — чип/кнопка/тихий ре-лоад; `app/schemas/api.py`+`app/api/portfolio.py` — `lag_days`.

### Контрактні зміни (Contract changes)

Адитивно: `PortfolioMetricDTO.lag_days: int = 0` (GSC 3, GA4 2). OpenAPI-снапшот перегенеровано — діф рівно 5 рядків, жодного поля не змінено й не прибрано. Споживач — `frontend/js/trends-page.js`; фікстури `frontend/fixtures/trends-*.json` оновлені під той самий контракт.

### Як перевірити (verification)

```bash
docker compose up -d --build
docker compose exec api poetry run pytest -q            # 1006 passed
docker compose exec api poetry run ruff check .
docker compose exec api poetry run mypy app
bash tests/e2e/run_frontend_smoke.sh                    # дашборд + модалка
NODE_PATH=/tmp/yovth-e2e/node_modules CHROMIUM_PATH=/snap/bin/chromium \
  node tests/e2e/frontend/trends-refresh.cjs            # fixture-mode, нуль units
```

В UI: `#/trends` → чип показує останній зібраний день і розрив → «Refresh data» → preflight із діапазоном → старт → матриця й ETA оновлюються без блимання → закрити діалог (збір триває, док видно) → після завершення графік перемальовується сам, фільтри й вибір клієнтів збережені.

### Тести

- `tests/unit/api/test_portfolio_api.py`: `test_metric_reports_source_lag_days` (GSC 3 / GA4 2), `test_series_last_day_is_the_last_day_with_data` (пропуск у кінці вікна зсуває `last_day`, а не маскується кінцем діапазону).
- `tests/unit/api/data/openapi_snapshot.json` — перегенеровано.
- `tests/e2e/frontend/trends-refresh.cjs` (новий, fixture-mode): чип і його тон, preflight-вікно = рівно 14 днів і закінчується вчора, тільки два провайдери в матриці, закриття діалогу не спиняє збір, немає горизонтального скролу на 390px, `reducedMotion: "reduce"`, порожній портфель не вигадує дату.
- Живий прогін (metered, за згодою користувача) — числа вище.

### Follow-ups / борг

- `TODO(task-31)`: паралельні браузерні слоти; панель «Browser lanes» стане поверх цього ж рендера.
- `TODO(task-32)`: нічний авто-refresh (планувальник) — щоб чип не доводилось лікувати руками.
- `.progress-eta` показує оцінку тільки після ≥3 завершених клітинок; для дуже коротких прогонів вона так і лишається «estimating time…» — свідомо, бо брехати числом гірше.
- `tests/e2e/frontend/trends-refresh.cjs` не входить у `run_frontend_smoke.sh` (як і решта окремих suite-ів) — запускається вручну за інструкцією в шапці файлу.
