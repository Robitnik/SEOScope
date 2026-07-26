# task-27 — Сторінка Tools + перший інструмент «Image Alt Text»

- **Пакет:** нова верхньорівнева сторінка **Tools** (хаб інструментів: гарні блоки + пошук, кожен блок відкриває свій інструмент) і перший інструмент — **Image Alt Text**: приймає таблицю аудиту (CSV/XLSX), дериває для кожної картинки **пошуковий фрагмент, за яким WordPress-адмінка реально знаходить медіафайл**, віддає операторську таблицю з кнопками «відкрити / копіювати», веде **прогрес по кожній картинці в БД** і вміє **перевірити без кешу**, чи новий alt уже на сторінці.
- **Хвиля:** I (розширення після MVP; усі попередні таски закриті)
- **Depends on:** — (адитивно розширює закриті зони task-02/03/06/12; координація контрактних змін — за процесом task-02, як робили таски 14–26)
- **PLAN.md:** §5.2 (дерево `app/`), §6 (модель даних, immutability), §10 (REST-конвенції, DTO), §12 (`None` ≠ `0`, quality/стани), §13 (безпека завантажень і download, generated safe filename).
- **frontend/AGENTS.md:** обов'язковий для всієї роботи у `frontend/`.
- **AISeoTools:** **не використовується** (рішення користувача). Інструмент повністю self-contained у цьому репозиторії й працює на CI/деплої без bind-mount.

Зведений таск-док (як task-25/26). Межі під-частин проведені в «Owned paths».

---

## Мета

### 1. Сторінка Tools (хаб)

Окремий верхньорівневий маршрут `#/tools` — операторська полиця внутрішніх інструментів агенції. Інструментів поки мало, але сторінка проєктується як **каталог, що росте**:

- **пошук** по інструментах (назва / опис / теги) — не «декоративний», а робочий фільтр із клавіатурним фокусом;
- **блок на інструмент**: іконка, назва, один рядок «що він робить», фактичний стан (напр. `3 reports · last used 24 Jul`), одна основна дія «Open»;
- блок — це `<a>` (навігація), а не clickable `<div>` (AGENTS.md §7);
- каталог приходить з API (`GET /api/v1/tools`), тож додати наступний інструмент = один запис у реєстрі, а не переверстка сторінки.

### 2. Інструмент Image Alt Text

Робочий процес оператора сьогодні: в аудиті є таблиця картинок без alt (або з поганим alt), у ній URL картинки та кілька полів, **головне — новий alt**. Далі оператор руками вирізає з URL частину назви файлу, бо:

- у медіатеку WordPress **повний URL не шукається** — пошук іде по назві/файлу вкладення, не по URL;
- resize-плагіни й самі розміри WP додають суфікси (`-300x200`, `-scaled`, `-e1712345678901`, `@2x`), і **з такою назвою файл теж не знаходиться**.

Інструмент прибирає цю ручну роботу:

1. **Завантажити файл** таблиці — **XLSX** (з вибором вкладки) або **CSV/TSV**.
2. **Розпарсити** й змапити колонки (автовизначення + ручний override).
3. **Дерівувати для кожного рядка ідеальний пошуковий фрагмент** для медіатеки + класифікувати URL (медіатека / тема / плагін / зовнішній хост) і показати, що саме було відрізано.
4. **Гарна таблиця:** прев'ю картинки, посилання (відкрити + копіювати), пошуковий фрагмент (копіювати), новий alt (копіювати).
5. **Кожен звіт пишеться в БД.** Натиснули «копіювати alt» → рядок отримує статус «alt copied», щоб зручно бачити прогрес (`12 / 148`).
6. Якщо у файлі є **сторінка, де картинка використовується** — кнопка **«Verify alt»**: запит **без кешу** (рандомний cache-buster + `no-cache`-заголовки, бо шар кешу дуже великий), пошук цієї картинки в HTML і порівняння alt із очікуваним. Результат зберігається (verdict + знайдений alt + час).

**Ключова інваріанта:** інструмент **нічого не вигадує**. Пошуковий фрагмент — детермінована функція від URL; кожне відрізання показується оператору. Якщо URL не з медіатеки (`/wp-content/themes/…`) — це чесно позначено окремим станом, а не «схожим» фрагментом. `None` (не змогли перевірити) ≠ «alt не оновлений» (PLAN §12).

---

## Дослідження: чому URL не шукається і як вивести правильний фрагмент

Це серце таски — тут вся складність, тому логіка фіксується як **чиста функція** з табличними тестами.

### Що насправді шукає медіатека WP

Пошук у Media Library — це `LIKE '%term%'` по полях вкладення (`post_title`, `post_content`, `post_excerpt`) і, у сучасних версіях, по значенню `_wp_attached_file` — а це **`2024/03/hero-image.jpg`**, тобто **шлях + назва оригіналу**, ніколи не повний URL і ніколи не назва зі суфіксом розміру. Звідси два практичні висновки:

- шукати треба **stem оригінального файлу** (назву без розширення), а не URL і не `?query`;
- **усі похідні суфікси** (розмір, `-scaled`, edit-timestamp, `@2x`) мусять бути відрізані, інакше збігу не буде.

### Реальні форми URL, які треба витримати

