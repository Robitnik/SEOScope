# task-29 — Інструмент «Redirect Checker» на сторінці Tools

- **Пакет:** другий інструмент у хабі `#/tools` — **Redirect Checker**: оператор вставляє посилання (одне або список), інструмент показує **повний ланцюжок редіректів підряд, хоп за хопом** (URL → статус → наступний URL → … → фінальна сторінка), класифікує ланцюжок і зберігає результат в БД як перевірку з історією.
- **Хвиля:** I (розширення хабу Tools після task-27)
- **Depends on:** [task-27](../done/task-27-tools-page-and-alt-text-tool.md) ✅ — реєстр інструментів (`app/api/tools.py`), маршрути `#/tools/*`, `frontend/js/tools-page.js`, `frontend/css/tools.css`, SSRF-гард і no-cache fetch у `app/tools/alt_text/verify.py`.
- **PLAN.md:** §5.2 (дерево `app/`), §6 (модель даних), §10 (REST-конвенції, DTO), §12 (`None` ≠ `0`, окремі стани замість «ок/не ок»), §13 (безпека мережевих запитів, download).
- **frontend/AGENTS.md:** обов'язковий для всієї роботи у `frontend/`.
- **AISeoTools:** **не використовується** (як і task-27). Наявний `seo_audit/gsc/tools/redirect_check.py` (Playwright) — лише **референс**: він тягне Chromium і живе за bind-mount-ом, тож інструмент був би недоступний на CI/деплої. Наш трейсер — stdlib, self-contained. Playwright-режим для JS-редіректів — свідомий follow-up.

---

## Мета

### Що робить оператор сьогодні

Питання «а куди насправді веде це посилання» виникає постійно: після міграції сайту, після зміни структури URL, при розборі 3xx у Search Console, при перевірці backlink-ів. Зараз це `curl -IL` у терміналі (є не в кожного), «redirect checker» на випадковому сайті (дані клієнта — на чужому сервері, ліміт 5 URL) або клік у браузері, який **приховує проміжні хопи**: браузер покаже фінальну сторінку, але не те, що між нею й вставленим URL було два 302 і один meta-refresh.

### Що робить інструмент

1. Оператор вставляє **посилання** — одне або список (по одному в рядок; типовий вхід — колонка з Ahrefs/GSC-експорту).
2. Інструмент проходить ланцюжок **сам, хоп за хопом**, не дозволяючи HTTP-клієнту «проглотити» проміжні відповіді.
3. Показує ланцюжок **підряд, як список кроків**:
   ```
   1  http://old-site.com/page          301 Moved Permanently
   2  https://old-site.com/page         301 Moved Permanently
   3  https://new-site.com/page/        200 OK   ← final
   ```
   Кожен хоп несе: статус, `Location`, час відповіді, і **що саме змінилося** на цьому кроці (схема, хост, `www`, слеш, query, регістр).
4. Дає **вердикт по ланцюжку** й перелік прапорців-проблем (ланцюжок довший за один хоп, тимчасовий редірект там, де мусив бути постійний, петля, downgrade `https → http`, редірект на головну замість релевантної сторінки, обрив у 404/5xx, canonical фінальної сторінки ≠ фінальний URL).
5. **Кожна перевірка лежить у БД**: історія прогонів, повторний запуск окремого URL, експорт CSV/XLSX.

**Ключова інваріанта (як у task-27): інструмент нічого не вигадує.** Ланцюжок — це те, що реально віддав сервер. Якщо ми не змогли дійти до кінця (таймаут, бот-блок, ліміт хопів) — це **окремий стан** з причиною, а не «ланцюжок закінчився». `None` («не перевіряли») ≠ «редіректів немає» (PLAN §12). Прапорці подаються як **сигнали для людини**, а не як «сайт зламаний».

---

## Дослідження: чому це не «один GET з follow-redirects»

Тут вся складність, тому вона фіксується в чистому модулі з табличними тестами.

### 1. Стандартний клієнт ховає ланцюжок

`urllib.request.urlopen` за замовчуванням сам іде за 3xx і віддає **лише фінальну** відповідь; `response.geturl()` дасть фінальний URL, але не проміжні статуси. Тому редіректи треба **вимкнути** й ходити вручну:

- будуємо opener **без** `HTTPRedirectHandler` і з passthrough-`HTTPErrorProcessor` (щоб 3xx повертався як звичайний response, а не як `HTTPError`);
- беремо `Location`, робимо `urljoin` з поточним URL (`Location` часто відносний — це легальний RFC 7231, і саме тут ламаються наївні реалізації);
- повторюємо, поки не дійдемо до не-3xx, петлі або ліміту хопів.

### 2. Форми хопів, які треба витримати

| Форма | Приклад | Що робимо |
|---|---|---|
| Постійний | `301`, `308` | хоп із `permanence = permanent` |
| Тимчасовий | `302`, `307`, `303` | `permanence = temporary` + прапорець `temporary_redirect` |
| `303 See Other` після POST | | метод наступного хопа → `GET` (ми й так GET-ом ходимо, але фіксуємо в хопі) |
| Відносний `Location` | `Location: /new/` | `urljoin` |
| `Location` з пробілами / не-ASCII / percent-encoding | `Location: /статті/1` | `quote` небезпечних байтів перед запитом, оригінал показуємо як є |
| Кілька `Location` у відповіді | | беремо перший, ставимо note `multiple_location_headers` |
| `3xx` **без** `Location` | | обрив: `verdict = broken_chain`, detail «301 without a Location header» |
| `meta refresh` | `<meta http-equiv="refresh" content="0;url=/new/">` | хоп `kind = meta_refresh`, продовжуємо ланцюжок (у межах ліміту) |
| JS-редірект (`window.location`) | | **не** детектується stdlib-ом → чесний note `possible_js_redirect` на фінальній сторінці, якщо в HTML є `location.href`/`location.replace` присвоєння; вердикт не змінюємо |
| Петля | `A → B → A` | `verdict = loop`, показуємо, який саме URL повторився |
| Дуже довгий ланцюжок | > 25 хопів | `verdict = too_many_hops` (не «ок») |
| Cookie-wall / бот-блок | `403`, `429`, Cloudflare-челендж | `verdict = blocked` + підказка «сайт блокує не-браузерні запити; перевірте вручну» |
| Фінал 404/410/5xx | | `verdict = broken` (редірект веде в нікуди — найдорожча знахідка) |
| `https → http` десь у ланцюжку | | прапорець `protocol_downgrade` (security + SEO) |
| Редірект на головну | `…/blog/post → …/` | прапорець `redirect_to_homepage` |
| Різниця тільки в слеші/регістрі/`www` | | прапорці `trailing_slash_only`, `case_only`, `www_change` — типова причина зайвого хопа |
| Query відкинуто | `?utm_source=…` зникло | прапорець `query_dropped` |
| Кросдоменний хоп | | прапорець `host_change` (+ фінальний хост показуємо явно) |
| Вхідний URL без схеми | `old-site.com/page` | нормалізуємо в `https://`, **показуємо** цю нормалізацію оператору |
| `mailto:`/`tel:`/`javascript:`/`data:` | | `verdict = unsupported_scheme`, рядок лишається у звіті |

