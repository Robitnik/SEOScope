# task-100 — Indexing Planner: календар ручного індексування, черга з доказом і аналітика лагу

**Depends on:** 27 ✅ (Tools hub), 35 ✅ (Site Monitor / `app/monitor/sitemaps.py`), 83 ✅ (прямий Google API transport), 88 ✅ (`UrlInspectionClient`), 71 ✅ (auth/ролі), 72 ✅ (audit), 06 ✅ (REST), 52 ✅ (scheduler-патерн у worker)
**Owned paths:**

```
app/indexing/                       (новий пакет: sync, inspect, score, plan, analytics, scheduler, xlsx)
app/api/tools_indexing.py           (новий роутер)
app/schemas/indexing.py             (новий DTO-модуль)
app/db/repositories/indexing.py     (новий репозиторій)
frontend/js/indexing-tool.js        (нова сторінка)
frontend/js/indexing-calendar.js    (нова: 8-денна сітка)
frontend/js/indexing-analytics.js   (нова: воронка + контрольна група)
frontend/css/indexing.css           (нова)
tests/unit/indexing/                (нові)
tests/unit/api/test_indexing_api.py (новий)
```

**Read-only дотики (правити НЕ можна, лише читати):** `app/monitor/sitemaps.py`, `app/urlinspector/google/index.py`, `app/integrations/google_api/*`, `app/db/models.py` (нові моделі додає ця таска в кінець файлу — узгодити з `INDEX.md`), `migrations/versions/` (нова ревізія через процедуру task-03).

**Адитивні дотики (рядок у наявний файл):** `app/api/tools.py` (запис у каталог), `app/jobs/worker.py` (старт планувальника), `frontend/js/tools-page.js` (`TOOL_MODULES`), `app/audit/actions.py` (нові дії), `app/core/config.py` (нові налаштування), `app/cli.py` (CLI-команди).

---

## 0. Одне речення

Тул не відправляє сторінки на індексацію — **API для цього не існує** (див. §1.1); він вирішує, **які саме 10 сторінок на клієнта варто нести руками в GSC сьогодні**, тримає докази цього рішення, не дає відправити ту саму сторінку двічі за 90 днів і потім чесно рахує, чи ця робота взагалі щось дала.

---

## 1. Дослідження (заміряно живо 2026-09-21, прямі виклики з контейнера `api`)

### 1.1 Відправити на індексацію програмно неможливо — і це визначає весь тул

`searchconsole.googleapis.com` має 11 методів; жоден не запитує індексацію. `urlNotifications.publish` (Indexing API) документовано обмежений `JobPosting`/`BroadcastEvent`, наші scope навмисно read-only (`webmasters.readonly`). Перевірено окремо 2026-09-09, рішення не додавати запис — чинне.

**Наслідок для дизайну, не примітка:** єдина дія, яку тул може автоматизувати, — це **рішення і облік**. Клацання «Request indexing» лишається людським. Тому центральний екран — не «запустити», а «ось сьогоднішні 10 URL, скопіюй, зроби, відміть». І тому **кнопка «Mark sent» — це найнебезпечніша мутація в тулі**: помилкове натискання ховає сторінку на 90 днів (§2.7).

### 1.2 `sitemaps.indexed` мертвий — стан індексації дає тільки URL Inspection

`GET /sites/{site}/sitemaps` для всіх 10 клієнтів із GSC:

| Клієнт | sitemap-ів | submitted | indexed |
|---|---|---|---|
| 3XE | 6 | 691 | **0** |
| ALPHA | 1 | 272 | **0** |
| EFFYS | 1 | 16 | **0** |
| GRP | 2 | 30 | **0** |
| LAXTON | 1 | 174 | **0** |
| NYCHA | 1 | 90 | **0** |
| SAPHNY | 2 | 978 | **0** |
| SDS | 2 | 744 | **0** |
| SHB | 2 | 125 | **0** |
| SWM | 3 | 62 | **0** |
| **Разом** | 21 | **3182** | **0** |

`indexed` — нуль **скрізь**, включно з клієнтом, у якого 103 зі 104 сторінок реально в індексі (§1.4). Google перестав наповнювати це поле; воно лишилось у відповіді. Узяти його як «проіндексовано 0 із 3182» означало б поставити в чергу весь портфель — 32 робочих дні марної ручної роботи.

> **Інваріант:** `sitemaps.contents[].indexed` не читається взагалі. Єдине джерело стану індексації — `urlInspection.index.inspect`.

### 1.3 Масштаб — 2311 живих URL, не 10 000 (і не 3182, які рапортує GSC)

GSC рапортує 3182 `submitted` URL на 10 клієнтів (GOREPORT — 11-й активний — GSC-інтеграції не має, тож поза тулом), але **живим завантаженням тих самих sitemap-ів дістається лише 2311** — решта живе в sitemap-ах, які GSC пам'ятає, а сайт більше не віддає (§1.5, п. 4). Краулер monitor'а бачить 3838 сторінок, але з урахуванням paused-клієнтів і не-sitemap сміття.

**Рішення користувача:** джерело черги — **тільки sitemap**. Сторінка, якої сайт сам не декларує Google, у чергу не потрапляє. Це і межа обсягу, і захист від сміття з §1.6.

Реальна арифметика (беклог 274 URL, §1.5) виявилась м'якшою за початкову оцінку по всьому портфелю:

| | Перший прохід | Далі |
|---|---|---|
| три «важкі» клієнти (SAPHNY 112, ALPHA 83, 3XE 46) | ~12, ~9, ~5 роб. днів при 10/день | приплив нових сторінок + регреси |
| решта сім клієнтів (33 URL разом) | ≤ 1 день кожен | переважно порожні дні |

