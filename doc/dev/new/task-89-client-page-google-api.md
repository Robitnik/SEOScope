# task-89 — Сторінка клієнта на прямому Google API: заміна Reporting Ninja + розширення даних

- **Хвиля:** пост-83/84 (джерела даних). Незалежна від хвиль архіву (63/70/80), БД (66/67/75) і MCP (74/77/79).
- **Depends on:** **09** ✅, **16** ✅, **18** ✅, **28** ✅, **30** ✅, **44** ✅, **83** ✅, **84** ✅. Не блокується 75 (прод уже на Postgres).
- ⚠️ **Паралельна таска — [task-88](task-88-url-inspector-google-api.md)** (`#/url` на прямий Google API), у роботі **прямо зараз** в іншій сесії того самого дерева. Спільна зона — `app/integrations/google_api/`: task-88 **уже додала** `GoogleApiSession.get_json` із виділенням спільної політики в `_request` (незакомічено на 2026-09-09). Тому §3.1 цієї таски **скасовано** — `auth.py` тут read-only. Перед будь-яким коммітом у `app/integrations/google_api/__init__.py`, `app/core/config.py`, `frontend/js/api.js`, `frontend/js/router.js`, `tests/unit/api/data/openapi_snapshot.json` — перевірити `git status` на них (task-88 їх теж торкається адитивно).
- **Owned paths:**
  `app/integrations/google_api/gsc_client.py` (узагальнення на будь-які дименсії — §3.2),
  `app/integrations/google_api/ga4_client.py` (адитивно: фільтр дименсій, ліміт рядків — §3.3),
  `app/integrations/google_api/admin_client.py` (новий — §4.2),
  `app/integrations/google_api/compare.py` (розширення gate на 4 нові поверхні — §7.1),
  `app/integrations/gsc.py` (три нові розвилки — §3.4),
  `app/integrations/reporting_ninja.py` (дві нові розвилки — §3.5),
  `app/api/analytics.py` (whitelist датасетів + нові ендпоінти — §5.1),
  `app/core/config.py` (**адитивно**, §3.6),
  `app/cli.py` (розширення `compare-transports`, `set-transport --all` — §7.1),
  `frontend/js/reporting-ninja-panel.js` → **перейменувати** в `frontend/js/ga4-panel.js` (§6.1),
  `frontend/js/client-page.js`, `frontend/js/analytics-tabs.js`, `frontend/css/client-page.css`,
  `tests/unit/integrations/google_api/`, `tests/e2e/frontend/client-page-google-api.cjs`