### 3. Що ми обіцяємо і що ні

- **Обіцяємо:** те, що віддав сервер на наш GET, з нашим browser-UA, без кешу.
- **Не обіцяємо:** що клієнтський CDN віддасть те саме комусь іншому (geo/UA-залежні редіректи), що JS-редірект буде видно, що бот-блок — це «сайт лежить». Усе це — окремі стани й notes, і UI пише про це прямо, без обіцянок, яких не можемо дотримати (той самий принцип, що cache-buster-дисклеймер у task-27).
- **Метод — GET, не HEAD.** HEAD швидший, але частина серверів на HEAD відповідає інакше (405, або 200 замість 301) — тоді інструмент брехав би. Тіло читаємо **тільки** для фінального хопа (і лише щоб знайти `meta refresh` / `canonical` / `title`), проміжні тіла не читаємо взагалі.
- **Без кешу:** ті самі `Cache-Control: no-cache`/`Pragma`, що в `alt_text/verify.py`. Cache-buster-параметр тут, на відміну від alt-verify, **опційний і за замовчуванням вимкнений**: він змінює URL, а деякі сайти редіректять залежно від query — тобто зіпсував би саму річ, яку міряємо. Оператор може ввімкнути перемикачем.

---

## Архітектура (рішення)

**Реюз шаблону task-27: не провайдер і не job.** `Provider` enum не розширюється, `app/jobs/**` і `app/metrics/**` не чіпаються. Один трейс — це один-кілька HTTP-запитів на кілька секунд, а не колекція метрик за період.

**Але список на 200 URL в один запит не влізе.** 200 × ~2 c = хвилини — це не HTTP-ендпоінт. Рішення — **frontend-driven прогін** (той самий патерн, що per-row «Verify alt» у task-27, лише керований циклом):

1. `POST /checks` створює перевірку і **рядки-цілі** у стані `queued` (нормалізація й дедуплікація вхідних URL — на сервері, синхронно, мілісекунди).
2. Фронт послідовно (concurrency ≤ 3, і **1** на той самий хост) викликає `POST /checks/{id}/targets/{tid}/run`; кожен виклик трасує один URL і **комітить результат**.
3. Прогрес живе в БД → перезавантаження сторінки не втрачає нічого, можна закрити вкладку й повернутися; є «Stop» (кооперативна зупинка циклу на фронті) і «Run remaining».

Чому не durable-worker: він дав би фонове виконання без відкритої вкладки, але коштував би нової доріжки в `runner.py`, lease/recovery, SSE-подій і розширення `Provider`/`SyncJob` — тобто зміни в закритих зонах task-05 заради інструмента, яким користуються синхронно, дивлячись у таблицю. Фонові прогони — явний follow-up, якщо реально знадобиться список на тисячі URL.

**Politeness-гард на сервері.** Незалежно від того, що робить фронт, трейсер тримає **мінімальний інтервал між запитами до одного хоста** (in-process, 250 мс) і глобальний семафор. Інакше вставлений список на 200 URL одного клієнта = міні-нагрузочний тест по його сайту. Це рішення на бекенді, бо на фронт у таких речах спиратися не можна.

**Одна нова таблиця + JSON-ланцюжок.** `redirect_checks` (прогін) + `redirect_check_targets` (рядок = вхідний URL). Хопи **не** окрема таблиця: ланцюжок завжди читається цілком, ніколи не фільтрується й не агрегується по хопах, а `chain_json` дає атомарний запис «як воно було» одним UPDATE-ом. Дані мутабельні за задумом (повторний прогін рядка перезаписує ланцюжок) — тобто immutable `AnalyticsDatasetRow` тут не підходить із тієї ж причини, що в task-27.

**Спільний HTTP-safety шар.** SSRF-гард (`assert_public_http_url`), browser-UA, cap тіла й no-cache-заголовки вже написані в [app/tools/alt_text/verify.py](../../../app/tools/alt_text/verify.py) і потрібні тут один-в-один. Дублювати гард безпеки — гарантія розсинхрону (виправлять в одному місці, забудуть у другому). Тому: **витягнути в `app/tools/http_safety.py`** (наш пакет `app/tools/**`), а `alt_text/verify.py` імпортує звідти — точковий рефакторинг без зміни поведінки й публічних імен (`verify.py` реекспортує `assert_public_http_url`/`UnsafeUrlError`, щоб наявні тести task-27 лишились зеленими). Задокументувати в Completion report як дотик чужої зони.

**Без нових залежностей.** Мережа — stdlib `urllib.request`/`http.client`, HTML (meta refresh, canonical, title) — stdlib `html.parser`, XLSX-експорт — наявний `openpyxl`, CSV — stdlib `csv`. `pyproject.toml` не змінюється.

---

## Модель даних (2 нові таблиці + 1 міграція)

Міграція `revises = f69c3d5a8b17` (поточний head — alt-text tool). Стиль — як [f69c3d5a8b17](../../../migrations/versions/f69c3d5a8b17_alt_text_tool.py): явні `sa.Column`, `app.db.base.GUID/UTCDateTime`, іменовані констрейнти через `op.f()`. Статуси/вердикти — **`String`, не `sa.Enum`** (UI-стани інструмента, не спільний контракт провайдерів); валідація — на рівні Pydantic-DTO.

### `redirect_checks`

| Колонка | Тип | Призначення |
|---|---|---|
| `id` | GUID PK | |
| `client_id` | GUID FK `clients.id` nullable, `ondelete=SET NULL` | перевірка може бути й не для клієнта |
| `title` | String(200) | людська назва (авто: `Redirects · 24 Jul · 18 URLs`, редагована при створенні) |
| `input_count` | Integer | скільки URL прислали |
| `target_count` | Integer | скільки лишилось після нормалізації/дедуплікації (різниця видима в UI) |
| `options_json` | JSON | `{max_hops, follow_meta_refresh, cache_buster, timeout_seconds}` — з чим прогін зроблено |
| `created_at` / `updated_at` | UTCDateTime | |

### `redirect_check_targets`