| Форма | Приклад | Що робимо |
|---|---|---|
| Оригінал | `/wp-content/uploads/2024/03/hero-image.jpg` | stem як є |
| Розмір (WP / resize-плагіни) | `hero-image-300x200.jpg`, `hero-image-1024x683.jpg` | зняти `-\d{1,5}x\d{1,5}$` |
| Big-image (WP 5.3+) | `hero-image-scaled.jpg` | зняти `-scaled$` |
| Повернуте зображення | `hero-image-rotated.jpg` | зняти `-rotated$` |
| Редаговане в WP-редакторі | `hero-image-e1712345678901.jpg` (+ його розміри) | зняти `-e\d{10,13}$` |
| Retina | `hero-image@2x.jpg` | зняти `@\d+x$` |
| WebP-конвертери | `hero-image.jpg.webp`, `…/webp-express/doc-root/wp-content/uploads/…/hero-image.jpg.webp` | зняти подвійне розширення, взяти шлях від **останнього** `wp-content/uploads` |
| Photon / Jetpack | `i0.wp.com/site.com/wp-content/uploads/…?resize=300,200` | розгорнути проксі-обгортку, відкинути `?resize`/`?fit`/`?w` |
| Cloudflare Image Resizing | `/cdn-cgi/image/width=300,quality=80/wp-content/uploads/…` | те саме правило «останній `wp-content/uploads`» |
| Percent-encoding / не-латиниця | `%D1%84%D0%BE%D1%82%D0%BE-1.jpg` | percent-decode перед деривацією |
| Дублікат завантаження | `hero-image-1.jpg` | **не** відрізати `-1` (це справжня назва вкладення), але видати **fallback-фрагмент** без нього |
| Не медіатека | `/wp-content/themes/foo/img/x.png`, `/wp-content/plugins/…` | `url_kind = theme_asset / plugin_asset` + підказка «правиться в шаблоні, не в медіатеці» |
| Зовнішній хост | `https://cdn.other.com/x.jpg` | `url_kind = external` + попередження |
| `data:`/порожньо/сміття | | `url_kind = unknown`, рядок лишається у звіті зі станом «не розпізнано» |

### Алгоритм `derive_search_key(url)` (чиста функція)

1. Розпарсити URL; зберегти хост; відкинути fragment; запам'ятати resize-подібні query-параметри (сигнал, не дані).
2. **Розгорнути проксі/CDN-обгортки:** якщо в шляху `wp-content/uploads` трапляється більше одного разу — брати від **останнього**; окремо розпізнати `i[0-3].wp.com/<host>/<path>` і `/cdn-cgi/image/<opts>/<path>`.
3. Percent-decode шляху, взяти basename.
4. Зняти подвійне розширення (`.jpg.webp` → `.jpg`), потім розширення зі списку відомих (`jpg|jpeg|png|gif|webp|avif|svg`).
5. **Ітеративно** знімати хвостові модифікатори (поки хоч один матчиться), фіксуючи кожне зняття в `stripped[]`: `-{W}x{H}`, `-scaled`, `-rotated`, `@Nx`, `-e{timestamp}`.
6. **Не** знімати хвостовий `-\d+` — але якщо він є, віддати `search_key_fallback` без нього + попередження «можливо, лічильник дубліката: якщо не знайдеться, шукайте коротший фрагмент».
7. Класифікувати `url_kind` за шляхом/хостом (див. таблицю).
8. Повернути `SearchKey(search_key, search_key_fallback, url_kind, stripped, warnings)`.

Функція **ідемпотентна** (повторне застосування нічого не змінює) і **не звертається в мережу**. Хост клієнта для класифікації `external` беремо з `Client.domain`, якщо звіт привʼязаний до клієнта; інакше — з домену-модою по самому файлу (і це позначається як припущення, а не факт).

### Перевірка alt без кешу

- `GET <page_url>` з **рандомним cache-buster** (`?yv=<8 hex>`, коректно доклеєним до наявного query) + `Cache-Control: no-cache`, `Pragma: no-cache`, звичайний browser-UA (частина хостів ріже `python-urllib`), follow-redirects, timeout ~15 c, **cap на розмір тіла** (5 МБ).
- Розпарсити HTML **stdlib-парсером** (`html.parser`), зібрати всі `<img>` (+ `<source>` у `<picture>`), для кожного взяти `src`/`data-src`/`data-lazy-src`/перший елемент `srcset`.
- **Зматчити** кандидата тим самим `derive_search_key` (stem-рівність), не рівністю URL — інакше resize-варіант на сторінці не збігся б із URL зі звіту.
- Вердикти (**окремі стани**, не «ок/не ок»): `match` · `mismatch` (показуємо знайдений alt) · `empty_alt` · `image_not_found_on_page` · `fetch_failed` (з HTTP-статусом/причиною).
- Чесне обмеження: якщо тема рендерить картинки через JS, alt у HTML не буде → `image_not_found_on_page` з підказкою «можливо, lazy-рендер; перевірте вручну». Рендер у Chromium — свідомий follow-up (див. нижче), як і в task-25 фаза 1.
- UI прямо каже: «Fetched with a cache-buster; an edge cache may still serve stale HTML — retry if you just saved.» Ніякої обіцянки, якої не можемо дотримати.

---

## Архітектура (рішення зафіксовані)

**Це НЕ провайдер і НЕ job.** Свідоме відхилення від шаблону task-25/26:

- парсинг таблиці — мілісекунди, один HTTP-запит перевірки — секунди; durable-worker, `SyncJob`, SSE, lease/recovery тут не дають нічого, крім складності;
- `Provider` enum **не розширюється** (жодного `Provider.alt_text`) — інструмент не колекціонує метрики, не має інтеграції, не бере участі в синку/health/каталозі;
- усе живе в API-процесі; блокуючі частини (openpyxl, HTTP-перевірка) ідуть через `run_in_threadpool` — патерн уже застосований у [app/api/blogs.py](../../../app/api/blogs.py).

**Нові таблиці — потрібні** (теж відхилення від task-25/26, які обійшлися `AnalyticsDatasetRow`). Причина принципова: `AnalyticsDatasetRow` привʼязаний до `ProviderSnapshot`/провайдера/періоду й **immutable**, а тут дані (а) не мають провайдера й періоду, (б) **мутабельні за задумом** — статус прогресу та результат перевірки змінюються багато разів на життя рядка. Втискувати це в immutable snapshot-модель означало б або ламати інваріант, або append-only-хаки на кожен клік.

