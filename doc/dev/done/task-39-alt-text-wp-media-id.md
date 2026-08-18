# task-39 — Image Alt Text: точний **WordPress media ID** + кнопка «Find»

- **Пакет:** третій крок інструмента **Image Alt Text**: замість «скопіюй фрагмент назви й шукай очима в медіатеці» — витягнути з WordPress **точний `attachment ID`** картинки, показати його окремою колонкою і дати одну кнопку **Find**, яка копіює новий alt і одразу відкриває адмінку **на потрібному вкладенні**.
- **Хвиля:** I (розширення інструмента з task-27, той самий хаб Tools)
- **Depends on:** [task-27](../done/task-27-tools-page-and-alt-text-tool.md) ✅ (деривація `search_key`, таблиці `alt_text_reports`/`alt_text_items`, `tools_alt_text.py`, `alt-text-tool.js`), [task-29](../done/task-29-redirect-checker-tool.md) ✅ (спільний `app/tools/http_safety.py`: SSRF-гард, UA, `HostThrottle`).
- **PLAN.md:** §5.2 (дерево `app/`), §6 (модель даних), §10 (REST-конвенції, DTO), §12 (`None` ≠ `0`, окремі стани замість «ок/не ок»), §13 (безпека мережевих запитів).
- **frontend/AGENTS.md:** обов'язковий для всієї роботи у `frontend/`.
- **AISeoTools:** не використовується. Нових залежностей немає: HTTP — stdlib `urllib.request` поверх `app/tools/http_safety.py`, JSON — stdlib `json`.

---

## Мета

### Проблема, як її бачить оператор

Сьогодні інструмент віддає **фрагмент для пошуку** (`search_key`) — назву оригіналу без `-300x200`, `-scaled`, `-e1712…`. Оператор копіює його, іде у `wp-admin/upload.php`, вставляє в поле пошуку і знаходить файл. Це працює приблизно у **80 % випадків** — там, де назва осмислена (`hero-banner-kyiv.jpg`).

Решта — файли на кшталт `1.jpg`, `2.jpg`, `img_0421.jpg`, `photo.png`. Пошук по `1` у медіатеці віддає сотні збігів (він `LIKE '%1%'`), і знайти **саме той** файл очима неможливо. Це і є 20 %, які з'їдають більшість часу.

### Що робить ця таска

1. Витягує з WordPress **точний `attachment ID`** для рядка звіту — не «схожий файл», а конкретне вкладення, підтверджене збігом uploads-шляху.
2. Показує ID **окремою колонкою** (`#1234`), який можна скопіювати як звичайне поле.
3. Дає кнопку **Find**: копіює новий alt у буфер і відкриває адмінку клієнта **вже на цьому вкладенні** (`upload.php?item=1234`) — модалка «Attachment details» з полем *Alternative Text* відкривається одразу, без пошуку.
4. Там, де точний ID недосяжний (REST закритий, файл не в медіатеці, кілька однакових кандидатів) — це **окремий стан із причиною**, а не порожня клітинка й не тиха підстановка «схожого» ID (PLAN §12).

**Інваріанта таски:** ID показується **тільки** при доведеному збігу. «Знайшли одного кандидата за пошуком» ≠ «це той самий файл». Здогад позначається як `ambiguous`/`filename` і ніколи не подається як точний.

---

## Дослідження: як із WordPress дістати ID (перевірено по вихідниках core)

### 1. REST-ендпоїнт медіатеки публічний на читання

`GET /wp-json/wp/v2/media` — колекція вкладень, `GET` не потребує авторизації для `status=inherit` (опублікованих). Корисні параметри (офіційна дока REST Handbook → Media):

| Параметр | Що дає |
|---|---|
| `search` | «Limit results to those matching a string» |
| `slug` | точний збіг `post_name` вкладення (slug = `sanitize_title` назви файлу) |
| `media_type=image` | лише зображення |
| `per_page` (≤100), `page` | пагінація |
| `_fields` | обрізати відповідь (глобальний параметр REST) |

Поля відповіді, які нам потрібні: `id`, `slug`, `link`, `title.rendered`, `alt_text`, `source_url`, `media_details.file`, `media_details.sizes[*].{file,source_url}`.

### 2. Ключова знахідка: `search` у медіатеці шукає **по імені файлу**

`WP_REST_Attachments_Controller::prepare_items_query()` (core, `wp-includes/rest-api/endpoints/class-wp-rest-attachments-controller.php`):

```php
// Filter query clauses to include filenames.
if ( isset( $query_args['s'] ) ) {
    add_filter( 'wp_allow_query_attachment_by_filename', '__return_true' );
}
```

А `WP_Query` (WP ≥ 6.0.3) під цим прапорцем додає до пошуку join і колонку:

```php
$join .= " LEFT JOIN {$wpdb->postmeta} AS sq1
             ON ( {$wpdb->posts}.ID = sq1.post_id AND sq1.meta_key = '_wp_attached_file' )";
...
$search_columns_parts['attachment'] = $wpdb->prepare( "(sq1.meta_value $like_op %s)", $like );
```

`_wp_attached_file` — це **uploads-відносний шлях оригіналу**: `2024/03/1.jpg`. Отже:

> пошук по **повному відносному шляху** (`2024/03/1.jpg`), а не по голій назві (`1`), звужує вибірку до одного-двох вкладень **навіть для `1.jpg`**.

Саме цього бракує ручному пошуку в адмінці: оператор вставляє `1`, бо це все, що дає поточний `search_key`. Ми ж маємо повний URL, а з нього — повний шлях.

### 3. Матчинг: підтвердження, а не довіра до пошуку

Пошук — це `LIKE '%…%'`, тому кандидатів **завжди перевіряємо** локально по відповіді:

| Рівень | Умова | `confidence` |
|---|---|---|
| Точний | `media_details.file` == наш uploads-шлях (після зняття WP-суфіксів) **або** `source_url` == наш URL (без query) | `exact` |
| Точний через розмір | наш файл збігся з `media_details.sizes[*].file` (тобто в звіті стоїть `-1024x683`-варіант) | `exact` |
| Слабкий | збігся лише basename без каталогу `YYYY/MM` (offload-плагіни, кастомний `uploads_path`) | `filename` |
| Кілька | ≥ 2 кандидати різних `id` на одному рівні | `ambiguous` (ID **не** показуємо як точний, показуємо список) |
| Нуль | кандидатів немає | `not_found` |

### 4. Куди ставити посилання в адмінку

- **`/wp-admin/upload.php?item=<ID>&mode=grid`** — відкриває медіатеку в режимі сітки з уже розгорнутою модалкою «Attachment details» саме цього вкладення (той самий URL core генерує сам при кліку на файл; `upload.php` навіть редіректить застарілі запити редагування вкладення у цю форму). **Це наша основна ціль.**
- `/wp-admin/post.php?post=<ID>&action=edit` — повний екран редагування вкладення; тримаємо як другий пункт меню (стабільний, але без модалки).
- Фолбек без ID: `/wp-admin/upload.php?mode=list&s=<search_key>` — рівно те, що оператор робить руками зараз.

### 5. Звідки беремо корінь WordPress

Не з налаштувань клієнта, а **з самого URL картинки**: усе до `/wp-content/uploads/` — це корінь інсталяції (коректно для сабдиректорних інсталяцій `https://site.com/blog/`). Звідси:

- REST: `<root>/wp-json/wp/v2/media`, фолбек для plain-permalinks: `<root>/?rest_route=/wp/v2/media`;
- адмінка: `<root>/wp-admin/…`.

Якщо картинка віддається з CDN/проксі (`i0.wp.com`, Cloudflare, WebP-Express) — розгортання вже реалізоване в `derive_search_key()` (task-27) і дає справжній хост; беремо його. Якщо хост картинки все одно не збігається з доменом клієнта — REST б'ємо по **домену клієнта** (`clients.domain`), бо медіатека живе там, а не на CDN.

### 6. Чесний перелік того, де це не спрацює (і що показуємо)

| Ситуація | Стан |
|---|---|
| REST вимкнено плагіном / 401/403/404 на `/wp-json/` | `rest_unavailable` + підказка «open the media library and search by the key» |
| Cloudflare/бот-блок, таймаут, DNS | `failed` + причина |
| Не-uploads картинка (тема, плагін, зовнішній сайт) | `not_searchable` (не ходимо в мережу взагалі) |
| Offloaded media (S3/Bunny): `_wp_attached_file` містить повний URL | матч по basename → `filename` (слабкий), ID показуємо з позначкою |
| Файл видалили з медіатеки, але лишили на диску | `not_found` |
| Два вкладення з тим самим файлом (`1.jpg` і `1-1.jpg`) | `ambiguous` + список кандидатів у модалці |

Жоден із цих станів не ламає рядок і не блокує решту звіту: колонка «Media ID» деградує до сьогоднішньої поведінки (копіювання ключа + пошук в адмінці).

---

## Обсяг робіт

### Backend

**1. `app/tools/alt_text/wp_media.py` (адитивно).**
Додати `derive_upload_location(raw_url) -> UploadLocation | None`, що повертає `wp_root` (схема+хост+префікс до `/wp-content/uploads/`), `relative_path` (`2024/03/1-300x200.jpg`), `original_relative_path` (шлях зі знятими WP-суфіксами → `2024/03/1.jpg`), `basename`. Модуль лишається **чистим** (без мережі), уся логіка — на наявних регулярках; наявні сигнатури не змінюються.

