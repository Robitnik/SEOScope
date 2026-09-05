# task-64 — URL Inspector v2: скріншоти (desktop/tablet/mobile), запити/ключі, двоколонковий layout

**Depends on:** 61 ✅ (URL Inspector), 16 ✅ (GSC dimensions), 57 ✅ (Page Inventory), 58 ✅ (monitor crawl), 59/60 ✅ (Web Archive, body-блоби), 53 ✅ (contentqa policy bridge)

**Owned paths:**
- `app/urlshots/**` (новий пакет: рендер і зберігання скріншотів)
- `app/urlkeywords/**` (новий пакет: ключі/запити для URL)
- `app/urlinspector/sources/screenshots.py`, `app/urlinspector/sources/keywords.py`
- `app/api/url_screenshots.py`
- `app/schemas/url_screenshots.py`; адитивні поля в `app/schemas/url_inspector.py`
- `frontend/js/url-inspector.js`, `frontend/css/url-inspector.css`
- `tests/unit/urlshots/**`, `tests/unit/urlkeywords/**`, `tests/unit/api/test_url_screenshots_api.py`
- адитивні дотики: `frontend/js/api.js`, `frontend/index.html` (`?v=` бампи), `migrations/` (одна таблиця, inherited zone — див. §6)

**Не чіпаємо:** `app/webarchive/**` (task-60/62), `app/db/repositories/{monitor,dimensions}.py` (read-only), `app/reports/ninja_api.py`.

---

## 0. Результати дослідження (виміряно наживо, а не припущено)

Це визначило весь дизайн нижче. Кожен пункт перевірено в контейнері на бойових даних.

### 0.1 Скріншоти в контейнері працюють — заміряно

Спайк через наявний `app.reports.stealth_browser.new_stealth_context` (`hearwellservices.com`):

| Виміряно | Значення |
|---|---|
| 3 вьюпорти, холодний старт браузера | **19.3 с** (desktop 10.5 + tablet 3.8 + mobile 4.6) |
| PNG, лише вьюпорт | 533 КБ |
| **JPEG q80, лише вьюпорт** | **120 КБ, 0.1 с** |
| **JPEG q80, full-page** | **724 КБ, 0.3 с** |
| Pillow у контейнері | **12.3.0** є (тумбнейли без нових залежностей) |

Висновок: PNG відкидаємо (×4.4 ваги без користі), беремо JPEG. Основний час — не сам знімок (0.1–0.3 с), а старт браузера + `goto` + очікування. Тому **один запуск Chromium на всі три вьюпорти**, як `BrowserFetchSession` у task-50, а не три окремі.

### 0.2 Per-URL запити з GSC **недоступні** через Reporting Ninja — доведено двома пробами

Це головна знахідка. Спершу перевірено крос-вимір:

```
fields=["page","query","clicks","impressions"]
→ APIError INTERNAL: You can only use one dimension at a time.
```

Потім — чи можна фільтрувати за сторінкою, лишивши вимір `query`. `settings.dimensionFilterGroups` відхиляється (`allowed: search_type`), `filters:[{field:"page",operator:...}]` відхиляє **будь-який** оператор (`contains/equal/is/=/in/exact/matches/startsWith/begins_with/like`). А форма `{"filter":{"page": ...}}` повертає `200 OK` — і **мовчки ігнорується**:

```
без фільтра    : ['sapphire nyc', 'sapphire 39', 'anissa kate escort', ...]
фільтр реальний: ['sapphire nyc', 'sapphire 39', 'anissa kate escort', ...]   # ідентично
фільтр-нонсенс : ['sapphire nyc', 'sapphire 39', 'anissa kate escort', ...]   # ідентично
```

Однакова відповідь на реальну і на неіснуючу сторінку — це не фільтр, це тихий no-op. **Якби ми повірили 200-ці, панель «ключі цієї сторінки» показувала б топ запитів усього сайту під виглядом сторінкових.** Саме тому §3 нижче ніде не називає ці дані сторінковими.

Наслідок: чесних per-URL GSC-запитів у нас немає і не буде без окремої інтеграції (§7 follow-up).

### 0.3 Що по запитах у нас **реально є**

`gsc_dimension_metrics` заповнена щедро — але `page` і `query` це **два незалежні зрізи**, без перетину:

