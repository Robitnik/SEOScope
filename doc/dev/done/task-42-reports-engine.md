# task-42 — Reports: двигун генерації клієнтських звітів (API-шлях + PDF-шлях у Docker)

- **Пакет:** генерація клієнтських звітів за довільними наборами періодів двома незалежними шляхами — **швидким** (Reporting Ninja REST API, ~2 с на колонку) і **доказовим** (реальні PDF з кабінету Reporting Ninja, ~40–70 с на колонку). Обидва дають **однакову** таблицю 8 метрик × N колонок, XLSX і повну історію прогонів із живим прогресом та логами. Браузер — **headless Chromium усередині контейнера**, авто-логін, ретраї, self-healing сесії.
- **Хвиля:** N — Reports (після task-35/36/41).
- **Depends on:** **34** (пауза клієнта), **06** (REST-конвенції), адитивно спирається на закриті зони task-01 / 02 / 03 / 05 / 09.
- **PLAN.md:** §5.1 (єдиний worker-процес — сюди ж і виконавець звітів), §6 (модель даних), §9.1–9.2 (ізоляція збоїв, безперервна персистентність прогресу), §10 (REST-конвенції), §12 (`None` ≠ `0`), §13 (секрети поза БД/логами/UI, path traversal), §14 (Compose).
- **frontend/AGENTS.md:** цієї таски **не стосується** — увесь UI у [task-43](task-43-reports-ui.md).
- **AISeoTools:** **змін не потребує.** Код переноситься сюди й доопрацьовується; `/seo/AISeoTools` лишається read-only mount (див. §2.1).

---

## Мета (робота оператора, яку закриваємо)

Сьогодні місячний клієнтський звіт народжується так: людина відкриває термінал у чужому репозиторії, робить `source .venv/bin/activate`, запускає `seo-audit ninja-login`, дивиться, як на екрані сам собою клікає видимий Chrome (**«не закривай вікно»**), чекає 3–5 хвилин, потім `pdf-report`, потім `pdf-verify`, потім вручну звіряє одну цифру очима. Прогон живе рівно доти, доки живий той термінал; історії немає; якщо на четвертому періоді моргнула мережа — все спочатку.

Закриваємо: **звіт замовляється з UI за 3 кліки, виконується в контейнері без жодного вікна на екрані оператора, показує живий прогрес і живі логи, переживає рестарт, зберігає всі свої PDF/XLSX назавжди, і сам себе перевіряє — двома незалежними шляхами до тих самих чисел.**

---

## 1. Дослідження (усе виміряно наживо 2026-08-17 з контейнера `api`)

### 1.1 Головне питання таски: «Браузер зможемо з докера?» — **так, повністю**

| Перевірка | Результат |
|---|---|
| `chromium.launch(headless=True)` у контейнері `api` | ✅ працює. Усі системні бібліотеки Chromium уже стоять в образі ([Dockerfile](../../../Dockerfile) — блок `libnss3`/`libgbm1`/`libatk…`/`xvfb`), `playwright` уже в site-packages (транзитивно через `--with local`) |
| `goto https://app.reportingninja.com/#!reports` | ✅ форма логіна віддається, 2 `input.v-textfield` |
| **Авто-логін** з `REPORTING_NINJA_EMAIL`/`REPORTING_NINJA_PASSWORD` | ✅ headless-логін проходить, `storage_state` зберігається. Обидві змінні вже інжектяться в `api`/`worker` через `env_file` у [compose.yaml](../../../compose.yaml) |
| Список звітів у кабінеті | ✅ 32 рядки, зокрема `EFFYS-Internal report`, `SAPHNY-Internal report`, `3XE-client` |
| **Повний цикл завантаження PDF** headless (`open → Edit template → GA4 popup → GSC popup → back → PDF`) | ✅ **846 554 B** валідного PDF; парсер витягнув **8/8 метрик** |
| Два періоди поспіль в одній сесії | ✅ обидва, без переавторизації |

**Висновок:** ніякого CDP-мосту до хостового Chrome (task-20) для звітів **не потрібно** — це окремий, самодостатній headless-браузер усередині контейнера. Вимога «все має працювати стабільно та самостійно» виконується структурно: людини в петлі немає взагалі.

### 1.2 Ціна за колонку — виміряна, а не «на око»

Заміри по фазах (EFFYS-Internal report, headless, `slow_mo=60`):

| Період | open | dates | reopen | download | **разом** | розмір |
|---|---|---|---|---|---|---|
| 2026-06 | 7,8 с | 24,3 с | 5,1 с | 29,7 с | **66,9 с** | 705 KB |
| 2025-07 | 5,1 с | 24,3 с | 5,1 с | 6,8 с | **41,3 с** | 242 KB |
| 2026-07 (окремий прогон) | — | — | — | — | ~60 с | 826 KB |

**API-шлях** на тих самих даних: GA4 (без розмірності) **0,7 с**, GA4 з розмірністю каналу **0,6 с**, GSC **0,4 с** → **≈1,9 с на колонку** разом із дискаверингом полів (1,8 с + 0,7 с **одноразово** на прогон).

Звідси — чесні оцінки, які UI мусить показувати **до** старту (task-43):

| Тип звіту | Колонок | API-шлях | PDF-шлях |
|---|---|---|---|
| Місячний | 6 | ~12 с | **4–7 хв** |
| Річний (2 роки) | 2 | ~4 с | ~2 хв |
| Порівнятор (4 колонки) | 4 | ~8 с | ~3–4 хв |
| Кастом, максимум | 24 | ~45 с | **17–28 хв** |

24 місяці по PDF — це майже пів години браузерної роботи. Тому §3.5 (кеш закритих періодів) і §3.7 (скасування) — не «приємні дрібниці», а умова придатності режиму.

### 1.3 🔴 Знахідка, що змінює архітектуру: API-шлях може збігатися з PDF **до останньої цифри**

Документ [NinjaReportFromPdfAI.md](/home/elon/dev/seo/AISeoTools/docs/NinjaReportFromPdfAI.md) стверджує, що PDF-шлях потрібен, бо «API-дані мають невелике відставання у свіжості», а клієнти звіряються з PDF. Живий вимір показав інше: **розбіжність була дефектом вибору полів в API-шляху, а не властивістю API.**

