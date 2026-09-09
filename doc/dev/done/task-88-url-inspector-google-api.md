# task-88 — Сторінка `#/url`: перевести дані з Reporting Ninja на прямий Google API

- **Пакет:** URL Inspector переходить із «читаю те, що випадково зібралось у БД через Reporting Ninja» на «питаю Google про саме цей URL і саме це вікно». Заразом відкриваються дані, яких RN не вміє віддати в принципі — реальні пошукові запити сторінки та її стан індексації.
- **Хвиля:** пост-84 (джерела даних). Продовження лінії **83** (`#/trends`) → **84** (звіти) → **88** (`#/url`).
- **Depends on:** **83** ✅ (транспорт `GoogleApiSession`/`GscClient`/`Ga4Client`), **61** ✅ (URL Inspector), **64** ✅ (keywords-панель), **56** ✅ (schema markup — для звірки rich results). Не залежить від 75.
- **Owned paths:**
  - `app/urlinspector/google/**` (новий підпакет: клієнт-рівень запитів по одному URL)
  - `app/urlinspector/sources/google_gsc.py`, `app/urlinspector/sources/google_ga4.py`, `app/urlinspector/sources/google_index.py` (нові)
  - `app/urlinspector/service.py` (розвилка транспорту)
  - `app/api/url_inspector.py` (нові ендпоінти)
  - `app/schemas/url_inspector.py` (адитивні DTO)
  - `frontend/js/url-inspector.js`, `frontend/css/url-inspector.css`
  - `tests/unit/urlinspector/**`
