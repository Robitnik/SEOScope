# task-55 — Blog Plan: read-only дзеркало Notion «Blog Management» + історія змін

- **Пакет:** новий модуль `app/blogplan/**` — щогодинний **read-only** синк редакційного плану з Notion, нормалізація wide-таблиць у плоскі рядки «один блог = один запис», сторінка клієнта з таймлайном по місяцях і **власна історія змін**, якої Notion API не віддає.
- **Хвиля:** R — Editorial (перша таска хвилі).
- **Depends on:** **06** ✅ (REST), **03** ✅ (ORM/міграції), **05** ✅ (worker/scheduler-патерн), **13** ✅ (harness)
- **Не конфліктує** з task-51 (`app/monitor/**`) і task-53 (`app/contentqa/**`) — зони не перетинаються.
- **Owned paths:** `app/blogplan/**`, `app/api/blog_plan.py`, `app/schemas/blog_plan.py`, `app/db/repositories/blog_plan.py`, `migrations/versions/*_blog_plan.py`, `frontend/js/{blog-plan-page,blog-plan-timeline,blog-plan-history}.js`, `frontend/css/blog-plan.css`, `tests/unit/blogplan/**`, `tests/unit/api/test_blog_plan.py`
- **Адитивні дотики (узгодити):** `app/db/models.py` (+4 моделі), `app/core/config.py` (+`notion_*` поля), `app/jobs/worker.py` (+`BlogPlanScheduler.start()/stop()`, як `CloudScheduler`), `app/main.py` (+router), `frontend/js/router.js` (+2 маршрути), `frontend/js/client-page.js` (+вкладка/картка), `frontend/index.html` (+1 `<link>`), `.env.example` (+`NOTION_TOKEN`)
- **PLAN.md:** §12 (чесні стани: «не синкали» ≠ «блогів немає»), §14 (секрети не течуть у БД/логи/UI)
- **frontend/AGENTS.md:** §5 (правда даних), §6 (стани), §15–16 (візуальний loop)
- **AISeoTools:** змін **не потребує** — Notion це прямий REST, поза скоупом AISeoTools.
- **Нових Python-залежностей: нуль.** Notion REST б'ємо через stdlib `urllib.request`, як увесь `app/monitor/**`. Це свідоме рішення: офіційний SDK `notion-client` не дає нічого понад «GET з двома заголовками», а `poetry lock` у цій пісочниці не проходить (див. `env-poetry-lock-limitation`).

---

## Мета (робота оператора, яку закриваємо)

Редакційний план живе у Notion-сторінці «Blog Management» і **не читається**. Скарги користувача, дослівно: «не юзабильна таблиця важко читати», «по місяцях по 1 клієнту путаєш», «сортування криве», «немає історії змін».

Це не вигадані незручності — вони прямо випливають зі структури, яку Notion нав'язав (§1). Ціль: той самий контент, але в структурі, де **один блог — один рядок**, а «усе по моєму клієнту» — це одна сторінка, а не полювання по 21 вкладці.

---

## 1. Дослідження (факти, на яких стоїть план)

### 1.1 Чому таблиця нечитабельна — це структурна, а не косметична проблема

Зі скріншота користувача (2026-08-26) видно три накладені одна на одну вади:

**(а) База на кожен місяць.** Вкладки `0126 0226 0326 0426 0526 0626 0726 0826 0926` під заголовком `2026`, і `1225 1125 1025 0925 0825 0725 0625 0525 0425 0325 0225 0125` під `2025`. Це ~21 окрема Notion-database. «Історія клієнта за рік» = відкрити 12 вкладок по черзі й тримати в голові.

**(б) Wide-формат: блоги розкладені в колонки, не в рядки.** Рядок = клієнт, а блоги — це повторювані групи колонок: `Blog topic 1 | Framework 1 | Images 1 | Status 1 | Blog topic 2 | Framework 2 | Images 2 | Status 2 | Blog topic 3 | Framework 3 | …`. Клієнт із 4 блогами тягне 16+ колонок вправо. Саме тому таблиця має горизонтальний скрол на два екрани, а «сортування криве»: **відсортувати за статусом неможливо в принципі** — статусів у рядку чотири, і кожен у своїй колонці.

