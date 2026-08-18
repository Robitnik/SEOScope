# task-44 — Trends: метрики конверсій (Total / Organic / Direct) + правда в бакетах

- **Пакет:** три нові метрики на сторінці Portfolio Trends — `Total Conversions`, `Organic Conversions`, `Direct Conversions` — із денною історією, тією самою атрибуцією, що й клієнтський звіт (task-42), і виправленням чотирьох дефектів, які нові метрики роблять видимими (нульова база в `Indexed`, `Δ` при нулі, «not connected» замість «no data», сумування неадитивної метрики).
- **Хвиля:** J — Portfolio Trends (доповнення до закритої task-28).
- **Depends on:** **28** ✅ (сторінка й двигун), **09** ✅ (адаптер Reporting Ninja), **30** ✅ (Refresh data). Узгоджується з **42** (той самий вибір атрибуції) — але **не блокується** нею: файли не перетинаються.
- **PLAN.md:** §6 (модель даних), §10 (REST-конвенції), §12 (`None` ≠ `0`; метрики snapshot і range не рівні), §21.
- **frontend/AGENTS.md:** **обов'язковий** — таска чіпає `frontend/js/trends-*.js` і `frontend/css/trends.css`.
- **Міграції:** **не потрібні.** Нові дані живуть у `metrics_json` наявної таблиці `analytics_dataset_rows`.

---

## Мета

Сторінка Trends сьогодні відповідає на питання «як росте трафік» (Clicks, Impressions, Sessions, Users), але не на головне питання агенції — **«як росте результат»**. Конверсія — те, за що клієнт платить, і саме її немає в портфельному зрізі: щоб побачити, чи падають конверсії у клієнта, треба відкрити його картку або дочекатись місячного звіту.

Закриваємо: **три метрики конверсій у тому самому зрізі — 12 клієнтів на одному графіку, day/week/month, до 400 днів історії — з гарантією, що місячний бакет у Trends дорівнює колонці того самого місяця у клієнтському звіті.**

---

## 1. Дослідження (усе виміряно наживо 2026-08-17: контейнер `api`, база проєкту, Reporting Ninja API)

### 1.1 `Total Conversions` уже зібраний — і має історію з 2024 року