| Клієнт | page-рядків | query-рядків |
|---|---|---|
| SAPHNY | 19 240 | 65 065 |
| SHB | 8 683 | 72 072 |
| HWELL | 15 543 | 62 062 |
| GRP | 1 957 | 62 062 |

Тобто топ-запити сайту — це справжні дані, просто **сайтового**, а не сторінкового рівня.

### 0.4 Текст сторінки для видобутку ключів у нас є

`MonitorPageVersion.body_blob_sha256` зберігає сирий HTML версії, `app.monitor.page.extract_signals` дає `visible_text` + `word_count`. Плюс `InventoryRow.keywords` (meta keywords) з task-57. Тобто on-page ключі рахуються **зі збережених даних, без жодного мережевого запиту**.

Токенізація/класифікація не пишеться з нуля: в `AISeoTools/seo_audit/policy.py` вже є `words()`, `key_tokens()`, `is_brand_query()`, `is_targetable_keyword()`, `keyword_in_title()`, `keyword_stuffing_terms()`, а в репо вже є усталений міст `app/contentqa/policy_bridge.py` (task-53) — імпортуємо, не переписуємо.

### 0.5 Скріншоти в архіві не зберігаються

`ArchiveSnapshot` має `body_blob_sha256`, `title`, `word_count` — і **жодного** поля під зображення. Тобто це справді нова можливість, а не дублювання task-59/60.

---

## 1. Мета

Три речі, яких бракує сторінці `#/url?u=…`:

1. **Побачити сторінку**, а не лише її метрики — знімок у desktop / tablet / mobile.
2. **Запити і ключі** — чесно розділені на «дані GSC» і «видобуте з самої сторінки».
3. **Двоколонковий layout** — зараз усе одним стовпчиком, і на 1440px половина ширини порожня.

---

## 2. Скріншоти (`app/urlshots/`)

### 2.1 Вьюпорти

| Пресет | Розмір | Прапорці |
|---|---|---|
| `desktop` | 1440×900 | — |
| `tablet` | 834×1112 | `is_mobile=True`, DSF 2 |
| `mobile` | 390×844 | `is_mobile=True`, `has_touch=True`, DSF 2 |

`390` і `1440` — рівно ті ширини, що вимагає `AGENTS.md §11`, тож знімки водночас є артефактом візуальної перевірки самого клієнтського сайту.

### 2.2 Захоплення

- `app/urlshots/capture.py::capture_screenshots(url, presets) -> ShotSetResult`.
- **Один** `chromium.launch()` на весь набір, окремий `BrowserContext` на пресет (щоб `is_mobile`/DSF справді застосувались — вони context-level, не page-level).
- `full_page=True`, JPEG `quality=82`. Плюс `fold_height` (висота вьюпорта) в метаданих, щоб UI малював лінію згину.
- Пер-пресет ізоляція: падіння одного вьюпорта → його власний `failed`, інші два зберігаються (PLAN §9.1).
- Ліміт повної висоти: `full_page` на нескінченному скролі може дати мегабайти — кап `max_full_height=12000` px, при перевищенні ріжемо через `clip` і ставимо `truncated=True` (чесна відсутність, не тиха обрізка).
- URL проходить `assert_public_http_url` з `app/tools/http_safety.py` — SSRF-захист уже написаний і використовується монітором.

### 2.3 Зберігання — **власний каталог, не спільний blob-root**

Файли: `settings.artifacts_dir / "urlshots" / <sha[:2]> / <sha>.jpg`, content-addressed.

Свідомо **не** переиспользуємо `app/webarchive/storage.py::store_blob`: він кладе у спільний `blobs/`-root, який підмітає `app/cloud/sweeper.py` за таблицями `monitor_blobs`/`archive_*`. Скріншот, що лежить у тому root без рядка в цих таблицях, — це рівно та форма «непосиланого блоба», яку свіпер створений видаляти. Окремий каталог робить цю помилку неможливою за конструкцією, ціною ~30 рядків власного резолвера (з тією ж перевіркою виходу за корінь, що в `resolve_blob_path`).

Тумбнейл через Pillow (ширина 480, JPEG q70) — для картки в гріді, щоб не тягнути 700 КБ у список.

### 2.4 Персистенція

Одна нова таблиця `url_screenshots`:

```
id, client_id (nullable), inspector_key, url, preset,
blob_sha256, thumb_sha256, width, height, fold_height,
byte_size, truncated, http_status, final_url,
state ('ok'|'failed'), error_code, captured_at
```