**(в) Схема дрейфує між роками.** Таблиця 2026 має `Author name`, `Monthly blogs`, `Client approval`, `Blog topic N`, `Framework N`, `Images N`, `Status N`. Таблиця 2025 — інший набір: `Author Name`, `Note`, `Blog Topics`, `Framework`, `Copy`, `Images Blog 1`, `Status`, `Post Date Blog 1`, `Link Blog 1`, `Blog 2`. Тобто парсер **не має права** покладатися на фіксований список колонок; він мусить розпізнавати схему щоразу (§3.2).

> **Наслідок для дизайну:** нормалізація — це не «гарніший UI», це єдиний спосіб отримати сортування й фільтри взагалі. Уся таска стоїть на переході wide → long.

### 1.2 Клієнти Notion мапляться на наших майже 1:1

Порівняння імен зі скріншота вкладки `0926` з `clients.json` (14 клієнтів):

| Notion `Name` | Наш `code` |
|---|---|
| 19 Cleveland | `19CLV` |
| Grill Point | `GRP` |
| Hearwell Services | `HWELL` |
| New York Hearing Associates | `NYCHA` |
| PJ Clarkes | `PJC` |
| Sapphire NY | `SAPHNY` |
| Shawarma Mia | `SWM` |
| Shoreline Dental Studio | `SDS` |
| Smash House Burgers | `SHB` |
| Effy's | `EFFYS` |
| 3XE | `3XE` |

**11 з 11 рядків вкладки 0926 мапляться.** Не мають блогів у цій вкладці: `ALPHA`, `LAXTON`, `GOREPORT` — це нормально, не помилка мапінгу.

У таблицях 2025 трапляються імена поза дашбордом (`Advanced Dental Art`, `Anita`, `BVI`). **Рішення користувача: тягнемо тільки клієнтів дашборду.** Але вони мусять відсіюватися **гучно**: кожне незмаповане ім'я лягає в `blog_plan_sync_runs.unmapped_names` і показується в UI як «N rows skipped — unmapped client». Мовчазна втрата рядків — заборонена (PLAN §12).

**Мапінг двоступеневий:** спершу explicit-таблиця `NOTION_NAME_OVERRIDES` (`"PJ Clarkes" → "PJC"`), потім нормалізоване порівняння (lowercase, прибрати не-alphanum) з `name`/`code`/`domain`. Explicit-таблиця перемагає завжди — щоб перейменування в Notion не ламало прив'язку тихцем.

### 1.3 Notion API дає рівно те, що треба для читання — і не дає історії

- **Auth:** internal integration token, заголовки `Authorization: Bearer <token>` + `Notion-Version: 2022-06-28`. Сторінку «Blog Management» треба **явно** розшарити з інтеграцією (`···` → `Connections`), інакше API поверне 404 на об'єкт, який у браузері видно.
- **Обхід дерева:** `GET /v1/blocks/{page_id}/children` рекурсивно → збирає `child_database` блоки (це і є вкладки `0126`…). Рік беремо з батьківського heading-блоку, місяць — з назви бази (`MMYY`).
- **Читання рядків:** `POST /v1/databases/{id}/query`, пагінація по 100 (`start_cursor`/`has_more`). Це POST, але семантично **read** — нічого не змінює.
- **Rate limit:** ~3 запити/сек середнє. При ~21 базі × (1 query + пагінація) це ≈30–60 запитів на повний синк. Спокійно вкладається; троттлимо на 3 rps з запасом і ретраїмо `429` за `Retry-After`.
- **Історії правок API не віддає.** Page history є лише в UI Notion і лише на платних планах; жодного `/history`-ендпоінта немає. `last_edited_time` на сторінці каже **коли**, але не **що** змінилося.

> **Наслідок:** історію будуємо самі, вперед від першого синку (§4). Ретроспективу з Notion дістати неможливо — це треба сказати користувачу прямо, а не робити вигляд, що «історія за 2025» з'явиться.

### 1.4 Read-only — це інваріант рівня коду, а не обіцянка

Користувач: «Апі лише читання. зміни ні». Тому:

- HTTP-клієнт `app/blogplan/notion_api.py` має **білий список методів**: дозволені `GET` і `POST` **тільки** на шляхи `/v1/search`, `/v1/databases/*/query`. Будь-який `PATCH`/`PUT`/`DELETE` або POST на інший шлях → `NotionWriteForbidden` ще до відкриття сокета.
- Тест `test_notion_api_is_read_only.py` перебирає всі публічні методи клієнта і падає, якщо хоч один може сформувати запит поза білим списком.