Датасет `ga4_daily` пише `conversions` (`keyEvents`) у `metrics_json` **з першого дня** ([reporting_ninja.py:58](../../../app/integrations/reporting_ninja.py#L58)):

| Перевірка | Результат |
|---|---|
| Рядків `ga4_daily` у базі | **10 181**, `2024-01-01 → 2026-08-16` |
| Рядків із заповненим `conversions` | **10 181 / 10 181 (100 %)** — 732/732 за 2024, 1095/1095 за 2025, 8354/8354 за 2026 |
| Ключі в `metrics_json` | `users`, `sessions`, `conversions`, `engagement_rate` |

А `GET /analytics/portfolio-series` повністю параметризований: `metric → SeriesSpec → (dataset_key, field)`, і [api/portfolio.py:170](../../../app/api/portfolio.py#L170) читає `row.metrics_json.get(spec.field)`. `CATALOG` уже містить `rn_total_conversions` з лейблом `Total Conversions` ([catalog.py:156](../../../app/metrics/catalog.py#L156)).

**Отже `Total Conversions` — це один запис у `PORTFOLIO_SERIES` і один у фронтовому `METRICS`.** Нуль змін у збиранні, нуль backfill, повна історія одразу. Це перший, найдешевший крок таски.

### 1.2 `Organic` / `Direct Conversions` денно **не існують** — але добуваються одним запитом

`ga4_channels` розбиває по каналах, але **не по днях** (dimension = канал, один рядок на канал за період). Тобто денного зрізу organic/direct у базі немає взагалі.

Живий вимір Reporting Ninja API — **дві розмірності в одному запиті працюють**:

```
fields = ["date", "firstUserDefaultChannelGroup", "totalUsers", "keyEvents"]
```

| Клієнт | Рядків | Днів | Час | Каналів |
|---|---|---|---|---|
| EFFYS (400-денне вікно) | 1 062 | 279 | **2,9 с** | 7 |
| SAPHNY (400-денне вікно) | 2 902 | 400 | **3,6 с** | 14 |

`truncated=false` в обох. Тобто **вся 400-денна історія organic/direct добувається одним запитом на клієнта за ~3 с** — backfill усього портфеля ≈ 50 с.

### 1.3 Яка атрибуція: `firstUserDefaultChannelGroup` (та сама, що в клієнтському звіті)

Наявний `ga4_channels` використовує `sessionDefaultChannelGroup`. Клієнтський звіт (task-42, §1.3 того плану) — `firstUserDefaultChannelGroup`. На EFFYS/липень 2026 вони розходяться на **5–15 %** на organic/direct.

Беремо **`firstUserDefaultChannelGroup`**, бо:
- це визначення, яке клієнт бачить у своєму PDF-звіті й у Reports (task-42);
- CLAUDE.md прямо вимагає: «провайдер/метрика називаються **однаково** скрізь». `Organic Conversions` у Trends, що не дорівнює `Organic Conversions` у звіті, — це не друга метрика, це витрачений тиждень чиїхось пошуків привида.

> **Свідомий борг:** `ga4_channels` (таб Channels на сторінці клієнта) лишається на `sessionDefaultChannelGroup`. Ми **не** переписуємо його в цій тасці: це змінило б семантику вже збережених 2 667 рядків. Розбіжність задокументована тут і виноситься окремим follow-up `TODO(task-46)`: звести таб Channels на ту саму атрибуцію.

### 1.4 🔴 Конверсії **адитивні**, `Users` — **ні**. І це вже ламає наявну метрику

Ключове вимірювання таски. EFFYS, липень 2026 — сума денних значень проти агрегату за місяць:

| Метрика | Сума по днях | Агрегат за місяць | Збіг |
|---|---|---|---|
| Total Conversions (`keyEvents`) | **1 099** | **1 099** | ✅ точний |
| Organic Conversions | **654** | **654** | ✅ точний |
| Direct Conversions | **391** | **391** | ✅ точний |
| Users (всього) | 7 335 | **6 251** | ❌ **+17,3 %** |
| Organic Users | 3 743 | **3 061** | ❌ **+22,3 %** |

Причина зрозуміла: key events рахуються по подіях (сума по днях = сума за період), а users — це **унікальні** за період, і сумувати денні уніки означає рахувати одного відвідувача, що приходив 5 днів, як п'ятьох.

**Два наслідки, обидва в обсязі таски:**

1. **Для нових метрик це гарантія якості.** `aggregate_client_series` сумує денні значення в бакет ([portfolio.py:245](../../../app/metrics/portfolio.py#L245)) — для конверсій це математично правильно, і місячний бакет у Trends **дорівнює** колонці того самого місяця у звіті. Це стає приймальним тестом (§6).
2. **Для наявної метрики `Users` це дефект, який ми щойно знайшли.** У week/month-бакетах сторінка показує суму денних уніків: у скріншоті оператора `TOTAL (WINDOW) 23 489` за 365 днів для GRP — це не 23 489 користувачів. Переписувати збір `Users` на бакетний запит — окрема велика робота (це зміна семантики історії, а не рядок коду). Тому **в обсязі цієї таски** — мінімальне й чесне: `SeriesSpec.additive: bool`, і для неадитивної метрики в неденному бакеті сторінка показує явну ноту («Weekly/monthly values sum daily unique users and overstate the true unique count»). Повний фікс — `TODO(task-46)`.

### 1.5 `Indexed` мовчки губить лінію з нульовою базою — а конверсії нулюються часто

[trends-chart.js:163](../../../frontend/js/trends-chart.js#L163):

```js
if (indexed) return baseValue ? (Number(point.value) / baseValue) * 100 : null;
```

`baseValue === 0` — falsy → **уся лінія стає `null`**. При цьому фільтр рядком вище пропускає такий клієнт (`0 !== null`), тож він **лишається в легенді з порожньою лінією**. З Clicks/Users нульовий тиждень — екзотика; з `Organic Conversions` клієнт із нулем у базовому бакеті — норма. Тобто без фікса нова метрика в `Indexed` регулярно малювала б порожнечу без жодного пояснення.

Той самий клас у таблиці: [trends-page.js:557](../../../frontend/js/trends-page.js#L557) `previousValue ? (delta/previousValue)*100 : null` — перехід **0 → 5 конверсій** показується як `—`. Для метрики з малими числами абсолютна дельта важливіша за відсоток.

### 1.6 🔴 «NOT CONNECTED» показується клієнтам, у яких інтеграція **підключена**

У скріншоті оператора `ALPHA`, `GOREPORT`, `LAXTON` позначені `NOT CONNECTED`. Перевірка бази:

| Клієнт | status | enabled | `reporting_ninja` увімкнена | рядків `ga4_daily` |
|---|---|---|---|---|
| ALPHA | active | 1 | **1** | **0** |
| GOREPORT | active | 1 | **1** | **0** |
| LAXTON | active | 1 | **1** | **0** |
| EFFYS | active | 1 | 1 | 717 |

API розрізняє ці стани коректно (`state=no_data` з reason «No daily history collected yet for this range») — і `renderExcludedList` теж. А **пікер клієнтів — ні**: [trends-page.js:500](../../../frontend/js/trends-page.js#L500) хардкодить `not connected` для будь-якого виключеного клієнта. Оператору кажуть іти лагодити інтеграцію, яка не зламана; насправді треба натиснути `Refresh data`.

Це вже третій випадок у проєкті класу «фронт склеїв два різні стани в один підпис», і нові метрики його загострюють: у клієнта може бути `ga4_daily`, але ще не бути `ga4_daily_channels` — тобто на Users він у графіку, а на Organic Conversions «not connected».

### 1.7 Обсяг і збереження

- Ongoing: +1 рядок на день на клієнта (**не** рядок на день × канал) → ~30–60 рядків на клієнта за прогон, як у `ga4_daily`. Для порівняння: `ga4_landing_pages` вже тримає 67 183 рядки.
- Backfill 400 днів × 14 клієнтів ≈ **5 600 рядків**, ~50 с, ~14 запитів.
- `+1` запит до GA4 на період на прогон (тобто `+2` на клієнта на прогон, ~0,6 с кожен на 30-денному вікні).

---

## 2. Архітектурні рішення

### 2.1 Новий датасет `ga4_daily_channels`, а **не** розширення `ga4_daily`

Спокуса була покласти `conversions_organic`/`conversions_direct` у `metrics_json` наявного `ga4_daily`. Відкинуто через одне правило: `daily_series` віддає **найновіший рядок на (клієнт, день)** ([analytics_datasets.py:198](../../../app/db/repositories/analytics_datasets.py#L198)). Тобто будь-який writer, що напише рядок без повного набору ключів, **зробить наявні `users`/`sessions` невидимими за той день** — а backfill за визначенням знає не все.

Тому: окремий `dataset_key="ga4_daily_channels"`, `dimension_key="date"`, `dimension_value` — звичайна ISO-дата.

Ціна цього рішення — нуль: `dimension_value` лишається датою, тож **`daily_series` працює без жодної зміни репозиторію**, а старі рядки `ga4_daily` фізично недосяжні для нового коду.

```
ga4_daily            metrics_json: {users, sessions, conversions, engagement_rate}   ← не чіпаємо
ga4_daily_channels   metrics_json: {conversions_organic, conversions_direct}         ← новий
```

### 2.2 `users_organic` / `users_direct` денно **не зберігаємо** — навмисно

Той самий запит віддає їх безкоштовно. Не зберігаємо саме тому: вони **неадитивні** (§1.4, +22 %), і збережене денне поле «Organic Users» — це заряджена пастка для наступного агента, який додасть його в `PORTFOLIO_SERIES` як «ще один рядок» і намалює завищені на чверть числа. Зберігаємо тільки те, що можна коректно просумувати.

### 2.3 Backfill — окрема CLI-команда, не розширення `Refresh data`

`Refresh data` на сторінці (task-30) створює `SyncJob` з `custom`-періодом на 14/30/90 днів по `gsc` + `reporting_ninja` — після цієї таски він автоматично наповнює й новий датасет за своє вікно. Але 400 днів через нього гнати не можна: це підняло б **усі** GA4-датасети (включно з `ga4_landing_pages`) за 400 днів.

Тому — точкова команда `yovth-report backfill-ga4-channels --days 400 [--client CODE] [--dry-run]`, яка тягне рівно один запит на клієнта й пише рядки під синтетичним прогоном. Прецедент є: `JobRepository.create_imported_run()` + `SnapshotRepository.create_immutable()` — саме так наповнює історію імпортер ([ai_seo_tools.py:305](../../../app/importers/ai_seo_tools.py#L305)).

### 2.4 Адаптер: окрема гілка, не generic-цикл

Цикл датасетів у `collect_analytics_datasets` побудований на **одній** розмірності: `[dimension, *metrics]` → рядок на значення розмірності ([reporting_ninja.py:341](../../../app/integrations/reporting_ninja.py#L341)). Двовимірний запит із півотом каналу в ключі метрик туда не вкладається. Робимо окрему гілку в тій самій функції — так, як там уже зроблено для GBP. `dataset_steps` у `PhaseTracker` збільшується на 2, щоб шкала прогресу лишалась вимірюваною (task-32).

---

## 3. Що саме реалізується

### 3.1 Три метрики

| `metric` key | Лейбл | dataset_key | field | additive | Джерело лейбла |
|---|---|---|---|---|---|
| `rn_total_conversions` | Total Conversions | `ga4_daily` | `conversions` | ✅ | `CATALOG` |
| `rn_organic_conversions` | Organic Conversions | `ga4_daily_channels` | `conversions_organic` | ✅ | `CATALOG` |
| `rn_direct_conversions` | Direct Conversions | `ga4_daily_channels` | `conversions_direct` | ✅ | явний `SeriesSpec` (у `CATALOG` ключа немає) |

`rn_direct_conversions` **не додається** в `CATALOG` — з тієї самої причини, що й `rn_sessions` ([portfolio.py:43](../../../app/metrics/portfolio.py#L43)): жоден адаптер не пише для нього `MetricValue`, тож запис у каталозі поставив би порожній рядок «Direct Conversions» на картку кожного клієнта.

`lag_days` — `GA4_LAG_DAYS = 2`, як в інших GA4-серій.

### 3.2 Збір

У `_GA4_DATASETS` додається опис нового датасету з двома розмірностями; в адаптері — гілка, що:
1. робить `query_all("ga4", …, ["date", "firstUserDefaultChannelGroup", "keyEvents"], …)`;
2. групує за днем, беручи рядки каналів `Organic Search` і `Direct`;
3. пише один рядок на день: `{conversions_organic, conversions_direct}`.

Правила:
- канал, відсутній у дні, дає **`None`, а не `0`** (PLAN §12) — але день, у якому канал є зі значенням `0`, зберігає `0`;
- день, якого немає у відповіді, рядка не отримує (відсутність ≠ нуль);
- `keyEvents` — той самий field id, що вже використовує `ga4_daily`, щоб total і його частини мали одне визначення;
- збій цього датасету йде у `warnings` і **не** валить решту збору (наявна поведінка циклу).

### 3.3 Правда в бакетах (`additive`)

`SeriesSpec` отримує `additive: bool`. `PortfolioMetricDTO` — те саме поле (адитивна зміна контракту, узгоджується як розширення task-28). `gsc_clicks`, `gsc_impressions`, `rn_sessions` і всі три конверсії — `True`; `rn_users` — `False`.

Фронт: при `additive === false` і `bucket !== "day"` під графіком з'являється нота, і в таблиці колонка `TOTAL (WINDOW)` для такої метрики підписується як сума денних значень, а не як унікальні. Нічого не приховуємо і нічого не «виправляємо» тихо.

### 3.4 Чотири фікси, яких вимагають нові метрики

| Дефект | Фікс |
|---|---|
| `Indexed`: база `0` → порожня лінія в легенді (§1.5) | клієнт із нульовою базою **виключається** з `Indexed` з іменем у ноті («N client(s) had 0 in the baseline period and cannot be indexed»), а не малюється порожнім |
| Таблиця `Δ`: previous `0` → `—` (§1.5) | коли % невизначений, показуємо абсолютну дельту (`+5`), а тренд-тон беремо з неї |
| Пікер: `no_data` підписаний як `not connected` (§1.6) | підпис із `item.state` (`no data yet` / `not connected`), `title` — з `item.reason`; ті самі `STATE_META`, що вже використовує `renderExcludedList` |
| 7 метрик в одному сегментному контролі | METRIC-група розділяється за провайдером (Search Console \| GA4) з підписами; на вузьких екранах — перенос без горизонтального скролу (AGENTS §11) |

---

## 4. Owned paths

```
app/metrics/portfolio.py                    (+3 SeriesSpec, +`additive`)
app/integrations/reporting_ninja.py         (+датасет `ga4_daily_channels`)
app/schemas/api.py                          (+`additive` у PortfolioMetricDTO)
app/cli.py                                  (+`backfill-ga4-channels`)
frontend/js/trends-page.js                  (метрики, нота, Δ, пікер)
frontend/js/trends-chart.js                 (Indexed із нульовою базою)
frontend/css/trends.css                     (група METRIC, нота)
tests/unit/metrics/test_portfolio.py        (додатки)
tests/unit/integrations/test_reporting_ninja.py (додатки)
tests/unit/api/test_portfolio_api.py        (додатки)
tests/e2e/frontend/trends.cjs               (додатки)
frontend/fixtures/                          (фікстури нових метрик)
```

**Перетинів із task-42 / task-43 немає** (ті володіють `app/reports/**`, `app/api/reports.py`, `frontend/js/reports-*.js`). `app/api/portfolio.py` **не змінюється взагалі** — ендпоінт уже параметризований. `api.js` не змінюється: новий метрик — це значення параметра, а не новий виклик.

Успадковані зони (`portfolio.py` — task-28, `reporting_ninja.py` — task-09, `cli.py` — task-07, `schemas/api.py` — task-15): усі дотики **суто адитивні**, наявна поведінка не змінюється.

---

## 5. Тести

**Unit:**
- `portfolio` — три нові `SeriesSpec` (лейбли з `CATALOG`, де є ключ), `additive` у всіх серіях, сумування конверсій у week/month-бакети, `None` для дня без даних проти справжнього `0`;
- `reporting_ninja` — на фейковому клієнті: двовимірна відповідь → один рядок на день; канал відсутній у дні → `None`, не `0`; канал зі значенням `0` → `0`; збій датасету → `warning` без падіння решти; `dataset_steps` збільшився на 2;
- **інваріант:** `conversions_organic + conversions_direct <= conversions` для кожного дня (на фікстурі й у живому гейті);
- `cli` — backfill: `--dry-run` нічого не пише; повторний прогін не дублює днів; синтетичний job/snapshot створюється як в імпортері;
- API — `GET /analytics/portfolio-series?metric=rn_organic_conversions` віддає повний DTO з `additive`; невідомий метрик → 404; `openapi_snapshot.json` оновлено.

**Frontend e2e (`trends.cjs`):** фікстури на три нові метрики × три бакети; `Indexed` із нульовою базою (лінія не малюється порожньою, є нота); `Δ` при previous `0`; пікер розрізняє `no_data` і `not_configured`; нота неадитивності на `Users`+`Week`; 7 кнопок METRIC на 390 px без горизонтального скролу.

**Живий гейт (обов'язковий):**
1. `backfill-ga4-channels --days 400` на реальному портфелі: ≈50 с, рядки для 11 клієнтів із GA4-історією.
2. **Крос-перевірка з task-42:** місячний бакет `Organic Conversions` для EFFYS/липень 2026 у Trends = **654**, тобто рівно колонка `Organic Conversions / This Month` у звіті. Так само `Direct` = **391**, `Total` = **1 099**.
3. `ALPHA`/`GOREPORT`/`LAXTON` показані як `no data yet`, **не** `not connected`.

---

## 6. Definition of Done / Gate

- [ ] Три кнопки метрик на сторінці, кожна малює лінії на день/тиждень/місяць і на діапазонах 7…365 днів.
- [ ] `Total Conversions` працює **без backfill** і показує історію з 2024 року там, де вона є (SAPHNY).
- [ ] `Organic` + `Direct` після backfill мають 400 днів історії; сума за місяць **точно** дорівнює відповідній колонці клієнтського звіту (654 / 391 / 1099 на EFFYS/липень 2026).
- [ ] `conversions_organic + conversions_direct <= conversions` на всіх зібраних днях (перевірено запитом до бази).
- [ ] День без даних дає `—`, а справжній нульовий день — `0`. Ніде не навпаки.
- [ ] `Indexed` із нульовою базою: клієнт названий у ноті, порожньої лінії в легенді немає.
- [ ] Перехід `0 → N` у таблиці показує `+N`, а не `—`.
- [ ] Виключені клієнти з підключеною інтеграцією підписані `no data yet`; підказка радить `Refresh data`.
- [ ] `Users` у week/month має видиму ноту про сумування денних уніків.
- [ ] Звичайний `Refresh data` (30 днів) наповнює новий датасет автоматично — окремої дії не треба.
- [ ] `ruff check .`, `mypy app`, `pytest` зелені; `openapi_snapshot.json` оновлено.
- [ ] Чекліст `frontend/AGENTS.md §16` пройдено; скріншоти 390/768/1440 переглянуті.
- [ ] `TODO(task-46)` зафіксовані у Completion report: (а) `ga4_channels` на `firstUserDefaultChannelGroup`, (б) бакетний збір `Users` замість сумування денних уніків.

---

## 7. Порядок робіт

1. **`Total Conversions`** — `SeriesSpec` + кнопка. Найдешевша третина цінності, працює на наявних даних, перевіряється тим самим днем.
2. `additive` у `SeriesSpec`/DTO + нота на фронті — фундамент довіри до решти.
3. Датасет `ga4_daily_channels` в адаптері + юніти.
4. CLI backfill + живий прогін на 400 днів.
5. `Organic` / `Direct` у `PORTFOLIO_SERIES` + кнопки + крос-перевірка зі звітом (§5, гейт 2).
6. Чотири фікси §3.4 і візуальний цикл AGENTS §15.

---

## ✅ Completion report

- **Дата:** 2026-08-17
- **Гілка / комміт:** `task-34-client-lifecycle-status` (робоче дерево поверх `eda4d1f`, не закомічено — див. «Follow-ups»)
- **Статус:** done

### Що зроблено

Три нові метрики конверсій у Trends (`Total`, `Organic`, `Direct Conversions`), новий датасет `ga4_daily_channels` (та сама атрибуція `firstUserDefaultChannelGroup`, що й у клієнтському звіті task-42), точкова CLI-команда `backfill-ga4-channels` та всі чотири фікси §3.4. Живий гейт §6 пройдено повністю: місячний бакет Trends для EFFYS/липень 2026 дав рівно **654 / 391 / 1099** — той самий прогін, яким користувач бачив би колонку клієнтського звіту.

### Як зроблено (ключові рішення)

- **Бекенд:** `SeriesSpec.additive: bool` (task-44) — `_from_catalog()` тепер приймає `additive` явним ключовим аргументом; `rn_direct_conversions` описаний прямим `SeriesSpec`, як і `rn_sessions` до нього, бо каталог для нього порожній. `app/metrics/portfolio.py:36-172`.
- **Адаптер:** `ga4_daily_channels` — **не** запис у `_GA4_DATASETS` (та структура — одна розмірність), а окрема гілка в `_collect_datasets`, що виконується по одному разу на роль (current/previous), плюс перевикористовуваний `query_daily_channels()` — та сама функція, яку викликає й адаптер (по одному періоду), і CLI-бекфіл (по всьому вікну за раз). Правило `None`/`0` (PLAN §12) реалізовано буквально: канал, відсутній у дні відповіді, дає `None`; канал з `keyEvents=0` дає `0`; день, якого немає в жодному рядку відповіді, не отримує рядка взагалі. `app/integrations/reporting_ninja.py:121-186, 484-531`.
- **CLI:** `backfill-ga4-channels --days 400 [--client ID] [--dry-run]` — рівно один запит на клієнта, синтетичний прогон через `JobRepository.create_imported_run` + `SnapshotRepository.create_immutable` (той самий прецедент, що й в імпортері), ідемпотентний: дні, вже наявні в `daily_series()` за це вікно, не перезаписуються. `app/cli.py`.
- **Контракт:** `PortfolioMetricDTO.additive: bool = True` — дефолт `True` (усі метрики до task-44 адитивні), `rn_users` передається явно з `False`. `app/api/portfolio.py` довелося torkнути на один рядок (`additive=spec.additive`) всупереч початковому формулюванню плану «не змінюється взагалі» — цей файл явно (`key=..., label=..., ..., lag_days=spec.lag_days`) конструює DTO по полях, і той самий прецедент уже стався для `lag_days` у task-30. Задокументовано тут, а не мовчки: дотик суто адитивний, жодної бізнес-логіки не змінено.
- **Фронт:** `trends-chart.js` — фікс `Indexed` із нульовою базою: раніше `baseValue ? ... : null` малювало **всю лінію** порожньою для клієнта з реальним `0` у базовому бакеті, залишаючи його в легенді; тепер такий клієнт виключається з `datasets` **до** побудови, а `zeroBaselineClients()` (новий експорт) дає сторінці список імен для ноти. `trends-page.js` — 7 кнопок METRIC згруповано за провайдером (`METRIC_GROUPS`, той самий делегований клік на `#tr-metric`, жодних змін у `bindControls`); `additiveNote()`/`indexedZeroBaselineNote()` рендерять явні ноти; `Δ` у таблиці показує `+N` замість `—`, коли previous є `0`, а не відсутній; пікер розрізняє `no_data` («no data yet») і `not_configured` («not connected») тим самим `stateClass()`, що й `renderExcludedList`.
- **Скрізь:** cache-buster у query-рядку імпортів фронтенду піднято з `?v=20260814-media1` до `?v=20260817-conv1` — той самий репозиторний конвеншн, що й попередні таски (task-39), торкається всіх `frontend/js/*.js`, суто механічно.
- **Відхилення від плану:** `app/api/portfolio.py` таки змінено (один рядок) — обґрунтування вище. Формулювання «не змінюється взагалі» в §2.1/§4 плану було неточним.

### Контрактні зміни (Contract changes)

- `PortfolioMetricDTO` (`app/schemas/api.py`) отримав нове поле `additive: bool = True` — зворотньо сумісне (дефолт), споживає `frontend/js/trends-page.js`.
- `SeriesSpec` (`app/metrics/portfolio.py`, внутрішній тип, не публічний контракт) отримав поле `additive: bool`.
- Новий `dataset_key="ga4_daily_channels"` у таблиці `analytics_dataset_rows` (без міграції — та сама таблиця, що й усі інші датасети).
- Новий CLI-підкоманда `backfill-ga4-channels`.
- `openapi_snapshot.json` перегенеровано (`docker compose exec api poetry run python /app/tests/unit/api/test_openapi_snapshot.py` — канонічна команда з докстрінга файлу не спрацювала через колізію просторів імен `tests` між цим репо й пінованим `AISeoTools` на `sys.path`; прямий запуск скрипта як файл, а не як `-m`, обходить це). **Застереження:** файл — згенерований артефакт усього застосунку; на момент фіксації в цій самій робочій копії паралельно велась робота над task-42/43 (репорти), тож перегенерований знімок відображає й їхні ще незакомічені зміни API. Це не є регресією цієї таски, але перегенерувати ще раз перед комітом варто, якщо стан застосунку зміниться.

### Як перевірити (verification)

```bash
# юніти цієї таски
docker compose exec api poetry run pytest tests/unit/metrics/test_portfolio.py tests/unit/integrations/test_reporting_ninja.py tests/unit/api/test_portfolio_api.py -q

# lint/types на власних файлах
docker compose exec api poetry run ruff check app/metrics/portfolio.py app/schemas/api.py app/api/portfolio.py app/integrations/reporting_ninja.py app/cli.py
docker compose exec api poetry run mypy app/metrics/portfolio.py app/schemas/api.py app/api/portfolio.py app/integrations/reporting_ninja.py app/cli.py

# живий бекфіл (метровано! ~12 клієнтів × 1 запит)
docker compose exec api poetry run python -m app.cli backfill-ga4-channels --dry-run
docker compose exec api poetry run python -m app.cli backfill-ga4-channels --days 400

# крос-перевірка з клієнтським звітом (гейт §6, п.2) — має дати 654 / 391 / 1099
curl -s "http://127.0.0.1:6524/api/v1/analytics/portfolio-series?metric=rn_organic_conversions&bucket=month&start=2026-07-01&end=2026-07-31" | jq '.series[] | select(.name=="EFFYS") | .points[0].value'

# e2e (fixture-режим, живий стек має бути піднятий на :6524)
NODE_PATH=<playwright-core> BASE_URL=http://127.0.0.1:6524 node tests/e2e/frontend/trends.cjs
NODE_PATH=<playwright-core> BASE_URL=http://127.0.0.1:6524 node tests/e2e/frontend/trends-refresh.cjs
```

Живий гейт §6, усі три пункти перевірено фактично (не лише юніт-тестами):
1. `backfill-ga4-channels --days 400` на реальному портфелі: 12 клієнтів (з 14 — 2 без увімкненої/налаштованої Reporting Ninja), 3940 нових рядків за один прогін; повторний прогін ідемпотентний (`3XE: 274 day(s) fetched, already up to date`).
2. Крос-перевірка з task-42: `GET /analytics/portfolio-series?metric=rn_organic_conversions&bucket=month` для EFFYS/липень-2026 дав **654**; `rn_direct_conversions` — **391**; `rn_total_conversions` — **1099**. `conversions_organic + conversions_direct (1045) <= conversions (1099)` — інваріант тримається.
3. `ALPHA`/`GOREPORT`/`LAXTON` у живому (не fixture) режимі сторінки Trends дійсно показані як `NO DATA YET`, не `NOT CONNECTED` (перевірено Playwright-скриптом проти реального `#/trends` на порту 6524).

Додатково перевірено живим прогоном: звичайний `backfill-daily-history` (той самий шлях, що й кнопка «Refresh data») для EFFYS на 5-денному вікні наповнив `ga4_daily_channels` без жодної окремої дії — воркер обробив джобу за ~15 с і записав свіжі рядки.

### Тести

- `tests/unit/metrics/test_portfolio.py`: реєстр із 7 метрик, `additive` для кожної, `dataset_key`/`field` для всіх трьох конверсій, відсутність `rn_direct_conversions` у `CATALOG`, сумування конверсій у місячний бакет (детермінований аналог живого гейту).
- `tests/unit/integrations/test_reporting_ninja.py`: `query_daily_channels()` — канал відсутній → `None`, канал з `0` → `0`, день відсутній у відповіді → не з'являється; інваріант `organic + direct <= total`; збій датасету не валить решту (`_collect_datasets` без `ga4_daily_channels`, з warning); `dataset_steps` враховує `+2`. Існуючі `FakeClient.list_fields()` у двох тестах task-09 доповнено полем каналу (додаткова, не руйнівна зміна — узгоджено з дозволом «додатки» в owned paths); лічильники рядків/викликів оновлено на `+2`/`14`.
- `tests/unit/api/test_portfolio_api.py`: `additive` у відповіді API, `rn_organic_conversions`/`rn_direct_conversions` читають `ga4_daily_channels` і сумують коректно (включно з відсутнім днем ≠ `0`), `rn_direct_conversions` вибирається попри відсутність у `CATALOG`.
- `tests/e2e/frontend/trends.cjs` (новий): згрупований METRIC-контрол (7 кнопок, дві підписані групи, без горизонтального скролу на 390/768/1440), `Indexed` виключає клієнта з нульовою базою й називає його в ноті (перевірено і текстом, і напряму через `window.Chart.getChart(...).data.datasets`), нота про сумування уніків з'являється для `Users`/`week` і зникає для `Users`/`day` та для адитивної метрики, `Δ` показує `+6` при переході `0 → 6`, пікер розрізняє `no data yet`/`not connected`.
- `frontend/fixtures/trends-{normal,empty,partial,sparse}.json`: додано `additive` в усі існуючі метрики й три нові метрики-бандли; `trends-normal` містить навмисний нульовий базовий бакет (3XE, Organic Conversions) і навмисний перехід `0 → 6` (GOREPORT, Direct Conversions) — саме ці два сценарії ловлять дефекти §1.5.

### Follow-ups / борг

- `TODO(task-46)`: звести таб Channels клієнтської картки (`ga4_channels`, `sessionDefaultChannelGroup`) на ту саму атрибуцію `firstUserDefaultChannelGroup`, що й Trends/звіт — задокументовано розбіжність 5–15% у §1.3 плану, свідомо не займались тут, щоб не змінити семантику вже збережених рядків.
- `TODO(task-46)`: бакетний збір `Users` замість сумування денних уніків (нота на фронті — тимчасовий, чесний обхід, не фікс кореня; сумування завищує на 17–23% за виміром §1.4).
- Комміт не зроблено: git-safety-протокол цієї сесії комітить лише за прямим проханням користувача. Робоче дерево готове; `openapi_snapshot.json` варто перегенерувати ще раз перед фіксацією, якщо паралельна робота над task-42/43 (та сама гілка) просунеться далі.