- **Адитивні дотики спільних файлів** (перед комітом перевірити `git status`): `app/integrations/google_api/auth.py` (+`get_json`, §3.4), `app/integrations/google_api/__init__.py` (+експорт), `app/core/config.py` (2 поля), `frontend/js/api.js` (+2 методи), `frontend/js/router.js` (query-параметр вікна), `tests/unit/api/data/openapi_snapshot.json`.
- **Міграції:** **не потрібні** — обґрунтовано в §2.4 (жодне з нових даних не персиститься в цій тасці; історія стану індексації — свідомий follow-up, §9).
- **Нових залежностей — нуль.**
- **PLAN.md:** §12 (`None` ≠ `0` — тут це головна пастка, §0.3), §13 (жодного нового вихідного трафіку до недовіреного хоста), §10 (форма REST), §21.4.
- **frontend/AGENTS.md:** §5 (правда даних), §6 (обов'язкові стани), §11, §12 (CSP), §14 (UI англійською), §15–16.

---

## §0. Досліди на бойових даних (виміряно 2026-09-09, живий Google API + бойова Postgres)

Усе нижче — прогони на клієнті **HWELL** (`sc-domain:hearwellservices.com`, `properties/395442288`) і на URL із запиту користувача `https://hearwellservices.com/about-us/jessica-m-frankel/`. Скрипти — `/tmp/.../probe_*.py`, ключові виводи вставлені дослівно.

### §0.1 Що сторінка `#/url` показує сьогодні

`search_analytics`-панель читає рівно дві таблиці, обидві наповнені через Reporting Ninja:

| Панель | Таблиця | Хто пише | Метрики |
|---|---|---|---|
| Search Console | `gsc_dimension_metrics` (`dimension='page'`) | [gsc.py](app/integrations/gsc.py) через RN | clicks, impressions, ctr, position |
| Reporting Ninja / GA4 | `analytics_dataset_rows` (`ga4_landing_pages`) | [reporting_ninja.py](app/integrations/reporting_ninja.py) | sessions, users, conversions, views, engagement_rate |

Для нашого URL у БД лежить **31 GSC-рядок і 29 GA4-рядків**. Виглядає рясно — але це не серія:

```
2026-05-01..2026-05-31 clicks=0 impr=26 pos=11.96  created=2026-07-23 12:28
2026-05-01..2026-05-31 clicks=0 impr=26 pos=11.96  created=2026-07-23 12:32   ← дубль снапшота
2026-05-02..2026-07-30 clicks=1 impr=57 pos=10.98  created=2026-07-31 11:50   ← 90 днів
2026-05-19..2026-08-16 clicks=2 impr=63 pos=10.43  created=2026-08-17 13:16   ← 90 днів, інше вікно
2026-06-01..2026-06-30 clicks=1 impr=31 pos=10.16  created=2026-07-23 12:28   ← місяць
2026-06-05..2026-06-30 clicks=1 impr=28 pos=10.25  created=2026-07-27 12:20   ← обрізаний місяць
2026-06-15..2026-07-14 clicks=0 impr=5  pos=8.80   created=2026-08-14 08:34   ← 30 днів
2026-07-20..2026-08-02 clicks=0 impr=2  pos=6.50   created=2026-08-03 08:13   ← 14 днів
```

Це не часовий ряд, це **журнал того, коли випадково запускався збір**: вікна різної довжини, накладені одне на одне, з дублями снапшотів. Уся машинерія `build_series` / `other_length_periods` / `absence_reason` у [series.py](app/urlinspector/series.py) існує саме щоб цей мотлох було видно чесно — і вона працює правильно. Але вона лікує симптом.

**Друга, гірша проблема — усічення.** RN віддає top-N сторінок за період. Із власного плану task-61 §0.4: **83 % збережених GSC page-рядків цього портфеля мають `quality=truncated`**. Для сторінки-персоналії з 47 показами за 90 днів це означає, що вона просто не потрапляє у top-N більшості періодів — і панель показує `absence_reason=truncated`, тобто «ми не знаємо», хоча Google знає точно.

### §0.2 ⚠️ Головна знахідка: RN у принципі не вміє віддати запити сторінки, а Google API вміє

[sitequeries.py](app/urlkeywords/sitequeries.py) у власному докстрінгу фіксує вердикт task-64:

> NOT per-page GSC data — Reporting Ninja has no page x query cross for GSC (confirmed live, task-64 §0.2: a same-request `fields=["page","query",...]` is rejected outright with `You can only use one dimension at a time`, and a `{"filter": {"page": ...}}` body on a query-dimensioned request returns `200 OK` with IDENTICAL rows for a real page and a nonexistent one — a silent no-op, not a working filter).

Через це keywords-панель показує **сурогат**: топ-запити САЙТУ, відфільтровані за повним покриттям токенів текстом сторінки, з дисклеймером «це не запити цієї сторінки».

Прямий GSC API робить рівно те, що RN відмовляє. Виміряно на трьох найтрафіковіших сторінках HWELL:

| Сторінка | Запитів | Кліків | Топ-запит |
|---|---|---|---|
| `/blog/hearing-aid-dome-size-chart-tips-for-the-right-fit/` | **499** | 272 | `hearing aid dome size chart` — 124 кліки, 1750 показів, поз. 2.5 |
| `/blog/charging-hearing-aids-how-long-it-takes.../` | **568** | 109 | `how to charge hearing aids without charger` — 16 кліків, поз. 3.6 |
| `/blog/how-to-turn-off-rechargeable-hearing-aids.../` | **692** | 123 | `how to turn off phonak hearing aids` — 7 кліків, 1160 показів, поз. 6.7 |

Навіть наша тиха сторінка-персоналія дає 2 реальні запити (`hearing aid fitting in midtown manhattan` поз. 69, `hearing loss sewell` поз. 63) — обидва не мають до неї змістового стосунку, що саме по собі корисний сигнал.

**Це не «додаткова метрика». Це заміна сурогату на факт**, і найбільша окрема цінність цієї таски.

### §0.3 ⚠️⚠️ Пастка, яка тихо зіпсує позиції: GSC вигадує нульові рядки

GSC із фільтром по `page` і дименсією `date` віддає рядок на **кожен** день вікна, включно з днями, коли сторінка не мала жодного показу. Ці рядки виглядають як дані:

```json
{"keys": ["2026-06-09"], "clicks": 0, "impressions": 0, "ctr": 0, "position": 0}
```

`position: 0` — фізично неможливе значення. Виміряно на 365-денному вікні нашого URL:

| | Рядків |
|---|---|
| Віддано API | **365** |
| З них синтетичних (`impressions=0`, `position=0`) | **321** |
| Реальних | **44** |

**Наслідок, якщо взяти як є:**

| Середня позиція | Значення |
|---|---|
| По всіх 365 рядках (наївно) | **1.17** |
| По 44 реальних | **9.68** |

Тобто наївна реалізація намалювала б сторінку на **1-й позиції** замість 10-ї, і графік виглядав би абсолютно правдоподібно. Це той самий клас дефекту, що `ctr`×100 у task-83, тільки дорожчий — бо помилка не в масштабі, а в напрямку висновку.

Перевірено, що це артефакт саме фільтра, а не даних:

- запит із дименсією `page` **без** фільтра за той самий день (`2026-06-09`) віддає 199 сторінок, і нашого URL серед них **немає**;
- запит із дименсією `page` **без** фільтра за 90 днів віддає 294 сторінки, з них із нульовими показами — **0**.

**Правило для реалізації:** рядок з `impressions == 0` — це відсутність даних (`value=None`), а не нуль. `clicks == 0` при `impressions > 0` — справжній нуль і лишається нулем.

### §0.4 ⚠️ Кінцевий слеш ламає матчинг з обох боків, але по-різному

**GSC** — фільтр `equals` строго рядковий:

| Фільтр | Результат |
|---|---|
| `equals https://…/jessica-m-frankel/` | `clicks=3 impressions=47 position=9.55` ✅ |
| `equals https://…/jessica-m-frankel` (без слеша) | `clicks=0 impressions=0 position=0` ← синтетичний нуль, тобто «немає даних» |
| `includingRegex ^https://hearwellservices\.com/about-us/jessica-m-frankel/?$` | `clicks=3 impressions=47 position=9.55` ✅ |

Групи `dimensionFilterGroups` між собою AND-яться, тому «або зі слешем, або без» одним запитом дає **лише** `includingRegex`.

**GA4** — розкол живе всередині самого GA4, і різні дименсії поводяться по-різному. Виміряно за 90 днів:

| Дименсія + фільтр | sessions | users | views | engagementRate |
|---|---|---|---|---|
| `landingPagePlusQueryString` EXACT `/…frankel/` | 26 | 22 | 36 | 0.2308 |
| `landingPagePlusQueryString` IN_LIST обидва | **32** | 28 | 46 | 0.2813 |
| `landingPage` EXACT без слеша | **32** | 28 | 46 | 0.2813 |
| `landingPage` IN_LIST обидва | **32** | 28 | 46 | 0.2813 |
| `pagePath` EXACT `/…frankel/` | 45 | 38 | 47 | 0.5333 |
| `pagePath` IN_LIST обидва | **52** | 45 | 57 | 0.5385 |

Тобто:

- `landingPage` **сама** зливає варіанти зі слешем і без (`/about-us/jessica-m-frankel` = 32 = 26 + 6) — це правильна дименсія для landing-метрик, і `IN_LIST` на ній безпечний (той самий результат);
- `landingPagePlusQueryString` **не** зливає — EXACT по одному варіанту губить 6 із 32 сесій (**19 %**);
- `pagePath` **не** зливає — EXACT губить 7 із 52 сесій (**13 %**) і 10 із 57 переглядів (**18 %**).

**Правило:** `landingPage` для landing-метрик, `pagePath` для page-метрик, обидві — завжди через `inListFilter` з двома варіантами шляху (`ga4_landing_variants` уже їх дає).

### §0.5 URL Inspection API працює з наявним scope і закриває рядок, який task-61 позначив «ніде»

Таблиця мотивації task-61 має рядок: «Чи вона взагалі індексується зараз → **ніде — тільки живий curl**». Виявилось, що це є в API, і наявного scope `webmasters.readonly` для нього достатньо — нічого перевидавати не треба. Живий виклик `POST searchconsole.googleapis.com/v1/urlInspection/index:inspect`:

```json
{
  "indexStatusResult": {
    "verdict": "PASS",
    "coverageState": "Submitted and indexed",
    "robotsTxtState": "ALLOWED",
    "indexingState": "INDEXING_ALLOWED",
    "lastCrawlTime": "2026-08-25T01:19:08Z",
    "pageFetchState": "SUCCESSFUL",
    "googleCanonical": "https://hearwellservices.com/about-us/jessica-m-frankel/",
    "userCanonical": "https://hearwellservices.com/about-us/jessica-m-frankel/",
    "referringUrls": [
      "https://hearwellservices.com/blog/why-hearing-aids-stop-sounding-clear-how-accessories-can-help/",
      "https://hearwellservices.com/about-us/"
    ],
    "crawledAs": "MOBILE"
  },
  "mobileUsabilityResult": { "verdict": "VERDICT_UNSPECIFIED" },
  "richResultsResult": {
    "verdict": "PASS",
    "detectedItems": [{ "richResultType": "Breadcrumbs", "items": [{ "name": "Unnamed item" }] }]
  }
}
```

Плюс `inspectionResultLink` — глибокий лінк у сам GSC UI на цей URL.

**Ціна:** **6641 мс** на один виклик (виміряно). Це на порядок повільніше за все інше, тож у швидкий шлях він не йде (§2.2).

### §0.6 Латентність: швидкий набір вкладається в інтерактивний бюджет

Вісім запитів, потрібних для повної Google-панелі (365-денне вікно), один `httpx.Client`, теплий access-token:

| Запит | Послідовно |
|---|---|
| GSC date-серія 365 д | 472 мс (365 рядків) |
| GSC queries | 454 мс |
| GSC device | 326 мс |
| GSC country | 525 мс |
| GA4 landing date-серія | 1041 мс |
| GA4 channel | 1698 мс |
| GA4 device+country | 781 мс |
| GA4 page-scoped | 907 мс |
| **сума послідовно** | **6.2 с** |

Паралельно у 8 потоків, три послідовні прогони: **1570 мс → 589 мс → 418 мс**. Перший прогін платить за TLS-handshake і refresh токена, далі — пів секунди на всю панель.

Із URL Inspection у тому ж пулі wall clock стає **6602 мс** — тобто один цей виклик визначає всю затримку. Звідси розділення на два ендпоінти (§2.2).

### §0.7 Межі, які треба показувати як межі, а не як «немає даних»

| Межа | Поведінка API (виміряно) |
|---|---|
| GSC історія ~16 місяців | запит на 700 днів тому → `200 OK`, `rows=0`, **без помилки** |
| GA4 до створення property | запит на 700 днів тому → `200 OK`, `rows=0` |
| `dataState: "all"` | +2 свіжі дні понад `final` і `metadata.firstIncompleteDate: "2026-09-07"` |

Обидва нулі мовчазні. Якщо не рахувати вікно самостійно, оператор побачить «no data» там, де правильна відповідь — «за межею історії GSC».

### §0.8 Ліміти й несумісності GA4 (кожна дає той самий непрозорий 400 INVALID_ARGUMENT)

**⚠️ Перше вимірювання цієї секції було помилковим і виправлене бісекцією.** Початково запит із 11 метриками під фільтром `landingPagePlusQueryString` падав, і це прочиталось як «дименсія несумісна з набором метрик». Насправді причина інша й куди прозаїчніша — **жорсткі ліміти на розмір запиту**:

| Ліміт `runReport` | Виміряно |
|---|---|
| Метрик на запит | **10 — OK, 11 — 400** |
| Дименсій на запит | **8 — OK, 9 — 400** |

Тобто повний набір landing-метрик (15 полів) фізично не влазить в один запит і мусить різатись на пачки по ≤10. Жодної несумісності «дименсія × метрика» там не було: окремо перевірено **28 метрик по одній** під фільтром `landingPage` — усі 28 сумісні (`sessions`, `totalUsers`, `newUsers`, `activeUsers`, `engagedSessions`, `engagementRate`, `bounceRate`, `averageSessionDuration`, `userEngagementDuration`, `screenPageViews`, `screenPageViewsPerSession/PerUser`, `keyEvents`, `sessionKeyEventRate`, `userKeyEventRate`, `eventCount`, `eventsPerSession`, `eventCountPerUser`, `scrolledUsers`, `sessionsPerUser`, `active1/7/28DayUsers`, e-commerce-набір). E-commerce і `scrolledUsers` віддають `None`, не `0` — чесна відсутність, нічого підміняти не треба.

Справжні несумісності, що лишились після поправки:

| Що | Працює | Не працює |
|---|---|---|
| `organicGoogleSearchClicks/Impressions/AveragePosition/ClickThroughRate` | з `landingPagePlusQueryString` самою; з `date` самою | **без дименсій**; з `landingPage`; з `date` + `landingPagePlusQueryString` разом |
| Іменовані key events | `keyEvents:Phone`, `keyEvents:Email`, `keyEvents:purchase` | `keyEvents:Contact Us`, `keyEvents:Insurance Form` — **пробіл у назві валить запит**, і `%20` не рятує |
| `keyEvents` | з фільтром по `landingPage` → `1` | з фільтром по `pagePath` → `None` (конверсія не page-scoped) |

**Обхід для назв із пробілом** (перевірено — запит валідний): дименсія `eventName` + метрика `keyEvents` під `andGroup` із `landingPage`-фільтром і `inListFilter` за назвами подій. Дає ті самі числа для будь-якої назви, включно з пробілами, одним запитом на всі конверсії замість запиту на назву. Тому реалізація бере **лише** цей шлях, а `keyEvents:<name>` не використовує взагалі — інакше набір конверсій клієнта тихо різався б за наявністю пробілу.

`properties/{id}:checkCompatibility` працює (без `compatibilityFilter`), але з фільтром `INCOMPATIBLE` + `landingPage` сам падає на 400 — як інструмент авто-перевірки ненадійний, тому список сумісності зафіксований тестом (§8), а не запитом у рантаймі.

### §0.9 Метадані property: що взагалі доступно

`GET /v1beta/properties/395442288/metadata` → **375 дименсій, 101 метрика**. Кастомних `customEvent:*`/`customUser:*` у HWELL немає, але є **5 іменованих key events**, налаштованих клієнтом: `Contact Us`, `Email`, `Insurance Form`, `Phone`, `purchase`. Набір у кожного клієнта свій — тому список конверсій має читатися з `metadata`, а не бути прошитим (§2.3).

### §0.10 Вбудована звірка GSC↔GA4, яку дає сам Google

`organicGoogleSearch*` у GA4 (потребує GSC↔GA4-лінку) за 90 днів для нашого URL:

```
organicGoogleSearchClicks              = 3
organicGoogleSearchImpressions         = 47
organicGoogleSearchAveragePosition     = 9.5531914893617014
organicGoogleSearchClickThroughRate    = 0.063829787234042548
```

Прямий GSC API за той самий період: `clicks=3 impressions=47 position=9.553191489361701 ctr=0.06382978723404255`.

**Збіг до останньої значущої цифри, двома незалежними шляхами.** Це готовий вбудований інваріант: якщо ці два числа розійшлись — зламався наш join по URL, а не дані. Рівно та логіка, якої вимагав task-86 (арбітр — Google, не RN).

---

## §1. Мета

Панель Search Analytics на `#/url` перестає бути «зліпком того, що встиг зібрати RN» і стає **точною відповіддю Google на питання про цей URL і це вікно**: денна серія без усічення, реальні запити сторінки, стан індексації, зрізи за пристроєм/країною/каналом.

| Питання оператора | Сьогодні | Після таски |
|---|---|---|
| Скільки кліків мала сторінка за серпень | «залежить, який період зібрався»; 83 % рядків `truncated` | точна цифра за будь-яке вікно ≤16 міс |
| Яка в неї середня позиція | іноді `truncated`, іноді значення з чужого 90-денного вікна | денна серія, без синтетичних нулів |
| **За якими запитами її знаходять** | **сурогат: запити САЙТУ, збіглі за токенами** | **реальні запити сторінки з кліками/позиціями** |
| **Чи вона в індексі** | **ніде** | `coverageState`, `lastCrawlTime`, `robotsTxtState`, канонікал |
| З якого каналу на неї приходять | немає | Organic / Direct / Referral + source/medium |
| Куди йдуть далі | немає | наступні сторінки за `pageReferrer` |

---

## §2. Архітектура

### §2.1 Транспорт, не провайдер (успадковано з task-83 §2)

`Provider` не змінюється. Джерело для панелі обирається так само, як у 83/84 — `resolve_transport(config_json)`:

- `transport="google_api"` (або глобальний дефолт) **і** є `GOOGLE_API_REFRESH_TOKEN` → панель йде напряму в Google;
- інакше → панель лишається на збережених RN-даних, **точно як сьогодні**, з явною позначкою джерела в UI.

Нуль регресії за замовчуванням: клієнт без `transport` бачить те саме, що бачив.

### §2.2 Два ендпоінти, бо латентності відрізняються на порядок

| Ендпоінт | Що віддає | Виміряний бюджет |
|---|---|---|
| `GET /api/v1/url-inspector` (наявний) | усе збережене: archive, monitor, meta, tech audit, screenshots + RN-серії як fallback | десятки мс, **не змінюється** |
| `GET /api/v1/url-inspector/google` (новий) | GSC-серія + запити + зрізи, GA4-серія + зрізи | **~0.4–1.6 с** (§0.6) |
| `POST /api/v1/url-inspector/index-status` (новий) | URL Inspection API | **~6.6 с**, opt-in кнопкою |

Фронт малює сторінку одразу з першого виклику, другий підмінює вміст Search Analytics, третій — тільки після кліку (той самий патерн, що наявний `POST /live`).

Чому не один виклик: `GET /url-inspector` зараз обслуговує шість панелей, які не мають чекати мережу. Чому Index Status окремо: 6.6 с у спільному пулі з'їдає всю економію §0.6.

### §2.3 Розкладка модулів

```
app/urlinspector/google/
  window.py     — вікно та бакетизація: (window, granularity) -> [DateRange]; межа GSC 16 міс
  gsc.py        — запити GSC по одному URL (includingRegex, §0.4), відкидання синтетичних нулів (§0.3)
  ga4.py        — запити GA4 по одному URL (landingPage/pagePath + inListFilter, §0.4)
  index.py      — URL Inspection API
  keyevents.py  — читання metadata property -> перелік іменованих key events (§0.9), фільтр назв із пробілом (§0.8)
app/urlinspector/sources/
  google_gsc.py, google_ga4.py, google_index.py — обгортки рівня "джерело панелі", що дають UrlSourceStateDTO
```

Причина окремого підпакета: `app/integrations/google_api/*` — транспорт (HTTP, retry, токен) і володіється task-83; тут живе **семантика одного URL** (варіанти шляху, синтетичні нулі, бакети). Змішувати їх означало б класти знання про trailing slash у транспортний шар, який про URL нічого не знає.

### §2.4 Чому без міграції

Ця таска нічого не персиститься: усі три ендпоінти — read-through до Google. `gsc_dimension_metrics` і `ga4_landing_pages` продовжують наповнюватись збором як є (їх читає сторінка клієнта, page-inventory і звіти — не чіпаємо). Історія стану індексації **потребувала б** таблиці — свідомо винесено у follow-up §9.1, бо один снапшот без історії вже корисний, а таблиця без політики ретеншну — ні.

### §2.5 Ізоляція збоїв

Кожен запит — окрема клітинка з власним `UrlSourceStateDTO`, як у [service.py](app/urlinspector/service.py). `GoogleApiPermissionError` на GSC не має гасити GA4-панель; `GoogleApiConfigError` (немає токена) → `not_configured` для всієї Google-групи + автоматичний показ збережених RN-даних.

---

## §3. Що саме запитувати (виміряні, робочі тіла запитів)

### §3.1 GSC

```python
# фільтр, що ловить обидва варіанти слеша одним запитом (§0.4)
pf = [{"filters": [{"dimension": "page", "operator": "includingRegex",
                    "expression": rf"^{re.escape(url_no_slash)}/?$"}]}]
```

| Блок | dimensions | Примітка |
|---|---|---|
| Денна серія | `["date"]` | `dataState="all"` + позначити дні після `firstIncompleteDate` як provisional |
| Тотал за вікно | `[]` | не сума серії — GSC агрегує позицію сам |
| Запити сторінки | `["query"]` | `rowLimit=25000`, пагінація обов'язкова (§0.2: 692 рядки на сторінку) |
| Пристрої | `["device"]` | |
| Країни | `["country"]` | |
| SERP-вигляд | `["searchAppearance"]` | на нашому URL 0 рядків — валідна порожнеча, не помилка |

⚠️ `re.escape` на URL, а не конкатенація: точка в домені без екранування зробить `hearwellXservices` валідним матчем. GSC-регекс — RE2, регістрозалежний.

### §3.2 GA4

```python
def in_list(field, url):  # ga4_landing_variants дає ["/a", "/a/"]
    return {"filter": {"fieldName": field,
                       "inListFilter": {"values": ga4_landing_variants(url)}}}
```

| Блок | dimensions | filter | Метрики |
|---|---|---|---|
| Landing-серія | `["date"]` | `landingPage` | sessions, totalUsers, screenPageViews, engagementRate, keyEvents |
| Page-метрики | `[]` | `pagePath` | screenPageViews, activeUsers, userEngagementDuration, eventCount, scrolledUsers |
| Канал | `["sessionDefaultChannelGroup"]` | `landingPage` | sessions, totalUsers, keyEvents |
| Джерело/канал | `["sessionSourceMedium"]` | `landingPage` | sessions, keyEvents |
| Пристрій × країна | `["deviceCategory","country"]` | `landingPage` | sessions, keyEvents |
| Нові/повторні | `["newVsReturning"]` | `landingPage` | sessions |
| Події | `["eventName"]` | `pagePath` | eventCount, keyEvents |
| Наступні сторінки | `["pagePath"]` | `pageReferrer` CONTAINS шлях | screenPageViews |
| Звірка з GSC | `["landingPagePlusQueryString"]` | той самий | organicGoogleSearch* (§0.10) |

`engagementRate` — частка `0..1`, у БД лежить відсоток; **не змішувати** (та сама пастка ×100, що в task-83 §7.3).

⚠️ **≤10 метрик і ≤8 дименсій на запит** (§0.8). Повний набір landing-метрик — 15 полів, тож він ріжеться на дві пачки, і `_batched` мусить бути єдиною точкою, через яку метрики їдуть у запит: набір, що доріс до 11-ї метрики повз цю функцію, впаде непрозорим 400 уже в проді, а не в тесті.

⚠️ **Конверсії за назвою — тільки через дименсію `eventName`** (§0.8), ніколи через `keyEvents:<name>`: назва з пробілом валить запит, і клієнт із конверсією `Contact Us` тихо втратив би саме її.

### §3.3 URL Inspection

`POST https://searchconsole.googleapis.com/v1/urlInspection/index:inspect` з `{inspectionUrl, siteUrl, languageCode:"en-US"}`. Квота за документацією — 2000/добу і 600/хв на property; наш профіль (одна перевірка на клік оператора) від неї далеко, але кнопка мусить бути кнопкою, не автозапуском.

### §3.4 Дотик до `auth.py`

`GoogleApiSession` має лише `post_json`. Для `GET /metadata` (§0.9) і `GET /sitemaps` (§4, п. 23) потрібен `get_json` з тією ж політикою retry/редакції. Це **адитивна** правка одного методу у файлі task-83 — оголосити в Contract changes.

---

## §4. Таблиця: що ще можна додати завдяки Google API

Легенда цінності: 🔴 закриває питання, на яке сьогодні немає відповіді ніде · 🟡 суттєво точніше за наявне · 🟢 приємне доповнення.

| # | Що додати | Джерело / запит | Перевірено живо на цьому URL | Цін. | Ціна |
|---|---|---|---|---|---|
| 1 | **Реальні пошукові запити сторінки** (клік/показ/CTR/позиція) | GSC `page` filter × `["query"]` | 2 запити тут; **499 / 568 / 692** на топ-сторінках | 🔴 | 1 запит, ~450 мс; замінює сурогат task-64 |
| 2 | **Стан індексації** — verdict, `coverageState`, `robotsTxtState`, `indexingState`, `pageFetchState`, `crawledAs` | URL Inspection API | `PASS` / `Submitted and indexed` / `ALLOWED` / `INDEXING_ALLOWED` / `SUCCESSFUL` / `MOBILE` | 🔴 | 6.6 с, opt-in |
| 3 | **Дата останнього краулу Google** | URL Inspection | `2026-08-25T01:19:08Z` | 🔴 | у тому ж виклику |
| 4 | **Канонікал: обраний Google vs заявлений сторінкою** | URL Inspection | обидва `…/jessica-m-frankel/` (збіг) | 🔴 | у тому ж виклику |
| 5 | **Внутрішні посилання, які Google бачить на цю сторінку** | URL Inspection `referringUrls` | 2 URL (`/blog/why-hearing-aids…`, `/about-us/`) | 🟡 | у тому ж виклику |
| 6 | **Rich results: вердикт + типи** | URL Inspection `richResultsResult` | `PASS`, `Breadcrumbs` | 🟡 | у тому ж виклику; звірка з task-56 |
| 7 | **Mobile usability вердикт** | URL Inspection | `VERDICT_UNSPECIFIED` (Google згорнув звіт) | 🟢 | у тому ж виклику |
| 8 | **Глибокий лінк у GSC UI на цей URL** | `inspectionResultLink` | є | 🟢 | безкоштовно |
| 9 | **Денна серія без усічення, точне вікно** | GSC `["date"]`, `dataState="all"` | 365 рядків, 44 реальні дні | 🔴 | 470 мс; **вимагає §0.3** |
| 10 | **Свіжі 2 дні + мітка «неповний день»** | `dataState="all"` + `firstIncompleteDate` | `2026-09-07` | 🟡 | безкоштовно |
| 11 | **GSC-зріз за пристроєм** | GSC × `["device"]` | `DESKTOP` 2 показів | 🟡 | 330 мс |
| 12 | **GSC-зріз за країною** | GSC × `["country"]` | `usa` 2 показів | 🟡 | 520 мс |
| 13 | **SERP-вигляд** (`searchAppearance`) | GSC × `["searchAppearance"]` | 0 рядків — валідна порожнеча | 🟢 | 1 запит |
| 14 | **Типи пошуку: image / video / news / discover / googleNews** | GSC `type=` | усі відповідають; на цьому URL 0 | 🟢 | по запиту на тип |
| 15 | **Канал трафіку на сторінку** | GA4 × `sessionDefaultChannelGroup` | Direct 17, Organic Search 9 (1 конверсія) | 🔴 | 1.7 с |
| 16 | **Джерело/канал** | GA4 × `sessionSourceMedium` | `(direct)/(none)` 17, `google/organic` 5, **`bing/organic` 4 — і саме там 1 конверсія** | 🟡 | 1 запит |
| 17 | **Конверсії на сторінці, за назвою** | GA4 `keyEvents:<name>`, перелік із `metadata` | HWELL має 5: Contact Us, Email, Insurance Form, Phone, purchase | 🔴 | 1 запит на назву; ⚠️ пробіл у назві валить (§0.8) |
| 18 | **Наступні сторінки після цієї** | GA4 `pageReferrer` CONTAINS × `pagePath` | `/contact-us/` 4, `/locations/upper-west-side/` 2, `/about-us/` 1 | 🔴 | 840 мс |
| 19 | **Page-scoped поведінка** | GA4 фільтр `pagePath` | views 57, activeUsers 45, engagement 1158 с, events 139 | 🟡 | 900 мс |
| 20 | **Глибина залучення** | `bounceRate`, `averageSessionDuration`, `engagedSessions`, `screenPageViewsPerSession` | 0.719 / 32.1 с / 9 / 1.44 | 🟡 | у тому ж запиті |
| 21 | **Скрол 90 %** | `scrolledUsers` | `None` — подія не налаштована; чесна відсутність | 🟢 | безкоштовно |
| 22 | **Нові vs повторні** | GA4 × `newVsReturning` | new 22, returning 2, `(not set)` 2 | 🟢 | 1 запит |
| 23 | **Sitemap-статус** | `GET webmasters/v3/sites/{s}/sitemaps` | `sitemap_index.xml`, submitted 248 web / 515 image, warnings 1, lastDownloaded `2026-09-08` ⚠️ `indexed` завжди `0` — API так віддає, не показувати | 🟢 | 1 запит; потрібен `get_json` (§3.4) |
| 24 | **Вбудована звірка GSC↔GA4** | `organicGoogleSearch*` × `landingPagePlusQueryString` | 3 / 47 / 9.5531948 — **збіг із GSC до значущої цифри** | 🔴 | 1 запит; доказ здоров'я join'у |
| 25 | **Дрейф `<title>`, який бачив GA4** | GA4 × `pageTitle` | 4 різні title на один landing — сигнал про зміну або мультизаголовок | 🟡 | 1 запит |
| 26 | **Місто** | GA4 × `city` | Vinnytsia 15 (це ми), New York 7 | 🟢 | у зрізі |
| 27 | **Мова браузера** | GA4 × `languageCode` | en-US 24, en-GB 1 | 🟢 | у зрізі |
| 28 | **Активні зараз на сторінці** | GA4 `runRealtimeReport` | працює (1 користувач на іншій сторінці) | 🟢 | 1 запит, окремий бюджет |
| 29 | **Події сторінки** | GA4 × `eventName` (`pagePath`) | page_view 57, session_start 34, first_visit 28, user_engagement 20 | 🟢 | 1 запит |
| 30 | **1/7/28-денні активні** | `active1/7/28DayUsers` | 28 / 185 / 475 | 🟢 | у тому ж запиті |

**Чого Google API не дає** (щоб не планувати неможливе):

| Чого немає | Де це насправді живе |
|---|---|
| Беклінки на URL, URL Rating | Ahrefs (наявна інтеграція) |
| Кількість проіндексованих URL із sitemap | `indexed` в API завжди `0` — тільки GSC UI |
| Історія стану індексації | API віддає точковий знімок; історія = наша таблиця (§9.1) |
| `searchAppearance` у комбінації з іншими дименсіями | обмеження GSC API |
| Дані старші ~16 місяців | стеля GSC; наші збережені RN-рядки тут цінніші за API |

Останній рядок важливий: збережені дані **не стають смітником**. Для вікон за межею 16 місяців вони — єдине джерело, і панель має вміти показати їх поруч із поміткою походження.

---

## §5. Обсяг

**Рішення користувача (2026-09-09): усі 30 позицій §4 у цій тасці.** Первісна рекомендація плану була вужчою (17 позицій ядра, решта — follow-up); користувач розширив обсяг до повної таблиці.

Наслідки для реалізації, які це тягне за собою:

- **Запитів на завантаження стає більше за виміряні 8** (§0.6). Тому Google-панель ділиться на **три хвилі**, а не одну: `core` (серія + тотали + запити + канал + конверсії), `breakdowns` (пристрій/країна/місто/мова/джерело/нові-повторні/події/title/типи пошуку/searchAppearance), `slow` (URL Inspection 6.6 с, realtime, sitemaps). Хвилі 2 і 3 — ліниві, за розкриттям секції або кнопкою, щоб перший екран лишився в межах піводини секунди.
- **Порожні позиції мусять лишатись видимими як порожні.** На цьому портфелі `image`/`video`/`news`/`discover`-пошук, `searchAppearance` і `scrolledUsers` дають нуль рядків. Це валідна відповідь Google, і панель має казати «no data in this search type», а не зникати — інакше оператор не відрізнить «немає» від «не запитували» (PLAN §12).
- **Sitemap-позиція (23) вимагає `get_json`** у `auth.py` — тобто адитивний дотик до owned path task-83 стає обов'язковим, не опційним (§3.4).
- **Realtime (28) має власний бюджет** і не кешується разом з рештою: його значення істинне лише в момент запиту.

---

## §6. UI

- Панель Search Analytics отримує перемикач вікна (`28d / 90d / 12m / 16m`) замість поточного `week/month/quarter` над нерівними бакетами. Гранулярність графіка похідна від вікна, бо тепер у нас денні дані.
- Явна плашка джерела: `Direct from Google` / `via Reporting Ninja (stored)` — той самий копірайт, що task-85 уже ввів у футер документа звіту.
- Новий блок **Page queries** (табличка запит/кліки/покази/CTR/позиція, з пошуком) — замінює блок-сурогат, а сурогатний лишається під згорткою з наявним дисклеймером, поки на клієнті немає `transport=google_api`.
- Новий блок **Index status** із кнопкою `Check now` (6.6 с) і мітками: канонікал-розбіжність — попередження, `robotsTxtState != ALLOWED` — помилка.
- Порожнеча за межею 16 місяців — окремий текст `Beyond Search Console's 16-month history`, ніколи не `No data`.
- CSP: жодного інлайн-`style`, жодного JS-`.style.top/left` (пастка, ловлена двічі — task-43).

---

## §7. Definition of Done / Gate

**G1 — зелено:** `pytest tests/unit/urlinspector tests/unit/api`, `ruff check .`, `mypy app` у контейнері.

**G2 — паритет із самим Google (головний гейт):** для 3 клієнтів × 3 URL панель `#/url` за вікно «останній повний місяць» показує ті самі clicks/impressions/position, що прямий запит до GSC API, і ті самі sessions/users/views, що прямий запит до GA4 — **байт-у-байт**, як у task-84 §5.

**G3 — регресія нулів:** тест на живих формах відповіді: 365-рядкова серія з 321 синтетичним нулем дає **44** точки і середню позицію **9.68**, а не 365 точок і **1.17** (§0.3).

**G4 — слеш:** тест, що `pagePath` EXACT-варіант дає 45 сесій, а `inListFilter` — 52, і що реалізація бере другий (§0.4).

**G5 — межі:** вікно за 16 міс дає стан «за межею історії», не `no_data`.

**G6 — секрети:** ні `refresh_token`, ні `client_secret`, ні access-token не з'являються у відповідях, подіях і повідомленнях помилок.

**G7 — фронт:** живий Playwright-прогін на 390/768/1440, нуль помилок консолі, нуль горизонтального скролу (пастка `contain: paint`, task-57).

**G8 — нуль регресії:** клієнт без `transport=google_api` бачить рівно те, що бачив до таски.

---

## §8. Тести

`tests/unit/urlinspector/test_google_gsc.py` — синтетичні нулі (§0.3), regex-екранування домену, пагінація 25000, `firstIncompleteDate`, 16-місячна межа.
`tests/unit/urlinspector/test_google_ga4.py` — `inListFilter` обох варіантів, `landingPage` vs `pagePath` scope, `engagementRate` як частка, `keyEvents` лише landing-scoped, пробіл у назві key event.
`tests/unit/urlinspector/test_google_index.py` — парсинг усіх п'яти блоків, канонікал-розбіжність, відсутні блоки → `None`, не «PASS».
`tests/unit/urlinspector/test_google_window.py` — бакетизація, обрізання по 16 міс.
`tests/unit/api/test_url_inspector_api.py` — три ендпоінти, ізоляція збоїв, `not_configured` без токена, fallback на збережене.

---

## §9. Follow-ups

1. **Історія стану індексації** — таблиця `url_index_status` + щоденна перевірка ключових URL, щоб `coverageState` мав часову вісь. Потребує міграції (task-03) і політики ретеншну.
2. **Google API як транспорт для решти per-URL панелей** — page-inventory (`task-57`) досі бере GSC через RN і теж страждає від top-N.
3. **Заміна сурогату в keywords-панелі остаточно** — прибрати `sitequeries` після того, як `transport=google_api` стане дефолтом для всіх клієнтів.
4. **Item 23 (sitemaps)** очікує `get_json` у `auth.py` — якщо його додасть інша таска раніше, тут просто використати.

---

## ✅ Completion report

- **Дата:** 2026-09-09
- **Гілка / комміти:** `feat/task-85-client-report-document-design` @ `eadc5f5` (план), `e145a72` (бекенд), `f57bbdb` (фронт), + комміт цього звіту
- **Статус:** done-with-followups

### Що зроблено

Сторінка `#/url` більше не показує «те, що випадково зібралось» — вона питає Google про конкретний URL і конкретне вікно. Чотири нові панелі в порядку питань оператора: **Search performance** → **Page queries** → **Traffic & behaviour** → **Audience & discovery**, плюс **Index status** у сайдбарі за кнопкою. Обсяг — усі 30 позицій таблиці §4 за рішенням користувача.

Збережена RN-серія **не видалена**: вона лишилась секцією «Stored collection history» з поясненням, що це і навіщо. Причина в §4: за 16-місячною стелею Search Console вона — єдине джерело, яке в нас є.

Дві речі стали доступні вперше:

1. **Реальні пошукові запити сторінки.** RN не вміє page×query у принципі ([sitequeries.py](../../../app/urlkeywords/sitequeries.py) фіксує це живими пробами task-64), тому keywords-панель показувала сурогат. Живо на блозі HWELL: **493 запити** на одну сторінку, топ-200 у таблиці з явним «showing the top 200 of 493» і клієнтським фільтром.
2. **Стан індексації** — рядок, який task-61 позначив «ніде — тільки живий curl». URL Inspection API працює з наявним scope `webmasters.readonly`, нічого перевидавати не довелось.

### Як зроблено (ключові рішення)

- **Розділ шарів.** `app/integrations/google_api/**` (task-83) лишився транспортом; уся семантика «однієї сторінки» — у новому `app/urlinspector/google/**`. Класти знання про trailing slash у модуль, який приймає готовий `siteUrl`, було б розмиванням межі, за яку task-83 уже заплатив.
- **Три ендпоінти, розділені за ВИМІРЯНОЮ латентністю**, а не за темою: `google` (0.9–1.1 с), `google/breakdowns` (0.8 с), `index-status` (6.8 с, лише за кнопкою), `google/realtime` (0.5 с). Один ендпоінт означав би 8 с на кожне відкриття сторінки.
- **Паралелізм двох рівнів.** Спершу зрізи йшли послідовно й давали **6399 мс**; винесення розкладу в `google/fanout.py` (`defer` для різнотипних викликів, `fanout` для однотипних) звело їх до **~820 мс**. Окремо знайдено й прибрано точку серіалізації: на `key_event_names()` (GET на `metadata`, ~220 мс) **чекали**, перш ніж поставити GA4-панель у пул — це робило хвилю 1 удвічі довшою (1.9 с проти 0.9 с).
- **Транспорт ≠ провайдер** (успадковано з task-83): клієнт без `config_json["transport"]` бачить рівно те саме, що бачив. Гейт G8 закритий тестом.
- **`_metrics()` — єдина точка правила §0.3.** Правило «`impressions == 0` ⇒ відсутність» живе в одному місці саме тому, що жодна нова панель не повинна мати змоги його забути.
- **Головні файли:**
  - `app/urlinspector/google/gsc.py` — Search Analytics про один URL: синтетичні нулі, regex написань, пагінація, типи пошуку, сайтмапи.
  - `app/urlinspector/google/ga4.py` — дві дименсії сторінки, `inListFilter`, пачки метрик, конверсії через `eventName`, realtime.
  - `app/urlinspector/google/index.py` — URL Inspection, з нормалізацією `VERDICT_UNSPECIFIED` у `None`.
  - `app/urlinspector/google/window.py` — вікно, бакети, межа історії GSC.
  - `app/urlinspector/google/fanout.py`, `units.py`, `target.py` — розклад, одиниці, резолв property.
  - `app/urlinspector/google_service.py` — оркестратор і звірка.
  - `app/urlinspector/sources/google_{gsc,ga4,index}.py` — переклад у DTO.

### Відхилення від плану (усі — виправлення вимірюванням)

1. **§0.8 первісно був неправильний.** «`landingPagePlusQueryString` несумісна з набором метрик» виявилось хибним прочитанням: справжня причина — **ліміти запиту, 10 метрик і 8 дименсій**, і 11-та метрика падає непрозорим 400. Секцію переписано, реалізація ріже метрики на пачки.
2. **Конверсії за назвою — тільки через `eventName`.** `keyEvents:<name>` падає на будь-якій назві з пробілом (`Contact Us`), і `%20` не рятує. Обхід перевірено; у HWELL 3 із 5 налаштованих конверсій мають пробіл, тобто прямий шлях тихо втратив би саме їх.
3. **`clamped_to_gsc_history` був мертвим полем.** Найдовше вікно (`16m`) впирається в стелю, не переходячи її, тож прапорець ніколи не ставав `True` і текст «за межею історії» не показався б. Додано `at_gsc_history_edge` — і тест G5 тепер перевіряє поведінку, а не існування поля.
4. **`page_regex` розширено на варіанти хоста — знайдено гейтом G2.** `sc-domain:` property покриває і `www.`, і голий хост; запит по одному написанню дав на SHB **15 372** кліки проти **18 144** у GA4 — 18 % різниці, яку легко було б прийняти за розбіжність даних. Тепер regex ловить `https?://(?:www\.)?host…/?`, тобто те саме визначення «однієї сторінки», що вже діє в `urlkey.lookup_variants`. Мати в одному модулі два різні визначення означало б два різні числа на одному екрані.
5. **Звірка отримала допуск.** Точна рівність кричала на 3XE, де різниця — **1 показ із 73 030** (0.0014 %). Допуск 0.5 % із підлогою в одну одиницю: ловить 18 % (зламаний матчинг), мовчить про один показ.
6. **400 на `organicGoogleSearch*` — це «немає GSC↔GA4 лінку», не збій.** Половина портфеля (2 клієнти з 4 у G2) не має лінку, і кожне відкриття їхньої сторінки писало traceback у лог і фарбувало звірку в `failed`. Тепер це порожній результат.

### Контрактні зміни (Contract changes)

1. **`app/schemas/url_inspector.py`** — 14 нових DTO (`UrlGoogleCoreDTO`, `UrlGoogleBreakdownsDTO`, `UrlIndexStatusDTO`, `UrlRealtimeDTO` та їхні складові). **Тільки адитивно**: жоден наявний DTO не змінено, тому task-02 синхронізувати нема чого.
2. **`app/api/url_inspector.py`** — 4 нові ендпоінти. `409` = «Google не підключений для цього клієнта» (стан панелі, не помилка сторінки).
3. **`app/integrations/google_api/auth.py`** (owned by task-83) — **адитивно** додано `GoogleApiSession.get_json`; `post_json`/`get_json` тепер обидва проходять через спільний `_request`, тож політика retry не може розійтись між дієсловами. Потрібно для GET-ендпоінтів (GA4 `metadata`, GSC `sitemaps`), яких task-83 не мала причини мати.
4. **`app/core/config.py`** — адитивно `url_inspector_google_workers: int = 8`, `url_inspector_query_cap: int = 200`.
5. **Міграцій — нуль**, як і планувалось: усе read-through, нічого не персиститься.

### Як перевірити (verification)

```bash
docker compose exec api poetry run pytest tests/unit/urlinspector -q     # 169 passed
docker compose exec api poetry run ruff check app/urlinspector tests/unit/urlinspector
docker compose exec api poetry run mypy app                              # чисто, 327 файлів
```

**Живо в UI** — `http://localhost:6524/#/url?u=https%3A%2F%2Fhearwellservices.com%2Fabout-us%2Fjessica-m-frankel%2F`:

- вгорі має бути **Search performance** з перемикачем вікна (28 days / 90 days / 12 months / 16 months), тайлами (Clicks 3 · Impressions 39 · CTR 7.69% · Avg. position 8.9) і бейджем `GSC ↔ GA4 AGREE`;
- **Page queries** — реальні запити сторінки; на блоговій сторінці (`/blog/hearing-aid-dome-size-chart-tips-for-the-right-fit/`) має бути «493 queries» і рядок «Showing the top 200 of 493»;
- **Traffic & behaviour** — два блоки тайлів із поясненням, чому числа різні (`landingPage` проти `pagePath`), конверсії, графік;
- **Audience & discovery** — смужка типів пошуку (Web 39 impr., Images 1 impr., решта «not shown here») і 14 карток зрізів;
- **Index status** у сайдбарі: `Check now` → через ~7 с `PASS` / `Submitted and indexed` / канонікал / внутрішні посилання / сайтмапи;
- перемикання метрики на `CTR` має дати вісь у відсотках, на `Position` — перевернуту вісь.

**Гейти:**

| Гейт | Результат |
|---|---|
| **G1** лінт / типи / тести | зелено |
| **G2** паритет із Google | **ПРОЙДЕНО** на 4 клієнтах (3XE, PJC, SDS, SHB). Еталон будується незалежно: окремий `equals`-запит на кожне з 8 написань `lookup_variants`, кліки/покази сумою, CTR як `clicks/impr`, позиція зважена за показами. Панель дорівнює цій сумі байт-у-байт на цілих і до 1e-7 на дробових — тобто один regex-запит доведено еквівалентний восьми точним. |
| **G3** регресія нулів | тест `TestSyntheticZeros`: 5 днів, 3 синтетичні → серія має 2 точки, позиція не тягнеться до нуля |
| **G4** слеш | тести на `page_regex` (обидва слеші, обидва хости, обидві схеми, чужий домен не матчиться) + `inListFilter` в GA4 |
| **G5** межі | `at_gsc_history_edge` `True` рівно для `16m`, текст «за межею історії» перевірено тестом |
| **G6** секрети | у відповідях і станах панелей — жодного токена; помилки будуються з `type(exc).__name__` і повідомлення, секрети не потрапляють у них уже на рівні `auth.py` (task-83) |
| **G7** фронт | Playwright на 390/768/1440: **0 помилок консолі**, 0 горизонтального переповнення від нових панелей |
| **G8** нуль регресії | тест `test_default_transport_stays_reporting_ninja` + збережена панель на місці |

**Живі числа, на яких усе перевірялось** (HWELL, вікно 90 днів): GSC 3 кліки / 39 показів / позиція 8.897; GA4 landing 31 сесія / 27 користувачів, page 56 переглядів / 44 активних; звірка `organicGoogleSearch*` дала 3 / 39 / 8.897 — збіг обома шляхами.

### Тести

`tests/unit/urlinspector/` — 5 нових файлів, 169 тестів разом із наявними:

- **`test_google_gsc.py`** — синтетичні нулі (три кути: рядок відкидається, позиція не падає в нуль, справжній `clicks=0` виживає), regex (слеш, хост, схема, екранування метасимволів, чужий домен), `dataState=all` + `firstIncompleteDate`, пагінація за 25 000, типи пошуку (порожній лишається), бакетизація (кліки сумою, позиція зважена показами), обрізання запитів, одиниці метрик.
- **`test_google_ga4.py`** — `inListFilter` обох написань, розділення `landingPage`/`pagePath`, ліміти 10/8 із локальним `ValueError` замість 400 у проді, конверсії лише через `eventName` (жодна метрика не містить `:`), звірка `organicGoogleSearch*` (сума кліків, `None` для позиції по кількох рядках), `engagementRate` як частка, `pageReferrer` CONTAINS.
- **`test_google_index.py`** — дослівна жива відповідь API, `VERDICT_UNSPECIFIED` → `None`, канонікал (збіг / розбіжність / лише слеш / одна сторона відсутня), сайтмапи (числа з рядків, `indexed` не існує в DTO, збій сайтмап не гасить вердикт).
- **`test_google_window.py`** — усі ключі вікон, тихий фолбек на `90d`, бакети, межа історії, лютий у високосному році.
- **`test_google_service.py`** — резолв цілі (невідомий хост / без інтеграції / лише GSC / лише GA4 / вимкнена), ізоляція клітинок, `not_configured` проти `failed`, збій групи зрізів як рядок, і чотири тести допуску звірки на живих числах (1 показ із 73 030 — збіг; 18 % — розбіжність; підлога в одну одиницю).

### Follow-ups / борг

- **`tests/unit/api/data/openapi_snapshot.json` навмисно НЕ закомічено.** У робочому дереві він містить і мої ендпоінти, і незавершену поверхню сусідньої сесії (task-89/90 правлять `app/api/analytics.py`, `app/schemas/api.py`, `app/integrations/google_api/{gsc,ga4}_client.py` просто зараз). Регенерація належить тому, хто приземлиться останнім; сам тест зелений на поточному дереві.
- **Дублювання транспорту з task-89.** Паралельна сесія додає `GscClient.search_analytics` (довільні дименсії) і `dimension_filter` у `Ga4Client.run_report` — тобто те саме, що мої `GscUrlClient`/`Ga4UrlClient` роблять самі. Коли обидві таски приземляться, `google/gsc.py` і `google/ga4.py` варто перевести на цей транспорт, лишивши собі **лише** URL-семантику (regex, синтетичні нулі, варіанти шляху). Зараз перетинати межу owned paths було б гірше за дублювання.
- **`div.header-start` дає 39 px горизонтального переповнення на 390 px** — на `#/`, `#/trends`, `#/reports` і `#/url` однаково, тобто це глобальний хедер (task-61 + `app.css`), не ця таска. `#/trends` і `#/reports` додатково переповнює `table.analytics-table` — уже зафіксовано у follow-ups task-84.
- **Спільна тестова БД `yovth_test` не витримує двох одночасних прогонів `pytest`** у тому самому контейнері: `build_test_engine` робить `TRUNCATE` на кожен виклик, тож паралельний прогін сусідньої сесії вимиває чужі рядки посеред тесту (`duplicate key uq_clients_slug`, падіння «переїжджають» між прогонами). Не дефект коду; але поки dev-процес допускає дві сесії в одному контейнері, це коштуватиме хибних червоних. Варіант рішення — окрема БД на процес (`yovth_test_$PID`) у `tests/harness/db.py` (власність task-13/75).
- **`renderSearchTypeStrip` живе у хвилі 2**, хоча описує дані Search Console. Заголовок «Where this page appears in Google» це компенсує, але якщо колись типи пошуку стануть дешевими, їм місце в секції Search performance.
- **Історія стану індексації** (успадковано з §9.1) — `url_index_status` + щоденна перевірка ключових URL. Потребує міграції (task-03) і політики ретеншну.
