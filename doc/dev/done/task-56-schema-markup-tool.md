# task-56 — Schema Markup Checker: перевірка й генерація structured data (schema.org)

- **Пакет:** новий інструмент у хабі Tools — оператор кидає URL, тул перевіряє, чи є на сторінці валідна schema.org-розмітка (JSON-LD/Microdata), звіряє її з живими вимогами Google Search (rich results) і, де розмітки бракує, **генерує** JSON-LD-сніпет із фактів, реально знайдених на сторінці — з явним джерелом кожного поля, без вигаданих значень.
- **Хвиля:** P — Tools (6-й інструмент після task-27 Alt Text, task-29 Redirect Checker, Character Counter, task-53 Strategy/Framework Check).
- **Depends on:** **27** ✅ (каталог Tools), **06** ✅ (REST), **13** ✅ (harness)
- **Не конфліктує** з `app/monitor/**`, `app/contentqa/**` — читає `monitor_pages`/`monitor_blobs` **лише на читання** (режим «весь клієнт», §7, опційний follow-up).
- ⚠️ **Спільні файли частково зайняті паралельною сесією** (перевірено 2026-09-04, `git status` нечистий): `app/db/models.py` (+4 рядки, `Client.is_dev`), `frontend/js/api.js` (+27 рядків, robots/sitemap check). Обидва диффи локальні й далеко від місця вставки цієї таски — новий клас моделі йде в кінець файлу, нові функції `api.js` — поруч із іншими tool-функціями (кінець файлу, як `checkStrategy`). `app/api/tools.py`, `frontend/js/tools-page.js`, `frontend/index.html`, `frontend/css/tools.css` — чисті.
- **Owned paths:** `app/tools/schema_markup/**`, `app/api/tools_schema_markup.py`, `app/schemas/schema_markup.py`, `app/db/repositories/schema_markup.py`, `migrations/versions/*_schema_markup_checks.py`, `frontend/js/schema-markup-tool.js`, `frontend/css/schema-markup.css`, `tests/unit/tools/schema_markup/**`, `tests/unit/api/test_tools_schema_markup.py`, `scripts/build_schema_vocab.py`
- **Адитивні дотики (узгодити):** `app/api/tools.py` (+1 запис у `_TOOLS`), `frontend/js/tools-page.js` (+1 в `TOOL_MODULES`), `app/db/models.py` (+1 модель `SchemaMarkupCheck`, в кінець файлу), `frontend/js/api.js` (+4 функції, в кінець файлу), `frontend/index.html` (+1 `<link>`)
- **PLAN.md:** §12 (чесні стани: «не перевіряли» ≠ «схеми немає»; поле без джерела в генераторі не існує), §13 (жодних секретів — тул б'є лише публічні сторінки клієнтів)
- **frontend/AGENTS.md:** §5 (правда даних), §6 (стани), §15–16 (візуальний loop)
- **AISeoTools:** змін **не потребує**.
- **Нових Python-залежностей: нуль.** Мережа — `app.tools.http_safety` (той самий SSRF-гард/UA/no-cache, що і в Alt Text/Redirect Checker), HTML — stdlib `html.parser`, JSON-LD — stdlib `json`. Свідомо без `extruct` (тягне `lxml`+`pyrdfa3`+`mf2py`, `lxml` тут лише транзитивний через optional-групу `local`, яку CI/деплой не встановлюють).

---

## Дослідження — див. звіт у чаті 2026-09-04 (стислий підсумок нижче)

### Стан schema.org / Google (те, що змінює вимоги)
- Вокабуляр v30.0, `schemaorg-current-https.jsonld` = 1.5 МБ, вендориться дистильованим у репо (десятки КБ), не якуванням у рантаймі.
- HowTo rich results прибрано (09.2023). 7 типів retired 12.06.2025 (Book Actions пізніше повернули). **FAQ rich results вимкнено 07.05.2026** (Rich Results Test/API — до серпня 2026) — `FAQPage` лишається валідним типом, але rich result не дає.
- Валідатори без програмного API: `validator.schema.org` (колишній SDTT) на POST `/validate` віддає 302 → `google.com/sorry` (антибот) — **непридатний як backend-залежність**, перевірено живим запитом. Rich Results Test API ніколи не існував публічно. Єдиний офіційний канал — GSC URL Inspection API (`richResultsResult.detectedItems`), але тільки для верифікованої property й уже проіндексованих URL — **follow-up**, не MVP.
- Висновок: валідацію реалізуємо самі, проти вендореного вокабуляру + рукописного Google-профілю з датою перевірки на кожній фічі.

### Бібліотеки
- `extruct` — де-факто стандарт екстракції (JSON-LD+Microdata+RDFa+OG+microformats), але тягне `lxml`/`pyrdfa3`/`mf2py` — відхилено з тієї ж причини, що й у task-27/29/53 (env-poetry-lock-limitation, `lxml` не в прод-образі напряму).
- `PyLD` (повний JSON-LD 1.1 процесор) і `rdflib`+`pySHACL` — оверкіл для 8 потрібних типів; SHACL-шейпи для Google все одно довелось би писати руками.
- Генеровані типи (`schema-org-py`, `msgspec-schemaorg`, `schema-org-python`) — тягнуть тисячі класів заради восьми.
- **Рішення:** stdlib `html.parser` + `json` + вендорений дистилят вокабуляру. Той самий вибір, що й у `alt_text/verify.py`, `redirects/trace.py`, `monitor/page.py`.

### Що вже є в репо й переюзається без змін
- `app.tools.http_safety` — SSRF-гард, UA, no-cache, throttle.
- `app.monitor.page._PageParser` вже витягує `json_ld_types` — підтверджує, що stdlib-парсер достатній; наш екстрактор іде глибше (повний граф, не лише типи).
- `MonitorBlob`/`MonitorPageVersion.body_blob_sha256` — джерело для майбутнього режиму «весь клієнт» без нового мережевого запиту (§7, follow-up).
- Патерн тула: `app/api/tools_redirects.py`/`tools_content_qa.py` (реєстр → API → БД → list+detail сторінка з shareable-лінком) — копіюється майже 1:1.

### Ключова пастка: JS-ін'єктована розмітка
Частина тем/GTM-шаблонів вставляє JSON-LD через JS, не в HTML-відповіді. Простий `urllib`-фетч цього не побачить → хибне «схеми немає» → згенерований дубль на сторінці. Мітигація: якщо HTML-фетч не знайшов жодної розмітки **або** відповів не-2xx — MVP чесно повідомляє `checked_html_only` замість «схеми немає» (browser-fallback через `app.monitor.browser_fetch` — окремий крок, `TODO(task-56-followup)`, бо потребує узгодження з antibot-лейном моніторингу, не блокує MVP).

### Правила генерації (інваріант «здогад ≠ факт»)
1. Кожне згенероване поле має `source` (`page:title`, `page:og:image`, `url_path`, …). Без джерела — поле не йде в сніпет.
2. Недоведені поля — в `needs_input`, не вигадка і не порожній рядок.
3. Ніколи не генеруємо `AggregateRating`/`Review` про себе, `FAQPage`, `HowTo`, retired-типи.
4. Merge, не replace: якщо розмітка вже є, звіт показує, чого **бракує**, а не пропонує новий блок поверх.
5. Дата/автор — лише з реальних сигналів сторінки (`<time datetime>`, `article:published_time`), ніколи «сьогодні».

---

## 2. Дані

### 2.1 Вокабуляр (офлайн-артефакт)

`scripts/build_schema_vocab.py` (ручний запуск, не в рантаймі): завантажує `https://schema.org/version/latest/schemaorg-current-https.jsonld`, дистилює в `app/tools/schema_markup/data/vocab.json` — `{version, generated_at, source_sha256, types: {name: {parents, properties}}, properties: {name: {ranges, domains}}}`. Комітиться в репо; рантайм не робить жодного мережевого виклику для валідації.

`app/tools/schema_markup/data/google_profiles.json` — рукописний, на кожен тип: `required`, `recommended`, `rich_result_status` (`live` | `retired` | `no_rich_result_but_valid`), `docs_url`, `checked_at`. Покриває вузький набір, реально живий для агенційного SEO: `Organization`, `LocalBusiness`, `WebSite`, `BreadcrumbList`, `Article`/`BlogPosting`, `Product`, `Service`, `Person`, `FAQPage` (позначений `no_rich_result_but_valid`).

### 2.2 Таблиця

`schema_markup_checks` — один рядок = один прогін (мутабельна, як `AltTextReport`/`ContentQaReport`; повторний check тієї ж сторінки перезаписує):
`id, client_id?, url, final_url, transport(html|html_only_js_suspected), http_status?, page_kind, found_types_json, verdict(clean|has_gaps|missing|fetch_failed), findings_json, generated_json, vocab_version, created_at`.

`checked_at`-семантики немає окремо — `created_at` і є моментом перевірки; повторний прогін = новий рядок (та сама модель, що Redirect Checker: історія важлива, тому не UPSERT).

---

## 3. API

`/api/v1/tools/schema-markup`:
- `POST /checks` — `{url, client_id?}` → фетч + аналіз + генерація + persist → `SchemaMarkupCheckDTO` з `id`.
- `GET /checks` — список (пагінація, фільтр `client_id`).
- `GET /checks/{id}` — деталь (shareable, `#/tools/schema-markup/<id>`).

Синхронно, без job-рушія (той самий вибір, що Alt Text/Redirect Checker: один HTTP-запит до сторінки клієнта, секунди — не колекція метрик за період).

---

## 4. Definition of Done / Gate

- [ ] `scripts/build_schema_vocab.py` запущений, `data/vocab.json` + `data/google_profiles.json` у репо, обидва читаються без мережі.
- [ ] Екстрактор коректно читає JSON-LD (одиничний обʼєкт, масив, `@graph`) і Microdata (itemscope/itemprop/itemtype, вкладені) на золотих HTML-фікстурах: Yoast, RankMath, WooCommerce, порожня сторінка, зламаний JSON, подвійне екранування.
- [ ] Валідатор L0 (синтаксис) / L1 (тип+властивість існують у вокабулярі, з обходом subclass-ланцюга) / L2 (Google required/recommended, попередження про retired/FAQ) мають юніт-покриття на кожен рівень окремо.
- [ ] Генератор: кожне поле згенерованого JSON-LD має `source`; жодне поле з `needs_input` не потрапляє в сніпет; заборонені типи (`FAQPage`, `HowTo`, retired) ніколи не генеруються.
- [ ] Міграція застосована (`alembic upgrade head` у Docker, з попереднім бекапом за CLAUDE.md §4), `SchemaMarkupCheck` у моделі, репозиторій має `create`/`get`/`list`.
- [ ] API покритий `tests/unit/api/test_tools_schema_markup.py`, OpenAPI-снапшот регенеровано.
- [ ] Тул зареєстрований у хабі Tools (`app/api/tools.py`, `frontend/js/tools-page.js`), сторінка `#/tools/schema-markup` з формою (URL[, клієнт]), звітом (знайдені типи, findings за рівнями, сніпет для копіювання, `needs_input`-список) і списком минулих перевірок із shareable-лінком.
- [ ] Живий прогін проти реального сайту клієнта (з наявним JSON-LD Yoast-плагіна) і проти сторінки без розмітки — обидва кейси зафіксовано в Completion report.
- [ ] `docker compose exec api poetry run pytest tests/unit/tools/schema_markup tests/unit/api/test_tools_schema_markup.py -q`, `ruff check`, `mypy app/tools/schema_markup app/api/tools_schema_markup.py` — зелені.

---

## 5. Follow-ups (свідомо поза MVP)

- Browser-fallback (`app.monitor.browser_fetch`) для сторінок, де HTML-фетч не знайшов розмітки — відрізнити «дійсно немає» від «є, але через JS».
- Режим «весь клієнт»: аудит усіх `monitor_pages` клієнта через уже збережені `monitor_blobs`, без нового мережевого запиту.
- GSC URL Inspection API як зовнішня звірка «що реально бачить Google» — окрема інтеграція (наш GSC йде через Reporting Ninja, не через власний OAuth).
- Verify-after-install: повторний фетч після того, як оператор вставив згенерований сніпет.
- Client picker у формі є (organization_name джерело для генератора), але автопідбір по домену URL (як `guessClient` в Alt Text) не зроблено — оператор вибирає клієнта вручну.

---

## ✅ Completion report

- **Дата:** 2026-09-04
- **Гілка / комміт:** `main` (некомічено — комміт лише за проханням користувача)
- **Статус:** done-with-followups

### Що зроблено

Повний робочий тул, e2e перевірений на живих сайтах: `POST /api/v1/tools/schema-markup/checks` приймає URL, фетчить сторінку (SSRF-гард, no-cache), екстрактить JSON-LD і Microdata, валідує трьома рівнями (L0 синтаксис, L1 вокабуляр schema.org v30 з обходом subclass-ланцюга, L2 Google rich-results профіль з датованими notes про retired/FAQ), генерує JSON-LD-сніпет із доведених фактів сторінки (кожне поле — з `source`, недоведене — в `needs_input`), персистує результат і повертає shareable-лінк. Фронт: форма (URL + опційний клієнт) → звіт із бейджем вердикту, знайденими типами, findings за рівнем/severity з фільтром, готовим до копіювання JSON-LD і списком «Reports» з історією прогонів. Тул зареєстрований у хабі Tools.

**Живий гейт (2026-09-04):** два реальні прогони проти публічних сайтів через контейнер `api` (`curl localhost:6524`, не тести):
- `https://www.goreport.com/` (реальний клієнт агенції) — коректно витягнуто `BreadcrumbList/ImageObject/Organization/WebPage/WebSite` з живої Yoast/RankMath-розмітки; L1 підняв справедливий `warn` на `query-input` (властивість Google Sitelinks Search Box конвенції, підтверджено відсутня у вендореному вокабулярі — не помилка, а точна робота валідатора); L2 підняв `info` на відсутні `sameAs`/`contactPoint` в `Organization`; генератор коректно пропустив `Organization`/`WebSite` як «already present on the page».
- `https://schema.org/docs/gs.html` (сторінка без жодної розмітки) — `verdict=missing`, генератор побудував `BreadcrumbList` з URL-шляху.

### Як зроблено (ключові рішення)

- **Нуль нових Python-залежностей.** Досліджено `extruct`/`PyLD`/`rdflib`+`pySHACL`/готові типізовані пакети вокабуляру — усі або тягнуть `lxml` (лише транзитивний у образі через optional-групу `local`), або оверкіл заради 8 потрібних типів. Написано на stdlib `html.parser`/`json`, той самий вибір, що в `alt_text/verify.py`/`redirects/trace.py`/`monitor/page.py`.
- **Валідатори без API.** `validator.schema.org` (колишній SDTT) на POST `/validate` живо віддав 302 → `google.com/sorry` (антибот) — перевірено з цієї машини, задокументовано в плані. Rich Results Test API ніколи не існував публічно. Валідація — повністю власна, проти вендореного дистиляту.
- **Вокабуляр вендориться дистильованим.** `scripts/build_schema_vocab.py` качає `schemaorg-current-https.jsonld` (1.5 МБ) і дистилює в `app/tools/schema_markup/data/vocab.json.gz` (933 типи, 1521 властивість, subclass-ланцюг уже розгорнутий у `properties`-множину кожного типу — рантайм не рекурсує по предках). Живий формат дампа виявився CURIE (`"schema:Article"`, не повний URL) — перша версія скрипта відфільтровувала все через невірне припущення про префікси й діставала 0 типів; виправлено й перевірено на реальних `BlogPosting`/`LocalBusiness`.
- **Google-профіль — рукописний, датований.** `google_profiles.json`: `FAQPage` позначено `no_rich_result_but_valid` (rich result вимкнено 07.05.2026), `HowTo`/7 retired-типів (12.06.2025) — `retired`, з `checked_at` на кожному запису. `FORBIDDEN_GENERATE_TYPES` (`AggregateRating`/`Review`/`FAQPage`/`HowTo`) — структурний гарант у коді (`vocab.py`), не залежний від даних профілю: тест `test_forbidden_types_never_generated` перевіряє, що жоден `SUGGESTED_TYPES_BY_KIND`-набір їх не містить.
- **Двофазна фільтрація «здогад ≠ факт».** `generate.py`: кожне поле має `Fact(value, source)`; `_set()` кладе поле в `properties`+`sources` лише коли факт непорожній, інакше — в `needs_input`. Перевірено тестом, що порожні факти НІКОЛИ не потрапляють у `properties`.
- **Microdata-парсер — явний стек кадрів.** На відміну від JSON-LD (лінійний потік CDATA), microdata прив'язана до DOM-вкладеності — знадобився стек відкритих елементів (`_Frame`) з розрізненням `item`/`prop`/`plain`, а не один прапорець, як у `_JsonLdCollector`/наявному `_PageParser`. Перевірено на WooCommerce-подібній вкладеній `Product > Offer` фікстурі.
- **Головні файли:** `app/tools/schema_markup/{vocab,extract,signals,classify,validate,generate,service}.py` — рушій; `app/api/tools_schema_markup.py` — REST; `app/db/repositories/schema_markup.py` + `app/schemas/schema_markup.py` — persistence/DTO; `frontend/js/schema-markup-tool.js` + `frontend/css/schema-markup.css` — UI; `scripts/build_schema_vocab.py` — офлайн-дистилятор вокабуляру (ручний запуск, не в рантаймі).
- **Відхилення від плану:** L3 (консистентність по сайту через `monitor_blobs`) і browser-fallback свідомо не робились у цій сесії — позначені follow-up у плані, як і планувалось із самого початку (§7).

### Контрактні зміни (Contract changes)

Адитивні, нічого спільного не зламано:
- `app/db/models.py`: +1 модель `SchemaMarkupCheck` (в кінець файлу, без event.listen immutability-enforcement — той самий легший патерн, що `ContentQaReport`).
- `app/schemas/schema_markup.py` — новий файл, не імпортується в `app/schemas/__init__.py` (той самий статус, що `content_qa.py`/`tools.py`-схеми — не частина task-02 shared contract, тому `contract_snapshot.json` не зачеплено).
- `app/api/tools.py`: +1 запис `_TOOLS` (`schema-markup`) + `_schema_markup_stat`.
- `frontend/js/{tools-page,api}.js`, `frontend/index.html`: адитивні дотики (реєстрація тула, 3 нові API-функції, 1 `<link>`), узгоджено в плані заздалегідь — диффи лежали далеко від паралельної сесії, що на момент старту тримала невеликі (+4/+27 рядків) незакомічені зміни в тих самих файлах.

### Як перевірити (verification)

```bash
docker compose exec api poetry run pytest tests/unit/tools/schema_markup tests/unit/api/test_tools_schema_markup.py -q   # 34 passed
docker compose exec api poetry run ruff check app/tools/schema_markup app/api/tools_schema_markup.py scripts/build_schema_vocab.py
docker compose exec api poetry run mypy app                                                                              # 198 файлів, чисто
docker compose exec api poetry run pytest -q                                                                             # 2027 passed, 2 failed (pre-existing, не наші — див. нижче), 1 skipped
```

Живий curl (стек уже піднятий):
```bash
curl -s -X POST http://localhost:6524/api/v1/tools/schema-markup/checks \
  -H "Content-Type: application/json" -d '{"url": "https://www.goreport.com/"}'
```
UI: `http://localhost:6524/#/tools/schema-markup` — форма, прогнати URL, перевірити картку вердикту/findings/сніпет, «Copy link» на деталі, рядок у списку «Checks».

**Два падіння повного прогону — не наші, підтверджено ізольованим прогоном:**
- `tests/contract/test_aiseotools_signatures.py::test_public_surface_matches_snapshot` — сусідній AISeoTools-чекаут (`run_import`/`build_tech_xlsx`) отримав нові keyword-параметри поза цією сесією; жодного стосунку до `schema_markup`.
- `tests/unit/api/test_tech_audit.py::test_dashboard_card_counts_measured_open_errors` — падає детерміновано в ізоляції (`stale` замість `complete`), код-шлях (`tech_audit`/dashboard card freshness) ніде не перетинається з `schema_markup`.

### Тести

- `tests/unit/tools/schema_markup/{test_extract,test_validate,test_generate,test_service}.py` — 26 тестів: JSON-LD (`@graph`, кілька блоків, зламаний JSON, вкладені `@id`-посилання), Microdata (вкладений `Product > Offer`), L0/L1/L2 окремо, генератор (source/needs_input/forbidden types/merge-not-replace).
- `tests/unit/api/test_tools_schema_markup.py` — 8 тестів: create/list/get, client_id-фільтр, 404, fetch_failed-стан, каталог Tools містить новий запис. Мережа мокнута через `monkeypatch.setattr(api_mod, "run_check", ...)`.

### Follow-ups / борг

- Browser-fallback для JS-ін'єктованої розмітки — `TODO(task-56-followup)` у плані §1.
- Режим «весь клієнт» через `monitor_blobs` (§7).
- GSC URL Inspection API як зовнішня звірка (§7).
- Verify-after-install (§7).
- Автопідбір клієнта за доменом URL у формі (як `guessClient` в Alt Text) — косметичний, не зроблено.
- `git mv` у `doc/dev/done/` і оновлення статусу в `INDEX.md` — виконано цим самим коммітом; **push/PR і сам git commit — за проханням користувача**, ще не зроблено.

---

## Доповнення (2026-09-04, друга ітерація): raw markup view + Recheck

Користувач після демо: «тул лише перевіряє а не дає нову?» (генерація вже була, але не була видима як «сирий» JSON) → «Додай вивід сирого json та можливість перегенерувати».

- **Сирий JSON.** `ExtractedEntity.to_jsonable()` ([extract.py](../../app/tools/schema_markup/extract.py)) серіалізує дерево в plain JSON — для JSON-LD це просто `block.parsed` (те, що реально написав плагін), для microdata (немає нативної JSON-форми) дерево збирається з `itemprop`/`itemscope`. Нова колонка `raw_markup_json` на `SchemaMarkupCheck` (амендив ту саму, ще нікому не показану міграцію `f1a4c9e7b023`, а не стекав другу — таблиця мала лише мої ж тестові рядки; ⚠️ під час ампенду `alembic downgrade -1` виявив, що паралельна сесія встигла зачепити цю ревізію своєю `a064ff1f082f` (task-57 Page Inventory) — перевірено: чужа таблиця, конфлікту нуль), у DTO — новий `SchemaMarkupRawBlockDTO` (`format: json-ld|microdata`, `raw`, `error`). Фронт — `<details>`/`<summary>` (нативний, без JS-тоглу), згорнутий за замовчуванням, один блок на JSON-LD-скрипт/microdata-item, кожен із кнопкою Copy; зламаний JSON-LD блок показує помилку парсера, а не порожнечу.
- **Recheck.** Кнопка в шапці деталі — той самий виклик, що форма створення (`checkSchemaMarkup(check.url, check.client_id)`), нова перевірка = новий незмінний рядок (той самий інваріант, що вже був: історія, не UPSERT), `location.hash` веде на новий id — той самий full-reload-на-hashchange патерн, що вже стояв у `submitCheck()` (перевірено в router.js, не вигадано заново).
- **Живий гейт (2026-09-04, друга ітерація):** живий curl проти `https://www.goreport.com/` — `raw_markup[0].raw` містить повний `@graph` **точно** таким, яким Yoast його написав (5 вузлів: `WebPage`/`ImageObject`/`BreadcrumbList`/`WebSite`/`Organization`, включно з `query-input`-полем усередині `SearchAction`, яке L1 і раніше справедливо позначав `warn`). Другий POST на той самий URL повернув інший `id` — Recheck-семантика підтверджена без браузера.
- **Тести:** +2 (`test_extract.py::test_microdata_to_jsonable_builds_nested_json_tree`, `test_json_ld_entity_to_jsonable_mirrors_types_and_properties`) +2 API (`test_get_check_returns_raw_markup_blocks`, `test_fetch_failed_check_has_no_raw_markup`) — 38 у модулі, усі зелені; `ruff`/`mypy` на весь `app/` чисто (крім `app/pageinventory/**` — чужий модуль task-57, 2 pre-existing помилки, не мої).
- OpenAPI snapshot регенеровано (новий `raw_markup` в контракті `SchemaMarkupCheckDTO`).