Тобто **квота Google не є вузьким місцем — і людино-години теж перестають ним бути після перших двох тижнів**. Це причина, чому per-client денний ліміт (рішення користувача) важливіший за глобальний: більшість клієнтів не вибере й двох слотів на день, а SAPHNY варті всіх десяти.

### 1.4 URL Inspection: 0.88 с/URL при 8 потоках — масовий скан реальний

Виміряно на SHB, повний sitemap:

| Режим | Час |
|---|---|
| холодний одиничний виклик | 6.81 с |
| послідовно, 3 URL | 6.5 с/URL |
| паралельно ×4 | 6.62 с на 4 URL |
| повний скан SHB, 104 URL, 8 потоків | 92 с (0.88 с/URL) |
| повний скан SAPHNY, 491 URL | 407 с (0.83 с/URL) |
| повний скан 3XE, 715 URL | 597 с (0.83 с/URL) |
| **весь портфель, 2311 URL** | **~33 хв сумарно** |

API не серіалізує запити — латентність майже цілком мережева, паралелізм лінійний і **тримається на великих обсягах**: 0.83 с/URL на 715 URL не гірше за 0.88 с/URL на 104. Це вже не екстраполяція — весь портфель просканований фактично. Квота 2000/добу **на property**, найбільший клієнт — 715 URL, тобто щоденний повний скан кожного клієнта в межі влазить із запасом ×2.8.

`GoogleApiSession` уже має thread-safe refresh токена і backoff на 429/403 — окремий throttle не потрібен, потрібен лише кап на кількість потоків.

### 1.5 Результат скану: беклог 274 URL із 2311, і він сконцентрований

Повний sitemap + повна інспекція **всіх 10 клієнтів із GSC**, 2311 URL (2026-09-21):

| Клієнт | sitemap | в індексі | кандидати | виключення | покриття |
|---|---|---|---|---|---|
| 3XE | 715 | 652 | 46 | **17** | 91.2% |
| SAPHNY | 491 | 365 | **112** | **14** | 74.3% |
| SDS | 372 | 350 | 22 | 0 | 94.1% |
| ALPHA | 287 | 204 | **83** | 0 | 71.1% |
| LAXTON | 173 | 166 | 2 | 5 | 96.0% |
| SHB | 104 | 103 | 1 | 0 | 99.0% |
| NYCHA | 91 | 89 | 2 | 0 | 97.8% |
| GRP | 34 | 31 | 3 | 0 | 91.2% |
| SWM | 28 | 27 | 1 | 0 | 96.4% |
| EFFYS | 16 | 14 | 2 | 0 | 87.5% |
| **Разом** | **2311** | **2001** | **274** | **36** | **86.6%** |

Чотири висновки, кожен змінює дизайн:

1. **Беклог — 274 URL, не 3182, і він у трьох клієнтах.** SAPHNY (112), ALPHA (83) і 3XE (46) дають 241 із 274 — 88%. Решта сім клієнтів разом — 33 URL, тобто менше ніж один робочий день. Порожній день у календарі буде **типовим станом** для більшості клієнтів, і UI мусить читати його як «черга вичерпана», а не як помилку чи нуль. Арифметика першого проходу при квоті 10/день/клієнта: SAPHNY ~12 днів, ALPHA ~9, 3XE ~5, решта — по одному дню. Тобто портфель закривається приблизно за два тижні, а далі тул живе в режимі «нові сторінки + регреси».
2. **Виключення обов'язкові — 36 URL, 12% від усього, що інакше пішло б у чергу.** 15 `noindex`, 14 редиректів, 3 `robots.txt`, 3 чужих каноніки, 1 `404`. Без фільтра §2.3 це 36 одиниць денної квоти, витрачених на сторінки, які Google свідомо не індексує. У 3XE виключень (17) більше третини від його кандидатів (46).
3. **`Crawled`/`Discovered - currently not indexed` — найбільший клас, а не `unknown`.** 78 + 86 = 164 проти 110 «Google не знає цього URL». Це різні хвороби: `unknown` лікується відправкою, а `crawled - not indexed` означає, що Google сторінку **бачив і свідомо не взяв** — там відправка сама по собі, найімовірніше, не допоможе, і рядок має вести оператора до контенту, а не просто в чергу. Тому в UI ці причини мають різний текст дії, а не однакове «Submit».
4. **sitemap-и в GSC масово протухли.** Живих URL 2311 проти 3182 `submitted` із §1.2 — розрив 871. Причина видима: у 3XE **чотири з шести** зареєстрованих sitemap-ів мертві (`http://exxxoticaexpo.com/sitemap.xml` → HTTP-помилка, `nj.`/`chi.`/`mb.` субдомени → взагалі не з'єднуються), у GRP і SWM — по одному. Це самостійна знахідка для оператора, і тул показує її окремим станом (§2.3, крок 1).

### 1.6 Що саме віддає `coverage_state` — це і є словник причин

Стани, що **реально прийшли** на всіх 2311 просканованих URL, із частотою:

| `coverage_state` | К-сть | Клас |
|---|---|---|
| `Submitted and indexed` | 2001 | ок |
| `URL is unknown to Google` | 110 | кандидат — Google не знає сторінки |
| `Discovered - currently not indexed` | 86 | кандидат — знає, але не краулив |
| `Crawled - currently not indexed` | 78 | кандидат — краулив і **не взяв** (див. §1.5, п. 3) |
| `Excluded by ‘noindex’ tag` | 15 | **виключення** |
| `Page with redirect` | 14 | **виключення** |
| `Blocked by robots.txt` | 3 | **виключення** |
| `Duplicate, Google chose different canonical than user` | 3 | **виключення** |
| `Not found (404)` | 1 | **виключення** |

Очікувані далі, але поки не трапились: `Duplicate without user-selected canonical`, `Alternate page with proper canonical tag`, `Soft 404`, `Server error (5xx)`.

Зверни увагу на **типографські лапки** в `Excluded by ‘noindex’ tag` — це U+2018/U+2019, а не ASCII-апостроф. Порівняння рядком по `'noindex'` не спрацює; матчинг іде по нормалізованому підрядку (`noindex`), а не по повному тексту стану.

Половина цього списку — **не кандидати на індексацію, а сміття для виключення**. Нести в GSC сторінку з `noindex` або з чужим каноніком означає спалити одиницю денної квоти на операцію, яку Google свідомо ігнорує.

Побічно виміряно: у GSC page-вимірі (`gsc_dimension_metrics`) трапляються `/wp-content/uploads/...` і `cdn.`-субдомен, тобто зображення й не-сторінки. Ще один аргумент за sitemap як єдине джерело (§1.3).

### 1.7 Дані для «причини» вже лежать у БД — нового збору не треба

- `gsc_dimension_metrics` (dimension=`page`): 110 161 рядок по 11 клієнтах, періоди від 2025-01-01 до 2026-09-20 — джерело для `no_traffic` і `traffic_lost`.
- `page_inventory_items`: `status`, `indexable`, `canonical_missing`, `title` — джерело для `excluded` до того, як витрачено квоту.
- `monitor_pages` / `monitor_changes`: факт зміни сторінки — джерело для `stale_crawl` («ми її переписали, Google цього ще не бачив»).
- `SitemapEntry.lastmod` із `app/monitor/sitemaps.py` — джерело для `new_page`.

### 1.8 Прецеденти в кодовій базі, на які спирається дизайн

| Що потрібно | Готовий зразок |
|---|---|
| щонічний цикл у worker | `app/cloud/scheduler.py` (ідемпотентність через леджер, не через пам'ять потоку) |
| персистентна черга з claim/heartbeat | `archive_captures` (task-62: `queued_at`/`owner`/`heartbeat_at`) |
| per-client налаштування, створені лінько | `MonitorSettings`, `ArchiveSettings` |
| снапшот-таблиця тула + XLSX із того самого DTO | `app/tools/ga4_events/`, `app/pageinventory/xlsx.py` |
| реєстрація в хабі | `app/api/tools.py` + `TOOL_MODULES` у `frontend/js/tools-page.js` |

---

## 2. Дизайн

### 2.1 Контракт назви

Скрізь однаково (CLAUDE.md, «Провайдер/метрика називаються однаково»): ключ тула `indexing`, назва в UI — **Indexing Planner**, маршрут `#/tools/indexing`, префікс API `/api/v1/tools/indexing`, пакет `app/indexing/`, іконка `calendar-clock`.

### 2.2 Модель даних (4 таблиці, одна міграція)

Нова ревізія створюється **процедурою task-03**: чистий `op.create_table`, без `batch_alter_table`; перед застосуванням — бекап (`pg_dump -Fc`, як у task-98).

#### `indexing_settings` — per-client політика

```
client_id            GUID  UNIQUE  FK clients ON DELETE CASCADE
enabled              bool   default false     -- тул не вмикається сам на новому клієнті
daily_quota          int    default 10        -- ручний ліміт GSC на property
resubmit_after_days  int    default 90        -- цикл повтору
scan_mode            str(16) default "rotating"  -- "rotating" | "full"
created_at / updated_at
```

`enabled=false` за замовчуванням — новий клієнт дає стан `not_configured`, не нуль і не краш (PLAN §12). `daily_quota` per-client, бо вузьке місце — люди, а не квота Google (§1.3, рішення користувача); сума квот по увімкнених клієнтах і є денний план.

#### `indexing_urls` — реєстр sitemap-URL і його поточний стан

```
id, client_id FK, url String(2048), url_key String(2048)   -- UNIQUE (client_id, url_key)
-- походження
first_seen_at, last_seen_in_sitemap_at, gone_from_sitemap_at (nullable)
sitemap_lastmod (nullable date)
-- останній зріз URL Inspection (усе nullable: None = Google не сказав / ще не питали)
coverage_state, verdict, robots_txt_state, indexing_state, page_fetch_state
google_canonical, user_canonical, last_crawl_time
inspection_result_link (nullable)  -- deep-link у GSC; НЕ будується, лише зберігається (§2.5)
inspected_at (nullable)
is_indexed (bool | NULL)          -- NULL = НІКОЛИ не інспектували; не false
first_indexed_seen_at (nullable)  -- перший наш доказ присутності в індексі
left_index_at (nullable)          -- був indexed -> перестав
-- рішення планера
excluded bool default false, excluded_reason String(32) nullable, excluded_by_user_id nullable
priority_score numeric nullable, reason_code String(32) nullable, reason_json JSON
last_submitted_at (nullable), submissions_count int default 0
```

`is_indexed` — саме тризначне. `NULL` («ще не питали») і `false` («Google каже, що ні») — різні факти, і змішати їх означає або поставити в чергу весь непросканований портфель, або тихо його пропустити (PLAN §12: відсутність ≠ нуль).

`gone_from_sitemap_at` замість видалення рядка: сторінка, прибрана з sitemap, зберігає свою історію відправок, інакше аналітика §2.6 почне брехати заднім числом.

#### `indexing_submissions` — календар І історія в одній таблиці

```
id, client_id FK, indexing_url_id FK ON DELETE CASCADE
planned_for date                      -- клітинка календаря
status String(16)                     -- "planned"|"sent"|"quota_blocked"|"skipped"|"cancelled"
-- заморожений на момент планування доказ
reason_code String(32), reason_json JSON, state_at_plan String(64)
-- факт
submitted_at (nullable), submitted_by_user_id (nullable FK users)
state_at_submission String(64) nullable
-- результат
indexed_at (nullable), days_to_index int nullable
note String(500) nullable
created_at
UNIQUE (indexing_url_id, planned_for)
INDEX (client_id, planned_for, status)
```

Один рядок = одна клітинка календаря. Календар не є окремою сутністю: «понеділок, SDS, 10 URL» — це десять рядків зі `planned_for = <понеділок>`.

`reason_code`/`reason_json` **копіюються в момент планування, а не читаються з `indexing_urls` на екрані**. Через місяць стан URL інший, і питання «чому ми його тоді послали» має отримати відповідь того часу, а не поточну. Це буквальна вимога «у таблиці юрл мета та причина».

#### `indexing_scans` — прогін скану (аудит і прогрес)

```
id, client_id FK (nullable = портфельний), started_at, finished_at (nullable)
status String(16)   -- "running"|"ok"|"partial"|"failed"
trigger String(16)  -- "scheduled"|"manual"
urls_total int|NULL, urls_inspected int default 0, urls_failed int default 0
sitemap_added int, sitemap_gone int
error_code, error_message
owner String(64) nullable, heartbeat_at nullable   -- як archive_captures (task-62)
```

`urls_total` саме nullable: прогін, який упав до того, як порахував свій обсяг, не має показувати `0` (PLAN §12).

### 2.3 Конвеєр: чотири кроки, кожен ізольований по клієнту

```
sync_sitemap  ->  inspect  ->  score  ->  plan
```

Клітинка `(client, крок)` ізольована: виняток на одному клієнті не валить прогін решти (PLAN §9.1, застосований до read-шляху — той самий принцип, що вже діє в `app/urlinspector/google_service.py`).

**1. `sync_sitemap`** (`app/indexing/sync.py`). `GscClient.sitemaps(site)` → перелік поданих sitemap-ів → `fetch` + `parse_sitemap` (`app/monitor/sitemaps.py`, index → children, `.gz` прозоро). Нормалізація в `url_key` бере правило з task-88 (trailing slash губить 96% матчів — [[per-url-data-join-gotchas]]). Нові URL додаються, зниклі отримують `gone_from_sitemap_at`, повернені — його скидають.

**Биті sitemap-и — окремий стан, не тихий пропуск.** Живо (§1.5) два з 21 поданих sitemap-ів віддають HTTP-помилку: GSC їх пам'ятає, сайт більше не віддає. Якщо просто проковтнути виняток, перелік сторінок клієнта мовчки зменшиться, а частина сайту назавжди випаде з черги, і ніхто цього не помітить. Тому: прогін стає `partial`, кожен недоступний sitemap записується з причиною, а URL, відомі з попереднього синку, **не** позначаються як зниклі з sitemap — відсутність відповіді не є доказом відсутності сторінки (PLAN §12).

**2. `inspect`** (`app/indexing/inspect.py`). `ThreadPoolExecutor(max_workers=indexing_inspect_workers)`, default 8 (виміряно §1.4). Пріоритет черги інспекції — не порядок sitemap, а потреба:

| Група | Частота | Навіщо |
|---|---|---|
| відправлені за останні 30 днів | **щодня** | точність `indexed_at` — головна цифра аналітики |
| кандидати в черзі (`is_indexed IS NOT TRUE`, не `excluded`) | щодня | чергу не можна будувати на тижневих даних |
| решта портфеля | ротація, кожен URL ≥ 1 раз на 7 днів | тримати покриття свіжим без 2311 викликів щодня |

При `scan_mode="full"` — весь портфель клієнта щодня (47 хв на все, §1.4; дозволено, але не за замовчуванням).

**3. `score`** (`app/indexing/score.py`). Перше правило: **URL із `is_indexed IS NULL` не скориться взагалі** — він не кандидат і не «в порядку», він просто ще не перевірений, і потрапляє в чергу інспекції, а не в чергу відправки. Плутанина саме тут коштувала б найдорожче: непросканований портфель виглядав би як 3182 кандидати (§1.2).

Далі **виключення** — до будь-якого балу, бо виключений URL не змагається за квоту:

| `excluded_reason` | Умова |
|---|---|
| `noindex` | `indexing_state` каже про meta-noindex, або `page_inventory_items.indexable = false` |
| `robots_blocked` | `robots_txt_state` DISALLOWED |
| `non_canonical` | `google_canonical` є і ≠ `url_key` — Google уже обрав інший канонік |
| `http_error` | `page_fetch_state` не OK, або `page_inventory_items.status` не 2xx |
| `manual` | оператор виключив руками (переважає все) |

Далі `reason_code` кандидата — перший підхожий, зверху вниз:

| `reason_code` | Умова | База балу |
|---|---|---|
| `dropped_out` | був `is_indexed=true`, став не-indexed | 100 |
| `never_indexed` | `coverage_state` = URL unknown to Google | 90 |
| `discovered_not_indexed` | Discovered – currently not indexed | 85 |
| `crawled_not_indexed` | Crawled – currently not indexed | **55** |
| `new_page` | `sitemap_lastmod`/`first_seen_at` свіжіші за 30 днів і ще не в індексі | 75 |
| `stale_crawl` | в індексі, але `monitor_changes` бачив зміну ПІСЛЯ `last_crawl_time` | 60 |
| `traffic_lost` | в індексі, кліки/покази впали проти попереднього періоду | 40 |
| `no_traffic` | в індексі ≥ 90 днів, 0 показів за останні 90 днів | 30 |
| `periodic_refresh` | cooldown минув, інших причин немає | 10 |

`crawled_not_indexed` свідомо стоїть **нижче** за `discovered_not_indexed`, хоча інтуїтивно здається «ближчим до індексу». Це прямий висновок із §1.5 п. 3: `Discovered` означає, що Google ще не дійшов краулити — і саме тут відправка найкорисніша; `Crawled – currently not indexed` означає, що він **уже подивився і не взяв**, тобто проблема в самій сторінці (тонкий контент, дублікат, слабкий сигнал), і повторна відправка того самого без змін нічого не дає. Таких URL 78 — третина беклогу, тож помилка тут коштувала б третини всієї ручної роботи.

Відповідно **дія в UI різна**: для `unknown`/`discovered` — «Submit in GSC», для `crawled_not_indexed` — «Review content, then submit», із посиланням на Page Inventory і Content QA цього URL. Рядок веде оператора до причини, а не просто в чергу.

Модифікатори (± до бази, не множники — щоб бал лишався читабельним у UI): глибина URL, наявність у `page_inventory_items` з чистим title/description, вік без індексації.

`reason_json` тримає **докази**, а не текст: `{"coverage_state": ..., "last_crawl_time": ..., "clicks_prev": 41, "clicks_now": 3, "changed_at": ...}`. UI будує речення з цього, а не зберігає готове речення.

**4. `plan`** (`app/indexing/plan.py`). Для кожного клієнта з `enabled=true`:

- **сьогодні заморожене.** Рядки зі `planned_for = today` не чіпаються взагалі — ні перестановки, ні видалення. Оператор не має бачити, як з-під рук зникає URL, який він щойно почав нести в GSC.
- дні `today+1 .. today+7` **перебудовуються щоночі повністю** за свіжим скором.
- далі 7-го дня — **прогноз, не рядки**: «за поточного темпу черга (N URL) пройдеться за M робочих днів; 90-денний цикл витримується / НЕ витримується».
- **cooldown** — жорсткий фільтр перед скором: URL із `last_submitted_at` молодшим за `resubmit_after_days` не потрапляє в план узагалі, хоч би який був бал.
- **склад дня**: `daily_quota` найвищих балів клієнта. Якщо кандидатів менше — день коротший; порожній день — це «черга вичерпана», а не помилка.

### 2.4 API (`/api/v1/tools/indexing`, мутації `member`+, читання `viewer`)

| Метод | Шлях | Що робить |
|---|---|---|
| `GET` | `/overview` | портфельне зведення: покриття індексом, розмір черги, темп, стан останнього скану |
| `GET` | `/clients` | клієнти + `configured`/`enabled` + квота + розмір черги + покриття |
| `PUT` | `/clients/{id}/settings` | `enabled`, `daily_quota`, `resubmit_after_days`, `scan_mode` |
| `GET` | `/today` | сьогоднішній план по всіх клієнтах (головний екран) |
| `GET` | `/calendar?days=8` | сітка «день × клієнт», сьогодні + 7 |
| `GET` | `/queue` | беклог із фільтрами (клієнт, `reason_code`, excluded) і сортуванням |
| `GET` | `/urls/{id}` | картка URL: стан, докази, уся історія відправок |
| `POST` | `/submissions/{id}/sent` | **відмітити відправленим** (ідемпотентно, див. §2.7) |
| `POST` | `/submissions/{id}/quota-blocked` | GSC відмовив по квоті — **без** cooldown, перенести на завтра |
| `POST` | `/submissions/{id}/undo` | скасувати помилкову відмітку (вікно 24 год) |
| `POST` | `/urls/{id}/exclude` / `/include` | ручне виключення з причиною |
| `POST` | `/scans` | ручний скан (клієнт або портфель) |
| `GET` | `/scans/{id}` | прогрес скану |
| `GET` | `/analytics` | воронка, лаг, контрольна група (§2.6) |
| `GET` | `/analytics.xlsx`, `/calendar.xlsx` | експорт |

Усе — під загальним `AuthMiddleware`; жодного нового винятку в allowlist. Мутації пишуться в журнал дій (task-72): нові дії `indexing.submission.sent`, `indexing.submission.undo`, `indexing.url.excluded`, `indexing.settings.updated`, `indexing.scan.started`.

### 2.5 Фронт: чотири вкладки, одна головна дія

`#/tools/indexing`, вкладки `Today` (за замовчуванням) · `Calendar` · `Backlog` · `Analytics`.

**Today** — робочий екран дня. Групування по клієнту, у кожній групі до `daily_quota` рядків:

| Колонка | Вміст |
|---|---|
| URL | клікабельний, із позначкою нової сторінки |
| Meta | title/description із `page_inventory_items` (порожнє — це `—`, не вигаданий текст) |
| Reason | бейдж `reason_code` + одне речення, зібране з `reason_json` |
| Evidence | розкривний рядок: coverage state, останній краул, кліки було/стало |
| Status | `Planned` / `Sent 14:20` / `Quota blocked` |
| Дії | `Copy URL` · `Open in GSC` · `Mark sent` |

`Open in GSC` веде на **збережений** `inspection_result_link`, а не на зібраний рядок. Перевірено живо: посилання має вигляд `…/inspect?resource_id=sc-domain:smashhouseburgers.com&id=YsE6BUOtgaXYGOLfUP9YAg`, де `id` — **непрозорий хеш**, а не URL сторінки. Побудувати його самотужки не можна; його віддає тільки `urlInspection`, і тому воно зберігається в `indexing_urls.inspection_result_link` на кожній інспекції. URL, який ще жодного разу не інспектували, показує кнопку в неактивному стані з підказкою, а не веде на зламане посилання.

Плюс кнопка групи **`Copy all 10`** — оператор носить пачку, а не по одному.

Прогрес дня: `7 of 10 sent` на групу і смужка по портфелю. Це і є «зручна таблиця з прогресом» із запиту.

**Calendar** — сітка 8 днів × клієнти, у клітинці кількість і зведення причин. Сьогоднішня колонка візуально зафіксована (заморожена), майбутні позначені як перебудовувані. Під сіткою — прогноз: «черга 412 URL, темп 64/день → повний прохід за 7 роб. днів; цикл повтору 90 днів витримується».

**Backlog** — уся черга з фільтрами по клієнту й причині, сортування за балом; окремий фільтр `Excluded` з причиною виключення і кнопкою повернення.

**Analytics** — §2.6.

Обов'язкове з `frontend/AGENTS.md`: усі шість станів (loading skeleton / empty / error / not_configured / partial / ok), жодного inline `style=` (CSP), кожен ES-import із власним `?v=` і піднятим ланцюжком до `index.html` ([[frontend-module-cache-bust-chain]]), уся копірайт-частина англійською.

### 2.6 Підмодуль аналітики: чи ручне індексування взагалі працює

Це найцінніша частина, і вона не зводиться до «послали тоді-то, отримали індекс тоді-то».

1. **Воронка** `planned → sent → indexed`, із втратами на кожному кроці (зокрема `quota_blocked` — скільки разів GSC відмовив).
2. **Лаг до індексації** — розподіл `days_to_index` (медіана + p25/p75, не лише середнє: одна сторінка, що зайшла через 60 днів, зсуває середнє й малює хибну картину).
3. **Контрольна група — ключова ідея.** Сторінки, які за той самий період перейшли в індекс **без** нашої відправки, у нас уже є: щоденний скан бачить кожен перехід. Порівняння двох медіан («відправлені — 6 днів, самостійні — 19») перетворює тул із журналу роботи на **доказ її корисності**. Якщо різниці немає — тул зобов'язаний це показати, а не сховати; тоді 100 ручних операцій на день варто припинити, і це буде найдорожчий висновок, який він дасть.
4. **Утримання** — скільки відсотків проіндексованих випали з індексу через 30/60/90 днів (`left_index_at`).
5. **Розріз по причинах** — який `reason_code` конвертується найкраще. Через квартал це перекалібрує ваги §2.3 на реальних даних, а не на здогадках.
6. **Per-client таблиця** — покриття індексом, черга, відправлено за 30 днів, медіанний лаг, чи витримується цикл.

Чесність вибірки: сторінка, відправлена 3 дні тому, ще **не** «не проіндексувалась» — вона в дорозі. Медіана рахується тільки по завершених спостереженнях, незавершені показуються окремим числом «ще в дорозі: N». Без цього метрика поїде вниз щоразу після великої відправки.

### 2.7 Пастки, які цей дизайн закриває свідомо

1. **Помилковий `Mark sent` ховає сторінку на 90 днів.** Тому: підтвердження на масову дію, `undo` протягом 24 год, і окрема кнопка `Quota blocked` — бо GSC після ~10–12 URL відмовляє, і це **не** відправка. Якби обидва випадки писались як `sent`, cooldown блокував би сторінку, яку насправді ніхто не подав.
2. **`indexed` із sitemap API** (§1.2) — не читається ніде.
3. **Синтетичні нулі GSC** ([[gsc-page-filter-fake-zeros]]): для `traffic_lost`/`no_traffic` `impressions == 0` означає відсутність, не нуль; `sc-domain` покриває `www`/голий хост — матчинг регексом із `re.escape`, не рядковою рівністю.
4. **Відсутній блок у відповіді ≠ «усе гаразд»** (task-88): `VERDICT_UNSPECIFIED` і відсутній `richResultsResult` лишаються `None` і показуються як «Google не сказав».
5. **Дві паралельні сесії Google API** — скан іде в одному потоці планувальника з пулом воркерів усередині; другий одночасний портфельний скан відсікається `owner`/`heartbeat_at` на `indexing_scans` (урок task-62).
6. **Тест, що пише в бойову БД** ([[global-test-singleton-db-leak]]): `IndexingScheduler` — процес-глобальний фоновий потік, тому вимкнений за замовчуванням у `tests/conftest.py`, вмикається маркером.
7. **Квота Google на URL Inspection** (2000/добу/property): лічильник у `indexing_scans`, при досягненні 80% скан зупиняється і позначає прогін `partial`, а не впирається в 429.

### 2.8 Планувальник

`IndexingScheduler` у `app/indexing/scheduler.py`, зареєстрований у `app/jobs/worker.py` поруч із п'ятьма наявними. Тік 60 с; раз на добу після `indexing_scan_at_utc` (default `02:00`) виконує повний цикл §2.3. Ідемпотентність — через `indexing_scans` (сьогоднішній успішний прогін уже є → нічого не робити), не через пам'ять потоку: рестарт воркера не має давати ні нуль, ні два прогони за добу (зразок — `app/cloud/scheduler.py`).

Нові налаштування в `app/core/config.py`: `indexing_enabled` (default `false`), `indexing_scan_at_utc`, `indexing_inspect_workers` (8), `indexing_plan_horizon_days` (7), `indexing_inspection_daily_cap` (1600 = 80% від 2000).

### 2.9 CLI

```
yovth-report indexing-scan [--client SLUG] [--full]
yovth-report indexing-plan --client SLUG [--date YYYY-MM-DD]
yovth-report indexing-export --client SLUG --out plan.xlsx
```

Для разових прогонів і для перевірки без UI.

---

## 3. Етапи

| # | Етап | Вихід | Перевірка |
|---|---|---|---|
| 1 | **Модель і міграція** — 4 таблиці, репозиторій, DTO | `app/db/models.py` (додано в кінець), нова ревізія, `app/schemas/indexing.py`, `app/db/repositories/indexing.py` | `alembic upgrade head` на копії бекапу, юніти репозиторію |
| 2 | **`sync_sitemap`** — GSC sitemaps → fetch → parse → реєстр | `app/indexing/sync.py` | живий прогін на GRP/SWM: биті sitemap-и дають `partial` із причиною, не тихий пропуск |
| 3 | **`inspect`** — пул воркерів, пріоритетні групи, кап квоти | `app/indexing/inspect.py` | повний скан LAXTON (173 URL) < 3 хв, лічильник квоти росте, 5 виключень розпізнано |
| 4 | **`score`** — виключення + `reason_code` + докази | `app/indexing/score.py` | на ALPHA: 83 кандидати розкладені по причинах, жодного `noindex` серед них |
| 5 | **`plan`** — заморожене сьогодні, 7 днів наперед, cooldown, прогноз | `app/indexing/plan.py` | повторний переплан не рухає сьогоднішній день; URL із відправкою 10 днів тому не з'являється |
| 6 | **API + audit** | `app/api/tools_indexing.py`, записи в `app/audit/actions.py` | contract-тести на всі ендпоінти, 403 для `viewer` на мутаціях |
| 7 | **Планувальник + config + CLI** | `app/indexing/scheduler.py`, рядок у `app/jobs/worker.py`, `app/cli.py` | два тіки поспіль дають один прогін; рестарт воркера не дублює |
| 8 | **Фронт: Today + Calendar** | `indexing-tool.js`, `indexing-calendar.js`, `indexing.css` | візуальний loop за `AGENTS.md §15`, усі шість станів, порожній день читається як «черга вичерпана» |
| 9 | **Фронт: Backlog + Analytics + XLSX** | `indexing-analytics.js`, `app/indexing/xlsx.py` | контрольна група рахується; «ще в дорозі» показано окремо від медіани |
| 10 | **Живий прогін end-to-end** | — | §4 Gate |

Етапи 2–5 незалежні від 6–9 за контрактом DTO — фронт кодується проти оголошених схем, не чекаючи бекенду (CLAUDE.md §3).

---

## 4. Definition of Done / Gate

**Функціонально**

1. `#/tools/indexing` відкривається з хаба, вкладка `Today` показує план на сьогодні по всіх увімкнених клієнтах.
2. Кнопка `Mark sent` фіксує відправку, `Undo` протягом 24 год її скасовує, `Quota blocked` **не** ставить cooldown і переносить URL на наступний день. Подвійний клік не створює двох записів.
3. URL, відправлений менш ніж `resubmit_after_days` тому, не з'являється в плані за жодного балу.
4. Календар показує сьогодні + 7 днів; нічний переплан **не змінює сьогоднішній день** (перевірено двома прогонами `plan` поспіль із порівнянням рядків).
5. Сторінки з `noindex`, `robots`-блоком, чужим каноніком, редиректом і не-2xx у чергу не потрапляють і видимі окремим фільтром `Excluded` із причиною.
6. Картка URL показує всю історію відправок із замороженою причиною кожної.
7. Аналітика дає воронку, медіану лагу з p25/p75, **контрольну групу** (проіндексовані без відправки) і «ще в дорозі» окремим числом.
8. Excel-експорт календаря й аналітики будується з того самого DTO, що й екран.
9. Клієнт без GSC-інтеграції (GOREPORT) і клієнт із `enabled=false` дають стан `not_configured` — не нуль, не помилка.

**Коректність (PLAN §12)**

10. `is_indexed IS NULL` ніде не читається як `false`; «ще не інспектували» видно в UI окремо від «не в індексі».
11. `sitemaps.contents[].indexed` не використовується в жодному розрахунку (grep по `app/indexing/` порожній).
12. `impressions == 0` у розрахунку `traffic_lost`/`no_traffic` трактується як відсутність; матчинг URL до GSC — регексом із `re.escape`, не рядковою рівністю.
13. Биті/недоступні sitemap-и роблять прогін `partial` із переліком URL, що впали, а не зменшують мовчки перелік сторінок.
14. Скан зупиняється на `indexing_inspection_daily_cap` і позначає прогін `partial`, не впираючись у 429.

**Безпека / інфраструктура**

15. Усі мутації — `member`+, `viewer` отримує 403; кожна мутація в журналі дій task-72.
16. `IndexingScheduler` вимкнений за замовчуванням у `tests/conftest.py`; прогін усієї сюїти не додає жодного рядка в бойові `indexing_*` (перевірено `count(*)` до й після).
17. `docker compose exec api poetry run ruff check .`, `mypy app`, `pytest tests/unit/indexing tests/unit/api/test_indexing_api.py` — зелені.

**Фронт (`frontend/AGENTS.md`)**

18. Чекліст `§15–16` пройдено: шість станів, жодного inline `style=`, ланцюжок `?v=` піднято до `index.html`, уся копірайт-частина англійською, таблиця не скролить сторінку (`contain: paint` на обгортці — [[table-scroll-contain-paint]]).

**Живий gate**

19. Прогін на ALPHA: скан → **83 кандидати** з причинами → план на 7 днів по 10 → відмітка 3 відправок → наступного дня скан бачить зміну стану й проставляє `indexed_at`.
20. Прогін на 3XE: **17 виключень** (12 redirect, 3 robots, 1 чужий канонік, 1 404) відфільтровані й у план не потрапили; **4 мертві sitemap-и** дають `partial` із переліком, а не тихо скорочений реєстр.
21. Прогін на SHB (99% покриття): черга порожня, календар показує «queue exhausted», жодного вигаданого рядка.
22. Прогін на SAPHNY: **112 кандидатів**, з них 47 `crawled_not_indexed` — усі отримують дію «Review content, then submit», а не «Submit» (§2.3).

---

## 5. Свідомо поза обсягом

- **Автоматична відправка на індексацію** — API не існує (§1.1). Якщо колись знадобиться IndexNow (Bing/Yandex), це окрема таска; Google його не використовує.
- **`sitemaps.submit`** — вимагає повного scope `webmasters`, який заразом відкриває `sites.delete`. Рішення 2026-09-09 не розширювати scope лишається чинним.
- **Портфельна кнопка «Send all»** — фізично неможлива й концептуально шкідлива: відправляє людина.
- **Автокалібрування ваг §2.3** — після кварталу реальних даних, на основі розрізу по `reason_code` (§2.6 п.5). Поки ваги — обґрунтована здогадка, і план це визнає.

---

## ✅ Completion report

- **Дата:** 2026-09-21
- **Гілка / комміт:** `feat/task-100-indexing-planner` @ (див. нижче)
- **Статус:** done-with-followups

### Що зроблено

Тул **Indexing Planner** (`#/tools/indexing`) — повний вертикальний зріз: конвеєр, API, планувальник, UI, CLI, Excel, тести.

- **Конвеєр** `sync_sitemap → inspect → score → plan` (`app/indexing/`). Перелік сторінок береться з sitemap-ів, зареєстрованих у GSC; стан індексації — з `urlInspection` у 8 потоків; кожен кандидат отримує причину з доказами і бал; план розкладається по днях у межах per-client квоти.
- **Чотири таблиці** (`indexing_settings`/`urls`/`submissions`/`scans`), дві міграції.
- **15 REST-ендпоінтів** + Excel-експорт, усі мутації в журналі дій.
- **UI з чотирма вкладками**: `Today` (робочий екран дня з `Copy`/`Open in GSC`/`Mark sent`), `Calendar` (8 днів, сьогодні заморожене), `Backlog` (черга + виключені + per-client налаштування), `Analytics` (воронка, лаг, контрольна група).
- **Планувальник** у worker (вимкнений за замовчуванням) і дві CLI-команди.

### Як зроблено (ключові рішення)

- **Тул нічого не відправляє.** API для запиту індексації не існує (§1.1), тому центральна дія — рішення й облік, а найнебезпечніша мутація — `Mark sent`, що ставить 90-денний cooldown. Звідси `Undo` на 24 год і **окрема** кнопка `Quota blocked`, яка cooldown не ставить: GSC відмовляє після ~10–12 URL на добу, і записати це як відправку означало б сховати сторінку, якої ніхто не подав.
- **`is_indexed` тризначний.** `NULL` («ще не інспектували») ніде не читається як `False`. Інакше непросканований портфель дав би 2311 фантомних кандидатів замість 274 реальних.
- **`crawled_not_indexed` скориться НИЖЧЕ за `discovered_not_indexed`** (55 проти 85). Це не смак: `Discovered` — Google ще не дійшов краулити, там відправка працює; `Crawled — not indexed` — уже подивився й не взяв, там потрібні правки контенту. Таких 78, третина беклогу, і рядок веде оператора в контент (`Review content, then submit`), а не в чергу.
- **Сьогодні заморожене на рівні `DELETE`.** `replace_future_plan` фізично не може зачепити `planned_for <= today` — умова в самому запиті, а не в викличнику.
- **Ізоляція клієнтів** (PLAN §9.1): виняток на одному не валить портфельний прогін; клієнт без GSC — `not_configured`.

Головні файли: `app/indexing/{sync,inspect,score,plan,service,analytics,scheduler,context,dto,xlsx}.py`, `app/api/tools_indexing.py`, `app/db/repositories/indexing.py`, `app/schemas/indexing.py`, `frontend/js/indexing-{tool,calendar,backlog,analytics}.js`, `frontend/css/indexing.css`.

**Відхилення від плану:** додано `excluded_reason="redirect"` окремо від `http_error` (редирект — не поламана сторінка, і оператора не варто слати лагодити те, що не зламане) і колонку `first_not_indexed_at` (див. нижче).

### Контрактні зміни (Contract changes)

Усе адитивне, нічого наявного не змінено:

- нові таблиці `indexing_settings`, `indexing_urls`, `indexing_submissions`, `indexing_scans` (міграції `a7f31c92e4d8`, `c8a2d5f10b73`);
- новий DTO-модуль `app/schemas/indexing.py` (нічого не імпортує з чужих схем);
- новий запис `indexing` у каталозі тулів (`app/api/tools.py`) і в `TOOL_MODULES`;
- сім нових дій у `app/audit/actions.py`;
- п'ять нових налаштувань у `app/core/config.py` (усі з дефолтами, `indexing_enabled=false`).

Споживачі: лише сам тул. `task-02` правити не довелось.

### Тести

64 нових: `tests/unit/indexing/` (score, plan, inspect/sync, submissions/analytics) + `tests/unit/api/test_indexing_api.py`.

Покривають саме ті інваріанти, які коштують найдорожче: типографські лапки в `Excluded by ‘noindex’ tag`, тризначність `is_indexed`, порядок `crawled` < `discovered`, заморожене сьогодні між переплануваннями, cooldown як фільтр за будь-якого балу, порожня черга як норма, ідемпотентність `Mark sent`, `quota_blocked` без cooldown, `undo` з відновленням попередньої відправки, контрольна група без уже-проіндексованих сторінок, `None` замість `0` на порожніх вибірках.