| Колонка | Тип | Призначення |
|---|---|---|
| `id` | GUID PK | |
| `check_id` | GUID FK `redirect_checks.id` `CASCADE`, index | |
| `row_index` | Integer | порядок як у вставленому списку |
| `input_url` | String(2048) | **як вставили** (traceability) |
| `normalized_url` | String(2048) | що реально запитували |
| `run_status` | String(12) | `queued` \| `running` \| `done` \| `failed` |
| `verdict` | String(24) nullable | див. нижче |
| `final_url` | String(2048) nullable | |
| `final_status_code` | Integer nullable | |
| `hop_count` | Integer nullable | кількість **редіректів** (0 = прямий 200) |
| `chain_json` | JSON nullable | список хопів (форма нижче) |
| `flags_json` | JSON nullable | прапорці-сигнали |
| `final_page_json` | JSON nullable | `{title, canonical, meta_robots, possible_js_redirect}` — лише коли фінал = 200 HTML |
| `error_code` / `error_detail` | String(32) / Text nullable | причина `failed`, без секретів і без токенів з URL |
| `elapsed_ms` | Integer nullable | сумарний час трейсу |
| `checked_at` | UTCDateTime nullable | **`None` = не перевіряли** (не «немає редіректів») |

Констрейнти: `UniqueConstraint(check_id, row_index)`, `Index(check_id, run_status)`, `Index(check_id, verdict)`.

### Форма хопа в `chain_json`

```json
{
  "index": 1,
  "url": "http://old.com/page",
  "method": "GET",
  "status": 301,
  "status_text": "Moved Permanently",
  "kind": "http",                    // http | meta_refresh
  "permanence": "permanent",         // permanent | temporary | null (фінал)
  "location": "https://old.com/page",
  "changes": ["scheme_http_to_https"],
  "elapsed_ms": 184,
  "notes": []
}
```

Останній елемент — **фінальна відповідь** (`status` не-3xx, `location = null`, `permanence = null`), щоб фронт малював ланцюжок одним списком без спецкейсів.

### Вердикти (окремі стани, не «ок/не ок»)

`ok_direct` (200 без редіректів) · `ok_redirect` (рівно один хоп → 200) · `chain` (2+ хопи → 200; технічно працює, але SEO-борг) · `loop` · `too_many_hops` · `broken` (фінал 4xx/5xx) · `broken_chain` (3xx без `Location`) · `blocked` (403/429/челендж) · `unsupported_scheme` · `unsafe_url` (SSRF-гард) · `fetch_failed` (DNS/TLS/таймаут, з причиною).

### Прапорці (`flags_json`) — сигнали, не вироки

`temporary_redirect` · `protocol_downgrade` · `host_change` · `www_change` · `trailing_slash_only` · `case_only` · `query_dropped` · `redirect_to_homepage` · `meta_refresh_used` · `mixed_permanence` (у ланцюжку і 301, і 302) · `canonical_mismatch` · `noindex_destination` · `possible_js_redirect` · `normalized_input` (ми додали схему/прибрали пробіли).

---

## API (префікс `/api/v1/tools/redirects`, конвенції PLAN §10)

**Каталог хабу** — `app/api/tools.py`: **один новий запис** `_ToolEntry(key="redirects", …)` + функція статистики (скільки перевірок, коли остання). Це і є доказ, що каталог task-27 спроєктований правильно.

**Інструмент:**

- `POST /trace` — `{url, options?}` → `RedirectTraceDTO` **без запису в БД**. Це прямий шлях під головну вимогу «вставив посилання — бачу ланцюжок»: нуль кліків, нуль сутностей. У відповіді — кнопка «Save as check» (тоді йде `POST /checks`). Rate-обмеження: той самий per-host politeness-гард.
- `POST /checks` — `{title?, client_id?, urls: [str], options?}` → `RedirectCheckDTO` + цілі у `queued`. `422 no_valid_urls`, якщо після нормалізації не лишилось нічого; `413 too_many_urls` (cap 200).
- `POST /checks/{id}/targets/{tid}/run` → трасує одну ціль, зберігає, віддає `RedirectTargetDTO`. Повторний виклик — легальний (перезапуск рядка). `409 target_running`, якщо той самий рядок уже виконується (захист від подвійного кліку/подвійної делегації — той самий дефект, що ловили в task-27).
- `GET /checks` → `[RedirectCheckSummaryDTO]` (+ лічильники по вердиктах і по `run_status`).
- `GET /checks/{id}` → перевірка + лічильники + опції.
- `GET /checks/{id}/targets?q=&verdict=&flag=&run_status=&limit=50&offset=0` → `{total, limit, offset, rows[]}`.
- `DELETE /checks/{id}` → `204`, каскад.
- `GET /checks/{id}/export.csv` / `export.xlsx` — по рядку на URL: вхідний URL, фінальний URL, статус, кількість хопів, вердикт, прапорці, **ланцюжок одним текстовим полем** (`301 → 301 → 200`) + окремий аркуш/секція «hop per row» для XLSX. CSV — з екрануванням formula-injection; `Content-Disposition` — через ASCII+`filename*` (RFC 5987), **не** повторюючи баг, який task-27 знайшов у `blogs.py`/`tech_audit.py`.

DTO — **додаються в наявний `app/schemas/tools.py`** (той самий файл інструментів, суто адитивно; жоден наявний DTO не змінюється). `openapi_snapshot.json` перегенерувати.

---

## Frontend (AGENTS.md обов'язковий)