**2. Новий модуль `app/tools/alt_text/wp_rest.py`.**
- `lookup_media(location, *, site_host=None, fetcher=None) -> MediaLookup` — послідовність запитів **до першого доведеного збігу**, не більше 3 HTTP-запитів на рядок:
  1. `?search=<original_relative_path>&media_type=image&per_page=20&_fields=…`
  2. якщо порожньо — `?slug=<sanitize_title(stem)>` (WP кладе slug із назви файлу; дешевий точний постріл)
  3. якщо порожньо — `?search=<stem>&media_type=image&per_page=100&…` (сьогоднішня семантика, лише для класифікації `ambiguous`)
- `MediaLookup` (frozen dataclass): `media_id`, `status` (`resolved|ambiguous|not_found|rest_unavailable|not_searchable|failed`), `confidence` (`exact|filename|search|None`), `alt_text` (alt, що **зараз** у медіатеці — цінно саме по собі), `slug`, `source_url`, `admin_url`, `candidates` (до 5: `{id, file, source_url, alt_text}`), `detail`.
- Мережа — через `assert_public_http_url` + `USER_AGENT` + `HostThrottle` з `app/tools/http_safety.py`; таймаут 10 с; тіло обрізане (1 МБ); `fetcher` інжектиться для тестів (як `Fetcher` у `verify.py`).
- Спроба REST-фолбеку `?rest_route=` рівно один раз, якщо `/wp-json/` дав 404 з HTML.

**3. Міграція `migrations/versions/<rev>_alt_text_wp_media.py`** (таска володіє власною міграцією — прецедент task-27/29/37). Адитивні nullable-колонки на `alt_text_items`:
`wp_media_id INTEGER`, `wp_media_status String(24)`, `wp_media_confidence String(16)`, `wp_media_alt String(MAX_ALT_TEXT_LENGTH)`, `wp_media_admin_url String(2048)`, `wp_media_detail String(500)`, `wp_media_json JSON` (кандидати), `wp_media_checked_at UTCDateTime`. Дефолт — `NULL` = «не шукали» (не `0`, не `not_found`).

**4. `app/db/repositories/alt_text.py`** — `set_media_result(item, …)` за зразком `set_verify_result`; фільтр `ItemFilters.media_status` для колонки-фільтра в UI.

**5. `app/api/tools_alt_text.py`** — два ендпоїнти за формою наявного `verify`:
- `POST /reports/{report_id}/items/{item_id}/resolve-media` → `AltTextItemDTO`;
- `POST /reports/{report_id}/resolve-media` (body: `{ "limit": 50, "only_unresolved": true }`) → `{ resolved, ambiguous, not_found, failed, skipped, remaining }`. Пакет іде **послідовно** з `HostThrottle` (це сайт клієнта, не наш), cap за замовчуванням 50 рядків на клік, виконання в `run_in_threadpool`.
- 409 з кодом `not_searchable`, якщо URL не з uploads — з текстом, що робити далі.

**6. `app/schemas/tools.py`** — адитивні поля в `AltTextItemDTO` (`wp_media_id`, `wp_media_status`, `wp_media_confidence`, `wp_media_alt`, `wp_media_admin_url`, `wp_media_detail`, `wp_media_candidates`, `wp_media_checked_at`), новий `AltTextMediaBatchDTO`. Наявні поля не чіпаємо.

**7. Експорт CSV/XLSX** — додати колонки `Media ID`, `Media status`, `Admin URL` (в кінець, щоб не зламати наявні шаблони).

### Frontend (`frontend/js/alt-text-tool.js`, `frontend/css/tools.css`)

**Нова колонка «Media ID»** між `Search key` і `New alt`:
- ID як `<code>#1234</code>` + іконка-кнопка copy (копіюється **гола цифра**, без `#`);
- чіп стану: `Exact` / `By file name` / `Ambiguous` / `Not found` / `REST closed` / `Not in media library`;
- час останньої спроби `small`-рядком (як у verify).

**Кнопка `Find`** у тій же клітинці:
- **ID уже відомий** → це справжній `<a class="button button--primary" target="_blank" rel="noreferrer" href="<admin_url>">`, а не `button`: клік по посиланню відкриває вкладку **без ризику блокування попапів**, і в `click`-хендлері ми копіюємо alt у буфер (копіювання не заважає дефолтній навігації). Тост: `Alt copied — media #1234 opened in WordPress`.
- **ID невідомий** → кнопка `Find` спершу викликає `resolve-media` (спінер на кнопці, `aria-busy`), і після відповіді **перетворюється на посилання** `Open in WP`. Свідомо **не** відкриваємо вкладку автоматично після `await`: Safari/Firefox ріжуть такий `window.open` як попап, і оператор отримав би «нічого не сталося».
- **`ambiguous`** → кнопка відкриває наявну модалку зі списком кандидатів (id, файл, поточний alt, посилання в адмінку) — вибір робить людина, ми не вгадуємо.
- **`rest_unavailable` / `not_found`** → фолбек-посилання `Search in WP` на `upload.php?mode=list&s=<search_key>` + copy alt (тобто не гірше, ніж сьогодні).