**Без нових залежностей.** `openpyxl` уже пряма залежність (task-07) → XLSX. CSV/TSV — stdlib `csv`. HTTP-перевірка — stdlib `urllib.request`; HTML — stdlib `html.parser`. `httpx`/`lxml`/`pandas` у образі присутні лише **транзитивно** через optional-групу `local` (AISeoTools) — спиратись на них означало б зламати CI/деплой без bind-mount, тому **не спираємось**. Це також знімає ризик із `poetry lock` (див. пам'ять про sandbox) і не вимагає нічого від task-01.

**Legacy `.xls` (BIFF, Excel ≤2003) не підтримуємо у цій тасці** — для нього потрібен `xlrd`, якого в образі немає, а нова залежність — зона task-01. Замість тихого падіння: `415` з дією — «Legacy .xls is not supported. Re-save the sheet as .xlsx or export CSV.» Сучасні експорти аудитів (Ahrefs / Semrush / Screaming Frog / Google Sheets) — `.xlsx`/`.csv`, тож практичного розриву немає. Якщо реальні файли все ж `.xls` — follow-up із запитом залежності до task-01.

**Двокроковий upload — вимушений і корисний.** Вкладку XLSX і мапінг колонок неможливо знати до читання файлу, тому: крок 1 `POST /uploads` (сирі байти) → повертає `upload_id`, список вкладок, прев'ю заголовків і **автопропозицію мапінгу**; крок 2 `POST /reports` (`upload_id` + вкладка + мапінг) → парсить усе й пише звіт. Файл тримається на артефакт-волюмі під **згенерованим** імʼям (`safe_resolve`, ніколи з імені користувача — PLAN §13), TTL-прибирання при створенні звіту.

**Автовизначення колонок** — за нормалізованими заголовками (lowercase, без пунктуації), з синонімами: `image url|image|img|src|url картинки` → `image_url`; `new alt|alt text|new alt text|alt|альт` → `new_alt`; `page url|page|used on|source page|location` → `page_url`; `old alt|current alt|existing alt` → `old_alt`. Усі решта колонок зберігаються в `extra_json` (нічого не втрачаємо) і показуються у деталях рядка. Обовʼязкові для мапінгу: `image_url` і `new_alt`; решта опційна. Оператор бачить і може перебити будь-який автозбіг.

**Multipart не використовуємо** — сирі байти в тілі + `X-Filename` заголовок, точно як `upload_logo` в [app/api/clients.py:217](../../../app/api/clients.py#L217) (щоб не тягнути `python-multipart`).

---

## Модель даних (2 нові таблиці + 1 міграція)

Міграція `revises = e58b2f4d6a91` (поточний head). Стиль — як [b6e13a8c9f42](../../../migrations/versions/b6e13a8c9f42_analytics_dataset_rows.py): явні `sa.Column`, `app.db.base.GUID/UTCDateTime`, іменовані констрейнти через `op.f()`. Статуси й вердикти — **`String`, не `sa.Enum`** (це UI-стани інструмента, а не спільний контракт провайдерів); валідація — на рівні Pydantic-DTO.

### `alt_text_reports`

| Колонка | Тип | Призначення |
|---|---|---|
| `id` | GUID PK | |
| `client_id` | GUID FK `clients.id` **nullable**, `ondelete=SET NULL` | звіт може бути й для не-клієнта (проспект/разовий аудит) |
| `title` | String(200) | обовʼязкова людська назва |
| `source_filename` | String(255) | оригінальне імʼя (лише для показу) |
| `source_kind` | String(16) | `xlsx` \| `csv` \| `tsv` |
| `source_sheet` | String(255) nullable | вибрана вкладка (для XLSX) |
| `source_sha256` | String(64) | ідемпотентність: повторне завантаження того самого файлу+вкладки → `409` з `existing_report_id` |
| `column_map_json` | JSON | фактичний мапінг, який застосували |
| `row_count` | Integer | скільки рядків розпарсили |
| `skipped_count` | Integer | скільки рядків відкинули (без URL) — видно в UI, не ховаємо |
| `created_at` / `updated_at` | UTCDateTime | |

### `alt_text_items`

| Колонка | Тип | Призначення |
|---|---|---|
| `id` | GUID PK | |
| `report_id` | GUID FK `alt_text_reports.id` `CASCADE`, index | |
| `row_index` | Integer | номер рядка у джерелі (traceability) |
| `image_url` | String(2048) | як у файлі |
| `search_key` | String(512) | **головний результат деривації** |
| `search_key_fallback` | String(512) nullable | коротший варіант (зняли `-\d+`) |
| `url_kind` | String(24) | `uploads` \| `theme_asset` \| `plugin_asset` \| `external` \| `unknown` |
| `derivation_json` | JSON | `stripped[]` + `warnings[]` — щоб оператор бачив, **що** відрізали |
| `new_alt` | Text | головне поле |
| `old_alt` | Text nullable | |
| `page_url` | String(2048) nullable | вмикає кнопку «Verify alt» |
| `extra_json` | JSON | усі інші колонки джерела |
| `status` | String(16) | `pending` \| `copied` \| `applied` \| `skipped` |
| `status_changed_at` | UTCDateTime nullable | |
| `verify_status` | String(24) nullable | `match` \| `mismatch` \| `empty_alt` \| `image_not_found_on_page` \| `fetch_failed` |
| `verify_alt` | Text nullable | фактично знайдений alt |
| `verify_http_status` | Integer nullable | |
| `verify_detail` | Text nullable | причина `fetch_failed` (без секретів) |
| `verify_checked_at` | UTCDateTime nullable | |

Констрейнти: `UniqueConstraint(report_id, row_index)`, `Index(report_id, status)`.

**Семантика статусів** (пряме прочитання вимоги «коли тиснули копіювати має створюватися статус що ця картинка додана»):

- `pending` — нічого не робили;
- `copied` — оператор скопіював **новий alt** (копіювання пошукового фрагмента статус **не** змінює: це проміжний крок, а не «зроблено»);
- `applied` — підтверджено: або ручним перемикачем, або автоматично, коли `verify_status == match`;
- `skipped` — оператор свідомо пропустив (напр. картинка з теми).

Усі чотири — різні стани з іконкою+текстом (не колір-онлі, AGENTS.md §7). Є undo (`PATCH status=pending`). `applied` **ніколи** не виставляється здогадкою.

---

## API (префікс `/api/v1`, конвенції PLAN §10)

**Каталог хабу**

- `GET /tools` → `ToolsCatalogDTO{ tools: [ToolCardDTO{key, name, summary, tags[], route, status, stat_label, stat_value}] }`. Реєстр — у коді (`app/api/tools.py`); `stat_*` наповнює сам інструмент (для alt-text — кількість звітів + дата останнього).

**Інструмент Alt Text** (`/tools/alt-text/…`)

- `POST /uploads` — сирі байти + `X-Filename`; cap 10 МБ → `AltTextUploadDTO{upload_id, source_kind, sheets[], preview{sheet: {headers[], sample_rows[≤5]}}, suggested_column_map}`. Помилки: `413 upload_too_large`, `415 unsupported_source` (в т.ч. legacy `.xls`), `400 unreadable_source`.
- `POST /reports` — `{upload_id, sheet?, column_map, title, client_id?}` → `AltTextReportDTO`. `409 report_exists` з `existing_report_id`, якщо той самий `sha256+sheet` уже є; `?force=true` — створити копію свідомо. `422` якщо не змаплені `image_url`/`new_alt`.
- `GET /reports` → список `AltTextReportSummaryDTO` (+ прогрес-лічильники по статусах).
- `GET /reports/{id}` → звіт + лічильники + мапінг + `skipped_count`.
- `GET /reports/{id}/items?q=&status=&url_kind=&verify=&limit=50&offset=0` → `{total, limit, offset, rows[AltTextItemDTO]}`.
- `PATCH /reports/{id}/items/{item_id}` — `{status}` → оновлений `AltTextItemDTO` (ідемпотентно).
- `POST /reports/{id}/items/{item_id}/verify` → виконує no-cache перевірку, зберігає й повертає результат. `409 no_page_url`, якщо у рядка немає сторінки.
- `DELETE /reports/{id}` → `204`, каскадом чистить рядки.
- `GET /reports/{id}/export.csv` / `export.xlsx` — таблиця з деривацією + статусами + вердиктами (патерн експорту з [app/api/blogs.py:257](../../../app/api/blogs.py#L257)); CSV-експорт екранує formula-injection (`=`/`+`/`-`/`@` → префікс `'`).

DTO — у **новому** файлі `app/schemas/tools.py` (суто адитивно до зони task-02; жоден наявний контракт не змінюється). `openapi_snapshot.json` і `contract_snapshot.json` перегенерувати.

---

## Frontend (AGENTS.md обовʼязковий)

**Маршрути** (`router.js`, адитивно до наявних `#/client/…`):

- `#/tools` — хаб;
- `#/tools/alt-text` — інструмент: створення звіту + список звітів;
- `#/tools/alt-text/{reportId}?q=&status=&off=` — робоча таблиця (весь стан у URL, AGENTS.md §12).

**Вхід у сторінку:** посилання `Tools` у masthead (`index.html`), видиме на всіх маршрутах — не hamburger (AGENTS.md §3), бо навігація вміщується.

**Хаб `#/tools`:** eyebrow `Operations / Tools`, заголовок, пошуковий інпут із видимим лейблом і `aria-live` лічильником результатів, сітка блоків. Блок несе **реальну** інформацію (що робить + власна статистика), одну дію, без карток у картках і без «01/02/03».

**Інструмент `#/tools/alt-text`:**

- панель **New report**: нативний `<input type="file">` з видимим лейблом (+ drop-zone як прогресивне доповнення, ніколи як єдиний спосіб), потім крок 2 — `<select>` вкладки, мапінг колонок нативними `<select>`, прев'ю перших 5 рядків, `Create report`. Submit лишається активним до старту запиту, далі — спінер зі збереженим лейблом (AGENTS.md §8).
- список звітів: назва, клієнт, файл/вкладка, дата, прогрес `12 / 148` (справжній completed/total, без анімованих відсотків), дії `Open` / `Export` / `Delete` (з підтвердженням).

**Таблиця звіту `#/tools/alt-text/{reportId}`:**

| Колонка | Вміст |
|---|---|
| `#` | номер рядка джерела |
| Preview | `<img loading="lazy">` у боксі з фіксованим `aspect-ratio` (нуль layout shift), `alt=""` (декоративне), `onerror` → текстовий стан «Preview unavailable» |
| Image URL | усічений моно-текст + `Open ↗` (`<a target=_blank rel=noopener>`) + `Copy` |
| Search key | моно-текст + `Copy` + чіп-підказка, **що** відрізали (`-1024x683`, `-scaled`) і `url_kind`-бейдж |
| New alt | текст із line-clamp + `Expand` + `Copy` (**ця копія ставить статус `copied`**) |
| Page | `Open ↗` + `Verify alt` + бейдж вердикту з часом; нема `page_url` → чесний прочерк, не disabled-кнопка без пояснення |
| Status | бейдж (іконка+текст) + меню `Mark applied` / `Skip` / `Reset` |

- Фільтри: пошук по URL/alt/фрагменту, статус, `url_kind`, вердикт; лічильники прогресу в шапці; пагінація 50/стор.
- Копіювання: `navigator.clipboard.writeText` + fallback на `document.execCommand` для non-secure origin; успіх → toast (`aria-live`) і `PATCH` статусу; помилка копіювання **не** ставить статус.
- Порядок клавіатурою: кожна дія — справжній `<button>` з `aria-label`, що містить контекст рядка (`Copy alt text for row 12`).
- Обовʼязкові стани (AGENTS.md §6): initial loading (скелет у геометрії таблиці), пустий звіт, звіт без `page_url` у жодному рядку, дуже довгі alt/URL, `unknown`-URL, помилка мережі, помилка парсингу, помилка перевірки, 0 результатів фільтра.
- CSS — новий `frontend/css/tools.css` з наявними токенами; `.table-scroll` як у client-page + урок task-26 (`overflow-x: hidden` на корені сторінки, щоб flex-column не давав горизонтальний page-scroll на 390px).
- Фікстури: `tools-catalog.json`, `alt-text-reports.json`, `alt-text-items.json`, `alt-text-items-empty.json` — для fixture-режиму `api.js`.

---

## Безпека (PLAN §13)

- **Upload:** cap 10 МБ; sniff за магією (`PK\x03\x04` для xlsx, декодовний текст для csv), а не за розширенням; **згенероване** імʼя файлу через `safe_resolve(settings.artifacts_dir, …)`; оригінальне імʼя лише як дані для показу.
- **Zip-bomb / величезні аркуші:** `openpyxl.load_workbook(read_only=True, data_only=True)`, жорсткі cap-и на рядки (20 000) і довжину клітинки (8 КБ) — перевищення = `truncated` у звіті, чесно показане, а не тихе обрізання.
- **SSRF на перевірці:** лише `http`/`https`; резолв хоста й **відмова** для loopback / private / link-local / metadata-адрес; заборона redirect-у в приватний діапазон; timeout і cap тіла.
- **CSV-експорт:** захист від formula injection.
- **XSS:** усі значення з файлу — недовірені; рендер через DOM API / `escapeHtml`; `href` прев'ю та посилань валідуються на схему.
- Секрети в цьому інструменті не фігурують; `verify_detail` пише причину без заголовків і без URL із токенами.

---

## Обсяг

**Входить:**

1. Чисті модулі: `app/tools/alt_text/wp_media.py` (деривація), `tables.py` (CSV/XLSX, вкладки, автомапінг), `verify.py` (no-cache fetch + пошук `<img>` + порівняння alt, з інʼєктованим fetcher-ом для тестів).
2. Моделі `AltTextReport`/`AltTextItem` + міграція від `e58b2f4d6a91` + репозиторій `app/db/repositories/alt_text.py`.
3. DTO `app/schemas/tools.py`; роутери `app/api/tools.py` (каталог) і `app/api/tools_alt_text.py`.
4. Frontend: `tools-page.js`, `alt-text-tool.js`, `css/tools.css`, фікстури, адитивні дотики `router.js` / `api.js` / `index.html`.
5. Тести (див. нижче) + перегенеровані OpenAPI/contract-снапшоти.
6. `graphify update .` після робіт.

**Не входить (свідомо):**

- запис alt **у** WordPress (REST/WP-CLI) — інструмент лишається «operator-in-the-loop»: копіює й перевіряє, не пише в чужий сайт;
- legacy `.xls`;
- рендер сторінки в Chromium для JS-рендереного alt;
- bulk-verify всієї таблиці одним натиском (лишаємо per-row; послідовний прохід по видимій сторінці — follow-up, щоб не влаштувати мимоволі краш-тест клієнтського сайту);
- будь-яка інтеграція з AISeoTools;
- ШІ-генерація alt-текстів (alt приходить із таблиці аудиту).

---

## Owned paths

**Нові (повністю наші):**

```
app/tools/**                                (новий пакет)
app/api/tools.py, app/api/tools_alt_text.py
app/db/repositories/alt_text.py
app/schemas/tools.py                        (новий файл у зоні task-02 — адитивно)
migrations/versions/<new>_alt_text_tool.py  (зона task-03 — одна нова міграція, адитивно)
frontend/js/tools-page.js, frontend/js/alt-text-tool.js
frontend/css/tools.css
frontend/fixtures/tools-*.json, frontend/fixtures/alt-text-*.json
tests/unit/tools/**, tests/unit/api/test_tools_alt_text.py
tests/e2e/frontend/tools-alt-text.cjs
```

**Адитивні дотики чужих файлів (кожен — задокументувати в Completion report, не мовчки):**

```
app/db/models.py            + 2 класи моделей            (task-03)
frontend/js/router.js       + маршрути #/tools*          (task-18)
frontend/js/api.js          + функції інструмента        (task-12)
frontend/index.html         + nav-лінк Tools + #tools-page-root (task-12)
tests/unit/api/data/openapi_snapshot.json                (task-06/13 — перегенерація)
tests/unit/schemas/data/contract_snapshot.json           (task-02/13 — перегенерація)
doc/dev/new/INDEX.md        + рядок таски                (процес)
```

**Не торкаємось:** `app/schemas/enums.py` (жодного нового `Provider`), `app/jobs/**`, `app/metrics/**`, `pyproject.toml`, `Dockerfile`, `compose.yaml`, `app/core/config.py` (константи інструмента живуть у його модулі, щоб не заходити в зону task-01).

---

## План виконання

1. **Деривація перша, все інше — потім.** `wp_media.py` + табличний тест на ~35 реальних форм URL із таблиці вище. Це той шар, від правильності якого залежить весь сенс інструмента.
2. **Парсер таблиць.** `tables.py`: sniff формату, список вкладок, прев'ю, автомапінг, повний парсинг у dataclass-рядки; окремо тести на BOM, `;`-делімітер, порожні рядки, дублікати URL, відсутність обовʼязкової колонки.
3. **БД:** моделі → міграція → репозиторій (create report + bulk insert items, пагінований/фільтрований read, `set_status`, `set_verify_result`, counters). `alembic upgrade head` у контейнері; перевірити ще й `downgrade`.
4. **API:** DTO → роутер каталогу → роутер інструмента. Кожен мутуючий ендпоінт комітить сам (як у `app/api/clients.py`). Перегенерувати снапшоти.
5. **Перевірка alt:** `verify.py` з інʼєктованим fetcher-ом + SSRF-гард; тести на всі пʼять вердиктів і на присутність cache-buster-а.
6. **Frontend:** хаб → сторінка інструмента → таблиця; спершу на фікстурах, далі проти живого API. Візуальний loop AGENTS.md §15 на 390/768/1024/1440 + 200% zoom, скріншоти до/після.
7. **e2e** сценарій: upload → мапінг → створення → копія alt (статус) → verify (замоканий сервер) → фільтр → експорт.
8. **Гейт:** `ruff`, `mypy app`, `pytest` — усе в Docker. Потім `graphify update .`, `git mv` плану в `done/`, Completion report, оновлення INDEX.

---

## Ризики й рішення

| Ризик | Рішення |
|---|---|
| Деривація дасть фрагмент, за яким у медіатеці нічого не знайдеться | Ніколи не подаємо один «магічний» рядок як істину: показуємо `search_key` + `search_key_fallback` + перелік знятого + `url_kind`. Оператор бачить логіку, а не чорну скриньку. |
| Хтось із плагінів має ще один формат суфікса | Правила — дані (таблиця регексів у одному місці) + тести-таблиця; додати новий шаблон = один рядок і один кейс. `warnings[]` підсвічує «залишився підозрілий хвіст». |
| Edge-кеш віддасть стару HTML і перевірка збреше | Cache-buster + no-cache заголовки, а в UI — явна дисклеймер-копія й `verify_checked_at`. Вердикт ніколи не переписує статус на `applied`, окрім `match`. |
| Клієнтський сайт впаде від перевірок | Per-row перевірка на клік; bulk свідомо поза обсягом; timeout + cap тіла. |
| Legacy `.xls` реально трапляється в аудитах | Явна `415`-помилка з дією замість тихої поломки; якщо підтвердиться — follow-up із запитом `xlrd` до task-01. |
| Довгі alt/URL і не-латиниця розваляють таблицю | line-clamp + `min-width: 0` + `title`-tooltip + перевірка на реальних довгих значеннях у візуальному loop. |
| Таблиця на 5–10 тис. рядків | Пагінація 50 на сервері + `content-visibility: auto`; віртуалізація — тільки після вимірювання (AGENTS.md §13). |

---

## Тести

- `tests/unit/tools/test_wp_media.py` — табличні кейси деривації (усі форми з таблиці вище + ідемпотентність + `url_kind`).
- `tests/unit/tools/test_tables.py` — CSV (BOM, `,`/`;`/`\t`), XLSX (кілька вкладок, порожні рядки, cap рядків/клітинок), автомапінг заголовків, помилки обовʼязкових колонок, збереження решти колонок в `extra_json`.
- `tests/unit/tools/test_verify.py` — фейковий fetcher: `match` / `mismatch` / `empty_alt` / `image_not_found_on_page` / `fetch_failed`(404, timeout); присутність cache-buster-а при наявному й відсутньому query; SSRF-відмови (loopback/private/`file:`); матч resize-варіанта на сторінці проти оригіналу зі звіту.
- `tests/unit/db/test_alt_text_repository.py` — create+bulk insert, унікальність `(report_id, row_index)`, фільтри/пагінація, `set_status` (ідемпотентно, `status_changed_at`), `set_verify_result`, каскадне видалення, лічильники.
- `tests/unit/api/test_tools_alt_text.py` — на harness (`temp_db`, `api_client`, `seed_client`): повний флоу + `409` ідемпотентності + `413`/`415`/`422` + `404` на чужий `report_id`/`item_id` + експорт CSV/XLSX (у т.ч. екранування формул) + каталог `GET /tools`.
- `tests/unit/api/test_openapi_snapshot.py` — перегенерований снапшот зелений.
- `tests/e2e/frontend/tools-alt-text.cjs` + скріншоти 390/1440 (за патерном `client-page-smoke.cjs`).

---

## Definition of Done / Gate

- [ ] `#/tools` існує, доступний із masthead на будь-якому маршруті, має робочий пошук і блок першого інструмента з реальною статистикою.
- [ ] XLSX із кількома вкладками й CSV/TSV завантажуються; вкладка вибирається; автомапінг колонок пропонується й перебивається вручну; legacy `.xls` дає зрозумілу `415`-помилку з дією.
- [ ] Для кожного рядка є `search_key`, класифікація URL і видимий перелік знятих суфіксів; усі форми URL із таблиці дослідження покриті тестами.
- [ ] Таблиця показує прев'ю, «Open», «Copy» для URL / фрагмента / alt; копіювання alt створює статус і він **переживає перезавантаження** (лежить у БД).
- [ ] Прогрес звіту читається одним поглядом (`copied/applied/total`, справжні числа).
- [ ] «Verify alt» робить запит без кешу з рандомним параметром, повертає один із пʼяти окремих вердиктів зі знайденим alt і часом перевірки; `applied` виставляється лише на `match`.
- [ ] Кожен звіт і кожен рядок лежать у БД; звіт видаляється каскадом; експорт CSV/XLSX працює.
- [ ] `None`/«не перевіряли» ніде не показано як «не оновлено» чи `0`.
- [ ] Усі обовʼязкові стани AGENTS.md §6 реалізовані; чекліст §16 пройдено; скріншоти 390/768/1024/1440 + 200% zoom перевірені очима.
- [ ] Клавіатура-онлі проходить весь флоу; фокус у діалогах повертається на opener; статуси не колір-онлі.
- [ ] Безпека: cap-и на upload, sniff, `safe_resolve`, SSRF-гард, formula-escaping — з тестами.
- [ ] `ruff check .`, `mypy app`, `pytest` — зелені **в Docker**; `alembic upgrade head` і `downgrade -1` проходять.
- [ ] `Provider` enum, `app/jobs/**`, `app/metrics/**`, `pyproject.toml` не змінені.
- [ ] `graphify update .` виконано; план перенесено у `doc/dev/done/` з Completion report; INDEX оновлено.

---

## Follow-ups (свідомо відкладене)

- `TODO(task-28?)` — запис alt напряму в WordPress (REST `/wp/v2/media/{id}` або WP-CLI) з підтвердженням оператора; вимагає рішення про зберігання креденшелів (PLAN §13: секрети не в БД).
- `TODO(task-29)` — verify з рендером у Chromium для тем, що ставлять alt через JS (той самий образ-апгрейд, який уже запланований для `SLOW_PAGES` у task-25).
- `TODO` — bulk-verify видимої сторінки з дроселюванням і кооперативним стопом.
- `TODO` — підтримка legacy `.xls` (запит `xlrd` до task-01), якщо реальні файли аудитів це вимагають.
- `TODO` — «розумний» пошук дублікатів у звіті (одна картинка на кількох сторінках) з груповим статусом.

---

## ✅ Completion report

- **Дата:** 2026-07-26
- **Гілка / комміт:** `task-27-tools-alt-text` @ (див. коміт цієї таски)
- **Статус:** done-with-followups

### Що зроблено

**Сторінка Tools (`#/tools`)** — верхньорівневий хаб інструментів: блоки з реальною статистикою, робочий пошук по назві/опису/тегах, глобальна навігація «Clients / Tools» у masthead. Каталог приходить з `GET /api/v1/tools`, тож наступний інструмент — один запис у реєстрі, а не переверстка.

**Інструмент Image Alt Text** (`#/tools/alt-text`) — повний операторський цикл:

1. Завантаження таблиці аудиту (**XLSX** з вибором вкладки, **CSV/TSV**), автовизначення колонок і ручний override, прев'ю перших рядків, авто-вибір клієнта за префіксом імені файлу.
2. Для кожного рядка — **детермінований фрагмент для пошуку в медіатеці WordPress** (знято розміри `-1024x683`, `-scaled`, `-rotated`, `@2x`, `-e{ts}`, друге розширення `.webp`, розгорнуто Photon/Cloudflare/Statically/weserv/WebP-Express обгортки), класифікація URL (`uploads` / `theme_asset` / `plugin_asset` / `external` / `unknown`) і перелік того, **що саме відрізали**.
3. Операторська таблиця: прев'ю (клік/Enter → модальний перегляд у повний розмір із новим alt поруч і діями «Open original / Copy URL / Copy alt»), «Open»/«Copy» для URL, копія пошукового фрагмента (+ коротший fallback), копія нового alt.
4. **Прогрес у БД:** копіювання alt ставить статус `copied`; є `applied` / `skipped` / скидання; лічильники в шапці й смуга в списку звітів.
5. **Перевірка alt без кешу:** рандомний cache-buster + `no-cache`, обхід до 3 сторінок використання до першого підтвердження, 5 окремих вердиктів, збережений час перевірки.
6. Експорт CSV/XLSX із деривацією, прогресом і вердиктами.

### Як зроблено (ключові рішення)

- **Не провайдер і не job** (відхилення від шаблону task-25/26): парсинг — мілісекунди, перевірка — один HTTP-запит. `Provider` enum, `app/jobs/**`, `app/metrics/**` не змінені; блокуючі виклики — через `run_in_threadpool`.
- **Дві нові мутабельні таблиці** `alt_text_reports` / `alt_text_items` (міграція `f69c3d5a8b17`). `AnalyticsDatasetRow` не підійшов: він immutable і привʼязаний до `ProviderSnapshot`/провайдера/періоду, а тут статус і вердикт змінюються багато разів на життя рядка.
- **Нуль нових залежностей:** XLSX — наявний `openpyxl`, CSV — stdlib `csv`, мережа — stdlib `urllib.request`, HTML — stdlib `html.parser`. `httpx`/`lxml`/`pandas` присутні в образі лише транзитивно через optional-групу `local` (AISeoTools) — спиратися на них означало б зламати CI/деплой без bind-mount.
- **Головні файли:**
  - `app/tools/alt_text/wp_media.py` — чиста деривація пошукового фрагмента (серце інструмента).
  - `app/tools/alt_text/tables.py` — читання XLSX/CSV, вкладки, автомапінг, розгортання об'єднаних клітинок, розбиття «кілька сторінок в одній клітинці».
  - `app/tools/alt_text/verify.py` — no-cache fetch + пошук `<img>` + порівняння alt + SSRF-гард.
  - `app/db/repositories/alt_text.py`, `app/api/tools.py`, `app/api/tools_alt_text.py`, `app/schemas/tools.py`.
  - `frontend/js/{tools-page,alt-text-tool}.js`, `frontend/css/tools.css`.
- **Legacy `.xls` — поза обсягом** (підтверджено користувачем: експорт із Google Sheets). Замість тихої поломки — `415` з дією.

### Знайдено й виправлено під час живих прогонів

Три дефекти, яких юніт-тести не показали:

1. **CSP блокував прев'ю картинок.** `default-src 'self'` не дозволяв жодного зовнішнього зображення, тобто ключова колонка не працювала взагалі. Розширено **лише** `img-src` (`app/core/security.py`); скрипти/стилі/fetch лишились під `'self'`. Альтернатива (проксі-ендпоінт) створила б SSRF-поверхню, якої зараз немає.
2. **Не-ASCII у назві звіту валив експорт (500).** HTTP-заголовки кодуються latin-1, тож «—» чи кирилиця в `Content-Disposition` кидали `UnicodeEncodeError`. Тепер ASCII-варіант + `filename*` (RFC 5987), є регресійний тест.
3. **Інлайновий `style="width"` у смузі прогресу блокувався CSP** — смуга завжди була порожня. Ширина ставиться через CSSOM.

4. **Один клік давав купу тостів і купу запитів.** Делеговані обробники
   навішувалися в `bindItemHandlers` на **кожен** рендер таблиці, а контейнер
   `#at-rows-body` при цьому не перестворюється (міняється лише його
   `innerHTML`) — після N перемальовувань (фільтр, пошук, пагінація) один клік
   спрацьовував N разів. Тепер делегація вішається рівно один раз при побудові
   shell (`data-delegated`), а `bindItemHandlers` прив'язує лише щойно
   створені конкретні контроли. Покрито e2e-регресією.

Плюс дві речі, що виявились на **реальному** Ahrefs-експорті клієнта:

5. **Об'єднані клітинки:** аудити групують «одна картинка → багато сторінок», і значення URL лежить лише у верхньому рядку групи — 939 із 997 рядків потрапляли у `skipped`. Додано розгортання merged-діапазонів (fallback, лише коли пропуски реально є).
6. **Кілька сторінок в одній клітинці:** «Used on page(s)» містить URL через новий рядок — такий рядок не був валідним URL, тож посилання й перевірка не працювали. Тепер це список (`page_urls_json`), UI показує перші три + «+N more», перевірка обходить до 3 сторінок до першого `match`.

### Контрактні зміни (Contract changes)

- **Новий файл** `app/schemas/tools.py` (адитивно до зони task-02; жоден наявний DTO не змінено).
- **Нові таблиці** `alt_text_reports` / `alt_text_items` + міграція `f69c3d5a8b17` (зона task-03, адитивно).
- `app/db/models.py` — 2 нові моделі; `app/core/security.py` — CSP `img-src` (зона task-01, обґрунтовано вище).
- `frontend/js/{router,api}.js`, `frontend/index.html` — адитивні дотики (маршрути `#/tools*`, функції API, nav + `#tools-page-root`).
- `Provider` enum, `METRIC_PROVIDERS`, `app/jobs/**`, `app/metrics/**`, `pyproject.toml` — **не змінені**.
- Перегенеровано `tests/unit/api/data/openapi_snapshot.json`.

### Як перевірити (verification)

```bash
docker compose up -d --build
docker compose exec api poetry run pytest tests/unit tests/integration tests/contract -q   # 776 passed
docker compose exec api poetry run ruff check .
docker compose exec api poetry run alembic upgrade head   # і `downgrade -1` — обидва чисті

# e2e (потрібен playwright-core + chromium)
NODE_PATH=/tmp/yovth-e2e/node_modules BASE_URL=http://127.0.0.1:6524 \
  node tests/e2e/frontend/tools-alt-text.cjs
```

В UI: `#/tools` → блок «Image Alt Text» → завантажити `.xlsx` аудиту → перевірити, що клієнт підставився з імені файлу, а колонки — автоматично → «Create report» → у таблиці натиснути «Copy alt» (рядок стає `Alt copied`, витримує F5) → «Verify alt» на рядку зі сторінкою.

### Тести

- `tests/unit/tools/test_wp_media.py` — 55 кейсів деривації (усі реальні форми URL + ідемпотентність + класифікація).
- `tests/unit/tools/test_tables.py` — CSV/TSV/XLSX, вкладки, автомапінг, гіперпосилання, об'єднані клітинки, кілька сторінок в клітинці, cap-и.
- `tests/unit/tools/test_verify.py` — усі 5 вердиктів, cache-buster, матчинг resize/webp/lazy/srcset, SSRF-гард.
- `tests/unit/db/test_alt_text_repository.py` — persistence, статуси, вердикти, фільтри, каскад.
- `tests/unit/api/test_tools_alt_text.py` — 36 тестів наскрізного флоу + помилкові шляхи + експорт (у т.ч. formula-injection і не-ASCII назва).
- `tests/e2e/frontend/tools-alt-text.cjs` — хаб, пошук, таблиця, копія→статус (переживає reload), verify, фільтри в URL, відсутність горизонтального скролу на 390px.

### Follow-ups / борг

- `TODO` — запис alt напряму у WordPress (REST/WP-CLI); вимагає рішення про зберігання креденшелів (PLAN §13).
- `TODO(task-29)` — verify з рендером у Chromium для тем, що ставлять alt через JS (зараз чесний `image_not_found_on_page`).
- `TODO` — bulk-verify видимої сторінки з дроселюванням.
- `TODO` — fallback-ключ інколи хибно спрацьовує на назвах на кшталт `UFCFightNight7-18` (трактує `-18` як лічильник дубліката). Головний ключ при цьому правильний, fallback лише додатковий.
- **Знайдено в сусідніх зонах, не чіпав:** експорти `app/api/blogs.py` і `app/api/tech_audit.py` формують `Content-Disposition` тим самим небезпечним способом — клієнт із кирилицею в назві дасть там 500 (той самий дефект, що виправлено тут). Власники — task-25/26.
- **Успадковано:** `mypy app` показує 8 помилок у `app/api/dashboard.py` і `app/db/repositories/tech_audits.py` — це незакомічена робота іншої сесії, не ця таска.