Історія знімків зберігається (не перезаписуємо) — тоді «як сторінка виглядала місяць тому» стає безкоштовним побічним ефектом, у парі з таймлайном архіву.

### 2.5 API

- `POST /api/v1/url-screenshots` `{url, presets?, force?}` → рендерить і повертає набір. Синхронно через `run_in_threadpool`, як уже зроблено для `/url-inspector/live` (~20 с — той самий порядок, що живий аудит).
- `GET /api/v1/url-screenshots?url=…` → останній набір із кешу, миттєво.
- `GET /api/v1/url-screenshots/{id}/image` і `/thumb` → віддача JPEG з `Cache-Control: immutable` (content-addressed, тому безпечно).

Кеш: якщо набір свіжіший за 24 год і `force` не заданий — віддаємо збережений, не рендеримо.

---

## 3. Запити і ключі (`app/urlkeywords/`)

Панель ділиться на **три блоки з різними рівнями достовірності**, і кожен підписаний прямо в UI. Це не педантизм: §0.2 показує, як легко тут випадково збрехати.

### 3.1 Блок «This page in Search» — **факт**
Вже наявні GSC page-метрики цієї URL (clicks/impressions/CTR/position) + дельта до попереднього періоду. Джерело: `gsc_dimension_metrics`, `dimension='page'`.

### 3.2 Блок «On-page keywords» — **факт (з контенту сторінки)**
Видобуто зі збереженого HTML останньої версії (`MonitorPageVersion.body_blob_sha256` → `extract_signals`):
- топ-N термів і біграм за частотою, зі стоп-словами і `policy.words()`;
- позначка, чи термін є в `title` / `h1` / `description` (`keyword_in_title`);
- `meta keywords`, якщо є (`InventoryRow.keywords`);
- сигнал перенасичення через `keyword_stuffing_terms`.

Стан `no_data`, якщо сторінку ще не краулили — **не** порожній список (PLAN §12: `None` ≠ `0`).

### 3.3 Блок «Site queries this page may serve» — **явно помічена евристика**
Топ-запити сайту (`dimension='query'`) × покриття токенами цієї сторінки:
- `coverage = |key_tokens(query) ∩ page_tokens| / |key_tokens(query)|`, показуємо лише `coverage == 1.0` (усі значущі токени запиту присутні на сторінці), сортуємо за impressions;
- бренд/небренд через `is_brand_query(query, brand_name, domain)`;
- у шапці блока — постійний підпис: **"Site-level GSC queries matched to this page's text. Google does not expose per-page queries through this integration."**

Це і корисно, і не бреше. Жодне число тут не подається як «запити цієї сторінки».

### 3.4 Чого свідомо **не** робимо
Не вигадуємо per-URL clicks по запиту розподілом сайтових чисел пропорційно чомусь. Такий розподіл виглядав би як дані і не був би ними.

---

## 4. UI: двоколонковий layout

`.url-inspector__body` → CSS Grid:

```
@media (min-width: 1200px)  → grid-template-columns: minmax(0, 1fr) 360px
@media (max-width: 1199px)  → один стовпчик, сайдбар після основного
```

| Основна колонка | Сайдбар (sticky) |
|---|---|
| Search analytics (GSC/GA4 графіки) | Identity + матриця покриття |
| **Keywords & queries** (§3) | **Live audit** |
| **Screenshots** (§2) | Monitor facts |
| Archive timeline | Meta + QA |
| Monitor changes feed | Tech audit |

- Сайдбар `position: sticky; top: …; max-height: calc(100vh - …); overflow-y: auto` — але **лише** з `min-width: 1200px`; на вузькому він звичайний потік.
- Grid-діти отримують `min-width: 0` (AGENTS §11), інакше графіки і довгі URL розіпнуть колонку.
- Скріншоти: три картки-тумбнейли в ряд → клік відкриває повнорозмірний перегляд (нативний `<dialog>`, не саморобний оверлей) з перемикачем пресетів.
- **CSP:** жодних інлайн-стилів і `.style.x = …` — тільки `classList` (memory `frontend_verification_tooling`, обпікалися двічі).
- **Кеш:** бампнути `?v=` на **всіх** змінених файлах, включно з plain `<script src>` у `index.html` (memory `cloudflare_static_asset_cache_bug`).

---

## 5. Ізоляція джерел