Це дешевше й надійніше, ніж покладатися на «ми ж не писатимемо»: якщо колись хтось додасть write, тест зупинить його в CI.

### 1.5 Розведення з наявним `blogs` (task-26)

У проєкті вже є `blogs` — краулер, що ходить по **сайту клієнта** і збирає **опубліковані** пости (`app/integrations/blogs.py`, `#/client/<id>/blogs`). Це інша сутність.

| | `blogs` (task-26) | `blog-plan` (ця таска) |
|---|---|---|
| Джерело | краул сайту клієнта | Notion «Blog Management» |
| Питання | що **вже опубліковано** | що **заплановано / в роботі** |
| UI-назва | **Blogs** | **Blog Plan** |

Назви в UI розводимо жорстко, щоб не плодити плутанину. Зіставлення «план vs факт» (`Posted | QC` у Notion, але сторінки нема на сайті) — **очевидна наступна фіча**, але **не в скоупі**; це той самий `blog_publish_check`, що task-26 лишив у follow-ups. Модель даних §2 його не блокує.

---

## 2. Модель даних

Чотири адитивні таблиці. Ключове рішення — **розділити «поточний стан» і «історію»**: перший читається швидко для UI, друга росте вічно й ніколи не переписується.

### 2.1 `blog_plan_items` — поточний стан, один рядок = один блог

Головна таблиця. Wide-структура Notion (§1.1б) розкладається тут у long.

| Колонка | Тип | Нотатки |
|---|---|---|
| `id` | uuid PK | |
| `client_id` | FK → `clients.id` | тільки змаповане (§1.2) |
| `period_month` | `str` `YYYY-MM` | з назви бази `MMYY` |
| `slot` | `int` | 1..N — який це «Blog N» у рядку Notion |
| `notion_page_id` | `str` | id рядка-сторінки Notion |
| `notion_database_id` | `str` | id місячної бази |
| `topic` | `str \| None` | `Blog topic N` |
| `status_raw` | `str \| None` | оригінальний текст статусу, **як у Notion** |
| `status_key` | `str \| None` | нормалізований (§3.3) |
| `author` | `str \| None` | |
| `framework_url` | `str \| None` | докс-лінк |
| `copy_url` | `str \| None` | |
| `images_count` | `int \| None` | `None` ≠ `0` (PLAN-інваріант) |
| `post_date` | `date \| None` | |
| `live_url` | `str \| None` | `Link Blog N` |
| `client_approval` | `bool \| None` | tri-state: так / ні / поля не було |
| `monthly_blogs_planned` | `int \| None` | з рядка клієнта, не з блога |
| `note` | `str \| None` | |
| `content_hash` | `str` | sha256 значущих полів — рушій diff (§4) |
| `first_seen_at` / `last_seen_at` / `last_changed_at` | datetime | |

Unique: `(client_id, period_month, slot)`. Індекси: `(client_id, period_month)`, `(status_key)`, `(post_date)`.

> **`status_raw` зберігаємо завжди.** Notion-статуси змінюються без попередження (`Framework is ready`, `Posted | QC`, `Need info from client`, `Not started`). Якщо нормалізатор зустріне невідоме — UI показує сирий текст, а не «Unknown». Втрачати правду через власний енум — недопустимо.

### 2.2 `blog_plan_changes` — історія, append-only

Те, чого немає в Notion. Патерн — `MonitorChange` (task-35).

| Колонка | Тип |
|---|---|
| `id` | uuid PK |
| `item_id` | FK → `blog_plan_items.id` (nullable — переживає видалення) |
| `client_id`, `period_month`, `slot` | денормалізовано, щоб історія жила після зникнення item |
| `change_kind` | `added` \| `removed` \| `updated` |
| `field` | `str \| None` — яке поле (для `updated`) |
| `old_value` / `new_value` | `str \| None` |
| `sync_run_id` | FK → `blog_plan_sync_runs.id` |
| `detected_at` | datetime |