**Маршрути** — нічого нового в `router.js`: `TOOLS_ROUTE` уже парсить `#/tools/<toolKey>/<id>`, тож `#/tools/redirects` і `#/tools/redirects/{checkId}` працюють одразу. Реєстрація — **один рядок** у `TOOL_MODULES` у [frontend/js/tools-page.js](../../../frontend/js/tools-page.js#L15) (`"redirects": () => import("./redirect-tool.js?v=…")`). Фільтри/пагінація/пошук — у query-частині хешу (AGENTS.md §12), як в alt-text.

**Сторінка `#/tools/redirects`:**

- панель **Check links**: `<textarea>` з видимим лейблом («One URL per line»), лічильник розпізнаних URL під ним (`aria-live`), опції в `<details>` (max hops, follow meta refresh, cache-buster) з дефолтами, опційний клієнт і назва.
- **Один URL у полі** → кнопка `Trace` (миттєвий `POST /trace`, результат розкривається під панеллю, без створення перевірки). **Кілька** → кнопка `Check N links` (створює перевірку й веде в таблицю). Перемикання підписи кнопки — за фактичним вмістом поля, без модальних режимів.
- нижче — **історія перевірок**: назва, клієнт, дата, `18 links · 4 chains · 1 broken` (справжні числа), дії `Open` / `Export` / `Delete` (з підтвердженням).

**Сторінка перевірки `#/tools/redirects/{checkId}`:**

Таблиця, рядок = вхідний URL:

| Колонка | Вміст |
|---|---|
| `#` | номер у вставленому списку |
| Link | усічений моно-URL + `Open ↗` + `Copy` |
| Chain | **компактна стрічка хопів**: `301 → 301 → 200` (бейджі з іконкою+текстом, не колір-онлі) + `N hops`; клік/Enter розкриває рядок |
| Destination | фінальний URL + `Open ↗` + `Copy`; `None` (не перевіряли) — чесний прочерк, не «немає редіректів» |
| Verdict | бейдж (іконка+текст) + прапорці-чіпи (перші 2 + «+N») |
| | `Re-run` |

**Розкритий рядок — головний екран інструмента:** ланцюжок **вертикально, підряд**, по кроку на хоп:

```
1   http://old.com/page                    301 Moved Permanently   184 ms
    ↓  http → https
2   https://old.com/page                   301 Moved Permanently    96 ms
    ↓  host: old.com → new.com
3   https://new.com/page/                  200 OK                  212 ms   final
    title “Page — New”   canonical = final URL   ✓
```

Кожен хоп: номер, повний URL (`Copy` на кожному), бейдж статусу, час, і **що змінилося** словами. Плюс блок «What this means» — прапорці людською мовою («Two redirects before the destination. Point the first one straight at the final URL to stop losing crawl budget and link equity.»). Дії: `Copy chain as text`, `Copy final URL`, `Re-run`.

**Прогін:** після створення перевірки фронт сам крутить цикл по `queued`-цілях (concurrency 3, по 1 на хост), рядки заповнюються по одному — видимий прогрес `12 / 148 checked` (справжні числа, не анімовані відсотки), кнопки `Stop` / `Run remaining` / `Re-run failed`. Стан кожного рядка — з БД, тож F5 нічого не губить.

**Обов'язкові стани (AGENTS.md §6):** initial loading (скелет у геометрії таблиці), порожня історія, перевірка з усіма `queued`, 0 результатів фільтра, дуже довгі URL і не-латиниця, ланцюжок на 25 хопів, `blocked`, `fetch_failed`, помилка мережі самого API, повністю «broken» перевірка.

**CSS** — доповнення наявного `frontend/css/tools.css` (токени звідти ж) + урок task-26: `overflow-x: hidden` на корені сторінки, `.table-scroll` для таблиці, нуль горизонтального page-scroll на 390 px.

**Фікстури:** `redirects-checks.json`, `redirects-targets.json`, `redirects-targets-empty.json`, `redirects-trace.json` — для fixture-режиму `api.js` (і `tools-catalog.json` доповнити другим блоком).

Делегація подій — **рівно один раз** при побудові shell (`data-delegated`), як зафіксовано в Completion report task-27: інакше один клік дасть N запитів після N перемальовувань.

---

## Безпека (PLAN §13)

- **SSRF — найважливіше тут:** інструмент за задумом ходить за URL, які прислав оператор, **і за редіректами**. `assert_public_http_url` застосовується до **кожного** хопа (включно з `Location` і `meta refresh`), а не лише до першого URL; лише `http`/`https`; відмова для loopback/private/link-local/metadata-адрес. Редірект у приватний діапазон = `verdict = unsafe_url` з показом, на якому хопі це сталося.
- **DNS rebinding:** гард резолвить хост і перевіряє адреси перед запитом; ризик «резолв змінився між перевіркою і конектом» приймаємо усвідомлено (той самий, що в task-27) і фіксуємо як known limitation, бо власний connect-по-IP + SNI-піннінг на stdlib — окрема робота, не виправдана для внутрішнього інструмента.
- **Caps:** 200 URL на перевірку, 25 хопів на URL, 20 c на весь трейс одного URL, 5 c на конект, 512 КБ тіла для фінального HTML (проміжні тіла не читаємо взагалі), politeness 250 мс на хост.
- **Логи/помилки:** URL можуть містити токени в query — `error_detail` і логи пишуть **хост + причину**, а не повний URL із query.
- **XSS:** усі URL і `title`/`canonical` із чужих сторінок — недовірені: рендер через `escapeHtml`/DOM API; `href` лише для `http(s)`-схем (інші — тільки текстом, без посилання).
- **CSV-експорт:** захист від formula injection (`=`/`+`/`-`/`@` → префікс `'`).

---

## Обсяг

**Входить:**

1. `app/tools/http_safety.py` — витягнутий спільний шар (SSRF-гард, UA, no-cache-заголовки, caps); `alt_text/verify.py` переведений на нього без зміни поведінки.
2. `app/tools/redirects/trace.py` — **чистий трейсер** (ручний обхід 3xx, `meta refresh`, петлі, ліміти, `changes`/`flags`, вердикт) з **інжектованим fetcher-ом** для тестів; `app/tools/redirects/input.py` — нормалізація/дедуплікація вставленого списку.
3. Моделі `RedirectCheck`/`RedirectCheckTarget` + міграція від `f69c3d5a8b17` + репозиторій `app/db/repositories/redirects.py`.
4. DTO у `app/schemas/tools.py` (адитивно); роутер `app/api/tools_redirects.py`; **один запис** у реєстрі `app/api/tools.py`.
5. Frontend: `frontend/js/redirect-tool.js`, доповнення `tools.css`, фікстури, адитивні дотики `tools-page.js` (реєстр) і `api.js` (функції).
6. Тести (нижче) + перегенерований `openapi_snapshot.json`.
7. `graphify update .`.

**Не входить (свідомо):**

- **Playwright/Chromium-режим** для JS-редіректів і бот-блоків (референс — `seo_audit/gsc/tools/redirect_check.py`); зараз — чесний `possible_js_redirect` / `blocked`. Той самий образ-апгрейд, що вже висить follow-up-ом у task-25/27.
- фонові прогони без відкритої вкладки (durable-worker, SSE);
- імпорт URL із файлу (XLSX/CSV) — вставка списку покриває реальний кейс; якщо знадобиться, парсер із `alt_text/tables.py` реюзається окремою таскою;
- автоматичні перевірки за розкладом і алерти на зміну ланцюжка;
- перевірка redirect-правил у `.htaccess`/nginx і будь-яка **правка** редіректів на сайті клієнта (інструмент лишається read-only);
- порівняння «до/після» двох прогонів (diff) — follow-up.

---

## Owned paths

**Нові (повністю наші):**

```
app/tools/http_safety.py
app/tools/redirects/**                        (новий пакет: trace.py, input.py, __init__.py)
app/api/tools_redirects.py
app/db/repositories/redirects.py
migrations/versions/<new>_redirect_checker.py (зона task-03 — одна нова міграція, адитивно)
frontend/js/redirect-tool.js
frontend/fixtures/redirects-*.json
tests/unit/tools/test_redirect_trace.py, tests/unit/tools/test_redirect_input.py
tests/unit/db/test_redirects_repository.py
tests/unit/api/test_tools_redirects.py
tests/e2e/frontend/tools-redirects.cjs
```

**Адитивні дотики чужих файлів (кожен — задокументувати в Completion report):**

```
app/api/tools.py                 + запис у реєстрі + stat            (task-27)
app/schemas/tools.py             + DTO інструмента                   (task-27/02)
app/tools/alt_text/verify.py     імпорт із http_safety + реекспорт    (task-27, поведінка без змін)
app/db/models.py                 + 2 моделі                          (task-03)
frontend/js/tools-page.js        + 1 рядок у TOOL_MODULES             (task-27)
frontend/js/api.js               + функції інструмента                (task-12)
frontend/css/tools.css           + секція .rd-*                      (task-27)
frontend/fixtures/tools-catalog.json + другий блок                    (task-27)
tests/unit/api/data/openapi_snapshot.json  (перегенерація)            (task-06/13)
doc/dev/new/INDEX.md             + рядок таски                        (процес)
```

**Не торкаємось:** `app/schemas/enums.py` (жодного нового `Provider`), `app/jobs/**`, `app/metrics/**`, `app/api/tools_alt_text.py`, `pyproject.toml`, `Dockerfile`, `compose.yaml`, `frontend/js/router.js` (маршрут уже загальний), `frontend/index.html` (nav уже має `Tools`).

---

## План виконання

1. **Трейсер перший.** `app/tools/redirects/trace.py` + табличні тести на всі форми хопів із таблиці дослідження (фейковий fetcher: словник `url → (status, headers, body)`). Це шар, від правильності якого залежить сенс інструмента.
2. **http_safety.** Витягнути гард, перевести `alt_text/verify.py`, прогнати **наявні** тести task-27 — вони мусять лишитись зеленими без правок (це і є критерій «рефакторинг без зміни поведінки»).
3. **Нормалізація входу.** `input.py`: розбір textarea (переноси, коми, пробіли, лапки, markdown-посилання), додавання схеми, дедуплікація зі збереженням порядку, cap 200, прапорець `normalized_input`.
4. **БД:** моделі → міграція (`upgrade` і `downgrade` перевірити в контейнері) → репозиторій (create check + bulk targets, пагінований/фільтрований read, `claim_target`/`save_result`, лічильники).
5. **API:** DTO → `POST /trace` → `POST /checks` → `run` → читання/експорт/delete. Кожен мутуючий ендпоінт комітить сам; блокуючий трейс — через `run_in_threadpool`. Перегенерувати снапшот.
6. **Frontend:** реєстр-рядок → панель вставки + `Trace` → таблиця з розкриттям ланцюжка → цикл прогону з `Stop`. Спершу на фікстурах, далі проти живого API. Візуальний loop AGENTS.md §15 на 390/768/1024/1440 + 200 % zoom, скріншоти до/після.
7. **e2e:** вставити 3 URL (локальний мок-сервер із 301→301→200, петлею і 404) → прогін → перевірити ланцюжок, вердикти, фільтр в URL, `Re-run`, експорт, відсутність горизонтального скролу на 390 px.
8. **Гейт:** `ruff check .`, `mypy app`, `pytest` — усе **в Docker**. Потім `graphify update .`, `git mv` плану в `done/`, Completion report, оновлення INDEX.

---

## Ризики й рішення

| Ризик | Рішення |
|---|---|
| Ручний обхід 3xx — легко зробити наївно (відносний `Location`, петля, `Location` без схеми) | Трейсер — чиста функція з інжектованим fetcher-ом і **табличними** тестами на кожну форму з таблиці дослідження; жодного сценарію «перевіримо руками на реальному сайті». |
| Сайт клієнта отримає 200 запитів залпом | Politeness-гард **на сервері** (250 мс/хост, глобальний семафор) + concurrency 3 на фронті + cap 200 URL. Гард на бекенді, бо на клієнта в таких речах спиратися не можна. |
| Бот-захист віддасть 403 і оператор вирішить, що сторінка мертва | Окремий вердикт `blocked` із підказкою «сайт блокує не-браузерні запити; перевірте вручну» — ніколи не `broken`. |
| JS-редіректи не видно stdlib-ом | Не вдаємо, що бачимо: note `possible_js_redirect`, коли в HTML є присвоєння `location`, і чесний текст в UI. Playwright — окремий follow-up. |
| Ланцюжок залежить від UA/гео/cookies, і наш результат не збігається з браузером оператора | В UI явно: «Checked with a desktop browser UA from this server, no cookies. Geo- or UA-based redirects can differ.» Плюс `checked_at` на кожному рядку. |
| SSRF через `Location` у приватну мережу | Гард на **кожному** хопі; `verdict = unsafe_url` із номером хопа. Покрито тестами (loopback, `10.x`, `169.254.169.254`, `file:`). |
| Довгі URL і не-латиниця розвалять таблицю | Моно-текст із `min-width: 0`, усічення + `title`, `Copy` на кожному хопі; перевірка на реальних довгих URL у візуальному loop. |
| Перевірка на 5 000 URL | Cap 200 на прогін + чесне повідомлення «Split larger lists»; фонові прогони — follow-up, а не тихе підвисання вкладки. |
| Подвійний клік / повторна делегація дасть N запитів | Урок task-27: делегація один раз при побудові shell; `409 target_running` на сервері як другий рубіж. |

---

## Тести

- `tests/unit/tools/test_redirect_trace.py` — фейковий fetcher: прямий 200; 301→200; 301→301→200; 302/307/303; відносний `Location`; `Location` без схеми; 3xx без `Location`; петля A→B→A; 26 хопів; `meta refresh` (у т.ч. вимкнений опцією); фінал 404/410/500; 403/429 → `blocked`; таймаут/DNS/TLS → `fetch_failed`; `https→http` downgrade; `www`/слеш/регістр/query-прапорці; `canonical_mismatch`; `noindex_destination`; `possible_js_redirect`; **ідемпотентність** (повторний трейс тих самих даних дає той самий результат).
- `tests/unit/tools/test_redirect_input.py` — переноси/коми/лапки/markdown, дедуплікація з порядком, додавання схеми, `mailto:`/`javascript:`, cap 200, порожній вхід.
- `tests/unit/tools/test_http_safety.py` — SSRF-гард (loopback, private, link-local, metadata, multicast, нерезолвний хост, не-http схема) + що `alt_text/verify.py` реекспортує ті самі імена.
- `tests/unit/db/test_redirects_repository.py` — create + bulk targets, унікальність `(check_id, row_index)`, `claim_target` (двічі підряд не дає двох `running`), `save_result`, фільтри/пагінація, лічильники, каскадне видалення.
- `tests/unit/api/test_tools_redirects.py` — на harness (`temp_db`, `api_client`, `seed_client`): `POST /trace` без БД; повний флоу check→run→read; `409 target_running`; `413 too_many_urls`; `422 no_valid_urls`; `404` на чужий `check_id`/`target_id`; експорт CSV/XLSX (formula-injection + не-ASCII назва); каталог `GET /tools` містить обидва інструменти.
- `tests/unit/api/test_openapi_snapshot.py` — перегенерований снапшот зелений.
- `tests/e2e/frontend/tools-redirects.cjs` + скріншоти 390/1440 (за патерном `tools-alt-text.cjs`), з локальним мок-сервером ланцюжків.

---

## Definition of Done / Gate

- [ ] `#/tools` показує **два** блоки; блок Redirect Checker має реальну статистику (кількість перевірок / «Not used yet», ніколи `0 reports`).
- [ ] Вставлене **одне** посилання одразу показує ланцюжок (`POST /trace`, без створення сутностей); вставлений **список** створює перевірку й прогін.
- [ ] Ланцюжок показується **підряд, хоп за хопом**: номер, URL, статус (іконка+текст), час, що змінилося на кроці, фінальний хоп позначений; `Copy` на кожному URL і `Copy chain as text`.
- [ ] Усі 11 вердиктів реалізовані як **окремі** стани; `blocked` ніколи не показується як `broken`; `checked_at = None` ніколи не читається як «редіректів немає».
- [ ] Прапорці (`temporary_redirect`, `protocol_downgrade`, `chain`, `canonical_mismatch`, …) показані людською мовою в розкритому рядку.
- [ ] Прогін списку: справжні числа `N / M checked`, `Stop`, `Run remaining`, `Re-run failed`; **F5 не губить прогрес** (усе в БД).
- [ ] SSRF-гард застосовано до кожного хопа (тести на loopback/private/metadata/`file:` і на редірект у приватний діапазон); caps на хопи/час/тіло/кількість URL — з тестами.
- [ ] Politeness: не більше одного одночасного запиту на хост і мінімальний інтервал — на **сервері**, з тестом.
- [ ] Експорт CSV/XLSX працює, formula-injection екранований, не-ASCII назва не валить `Content-Disposition`.
- [ ] `alt_text/verify.py` після витягання `http_safety` працює як раніше — **усі тести task-27 зелені без правок**.
- [ ] Обов'язкові стани AGENTS.md §6 реалізовані; чекліст §16 пройдено; скріншоти 390/768/1024/1440 + 200 % zoom перевірені очима; на 390 px нема горизонтального page-scroll.
- [ ] Клавіатура-онлі проходить весь флоу (розкриття рядка теж), фокус повертається на opener, статуси не колір-онлі.
- [ ] `ruff check .`, `mypy app`, `pytest` — зелені **в Docker**; `alembic upgrade head` і `downgrade -1` проходять.
- [ ] `Provider` enum, `app/jobs/**`, `app/metrics/**`, `pyproject.toml`, `router.js`, `index.html` не змінені.
- [ ] `graphify update .` виконано; план перенесено у `doc/dev/done/` з Completion report; INDEX оновлено.

---

## Follow-ups (свідомо відкладене)

- `TODO` — Playwright-режим трейсу (JS-редіректи, бот-блоки, реальний network-ланцюжок) — той самий образ-апгрейд, що вже потрібен task-25/27; референс — `seo_audit/gsc/tools/redirect_check.py`.
- `TODO` — імпорт URL із XLSX/CSV (реюз `alt_text/tables.py`) для великих аудит-експортів.
- `TODO` — фонові прогони без відкритої вкладки (durable-worker + SSE), якщо реально знадобляться списки на тисячі URL.
- `TODO` — diff двох прогонів («ланцюжок змінився з минулого тижня») + алерт на регресію.
- `TODO` — перевірка ланцюжка з мобільним UA / з іншої гео (виявляти UA-cloaking).
- `TODO` — «pointing suggestions»: для `chain` показувати готове правило `301 first → final`, готове до вставки в `.htaccess`/nginx.

---

## ✅ Completion report

- **Дата:** 2026-07-27
- **Гілка / комміт:** `main` (робоче дерево сесії) @ див. комміт цієї таски
- **Статус:** done-with-followups

### Що зроблено

**Другий інструмент у хабі `#/tools` — Redirect Checker.** Оператор вставляє посилання й бачить **повний ланцюжок редіректів підряд, хоп за хопом**:

1. **Одне посилання** → `POST /tools/redirects/trace`: ланцюжок показується одразу, у БД нічого не створюється. Поруч — «Save as check», якщо результат потрібен в історії.
2. **Список** → перевірка з історією: сервер нормалізує й дедуплікує вставлений текст, рядки стають `queued`, сторінка проганяє їх по одному (concurrency 3) з `Stop` / `Check remaining` / `Re-check problems`. Прогрес — у БД, тож F5 і закрита вкладка нічого не гублять.
3. **Розкритий рядок** — головний екран: вертикальний список кроків (номер → URL → статус + `Moved Permanently` → час → **що змінилося**: `http → https`, `www added`, `slash added`, `query dropped`), позначений фінальний хоп, факти фінальної сторінки (`title`, `canonical`, `meta robots`) і блок **«What this means»** — прапорці людською мовою з підказкою, що робити.
4. **11 окремих вердиктів** замість «ок/не ок»: `ok_direct` · `ok_redirect` · `chain` · `loop` · `too_many_hops` · `broken` · `broken_chain` · `blocked` · `unsupported_scheme` · `unsafe_url` · `fetch_failed`. Бот-блок ніколи не показується як мертва сторінка; «не перевіряли» ніколи не показується як «редіректів немає» чи `0`.
5. **Експорт CSV/XLSX** — рядок на посилання (+ ланцюжок текстом `301 → 302 → 200`) і другий аркуш `Hops` — рядок на хоп.

### Як зроблено (ключові рішення)

- **Ланцюжок трасується вручну.** `urlopen` сам іде за 3xx і віддає лише фінальну відповідь — тобто ховає саме те, що інструмент показує. Тому opener без `HTTPRedirectHandler` + passthrough `HTTPErrorProcessor` (3xx приходить звичайною відповіддю, а не винятком), `Location` розвʼязується `urljoin` (він законно буває відносним — RFC 7231).
- **GET, не HEAD:** частина серверів на HEAD відповідає інакше (405 або 200 там, де GET дає 301) — трейс брехав би. Тіло читається **лише** для не-3xx відповіді й лише заради `<head>` (cap 512 КБ).
- **Не провайдер і не job** (як task-27): `Provider` enum, `app/jobs/**`, `app/metrics/**`, `pyproject.toml` не змінені; блокуючі виклики — через `run_in_threadpool`.
- **Список проганяє фронт**, а не durable-worker: 200 URL — це хвилини реальних запитів, тобто не один HTTP-запит, а власна доріжка в `runner.py` заради синхронного інструмента не виправдана. `claim_target` — **атомарний UPDATE** (`run_status != 'running'`), тож подвійний клік або дубльована делегація не дають двох запитів до чужого сайту (`409 target_running`).
- **Politeness-гард на сервері** (`HostThrottle`): один одночасний запит на хост + 250 мс між запитами. На клієнтський JS у такому покладатися не можна.
- **Спільний HTTP-safety шар витягнуто** з `app/tools/alt_text/verify.py` у `app/tools/http_safety.py` (SSRF-гард, browser-UA, no-cache-заголовки, cache-buster). `verify.py` реекспортує ті самі імена — усі 126 тестів task-27 лишились зеленими без правок. Дублювати гард безпеки означало б розсинхрон при першому ж виправленні.
- **SSRF — на кожному хопі**, включно з `Location` і `meta refresh`; редірект у приватний діапазон дає `unsafe_url` із номером хопа, а не тихий запит у внутрішню мережу.
- **Cache-buster вимкнений за замовчуванням** (на відміну від alt-verify): він змінює URL, а частина сайтів редіректить залежно від query — тобто зіпсував би саме те, що ми міряємо.
- **Без авто-старту прогону** при відкритті сторінки: кожен рядок — реальний запит до чужого сайту, тож він починається з кліку оператора, а не з F5.
- **Нуль нових залежностей:** мережа — stdlib `urllib.request`, HTML — stdlib `html.parser`, XLSX — наявний `openpyxl`, CSV — stdlib `csv`.
- **Головні файли:**
  - `app/tools/redirects/trace.py` — трейсер (серце інструмента): хопи, петлі, `meta refresh`, `changes`, прапорці, вердикти.
  - `app/tools/redirects/input.py` — нормалізація вставленого тексту (markdown, bullet-и, коми, IDN, percent-encoding), дедуплікація, cap 200.
  - `app/tools/http_safety.py` — SSRF-гард + politeness-throttle, спільні для інструментів.
  - `app/db/repositories/redirects.py`, `app/api/tools_redirects.py`, `app/schemas/tools.py` (доповнено), `migrations/versions/a3f81c62d5b4_redirect_checker.py`.
  - `frontend/js/redirect-tool.js`, `frontend/css/tools.css` (секція `.rd-*`).

### Знайдено й виправлено під час прогонів

1. **Авто-старт прогону при відкритті** сторінки означав, що F5 обстрілює клієнтський сайт без жодного кліку — прибрано, лишився явний `Check remaining`.
2. **`typing.cast` конфліктував із `sqlalchemy.cast`** в одному модулі (repository) — звужено через `typing.cast` із повним іменем.
3. **`«…».` давало `»` у кінці URL:** один прохід очищення обгорток/пунктуації не покривав вкладений випадок — тепер чистимо по колу.
4. **«not a url» рахувалося як три відкинуті кандидати** (розбиття по пробілах): рядок без жодного URL-подібного токена тепер один кандидат, тож лічильник «прислали N» не бреше.
5. **Копія URL хопа роздувалася до 44 px** (`.icon-button` з app.css) і перебивала сам URL — обмежено до 28 px у списку хопів.
6. **На 390 px вердикт — головна інформація — був за горизонтальним скролом:** приховані колонки `#` і `Destination` (обидві дублюються в розкритому ланцюжку), дії лишились в один рядок.

### Контрактні зміни (Contract changes)

- `app/schemas/tools.py` — **адитивно**: 10 нових DTO (`RedirectTraceDTO`, `RedirectTargetDTO`, `RedirectCheckDTO`, `RedirectHopDTO`, …). Жоден наявний DTO не змінено.
- **Нові таблиці** `redirect_checks` / `redirect_check_targets` + міграція `a3f81c62d5b4` (від `f69c3d5a8b17`; `upgrade` і `downgrade -1` перевірені).
- `app/db/models.py` — 2 нові моделі; `app/api/tools.py` — один запис у реєстрі + `_STATS`-мапа (статистику дає сам інструмент).
- `app/tools/alt_text/verify.py` — імпорт із `http_safety` + реекспорт; **поведінка й публічні імена не змінені**.
- `frontend/js/{api,tools-page}.js`, `frontend/fixtures/tools-catalog.json` — адитивні дотики; `frontend/js/router.js` і `index.html` **не** чіпалися (маршрут `#/tools/<tool>/<id>` уже загальний).
- `tests/e2e/frontend/tools-alt-text.cjs` — очікування «у каталозі 1 картка» → «усі зареєстровані інструменти» (хаб за задумом росте).
- `Provider` enum, `METRIC_PROVIDERS`, `app/jobs/**`, `app/metrics/**`, `pyproject.toml` — **не змінені**. `tests/unit/api/data/openapi_snapshot.json` перегенеровано (+7 шляхів, 0 видалених).

### Як перевірити (verification)

```bash
docker compose up -d --build
docker compose exec api poetry run pytest tests/unit tests/integration tests/contract -q   # 997 passed
docker compose exec api poetry run ruff check .          # All checks passed
docker compose exec api poetry run mypy app              # Success: no issues
docker compose exec api poetry run alembic upgrade head  # і `downgrade -1` — обидва чисті

# e2e (потрібен playwright-core + chromium)
NODE_PATH=/tmp/yovth-e2e/node_modules BASE_URL=http://127.0.0.1:6524 \
  node tests/e2e/frontend/tools-redirects.cjs
NODE_PATH=/tmp/yovth-e2e/node_modules BASE_URL=http://127.0.0.1:6524 \
  node tests/e2e/frontend/tools-alt-text.cjs      # регресія task-27

# живий трейс без UI
curl -s -X POST http://127.0.0.1:6524/api/v1/tools/redirects/trace \
  -H 'Content-Type: application/json' -d '{"url":"http://github.com/anthropics"}'
```

В UI: `#/tools` → блок «Redirect Checker» → вставити одне посилання → `Trace link` (ланцюжок зʼявляється під формою) → вставити кілька → `Check N links` → у таблиці `Check remaining` → розкрити рядок `Details` (хопи підряд + «What this means») → `Export CSV`.

**Живий прогін (виконано):** `http://github.com/anthropics` → `301 → 200` (`ok_redirect`, canonical збігається); `http://bit.ly/3xyzabc` → `301 → 404` (`broken`, `host_change`); `theyovth.com` → нормалізовано в https, `ok_direct`; `http://127.0.0.1/admin` і `http://169.254.169.254/…` → `unsafe_url` із причиною; `mailto:` — відкинуто ще до мережі; експорт CSV/XLSX із кириличною назвою прогону віддає `filename*=UTF-8''` без 500.

### Тести

- `tests/unit/tools/test_redirect_trace.py` — 79 кейсів на фейковому fetcher-і: усі форми ланцюжка (301/302/303/307/308, відносний `Location`, `Location` без схеми, кілька `Location`, 3xx без `Location`, петля й self-loop, ліміт хопів, `meta refresh` + його вимкнення + петля через нього, 401/403/407/429 → `blocked`, 4xx/5xx → `broken`, DNS/таймаут → `fetch_failed`, редірект у `mailto:`, SSRF-відмова на першому URL і на хопі), прапорці, `canonical_mismatch`, `noindex`, JS-підказка, cache-buster, `describe_change`, `same_page`, детермінованість.
- `tests/unit/tools/test_redirect_input.py` — 34 кейси: markdown, bullet-и, коми, обгортки й пунктуація, IDN, percent-encoding без подвійного кодування, фрагмент, дедуплікація після нормалізації, cap, відкинуті рядки з кодами, ідемпотентність.
- `tests/unit/tools/test_http_safety.py` — реекспорт імен task-27 (щоб витягання не зламало контракт) + politeness-гард (серіалізація на хост, незалежність різних хостів, інтервал, регістр).
- `tests/unit/db/test_redirects_repository.py` — 21 тест: вставка `queued`, унікальність `(check_id, row_index)`, атомарний `claim_target` (двічі підряд → один `running`), повторний прогін, `save_result`/`release_target`, обрізання деталей, лічильники, фільтри (у т.ч. `flag` не матчить схожу назву), пагінація, каскад, чужий `target_id` → `None`.
- `tests/unit/api/test_tools_redirects.py` — 35 тестів наскрізного флоу: каталог із двома інструментами, `POST /trace` без запису, нормалізація/дедуплікація/`rejected`, `422 no_valid_urls`, `413 too_many_urls`, cap 200 + `truncated`, `404 client_not_found`, прогін і `409 target_running` (реентрантним fetcher-ом), 5 вердиктів, `fetch_failed` без токена в деталях, несподіваний виняток → `run_status=failed`, фільтри, каскадне видалення, експорт (ланцюжок текстом, formula-injection, не-ASCII назва, аркуш `Hops`), «неперевірений рядок експортується порожнім, а не нулем».
- `tests/e2e/frontend/tools-redirects.cjs` — хаб → трейс одного посилання → таблиця → стрічка `301 → 302 → 200` → хопи підряд + `aria-expanded` → прогін рядка переживає reload → «один клік = один тост» → фільтри в URL → відсутність горизонтального page-scroll на 390 px. Скріншоти 1440/390 у `tests/e2e/frontend/screenshots/`.

### Follow-ups / борг

- `TODO` — Playwright-режим трейсу (JS-редіректи, бот-блоки, network-ланцюжок як його бачить браузер); зараз — чесні `possible_js_redirect` / `blocked`. Референс — `AISeoTools/seo_audit/gsc/tools/redirect_check.py`.
- `TODO` — фонові прогони без відкритої вкладки (durable-worker + SSE) для списків на тисячі URL.
- `TODO` — імпорт URL із XLSX/CSV (реюз `app/tools/alt_text/tables.py`).
- `TODO` — diff двох прогонів («ланцюжок змінився з минулого тижня») + алерт на регресію.
- `TODO` — «pointing suggestions»: для `chain` показувати готове правило `301 first → final`.
- **Відомий ліміт (задокументований):** DNS rebinding між перевіркою гарда й конектом не закритий (успадковано від task-27) — для цього потрібен власний connect-по-IP із SNI-піннінгом.
- **Прогін діє на видиму сторінку** (50 рядків): для більшого списку оператор перегортає сторінку й тисне `Check remaining` ще раз. Свідомо, щоб одна кнопка не запускала сотні запитів «наосліп».

---

## 🔧 Доробка після живого фідбеку (2026-07-27)

Користувач прогнав інструмент на реальному URL клієнта
(`nysapphire.com/ny-sapphire-vip-packages/bachelor-parties-nyc-sapphire/`) з **увімкненим** cache-buster-ом і побачив
`404 Dead end` без жодного хопа. Дві проблеми, обидві виправлені.

### 1. Cache-buster створював фальшивий 404 (і ховав увесь ланцюжок)

Сайт віддає `404` на **будь-який** невідомий query-параметр, тож `?yvcheck=…` перетворював живу сторінку на «мертву»:
трейс завершувався на першому ж хопі, а справжній ланцюжок (`301 → 301 → 200`) не було видно взагалі.

**Виправлено в трейсері:** якщо cache-buster увімкнений і вердикт потрапив у «підозрілі»
(`broken` / `blocked` / `fetch_failed` / `broken_chain`), трейс **автоматично повторюється без параметра**. Якщо чиста
спроба дала нормальну відповідь — показуємо саме її з прапорцем **`cache_buster_broke_the_url`** («With the
cache-busting parameter the site answered 404/403, so the link was re-checked without it. This result is the clean
one.»). Справжній 404 при цьому лишається `broken` — fallback не «лікує» мертві сторінки, бо друга спроба теж
підозріла. Реалізація: `trace_redirects` розділений на публічну функцію (fallback) і `_walk` (один прохід), прапорець
`cache_buster_used` тепер відображає **фактичний** прохід, а не опцію.

Живий результат після фікса — обидва режими дають однаковий, справжній ланцюжок:

```
1. 301 Moved Permanently  …/ny-sapphire-vip-packages/bachelor-parties-nyc-sapphire/
   → …/bachelor-parties-nyc-sapphire/            [different path]
2. 301 Moved Permanently  …/bachelor-parties-nyc-sapphire/
   → …/adult-packages/bachelor-parties-nyc/      [different path]
3. 200 OK                 …/adult-packages/bachelor-parties-nyc/
```

### 2. «Звідки → куди» не читалося

У списку хопів був URL кроку, статус і перелік змін, але **цільового URL кроку не було**: напрямок доводилось
виводити з URL наступного рядка, а для одного хопа (як у зламаному кейсі вище) не було видно нічого.

**Виправлено в UI:**

- **Рядок-маршрут над кроками:** `<початковий URL> → <фінальний URL> · via N redirects`. Це відповідь на перше
  питання оператора, і вона тепер перша.
- Якщо редіректів немає — явне твердження **«No redirects — this URL answered directly.»** замість порожнього місця.
- Кожен редірект-хоп отримав рядок **`REDIRECTS TO <target>`** (`.rd-hop__to`), тож напрямок кроку видно без
  порівняння з сусіднім рядком.

### 3. Побічно: виправлено flaky-тест task-27

`test_reimporting_the_same_file_and_sheet_points_at_the_existing_report` падав приблизно раз на кілька повних прогонів:
`openpyxl` пише в архів час створення, тож два виклики `_xlsx()` по різні боки межі секунди давали різний `sha256` і
409-конфлікту не виникало. Тест тепер вантажить **ті самі байти** двічі (це і є перевірка ідемпотентності).

### Тести доробки

- `tests/unit/tools/test_redirect_trace.py` — `cache_buster` ламає URL → fallback віддає чистий результат із
  прапорцем; справжній 404 лишається `broken`; без увімкненого buster-а другого запиту немає (перевіряється журналом
  fetcher-а).
- `tests/e2e/frontend/tools-redirects.cjs` — рядок-маршрут містить початок і фінал, `via 2 redirects`, і кожен
  редірект-хоп має підписане `redirects to`.
- Гейт після доробки: **1000 тестів** зелені, `ruff` і `mypy` чисті, обидва e2e-набори зелені.
