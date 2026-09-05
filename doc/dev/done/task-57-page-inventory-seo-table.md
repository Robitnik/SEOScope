# task-57 — Page Inventory: таблиця сторінок клієнта (Pages / URL / Breadcrumbs / Title / Description / Keywords) + вкладка per-field валідації

- **Пакет:** нова під-сторінка клієнта `#/client/<id>/pages` із **двома вкладками**. Вкладка 1 — **Inventory**: повна архітектура сайту одним аркушем (ім'я сторінки, URL, хлібні крихти, Title, Description, Keywords) — рівно та таблиця, яку оператор сьогодні збирає руками в Google Sheets. Вкладка 2 — **SEO QA**: ті самі рядки й ті самі поля, але **кожне поле** несе власний вердикт (SEO-правила + AI-слоп + brand voice) із рушія task-53. Плюс XLSX-експорт на два аркуші — файл, який іде клієнту.
- **Хвиля:** S — Content Inventory (перша таска хвилі; після хвилі P — Tools).
- **Depends on:** **35/36** ✅ (краул сторінок і `monitor_blobs` — джерело даних), **53** ✅ (рушій `app/contentqa` — джерело правил), **06** ✅ (REST), **54** ✅ (патерн «під-сторінка клієнта + власний роутер + XLSX»), **13** ✅ (harness)
- **Не конфліктує:**
  - `app/monitor/**` (task-35/50/51) — **нічого не редагуємо**, лише читаємо `monitor_pages` / `monitor_page_versions` / `monitor_blobs`. Особливо: **не чіпаємо `build_signals()`** — додавання поля туди змінило б `signals_sha256` кожної сторінки портфоліо й породило б лавину фальшивих `page_changed` (див. §2.2).
  - `app/contentqa/**` (task-53) — read-only споживач `run_checks()`/`Profile`. Калібрування живе в **нашому** модулі, не в їхніх правилах.
  - task-56 (Schema Markup Checker) — теж читає `monitor_blobs` тільки на читання; перетину по файлах немає. Спільний JSON-LD-екстрактор — свідомий **follow-up**, не залежність (§11).
  - ⚠️ **Паралельна сесія активна** (перевірено 2026-09-04, `git status` нечистий): `app/db/models.py`, `app/api/monitor.py`, `app/monitor/runner.py`, `frontend/js/{api,client-page,client-card,client-status,monitor-client}.js`, `frontend/css/app.css`, `doc/dev/new/INDEX.md`. З них наші адитивні дотики — `app/db/models.py` (+2 моделі **в кінець файлу**), `frontend/js/{api,client-page,router}.js`. Диффи піра локальні (`Client.is_dev`, robots/sitemap-check) і далеко від місць вставки, але **перед торканням цих файлів звір `git status`** (пам'ять: `multi-agent-collision-avoidance`).
- **Owned paths:** `app/pageinventory/**`, `app/api/client_pages.py`, `app/schemas/page_inventory.py`, `app/db/repositories/page_inventory.py`, `migrations/versions/*_page_inventory.py`, `frontend/js/{page-inventory-page,page-inventory-table,page-inventory-qa}.js`, `frontend/css/page-inventory.css`, `tests/unit/pageinventory/**`, `tests/unit/api/test_client_pages_api.py`
- **Адитивні дотики (узгодити):** `app/db/models.py` (+2 моделі `PageInventoryItem`, `PageInventorySnapshot` — у кінець файлу), `frontend/js/router.js` (+1 маршрут), `frontend/js/client-page.js` (+1 картка-вхід), `frontend/js/api.js` (+4 функції, у кінець), `frontend/index.html` (+1 `<main>`-root, +1 `<link>`)
- **PLAN.md:** §12 (чесні стани: «тег відсутній» ≠ «тег порожній» ≠ «не перевіряли»), §9.1 (ізоляція клітинки), §13 (жодних секретів)
- **frontend/AGENTS.md:** §5 (правда даних), §6 (обов'язкові стани), §11 (широка таблиця), §12 (CSP: жодного inline-`style`), §15–16 (візуальний loop + чекліст)
- **AISeoTools:** змін **не потребує** — `seo_audit.policy` доступний через наявний `app/contentqa/policy_bridge.py`.
- **Міграція:** створює ця таска (живий прецедент: кожна фіча-таска несе свою міграцію — task-27/29/50/51/52/53). Через прямий `op.create_table`, **без `batch_alter_table`** (CLAUDE.md, інцидент 2026-09-01).

---

## Мета (робота оператора, яку закриваємо)

Таблицю архітектури сайту (скріншот користувача: `Pages | URL | Breadcrumbs | …`) оператор сьогодні набиває вручну: обходить сайт, копіює хлібні крихти, витягує Title/Description по одній сторінці. На клієнті ALPHA це **443 унікальні сторінки**. Далі цей файл ніхто не перевіряє на SEO-правила взагалі — а на живому зрізі (§1.4) там 23 кластери дубльованих описів, 55 титулів понад ліміт і 44 описи з забороненим опенером.

Ціль: та сама таблиця будується за **2.6 секунди** з даних, які монітор уже зібрав, і одразу приходить із вердиктом по кожному полю.

---

## 1. Дослідження (усе виміряно на живій БД 2026-09-04, клієнт ALPHA `d182b771…`, `alphastyle.store`)

### 1.1 Дані вже зібрані — новий краул не потрібен

`monitor_pages` уже тримає рівно 4 з 6 потрібних колонок (`url`, `title`, `description`, плюс `h1`, `canonical`, `status`, `indexable`). Заміряно:

| Показник | Значення |
|---|---|
| Сторінок клієнта в моніторі | 742 (`200` — 655, `404` — 39, ще не перевірено — 48) |
| З title / description | 693 / 455 |
| Останній повний прохід | 2026-09-04 04:48 |
| Сторінок портфоліо з хоча б одним збереженим тілом | **3626 / 3648** |
| Сторінок ALPHA (200) зі збереженим тілом | **656 / 656 (100%)** |
| Вік найстарішого збереженого тіла | 3 дні (`2026-09-01`) |

`monitor_store_page_bodies=True`, `monitor_body_retention_days=0` — тіла не чистяться. Тобто HTML для екстракції **вже лежить на диску**, читати сайт вдруге не треба.

### 1.2 Breadcrumbs беруться з JSON-LD — покриття 100%

`BreadcrumbList` (Yoast) присутній у **443 із 443** дедуплікованих сторінок. Глибина: `1` — 1 сторінка, `2` — 160, `3` — 22, `4` — 260. Приклад живого трейлу:

```
Home > Shop All Street&shy;wear > BOGO Sales puffers > Apex Washed Hoodie
```

Два факти, що йдуть просто в реалізацію:
- у назвах трапляється `&shy;` (soft hyphen) — HTML-entity треба декодувати **і** прибирати невидимі керівні символи, інакше в клієнтський файл поїде `Street&shy;wear`;
- `itemListElement` не гарантує порядок у документі — сортуємо за `position`, а не за позицією в масиві.

Колонка `Pages` зі скріншота = трейл **без** `Home`, склеєний через ` > ` (`Domes > Oticon`); для кореня — `Home`.

### 1.3 Keywords: `<meta name="keywords">` на цьому сайті немає взагалі

Перевірено 200 сторінок — **0 збігів**. GSC-інтеграція клієнта вимкнена (`enabled=0`), `gsc_dimension_metrics` для нього порожня, і навіть за увімкненої GSC наша схема тримає `page` і `query` **як окремі виміри** — зв'язку «запит → сторінка» в БД не існує в принципі.

**Рішення користувача (2026-09-04): беремо тільки `<meta name="keywords">` зі збереженого HTML.** Наслідок приймається свідомо: на ALPHA колонка буде порожня, і це чесний стан `no_meta_keywords`, а не «ключів немає». Заборонено: виводити «ключі» з тексту титулу, з URL-слага чи з H1 — вигадані дані гірші за порожню клітинку (PLAN §12). Ручне введення та GSC page×query — у §11 (follow-ups), не в цій тасці.

### 1.4 Рушій task-53 працює на живих сторінках — але потребує калібрування

Наскрізний прототип (екстракція + `run_checks`) прогнано на всьому клієнті:

```
rows=657  deduped=443  extract=0.9s  qa=1.73s  findings=4114
```

| Check / severity | Кількість | Що це насправді |
|---|---|---|
| `meta-desc-duplicate` / error | **3060** | попарні порівняння: 23 реальні кластери, 75 сторінок |
| `meta-title-length` / warn | 404 | `floor is 50` — правило для документів-стратегій, не для товарного каталогу |
| `meta-title-duplicate` / error | 301 | ті ж кластери, попарно |
| `meta-desc-length` / warn + error | 170 + 35 | недобір / перебір |
| `slop-filler-term` / warn | 67 | реальні знахідки |
| `meta-desc-banned-opener` / error | 44 | реальні (`Description opens with banned opener 'shop'`) |
| `meta-title-length` / error | 20 | реальні (>60) |
| `voice-em-dash` / error | 6 | реальні |
| `slop-participle-chain`, `slop-copula-avoidance` / info | 2 | реальні |

Два висновки:
1. **Попарні дублікати не масштабуються.** 3060 findings — це 23 кластери, показані `O(n²)` разів. Це не баг task-53: правило проєктувалось під аркуш стратегії на 9 рядків. Кластеризацію робимо **у себе**, чужі правила не чіпаємо.
2. **`TITLE_MIN = 50` тут не house-rule, а шум.** 404 із 443 сторінок «порушують» його, бо це каталог товарів. Правило лишається, але його вага налаштовується профілем.

**Рішення користувача (2026-09-04): два режими перемикачем.** Дефолт — `calibrated`, тумблер `Strict` показує все 1:1 як task-53.

### 1.5 Дедуплікація по canonical обов'язкова

657 живих сторінок → **443 унікальні canonical**. 214 рядків (33%) — це `www` / не-`www` та подвійні маршрути WooCommerce (`/faq/` і `/pages/faq`). Без дедуплікації клієнт отримує файл, у якому третина рядків — та сама сторінка, а половина findings — фальшиві «дублікати». `canonical` порожній у 39 сторінок — для них ключ = нормалізований URL, і рядок несе прапорець `canonical_missing` (сам собою — валідна SEO-знахідка).

### 1.6 Семантика `None` у контенті сайту протилежна семантиці файлу

`app/contentqa/model.py` фіксує: `None` = поля в джерелі немає (правило мовчить), `""` = поле є й порожнє (правило б'є). Для XLSX-стратегії це правильно: «колонки Title у файлі немає» — не помилка автора.

Для **живої сторінки** все навпаки: 201 із 657 сторінок ALPHA не має тега `<meta name="description">` взагалі — і це саме дефект, а не «поле не застосовне». Тому адаптер `monitor_pages → ContentItem` мапить «тега немає» у `""`, а `None` лишає **виключно** для випадку «сторінку ще не перевіряли» (`status IS NULL`, 48 сторінок). Ця трансляція — окрема функція з окремим тестом, бо саме тут найлегше збрехати оператору.

### 1.7 Витрати на побудову

`0.9s` екстракція (443 blob-и, 24 МБ gzip) + `1.73s` QA = **2.6 с на клієнта**. Фонова джоба не потрібна: будуємо синхронно в threadpool і кешуємо знімок у БД. Per-field-прохід (§2.4) множить QA-частину на 4 → ≈ 8 с у найгіршому разі, все ще в межах одного HTTP-запиту з `run_in_threadpool`.

---

## 2. Рішення (архітектура)

### 2.1 Конвеєр

```
monitor_pages ──┐
monitor_blobs ──┤──► app/pageinventory/
(read-only)     │      source.py    — вибірка сторінок + найсвіжіший blob на сторінку
                │      extract.py   — BreadcrumbList (JSON-LD) + <meta keywords>, stdlib only
                │      dedupe.py    — canonical → один рядок, решта у `duplicate_of`
                │      inventory.py — InventoryRow (модель вкладки 1)
                │      qa.py        — InventoryRow → ContentItem → contentqa.run_checks
                │      calibrate.py — кластери дублів + профілі calibrated|strict
                └──►   xlsx.py      — 2 аркуші
                       ↓
              page_inventory_snapshots / page_inventory_items (кеш)
                       ↓
              app/api/client_pages.py ──► #/client/<id>/pages
```

### 2.2 Чому читаємо збережені blob-и, а не розширюємо монітор

Спокуса — додати `breadcrumbs` у `PageSignals`/`build_signals()` і збирати на льоту. **Не робимо**, дві причини:

1. `signals_json` і `signals_sha256` — це одна структура за побудовою (`build_signals` docstring). Нове поле змінює хеш **кожної** сторінки портфоліо на першому ж проході → 3648 фальшивих `page_changed` в алертах. Ціна ізоляції — 0.9 с на клієнта.
2. `app/monitor/**` зараз — активна зона task-51 із незакоміченими правками. Адитивний дотик там коштував би узгодження; читання blob-ів не коштує нічого.

Наслідок, який чесно показуємо в UI: інвентар відображає стан **останнього збереженого тіла** (для ALPHA — не старше 3 днів), а не «прямо зараз». Плашка `Built from monitor data collected <дата>` — обов'язкова.

### 2.2a Rebuild проти Re-crawl — дві різні дії, дві кнопки

`Rebuild` (§2.1, 2.6 с) читає те, що монітор **уже** зібрав — свіжість обмежена `monitor_full_interval_hours` (дефолт 3 год) або довше, якщо клієнт у antibot-доріжці. Для «сайт щойно переписали, покажи зараз» цього недостатньо — потрібен новий обхід.

**`Re-crawl`** не додає нового краулера — він **триґерить наявний** `POST /clients/{id}/monitor/runs?kind=full` (task-35/36, DONE), той самий ендпойнт, що стоїть за кнопкою «Check now · Full» у Site Monitor. Це full-run: монітор повторно фетчить кожен URL зі свіжого HTTP-запиту (dedup спрацьовує лише на **збереження тіла** за фінгерпринтом — сам запит завжди йде наживо), тож переписана сторінка гарантовано перечитується. Обираємо цей шлях, а не власний обхід у `app/pageinventory/`, з двох причин:

1. **Нуль нового коду обходу сайту.** Rate-limiting, robots, antibot-доріжка (task-50), паралельні слоти (task-31) — усе вже написано й перевірено в `app/monitor/**`. Дублювати краулер у своїй зоні — саме those «доробив мимохідь чужий модуль», якого забороняє CLAUDE.md.
2. **Один краул — одна правда.** Якщо ми запустимо власний паралельний фетч, дві системи почнуть розходитись у тому, яка версія сторінки «правильна». Триґеримо чужий craul, чекаємо його результат, читаємо той самий `monitor_pages`/`monitor_blobs`, що й Site Monitor.

Оркестрація — **цілком на фронтенді**, без нового бекенд-ендпойнту:

```text
[Re-crawl] → POST /clients/{id}/monitor/runs?kind=full   (task-36, вже є)
           → поллінг GET /clients/{id}/monitor/runs?kind=full&limit=1  (той самий
             plain-poll патерн, що monitor-client.js §"Check now / polling" —
             без job engine, без SSE)
           → по completed/failed → POST /clients/{id}/pages/rebuild  (наш)
           → перемалювати таблицю
```

Чесні наслідки в UI (копірайт англійською, як завжди):

- Повний прогін — це **хвилини**, не секунди (`monitor_run_max_minutes` — до 90 хв; antibot-клієнти повільніші). Кнопка одразу переходить у `Re-crawling… (started 2 min ago)`, лишається disabled, прогрес — з полів `MonitorRunDTO` (той самий DTO, що вже показує лічильник перевірених URL у Site Monitor).
- Якщо full-run уже виконується (запущений планувальником чи іншим оператором) — кнопка не запускає другий, а підключається до наявного (`listMonitorRuns(clientId, kind="full", limit=1)` перед стартом).
- Якщо вкладку закрили й повернулись — поллінг не переживає перезавантаження сторінки (та сама відома межа, що й у `monitor-client.js`); повторний вхід на `#/client/<id>/pages` перевіряє активний run і підхоплює поллінг знову, а не губить його мовчки.
- Провал краулу (сайт зверх антиботом, сайт лежить) не блокує `Rebuild` — вона й далі працює з тим, що є, і кнопка `Re-crawl` показує причину провалу з `MonitorRunDTO.error`/`MonitorChange`, а не проковтує її.

Жодних нових функцій в `frontend/js/api.js` для цього не треба — `startMonitorRun`/`listMonitorRuns` уже експортовані (task-36); викликаємо їх напряму з нового `page-inventory-page.js`, не редагуючи `monitor-client.js`.

### 2.3 Профілі QA (тумблер із §1.4)

| | `calibrated` (дефолт) | `strict` |
|---|---|---|
| Дублікати title/description | 1 finding на кластер: `N pages share this description` + список URL | як є в task-53 (попарно) |
| `meta-title-length` недобір (`< TITLE_MIN`) | `info` | `warn` (як у task-53) |
| `meta-desc-length` недобір | `info` | `warn` |
| Перебір лімітів (`> TITLE_MAX` / `> DESC_MAX`) | `error` | `error` |
| Слоп / brand voice | без змін | без змін |

Калібрування — **пост-процесор** над `list[Finding]` у `app/pageinventory/calibrate.py`. Жодне правило task-53 не переписується; понижується лише severity й згортаються кластери. Тумблер не змінює **набір** знахідок (нічого не ховаємо) — лише їх вагу й групування, і це написано в підказці біля тумблера.

### 2.4 Прив'язка finding до поля — без евристик

`Finding` не має поля `field`, а вгадувати по тексту `value` — крихко. Робимо детерміновано:

- **Детермінований словник** `FIELD_BY_CHECK` для чеків, у яких поле однозначне з назви: `meta-title-*` → `title`, `meta-desc-*` → `description`, `url-duplicate` → `url`, `keyword-*` → `keywords`, `h1-*` → `h1`.
- **Ізольований прохід** для `slop-*` / `voice-*` (вони бʼють по будь-якому текстовому полю): рушій прокручується окремо для документа, у якому заповнене лише одне поле за раз (`title`, потім `description`, потім `keywords`). Знахідка з `i`-го проходу належить `i`-му полю за побудовою. Ціна — ×3 до `1.73s` (§1.7).
- **Cross-page-чеки** (`*-duplicate`, `check_cross_page`) виконуються один раз на повному документі й вішаються на поле зі словника.
- Усе, що не змапилось, іде в бакет `page` і показується в рядку окремим бейджем — **не мовчки відкидається**.

### 2.5 Чого свідомо НЕ робимо

- Не тягнемо `body` сторінки в `ContentItem`: без нього мовчать `check_links`, `check_structure`, `check_readability` (перевірено — `None` вимикає правило), і це правильно: інвентар — про **meta-шар**, а не про тіло тексту. Аудит тіла — це task-53 Framework Check і task-25.
- Не вигадуємо «планові» рядки на кшталт `doesn't exist` зі скріншота (там це ручні записи майбутньої структури). Натомість даємо їхній чесний машинний еквівалент: `status` рядка (`live` / `broken 404` / `redirect` / `noindex` / `not_checked`) і `canonical_missing` — колонка, яку в експорті можна фарбувати як у скріншоті. Імпорт планової структури — §11.

---

## 3. Модель даних (міграція `*_page_inventory.py`)

```python
class PageInventorySnapshot(Base):          # один клієнт × одна побудова
    id, client_id (FK CASCADE), built_at,
    source_collected_at        # найсвіжіший monitor-прохід, з якого взяті дані
    pages_total, pages_unique, pages_with_breadcrumbs, pages_with_keywords,
    breadcrumb_source          # "json_ld" | "dom" | "mixed" | "none"
    warnings_json              # напр. ["48 pages not checked yet"]

class PageInventoryItem(Base):
    id, snapshot_id (FK CASCADE), client_id,
    url, canonical_key, duplicate_urls_json,
    page_label,                # "Domes > Oticon"
    breadcrumbs_json,          # [{"name": ..., "url": ...}, ...]
    breadcrumb_depth,
    title, description, keywords_json, keywords_source,   # "meta" | "none"
    h1, status, indexable, canonical_missing,
    findings_json              # [{check, severity, field, detail, value}]
```

- Один клієнт тримає **один** актуальний знімок: нова побудова пише новий рядок і видаляє попередній (історія архітектури — не мета цієї таски; за потреби — §11).
- `findings_json` зберігаємо разом зі знімком, щоб XLSX і UI показували те саме, а не два різні прогони. Профіль (`calibrated`/`strict`) застосовується **на читанні** — у знімку лежить повний, некалібрований набір.
- Міграція — прямий `op.create_table` × 2 + індекси. Перед `alembic upgrade` на бойовій — бекап (CLAUDE.md).

## 4. API (`app/api/client_pages.py`, self-registering роутер — патерн task-54)

| Метод | Шлях | Що робить |
|---|---|---|
| `GET` | `/clients/{id}/pages` | знімок + рядки; `?profile=calibrated\|strict`, `?q=`, `?status=`, `?field=`, `?severity=`, пагінація |
| `POST` | `/clients/{id}/pages/rebuild` | перебудова зі свіжих monitor-даних (409, якщо монітор для клієнта вимкнений / сторінок немає) |
| `GET` | `/clients/{id}/pages/summary` | лічильники для картки на сторінці клієнта |
| `GET` | `/clients/{id}/pages/xlsx` | двоаркушевий файл `ALPHA_Page_Inventory_YYYYMMDD.xlsx` |

Порожні/аварійні стани віддаються **як стани**, не як 500: `monitor_disabled`, `no_pages_yet`, `never_built`, `policy_unavailable` (коли `seo_audit.policy` недоступний — рушій task-53 уже віддає для цього окремий finding, ми його не ковтаємо).

`Re-crawl` (§2.2a) свідомо **не** новий ендпойнт цієї таски — фронтенд викликає наявні `POST /clients/{id}/monitor/runs?kind=full` і `GET /clients/{id}/monitor/runs` (task-36), а по завершенню — наш же `POST /pages/rebuild`. Таблиця вище лишається повним переліком нових роутів `app/api/client_pages.py`.

## 5. UI (`#/client/<id>/pages`)

- Вхід — картка на сторінці клієнта (`client-page.js`, +1 блок) із лічильниками `443 pages · 55 title issues · 23 duplicate groups` і датою збору.
- Панель дій над таблицею — **дві** окремі кнопки, різні за силою й ціною (§2.2a):
  - `Rebuild` — тихий `button--quiet`, `2–3s`, підказка `Rebuild from the latest crawl`.
  - `Re-crawl site` — `button--primary` з іконкою (той самий стиль, що `check-full` у `monitor-client.js`), підказка `Fetch every page fresh — use after the site changed. Takes several minutes.`; під час прогону текст міняється на `Re-crawling… (started Xm ago)`, кнопка `disabled`, поруч — компактний прогрес із `MonitorRunDTO` (перевірено/усього URL), той самий, що вже малює Site Monitor для `check-full`.
- Дві вкладки на одному наборі рядків (`role="tablist"`, як `analytics-tabs.js`):
  - **Inventory** — `Pages | URL | Breadcrumbs | Title | Description | Keywords`, точний порядок зі скріншота. URL — клікабельний, `rel="noopener"`. Ширина: `.table-scroll` + `contain: paint` на обгортці (пам'ять: `table-scroll-contain-paint`, інакше широка таблиця скролить усю сторінку в Chromium).
  - **SEO QA** — ті самі колонки, кожна клітинка з бейджем `error|warn|info|ok|not_checked`; клік по клітинці розкриває findings саме цього поля. Зверху — панель-зведення за чеками (згорнуті кластери) + тумблер `Calibrated / Strict`.
- Стани за AGENTS §6: `loading` (скелет), `empty` (`Monitor has not crawled this client yet`), `failed`, `not_configured` (`Site Monitor is off for this client`), `stale` (дані старші за 7 днів — плашка, не помилка).
- **CSP:** жодного inline-`style` і жодного JS-заданого `.style.top/left` (пам'ять: `frontend-verification-tooling`, `task43-reports-ui-done`) — тільки класи.
- UI-копірайт — англійською.

## 6. XLSX (`app/pageinventory/xlsx.py`, `openpyxl` — уже в дереві)

- **Sheet 1 `Inventory`**: 6 колонок зі скріншота + `Status`. Заливка рядка за статусом у палітрі оригінального файлу: зелена `#D9EAD3` — `live`, помаранчева `#FCE5CD` — `broken`/`redirect`, жовта `#FFF2CC` — `noindex`/`canonical_missing`, синя `#CFE2F3` — дублікат canonical. Freeze panes на шапці, autofilter, ширини під контент — як у `app/reports/monthly_xlsx.py`.
- **Sheet 2 `SEO QA`**: ті самі колонки + `Title issues`, `Description issues`, `Keywords issues`, `Other issues`; клітинка поля фарбується за найвищою severity свого поля. Легенда — на першому аркуші.

## 7. Тести (`tests/unit/pageinventory/**`, `tests/unit/api/test_client_pages_api.py`)

- `extract`: BreadcrumbList у `@graph` / у масиві / у голому обʼєкті; `position` не за порядком; `&shy;` та HTML-entities; сторінка без BreadcrumbList → DOM-fallback `#breadcrumbs`; биті JSON-LD → `None`, не виняток.
- `dedupe`: `www`/не-`www`, `canonical` на іншу сторінку, `canonical` порожній → `canonical_missing`.
- **Трансляція `None`/`""`** (§1.6) — окремий тест: тег відсутній → `""` → `meta-desc-missing`; сторінка не перевірена → `None` → жодного finding.
- `calibrate`: 3 сторінки з однаковим описом дають **1** finding із трьома URL у `calibrated` і 3 попарні у `strict`; пониження severity недобору довжини.
- `qa`: детермінована прив'язка поля — слоп у `description` не приписується `title` (регресійний тест на §2.4).
- API: `404` на невідомого клієнта; `409` на клієнта без монітора; `?profile=` змінює severity, але **не** кількість унікальних порушень; XLSX відкривається `openpyxl.load_workbook` і має рівно 2 аркуші з очікуваними шапками.
- Фікстури — локальні (обрізані HTML на ~30 рядків), **без** живих запитів у мережу.

## 8. Definition of Done / Gate

1. `docker compose exec api poetry run pytest tests/unit/pageinventory tests/unit/api/test_client_pages_api.py -q` — зелено.
2. `docker compose exec api poetry run ruff check .` і `mypy app` — чисто.
3. `POST /clients/d182b771-…/pages/rebuild` на живій БД будує **443** рядки (не 657) за < 10 с; `pages_with_breadcrumbs == 443`.
4. У вкладці QA в режимі `calibrated` дублікати описів показані як **23 кластери**, у `strict` — попарно; сума унікальних порушень однакова в обох режимах.
5. Колонка Keywords порожня з бейджем `no meta keywords`, а **не** з вигаданими значеннями (§1.3).
6. XLSX відкривається в LibreOffice і Google Sheets: 2 аркуші, кольори як у §6, жодного `&shy;` у назвах.
7. Візуальний loop за AGENTS §15–16 (Playwright `.cjs`, пам'ять `frontend-verification-tooling`): 1440 / 1024 / 375 px, темна й світла теми, порожній / помилковий / завантажувальний стани, а також стан `Re-crawling…` (кнопка disabled, прогрес видно).
8. Жодного диффу в `app/monitor/**` і `app/contentqa/**` (`git diff --stat` у гейті) — `Re-crawl` реалізовано **виключно** викликами наявного `POST/GET .../monitor/runs` (task-36) з нового фронтенд-модуля, без жодної правки в `app/monitor/**` чи `monitor-client.js`.
9. Живий прогін `Re-crawl` на клієнті з увімкненим монітором: кнопка запускає `kind=full`, поллінг ловить `completed`, `Rebuild` викликається автоматично, таблиця оновлюється без ручного натискання.

## 9. Ризики

| Ризик | Пом'якшення |
|---|---|
| Сайт без Yoast/JSON-LD → крихт немає | DOM-fallback `#breadcrumbs`; якщо і його нема — `breadcrumb_source="none"`, колонка порожня зі станом, таблиця будується далі |
| Клієнт із 25 000 сторінок (`monitor_max_urls_per_client`) | ліміт рядків на знімок + пагінація в API; при перевищенні — чесний `truncated`-прапор, як `Quality.truncated` у GSC-вимірах |
| `seo_audit.policy` недоступний | рушій task-53 уже віддає `policy-unavailable`; показуємо його як банер над вкладкою QA, і жодне поле не малюється зеленим |
| Паралельна сесія в `app/db/models.py` / `frontend/js/api.js` | вставки в кінець файлу; перед торканням — `git status` |
| Дані застаріли (монітор упав) | плашка `stale` з датою `source_collected_at`; ніколи не видаємо старий знімок за свіжий |

## 10. Follow-ups (свідомо поза скоупом)

- `TODO(task-58)` — **Keywords із GSC page × query**: новий вимір збору через Reporting Ninja, топ-N запитів на URL як «фактичні» ключі поруч із цільовими. Розблокує `meta-title-keyword-missing` на живих сторінках.
- `TODO(task-58)` — **ручні/імпортовані Keywords**: редагована колонка, що переживає перебудову, + імпорт цільових ключів із SEO-стратегії клієнта (XLSX-рідер task-53 їх уже читає).
- **Імпорт планової структури** (рядки `doesn't exist` зі скріншота) і дифф «план ↔ факт».
- **Спільний JSON-LD-екстрактор** із task-56: коли `app/tools/schema_markup/**` ляже в `done`, звести дві реалізації в одну (наша — мінімальна, лише `BreadcrumbList`).
- **Історія архітектури**: зберігати знімки й показувати «сторінка змінила батька / зникла з крихт».

---

## ✅ Completion report

- **Дата:** 2026-09-04
- **Гілка / комміт:** `main` @ незакомічено (див. «Follow-ups» — комміт свідомо відкладено через паралельну сесію)
- **Статус:** done-with-followups

### Що зроблено

Обидві вкладки живі й перевірені на реальних даних клієнта ALPHA (`alphastyle.store`, 491 унікальна сторінка):

- `#/client/<id>/pages` — під-сторінка з двома вкладками (**Inventory**, **SEO QA**), картка-вхід на `#/client/<id>` (лічильники `Pages / Duplicate groups / Issues`).
- **Inventory**: точно колонки зі скріншота — `Pages | URL | Breadcrumbs | Title | Description | Keywords`, дедуплікація по canonical, статус-бейджі (`live/broken/redirect/noindex/not_checked`), позначка `+N dup` на канонічному рядку.
- **SEO QA**: ті самі 6 колонок + бейдж-лічильник на кожному полі (title/description/keywords), клік по рядку розгортає деталізацію знахідок по полю. Тумблер **Calibrated / Strict** — живо перевірено: та сама сторінка в Strict дає 6+ попарних `meta-desc-duplicate`, у Calibrated — 1 фінding `"249 pages share this description: …"`.
- **Rebuild** (з кешу монітора, ~2.6 с) і **Re-crawl site** (справжній full-краул через наявний `POST .../monitor/runs?kind=full`, з поллінгом і авто-Rebuild по завершенню) — обидві кнопки в UI, живо перевірено натисканням.
- **Export to Excel** — двоаркушевий `.xlsx` (`Inventory` + `SEO QA` з легендою), перевірено `openpyxl.load_workbook`.

### Як зроблено (ключові рішення)

- **Нуль нових правок у `app/monitor/**` і `app/contentqa/**`** (перевірено `git diff --stat` — порожньо для обох). Інвентар читає `monitor_pages`/`monitor_blobs` лише на читання; `Re-crawl` — це чиста фронтенд-оркестрація над наявним `POST/GET .../monitor/runs` (task-36), не новий краулер.
- **Breadcrumbs** — `app/pageinventory/extract.py`, JSON-LD `BreadcrumbList` (покриття 438/491 живих сторінок) + DOM `#breadcrumbs`-фолбек, stdlib `html.parser`/`json`/`re` тільки.
- **Дедуплікація й трансляція `None`/`""`** — `app/pageinventory/inventory.py`: canonical-дедуп (657 живих → 491 унікальних із урахуванням broken/redirect/not_checked рядків — ширше за початкову оцінку 443, бо в повний інвентар свідомо включені й непрацюючі сторінки, не лише `200`); `""` для тега, якого браузер не знайшов на ЖИВІЙ сторінці, `None` — виключно для «сторінку ще не перевіряли» (інверсія правила `app.contentqa.model.ContentItem`, задокументована в docstring моделі).
- **Прив'язка finding→поле — детермінована** (`app/pageinventory/qa.py`): словник `FIELD_BY_CHECK` + ізольований прохід рушія по одному текстовому полю за раз для `slop-*`/`voice-*`-чеків. Окремо виявлено й виправлено пастку: `meta-title-duplicate`/`meta-desc-duplicate`/`url-duplicate`/`keyword-duplicate-across-pages` віддають **не** URL рядка, а комма-розділений список 2+ URL (`MULTI_REF_CHECKS`) — наївна прив'язка по `finding.ref` мовчки губила б ці знахідки.
- **Калібрування** (`app/pageinventory/calibrate.py`) — union-find по попарних duplicate-findings у кластери (живо: 19 кластерів, найбільший — 249 сторінок з однаковим описом), пониження `warn→info` для floor-порушень довжини title/desc, `error` для ceiling-порушень лишається `error` в обох режимах. Кластери рахуються безумовно (не залежать від активного профілю) — лічильник у картці й XLSX-легенді завжди правдивий.
- **Зберігання**: `findings_json` в БД завжди повний (strict) набір; `?profile=calibrated|strict` застосовується на читанні (`app/pageinventory/service.py`), рушій QA не перезапускається на кожен GET.
- **Головні файли:** `app/pageinventory/{extract,inventory,qa,calibrate,service,xlsx}.py` — конвеєр; `app/api/client_pages.py` — REST (`GET /pages`, `POST /pages/rebuild`, `GET /pages/summary`, `GET /pages/xlsx`); `app/db/repositories/page_inventory.py` — один активний снепшот на клієнта; `frontend/js/page-inventory-{page,table,qa}.js` + `frontend/css/page-inventory.css` — UI.
- **Відхилення від плану:** окремий `dedupe.py` не виділявся — логіка дедупу невелика й щільно зв'язана з побудовою рядка, залишена в `inventory.py` (жодних інших відхилень).

### Контрактні зміни (Contract changes)

Адитивно, без правок чужих контрактів:
- `app/db/models.py`: +2 моделі (`PageInventorySnapshot`, `PageInventoryItem`), вставлені в кінець файлу перед `_reject_update`.
- `frontend/js/api.js`: +4 функції в кінець файлу (`getPageInventory`, `getPageInventorySummary`, `rebuildPageInventory`, `pageInventoryXlsxUrl`).
- `frontend/js/router.js`: +1 маршрут (`#/client/<id>/pages`) + `buildPagesHash`.
- `frontend/js/client-page.js`: +1 картка-вхід (`#cp-pages`, `loadPageInventorySummary`/`renderPageInventorySummaryCard`).
- `frontend/index.html`: +1 `<main>`-root, +1 `<link>`.
- Нова міграція `a064ff1f082f_page_inventory.py` (застосована на dev-БД після `scripts/backup_sqlite.py`, прямий `op.create_table` × 2, без `batch_alter_table`).

### Як перевірити (verification)

```bash
docker compose exec api poetry run pytest tests/unit/pageinventory tests/unit/api/test_client_pages_api.py -q   # 37 passed
docker compose exec api poetry run ruff check app/pageinventory app/api/client_pages.py app/schemas/page_inventory.py app/db/repositories/page_inventory.py
docker compose exec api poetry run mypy app/pageinventory app/api/client_pages.py app/schemas/page_inventory.py app/db/repositories/page_inventory.py
```

Живий гейт (уже прогнано на реальному ALPHA):
```bash
curl -X POST http://localhost:6524/api/v1/clients/d182b771-91cf-4efd-b0dc-b4e1f7fff0d3/pages/rebuild
curl "http://localhost:6524/api/v1/clients/d182b771-91cf-4efd-b0dc-b4e1f7fff0d3/pages?profile=strict"
curl -o out.xlsx http://localhost:6524/api/v1/clients/d182b771-91cf-4efd-b0dc-b4e1f7fff0d3/pages/xlsx
NODE_PATH=/tmp/yovth-e2e/node_modules BASE_URL=http://localhost:6524 CHROMIUM_PATH=/snap/bin/chromium \
  node tests/e2e/frontend/page-inventory.cjs   # усі перевірки зелені, скріншоти в tests/e2e/frontend/screenshots/
```
UI: `#/client/d182b771-91cf-4efd-b0dc-b4e1f7fff0d3/pages` — обидві вкладки, тумблер Calibrated/Strict, Rebuild, Re-crawl site, Export to Excel.

### Тести

- `tests/unit/pageinventory/test_extract.py` (11) — breadcrumbs з `@graph`/масиву/голого обʼєкта, сортування за `position`, `&shy;`/entity-декодування, DOM-фолбек, биті JSON-LD, meta keywords present/absent/empty.
- `tests/unit/pageinventory/test_inventory.py` (6) — дедуп по canonical, `canonical_missing`, **трансляція `None`/`""`** (окремий тест саме на цю інверсію), статус-класифікація, екстракція з реального збереженого blob'а.
- `tests/unit/pageinventory/test_qa.py` (6) — детермінована прив'язка finding→поле, ізоляція слопу title/description, fan-out попарних duplicate-findings на обидва рядки, unchecked-рядки пропускаються.
- `tests/unit/pageinventory/test_calibrate.py` (5) — 3-way кластер → 1 finding на рядок у calibrated, попарний вигляд у strict, кластер-лічильник профіль-незалежний, floor↔ceiling severity.
- `tests/unit/api/test_client_pages_api.py` (9) — 404/409 стани (`client_not_found`/`never_built`/`no_pages_yet`/`monitor_disabled`), rebuild→get roundtrip, `?profile=` тумблер, XLSX-шапки.
- `tests/e2e/frontend/page-inventory.cjs` — живий Playwright проти реального бекенду й реального клієнта: картка на `#/client/<id>`, обидві вкладки, порядок колонок, відсутність горизонтального скролу сторінки, тумблер профілю, розгортання деталей рядка, кнопки Rebuild/Re-crawl/Export, нуль console-помилок.

### Живий баг знайдено й закрито (2026-09-04, після першого деплою)

Користувач показав живий скріншот клієнта GOREPORT — колонка `Pages` на **кожному** рядку показувала `"Home"`. Причина: у монітора для GOREPORT (і ще 6 клієнтів портфоліо — SHB, SWM, EFFYS, 19CLV повністю; 3XE, SDS, HWELL, NYCHA, GRP частково) **немає жодного збереженого HTML-тіла** (0 із 228 версій сторінок мають `body_blob_sha256`) — тому breadcrumb-екстракція повертала порожній трейл. Порожній трейл і трейл `["Home"]` (справжня домашня сторінка) оброблялись однаково: і `extract.page_label(())`, і фронтендівський фолбек `row.page_label || "Home"` мовчки перетворювали «даних немає» на `"Home"` — фабрикація факту, яку прямо забороняє PLAN §12.

Виправлено на рівні `app/pageinventory/inventory.py`: новий `_label_from_url()` дає **URL-похідну** назву (`/10-key-risks-identified-in-roof-surveys/` → `"10 Key Risks Identified In Roof Surveys"`) для рядків без реального breadcrumb-трейлу, і `"Home"` лишається виключно для дійсно кореневого URL. Колонка `Breadcrumbs` окремо виправлена не дублювати цю похідну назву — показує чесний `—`, коли `breadcrumb_source == "none"`. Живо переверифіковано на GOREPORT (228 сторінок) і ALPHA (491, включно з `404`-рядком `agents.md`, який раніше теж хибно показував `"Home"`) — обидва скріншоти в `tests/e2e/frontend/screenshots/page-inventory-goreport-fixed-1440.png` і оновленому `page-inventory-table-1440.png`. +4 тести в `test_inventory.py` фіксують саме цю поведінку.

Ширший факт для нотатки: 7 із 14 клієнтів портфоліо мають нуль/близько нуля збережених HTML-тіл у моніторі — це, найімовірніше, наслідок того, що `body_blob_sha256` записується лише коли `fingerprint_changed or prev_version is None` (`app/monitor/runner.py`), тобто сторінка, чий перший-коли-небудь прохід стався до/без надійного body-capture, ніколи не отримає тіло, поки її контент не зміниться. Це — межа `app/monitor/**` (task-35), яку task-57 свідомо не чіпав (DoD gate: нуль правок там); `Re-crawl site` тому НЕ гарантовано наповнює прогалину для незмінного контенту. `TODO(task-60?)` — розглянути в `app/monitor/**` окрему умову «зберегти тіло, якщо в нас його ще нема взагалі», незалежно від fingerprint-порівняння.

### Follow-ups / борг

- **Комміт свідомо не зроблено.** Паралельна сесія (task-56) тримає незакомічені зміни в тих самих файлах, куди йшли наші адитивні дотики (`app/db/models.py`, `frontend/js/api.js`, `frontend/index.html`) — одна з `git add`/`git commit` наразі закомітила б і чужу незавершену роботу впереміш із нашою (task-56 вже перенесена в `doc/dev/done/`, але `git status` показує решту файлів досі `M`/`??`). Коли `git status` на цих файлах стане чистим (той сеанс закомітить своє), закомітити task-57 окремим логічним коммітом.
- `TODO(task-58)` — Keywords із GSC page×query (топ-N запитів на URL) + ручне/імпортоване редагування колонки Keywords, поверх наявної `<meta name="keywords">`-колонки.
- Імпорт планової структури сайту (рядки «doesn't exist» зі скріншота користувача) і дифф план↔факт.
- Спільний JSON-LD-екстрактор із task-56 (Schema Markup Checker, вже DONE) — звести дві мінімальні реалізації в одну, коли обидві стабілізуються.
- Історія знімків інвентарю (зараз — один активний снепшот на клієнта, попередній видаляється при rebuild).
