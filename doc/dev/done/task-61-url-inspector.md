# task-61 — URL Inspector: один URL у шапці → все, що система про нього знає

- **Пакет:** глобальний пошук у шапці застосунку, куди оператор вставляє **будь-який URL сторінки клієнта** і отримує одну сторінку з усім: GSC/GA4 динаміка саме цієї сторінки, версії у Web Archive, зміни з Site Monitor, мета + копірайт-QA, і живий мікро-технічний аудит.
- **Хвиля:** V — URL Inspector (перша таска хвилі).
- **Depends on:** **16** ✅ (GSC page-dimension), **09** ✅ (`ga4_landing_pages`), **35/58** ✅ (monitor pages + fetcher/extractor), **57** ✅ (page-inventory QA), **56** ✅ (schema markup), **59** ✅ (Web Archive + `normalize_url`), **06** ✅ (REST-патерни), **25** ✅ (tech audit findings).
  - **task-60** (Archive assets) — **не** залежність, але **паралельна**: вона володіє `app/webarchive/**`. Ця таска туди **не пише жодного рядка**, лише імпортує `normalize_url`.
- **Owned paths:**
  - `app/urlinspector/**` (новий пакет)
  - `app/api/url_inspector.py` (новий роутер)
  - `app/schemas/url_inspector.py` (нова схема)
  - `frontend/js/url-inspector.js`, `frontend/css/url-inspector.css` (нові)
  - `tests/unit/urlinspector/**`, `tests/unit/api/test_url_inspector_api.py` (нові)