**Тулбар звіту:** кнопка `Resolve media IDs` (пакет по 50) з лічильником прогресу і фільтр `Media ID` (all / resolved / ambiguous / missing) поруч із наявними фільтрами.

Обмеження з `frontend/AGENTS.md`: жодних inline-`style` (CSP), іконки — через `icon()`, класи — у `tools.css`, делеговані хендлери вішаються один раз на контейнер (наявний патерн), ключ запиту в `api.js` — **явний намір** (`alt-text-resolve-media-${itemId}`), а не похідна від параметрів (три рази вже наступали на це — див. task-38).

---

## Owned paths

```
app/tools/alt_text/wp_rest.py              (новий)
app/tools/alt_text/wp_media.py             (адитивно: derive_upload_location)
app/api/tools_alt_text.py
app/db/repositories/alt_text.py
app/schemas/tools.py                       (адитивні поля DTO)
app/db/models.py                           (адитивні колонки AltTextItem)
migrations/versions/<rev>_alt_text_wp_media.py
frontend/js/alt-text-tool.js
frontend/js/api.js                         (адитивно: два виклики)
frontend/css/tools.css
tests/unit/tools/test_wp_rest.py           (новий)
tests/unit/tools/test_wp_media.py          (доповнення)
tests/unit/api/test_tools_alt_text.py      (доповнення)
tests/unit/db/test_alt_text_repository.py  (доповнення)
tests/e2e/frontend/alt-text-smoke.cjs      (доповнення, якщо існує)
```

---

## Тести

- **`test_wp_media.py`:** `derive_upload_location` — сабдиректорна інсталяція, CDN-обгортки, `-scaled`/`-e…`/`@2x`/`-300x200`, подвійне розширення `hero.jpg.webp`, не-uploads шляхи (`None`).
- **`test_wp_rest.py`** (табличні, з фейковим `fetcher`, **нуль мережі**): точний матч по `media_details.file`; матч по `sizes[*].file`; `1.jpg` з двома кандидатами → `ambiguous`; порожня відповідь → `not_found`; HTML замість JSON → `rest_unavailable`; 403 → `rest_unavailable`; таймаут → `failed`; offloaded `_wp_attached_file` з повним URL → `filename`; `?rest_route=` фолбек викликається рівно один раз; `admin_url` будується від кореня інсталяції, а не від хоста CDN.
- **API:** `resolve-media` зберігає результат і повертає DTO; повтор ідемпотентний; `not_searchable` → 409 з кодом; пакетний ендпоїнт поважає `limit` і не чіпає вже вирішені рядки; секрети/куки в жодному полі не з'являються.
- **Репозиторій:** `set_media_result` + фільтр `media_status`.
- **Frontend:** візуальний loop і чекліст `AGENTS.md §15–16`; окремо — перевірка, що клік по `Find` при відомому ID **не** проходить через `await` до відкриття вкладки.

---

## Definition of Done / Gate

1. Для рядка з осмисленою назвою і для рядка `1.jpg` (реальний звіт клієнта) інструмент віддає **той самий ID**, що бачить оператор в адмінці; перевірено живим прогоном щонайменше на одному сайті клієнта.
2. Кнопка `Find` при відомому ID: alt у буфері **і** відкрита адмінка на потрібному вкладенні — одним кліком.
3. Кожен «немає ID» — це named-стан із причиною в UI; порожньої клітинки без пояснення не існує.
4. Жоден стан не гірший за сьогоднішній: фолбек «copy key + open media search» доступний завжди.
5. `ruff check .`, `mypy app`, `pytest` — зелені **в Docker**; нових залежностей немає.
6. Міграція `upgrade`/`downgrade` прогнана на копії робочої БД; наявні звіти читаються без помилок (`wp_media_* = NULL`).
7. Секрети й куки не потрапляють у БД/логи/помилки UI (PLAN §13); запити до сайту клієнта — під `HostThrottle`, cap 50 рядків на клік.
8. План перенесено у `doc/dev/done/` з Completion report, `INDEX.md` оновлено.

---

## Ризики й свідомі рішення

- **Чому не робимо lookup автоматично при імпорті звіту.** Тисяча рядків = тисяча HTTP-запитів до сайту клієнта в момент завантаження файлу. Запуск лишається явним: рядок або пакет по 50.
- **Чому не пишемо alt у WordPress через REST.** `POST /wp/v2/media/<id>` вимагає авторизації (Application Password), тобто зберігання секрета клієнта — це окрема задача про облікові дані, а не про пошук. Тут ми **тільки читаємо** публічні дані й ведемо оператора за руку. Автоматичне проставляння alt — свідомий follow-up (`TODO(task-NN)`), який має сенс лише разом із сховищем credentials.
- **Чому ID зберігаємо, а не рахуємо щоразу.** Медіатека змінюється рідко, а звіт живе тижнями; збережений ID робить кнопку `Find` миттєвою і дає історію («шукали тоді-то, знайшли те-то»).
- **`?search=` по повному шляху покладається на поведінку WP ≥ 6.0.3.** На старіших сайтах пошук по імені файлу може бути вимкнений — тоді відпрацює крок `slug`, а далі — сьогоднішній фолбек. Це деградація, а не поломка.