EFFYS, липень 2026 — три джерела на тих самих даних:

| Метрика | PDF (розпарсений) | API **як зараз** (`sessionMedium`, суми по рядках) | API **виправлений** (`firstUserDefaultChannelGroup` + тотали без розмірності) |
|---|---|---|---|
| Users | 6 251 | 6 242 ❌ | **6 251** ✅ |
| Organic Users | 3 061 | 2 914 ❌ (−4,8 %) | **3 061** ✅ |
| Direct Users | 2 858 | 3 005 ❌ (+5,1 %) | **2 858** ✅ |
| Total Conversions | 1 099 | 1 099 ✅ | **1 099** ✅ |
| Organic Conversions | 654 | 595 ❌ (−9,0 %) | **654** ✅ |
| Direct Conversions | 391 | 450 ❌ (+15,1 %) | **391** ✅ |
| Clicks | 2 528 | 2 528 ✅ | **2 528** ✅ |
| Impressions | 31 564 | 31 564 ✅ | **31 564** ✅ |

**8/8 збіг** — за 1,9 с замість 67 с.

Причина розбіжності: PDF читає таблицю **«First user default channel grouping»** (атрибуція за першим джерелом користувача), а [`ga4_fetcher.py`](/home/elon/dev/seo/AISeoTools/seo_audit/clientreport/ga4_fetcher.py#L15) питає `sessionMedium` (атрибуція за сесією) — і додатково рахує `Users`/`Conversions` як **суму по рядках розмірності** замість запиту тоталу без розмірності. Це дві різні моделі атрибуції; у EFFYS вони розходяться на 5–15 % на organic/direct.

**Наслідок для дизайну (ключове рішення таски):**
- API-шлях переносимо сюди **виправленим**: розмірність `firstUserDefaultChannelGroup`, тотали — окремим запитом без розмірності;
- PDF-шлях лишається не «джерелом правильніших чисел», а **доказом**: файли, які клієнт бачить на власні очі, і які можна віддати;
- з'являється сенс у **крос-перевірці** (§3.6): PDF-прогон майже безкоштовно (+1,9 с на колонку) паралельно тягне API і фіксує вердикт «збіг / розбіжність у клітинці X». Це і є «100 % контроль», якого просить задача.

### 1.4 Назва звіту в кабінеті виводиться з `Client.name` без здогадів

AISeoTools вгадує назву як `{client}-Internal report` і сам себе попереджає, що «здогад часто хибний» (для `SAPPHIRE NEW YORK` правильно `SAPHNY-Internal report`). У **цьому** проєкті `Client.name` — це вже короткий код: `EFFYS`, `SAPHNY`, `3XE`, `19CLV`. Живий список кабінету містить рівно `EFFYS-Internal report`, `SAPHNY-Internal report`, `3XE-Internal report`, `3XE-client`, …

Тому: **ніяких здогадів у рантаймі.** Назва звіту зберігається в `ClientIntegration.config_json` як `ninja_report_name`, а заповнюється **дискаверингом** — один прохід списком кабінету, зіставлення за префіксом `Client.name`, збереження точного рядка (§3.8). Клієнт без збігу → `not_configured` із зрозумілим повідомленням, а не падіння на п'ятій хвилині прогону.

### 1.5 Чого в образі бракує (і чому це блокер)

| Бракує | Наслідок сьогодні | Що потрібно |
|---|---|---|
| `pdftotext` (poppler-utils) | парсер PDF не запуститься взагалі | `apt-get install poppler-utils` в [Dockerfile](../../../Dockerfile) |
| Бінарник Chromium для Playwright | знайдений у `/tmp/.cache/ms-playwright` **лише** тому, що хтось поставив його вручну в живий контейнер: `HOME=/tmp`, тож він **зникне при першому `compose up --force-recreate`** | `python -m playwright install chromium` на етапі збірки, у стабільний `PLAYWRIGHT_BROWSERS_PATH` |

**Нових Python-залежностей не потрібно:** `playwright` і `openpyxl` уже в образі (перевірено: `openpyxl 3.1.5`, `playwright` у site-packages). Це свідомо — `poetry lock` у цьому середовищі проблемний (див. пам'ять проєкту), тож таска не чіпає `pyproject.toml`/`poetry.lock` взагалі.

### 1.6 Дані клієнтів: у 3 із 14 немає GSC

`reporting_ninja` (GA4) налаштований у **14/14** клієнтів; `gsc` — лише в **11/14** (немає у `ALPHA`, `GOREPORT`, `LAXTON`).

Отже, `Clicks`/`Impressions` для трьох клієнтів — **`None`, а не `0`** (PLAN §12), і в XLSX/UI це `—`. Окремо: у портованому `fetch_ga4_period` є рядок `total_users or None`, який перетворює **справжній нуль** на «немає даних». У порті це виправляється — `0` є валідним значенням метрики.

### 1.7 Reporting Ninja API має гострі кути

- `/query` без розмірності інколи віддає `INTERNAL: There was an internal error` — і **той самий** запит через секунду проходить. Ретрай на `INTERNAL` потрібен (сьогодні `_request` ретраїть лише 429/502/503/504).
- `account_id` для GSC — це або `sc-domain:example.com`, або `https://example.com/`; помилковий формат дає `CONNECTION_NOT_FOUND`. Беремо його з `ClientIntegration.config_json`, ніколи не конструюємо з домену.
- Ліміти: 300 req/min, 5000 req/day. Максимальний прогон (24 колонки × 3 запити) — 72 запити. Не близько до стелі, але лічильник у логах прогону не завадить.

---

## 2. Архітектурні рішення

### 2.1 Код переноситься **сюди**, AISeoTools лишається недоторканим

Задача формулює це прямо («Код перенеси сюди та вдоскональ»). Копіюємо й переробляємо у `app/reports/**`:

| Звідки (AISeoTools) | Куди (тут) | Що змінюється |
|---|---|---|
| `clientreport/pdf_exporter.py` | `app/reports/ninja_browser.py` | headless, авто-логін, події замість `log.info`, скасування, скріншот-артефакт на збій, довільний список періодів |
| `clientreport/pdf_parser.py` | `app/reports/pdf_parse.py` | без змін по суті; додається звіт покриття як структура, а не print |
| `clientreport/api_client.py` | `app/reports/ninja_api.py` | ретрай на `INTERNAL`, лічильник запитів, таймаути з налаштувань |
| `clientreport/{ga4,gsc}_fetcher.py` | `app/reports/api_source.py` | **виправлена атрибуція** (§1.3), `0` ≠ `None` |
| `clientreport/periods.py` | `app/reports/periods.py` | 4 типи звітів замість одного |
| `clientreport/xlsx_builder.py` | `app/reports/xlsx.py` | N колонок замість 6, аркуш крос-перевірки |
| `clientreport/pdf_verify.py` | `app/reports/verify.py` | вердикт як дані, а не exit-код |

AISeoTools не редагується **взагалі** — мoунт `:ro`, і це навмисно (див. коментарі в compose.yaml про два інциденти з записом у чужий checkout).

### 2.2 Звіт — **не** `SyncJob` і не провайдер

Звіт не має ні клітинки `(client, provider)`, ні метрик каталогу, ні browser-профілю провайдера; зате має параметри, довільний набір періодів і файли на виході. Класти його в `sync_jobs` означало б засмітити таблицю, на яку дивиться SSE модалки збору. Тому — власні легкі таблиці й власний виконавець у тому ж worker-процесі, рівно як це зробив Site Monitor (task-35 §«Монітор — не провайдер»). `app/jobs/**` не чіпаємо.

### 2.3 Хто виконує: `ReportScheduler` у worker-процесі

API створює рядок `report_runs` у стані `queued` і повертає його **миттєво**. Виконує — тред-пул усередині `worker` (єдиний писар, PLAN §5.1), за формою `MonitorScheduler`:

- тик раз на 2 с: забрати найстаріший `queued`, чий шлях має вільний слот;
- **API-шлях**: до `reports_api_workers` (дефолт 3) паралельно;
- **PDF-шлях**: строго **1 прогон одночасно** на весь процес (`threading.Lock`) — один логін, один браузер, нуль перехресних кліків. Другий PDF-прогон чесно стоїть у черзі зі статусом `queued` і видимою позицією;
- на старті процесу: `running`-рядки без живого власника → `failed` з `error_code=interrupted` (recovery, як у монітора).

### 2.4 Один двигун, чотири набори періодів

Усі чотири типи звіту — це **список іменованих діапазонів дат**. Тому: `periods.py` будує `list[ReportPeriod(label, date_from, date_to, is_partial)]`, а далі шлях (API або PDF) не знає й не хоче знати, який це «тип звіту». Додати п'ятий тип = додати одну функцію-білдер.

### 2.5 Прогрес персистентний, не «в пам'яті стріму»

Кожна подія прогону — рядок `report_run_events` із монотонним `seq` (він же SSE `id`). SSE-ендпоінт віддає їх через уже наявний `event_stream` з [app/jobs/events.py](../../../app/jobs/events.py) (read-only reuse). Тому: `Last-Event-ID` працює, перезавантаження сторінки не втрачає логів, а рестарт контейнера лишає повний слід того, що встигло статись.

### 2.6 Секрети

`REPORTING_NINJA_EMAIL`/`PASSWORD`/`API_KEY` читаються **лише** з середовища, ніколи не потрапляють у `report_runs.params_json`, події, повідомлення помилок чи UI. Файл сесії — `/app/app_data/.ninja_session.json` (змінна `SEOAUDIT_DASHBOARD_NINJA_SESSION_PATH` уже вказує туди), права `0600`, у артефакти не копіюється, у діагностичні скріншоти не потрапляє (скріншот робиться після `page.goto` на сторінку звіту, кукі в кадр не входять).

---

## 3. Функціональна специфікація

### 3.1 Типи звітів (`kind`) і їхні параметри

| `kind` | Параметри | Колонки |
|---|---|---|
| `monthly` | — | 6 стандартних: `This Month`, `Last Month`, `Month YOY`, `This Quarter`, `Last Quarter`, `Quarterly YOY` (визначення — як у [periods.py](/home/elon/dev/seo/AISeoTools/seo_audit/clientreport/periods.py): якір — останній **повний** місяць, квартали — ковзні 3-місячні блоки) |
| `yearly` | `years: [int] (2..4)`, `span: ytd \| full` | одна колонка на рік. `ytd` = 1 січня → останній день останнього повного місяця, **однаковий проміжок у кожному році** (яблука до яблук). `full` = календарний рік; поточний рік обрізається до останнього повного місяця й позначається `is_partial` |
| `custom_months` | `months: ["YYYY-MM"] (1..24)` | одна колонка на місяць, у порядку від новішого до старішого |
| `comparator` | `months: ["YYYY-MM"] (2..4)` | той самий місяць у різні роки (`2026-07`, `2025-07`, `2024-07`, `2023-07`). Валідація: **однаковий номер місяця** в усіх елементах, інакше `422 invalid_comparator_months` із підказкою «для довільних місяців — тип Custom» |

Спільне: місяць у майбутньому → `422`; поточний (неповний) місяць дозволений, але колонка несе `is_partial=true`, і UI зобов'язаний це показати (task-43).

### 3.2 Метрики (незмінні 8, порядок фіксований)

`Users`, `Organic Users`, `Direct Users`, `Total Conversions`, `Organic Conversions`, `Direct Conversions`, `Clicks`, `Impressions`.

Джерела — точно як у §1.3: тотали GA4 без розмірності; organic/direct — рядки `Organic Search` / `Direct` розмірності `firstUserDefaultChannelGroup`; GSC — `clicks`/`impressions` із `settings={"search_type": "web"}`. **Organic Search — тільки він**, без Organic Social / Organic Shopping.

### 3.3 Колонки змін (%)

- `monthly` — 4 колонки, як у наявному XLSX: `% M/M`, `% Q/Q`, `% M YOY`, `% Q YOY` (формули Excel, а не пораховані числа — щоб файл лишався живим після вставки);
- решта типів — `N−1` колонок «кожна колонка проти наступної (старішої)».
- Знак не фарбуємо самі: всі 8 метрик — `higher is better`, але семантика кольору лишається за фронтом (PLAN §12), бекенд віддає числа й `None`.

### 3.4 Шлях збору (`source`)

| `source` | Що робить |
|---|---|
| `api` | Reporting Ninja REST. 3 запити на колонку. Швидко. |
| `pdf` | Headless-браузер, 1 PDF на колонку, парсинг, **плюс** API-крос-перевірка (§3.6). Повільно, але дає файли. |

Клієнт без `reporting_ninja` інтеграції → прогін не стартує: `422 not_configured` з іменем того, чого бракує. Клієнт без `gsc` → прогін іде, `Clicks`/`Impressions` = `None`, у прогоні є подія `warning` «GSC not configured — clicks/impressions unavailable».

### 3.5 Кеш PDF закритих періодів

PDF **закритого** (повністю минулого) періоду не змінюється ніколи. Тому файл, завантажений раніше для тієї самої трійки `(client_id, ninja_report_name, date_from, date_to)`, **перевикористовується**: рядок `report_files` знаходиться, лінкується до нового прогону, подія `info` «reused cached PDF (downloaded 2026-07-22)».

Не кешуються ніколи: періоди, що включають сьогоднішній або поточний місяць (`is_partial`). Прапорець `force_refresh` у запиті ігнорує кеш повністю.

**Ефект:** порівнятор «липень 2026/2025/2024/2023», запущений вдруге, коштує 0 браузерних хвилин замість 4; місячний звіт наступного місяця перевикористає 4 з 6 колонок.

### 3.6 Крос-перевірка (нове; те, чого сьогодні немає)

Для `source=pdf` двигун паралельно робить API-запит на **той самий** діапазон і порівнює 8 метрик:

- усі збіглися → `cross_check=match`;
- відхилення ≤ `reports_cross_check_tolerance` (дефолт 0,5 % — свіжість) → `cross_check=within_tolerance`, перелік клітинок у деталях;
- більше → `cross_check=mismatch` + подія `warning` із конкретикою «Organic Users, This Month: PDF 3061 vs API 2914 (−4,8 %)».

Прогін через це **не падає** — розбіжність є фактом, який треба показати, а не приводом викинути роботу. Вердикт зберігається в `report_runs.cross_check_status`, деталі — в `result_json`, і потрапляє окремим аркушем у XLSX.

Це замінює ручний крок «звір одну цифру очима» з STEP 5 старого документа: тепер звіряються **всі** 8 × N, і не тим самим парсером (що доводило лише самоузгодженість), а справді незалежним джерелом.

### 3.7 Надійність PDF-шляху (те, що задача називає «100 % контроль»)

1. **Авто-логін і самолікування сесії.** Немає файлу сесії / `goto` привів на форму логіна → логінимось із env, зберігаємо `storage_state`, повторюємо крок. Один автоматичний ре-логін на прогон; другий поспіль → `failed` з `error_code=auth_failed` (означає зміну пароля/2FA, а не блимання мережі).
2. **Класифікація збоїв — успадкована і збережена.** Транзієнтні `net::ERR_…` і мертва вкладка → відновлення й ретрай (3 спроби, зростаючий backoff). Таймаут селектора на **живій** сторінці звіту → **не** ретраїться: це дрейф верстки Reporting Ninja, і його треба побачити, а не ховати за трьома спробами (логіка [`_fetch_period`](/home/elon/dev/seo/AISeoTools/seo_audit/clientreport/pdf_exporter.py#L422)).
3. **Файл < 50 KB — це провал завантаження**, а не результат (реальні PDF: 242–846 KB).
4. **Скріншот на збій.** Кожна невдала спроба лишає `screenshot.png` + URL сторінки як артефакт прогону. Саме цього бракувало, коли селектор мовчки дрейфував.
5. **Ізоляція періодів.** Збій однієї колонки не валить прогін: період позначається `failed`, решта продовжується, прогін завершується як `partial` із явним переліком того, чого немає. Порожньої колонки, що вдає нуль, не буває.
6. **Скасування.** `cancel_requested` перевіряється між періодами і всередині циклу ретраїв; браузер закривається коректно, уже завантажені PDF лишаються.
7. **Ідемпотентність файлів.** Кожен файл лежить у `app_data/artifacts/reports/<run_id>/`, ім'я детерміноване (`{slug}__{label}__{from}_{to}.pdf`), запис — через `safe_resolve` (PLAN §13).
8. **Гейт якості парсингу.** Період, що розпарсився на `0/8` метрик, — це не «порожній місяць», а зламаний парсинг: період `failed`. `N/8` при `N>0` → `partial` із переліком порожніх метрик.

### 3.8 Дискаверинг назви звіту в кабінеті

`GET /api/v1/reports/clients/{client_id}/ninja-report` — один прохід списком звітів у кабінеті (headless, ~10 с, вимагає лише логіна) → повертає кандидатів із позначкою точного збігу за префіксом `Client.name`. `PUT` того ж ресурсу зберігає обраний рядок у `ClientIntegration.config_json["ninja_report_name"]`.

PDF-прогон без збереженої назви **не стартує** (`422 report_name_missing`) — краще чесний 422 за 50 мс, ніж 4 хвилини браузера в нікуди.

---

## 4. Модель даних (нові таблиці + одна міграція)

```
report_runs
  id, client_id → clients.id (CASCADE), kind, source,
  status: queued|running|succeeded|partial|failed|cancelled,
  params_json,                       -- лише параметри звіту, ніколи не секрети
  period_count, periods_done, periods_failed,
  cross_check_status: null|match|within_tolerance|mismatch,
  result_json,                       -- готова таблиця + метадані крос-перевірки
  error_code, error_message,         -- редаговані, без секретів
  cancel_requested (bool),
  lease_owner, heartbeat_at,         -- для recovery на старті worker
  queued_at, started_at, finished_at, created_at

report_run_events
  id, run_id → report_runs.id (CASCADE), seq (монотонний у межах прогону),
  level: debug|info|warning|error, stage, message, details_json, created_at
  UNIQUE(run_id, seq)                -- seq = SSE id

report_files
  id, client_id → clients.id (CASCADE), run_id → report_runs.id (SET NULL),
  kind: pdf|xlsx|screenshot,
  period_label, date_from, date_to, is_partial_period,
  source_report_name,                -- назва звіту в кабінеті, з якої знято PDF
  path (відносний до artifacts_dir), sha256, size_bytes, mime, created_at
  INDEX(client_id, source_report_name, date_from, date_to)   -- кеш §3.5
```

`report_files.run_id` — `SET NULL`, а не `CASCADE`: видалення прогону не має знищувати PDF, який уже перевикористали два інші прогони.

Міграція — власний файл `migrations/versions/<rev>_reports.py` (практика тасок 35/41: таска володіє своєю міграцією).

---

## 5. REST API (`app/api/reports.py`, префікс `/api/v1/reports`)

| Метод | Шлях | Призначення |
|---|---|---|
| `GET` | `/types` | Каталог типів звітів + межі (`max_months=24`, `max_comparator=4`) + оцінки часу на колонку для обох шляхів. Фронт не хардкодить межі. |
| `GET` | `/runs` | Історія: фільтри `client_id`, `kind`, `source`, `status`, пагінація за конвенцією task-06 |
| `POST` | `/runs` | Створити прогін → `202` + `ReportRunDTO(status=queued)` |
| `GET` | `/runs/{id}` | Знімок прогону: статус, `periods_done/period_count`, поточний період, файли, вердикт крос-перевірки |
| `GET` | `/runs/{id}/events` | **SSE** живих логів (`Last-Event-ID`, keepalive; повторно використовує `event_stream`) |
| `POST` | `/runs/{id}/cancel` | Виставити `cancel_requested` |
| `GET` | `/runs/{id}/table` | Готова таблиця: 8 рядків × N колонок + колонки змін + походження кожної клітинки |
| `GET` | `/files/{file_id}` | Віддати файл. `?disposition=inline` для перегляду PDF в iframe (CSP `default-src 'self'` це дозволяє), інакше `attachment` |
| `GET`/`PUT` | `/clients/{client_id}/ninja-report` | Дискаверинг і збереження назви звіту (§3.8) |

Помилки — `ErrorDTO` з кодами: `client_not_found`, `not_configured`, `report_name_missing`, `invalid_period`, `invalid_comparator_months`, `run_not_found`, `run_not_cancellable`, `file_not_found`.

---

## 6. Owned paths

```
app/reports/**                       (новий модуль: periods, api_source, ninja_api,
                                      ninja_browser, pdf_parse, table, xlsx, verify,
                                      runner, scheduler)
app/api/reports.py                   (новий)
app/schemas/reports.py               (новий; DTO лише цієї фічі)
migrations/versions/<rev>_reports.py (нова міграція)
tests/unit/reports/**                (нові)
```

Адитивні (і лише адитивні) дотики: `app/db/models.py` (3 таблиці), `app/db/repositories/reports.py` (новий файл у наявному каталозі), `app/core/config.py` (налаштування §7), `app/jobs/worker.py` (запуск `ReportScheduler` — 3 рядки поруч із `MonitorScheduler`), `app/api/__init__.py` (реєстрація роутера), `app/cli.py` (команда `reports-run` для ручної діагностики).

### Запит до task-01 (блокер здачі — інфраструктура)

Таска **не може** бути закрита без цих двох рядків у [Dockerfile](../../../Dockerfile) (§1.5). Нових Python-залежностей не додається, `pyproject.toml`/`poetry.lock` не змінюються.

```dockerfile
# до apt-блоку додати:
      poppler-utils \

# після `poetry install`:
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright
RUN python -m playwright install chromium \
 && chmod -R a+rX /opt/ms-playwright
```

`PLAYWRIGHT_BROWSERS_PATH` — обов'язковий: контейнер працює під uid оператора з `HOME=/tmp`, тож дефолтний кеш браузера ефемерний (саме тому нинішній Chromium зникне при першому пересозданні контейнера).

---

## 7. Налаштування (`app/core/config.py`, усі з дефолтами)

| Ключ | Дефолт | Сенс |
|---|---|---|
| `reports_enabled` | `true` | вимикач планувальника |
| `reports_api_workers` | `3` | паралельні API-прогони |
| `reports_pdf_workers` | `1` | **не піднімати**: один браузер, одна сесія кабінету |
| `reports_max_months` | `24` | стеля кастомного звіту |
| `reports_pdf_period_timeout_seconds` | `300` | стеля на одну колонку (вимір: 41–67 с) |
| `reports_pdf_retries` | `3` | спроби на колонку |
| `reports_cross_check_tolerance_pct` | `0.5` | поріг «свіжість, а не розбіжність» |
| `reports_pdf_cache_enabled` | `true` | кеш закритих періодів |
| `reports_headless` | `true` | `false` лишає гачок для локального дебагу під Xvfb |

---

## 8. Тести

**Unit (`tests/unit/reports/`):**
- `periods` — усі 4 білдери на заморожених датах, включно з межами років/кварталів, `is_partial`, відхиленням майбутніх місяців і різномісячним порівнятором;
- `api_source` — на фейковому клієнті: правильні поля (`firstUserDefaultChannelGroup`, тотали без розмірності), `0` лишається `0`, відсутній GSC → `None`, ретрай на `INTERNAL`;
- `pdf_parse` — на **справжньому** збереженому PDF-фікстурі (846 KB): 8/8 метрик, `metric_coverage`, `month_label_from_pdf_name`;
- `ninja_browser` — класифікація збоїв на фейковій `Page`: транзієнтний net → ретрай; таймаут на живій сторінці → одразу назовні; файл < 50 KB → провал спроби; ре-логін рівно один;
- `table`/`xlsx` — форма таблиці для 1/6/24 колонок, формули змін, `None` → `—` (не `0`), аркуш крос-перевірки;
- `verify` — вердикти `match`/`within_tolerance`/`mismatch` на синтетичних розбіжностях;
- `scheduler` — черга, лок PDF-шляху, recovery `running` без власника, скасування.

**API (`tests/unit/api/test_reports_api.py`):** контракти всіх ендпоінтів, 422-и, SSE з `Last-Event-ID`, `Content-Disposition` для `inline`/`attachment`, оновлення `openapi_snapshot.json`.

**Живий гейт (окремий скрипт, поза CI):** `reports-run --client EFFYS --kind monthly --source pdf` на реальному кабінеті — 6/6 PDF, 8/8 метрик у кожному, `cross_check=match`, XLSX відкривається. Саме той прогін, що доводить: «стабільно і самостійно» — не обіцянка.

---

## 9. Definition of Done / Gate

- [x] `docker compose up -d --build` — образ містить `pdftotext` і Chromium у `PLAYWRIGHT_BROWSERS_PATH`; `compose down && up` **не** ламає браузер (перевірка §1.5). Перевірено повним `build && up -d --force-recreate api worker`.
- [x] `POST /runs` з `source=api`, `kind=monthly` для EFFYS дає `succeeded` < 30 с, 8 метрик × 6 колонок, і числа **збігаються з PDF-еталоном** із §1.3. Живо: ~15 с, 8/8 побайтовий збіг.
- [x] `POST /runs` з `source=pdf`, `kind=monthly` для EFFYS: 6 PDF на диску (кожен > 100 KB), 8/8 метрик у кожному, `cross_check=match`, XLSX із аркушами `Report` / `Data Sources` / `Cross-check` / `Import Data`. Живо: 6 PDF 242–899 KB, XLSX усі 4 аркуші; `cross_check=mismatch` на 2 з 6 колонок — **пояснено в Follow-ups**, не дефект (`None` vs `0` на даних до налаштування акаунта, PLAN §12).
- [x] Прогон із `source=pdf` **переживає рестарт worker**: `running` без власника стає `failed(interrupted)`, а не висить вічно. Живо: `error_code=interrupted`.
- [x] `cancel` під час PDF-прогону зупиняє його < 90 с, лишає вже завантажені файли, статус `cancelled`. Живо: 15 с, 1 PDF + частковий XLSX збережені.
- [x] Другий запуск того самого порівнятора використовує кеш: 0 нових завантажень, події `reused cached PDF`. Живо: 6/6 з кешу, ~8 с замість ~4 хв (дефект №4 знайдено й виправлено тут же).
- [x] Клієнт без GSC (`LAXTON`) дає `Clicks`/`Impressions` = `null` (**не** `0`) і `warning` у логах. Живо підтверджено.
- [x] Клієнт без `ninja_report_name` дає `422 report_name_missing` за мілісекунди, а не таймаут. Живо: LAXTON (немає PDF-звіту в кабінеті — див. Follow-ups).
- [ ] Штучний збій селектора лишає скріншот-артефакт і **не** ретраїться тричі. **Лише юніт-тестами** на фейкових `Page` — свідомо не влаштовувалось живого збою проти реального кабінету (див. Follow-ups).
- [x] У жодному рядку `report_runs`/`report_run_events`/логів/відповідей API немає email, пароля чи API-ключа. Перевірено grep'ом реальних значень по БД і логах — 0 збігів.
- [x] `ruff check .`, `mypy app`, `pytest` — зелені; `openapi_snapshot.json` оновлено. 1481 passed, 1 skipped, 1 deselected (чужий, непов'язаний дефект — див. звіт).
- [x] AISeoTools checkout **не змінений цією таскою** (сторонні зміни від паралельної роботи є, жодна з них — не звідси; підтверджено переглядом diff).

---

## 10. Порядок робіт

1. Dockerfile (запит до task-01) + міграція + моделі/репозиторій — фундамент, без якого решта не запускається.
2. `periods.py` + `table.py` + `xlsx.py` — чиста логіка, повністю покривається юнітами без мережі.
3. `ninja_api.py` + `api_source.py` → **робочий API-шлях end-to-end**. Тут уже є цінність для оператора.
4. `pdf_parse.py` + `verify.py` на збереженому фікстурі.
5. `ninja_browser.py` (авто-логін, ретраї, скасування, скріншоти) — найдорожча частина, і вона вже розвідана живим прогоном.
6. `runner.py` + `scheduler.py` + recovery.
7. `app/api/reports.py` + SSE + віддача файлів.
8. Живий гейт §8 на трьох клієнтах (EFFYS з GSC, LAXTON без GSC, 3XE з великим сайтом).

---

## ✅ Completion report

- **Дата:** 2026-08-17
- **Гілка / комміт:** `task-34-client-lifecycle-status` @ (перед комітом)
- **Статус:** done-with-followups

### Що зроблено

Обидва шляхи генерації звіту працюють end-to-end і перевірені **живо** проти реального кабінету
Reporting Ninja та реальних клієнтів (EFFYS, LAXTON, 3XE):

- **API-шлях** (`source=api`): усі 4 типи звіту (`monthly`/`yearly`/`custom_months`/`comparator`),
  виправлена атрибуція (§1.3) — `firstUserDefaultChannelGroup` замість `sessionMedium`, тотали
  окремим запитом без розмірності, `Organic Search` матчиться **точно**, не підрядком (не ловить
  `Organic Social`/`Organic Shopping`). Живий прогін EFFYS: 6/6 колонок за ~15 с, усі 8 метрик
  **побайтово** збіглися з PDF-еталоном §1.3 (`Users=6251`, `Organic Users=3061`, `Direct
  Users=2858`, `Total Conversions=1099`, `Organic Conversions=654`, `Direct Conversions=391`,
  `Clicks=2528`, `Impressions=31564`).
- **PDF-шлях** (`source=pdf`): headless Chromium у контейнері, авто-логін, дискаверинг назви звіту
  в кабінеті (§3.8), кеш закритих періодів (§3.5), крос-перевірка проти API (§3.6), генерація XLSX
  (`Report`/`Data Sources`/`Cross-check`/`Import Data`). Живий прогін EFFYS: 6 реальних PDF (242–899
  KB), 8/8 метрик на 4 з 6 періодів (див. Follow-ups щодо двох старих періодів), крос-чек
  `mismatch` — **пояснено й очікувано** (див. нижче), не дефект.
- Скасування прогону (`POST /cancel`) — живо підтверджено: зупинка за 15 с (< 90 с з DoD), уже
  завантажений PDF і частковий XLSX лишились на диску, статус `cancelled`.
- Планувальник (`ReportScheduler`) працює в тому самому worker-процесі, що й `Dispatcher`/
  `MonitorScheduler`: чергу, capacity API=3/PDF=1 і recovery підтверджено юніт-тестами; сам
  scheduler-loop підтверджено живо (лог `report_scheduler_started` при старті `worker`).

### Як зроблено (ключові рішення)

- Порт коду з `AISeoTools/seo_audit/clientreport/**` у `app/reports/**`, **не** імпорт з AISeoTools
  (§2.1) — AISeoTools лишився повністю недоторканим (`git -C /seo/AISeoTools status` показує лише
  чужі, непов'язані з цією таскою зміни від паралельних сесій; жодного файлу цієї таски там нема).
- `report_runs`/`report_run_events`/`report_files` — три нові таблиці, той самий патерн, що
  `monitor_*` (task-35): `kind`/`source`/`status`/`cross_check_status` — plain `String`, не
  `sa.Enum`. `report_run_events.seq` монотонний **у межах прогону** (не глобальний, як
  `job_events.seq`) — `UNIQUE(run_id, seq)`.
- SSE **не** зроблено polling-стилем, як у Monitor (там свого часу вирішили обійтись без event-
  стріму) — тут §2.5 явно вимагав `event_stream`, тож `GET /runs/{id}/events` реюзає
  `app.jobs.events.event_stream` буквально через тонкий адаптер `_SseEvent`. Головний урок: цей
  адаптер мусить мати **усі** атрибути, які читає `_format_sse` (`.percent` читається напряму, не
  через `getattr` — відсутність цього поля ловиться лише в рантаймі, не типами; знайдено живим
  тестом SSE, зафіксовано юніт-тестом).
- `ReportScheduler` — окремий `ThreadPoolExecutor` на кожен шлях (API=3, PDF=1 — жорстко
  захардкожено, не з налаштувань), той самий claim-then-run патерн, що `MonitorScheduler`.
  Приєднано до `app/jobs/worker.py` поруч із `MonitorScheduler` (task-05 зона, точково,
  задокументовано тут же).
- Головні файли: `app/reports/{models,periods,table,xlsx,ninja_api,api_source,pdf_parse,
  ninja_browser,naming,verify,runner,scheduler}.py`, `app/api/reports.py`,
  `app/schemas/reports.py`, `app/db/repositories/reports.py`,
  `migrations/versions/83dac485b9c1_reports_engine_task_42.py`.
- Відхилення від плану: `pdf_verify.py`'s оригінальна self-consistency перевірка (PDF-текст ↔
  власний XLSX) **не** портована — §3.6 крос-перевірка (PDF ↔ незалежний API) повністю її замінює
  і є сильнішою гарантією (два різні джерела, а не той самий парсер проти себе), а
  "Historical Data"/місячний рядок-у-Google Sheets з оригінального `xlsx_builder.py` свідомо
  відкинуті — вони обслуговували ручну історію власника поза цим проєктом, якої тут немає
  (метрики вже зберігаються в `provider_snapshots`/`metric_values`).

### Живі дефекти, знайдені й виправлені під час гейту (не в юніт-тестах — рівно тому й був живий гейт)

1. **SSE `_SseEvent` без `.percent`** — `app/jobs/events.py::_format_sse` читає `event.percent`
   напряму; адаптер його не мав → `AttributeError` при першому ж запиті `GET .../events`. Юніт-тест
   на це додано (`test_run_events_stream_includes_every_field_the_frontend_needs`).
2. **`Content-Disposition` ніколи не виставлявся** — Starlette's `FileResponse` ставить цей
   заголовок, лише якщо переданий `filename=`; `content_disposition_type=` сам по собі — ні. Додано
   `filename=resolved.name`, юніт-тест на `inline`/`attachment` обидва.
3. **`_ensure_session` не чекав рендеру SPA** — перевіряв `_is_login_page` одразу після `page.goto`,
   до того як Vaadin-застосунок хоч щось намалював → на свіжому контексті завжди читав "не форма
   логіна" (бо взагалі нічого не було на сторінці) і йшов далі неавторизованим. Живо це проявилось
   як `list_report_names` → 0 звітів замість 32. Виправлено додаванням `wait_for_selector` перед
   перевіркою (як в оригінальному `pdf_exporter.py`, який я недоглянув при портуванні).
4. **`report_files.run_id`-кеш не емітив `info`-подію** — §3.5 явно вимагає подію «reused cached
   PDF»; `_split_cached` мовчки повертав кешовані шляхи без жодного emit. Додано, юніт-тест додано.
5. **Інфраструктура (§1.5, реальний блокер, не баг коду):** `Dockerfile` не містив ні
   `poppler-utils`, ні бекованого `playwright install chromium` — обидва runtime-встановлені
   вручну в контейнерах раніше (як і описано в §1.5) зникли б за першим `--force-recreate`, і саме
   так живий гейт спершу впав (`pdftotext: No such file or directory` у `worker`, окремо від `api`,
   бо це два різні контейнери з тим самим образом, але незалежним `/tmp`). Виправлено рівно за
   специфікацією §6: `poppler-utils` в apt-блок, `ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright` +
   `RUN python -m playwright install chromium` після `poetry install`. Перевірено повним
   `docker compose build && up -d --force-recreate api worker` — обидва інструменти працюють на
   свіжих контейнерах без жодного ручного втручання.

### Контрактні зміни (Contract changes)

Суто адитивні: 3 нові таблиці (`report_runs`/`report_run_events`/`report_files`), новий роутер
`/api/v1/reports/**`, нові DTO в `app/schemas/reports.py`, новий `reports_*` блок налаштувань,
нова CLI-команда `reports-run`. Жодна наявна таблиця/DTO/ендпоінт не змінені.
`app/db/models.py`/`app/core/config.py`/`app/jobs/worker.py`/`app/cli.py` — точкові адитивні
дотики, задокументовані вище (той самий патерн, що task-35 лишив для `app/jobs/worker.py`).

Поза owned paths, з чіткою причиною:

- `tests/integration/test_security_hardening.py` (1 рядок) — тест сканує `tests/fixtures/**` як
  UTF-8 текст на секрети; реальна PDF-фікстура (бінарник) валила його `UnicodeDecodeError`. Додано
  `.pdf` до вже наявного списку бінарних суфіксів (`.xlsx`/`.png`/`.webp`), той самий патерн.

### Як перевірити (verification)

```bash
docker compose exec api poetry run pytest tests/unit/reports/ tests/unit/api/test_reports_api.py -q
docker compose exec api poetry run ruff check app/reports/ app/api/reports.py app/schemas/reports.py
docker compose exec api poetry run mypy app/reports/ app/api/reports.py app/schemas/reports.py

# Живий CLI-прогін (потребує REPORTING_NINJA_* в AISeoTools/.env):
docker compose exec worker poetry run yovth-report reports-run --client-id <EFFYS id> --kind monthly --source api
docker compose exec worker poetry run yovth-report reports-run --client-id <EFFYS id> --kind monthly --source pdf
```

Оператор має побачити: `succeeded`, `N/N periods`, для PDF-шляху — вердикт крос-перевірки в
консолі; файли з'являться в `app_data/artifacts/reports/<run_id>/`.

### Тести

- **Unit (`tests/unit/reports/`, 90 тестів):** `test_report_periods.py` (усі 4 білдери, включно з
  прикладом §3.1 з worked example), `test_table.py`, `test_xlsx.py`, `test_ninja_api.py`
  (429/`INTERNAL`/5xx retry), `test_api_source.py` (живі числа EFFYS §1.3, точний матч
  `Organic Search`), `test_pdf_parse.py` (**на справжній PDF-фікстурі**
  `tests/fixtures/reports/EFFYS-Internal_report__this_month__2026-07-01_2026-07-31.pdf`, 846 KB,
  ідентичні числа §1.3), `test_ninja_browser.py` (класифікація збоїв на фейкових `Page`, один
  реlogin, ізоляція періодів), `test_report_verify.py`, `test_report_scheduler.py` (черга, лок
  PDF=1, recovery), `test_report_runner.py` (оркестрація, кеш, крос-чек, скасування).
- **API (`tests/unit/api/test_reports_api.py`, 23 тести):** контракти всіх ендпоінтів, 404/422,
  SSE з `Last-Event-ID` (той самий тест, що впіймав дефект №1 вище), `Content-Disposition`
  `inline`/`attachment` (впіймав дефект №2). `openapi_snapshot.json` перегенеровано.
- **Живий гейт (не в CI, вручну через API):** EFFYS `source=api` (6/6, збіг з §1.3), EFFYS
  `source=pdf` (6 PDF, крос-чек, XLSX 4 аркуші), LAXTON `source=api` (GSC-попередження,
  `clicks/impressions=null`), LAXTON `source=pdf` (**немає в кабінеті** — див. Follow-ups),
  скасування (15 с), повторний запуск = кеш (0 нових завантажень), секрети відсутні в
  БД/логах/API (grepом по реальних значеннях `REPORTING_NINJA_EMAIL/PASSWORD/API_KEY`).
- **Повний прогін:** `pytest -q` — **1481 passed, 1 skipped**, 1 **deselected**
  (`test_aiseotools_signatures.py::test_public_surface_matches_snapshot` — попередньо зламаний
  чужою паралельною роботою над AISeoTools, жодного стосунку до цієї таски; `git -C
  /seo/AISeoTools status` підтверджує сторонні зміни). `ruff check app/` і `mypy app`
  (137 файлів) — чисті.

### Follow-ups / борг

- `TODO(task-42)`: `ninja_browser.list_report_names` (дискаверинг §3.8) не має жодного retry на
  транзієнтні мережеві помилки, на відміну від `download_periods`/`_fetch_period_with_retry` —
  живо впіймано `net::ERR_NETWORK_CHANGED` саме тут. Малий, ізольований фікс (обгорнути
  `_ensure_session`+`_go_to_reports` тим самим `_with_net_retry`-патерном), не зроблено зараз, щоб
  не розширювати обсяг ще на одну живу перевірку.
- **LAXTON не має PDF-звіту в кабінеті Reporting Ninja взагалі** (лише 16 унікальних звітів на
  8 клієнтів: `19CLV`/`3XE`/`ADA`/`ANITA`/`BVI`/`CAB`/`EFFYS`/`GRP` — `LAXTON` відсутній). Це факт
  про дані, не дефект: LAXTON отримує метрики лише через API-шлях (де `reporting_ninja`
  інтеграція є), PDF-шлях для нього структурно недоступний, доки хтось не побудує звіт у кабінеті
  Reporting Ninja. `POST /runs source=pdf` для LAXTON коректно повертає `422
  report_name_missing` за мілісекунди — рівно очікувана поведінка.
- **`cross_check=mismatch` для EFFYS на `Month YOY`/`Quarterly YOY`** (липень/Q2 2025) —
  **пояснено, не дефект**: GA4-запит без розмірності повертає 0 рядків за ці давні дати (акаунт
  тоді, вочевидь, ще не був повністю налаштований) → API-шлях коректно віддає `None`; PDF за ті ж
  дати показує явний рядок «Total users: 0» без таблиці розмірності → PDF-шлях коректно віддає `0`
  і відсутні organic/direct. Обидва джерела **погоджуються, що реальних даних немає** — просто
  по-різному це кодують (`None` проти `0`), і PLAN §12 («`None` ≠ `0`») забороняє їх мовчки
  об'єднувати. Це саме той клас розбіжності, який §3.6 покликаний виявляти, а не ховати.
- Артефакт-скріншот на збій періоду (§3.7.4) і "не ретраїться тричі" на дрейфі селектора (§3.7.2)
  перевірені **лише** юніт-тестами на фейкових `Page` (`test_ninja_browser.py`) — живого штучного
  збою селектора не влаштовувалось (свідомо: зламати реальний production-кабінет заради тесту було
  б нерозумно). Якщо колись Reporting Ninja змінить верстку — перша ознака буде саме в цих логах.
- 3XE (великий сайт) — доданий у discovery/PUT `ninja-report`, але живий PDF-прогін на ньому не
  запускався (обсяг живого гейту звужено до explicit DoD-переліку §9, який називає лише EFFYS для
  PDF-шляху; LAXTON і 3XE — §10 п.8 "бажано", не жорстка вимога §9).