- **Адитивні дотики спільних файлів (узгодити, перед комітом перевірити `git status`):** `frontend/index.html` (поле пошуку в шапці + root-контейнер), `frontend/js/router.js` (+1 маршрут), `frontend/js/api.js` (+2 методи), `frontend/css/app.css` (стилі поля в шапці), `tests/unit/api/data/openapi_snapshot.json`.
- **Міграції:** **не потрібні.** Обґрунтовано вимірюванням у §0.5 — варіантний lookup по наявних індексах вкладається в одиниці-десятки мілісекунд, нова колонка `url_key` не окупається.
- **Нових залежностей — нуль.** Усе будується на `app/monitor/fetcher.py` + `app/monitor/page.py` + `app/webarchive/urls.py` + `app/contentqa`, які вже в образі.
- **PLAN.md:** §12 (`None` ≠ `0`, snapshot vs range, truncated), §13 (SSRF-гард на живому фетчі), §21.4 (розмірні дані GSC), §10 (форма REST).
- **frontend/AGENTS.md:** §5 (правда даних), §6 (обов'язкові стани), §11 (респонсив), §12 (CSP, vanilla JS), §14 (UI-копірайт англійською), §15–16 (візуальний loop + чекліст).

---

## Мета

Сьогодні дані про **одну сторінку** розкидані по шести місцях, і жодне з них не починається з питання «а що з оцим URL?»:

| Що оператор хоче знати про URL | Де це лежить сьогодні | Скільки кліків |
|---|---|---|
| Скільки трафіку з пошуку | `#/client/<id>` → таб Pages → знайти рядок | 4+ |
| Скільки сесій/конверсій з GA4 | `#/client/<id>` → Reporting Ninja → landing pages | 4+ |
| Як сторінка виглядала місяць тому | `#/archive` → знайти клієнта → знайти URL | 4+ |
| Що на ній змінилося | `#/client/<id>/monitor` → Pages → знайти рядок → Versions | 5+ |
| Чи в порядку title/description | `#/client/<id>/pages` → знайти рядок | 4+ |
| Чи вона взагалі індексується зараз | ніде — тільки живий curl | — |

Жодне з цих місць не приймає **URL** як вхід. Оператор завжди починає з клієнта, хоча в роботі він починає з посилання: його прислали в чаті, він у звіті, у таблиці правок.

**Після таски:** вставив URL у шапку → Enter → одна сторінка з усіма шістьма відповідями і чесно позначеним «цього джерела для цієї сторінки немає».

---

## §0. Досліди на бойових даних (виміряно 2026-09-05)

Не гіпотези. Усе нижче — прогони по реальній `app_data/app.db`: 15 клієнтів, 90 888 GSC page-рядків, 102 265 GA4 landing-рядків, 3 701 monitor-сторінка, 4 381 archive-URL, 14 233 monitor-зміни.

### §0.1 Скільки даних узагалі є на рівні URL

| Джерело | Рядків | Клієнтів | Одиниця |
|---|---|---|---|
| `gsc_dimension_metrics` (`dimension='page'`) | 90 888 | 10 | URL × період |
| `analytics_dataset_rows` (`ga4_landing_pages`) | 102 265 | 14 | landing path × період |
| `monitor_pages` | 3 701 | 14 | URL (поточний стан) |
| `monitor_page_versions` | — | — | історія стану URL |
| `monitor_changes` | 14 233 (3 739 унікальних `subject`) | — | подія на URL |
| `archive_urls` / `archive_snapshots` | 4 381 / 1 625 | 12 | версії сторінки |
| `page_inventory_items` | 986 | 3 | мета + QA-findings |
| `analytics_dataset_rows` (`tech_findings`) | 10 081 | — | `slug\x1fURL` |
| `alt_text_items` | 400 (210 сторінок) | — | картинка на сторінці |
| `schema_markup_checks` | 4 | — | перевірка URL |

Дані є, і їх багато. Проблема не в даних — у відсутності входу «за URL».

### §0.2 ⚠️ Головна знахідка: наївний ключ URL руйнує 96 % матчів

`app.webarchive.urls.normalize_url` (scheme геть, `www.` геть, tracking-параметри геть) — правильна основа, але **не нормалізує кінцевий слеш**. А GSC і GA4 віддають той самий шлях **по-різному**:

```
GSC : https://www.nysapphire.com/ny-sapphire-gentlemens-club-locations/sapphire-times-square/
GA4 : /ny-sapphire-gentlemens-club-locations/sapphire-times-square
```

Перетин множин URL між GSC і GA4 на тому самому клієнті:

| Клієнт | `normalize_url` як є | + нечутливість до кінцевого слеша | Приріст |
|---|---|---|---|
| SAPHNY | **48** | **552** | ×11.5 |
| 3XE | **4** | **690** | ×172 |
| HWELL | 11 | 326 | ×29 |
| SDS | 2 | 358 | ×179 |
| 19CLV | 3 | 100 | ×33 |
| SHB | 5 | 136 | ×27 |

**Без цієї нормалізації модуль показував би «GA4: немає даних» майже на кожній сторінці.** Це не косметика — це різниця між робочим і фейковим модулем.

Колізії від злиття `/x` і `/x/` виміряно окремо: максимум **35 на 859 GSC-URL** (3XE), решта клієнтів 0–15. Дивилися, що саме злипається — це `http://` vs `https://` і `/x` vs `/x/` **тієї самої сторінки**, тобто рівно те злиття, якого ми й хочемо. Жодного випадку двох різних сторінок під одним ключем не знайдено.

### §0.3 ⚠️ Другий капкан: один період має до 12 конкуруючих рядків

Розподіл кількості рядків на один `(client, url, period_start, period_end)` у `gsc_dimension_metrics`:

| Рядків на період | Скільки таких груп |
|---|---|
| 1 | 44 466 |
| 2 | 5 761 |
| 3 | 4 652 |
| 4–7 | 502 |
| 8–12 | 1 995 |

І значення в них **різні** — GSC дозріває заднім числом:

```
2026-07-20..07-26  clicks=396  observed_at 2026-07-27 10:12
2026-07-20..07-26  clicks=396  observed_at 2026-07-27 10:15
2026-07-20..07-26  clicks=470  observed_at 2026-07-29 09:03   ← +19 % за два дні
2026-07-20..07-26  clicks=470  observed_at 2026-08-03 08:24
```

**Наслідок:** серію не можна будувати простим `SELECT ... ORDER BY period_start`. На кожен період беремо рядок **останнього за `created_at` снапшота** — рівно те, що вже робить `DimensionRepository.for_period` для одного періоду ([dimensions.py:75](app/db/repositories/dimensions.py#L75)). Ця таска узагальнює той самий принцип на серію.

### §0.4 ⚠️ Третій капкан: періоди різної довжини лежать в одній таблиці

Гістограма довжин періодів (днів) серед збережених:

| Довжина | GSC page | GA4 landing |
|---|---|---|
| 5 | — | 2 |
| **7** | **8** | **8** |
| **14** | **13** | **13** |
| 26 | 2 | 2 |
| **30/31** | **12** | **12** |
| 90/91 | 6 | 8 |
| 365/366 | 1 | 2 |

Для однієї головної сторінки SAPHNY це 24 семиденні точки, 16 чотирнадцятиденних, 14 місячних, 4 квартальні, 3 річні — **разом 63 «точки», які категорично не можна класти на одну лінію**: 7-денні 400 кліків поруч із 30-денними 1 900 намалюють неіснуючий стрибок ×5.

**Рішення:** графік має **перемикач гранулярності** (Weekly / Monthly / Quarterly), кожна серія будується **лише** з періодів своєї довжини, а сама довжина підписана на осі. Це прямий наслідок PLAN §12 («snapshot- і range-метрики не порівнюються однаково»), поширений на range-метрики різної довжини.

### §0.5 Чому міграція не потрібна (виміряно)

Замість нової колонки `url_key` + індексу генеруємо **8 варіантів** URL (`{http,https} × {host, www.host} × {path, path/}`) і робимо `IN (...)`:

| Запит | Час |
|---|---|
| `gsc_dimension_metrics` по 8 варіантах (90 888 рядків, без `client_id`) | **66.6 ms** |
| `monitor_pages` (UNIQUE `client_id,url`) | **1.4 ms** |
| `archive_urls` (UNIQUE `client_id,url_key`) | **0.8 ms** |
| `monitor_changes` по `subject` | **2.3 ms** |

66 ms — це найгірший випадок **без** звуження по `client_id`. Клієнта ми резолвимо першим кроком (§0.6), тож реальний запит іде по індексу `ix_gsc_dimension_metrics_client_dimension_period`. Міграція заради цього не окупається — а кожна міграція на цій SQLite-базі має власну ціну (CLAUDE.md, інцидент 2026-09-01).

### §0.6 Клієнт резолвиться по хосту, включно з піддоменами

```sql
select * from clients where ? = domain or ? like '%.' || domain
```

| Хост | Результат |
|---|---|
| `www.nysapphire.com` | SAPHNY |
| `blog.nysapphire.com` | SAPHNY ✅ (у GSC цього клієнта реально є `blog.` URL) |
| `shop.alphastyle.store` | ALPHA |
| `unknown.com` | нічого → чесний стан «not a client domain» |

### §0.7 Що для URL є, а чого немає — чесна матриця покриття

Проміряно по всьому портфелю (кількість URL, відомих кожному джерелу, після нормалізації §0.2):

| Клієнт | Усього URL | GSC | GA4 | Monitor | Archive | ≥2 джерела |
|---|---|---|---|---|---|---|
| 3XE | 1 466 | 859 | 1 290 | 642 | 645 | 781 |
| SAPHNY | 2 095 | 1 193 | 1 334 | 528 | 670 | 690 |
| ALPHA | 1 251 | **0** | 746 | 519 | 886 | 519 |
| LAXTON | 1 313 | **0** | 847 | 173 | 639 | 176 |
| GOREPORT | 273 | **0** | 191 | 228 | 244 | 231 |
| SDS | 1 079 | 392 | 996 | 369 | 441 | 421 |
| PJC | 557 | 162 | 490 | **0** | **0** | 95 |
| HWELL | 707 | 428 | 602 | 248 | 265 | 335 |

**Висновок для UI:** «джерело порожнє» — це **норма, а не помилка**. Три клієнти взагалі без GSC, один без монітора й архіву. Кожна панель мусить мати окремий, різний стан: `no data for this URL` (джерело працює, сторінки в ньому немає) ≠ `not configured` (інтеграції в клієнта немає) ≠ `failed` (останній збір упав). Змішати їх — прямо порушити PLAN §12.

### §0.8 Обсяг даних на одну сторінку (щоб UI не захлинувся)

Для `https://www.laxton.se/ghost-tracker-emf-evp-recorder-app/`:

- 24 записи в `monitor_changes` (з них `page_noindex_appeared` × 10 і його `_resolved` × 10 — флапінг),
- 22 версії в `monitor_page_versions`,
- 1 знімок в архіві,
- 63 періодні точки в GSC (для головної SAPHNY).

Тобто панелі треба **згортати за замовчуванням і пагінувати**: перші 10 змін, перші 10 версій, решта — «Show all».

---

## §1. Що будуємо

### §1.1 Вхід — поле в шапці

`frontend/index.html`, у `<header class="app-header">`, після `<nav class="app-nav">`:

- `<form>` з `<input type="url" id="url-inspector-input" placeholder="Paste a page URL">` і сабмітом.
- Видиме на **всіх** маршрутах (на відміну від `#dashboard-header-actions`, який ховається поза дашбордом) — це і є сенс глобального пошуку.
- На вузьких екранах згортається в кнопку-іконку, що розкриває поле (AGENTS §11).
- Enter → `location.hash = buildUrlInspectorHash(url)`.
- Порожній/невалідний ввід → інлайн-помилка під полем, **без** переходу.

### §1.2 Маршрут і сторінка

`#/url?u=<encoded>` → `frontend/js/url-inspector.js`, `initUrlInspectorPage(query)`.

URL сторінки повністю описує стан (AGENTS §12): `u` (обов'язковий), `g` (гранулярність графіка: `week|month|quarter`, дефолт `week`), `tab` (якщо панелі стануть табами на мобільному).

### §1.3 Панелі (порядок згори вниз)

1. **Identity** — резолвлений клієнт (логотип + назва + лінк на `#/client/<id>`), хост, нормалізований ключ, і **рядок чипів покриття**: `GSC · GA4 · Monitor · Archive · Inventory · Tech audit`, кожен у стані `has data` / `no data` / `not configured`.
2. **Search & analytics** — один Chart.js-графік із перемикачем метрики (Clicks / Impressions / Position / CTR / Sessions / Users / Conversions) і перемикачем гранулярності (§0.4). Під ним — таблиця останнього періоду з дельтою до попереднього того ж розміру.
3. **Web Archive** — таймлайн знімків: дата, HTTP-статус, `store_reason`, зміна ваги; лінки на replay (`#/archive/view?snapshot=…`) і на diff між сусідніми.
4. **Monitor** — поточний стан сторінки (status, indexable, canonical, hop count, last checked) + стрічка змін із бейджами severity + лінк на diff версії.
5. **Meta & copy** — title / description / h1 / keywords з довжинами і QA-findings із `app.contentqa` (через той самий прохід, що `app/pageinventory/qa.py`, але на одному рядку).
6. **Micro tech audit** — **живий** фетч на вимогу (кнопка «Run live check»), результат не персистується в нову таблицю (див. §2.5).

Кожна панель має власні loading / empty / not-configured / error стани і рендериться **незалежно** — повільний живий фетч ніколи не блокує показ збережених даних.

---

## §2. Архітектура

### §2.1 `app/urlinspector/urlkey.py` — один ключ на всі джерела

```python
def inspector_key(url: str) -> str:
    """`normalize_url` + нечутливість до кінцевого слеша (§0.2)."""

def lookup_variants(url: str) -> list[str]:
    """8 форм для `IN (...)`: {http,https} × {host, www.host} × {path, path/} (§0.5)."""

def ga4_landing_variants(url: str) -> list[str]:
    """GA4 зберігає ШЛЯХ, не URL: ['/x', '/x/'] (+ '/' для кореня)."""
```

`normalize_url` **імпортується** з `app.webarchive.urls`, не копіюється: task-60 паралельно працює в тому пакеті, і форк ключа означав би два різні уявлення про «та сама сторінка» вже за тиждень.

⚠️ `normalize_url` **не чіпаємо** — `keep_query=True` там правильний (LAXTON має реальні `?taxonomy=` сторінки, task-59 §2). Наша обгортка знімає **лише** кінцевий слеш шляху і **лише** для матчингу.

### §2.2 `app/urlinspector/resolve.py` — URL → клієнт

- Валідація: тільки `http(s)`, є хост. Інакше `invalid_url`.
- Клієнт по `§0.6`. Не знайдено → `client_id=None`, і модуль усе одно віддає те, що не потребує клієнта (живий аудит), із чесним `client_match: "unknown_domain"`.
- Кілька клієнтів на хост неможливо (`clients.domain` UNIQUE), але порядок детермінований: точний збіг домену перед піддоменним.

### §2.3 `app/urlinspector/sources/` — по файлу на джерело

`gsc.py`, `ga4.py`, `monitor.py`, `archive.py`, `inventory.py`, `techaudit.py`. Кожен експортує одну функцію `collect(session, ctx) -> <PanelDTO>` і **сам** відповідає за свій стан «даних немає». Жоден не кидає назовні — виняток усередині джерела стає `error` того джерела, решта панелей живі (той самий принцип ізоляції клітинки, PLAN §9.1).

**`gsc.py` і `ga4.py` — серія періодів:**
1. Знайти всі `(period_start, period_end)`, де цей URL присутній.
2. Згрупувати за довжиною: `7 → weekly`, `28..31 → monthly`, `89..92 → quarterly`, решта → `other` (не показується на графіку, але лічиться в «X more periods of other lengths»).
3. У кожній групі на кожен період узяти рядок **останнього** снапшота (§0.3).
4. Період, де URL відсутній, а сам період існує з `quality='truncated'` → точка **`null` з причиною `truncated`**, ніколи `0` (PLAN §12). Період, де URL відсутній і набір `complete` → чесний `0`.

Останнє — найтонше місце всієї таски: 570 570 з 686 013 GSC-рядків мають `quality='truncated'`, тобто «сторінки немає в топ-N» — типова, а не рідкісна ситуація.

**`monitor.py`:** `monitor_pages` по варіантах → версії → `monitor_changes` по `subject` у варіантах (додатково перевіряємо `related_id = page.id` як другий шлях, бо `subject` історично писався і з `www.`, і без).

**`archive.py`:** `archive_urls` по `url_key` (там уже нормалізовано) + по варіантах, далі `archive_snapshots` за `observed_at`. Лінки будуються на **наявні** ендпойнти `app/api/archive.py` — жодного нового коду в архіві.

**`inventory.py`:** `page_inventory_items` по варіантах; якщо рядка немає — падаємо на `monitor_pages` (title/description/h1 там теж є) і позначаємо джерело мета-даних у відповіді (`meta_source: "inventory" | "monitor" | "live"`), бо оператор мусить бачити, наскільки свіжа мета.

**`techaudit.py`:** `analytics_dataset_rows` з `dataset_key IN ('tech_findings','ahrefs_affected_urls')` останнього кроулу клієнта, фільтр по `metrics_json.url` у варіантах (`dimension_value` має форму `slug\x1fURL`, §0.1). 10 081 рядок на портфель — фільтрація в Python після звуження по `client_id + dataset_key` (індекс `ix_analytics_dataset_client_key_period`).

### §2.4 `app/urlinspector/live.py` — мікро тех аудит

Один SSRF-гардований фетч через **наявний** `app.monitor.fetcher.fetch` (він уже вміє throttle по хосту, cap розміру, класифікацію антибот-блоку) + `app.monitor.page.extract_signals` / `is_indexable`. Віддає:

- ланцюг редиректів (кожен хоп: URL + код), фінальний URL і статус;
- `indexable` (`None` = не змогли визначити, ніколи не `False` за замовчуванням);
- title / description / h1 / canonical / meta robots / X-Robots-Tag / word count / hreflang;
- типи JSON-LD (`extract_signals` уже їх дістає) + повний вердикт schema-markup через `app.tools.schema_markup.service.run_check`, **якщо** оператор натиснув окрему кнопку (це другий фетч — не робимо його мовчки);
- QA-findings на живих title/description (той самий `app.contentqa`).

**Блок антибота** (`classify_block`) → чесний стан `blocked`, а не «сторінка зламана». ALPHA стабільно віддає 429 з Cloudflare (task-50) — на ньому це буде видно щодня.

### §2.5 Чому живий результат не персистується

Спокуса завести `url_inspector_checks` є, і ми свідомо від неї відмовляємось:

- це була б **сьома** таблиця з мета-сигналами сторінки поруч із `monitor_pages`, `monitor_page_versions`, `archive_snapshots`, `page_inventory_items`, `schema_markup_checks`, `analytics_dataset_rows` — і жодна з них не стала б від цього правдивішою;
- монітор **уже** веде історію тих самих полів із версіями і дифами; дублювати її ad-hoc знімками означало б два конкуруючі уявлення про «як сторінка виглядала 3 вересня»;
- користувачева вимога — **побачити**, а не накопичити.

Живий аудит лишається ефемерним. Якщо оператору треба зафіксувати стан — поруч є кнопка «Check with monitor» (наявний `POST /clients/{id}/monitor/pages/{page_id}/check`, task-35), яка запише справжню версію в справжню історію. Schema-markup перевірка — виняток: вона персистується, бо для цього **вже є** `schema_markup_checks` і власний ендпойнт (task-56), ми його просто викликаємо.

### §2.6 API

```
GET  /api/v1/url-inspector?url=<encoded>[&granularity=week|month|quarter]
     → UrlInspectorDTO  (усі панелі з БД, один запит, ~10 SELECT-ів)

POST /api/v1/url-inspector/live   {"url": "...", "with_schema": false}
     → UrlInspectorLiveDTO  (живий фетч; окремий виклик, бо повільний і мережевий)
```

Два виклики, а не один: збережені дані мусять з'явитися миттєво, живий фетч — коли приїде (до 30 с на антибот-хостах). Фронт малює панелі 1–5 одразу, панель 6 — після кнопки.

**Ліміти:** `url` ≤ 2048 символів (як колонки в БД); live-ендпойнт throttle-иться наявним `HostThrottle` з `app/tools/http_safety.py`.

**Секрети:** живий фетч не логує заголовки відповіді цілком і не віддає `Set-Cookie` (PLAN §13) — беремо ту саму фільтрацію, що `app/webarchive/capture.py` уже застосовує перед записом `headers_json`.

---

## §3. Схеми (`app/schemas/url_inspector.py`)

Нові DTO, **нічого наявного не змінюємо** — тому це не контрактна зміна для task-02:

- `UrlInspectorDTO` — `input_url`, `resolved`, `client`, `coverage[]`, `search_analytics`, `archive`, `monitor`, `meta`, `tech_audit`, `generated_at`.
- `UrlSourceStateDTO` — `source`, `state: has_data | no_data | not_configured | failed`, `detail`.
- `UrlSeriesPointDTO` — `period_start`, `period_end`, `period_days`, `value: Decimal | None`, `absence_reason: truncated | not_collected | null`, `quality`, `observed_at`.
- `UrlSeriesDTO` — `metric`, `granularity`, `points[]`, `truncated_periods`, `other_length_periods`.
- `UrlArchiveSnapshotDTO`, `UrlMonitorStateDTO`, `UrlChangeDTO`, `UrlMetaDTO`, `UrlFindingDTO`, `UrlTechFindingDTO`.
- `UrlInspectorLiveDTO` — `final_url`, `http_status`, `redirect_chain[]`, `blocked`, `indexable`, `signals`, `findings[]`, `schema_check_id | None`, `error`.

Провайдери/метрики звуться **точно** як у решті застосунку (CLAUDE.md: «одне ім'я — одна річ»): `gsc`, `reporting_ninja`, `clicks`, `impressions`, `ctr`, `position`, `sessions`, `users`, `conversions`, `engagement_rate`, `views`.

---

## §4. Фронтенд

- `frontend/js/url-inspector.js` — рендер сторінки, малі state-driven функції, жодного `innerHTML` з недовіреним текстом (URL і title приходять із чужих сайтів → тільки DOM API / екранування).
- `frontend/css/url-inspector.css` — токени наявні, нових кольорів не заводимо.
- **CSP:** ані `style="…"` в розмітці, ані `element.style.top/left` з JS (двічі обпікалися: task-36, task-43). Позиціювання — класами.
- **Chart.js** уже вендорений і підключений глобально; беремо патерн із `frontend/js/trends-chart.js`.
- `api.js`: `getUrlInspector(url, granularity)` і `runUrlInspectorLive(url, opts)` з **явними іменованими** request-key (`"url-inspector"`, `"url-inspector-live"`), а не похідними від параметрів — інакше зміна гранулярності не скасує попередній запит (наступали двічі, memory `task36_site_monitor_ui_done`).
- Порожня панель показує **причину**, а не порожній прямокутник: `No Search Console data for this URL` / `Search Console isn't connected for this client` — різні рядки (§0.7).
- `null` ніколи не рендериться як `0`; розрив у серії — розрив на графіку (`spanGaps: false`), із приміткою «N periods hidden: page outside top-N».

---

## §5. Тести

**Unit (`tests/unit/urlinspector/`):**
- `test_urlkey.py` — реальні пари з §0.2 (`https://www.x.com/a/` vs `/a`), збереження query, tracking-параметри, корінь, порт, юнікод-шлях.
- `test_resolve.py` — точний домен, піддомен, чужий домен, невалідна схема, `javascript:`.
- `test_series.py` — **ключовий**: фікстура з двома снапшотами на один період (§0.3) → береться новіший; змішані довжини (§0.4) → розкладаються по групах; `truncated`-період → точка `None` з причиною, **не** `0`.
- `test_sources_isolation.py` — виняток в одному джерелі не валить відповідь; решта панелей віддаються.
- `test_live.py` — редирект-ланцюг, антибот-429 → `blocked`, `Set-Cookie` не потрапляє у відповідь.

**API (`tests/unit/api/test_url_inspector_api.py`):** щасливий шлях, невідомий домен, URL без даних у жодному джерелі, невалідний URL → 400 з `error_code`, оновлення `openapi_snapshot.json`.

**E2E/візуальний loop:** Node-Playwright харнес (`.cjs`/`require` — memory `frontend_verification_tooling`), скріншоти desktop+mobile, стани: повний / без GSC / без монітора / невідомий домен / живий аудит блокований.

---

## §6. Definition of Done / Gate

- [ ] Поле в шапці видиме на всіх маршрутах, працює з клавіатури, має видимий фокус і доступне ім'я.
- [ ] `#/url?u=…` відновлює повний стан після перезавантаження і back/forward.
- [ ] **Живий гейт на реальних даних** — пройдено вручну на трьох URL із різним профілем покриття:
  - `https://www.nysapphire.com/ny-sapphire-gentlemens-club-locations/sapphire-times-square` — GSC ✅ + GA4 ✅ + Archive ✅, Monitor ❌ (сторінки в моніторі немає) → жодна панель не бреше;
  - будь-яка сторінка ALPHA — GSC **не сконфігуровано**, живий фетч віддає антибот-`blocked` → два різні чесні стани, не «помилка»;
  - `https://www.laxton.se/ghost-tracker-emf-evp-recorder-app/` — 24 зміни й 22 версії монітора → пагінація, а не стіна.
- [ ] GSC- і GA4-серії на тому самому URL показують **однакову кількість точок** для однакової гранулярності (доказ, що ключ §0.2 працює в проді, а не лише в тестах).
- [ ] Жодного `0` там, де насправді `None`; кожен розрив підписаний причиною.
- [ ] `ruff check .`, `mypy app`, `pytest tests/unit/urlinspector tests/unit/api/test_url_inspector_api.py` — зелені **в Docker**.
- [ ] Чекліст `frontend/AGENTS.md §16` пройдено, before/after скріншоти переглянуто.
- [ ] Нуль правок у `app/webarchive/**`, `app/monitor/**`, `app/api/archive.py`, `app/api/monitor.py` (лише імпорти).
- [ ] Перед комітом — `git status` на п'ятьох спільних файлах (§ Owned paths): якщо там чужі незакомічені зміни, коміт спільної частини відкладається (memory `multi_agent_collision_avoidance`).

---

## §7. Свідомо поза межами таски

- **Пошук за фрагментом URL / за назвою сторінки** — тут тільки повний URL. Автодоповнення по `archive_urls`/`monitor_pages` — окрема таска, коли стане зрозуміло, що операторові його бракує.
- **Історія інспекцій** («що я дивився вчора») — не персистуємо (§2.5).
- **Порівняння двох URL поруч** — цінна ідея, але це інший екран.
- **Денна крива для однієї сторінки** — **неможлива за даними**: `ga4_daily`/`gsc_daily` не мають розбивки по URL, а page-дименсія збирається періодами. Обіцяти денний графік було б брехнею; підпис осі каже, що точка = період.
- **Запис живого аудиту в історію** — робиться наявною кнопкою монітора (§2.5).

---

## ✅ Completion report

- **Дата:** 2026-09-05
- **Гілка / комміт:** `feat/task-61-url-inspector` @ 3d4a3e1 (база)
- **Статус:** done

### Що зроблено

Пошук за URL у шапці застосунку (видимий на всіх маршрутах) → `#/url?u=<url>&g=week|month|quarter` → одна сторінка з усім, що система знає про цей URL:

- **Search analytics** — окремі GSC- і GA4-серії саме цієї сторінки з перемикачем гранулярності (week/month/quarter) і метрики; таблиця останнього/попереднього періоду.
- **Web Archive** — таймлайн знімків (статус, `store_reason`, бейдж «Changed», лінк на replay).
- **Site Monitor** — поточний стан (status/indexable/canonical/last_checked) + стрічка змін (з severity) + версії, з пагінацією «показано N з M».
- **Meta & copy** — title/description/h1/keywords + QA-findings, з позначкою джерела (`page_inventory` чи `monitor`).
- **Technical audit** — findings останнього кроулу, що стосуються саме цього URL.
- **Live micro audit** — ланцюг редиректів + повні сигнали сторінки (title/description/h1/canonical/robots/hreflang/JSON-LD/word count) + indexable + QA-findings, на вимогу (кнопка), опційно з persisted schema-markup перевіркою.

Усі шість джерел ізольовані: збій одного джерела → стан `failed` лише для нього, решта панелей рендеряться нормально.

### Як зроблено (ключові рішення)

- **Головні файли:**
  - `app/urlinspector/urlkey.py` — trailing-slash-insensitive ключ + варіантний lookup (8 форм URL) + GA4 path-варіанти.
  - `app/urlinspector/resolve.py` — валідація вхідного URL + резолв клієнта по домену/піддомену.
  - `app/urlinspector/series.py` — спільна логіка «останній снапшот на період» + групування по довжині періоду (тиждень/місяць/квартал), використовується і GSC, і GA4.
  - `app/urlinspector/sources/{gsc,ga4,monitor,archive,inventory,techaudit}.py` — по джерелу, кожен повертає власний `UrlSourceStateDTO` + дані.
  - `app/urlinspector/metaqa.py` — спільний QA-прохід (title/description/h1) для stored-даних і live-фетчу, обидва через `app.pageinventory.qa.run_page_inventory_qa`.
  - `app/urlinspector/live.py` — живий мікро-аудит: `app.tools.redirects.trace.trace_redirects` (ланцюг редиректів + вердикт, включно з `blocked`) + `app.monitor.fetcher.fetch`/`app.monitor.page.extract_signals` (повні сигнали) — два незалежні, вже наявні, SSRF-гардовані виклики, не переписані заново.
  - `app/urlinspector/service.py` — оркестратор з ізоляцією джерел (try/except на кожне, ніколи не валить решту).
  - `app/api/url_inspector.py` — `GET /api/v1/url-inspector`, `POST /api/v1/url-inspector/live`.
  - `app/schemas/url_inspector.py` — нові DTO (не контрактна зміна: нічого наявного не редаговано).
  - `frontend/js/url-inspector.js` + `frontend/css/url-inspector.css` — сторінка.

- **Дотримано плану §2.1–§2.6 без відхилень**, окрім:
  1. Метод-рівня додаток до `app/db/repositories/monitor.py` (`get_page_by_url_variants`, `changes_for_subjects`) — модуль сам заявляє «єдине місце, що торкається `monitor_*`», тож варіантний lookup зроблено там, а не сирим запитом у своєму пакеті. Дописано в кінець файлу з коментарем `# --- Added by task-61 ---`, тим самим патерном, що вже використовували task-21/task-59 для чужих репозиторіїв. Торкнуто лише хвіст файлу (task-58 паралельно редагує середину `pages_in_ring_order`, конфлікту немає).
  2. `app/webarchive/**` і `app/db/repositories/web_archive.py` — **нуль правок**, як і обіцяно; варіантний lookup для архіву зроблено підбором 1-2 кандидатних `url_key` через наявний публічний `find_url()`, а не новим методом.
  3. Live-персистенція schema-markup (§2.4/§2.5) спрощена: вона персистується через наявний `SchemaMarkupRepository.create_check`, але JSON-шейпінг `generated`/`raw_markup` не переписаний з приватних хелперів `app/api/tools_schema_markup.py` — збережено лише `findings`/`verdict`/`found_types` (основна обіцянка плану), `generated=None`/`raw_markup=[]`. Задокументовано як follow-up нижче.

### Живі знахідки під час імплементації (не в первинному плані)

- **Header-шапка не мала запасу ширини** для 4-го flex-елемента: на 1440px між `.app-nav` і `.header-actions` було рівно 24px (сам flex-gap), і нове поле пошуку колапсувало до 0px ширини. Виправлено додаванням `flex-wrap: wrap` на `.app-header` (адитивно, `app.css`) — на дашборді (де є `header-actions`) кнопки «Run audits»/«Refresh data» переносяться на власний рядок, коли не вистачає місця; на решті маршрутів (без `header-actions`) усе в один рядок.
- **CSP заблокував усі inline `style="--state-color:…"`** (`default-src 'self'`, немає `style-src`) — усі тон-кольори (чіпи покриття, рядки змін/findings, стан-панелі) рендерились без кольору взагалі, без жодної помилки в DOM, лише в консолі браузера. Виправлено класами `.ui-tone-{success,warning,danger,info,dim,accent}`, обов'язково **в кінці** стилю (та сама специфічність, що й базові правила — перемагає порядок, не специфічність).
- **`.metric-table td` жорстко ставить `white-space: nowrap`** — `overflow-wrap: anywhere` на довгих URL/description-значеннях мовчки ігнорувався, і вузький екран (390px) скролив усю сторінку вбік. Виправлено `td.url-inspector__wrap { white-space: normal; ... }` (та сама специфічність, пізніший порядок) + `.url-inspector__table-scroll { overflow-x: auto; contain: paint }` навколо кожної таблиці (той самий фікс, що і `.pi-table-scroll`, task-57).
- **`ResolvedClientDTO.logo_path`** мав містити РЕЛЯТИВНИЙ шлях зі сховища (`client.logo_path` напряму) — 404 на реальному клієнті; виправлено на `f"/api/v1/clients/{id}/logo"`, як роблять `app/api/dashboard.py`/`app/api/clients.py`.
- **`InventoryRow.description`/`title`/`h1` мають бути `""`, не `None`**, коли сторінку перевірено, а тег відсутній (конвенція `app/pageinventory/inventory.py`) — без цієї конвертації в `app.urlinspector.metaqa.qa_findings` перевірка «тег відсутній» мовчки не спрацьовувала (0 findings замість реальних). Виправлено коерсією `title or ""` тощо.
- **Невалідний scheme без `://`** (`javascript:alert(1)`) проходив повз наївну перевірку `"://" in candidate`; виправлено регексом-детектором схеми (`^[a-zA-Z][a-zA-Z0-9+.\-]*:`), той самий підхід, що `app.tools.redirects.input`.

### Контрактні зміни (Contract changes)

Немає змін у спільних контрактах (`app/integrations/contract.py`, `app/schemas/adapter.py`, `app/schemas/api.py`, `app/schemas/enums.py`). Новий `app/schemas/url_inspector.py` — власна схема таски, за тим самим прецедентом, що і task-56/57/59.

Додатковий, задокументований дотик поза owned paths: два нових read-методи в `app/db/repositories/monitor.py` (див. вище) — сумісний, адитивний, не міняє жодної наявної сигнатури.

### Як перевірити (verification)

```bash
docker compose exec api poetry run ruff check app/urlinspector app/api/url_inspector.py app/schemas/url_inspector.py app/db/repositories/monitor.py
docker compose exec api poetry run mypy app/urlinspector app/api/url_inspector.py app/schemas/url_inspector.py app/db/repositories/monitor.py
docker compose exec api poetry run pytest tests/unit/urlinspector tests/unit/api/test_url_inspector_api.py -q
docker compose exec api poetry run pytest tests/unit -q   # повний unit-набір — без регресій від цієї таски

NODE_PATH=/tmp/yovth-e2e/node_modules BASE_URL=http://localhost:6524 CHROMIUM_PATH=/snap/bin/chromium \
  node tests/e2e/frontend/url-inspector.cjs
```

Живий гейт (реальні дані, 2026-09-05):
- `https://www.nysapphire.com/` (SAPHNY) — усі 6 джерел `has_data`, GSC/GA4 серії з однаковою кількістю точок на тижневій гранулярності (4 точки кожна), реальні QA-findings (title 66 символів/стеля 60, description 117/поріг 140).
- `https://www.laxton.se/ghost-tracker-emf-evp-recorder-app/` (LAXTON) — GSC `not_configured` (клієнт справді без GSC), монітор: 24 зміни / 22 версії (точно як у дослідженні §0.8), пагінація «10 з 24»/«10 з 22» працює.
- `https://unrelated.com/a` — `client_match: unknown_domain`, `client: null`, HTTP 200 (не помилка).
- Живий мікро-аудит на `https://www.alphastyle.store/` — реальний фетч, реальні сигнали (title/canonical/word_count/JSON-LD типи), реальний QA-finding (`meta-desc-banned-opener`).

### Тести

- `tests/unit/urlinspector/test_urlkey.py` — trailing-slash fold, варіанти lookup (8 форм), GA4 path-варіанти, реальна пара з портфеля (§0.2).
- `tests/unit/urlinspector/test_resolve.py` — валідація URL (порожній/задовгий/unsupported scheme/без хоста), резолв exact/subdomain/unknown, пріоритет exact над subdomain.
- `tests/unit/urlinspector/test_series.py` — останній снапшот на період (§0.3), групування по довжині (§0.4), чесна відсутність (`truncated` vs `complete`→0), сортування.
- `tests/unit/urlinspector/test_sources_isolation.py` — збій одного/усіх джерел не валить `inspect_url`; unknown-domain коротке замикання до жодного джерела.
- `tests/unit/urlinspector/test_live.py` — blocked-вердикт не викликає другий фетч; повні сигнали + indexable + findings на реальному HTML-фікстурі; fetch-рівня blocked; schema-check опціональний і його збій не топить панель.
- `tests/unit/api/test_url_inspector_api.py` — щасливий шлях, unknown domain як 200, невалідний URL як 400, ізоляція джерела через API, live blocked short-circuit.
- `tests/e2e/frontend/url-inspector.cjs` — живий браузерний прогін: шапка на дашборді, навігація з пошуку, реальні дані SAPHNY, перемикання гранулярності, unknown-domain стан, мобільний viewport без горизонтального скролу.

### Follow-ups / борг

- `UrlInspectorLiveDTO.schema_check_id` персистує **спрощений** запис (`generated=None`, `raw_markup=[]`) — повний JSON-шейпінг, як у `/tools/schema-markup`, залишено за `TODO(task-61-followup)`; оператор і так має посилання на повний інструмент.
- Пошук приймає лише повний URL, без автодоповнення за фрагментом/назвою сторінки (свідомо поза скоупом, план §7).
- Історія інспекцій не зберігається (свідомо, план §2.5/§7).