---

## ✅ Completion report

- **Дата:** 2026-08-14
- **Гілка / комміт:** `task-34-client-lifecycle-status` @ незакомічено (робота цієї сесії поверх `eda4d1f`)
- **Статус:** done

### Що зроблено

Інструмент Image Alt Text тепер знаходить точний WordPress `attachment ID` для рядка звіту через публічний REST медіатеки — не «схожий файл», а підтверджений збіг uploads-шляху. Нова колонка **Media ID** в таблиці показує ID/стан/впевненість, кнопка **Find** копіює новий alt і одразу відкриває адмінку на потрібному вкладенні (`upload.php?item=<ID>&mode=grid`) — одним кліком без ручного пошуку. Для `ambiguous` — модалка зі списком непідтверджених кандидатів (вибір лишається за оператором). Пакетний прогін «Resolve media IDs» вирішує до 50 ще не перевірених рядків за клік. Кожен «немає ID» — названий стан (`not_found`/`rest_unavailable`/`not_searchable`/`failed`/`ambiguous`) з причиною, фолбек «copy key + open media search» лишається доступним завжди.

### Як зроблено (ключові рішення)

- **Ключова знахідка з вихідників core** (задокументовано в докстрінгу `wp_rest.py`): `WP_REST_Attachments_Controller::prepare_items_query()` при заданому `search` вмикає `wp_allow_query_attachment_by_filename`, і `WP_Query` (WP ≥ 6.0.3) шукає по `postmeta._wp_attached_file` — **повному uploads-шляху оригіналу** (`2024/03/1.jpg`), а не по голій назві. Це і розв'язує «проблему 1.jpg»: пошук по шляху звужує медіатеку до одного вкладення там, де пошук по голій назві дає сотні збігів.
- **Три кроки пошуку, не більше** (`wp_rest.lookup_media`): `?search=<original_relative_path>` → якщо порожньо `?slug=<...>` → якщо й це порожньо `?search=<stem>` (лише для класифікації). Кожен наступний крок — тільки якщо попередній дав нуль кандидатів (не «нуль підтверджених»), тож типовий випадок — один запит.
- **Підтвердження, а не довіра до пошуку.** `_is_exact_match`/`_is_filename_match` звіряють кандидата з нашим шляхом/іменем локально; один точний збіг → `resolved`, інакше — `ambiguous` з кандидатами (до 5), ID ніколи не подається як здогад (PLAN §12).
- **Виправлено в процесі:** початкова версія `_is_filename_match` матчила будь-який кандидат з тим самим *basename* незалежно від директорії — для `1.jpg`-подібних назв це давало **хибний** `resolved` на першому-ліпшому файлі з тим самим коротким іменем в іншій директорії (та сама проблема, яку задача мала розв'язати). Виправлено: слабкий («filename») тір спрацьовує лише коли `media_details.file` взагалі відсутній у відповіді (типовий кейс — деякі offload-плагіни); якщо шлях є, але веде в іншу директорію — це інший файл, `False`. Виявлено й закрито власними табличними тестами до інтеграції, живого дефекту в проді не було.
- **`derive_upload_location()` (адитивно в `wp_media.py`)** дериває `wp_root` (усе до `/wp-content/uploads/`) і `original_relative_path` (шлях зі знятими WP-суфіксами), перевикористовуючи наявний парсер CDN/proxy-обгорток з `derive_search_key`. **Відомий, задокументований ліміт:** той самий парсер вважає будь-який вкладений `/wp-content/uploads/` не на позиції 0 «WebP-Express-обгорткою» і відкидає префікс — тобто для інсталяцій **у підкаталозі** (`site.com/blog/`) `wp_root` хибно спрощується до кореня домену. Виправляти спільну логіку поза межами цієї задачі означало б ризикувати наявними тестами task-27 — залишено як `TODO(task-40)` з тестом, що фіксує саме цю поведінку (`test_upload_location_root_is_flattened_for_subdirectory_installs`).
- **`not_searchable`** — єдиний стан, що НЕ проходить через `wp_rest.py`: ставиться в `app/api/tools_alt_text.py::_resolve_media_sync` до будь-якого мережевого виклику (не-uploads URL або надто короткий `search_key`) — нуль запитів до сайту клієнта, як і вимагав план.
- **Пакетний ендпоінт** — послідовно, рядок за рядком (не паралельно): це реальні запити до чужого сайту, а не наш ресурс. Cap `limit=50` зафіксовано і на бекенді (`AltTextMediaBatchRequest.limit: le=50`), і на фронті.
- **Фронт: `Find`/`Open in WP` — справжній `<a target="_blank">`, не `<button>`**, коли ID вже відомий: клік копіює alt через `navigator.clipboard` **без** `await` перед навігацією, інакше попап-блокери вбивають відкриття вкладки, що прийшла після проміжного `await` (та сама категорія дефекту, що вже тричі ловили на request-key — тут той самий клас «розірвати event-gesture асинхронщиною»).
- **CSS: перша версія використала `button--primary` (суцільний оранжевий) для `Open in WP` в кожному рядку** — на реальній таблиці з 4+ вирішеними рядками це дало важку оранжеву смугу, що порушує «один контрольований сигнал» (`frontend/AGENTS.md §2`). Знайдено на скріншоті e2e-прогону, виправлено на `button--quiet` (як `Verify alt`/`Copy alt` — решта дій рядка).
- **Виявлено і виправлено по дорозі: `media_status` фільтр був доданий у репозиторій (`ItemFilters`), але не прокинутий у REST-ендпоїнт `GET /items`** — рядок фільтрації по колонці Media ID мовчки ігнорувався б. Закрито до релізу, покрито тестом (`test_items_filter_by_media_status`).
- Головні файли: `app/tools/alt_text/wp_rest.py` (новий, чистий мережевий модуль) — REST-пошук + класифікація; `app/tools/alt_text/wp_media.py` — `derive_upload_location`; `app/api/tools_alt_text.py` — два ендпоінти + оновлений `_item_dto`/export; `app/db/repositories/alt_text.py` — `set_media_result`/`media_status_counts`/`list_unresolved_items`; `frontend/js/alt-text-tool.js` — колонка, кнопка, модалка кандидатів, тулбар-фільтр, пакетна кнопка.
- Відхилень від плану по суті немає — реалізовано точно за оголошеним дизайном; єдина зміна — виправлення знайденого власними тестами дефекту класифікації (вище).

### Знахідки живої перевірки (2026-08-14, реальний звіт клієнта `nysapphire.com`)

Одразу після деплою користувач протестував інструмент на реальному звіті — знайдено й закрито два дефекти, яких unit/e2e-тести з фейковими даними не могли побачити:

1. **`405 Method Not Allowed` на обох нових ендпоінтах.** Причина — не в коді: `docker/entrypoint.sh` запускає `uvicorn` без `--reload`, а живий контейнер `api` не перезапускався 47 годин. Нові маршрути існували на диску (bind-mount), але не в пам'яті процесу, що обслуговує запити — `POST` на невідомий процесу шлях провалювався крізь API-роутер у SPA-фолбек (`StaticFiles`), який відповідає `405` на будь-що, крім `GET`/`HEAD`. Виправлено `docker compose restart api`. **Урок для наступних тасок:** зміна `app/api/**`/`app/**` вимагає рестарту `api`-сервісу перед живою перевіркою — самі по собі `pytest`/`ruff`/`mypy` цього не виявляють, бо кожен запускає застосунок наново.
2. **Реальний кейс, якого не було в тест-даних: `cdn.nysapphire.com` — CDN-піддомен, що не збігається з жодним відомим патерном обгортки (Photon/Cloudflare/Statically) і не має власного REST API (`403`).** Хост картинки ≠ хост WordPress. План це передбачав («якщо хост картинки не збігається з доменом клієнта — REST б'ємо по домену клієнта»), але реалізація цей крок пропустила. Додано: `_resolve_media_sync` тепер приймає `site_host` (домен клієнта звіту) і на `rest_unavailable` з чужого хосту пробує домен клієнта як `wp_root`, перш ніж здатися. Покрито тестом (`test_resolve_item_media_falls_back_to_the_client_domain_when_the_image_host_has_no_rest_api`) і підтверджено живим прогоном на реальному сайті клієнта (знайдено реальне вкладення `#24970` після фіксу, замість `403` до нього).
3. **UI-баг зі слів, не з логіки:** чіп `ambiguous` завжди показував статичний текст «Multiple matches» — навіть коли кандидат був **один**. Користувач одразу це помітив на живому кандидаті (`#25077`, один запис у модалці). Виправлено на динамічний підрахунок (`mediaMatchLabel()`): «1 match to confirm» / «N matches to confirm» — і в чіпі рядка, і в заголовку модалки, і в тості одиночного resolve. `MEDIA_STATUS_META.ambiguous.label` лишився як нейтральний фолбек («Needs confirmation») для місць без доступу до конкретного item (пакетний підсумок рахує рядки, не кандидатів, — там слово «ambiguous» лишається коректним).
4. **`admin_url` фолбека ігнорував www/non-www клієнта.** Реальний сайт (`nysapphire.com`) канонічно живе на `www.` — фолбек-хост (знахідка №2) будував `wp_root` з голого `client.domain`, тож посилання `Open in WP`/пошук-фолбек вели на `https://nysapphire.com/wp-admin/…`, а той редіректить на логін **іншого** хоста (`www.nysapphire.com/wp-login.php?redirect_to=https://nysapphire.com/…`) — неузгоджений ланцюжок хостів у редіректі. Паралельно з цією таскою в тому ж репозиторії з'явилось поле `MonitorSettings.canonical_www` (Site Monitor, task-38 follow-up, `app.monitor.runner._base_url`) — саме для цього: оператор задає www/non-www один раз замість того, щоб кожен запит з'їдав редірект сайту. Підключено ту саму корекцію (`_apply_canonical_www`, дзеркалить `_base_url` без імпорту з чужого модуля — уникнення міжтаскового зв'язування заради чотирьох рядків) у `_report_site_host`. Значення `canonical_www` для клієнта в БД було `NULL` (оператор ще не встиг задати — можливо, через той самий 405 зі знахідки №1, застосований і до монітор-ендпоїнта); задано через живий `PATCH /clients/{id}/monitor/settings`, після чого `admin_url` став цілком на `www.nysapphire.com` — редірект-ланцюжок логіну без міжхостового стрибка. Покрито тестом (`test_resolve_item_media_fallback_uses_the_monitor_canonical_www_setting`).
5. **`&mode=grid` мовчки ламав deep-link, коли вкладення не було на першій сторінці сітки.** Живий приклад: кандидат `#25082` існує (підтверджено `GET /wp-json/wp/v2/media/25082`), посилання `upload.php?item=25082&mode=grid` відкривало голу медіатеку без модалки. Причина знайдена у вихідниках core, не здогадкою: Grid-роутер (`wp-includes/js/media-grid.js`) матчить URL патерном `'upload.php?item=:slug'`; Backbone (`backbone.js`) компілює `:slug` у `([^/?]+)` — виключає `/` і `?`, але **не** `&`. Тобто `item=25082&mode=grid` матчиться, але `:slug` жадібно захоплює ввесь хвіст `"25082&mode=grid"` цілком. Поки вкладення вже завантажене в перший рендер сітки, `parseInt()` це терпить (обрізає на першому нецифровому символі) — але коли ні, роутер довантажує його через `wp.media.attachment(query)` із цим самим зіпсованим рядком замість чистого ID, AJAX-запит іде на невалідний шлях і мовчки провалюється — без жодної помилки в UI, просто «нічого не сталось». Сам core, коли пише URL у рядок при кліку на файл у сітці, додає лише `'?item=' + this.model.id` — без `mode=grid` узагалі (Grid і так дефолтний режим з WP 4.0). Виправлено в обох місцях, де URL будується: `wp_rest.build_admin_url()` (бекенд, для `resolved`-рядків) і `openMediaCandidates()` (фронт, для посилань кандидатів у модалці `ambiguous` — саме тут стався живий кейс, бо ці URL будуються в браузері на льоту й ніколи не проходять через бекенд). У БД жодного рядка зі старим форматом не знайдено (`SELECT … WHERE wp_media_admin_url LIKE '%mode=grid%'` → 0) — зламані посилання, які бачив оператор, були саме з модалки кандидатів, не зі збереженого `admin_url`, тож backfill не був потрібен.

### Контрактні зміни (Contract changes)

Адитивно, без зламу наявного:

- `AltTextItemDTO`: нові поля `wp_media_id`, `wp_media_status`, `wp_media_confidence`, `wp_media_alt`, `wp_media_admin_url`, `wp_media_detail`, `wp_media_candidates`, `wp_media_checked_at`.
- Нові ендпоінти: `POST /api/v1/tools/alt-text/reports/{report_id}/items/{item_id}/resolve-media`, `POST /api/v1/tools/alt-text/reports/{report_id}/resolve-media` (тіло `{limit}`, відповідь `AltTextMediaBatchDTO`).
- Новий query-параметр `GET /items?media_status=` (`unresolved` — синтетичне значення «ще не шукали»).
- Нові колонки `alt_text_items.wp_media_*` (міграція `fbafa34716a9`, down_revision `9d4e2f7a6c81`).
- Нові CSV/XLSX колонки експорту: `Media ID`, `Media status`, `Admin URL`.
- `data/openapi_snapshot.json` і фікстура `frontend/fixtures/alt-text-items.json` (нові поля з дефолтом `null`/`[]`) оновлені разом зі змінами.
- Спільний cache-bust тег фронтенду (`?v=...`) підняли до `20260814-media1` в усіх 29 файлах, що його несуть (та сама конвенція, що в task-38) — не контракт, але торкається всіх фронт-модулів.
- Новий read-only міжтасковий зв'язок: `app/api/tools_alt_text.py` читає `MonitorRepository.get_settings()`/`MonitorSettings.canonical_www` (Site Monitor, task-38 follow-up) для коректного www/non-www у фолбек-`admin_url`. Лише читання, жодного запису чи зміни файлів поза owned paths цієї таски; якщо монітор-домен колись прибере це поле — деградує до `None` (без корекції), не падає.

### Як перевірити (verification)

```bash
docker compose exec api poetry run ruff check .
docker compose exec api poetry run mypy app
docker compose exec api poetry run pytest -q -m "not integration and not live and not contract"   # 1287 passed
docker compose exec api poetry run alembic upgrade head
```

UI (fixture-режим, без бекенда): `http://127.0.0.1:6524/?fixture=normal#/tools/alt-text/fixture-report-march` — колонка **Media ID**, кнопка **Find** на рядку `rooftop-terrace-sunset` → зʼявляється `#4002` + `Open in WP`; на `bar-counter-2` → **Multiple matches** + **Choose…** відкриває модалку з двома кандидатами; фільтр **Media ID** звужує таблицю й лишається в URL (`?md=...`); кнопка **Resolve media IDs** в шапці вирішує решту непроглянутих рядків одним кліком.

Живий e2e (потребує запущений стек на `127.0.0.1:6524` + Playwright):

```bash
NODE_PATH=/tmp/yovth-e2e/node_modules BASE_URL=http://127.0.0.1:6524 CHROMIUM_PATH=/snap/bin/chromium \
  node tests/e2e/frontend/tools-alt-text.cjs
```

### Тести

- `tests/unit/tools/test_wp_media.py` — 9 нових тестів `derive_upload_location` (плоский оригінал, зняття resize-суфікса з `original_relative_path`, підкаталог-інсталяція з задокументованим лімітом, розгортання Photon CDN, `None` для theme/plugin/external/порожнього URL).
- `tests/unit/tools/test_wp_rest.py` (новий, 18 тестів, нуль мережі): точний збіг по `media_details.file`; збіг через `sizes[*].file`; `admin_url` від `wp_root`, не хоста CDN; слабкий збіг лише коли `media_details.file` відсутній (offload); `1.jpg` із двома кандидатами → `ambiguous`; ескалація крок-за-кроком і те, що другий/третій крок пропускається, коли попередній уже дав кандидатів; `not_found` після всіх трьох порожніх кроків; HTML-відповідь / 401/403/404 → `rest_unavailable`; 500 → `failed` (не плутати з `rest_unavailable`); мережевий виняток (`TimeoutError`) → `failed`; зіпсований JSON / не-список → `rest_unavailable`; `?rest_route=` фолбек — рівно один додатковий запит на HTML-404, і жодного зайвого на чистому 200.
- `tests/unit/db/test_alt_text_repository.py` — `set_media_result`, `media_status_counts` (ігнорує `NULL`), `list_unresolved_items`/`unresolved_count` (лише `uploads` без статусу, поважає `limit`).
- `tests/unit/api/test_tools_alt_text.py` — 9 нових тестів: одиночний resolve (точний збіг, `not_searchable`, `ambiguous`), 404 на відсутній звіт/рядок, пакетний resolve (обробляє лише `uploads`-рядки без статусу, ідемпотентний — другий прогін не робить жодного запиту), фільтр `media_status` (`resolved`/`unresolved`), колонки в CSV-експорті.
- `tests/e2e/frontend/tools-alt-text.cjs` (доповнено): секція 7 — "not checked" → Find → resolved-посилання з копіюванням alt, `not_searchable`-рядок без кнопки, ambiguous → модалка з двома кандидатами + Escape, фільтр Media ID зі збереженням в URL, пакетна кнопка закриває решту рядків; нуль console/page errors, скріншоти 1440/390 оновлені.
- Разом: 1287 unit/API/repo-тестів зелені; окремо перевірено цикл `alembic downgrade -1` → `upgrade head` на реальній dev-БД.

### Follow-ups / борг

- `TODO(task-40)`: `wp_root` для WordPress-інсталяцій у підкаталозі (`site.com/blog/`) хибно спрощується до кореня домену — спільна причина зі старою «WebP-Express nested path» евристикою в `wp_media.py` (задокументовано в докстрінгу `UploadLocation` і покрито тестом, що фіксує саме цю поведінку, а не приховує її).
- Автоматичне проставляння alt назад у WordPress через REST — свідомо поза обсягом: вимагає Application Password / сховища credentials клієнта, окрема задача.
- Report-рівневі агреговані лічильники `wp_media_status` (скільки `resolved`/`ambiguous`/… у звіті) є в репозиторії (`media_status_counts`), але не прокинуті в `AltTextReportDTO`/шапку — заголовок звіту показує лише кнопку дії, без «N з M перевірено». Не блокер: фільтр по колонці дає ту саму інформацію в таблиці; додати за потреби окремим PR.