Один запис = **одне поле**, не «дифф рядка». Так UI може показати «Status: Not started → Framework is ready» без клієнтського парсингу, і так фільтр «покажи лише зміни статусів» — це `WHERE field = 'status_key'`.

### 2.3 `blog_plan_sync_runs` — чесність синку

`id`, `started_at`, `finished_at`, `status` (`running`/`ok`/`partial`/`failed`), `databases_seen`, `rows_read`, `items_upserted`, `changes_detected`, `unmapped_names` (JSON), `error` (`str | None`), `trigger` (`scheduled`/`manual`).

Без цієї таблиці UI не може чесно відповісти «дані свіжі чи синк відвалився три дні тому» — а це рівно та різниця, яку PLAN §12 вимагає показувати.

### 2.4 `blog_plan_databases` — карта вкладок

`notion_database_id` PK, `title` (`0926`), `year`, `month`, `period_month`, `schema_fingerprint`, `last_synced_at`, `row_count`.

Дає дешеву відповідь на «які місяці взагалі існують» для навігації, без сканування items.

---

## 3. Синк

### 3.1 Планувальник

`BlogPlanScheduler` — точна копія патерну `CloudScheduler`/`MonitorScheduler` (thread + tick loop + `.start()/.stop()` з `app/jobs/worker.py`). Tick — 60 с; повний синк запускається, якщо минуло ≥ `notion_sync_interval_minutes` (дефолт 60) від `finished_at` останнього успішного run.

Стан виводиться з БД, а не з полів у пам'яті — пропущений тік після рестарту не подвоює і не губить синк (той самий аргумент, що в докстрінгу `CloudScheduler`).

Нові налаштування в `app/core/config.py` (плоскі, як `b2_vault_*`):

```python
notion_enabled: bool = False
notion_token: str = ""              # тільки з .env, ніколи в БД/логах/UI
notion_root_page_id: str = ""       # сторінка "Blog Management"
notion_sync_interval_minutes: int = 60
notion_max_requests_per_second: float = 3.0
notion_history_retention_days: int = 0   # 0 = вічно
```

### 3.2 Розпізнавання схеми (не хардкод колонок)

З §1.1в: список колонок різний у 2025 і 2026. Тому `app/blogplan/schema.py` будує мапу з **фактичних** `properties` бази регулярками:

- `^blog\s*topics?\s*(\d+)?$` → `topic`, slot = група або 1
- `^framework\s*(\d+)?$` → `framework_url`
- `^status\s*(\d+)?$` → `status_raw`
- `^images?\s*(blog\s*)?(\d+)?$` → `images_count`
- `^(post\s*date|link)\s*(blog\s*)?(\d+)?$` → `post_date` / `live_url`
- `^author\s*name$` → `author`; `^monthly\s*blogs$`, `^client\s*approval$`, `^note$`, `^copy$`

Кількість слотів = максимум знайдених номерів. Колонка без номера = слот 1.

`schema_fingerprint` (sha256 відсортованих імен колонок) пишеться в `blog_plan_databases`. **Нерозпізнані колонки не ігноруються мовчки** — вони логуються один раз на fingerprint і показуються в UI синку як «unrecognized columns». Notion — жива таблиця; нова колонка з'явиться, і ми маємо про це дізнатися, а не «успішно» синкати неповні дані.

### 3.3 Нормалізація значень

- **Статуси:** `status_raw` → `status_key` (`not_started`, `need_info`, `framework_ready`, `copy_ready`, `posted_qc`, `posted`). Невідоме → `status_key = None`, `status_raw` показується як є (§2.1).
- **Порожньо ≠ нуль:** пуста Notion-клітинка → `None`. `images_count = 0` пишемо **тільки** якщо в Notion реально порожній масив файлів при наявній колонці.
- **Slot без даних не створює item.** Якщо `Blog topic 3` порожній і решта слота теж — рядка немає, а не «порожній блог».

### 3.4 Ізоляція збоїв

Одиниця збою — **одна база (місяць)**. Виняток на базі `0326` не валить синк решти: run завершується як `partial`, база лишається зі старим `last_synced_at`, помилка йде в `blog_plan_sync_runs.error`. Той самий принцип, що «клітинка `(client, provider)` ізольована» з CLAUDE.md.

---

## 4. Історія змін (те, чого Notion не вміє)

Рушій — порівняння `content_hash` + пофайлова дельта:

1. Синк зчитує рядок, будує `BlogPlanItemDraft`, рахує `content_hash` зі значущих полів (усі, крім `*_seen_at`).
2. Нема такого `(client, month, slot)` → `INSERT` + один change `added`.
3. Є, хеш збігся → тільки `last_seen_at`. Жодних змін, жодного шуму.
4. Є, хеш інший → пофайлове порівняння, по одному change `updated` **на кожне змінене поле**, `last_changed_at = now`.
5. Був у попередньому синку тієї ж бази, тепер його нема → change `removed`. **Сам item не видаляємо** — м'яке `removed_at`, щоб історія лишалась клікабельною.

Що це дає одразу: «PJC: Status 2 змінився Not started → Framework is ready 14 серпня», «SDS: Blog topic 1 переписали тричі за тиждень», «у GRP зник запланований блог 3». Жодного з цих питань Notion зараз не відповідає.

**Що чесно сказати в UI:** банер на сторінці історії — `History starts <date of first sync>. Notion does not expose past edits through its API.` Без цього користувач вирішить, що до цієї дати змін не було.

---

## 5. REST API (`app/api/blog_plan.py`, префікс `/api/v1`)

| Метод | Шлях | Віддає |
|---|---|---|
| `GET` | `/blog-plan/clients/{client_id}` | таймлайн клієнта: місяці ↓, у кожному items ↑ за slot |
| `GET` | `/blog-plan/clients/{client_id}/history` | changes клієнта, пагіновано, фільтри `field`, `since` |
| `GET` | `/blog-plan/months/{period_month}` | зріз «місяць → усі клієнти» (другорядний) |
| `GET` | `/blog-plan/status` | останній sync run + свіжість + `unmapped_names` |
| `POST` | `/blog-plan/sync` | ручний «Sync now» (ставить run; не пише в Notion) |

`POST /blog-plan/sync` — єдиний не-GET, і він пише **в нашу** БД. У Notion не йде жодного запису (§1.4).

Усі DTO — в `app/schemas/blog_plan.py`. Тексти помилок і значення enum — англійською (CLAUDE.md).

---

## 6. UI

Головний зріз — **клієнт → таймлайн місяців** (рішення користувача).

### 6.1 `#/client/<id>/blog-plan` — таймлайн (основна сторінка)

Вертикально: місяць за місяцем, найновіший зверху. У кожному місяці — компактна таблиця, **один рядок = один блог**:

```
September 2026                                    3 of 3 planned · 2 posted
┌──────┬─────────────────────────────┬────────────────┬───────────┬────────┐
│ #    │ Topic                       │ Status         │ Post date │ Links  │
├──────┼─────────────────────────────┼────────────────┼───────────┼────────┤
│ 1    │ Happy Hour in SoHo & Nolita │ Framework ready│    —      │ FW     │
│ 2    │ Dinner in SoHo & Nolita     │ Framework ready│    —      │ FW     │
└──────┴─────────────────────────────┴────────────────┴───────────┴────────┘
```

Те, що в Notion вимагало скролу на два екрани вправо, тут читається згори вниз. Сортування працює, бо статус — це **колонка**, а не чотири колонки.

- Заголовок місяця показує `monthly_blogs_planned` проти фактичних (`3 of 3 planned`) — розходження плану й наповнення видно без рахування.
- Порожній місяць — явний стан `No blogs planned`, **не** порожня таблиця (AGENTS.md §6).
- Ширина: `overflow-x: auto` + `contain: paint` на обгортці — інакше широка таблиця скролить усю сторінку в Chromium (див. `table-scroll-contain-paint`).

### 6.2 Історія

Вкладка/панель на тій же сторінці: стрічка змін клієнта, згрупована по днях, з фільтром по полю. Рендер дифів — за патерном [monitor-diff.js](frontend/js/monitor-diff.js), без винаходу нового вигляду.

### 6.3 Портфоліо `#/blog-plan`

Другорядне: місяць-селектор + усі клієнти, один рядок = один блог, фільтри `client`/`status`/`author`. Закриває «планування місяця», але не є головним входом.

### 6.4 Картка на сторінці клієнта

У [client-page.js](frontend/js/client-page.js) — компактний блок «Blog Plan: 2 of 3 this month · 1 posted» з лінком на таймлайн. Патерн уже відпрацьований на monthly-performance (task-54).