`screenshots` і `keywords` додаються в `inspect_url()` тим самим try/except-обгортанням, що й решта шести — падіння будь-якого не валить сторінку. Скріншоти в `GET` **не рендеряться** ніколи: у DTO йде лише кешований набір або `state='not_captured'`, інакше кожне відкриття сторінки коштувало б 20 с.

## 6. Міграція (inherited zone)

Одна `op.create_table("url_screenshots")` + індекси. Прямий `create_table`, **без** `batch_alter_table` — саме він стер `client_integrations` на проді 2026-09-01 (CLAUDE.md §4). Створення нової таблиці цього класу ризику не має. Бекап перед прогоном — обов'язковий, за процедурою з CLAUDE.md.

Прецедент inherited zone: task-16 так само взяв володіння рівно однією таблицею (`gsc_dimension_metrics`) у task-03-овому файлі.

## 7. Follow-ups

- **`TODO(task-65)`: справжні per-URL GSC-запити.** Єдиний шлях — браузерний експорт із самого GSC UI з фільтром по сторінці (`AISeoTools/seo_audit/browser/providers/gsc.py::collect_query_pages` + `_page_filter_fragment`). Не сюди, бо: параметр фільтра там помічений `LIVE-CONFIRM` (не підтверджений наживо), потрібен залогінений профіль `gsc`, і це хвилини на URL. Коли підтвердиться — блок §3.3 замінюється фактом, а не доповнюється.
- Скріншот-дифи між знімками (візуальна регресія) — окремо, після накопичення історії.
- Прибирання старих скріншотів за віком у `app/cloud/sweeper.py` — не наш owned path, запит до власника.

## 8. Definition of Done / Gate

1. `POST /url-screenshots` віддає 3 пресети для реального клієнтського URL; падіння одного пресета лишає два.
2. Повторний `GET` бере з кешу без запуску браузера (перевірити за часом відповіді).
3. Зображення віддаються, тумбнейли менші за оригінал, шлях не виходить за корінь артефактів (тест на `../`).
4. Keywords-панель на клієнті **зі** зібраним контентом показує терми; на URL **без** краулу — `no_data`, не порожній список.
5. Блок §3.3 у DOM містить дисклеймер про сайтовий рівень (перевіряється e2e-асертом, не оком).
6. Layout: 390 / 768 / 1024 / 1440 / ultra-wide — нуль горизонтального скролу; сайдбар sticky лише ≥1200.
7. Нуль CSP-помилок у консолі; нуль інлайн-стилів.
8. `ruff`, `mypy app`, `pytest tests/unit/urlshots tests/unit/urlkeywords tests/unit/api` — зелені.
9. Живий прогін через реальний тунель `seo.theyovth.space`, не лише localhost.
10. Бекап БД зроблено перед міграцією.

---

## ✅ Completion report

- **Дата:** 2026-09-05
- **Гілка / комміт:** main (робота в спільному робочому дереві — див. §«Follow-ups» нижче про причину, чому частина спільних файлів лишається незакомічена)
- **Статус:** done-with-followups

### Що зроблено

Усі три частини з плану — робочі й перевірені наживо на реальних клієнтських даних:

1. **Скріншоти** (desktop 1440×900 / tablet 834×1112 / mobile 390×844): `POST /api/v1/url-screenshots` рендерить усі три пресети одним запуском Chromium (~15–20с на живому клієнтському сайті, заміряно), зберігає JPEG-и + тумбнейли у власному content-addressed сховищі, персистить у нову таблицю `url_screenshots`. `GET` того самого шляху читає лише кеш (24 год TTL) — миттєво. Галерея на сторінці клікабельна, відкриває повнорозмірний перегляд у нативному `<dialog>`.
2. **Ключі й запити**: панель «Keywords & queries» з двома чесно розділеними блоками — терми зі збереженого HTML сторінки (факт) і сайтові GSC-запити, зіставлені з текстом сторінки за повним покриттям токенів (**явно позначена евристика**, з постійним дисклеймером у DOM).
3. **Двоколонковий layout**: `.url-inspector__body` — CSS grid `1fr + 360px` від 1200px, sticky-сайдбар (Live audit / Meta & copy / Technical audit), основна колонка (Search analytics / Keywords / Screenshots / Web Archive / Site Monitor).

### Як зроблено (ключові рішення)