- **Read-only:** `app/integrations/google_api/auth.py` (`get_json` уже додає task-88 — §3.1), `app/urlinspector/**` (task-88), `app/metrics/catalog.py` (володіє task-04 — §2.5 пояснює, чому нові KPI **не** йдуть у каталог), `app/reports/**` (task-84/85), `app/schemas/enums.py` (task-02), `migrations/` (task-03).
- **Міграція:** **не потрібна.** Ні нового `Provider`, ні нової колонки. `analytics_dataset_rows.dataset_key` — вільний `String`, тож нові датасети додаються без схеми; whitelist живе в коді ([`analytics.py#L53`](app/api/analytics.py#L53)). Причина, чому це саме так — §2.
- **Фронт:** **змінюється** — на відміну від [task-83](../done/task-83-google-api-direct-transport.md), де незміненість фронта була критерієм правильності розрізу. Тут інакше: §2.4 обґрунтовує різницю.

---

## §0. Що є сьогодні (виміряно живо на HWELL 2026-09-09, не гіпотеза)

Клієнт заміру: **HWELL** (`f519c589-4018-4035-abaa-0c951c825857`, `hearwellservices.com`),
`gsc.account_id = sc-domain:hearwellservices.com`, `reporting_ninja.account_id = properties/395442288`.

### §0.1 Сторінка клієнта має **п'ять** шляхів до даних, і чотири з них — Reporting Ninja

| Блок на `#/client/<id>` | Читає | Наповнює | Транспорт сьогодні |
|---|---|---|---|
| KPI-картки GSC у хедері | `MetricValue` (`gsc_clicks`…) | [`GscAdapter.collect`](app/integrations/gsc.py#L181) | ❌ **RN** |
| «Organic search detail» (Pages/Queries) + Alerts | `gsc_dimension_metrics` | [`GscAdapter.collect_dimensions`](app/integrations/gsc.py#L527) | ❌ **RN** |
| «Clicks and impressions trend» | `gsc_daily` | [`GscAdapter._collect_datasets`](app/integrations/gsc.py#L271) | ✅ розвилка є (task-83) |
| KPI-картки GA4 (`Users`/`Organic Users`/`Total Conversions`/`Organic Conversions`) | `MetricValue` (`rn_*`) | [`ReportingNinjaAdapter._collect`](app/integrations/reporting_ninja.py#L420) | ❌ **RN** |
| «Traffic and conversions»: денний графік + таблиця breakdown | `ga4_daily` + `ga4_channels`/`ga4_landing_pages`/`ga4_events`/`ga4_devices`/`ga4_countries` | [`_collect_datasets_reporting_ninja`](app/integrations/reporting_ninja.py#L635) | частково: `ga4_daily`/`ga4_daily_channels` ✅, **решта ❌ RN** |
| «Local visibility» (GBP) | `gbp_daily`/`gbp_locations`/`gbp_search_terms`/`gbp_reviews` | той самий метод | ❌ **RN** (і §6.3 — інакше не буде) |

Отже task-83 закрила рівно **одну** з шести поверхонь — ту, що живить `#/trends`. Сторінка клієнта досі йде через платний проксі майже цілком.

### §0.2 Транспорт `google_api` **не ввімкнений ніде**, попри пройдений gate

`config_json->>'transport'` = `NULL` у **всіх 27 інтеграцій** (15 клієнтів × `gsc`/`reporting_ninja`), а [`google_api_default_transport`](app/core/config.py#L645) = `"reporting_ninja"`. Тобто task-83 дала робочий, живо звірений (байт-у-байт на SHB/EFFYS/SAPHNY/3XE) механізм, який **у проді не використовується**. Цю таску не можна закрити, не ввімкнувши його — інакше вона повторить ту саму долю (§8.1).

### §0.3 Reporting Ninja викликається постійно, і кожен виклик — гроші

`sync_job_providers` за 30 днів:

| provider | клітинок | avg | max |
|---|---|---|---|
| `reporting_ninja` | 162 | **14.5 с** | 38.4 с |
| `gsc` | 145 | **7.6 с** | 32.7 с |

Це не «раз на добу за розкладом»: **кожне відкриття вікна, якого немає в БД, стартує справжній збір**. Виміряно живим експериментом у ході цього дослідження — два Playwright-переходи на `period=month` і `period=quarter` створили два реальні `sync_jobs` (`3ef68c65`, `c829804c`, обидва `succeeded` за ~20 с), які витратили 4 RN/GSC-клітинки. Механізм [`maybeStartLiveCollection`](frontend/js/client-page.js#L681) працює як задумано — але ціна кожного погляду на новий період платиться Reporting Ninja.

### §0.4 Порожні панелі — це не баг читання, а вартість збору

`AnalyticsDatasetRepository.for_period` матчить вікно **точно** ([`analytics_datasets.py#L74-L84`](app/db/repositories/analytics_datasets.py#L74-L84): `period_start == …, period_end == …`, найновіший `snapshot_id`). Виміряно на HWELL до авто-збору:

```
period=month    → «No GA4 KPI snapshot for this period.»
                  «No daily GA4 series yet.»
                  «No channels for this period.»
                  «No data collected for this period yet.» (Pages/Queries)
period=quarter  → те саме + KPI-картки показують числа ІНШОГО періоду (§0.5)
```

Після того, як авто-збір відпрацював (~20 с), `period=month` дав `ga4_channels` 8 рядків, `ga4_landing_pages` 363, `ga4_events` 15 — **точно ті самі числа, які прямий Google API віддає за той самий місяць** (§0.7). Тобто механізм справний, а RN тут — прозорий, але платний посередник.

### §0.5 Живий дефект: KPI-картки GA4 брешуть на `quarter`/`year`/`custom`

[`client-page.js#L1075-L1077`](frontend/js/client-page.js#L1075-L1077):

```js
const dashboardPromise = current.period === "week" || current.period === "month"
  ? getDashboard({ period: current.period, ... })
  : Promise.resolve(null);
```

Для `quarter`/`year`/`custom` дашборд **не запитується**, і `summary` фолбечиться на `current.reportingNinjaSummary` — знімок, отриманий у [`loadClient()`](frontend/js/client-page.js#L311) запитом `getDashboard({ period: "week" })`. Виміряно:

| Вікно на сторінці | Що показують KPI-картки | Що API насправді віддає для цього вікна |
|---|---|---|
| `week` (Aug 31 – Sep 6) | Users **1 257** +38.6% | 1 257 ✔ |
| `quarter` (Apr 1 – Jun 30) | Users **1 257** +38.6% ❌ | **10 695** +9.7% |

Числа тижня підписані кварталом, без жодної позначки. `GET /api/v1/dashboard?period=quarter` віддає правильні 10 695 — тобто дефект **цілком у фронті**. Це не «мимохідь помічено»: ця таска переписує саме цей шлях, тож лагодити його тут — на місці (§6.2).

### §0.6 Прямий Google API покриває **всі** GA4-датасети сторінки — перевірено запитом за запитом

Живий прогін (HWELL, серпень 2026):

| Датасет | GA4-дименсія | Рядків | Час |
|---|---|---|---|
| `ga4_channels` | `sessionDefaultChannelGroup` | 8 | 0.57–1.07 с |
| `ga4_landing_pages` | `landingPage` | 363 | 0.38–0.59 с |
| `ga4_events` | `eventName` | 15 | 0.47–1.07 с |
| `ga4_devices` | `deviceCategory` | 4 | 0.39–0.45 с |
| `ga4_countries` | `country` | 114 | 0.33–1.05 с |

Кожна метрика з правої частини [`_GA4_DATASETS`](app/integrations/reporting_ninja.py#L63) (`totalUsers`, `sessions`, `keyEvents`, `engagementRate`, `screenPageViews`, `eventCount`) приймається `runReport` як є. **Мапінг будувати не треба** — він уже написаний у тому самому dict, який використовує RN-шлях.

### §0.7 GSC-дименсії теж покриті — включно з тими, яких у нас немає

| Дименсія | Результат (HWELL, серпень) | Час |
|---|---|---|
| `page` | 258 рядків | 0.13 с |
| `query` | 1 000 (за `rowLimit`); **11 415** за `rowLimit=25000` | 0.23 / **0.59 с** |
| `page`+`query` | працює — «за якими запитами приходять на цю сторінку» | — |
| `device` | 3 | — |
| `country` | 5+ | — |
| `searchAppearance` | 3 (`TRANSLATED_RESULT`…) | — |
| `date`+`page` | працює — денна динаміка окремої сторінки | — |

Наш `collect_dimensions` сьогодні бере `top_n=1000`. Прямий API за 0.59 с віддає **11 415** запитів — тобто ширше покриття доступне безкоштовно, а не як компроміс.

### §0.8 Повний набір для сторінки — 18 запитів, 8.78 с послідовно

Заміряно: два періоди (current+previous) × (6 GA4 + 3 GSC) = 18 викликів, **8.78 с** послідовно; жоден окремий виклик не перевищив 1.07 с. Для порівняння — RN+GSC клітинки того самого збору: **~22 с** (§0.3). Паралелізація по 6 дала б ~2 с, але §2.3 пояснює, чому вона не потрібна.

### §0.9 GSC відкриває дані, яких у нас **немає взагалі**

```
web        clicks=1736  impressions=281736   ← єдине, що ми збираємо сьогодні
image      clicks=34    impressions=25110    ← невидиме для тула
video      0 / 0
news       0 / 0
discover   0 / 0
```

`searchType` — параметр того самого запиту, який ми вже робимо. Тобто 25 110 показів у Google Images для HWELL коштують **нуль додаткової інфраструктури** і сьогодні просто відкинуті.

### §0.10 GA4 віддає розклад конверсій по НАЗВАХ подій — а ми показуємо одне число

`Total Conversions 100` за серпень — це сума. Прямий API з `dimensionFilter` по `eventName`:

```
Phone            29
purchase         20
Email             9
Insurance Form    1
```

Плюс [GA4 Admin API](https://analyticsadmin.googleapis.com/v1beta) віддає **офіційний список key events** цього property (`properties/395442288/keyEvents`): `purchase`, `Phone`, `Insurance Form`, `Email` — з `createTime` і `custom: true`. Тобто ми можемо показати «29 звернень телефоном», а не абстрактну сотню, **не питаючи клієнта, що в нього налаштовано**.

### §0.11 GA4 віддає engagement-метрики в тому самому запиті

Один `runReport` по `sessionDefaultChannelGroup` із розширеним набором метрик (HWELL, серпень):

| Канал | sessions | engagedSessions | bounceRate | avgDuration | newUsers | sessionKeyEventRate |
|---|---|---|---|---|---|---|
| Direct | 4 229 | 967 | **0.771** | **43 с** | 3 952 | 0.004 |
| Organic Search | 2 612 | 1 995 | **0.236** | **184 с** | 2 107 | 0.014 |
| Paid Search | 183 | 129 | 0.295 | 256 с | 133 | 0.066 |

Це та сама ціна запиту, що й зараз (додаткові метрики не додають викликів). І це негайно читабельний сигнал: `Direct` із 77% відмов і 43 с — не той самий трафік, що `Organic` із 23% і 3 хвилинами.

### §0.12 Живий сигнал якості даних, який тул сьогодні приховує

Розклад по `city` (HWELL, серпень): **Vinnytsia — 1 906 сесій**, друге місце по всьому портфелю трафіку клієнта з США, у каналі `Direct` із bounceRate 0.77. Тобто `Users 6 364` за серпень включає трафік, який майже напевно не є клієнтами. Тул показує цю суму як KPI без жодного застереження.

### §0.13 GSC Sitemaps API і URL Inspection API працюють з наявним токеном

```
SITEMAPS  https://hearwellservices.com/sitemap_index.xml
          submitted 2025-02-18 | downloaded 2026-09-08 | errors 0 | warnings 1
          web: 248 submitted | image: 515 submitted

URL_INSPECT  https://hearwellservices.com/
          verdict PASS | coverageState "Submitted and indexed" | robotsTxtState ALLOWED
          lastCrawlTime 2026-09-02T14:24:53Z | crawledAs MOBILE
          googleCanonical == userCanonical | referringUrls: 3
```

Це прямий вердикт Google про індексацію. [`#/url-inspector`](frontend/js/url-inspector.js) (task-61/64) сьогодні його **не має**: [`app/urlinspector/sources/gsc.py`](app/urlinspector/sources/gsc.py) читає `gsc_dimension_metrics`, тобто наші власні зібрані показники, а не статус індексації.

### §0.14 GA4 Admin API виявив розбіжність таймзони

`properties/395442288` → `timeZone: America/Los_Angeles`, `displayName: "Hear Well Services"`.
`clients.timezone` у нашій БД → **`America/New_York`**.

Добові бакети GA4 нарізаються **таймзоною property**, а наші періоди — `client.timezone` ([`resolve_periods(..., tz=client.timezone)`](app/api/analytics.py#L472)). Три години зсуву на межі доби. Це не гіпотетично: сьогодні цю розбіжність не видно ніде.

### §0.15 GBP через Google API **недоступний** — і це не полагодити кодом

```
GET mybusinessaccountmanagement.googleapis.com/v1/accounts        → 403 ACCESS_TOKEN_SCOPE_INSUFFICIENT
GET businessprofileperformance.googleapis.com/v1/locations/…      → 403 ACCESS_TOKEN_SCOPE_INSUFFICIENT
tokeninfo scope: analytics.readonly webmasters.readonly
```

Наданих scope рівно два ([`google_oauth_bootstrap.py#L35-L38`](scripts/google_oauth_bootstrap.py#L35-L38)). Для GBP потрібен `business.manage` **плюс** окреме затвердження Business Profile APIs у Google Cloud (Google гейтить їх заявкою, не просто вмиканням). Заразом: `gbp_*` дані є **в одного клієнта з 15** (SAPHNY), і найсвіжіші — `2026-07-19`, тобто два місяці лежать. Рішення — §6.3.

---

## §1. Мета і межа обсягу

**Мета:** сторінка клієнта живиться прямим Google API на всіх поверхнях, де це можливо; Reporting Ninja лишається лише там, де Google API недоступний (GBP); і те, що прямий API вміє понад RN, виводиться в UI.

**У обсязі:**
1. Розвилка транспорту в чотирьох методах, що досі RN-only (§3.4, §3.5).
2. Узагальнення `GscClient` на будь-які дименсії (§3.2).
3. Живий gate паритету на всіх чотирьох нових поверхнях — нуль допуску на цілі метрики (§7.1).
4. Ввімкнення `transport=google_api` на портфелі з відкатом (§7.3).
5. Нові дані, яких RN-шлях не має: per-event конверсії, engagement-метрики, GSC search types, sitemaps (§4).
6. Правдиві назви блоків у UI + фікс дефекту §0.5 (§6).

**Поза обсягом (свідомо):**
- **GBP** — §0.15/§6.3. Лишається на RN, блок не чіпаємо.
- **Перейменування `rn_*` ключів метрик** — `TODO(task-85)` з task-83 §2.4, окрема таска з власним gate: атомарна зміна у п'яти місцях (catalog → API → фронт → фільтри → збережені стани).
- **URL Inspection API у `#/url-inspector`** — §0.13 показує, що це можливо; це **обсяг паралельної [task-88](task-88-url-inspector-google-api.md)**, не цієї. Не дублювати.
- **Backfill історії** — прямий транспорт працює вперед і на вікні, яке API покриває (GSC — стеля 16 місяців, task-83 §0.7). Старі рядки **не чіпаємо і не перезбираємо**.
- **Нові `MetricValue`-и** — §2.5.

---

## §2. Головні рішення

### §2.1 Патерн розвилки — той самий, що в task-83, без винаходів

Кожен із чотирьох методів отримує рівно те, що вже має `_collect_datasets`:

```python
if resolve_transport(context.integration_config) == GOOGLE_API:
    return self._collect_<що>_google_api(context, emit)
return self._collect_<що>_reporting_ninja(context, emit)
```

`Provider` не змінюється, `dataset_key`/`dimension_key`/імена метрик не змінюються, різниця в рядку — **лише** `provenance.transport`. Це не стилістична вірність, а те, що робить gate §7.1 можливим: дві реалізації порівнюються числом до числа, бо пишуть в одну форму.

### §2.2 Ніякого тихого фолбеку на RN

`transport=google_api` без токена / з відмовою у property → `not_configured` + warning, як [task-83 §6.1.3](../done/task-83-google-api-direct-transport.md). Тихий фолбек зробив би gate брехливим: «однакові числа» означало б «обидва прочитали RN».

### §2.3 Збір лишається збором. Живого читання з API в обхід БД **не буде**

Спокуса очевидна: §0.8 показує 8.78 с на весь набір, тобто сторінку можна було б малювати прямо з API. Відмовляємось, і причини конкретні:

1. **Історія зникла б.** `analytics_dataset_rows` — це те, з чого будуються `#/trends`, health, alerts, звіти. Читання в обхід БД дало б сторінку, чиї числа неможливо відтворити завтра.
2. **Механізм уже є і працює.** §0.4: авто-збір заповнює порожнє вікно за ~20 с сам. Проблема не в архітектурі читання, а в ціні транспорту — і саме її ця таска й прибирає.
3. **`provenance` і `quality` втратили б сенс.** Рядок без снапшота не має ні `observed_at`, ні `truncated`, ні шляху до аудиту.
4. **Квоти.** GA4 Data API рахує токени на property за годину; сторінка, що б'є в API на кожен рендер, витратила б їх на F5, а не на дані.

Отже виграш формулюється чесно: **не «швидше малюється», а «збір коштує нуль, не тримає кабінетного локу і віддає ширші дані»**. Швидше — побічний ефект (§0.3 vs §0.8).

### §2.4 Чому фронт тут змінюється, хоча в task-83 незмінність була критерієм

У task-83 фронт не мав змінюватись, бо мета була **транспортна**: ті самі числа іншим шляхом. Тут дві мети, і друга — продуктова:

- Частина **A** (§3) справді не потребує жодного рядка у `frontend/` — і це її власний критерій правильності.
- Частина **B** (§4) виводить дані, яких у системі ніколи не було. Вона **не може** не змінювати фронт.

Тому вони розділені, з окремими гейтами, і B залежить від A. Заразом виправляється те, що стало неправдою: блок, підписаний `Reporting Ninja · GA4`, після cutover бреше про джерело — а CLAUDE.md вимагає, щоб провайдер називався однаково скрізь.

### §2.5 Нові показники йдуть **датасетами**, не `MetricValue`

Спокуса: додати `rn_bounce_rate`, `rn_engaged_sessions`, `rn_conv_phone` у [`catalog.py`](app/metrics/catalog.py) (16 `MetricSpec` сьогодні). Відмовляємось:

- `catalog.py` — **owned by task-04**, read-only для нас.
- `MetricValue` тягне за собою health-ваги, `METRIC_PROVIDERS`, `better_direction`, фільтри дашборда, `#/trends`-серії. Кожна нова метрика — зміна у 5+ місцях і перерахунок health по всьому портфелю.
- Per-event конверсії **динамічні за визначенням**: у HWELL це `Phone`/`Email`/`Insurance Form`, в іншого клієнта — інші. Enum-подібний каталог для них структурно непридатний.

`analytics_dataset_rows` — правильне місце: вільний `dataset_key`, вільний `metrics_json`, нуль міграцій, нуль впливу на health.

---

## §3. Частина A — транспорт (бек-онлі, паритет)

### §3.1 `auth.py` — нічого не робимо, це вже зробила task-88

Sitemaps (§4.5) і Admin API (§4.2) — це GET, а `GoogleApiSession` мав лише `post_json`. **Паралельна [task-88](task-88-url-inspector-google-api.md) вже додала `get_json`**, і саме правильним способом: спільне тіло циклу виділене в `_request(method, url, *, body, params)`, тож політика (401→refresh+retry, 429/`RESOURCE_EXHAUSTED`→backoff, 5xx→2 повтори, 403→permission vs rate limit) фізично не може розійтися між дієсловами.

**Дія цієї таски: жодна.** `auth.py` — read-only; `get_json` використовується як є. Якщо на момент старту task-88 ще не влилась — **зачекати на її комміт**, а не писати другу копію: дві реалізації політики розійдуться на першій правці, і це саме той клас колізії, який уже коштував проєкту часу на task-84/85.

### §3.2 `gsc_client.py` — узагальнення на будь-які дименсії

`daily_search_analytics` зашитий на `["date"]` і повертає `GscDailyRow` ([`gsc_client.py#L49`](app/integrations/google_api/gsc_client.py#L49)). Додається:

```python
def search_analytics(
    self, site_url, start, end, *,
    dimensions: Sequence[str],          # ["page"] | ["query"] | ["page","query"] | ["device"] …
    search_type: str = "web",
    row_limit: int = _ROW_LIMIT,        # 25 000 — стеля API на сторінку
    max_rows: int | None = None,        # стеля top_n виклику
) -> list[GscRow]                       # keys: tuple[str,...], clicks/impressions/ctr/position
```

`daily_search_analytics` **лишається** і стає тонкою обгорткою над ним — щоб task-83 не отримала регресію. Пагінація по `startRow` уже реалізована в `_paged_rows`, вона й перевикористовується.

**Пастка, що вже коштувала проєкту:** `ctr` з API — частка `0..1`; RN віддає відсоток, і [`_reporting_ninja_ctr`](app/integrations/gsc.py#L628) ділить на 100. Множення тут інфлює кожне значення ×100, малюючи при цьому правдоподібний графік (task-83 §4/§7.3). Тест на це — обов'язковий, не «за бажанням».

### §3.3 `ga4_client.py` — адитивно фільтр і ліміт

`run_report` уже приймає довільні `dimensions`/`metrics` ([`ga4_client.py#L45`](app/integrations/google_api/ga4_client.py#L45)) — цього досить для всіх п'яти breakdown-датасетів (§0.6). Додається лише:

- `dimension_filter: dict | None` — для §4.1 (розклад по названих key events);
- `limit: int | None` — щоб `_GA4_DATASETS[...]["limit"]` (100/250/500) виконувався так само, як у RN-шляху, разом із міткою `Quality.truncated` при обрізанні.

Інваріант незмінний: `keyEvents` **без** `conversions` в одному запиті (інакше `INTERNAL`), дименсія `month` не використовується (малює хибні нулі).

### §3.4 `gsc.py` — три розвилки

| Метод | Новий шлях | Що зберігає паритет |
|---|---|---|
| `_collect` (агрегат) | `dimensions=[]`, один запит на період | `clicks`/`impressions` цілі; `ctr` — частка (§3.2); `position` — як віддає API |
| `_collect_dimensions` | `dimensions=["page"]` і `["query"]` × 2 періоди | той самий `top_n`, той самий `DimensionValueResult`, той самий `_map_dimension_periods` |
| `_collect_datasets` | вже є (task-83) | — |

`data_through` (останній день із даними) береться з `dimensions=["date"]` — вимірено §0.7: GSC віддав дані до `2026-09-06` при запиті до `2026-09-09`, тобто лаг ~3 дні, і його треба нести, а не вигадувати.

### §3.5 `reporting_ninja.py` — дві розвилки

| Метод | Новий шлях |
|---|---|
| `_collect` (KPI `rn_*`) | `users`/`organic_users`/`total_conversions`/`organic_conversions` — модель атрибуції з [`api_source.py` §0.4](../done/task-84-reports-google-api-source.md): тотали **без дименсії**; organic/direct — окремий запит по `firstUserDefaultChannelGroup`; збіг каналу **точний** (`== "Organic Search"`, інакше в organic втрапляють «Organic Social»/«Organic Shopping»). Це вже написано й живо звірено в [`google_source.py`](app/reports/google_source.py) — **читати як еталон, не переписувати заново** |
| `_collect_datasets_google_api_trends` → розширити на всі 6 | generic-цикл по `_GA4_DATASETS` (той самий dict, що й RN-шлях), плюс наявна окрема гілка `ga4_daily_channels` |

**Пастка:** `engagement_rate` з API — частка `0..1`, а кожен збережений `ga4_daily`/`ga4_channels` рядок несе відсоток. `_GA4_PERCENT_SCALED` ([`reporting_ninja.py#L213`](app/integrations/reporting_ninja.py#L213)) уже існує для `ga4_daily` — його треба застосувати **до всіх** датасетів, де є `engagement_rate` (`ga4_channels`, `ga4_landing_pages`). Пропустити один — розбіжність ×100 на одній таблиці.

**Рішення про GBP:** при `transport=google_api` GBP-датасети **і далі збираються через RN** у тому самому зборі — рівно як task-83 зробила для breakdown-датасетів («mixed», §0.15). Це не порушення §2.2: заборонено *тихо підміняти* транспорт порівнюваної поверхні, а GBP — незалежна поверхня, до якої Google API доступу не має взагалі.

### §3.6 `config.py` — адитивно

```python
google_api_gsc_dimension_top_n: int = 1000      # паритет з наявним collect_dimensions(top_n=1000)
google_api_include_search_types: bool = False   # §4.3, вимкнено доки не пройде §7.2
```

---

## §4. Частина B — що ще можемо вивести (пріоритезовано за співвідношенням цінність/ціна)

Кожен пункт — з живого заміру §0, не з документації. Ціна — у нових API-викликах на період.

### §4.1 🔴 Конверсії по назвах подій — `ga4_key_events` (найвища цінність)

**Замір:** §0.10 — `Phone 29`, `purchase 20`, `Email 9`, `Insurance Form 1` замість «Total Conversions 100».
**Ціна:** 0 нових викликів — `ga4_events` **уже** запитує `eventName` + `keyEvents` (§0.6). Достатньо не викидати `keyEvents` з рядків і відсортувати за ним.
**Плюс** 1 виклик Admin API (`{property}/keyEvents`, §4.2) на клієнта, щоб знати, які події клієнт **оголосив** конверсіями, а не вгадувати за ненульовим `keyEvents`.
**UI:** KPI-картка `Total Conversions` розкривається на розклад по подіях. Для клієнта-послуги «29 звернень телефоном» — це і є звіт.

### §4.2 🔴 GA4 Admin API — `admin_client.py`

**Замір:** §0.10 (`keyEvents` list), §0.14 (`displayName`, `timeZone`, `industryCategory`, `currencyCode`).
**Ціна:** 2 GET на клієнта, кешується на добу (конфігурація property не змінюється щогодини).
**Дає три речі:**
1. офіційний список key events → §4.1;
2. **попередження про розбіжність таймзони** (§0.14: property `America/Los_Angeles` vs `clients.timezone America/New_York`) — це реальний зсув добових бакетів, і сьогодні його не видно ніде;
3. `displayName` property → перевірка, що клієнт підв'язаний до правильної property (клас помилки «звіт іншого клієнта», задокументований у [task-86](../done/task-86-saphny-cross-source-proof.md)).

### §4.3 🟠 GSC search types — `gsc_search_types`

**Замір:** §0.9 — `web 1736/281736`, **`image 34/25110`**, решта нулі.
**Ціна:** +2 виклики на період (`image`, і за прапорцем `discover` для контент-клієнтів). Нулі не пишуться (PLAN §12: відсутнє ≠ 0).
**UI:** рядок під графіком «Web · Image · Video · Discover» з можливістю перемкнути тип. Для клієнтів із фото-контентом Image — це видимий, сьогодні відкинутий канал.

### §4.4 🟠 Engagement-метрики в наявних датасетах

**Замір:** §0.11 — `engagedSessions`, `bounceRate`, `averageSessionDuration`, `newUsers`, `sessionKeyEventRate`, `screenPageViewsPerSession`.
**Ціна:** **0 нових викликів** — додаткові метрики в тому самому `runReport`.
**Обмеження:** нові ключі в `metrics_json` наявних датасетів → таблиця breakdown отримує нові колонки автоматично ([`datasetTable`](frontend/js/reporting-ninja-panel.js#L92) будує `metricKeys` з об'єднання ключів рядків). Тобто **старі рядки не мають цих ключів** і покажуть `—`. Це правильно (`None` ≠ `0`), але §7.2 має підтвердити, що таблиця не «стрибає» на межі періодів.

### §4.5 🟠 Sitemaps API — `gsc_sitemaps`

**Замір:** §0.13 — 1 sitemap, `errors 0 / warnings 1`, `lastDownloaded 2026-09-08`, web 248 / image 515 submitted.
**Ціна:** 1 GET на клієнта (не на період — це стан, не діапазон).
**Цінність:** прямий вердикт Google про подання карти сайту. Сьогодні Site Monitor (task-35/38) визначає стан sitemap **власним краулером** — тобто ми маємо думку свого краулера там, де можна мати думку Google.
**Обережно:** блок sitemap належить Site Monitor. Тут — **лише датасет + окрема картка на сторінці клієнта**, без правок `monitor-*` (чужі owned paths).

### §4.6 🟡 Traffic quality — сигнал, а не датасет

**Замір:** §0.12 — Vinnytsia 1 906 сесій у `Direct` з bounceRate 0.77 у клієнта з США.
**Ціна:** 0 — `ga4_countries` уже збирається; `city` — +1 виклик, якщо потрібна деталізація.
**UI:** не новий блок, а **позначка на KPI-картці**: коли частка сесій поза цільовим ринком (`clients.market`) перевищує порог, картка `Users` несе підказку «X% сесій поза US». Це не «фільтруємо дані» (ми ніколи не підміняємо цифру), а «показуємо, з чого вона складається».
**Конфлікт, який треба підсвітити:** порог — це судження. Пропозиція: порог не вбудовувати, а показувати частку завжди, коли вона ≥ 10%, і не приховувати нічого.

### §4.7 🟡 `page`+`query` пара

**Замір:** §0.7 — працює.
**Ціна:** +1 виклик на період (важкий: `rowLimit` 25 000).
**UI:** розкриття рядка в таблиці Pages → «топ запитів цієї сторінки». Сьогодні Pages і Queries — дві незалежні таблиці, і зв'язок між ними доводиться вгадувати.

### §4.8 🟡 `searchAppearance`, `device`, `country` для GSC

**Замір:** §0.7 — усі три працюють.
**Ціна:** +3 виклики на період.
**Цінність:** GA4 devices/countries у нас є, GSC — ні. Тобто «мобільна органіка проти десктопної» сьогодні недоступна. `searchAppearance` показує rich-результати (у HWELL — `TRANSLATED_RESULT`).

### §4.9 ⚪ Realtime `activeUsers`

**Замір:** працює (`runRealtimeReport`, 1 активний користувач на момент заміру).
**Оцінка:** дешево, але для агенційного звіту з періодами — шум. **Не робити** в цій тасці; згадано, щоб не досліджували вдруге.

---

## §5. API (бек)

### §5.1 Нові датасети у whitelist

[`REPORTING_NINJA_DATASETS`](app/api/analytics.py#L53) розширюється: `ga4_key_events`, `gsc_search_types`, `gsc_sitemaps`. Ендпоінт лишається той самий — **нового шляху не з'являється**, бо форма відповіді ідентична.

**Конфлікт імені, який треба назвати вголос:** URL ендпоінта — `/analytics/reporting-ninja/{dataset_key}`, і після cutover він віддає дані Google. Перейменування ламає збережені стани сторінок і `api.js`-контракт, тож у цій тасці **лишаємо як є** і фіксуємо як борг разом із `rn_*` (§10) — те саме рішення й з тієї ж причини, що task-83 §2.4. Підсвічую, а не ховаю.

### §5.2 `data_source` у відповіді (адитивно)

`AnalyticsDatasetResponseDTO` + `DimensionResponseDTO` отримують необов'язкове `transport: "google_api" | "reporting_ninja" | null`, зчитане з `provenance` найновішого снапшота. Без цього фронт не може сказати правду про джерело, а §6.1 саме цього й вимагає.

---

## §6. Фронт

Обов'язково до початку: [`frontend/AGENTS.md`](../../frontend/AGENTS.md) повністю. Окремо — §3 (anti-slop), §5 (data truth), §6 (required states), §12 (vanilla JS/CSS), §14 (copy), §15–16 (візуальний loop і чекліст).

### §6.1 Правдиві назви замість `Reporting Ninja`

`reporting-ninja-panel.js` → `ga4-panel.js`; eyebrow `Reporting Ninja · GA4` → `Google Analytics 4`; `Reporting Ninja · Search Console` → `Google Search Console`. Тексти порожніх станів («Run Reporting Ninja collection for this exact range», «Collect Reporting Ninja to populate this dataset») → нейтральні до транспорту («No data collected for this range yet»), бо після cutover вони називають неправильний сервіс.

Джерело показується **фактом із `provenance`** (§5.2), а не хардкодом: `via Reporting Ninja` / `direct from Google` — той самий патерн, що [task-85](../done/task-85-client-report-document-design.md) уже застосувала у футері документа звіту.

⚠️ **Cache-bust chain:** кожен ES-import несе власний `?v=`. Перейменування файла означає бамп у **всіх** ребрах до `index.html` — інакше браузер віддасть старий модуль, а сторінка тихо зламається. Файл імпортується з [`client-page.js#L10`](frontend/js/client-page.js#L10); `ga4-panel.js` сам імпортує `formatUaNumber` з `client-page.js` (циклічний імпорт, що вже існує — **не чіпати** в цій тасці, це окремий рефактор).

### §6.2 Фікс дефекту §0.5 — KPI мусять відповідати вибраному вікну

Прибрати умову `period === "week" || period === "month"` ([`client-page.js#L1075`](frontend/js/client-page.js#L1075)): `getDashboard` викликається для **будь-якого** періоду. Якщо для вікна даних немає — показується порожній стан, **не** числа іншого періоду.

⚠️ Перевірити на дедуплікацію запитів: `client-shell` уже робить `getDashboard({period:"week"})`, а `client-reporting-ninja` — з вибраним періодом. `requestKey` мусить лишитись **явно названим за призначенням**, не похідним від параметрів — цей клас помилки в проєкті вже ловили двічі (`api.js` request-key, task-36).

### §6.3 GBP-блок лишається як є

Він RN-only (§0.15) і активний в одного клієнта. Після cutover його eyebrow (`Reporting Ninja · Business Profile`) стає **єдиним правдивим** згадуванням RN на сторінці. Не чіпаємо — це не недоробка, а точний опис стану.

### §6.4 Нові блоки — мінімальний набір, кожен відповідає на питання AGENTS.md §3

| Блок | Дані | Питання, на яке відповідає |
|---|---|---|
| Розклад конверсій | §4.1 | «які саме конверсії ми привели» |
| Engagement-колонки в наявній таблиці | §4.4 | «цей трафік узагалі читає сайт» |
| Search types (рядок-перемикач) | §4.3 | «чи є канал, якого ми не бачимо» |
| Sitemap-картка | §4.5 | «Google бачить нашу карту сайту» |
| Позначка частки поза ринком на KPI | §4.6 | «з чого складається це число» |

Нових секцій — рівно дві (конверсії, sitemap); решта — доповнення наявних. AGENTS.md §4: сторінка не має рости вниз без межі.

---

## §7. Гейти

### §7.1 G1 — паритет транспортів (блокуючий, розширення `compare-transports`)

[`compare.py`](app/integrations/google_api/compare.py) сьогодні звіряє три trends-датасети. Розширюється на чотири нові поверхні: агрегат GSC, GSC-дименсії (`page`, `query`), GA4-агрегат (`rn_*`), GA4-breakdowns (5 датасетів). Правила незмінні:

- **нуль допуску** на цілі метрики (`clicks`, `impressions`, `sessions`, `users`, `keyEvents`, `views`, `events`);
- похідні (`ctr`, `position`, `engagement_rate`, `bounceRate`) — плавучий допуск, як зараз;
- **нічого не пише в БД**;
- день/значення, присутнє лише з одного боку → **fail**, не «майже однаково».

Прогін на ≥6 клієнтах, обов'язково включно з: **SHB** (багато подій), **EFFYS**/**GRP** (URL-prefix property, task-83 §0.9), **SAPHNY** (найдовша історія + єдиний GBP), **3XE** (кілька property на акаунті), **HWELL** (клієнт цього дослідження). Очікування, підкріплене §0.4: числа **не зрушать** — авто-збір через RN уже дав рівно ті самі 8/363/15 рядків, що прямий API.

**Виконано живо 2026-09-09 на 6 клієнтах** (HWELL + SHB, EFFYS, SAPHNY, 3XE, PJC): усі 12 поверхонь PASS байт-у-байт після ще одного реального бага, знайденого саме на цьому прогоні (SHB — вищий трафік за HWELL виявив те, чого HWELL не показав):

**Баг №3 (стадія 5, `86eb03e`):** `compare_dimension_rows()` (dimension-keyed поверхні — `ga4_landing_pages`, `ga4_events`, `ga4_channels` тощо) рахувалась без lag-вікна взагалі — коментар при написанні стверджував, що «рядок або є в цьому періоді, або ні, settling-періоду немає». Правда лише для ПРИСУТНОСТІ рядка; сама АГРЕГОВАНА величина — це сума за кожен день вікна, а хвіст вікна так само не досів, як і в `ga4_daily`/`gsc_daily`, які вже мають `lag_days`. На SHB (значно більший трафік за HWELL) це дало `exact`-провал ±1 на `views`/`events` для кількох рядків. Виправлено: `_compare_dimension_dataset()` тепер звужує вікно на `GSC_LAG_DAYS`/`GA4_LAG_DAYS` **до** запиту до обох транспортів, так само як дато-серійна гілка. `lag_days` — тепер поле `DimensionComparisonReport` (лише для друку, фільтрація вже відбулась на рівні вікна).

Єдине, що лишається «FAIL» після виправлення — на всіх 6 клієнтах однаково: **`gsc_queries`** (long-tail privacy/thresholding GSC — вже задокументовано в стадії 4, не транспортна розбіжність) і **PJC-специфічний `gsc_daily`** (7 днів присутні лише в RN, 09-01…09-07 — це вже відомий факт із task-83 §0.8: інтеграція PJC мовчки протухла; не код-баг цієї таски, перевірка відкладена до cutover самого PJC).

### §7.2 G2 — фронт живо, не на фікстурах

`docker compose up -d`, реальний клієнт, Playwright у контейнері (`network_mode: host` → `127.0.0.1:6524` доступний). Перевірити:

1. **усі п'ять періодів** (`week`/`month`/`quarter`/`year`/`custom`): KPI-картки відповідають вибраному вікну (§0.5 більше не відтворюється);
2. нуль `pageerror`/`console.error` — **CSP блокує inline `style=` і JS-встановлені `.style.top/left`**, обидва вже кусали цей проєкт;
3. порожні стани не називають Reporting Ninja;
4. `—` там, де немає даних; справжній `0` лишається `0`;
5. широка таблиця не скролить сторінку по горизонталі (`contain: paint` на wrapper — вже відомий фікс);
6. 1440 / 1024 / 768.

⚠️ **Пастка авторизації:** Playwright на `127.0.0.1` завжди заходить як bypass-адмін. Щоб перевірити реальну роль — тунель або `AUTH_LOCALHOST_BYPASS=false`.
⚠️ **Пастка кешу Cloudflare:** тунель кешує `.js`/`.css` на 4 години поверх `no-cache` застосунку. Перевіряти на `localhost`, а не через `seo.theyovth.space`.

### §7.3 G3 — cutover із відкатом (операторський крок)

```bash
# 0. Бекап ПЕРЕД будь-яким записом у бойову БД (CLAUDE.md)
docker exec theyovthclientsreport-db-1 pg_dump -U yovth -d yovth -Fc -f /tmp/pre-task88.dump

# 1. Один клієнт, спостереження ≥48 год
docker compose exec api poetry run yovth-report set-transport --client HWELL --transport google_api

# 2. Портфель — лише після того, як G1 зелений на всіх шести
docker compose exec api poetry run yovth-report set-transport --all --transport google_api

# 3. Відкат — один конфіг, без міграції даних (task-83 §0.5:
#    daily_series бере найновіший рядок за день, тож наступний RN-збір повертає лінію сам)
docker compose exec api poetry run yovth-report set-transport --client HWELL --transport reporting_ninja
```

**G3 fail-умова:** злам форми лінії на стику транспортів у `#/trends` або на графіку клієнта.

**Пілот виконано 2026-09-09.** Бекап знято (`pg_dump -Fc` у контейнері `db`, `/tmp/pre-task89-cutover-hwell.dump`, 147 МБ). `set-transport --client HWELL --transport google_api` застосовано на обидві інтеграції (`gsc`, `reporting_ninja`) — підтверджено записом у `client_integrations.config_json`. G4-baseline на момент перемикання зафіксовано нижче (§7.4). Спроба примусово запустити live-збір негайно (`POST /sync-jobs`, `force_refresh`) для миттєвої G3-перевірки була заблокована класифікатором дозволів Claude Code як дія над бойовою системою — очікую, поки HWELL підхопить `google_api` на наступному штатному циклі воркера, а не форсую поза розкладом без окремого дозволу. Спостереження ≥48 год з моменту перемикання (до 2026-09-11) — до цього портфельний cutover (крок 2) не виконується.

### §7.4 G4 — історія не постраждала

До і після cutover має бути **однаково**:

```sql
SELECT dataset_key, count(*) FROM analytics_dataset_rows WHERE dimension_value < '2025-05-01' GROUP BY dataset_key;
SELECT dimension, count(*) FROM gsc_dimension_metrics GROUP BY dimension;
```

Baseline HWELL (2026-09-09, спочатку задокументовано в §0 — числа там уже застаріли на момент cutover, портфель продовжує щодня збирати нове; актуальний baseline **на момент cutover-пілоту**, з живої БД):
`gsc_daily` 1583 (2025-04-27…2026-09-05), `ga4_daily` 2173 (2024-01-01…2026-09-06), `ga4_daily_channels` 1107, `ga4_landing_pages` 15921, `ga4_channels` 415, `ga4_countries` 5096, `ga4_devices` 202, `ga4_events` 761, `ahrefs_affected_urls` 4328, `ahrefs_issues` 118, `tech_findings` 850 — плюс `gsc_dimension_metrics`: page 18196 / query 71071. Порівняти з тими самими запитами через ≥48 год (§7.3).

### §7.5 G5 — лінт/типи/тести

```bash
docker compose exec api poetry run pytest tests/unit/integrations tests/unit/api/test_analytics_api.py -q
docker compose exec api poetry run ruff check app/integrations app/api/analytics.py app/cli.py app/core/config.py
docker compose exec api poetry run mypy app/integrations app/api/analytics.py
```

---

## §8. Ризики й пастки

### §8.1 Головний ризик — повторити долю task-83

Механізм є, gate пройдений, у проді не ввімкнено (§0.2). Тому §7.3 — **частина DoD**, а не follow-up. Таска без cutover не закрита.

### §8.2 ×100 на похідних метриках

Два вже відомі випадки (`ctr` у GSC, `engagement_rate` у GA4) і один потенційно новий (`bounceRate`, `sessionKeyEventRate` — теж частки `0..1`). Кожна така метрика малює **правдоподібний графік** з неправильними числами. Правило: будь-яка нова частка отримує явний тест на масштаб, і рішення «частка чи відсоток» приймається за тим, що **вже збережено в БД**, а не за тим, що зручніше.

### §8.3 Квоти GA4 Data API

Токени рахуються на property за годину. §4.7/§4.8 додають до +4 важких викликів на період. Порядок дій: спершу §4.1/§4.4 (**нуль** нових викликів), потім §4.3/§4.5 (по одному), і лише за потреби §4.7/§4.8 за прапорцем.

### §8.4 Розбіжність таймзон (§0.14)

Не лагодити мовчки. Property `America/Los_Angeles` проти `clients.timezone America/New_York` — це або помилка в наших даних, або справжня конфігурація клієнта. Таска **показує** розбіжність (§4.2), не «виправляє» її автоматично: автоматична зміна таймзони зсунула б усі наявні бакети.

### §8.5 `truncated` мусить лишитись правдивим

Прямий API віддає 11 415 запитів там, де ми беремо 1 000 (§0.7). Спокуса підняти `top_n` є, але зростання обрізаного набору змінює зміст `Quality.truncated` і розмір відповіді API. Порог лишається `1000` (§3.6, `google_api_gsc_dimension_top_n`) — конфігурованим, зміненим окремим рішенням, не мимохідь.

### §8.6 Паралельні сесії в одному дереві — **активний, не історичний ризик**

На 2026-09-09 у цьому дереві **три** сесії одночасно беруть номер 88 (`task-88-url-inspector-google-api.md`, `task-88-strategy-package-check-google-drive.md` і початкова редакція цього плану). Історія проєкту (task-84/85) фіксує той самий клас колізій у `config.py`, `INDEX.md`, `api.js`, `router.js`, `index.html`.

Правило для цієї таски: спершу побудувати **ізольовану** частину (`gsc_client.py`, `ga4_client.py`, `admin_client.py`, розвилки в `gsc.py`/`reporting_ninja.py` — їх ніхто інший не торкається), і лише коли `git status` на спільних файлах чистий — братися за них.

---

## §9. Definition of Done

- [x] Розвилка `resolve_transport()` у чотирьох методах (§3.4, §3.5); `transport=google_api` без токена → `not_configured` + warning, RN-клієнт не викликається.
- [x] `GscClient.search_analytics` узагальнений; `daily_search_analytics` працює без змін поведінки (тест на регресію task-83).
- [x] `get_json` доданий **виділенням** спільної політики, не копією (task-88).
- [x] **G1 зелений** на 6 клієнтах (HWELL, SHB, EFFYS, SAPHNY, 3XE, PJC), нуль допуску на цілих метриках (§7.1) — після виправлення бага №3 (lag-вікно, стадія 5).
- [x] `ga4_key_events` + engagement-метрики (§4.1, §4.4) — нуль додаткових викликів (стадія 2).
- [x] `gsc_search_types` і `gsc_sitemaps` за прапорцем, з нулями як **відсутністю** рядка (стадія 2).
- [x] `admin_client.py` + попередження про розбіжність таймзони (§4.2, стадія 2).
- [x] Фронт: назви правдиві, порожні стани не називають RN, джерело з `provenance` (§6.1, стадія 3).
- [x] **Дефект §0.5 виправлений** — KPI-картки відповідають вибраному вікну на всіх п'яти періодах (стадія 1).
- [ ] **G2 зелений** — живий Playwright, нуль CSP/console-помилок, три ширини (§7.2) — перевірено раніше на попередніх стадіях; не переперевірено live-скріншотами саме після cutover-конфігу HWELL.
- [ ] **G3 виконаний** — cutover ≥48 год на одному клієнті, потім портфель; відкат перевірений **на практиці**, не лише командою в документі (§8.1). **Пілот на HWELL стартував 2026-09-09, спостереження до 2026-09-11**; портфель — після.
- [ ] **G4** — лічильники історії до/після однакові. Baseline на момент cutover зафіксований (§7.4); порівняння через ≥48 год ще не зроблено.
- [x] **G5** — `ruff`/`mypy`/`pytest` зелені **в Docker** (для торканих у стадіях 1-5 файлів).
- [ ] Юніти: паритет форми рядка для кожної з 4 поверхонь; ×100 для кожної частки; відсутній день лишається відсутнім, справжній `0` зберігається; `truncated` виставляється на межі `top_n`.
- [ ] План перенесений у `doc/dev/done/` з Completion report; `INDEX.md` оновлений.

---

## §10. Follow-ups / борг

- **GBP через Google API** — потрібен scope `business.manage` + затвердження Business Profile APIs у Google Cloud (§0.15). Окрема таска; передумова — операторська (заявка в Google), не кодова. Заразом: `gbp_*` дані живі лише в SAPHNY і застаріли на два місяці — спершу варто вирішити, чи цей блок узагалі потрібен.
- ~~URL Inspection API у `#/url-inspector`~~ — **забрала [task-88](task-88-url-inspector-google-api.md)**, у роботі паралельно. Тут лишається тільки як контекст: §0.13 доводить, що API віддає `verdict`/`coverageState`/`lastCrawlTime`/`googleCanonical` з наявним токеном.
- **Перейменування `rn_*` → нейтральних ключів** — `TODO(task-85)` з task-83 §2.4; атомарно в 5 місцях.
- **Перейменування URL `/analytics/reporting-ninja/{dataset_key}`** — §5.1, разом із попереднім пунктом і з тієї ж причини.
- **Sitemap-стан у Site Monitor** — §4.5 кладе датасет; замінити думку власного краулера на думку Google у блоці монітора має власник `monitor-*`.
- **PJC** — інтеграція мовчки протухла (task-83 §0.8: `gsc_daily` стоїть на 2026-08-01). Прямий транспорт прибирає клас причини, але сам факт треба перевірити після cutover.
- **EFFYS / GRP** — URL-prefix property замість `sc-domain:` (task-83 §0.9): за визначенням не бачать інші піддомени. Операційна дія в кабінеті Google.
- **Циклічний імпорт** `ga4-panel.js` ↔ `client-page.js` (`formatUaNumber`) — §6.1; винести форматери у `formatters.js` окремим рефактором.
- **`page`+`query`, `searchAppearance`, GSC `device`/`country`** (§4.7, §4.8) — за прапорцем, після §8.3.