### 6.5 Свіжість і чесні стани

Шапка сторінки показує `Synced <relative time>`. Якщо останній run `failed`/`partial` або старший за 3 інтервали — попереджувальний бейдж із причиною. **Ніколи** не показуємо старі дані як свіжі (PLAN §12). Якщо `notion_enabled = false` або токена немає — стан `Not configured` з поясненням, не помилка й не пустка.

CSP: жодних інлайнових `style=` і жодних JS-присвоєнь `.style.top/left` — обидва блокуються (див. `frontend-verification-tooling`, `task43_reports_ui_done`).

---

## 7. Безпека

- `NOTION_TOKEN` живе **тільки** в `.env` → `Settings`. Не потрапляє в БД, events, логи, API-відповіді, повідомлення помилок (PLAN §14).
- `GET /blog-plan/status` віддає `token_configured: bool` — і ніколи ані фрагмента токена.
- Помилки Notion (401/403/404) транслюються в UI як осмислений текст (`Notion integration has no access to the page`), без ехо тіла відповіді, яке може містити ідентифікатори.
- `.env.example` отримує `NOTION_TOKEN=` і `NOTION_ROOT_PAGE_ID=` з коментарем про шаринг сторінки з інтеграцією.

---

## 8. Тести

| Файл | Покриває |
|---|---|
| `tests/unit/blogplan/test_notion_api_read_only.py` | білий список методів; спроба write → `NotionWriteForbidden` (§1.4) |
| `tests/unit/blogplan/test_schema_detection.py` | схеми 2025 **і** 2026 з §1.1в; нерозпізнані колонки повідомляються |
| `tests/unit/blogplan/test_client_mapping.py` | 11 імен із §1.2; override перемагає; unmapped потрапляє в run, а не гине |
| `tests/unit/blogplan/test_diff_engine.py` | added/updated/removed; незмінений рядок **не** створює change; кілька полів = кілька changes |
| `tests/unit/blogplan/test_normalize.py` | порожньо → `None`, не `0`; невідомий статус зберігає `status_raw` |
| `tests/unit/blogplan/test_sync_isolation.py` | збій однієї бази → `partial`, решта синкнулась |
| `tests/unit/api/test_blog_plan.py` | ендпоінти §5; `status` не тече токеном |

Плюс фікстури реальних Notion-відповідей (з обрізаними id) у `tests/unit/blogplan/fixtures/`.

---

## 9. Definition of Done / Gate

1. `docker compose exec api poetry run pytest tests/unit/blogplan tests/unit/api/test_blog_plan.py` — зелено.
2. `ruff check .` і `mypy app` — чисто.
3. Живий синк на реальному воркспейсі: ≥1 `ok`-run, усі місячні бази в `blog_plan_databases`, 11 клієнтів змаповано, unmapped перелічені.
4. **Read-only доведено:** тест §1.4 зелений + у логах живого синку нема жодного не-whitelisted запиту.
5. Історія працює: змінити статус у Notion → після синку зміна видно на сторінці історії з правильними old/new.
6. UI-чеклист `AGENTS.md §15–16`; таблиця не скролить сторінку по горизонталі; порожній місяць має явний стан; `Not configured` без токена.
7. Банер про початок історії (§4) присутній.
8. Токен не зустрічається в жодній API-відповіді (`grep` по відповідях `/blog-plan/*`).

---

## 10. Follow-ups (свідомо поза скоупом)

- **План vs факт** — зіставити `posted` у Notion з краулом task-26, знайти «позначено Posted, а сторінки нема». Це `blog_publish_check` із follow-ups task-26; модель §2 його не блокує.
- **Клієнти поза дашбордом** (`Advanced Dental Art`, `Anita`, `BVI`) — зараз відсіюються (гучно). Якщо знадобляться — або завести в `clients.json`, або дозволити blog-only записи.
- **Двосторонній режим** — свідомо **ні**. Користувач: «Апі лише читання. зміни ні». Знімати цей інваріант — окреме рішення користувача, не побічний ефект іншої таски.
- **Алерти** («блог без руху 14 днів», «місяць почався, статуси Not started») — природне продовження поверх `blog_plan_changes`, окрема таска.