- **Reporting Ninja не дає per-URL запитів.** Перевірено наживо двома пробами (документовано в §0.2): крос-вимір `page`+`query` відхиляється API прямо; фільтр `{"filter":{"page":...}}` повертає `200 OK` з **ідентичними** рядками для реальної й неіснуючої сторінки — тихий no-op, не робочий фільтр. Тому «Site queries» — це топ запитів САЙТУ, зіставлені з текстом сторінки, а не дані per-URL; кожен рядок DTO (`UrlKeywordsDTO.queries_state`) і сам UI несуть це розрізнення явно.
- **Токенізація** — не винайдена заново: `app/urlkeywords/policy_shim.py` (власний lazy-seam, окремий від `app.contentqa.policy_bridge`, щоб не чіпати файл task-53) обгортає `seo_audit.policy.topic_tokens`/`is_brand_query`. Фрази — суміжні пари вже відфільтрованих токенів (документована спрощена модель, не справжні позиційні n-грами).
- **Скріншоти — окремий blob-корінь** (`artifacts_dir/urlshots/`), НЕ спільний `monitor_blobs`/`archive_*`: `app.cloud.sweeper` підмітає блоби без рядка у СВОЇХ таблицях, і скріншот у спільному корені без такого рядка був би рівно кандидатом на видалення.
- **Один запуск Chromium на всі 3 пресети** (виміряно: старт браузера домінує, сам знімок 0.1–0.3с) — та сама логіка, що `BrowserFetchSession` (task-50). SSRF-гард на кожній навігації (включно з редиректами), не лише на вхідному URL.
- **Головні файли:**
  - `app/urlshots/{capture,storage,service}.py` — захоплення, сховище, оркестрація кеш/капчур.
  - `app/urlkeywords/{policy_shim,onpage,sitequeries}.py` — токенізація, on-page терми, зіставлення запитів.
  - `app/db/repositories/url_screenshots.py`, `app/urlinspector/sources/{keywords,screenshots}.py`.
  - `migrations/versions/59d3e7ceaec2_url_screenshots.py` — нова таблиця (прямий `create_table`, без `batch_alter_table`).
  - `frontend/js/url-inspector.js` — `renderKeywords`/`renderScreenshots`/dialog-логіка; двоколонковий рендер у `render()`.
  - `frontend/css/url-inspector.css` — grid-layout, галерея скріншотів, стилі діалогу.
- **Відхилення від плану:** сайдбар не дублює «Identity + coverage matrix» (той рядок з плану §4) — вона вже є повношириним header'ом над колонками, дублювати нема сенсу. Monitor (facts + changes + versions) залишено ЦІЛІСНИМ у головній колонці, а не розділено на «facts» (сайдбар) / «changes feed» (main) — розбиття однієї когерентної функції рендеру заради одного рядка таблиці плану визнано зайвим ризиком.

### Контрактні зміни (Contract changes)

Адитивні поля в `UrlInspectorDTO`: `keywords: UrlKeywordsDTO`, `screenshots: UrlScreenshotsStateDTO` — нові DTO, нічого існуючого не змінено. Нові ендпоінти `GET/POST /api/v1/url-screenshots`, `GET /api/v1/url-screenshots/{id}/{image,thumb}`. `tests/unit/api/data/openapi_snapshot.json` перегенеровано (заразом підхопив і давно неоновлені записи `/api/v1/url-inspector*` та `/archive/view/snapshot/...` — знімок був застарілим ще до цієї таски, не через неї).

### Як перевірити (verification)

```bash
docker compose exec api poetry run pytest tests/unit/urlkeywords tests/unit/urlshots tests/unit/urlinspector tests/unit/api/test_url_inspector_api.py tests/unit/api/test_url_screenshots_api.py tests/unit/api/test_openapi_snapshot.py -q
docker compose exec api poetry run ruff check app/urlkeywords app/urlshots app/urlinspector app/db/repositories/url_screenshots.py app/api/url_screenshots.py app/api/url_inspector.py app/schemas
docker compose exec api poetry run mypy app/urlkeywords app/urlshots app/urlinspector app/db/repositories/url_screenshots.py app/api/url_screenshots.py app/schemas/url_inspector.py app/schemas/url_screenshots.py
NODE_PATH=/tmp/yovth-e2e/node_modules BASE_URL=http://localhost:6524 CHROMIUM_PATH=/snap/bin/chromium node tests/e2e/frontend/url-inspector.cjs
```
У UI: `#/url?u=<будь-який клієнтський URL>` → секція «Screenshots» → «Capture screenshots» (~15–20с, реальний Chromium) → три картки з тумбнейлами, клік відкриває повний розмір. Секція «Keywords & queries» — терми з тексту сторінки + сайтові запити з явним дисклеймером.

### Тести

- `tests/unit/urlkeywords/` (44 тести): токенізація/фрази (`test_onpage.py`), покриття запитів + сортування + дедуп + бренд-прапорець (`test_sitequeries.py`), lazy-seam graceful degradation (`test_policy_shim.py`), повна інтеграція `collect_keywords` на реальній SQLite-схемі з gzip-блобом і GSC-знімком (`test_source_keywords.py`).
- `tests/unit/urlshots/` (27 тестів): content-addressed store + root-escape guard, фейковий Playwright для `capture_screenshots` (per-preset ізоляція, height-cap/truncation, SSRF fast-fail, timeout-мапінг), репозиторій (`latest_set`/`is_fresh`), сервісний кеш/force/persist-failed-shot цикл.
- `tests/unit/api/test_url_screenshots_api.py` (7 тестів): capture→serve→cache-hit цикл через реальний `TestClient`, SSRF → 400 не 500, 404 на невідомий id.
- `tests/unit/urlinspector/test_sources_isolation.py` розширено: `keywords`/`site_queries`/`screenshots` додано до ізоляційної матриці (падіння одного джерела не валить інші).
- `tests/e2e/frontend/url-inspector.cjs` розширено: 9 coverage-чіпів, двоколонковий layout на 1440px, дисклеймер запитів у DOM, повний живий цикл захоплення скріншотів (реальний Chromium, ~20с) + відкриття/закриття `<dialog>`. Прогнано локально і через реальний тунель `seo.theyovth.space`.
- Повний бекенд-набір: `2378 passed, 2 pre-existing failures unrelated to this task` (сигнатурний дрейф сусіднього чекауту AISeoTools і час-залежний dashboard-staleness тест — жоден не торкається файлів цієї таски).

### Follow-ups / борг

- `TODO(task-65)`: справжні per-URL GSC-запити через браузерний експорт GSC UI з фільтром по сторінці (`app.py`'s `collect_query_pages`/`_page_filter_fragment`, обидва позначені `LIVE-CONFIRM`, не підтверджені наживо).
- Видалення застарілих скріншотів за віком у `app/cloud/sweeper.py` — не owned path цієї таски, потрібен запит до власника.
- Скріншот-дифи між знімками (візуальна регресія) — після накопичення історії.
- **Спільні файли лишаються незакомічені по встановленому прецеденту** (`frontend/index.html`, `frontend/js/{router,api}.js`, `app/db/models.py`, `tests/unit/api/data/openapi_snapshot.json`): git-статус на старті сесії вже показував перші три модифікованими іншими паралельними сесіями (task-63 та інші), а `app/db/models.py` вже ніс ~500 рядків чужих несуміжних доповнень (task-56 `SchemaMarkupCheck`, task-57 `PageInventorySnapshot`/`Item`, `Client.is_dev`) в одному безперервному diff-хунку з моїм `UrlScreenshot` — розділити по хунках без ризику зачепити чуже не вдалось (усі додані «в кінець файлу» одним блоком). Мої зміни в цих файлах — суто адитивні (два bump `?v=` рядки в `router.js`, дві нові `<link>`/`<script>` версії в `index.html`, дві нові функції в `api.js`, один клас моделі в кінці `models.py`) і перевірені живим Playwright-прогоном (localhost + реальний тунель), але закомічувати спільний файл із чужими незакомодженими правками означало б підхопити чужу неревʼюжену роботу в свій комміт. `openapi_snapshot.json` перегенерований бачить і чужі незакомічені роути (task-56/57/63) — тому теж не комітиться. **Наслідок:** щойно скомічені файли цієї таски (репозиторій, сервіс, API-роутер) імпортують `UrlScreenshot` з `app/db/models.py`, якого в **закомодженій** версії файлу ще нема — код працює в цьому спільному робочому дереві (модель фізично на диску), але не пройде на «чистому» чекауті самого коміту, доки хтось не замерджить/не закомітить `models.py`. Той самий компроміс, що вже задокументували task-59/60/62 для своїх спільних файлів.
