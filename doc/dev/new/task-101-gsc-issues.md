# task-101 — Search Console Issues: помилки з консолі на сторінці клієнта, власна перевірка «чи ще є», нові — у монітор

**Depends on:** 35 ✅ (Site Monitor engine: `fetch`/`extract_signals`/`parse_robots`/`MonitorChange`), 36 ✅ (`#/monitor`, `#/client/<id>/monitor`), 83 ✅ (прямий Google API transport), 88 ✅ (`UrlInspectionClient`), 100 ✅ (`indexing_urls` — свіжі інспекції для повторного використання, кап квоти), 56 ✅ (`app/tools/schema_markup` — валідатор для rich results), 29 ✅ (`app/tools/redirects/trace.py`), 71 ✅ (auth/ролі), 72 ✅ (audit), 06 ✅ (REST), 18 ✅ (`client-page.js`)

**Owned paths:**

```
app/gscissues/                          (новий пакет: universe, inspect, classify, verify, lifecycle, notify, service, scheduler, xlsx)
app/api/client_gsc_issues.py            (новий роутер, /api/v1/clients/{id}/gsc-issues)
app/schemas/gsc_issues.py               (новий DTO-модуль)
app/db/repositories/gsc_issues.py       (новий репозиторій)
frontend/js/gsc-issues-page.js          (нова під-сторінка #/client/<id>/gsc-issues)
frontend/js/gsc-issues-card.js          (нова: компактна картка на сторінці клієнта)
frontend/css/gsc-issues.css             (нова)
tests/unit/gscissues/                   (нові)
tests/unit/api/test_client_gsc_issues_api.py (новий)
tests/e2e/frontend/client-gsc-issues.cjs (новий)
```

**Read-only дотики (правити НЕ можна, лише читати/імпортувати):** `app/monitor/{fetcher,page,robots,sitemaps}.py`, `app/db/repositories/monitor.py` (`record_change`/`resolve_change`/`open_change` — єдиний дозволений спосіб писати в монітор), `app/monitor/severity.py`, `app/urlinspector/google/index.py`, `app/integrations/google_api/*`, `app/indexing/*` і таблиці `indexing_*` (тільки читання свіжих інспекцій і лічильників квоти), `app/tools/redirects/trace.py`, `app/tools/schema_markup/*`, `app/db/models.py` (нові моделі додає ця таска в кінець файлу — узгодити з `INDEX.md`), `migrations/versions/` (нова ревізія через процедуру task-03).

**Адитивні дотики (рядок у наявний файл, оголосити в Completion report):** `app/schemas/monitor.py` (`ChangeScope` + `"gsc"`), `app/monitor/severity.py` (нові ключі `gsc_issue_*` у `STATIC_SEVERITY` — або власна таблиця в `app/gscissues/severity.py`, див. §2.4), `app/jobs/worker.py` (старт планувальника), `app/audit/actions.py` (нові дії), `app/core/config.py` (нові налаштування), `app/cli.py` (CLI), `frontend/js/{client-page,router,api,formatters}.js`, `frontend/js/{monitor-page,monitor-client}.js` (лейбли/фільтр «Search Console»), `frontend/index.html` (root + `?v=`-ланцюжок), `tests/unit/api/data/openapi_snapshot.json`.

---

## 0. Одне речення

Google **не віддає звіт «Pages» через API** — тому модуль сам збирає всесвіт URL клієнта (sitemap ∪ сторінки з показами в GSC ∪ обхід монітора), питає в `urlInspection` стан кожного, розкладає відповіді на ті самі причини, що й таблиця «Why pages aren't indexed» у консолі, **одразу перевіряє власними парсерами, чи дефект досі є на сайті**, показує це на сторінці клієнта, а кожну **нову** помилку пише в Site Monitor як зміну зі своєю серйозністю.

---

## 1. Дослідження (документація + живі виклики з контейнера `api`, 2026-09-22)

### 1.1 Що з «помилок консолі» взагалі доступне через API

`searchconsole.googleapis.com` / `webmasters/v3` мають 11 методів. Із розділів GSC UI, де живуть «помилки», через API досяжні **три**:

| Розділ GSC UI | Через API | Як саме |
|---|---|---|
| **Pages / Indexing** («Why pages aren't indexed») | ⚠️ **лише per-URL** | `urlInspection.index.inspect` — стан індексації одного URL (`verdict`, `coverageState`, `pageFetchState`, `robotsTxtState`, `indexingState`, канонікали, `referringUrls`, `lastCrawlTime`). **Самого звіту зі списком URL по причинах API не має** — ані сьогодні, ані в Bulk data export до BigQuery (там рівно три таблиці: `searchdata_site_impression`, `searchdata_url_impression`, `ExportLog` — тільки performance-дані). Усі «екстрактори coverage-звіту» в дикій природі (напр. `index-coverage-extractor`) — це Playwright над UI під людським Google-логіном, з лімітом експорту **1000 рядків на причину**. |
| **Enhancements** (Breadcrumbs, FAQ, Product snippets, …) | ⚠️ per-URL | той самий `urlInspection` → `richResultsResult.detectedItems[].items[].issues[]{issueMessage, severity ∈ {WARNING, ERROR}}` |
| **Sitemaps** | ✅ | `sitemaps.list` → `errors`/`warnings` (**лише лічильники**, без текстів), `isPending`, `lastDownloaded`; `contents[].indexed` — мертве поле (task-100 §1.2: нуль скрізь) |
| Video pages, Removals, Links, Crawl stats, HTTPS, Security & Manual actions | ❌ | немає жодного методу |
| Core Web Vitals | ❌ (окремий CrUX API з API-ключем) | поза цією таскою (§5) |
| Mobile usability | ❌ | звіт закрито у 2023; `mobileUsabilityResult.verdict` живо приходить як `VERDICT_UNSPECIFIED` на всіх 37 інспекціях |

**Наслідок для дизайну:** «побачити всі помилки» = **скласти всесвіт URL самим** і опитати кожен. Повнота модуля дорівнює повноті всесвіту (§1.5), і UI зобов'язаний казати «N із M відомих URL перевірено за 30 днів», а не «всі помилки» (PLAN §12).

### 1.2 Довідник `urlInspection` (developers.google.com/webmaster-tools/v1/urlInspection.index)

Запит: `inspectionUrl` (мусить належати `siteUrl`), `siteUrl` (`sc-domain:…` або URL-prefix), `languageCode` (ми фіксуємо `en-US`, щоб тексти причин були стабільні). Scope — `webmasters.readonly` достатньо. **Лише стан у індексі** — «живий тест» URL через API неможливий.

`indexStatusResult`:

| Поле | Значення | Семантика (цитата документації) |
|---|---|---|
| `verdict` | `PASS` / `FAIL` / `NEUTRAL` / `PARTIAL` | `PASS` = «Valid», `FAIL` = «Error or Invalid», `NEUTRAL` = «**Excluded**», `PARTIAL` = «Reserved, no longer in use» |
| `coverageState` | вільний текст | «Could Google find and index the page» — це і є назва причини з таблиці Pages (`Not found (404)`, `Page with redirect`, `Excluded by ‘noindex’ tag`, …) |
| `pageFetchState` | `SUCCESSFUL`, `SOFT_404`, `BLOCKED_ROBOTS_TXT`, `NOT_FOUND`, `ACCESS_DENIED` (401), `SERVER_ERROR` (5xx), `REDIRECT_ERROR`, `ACCESS_FORBIDDEN` (403), `BLOCKED_4XX`, `INTERNAL_CRAWL_ERROR`, `INVALID_URL` | «Whether or not Google could retrieve the page» — **enum, а не текст**: саме на нього спирається класифікатор §2.3 |
| `robotsTxtState` | `ALLOWED` / `DISALLOWED` / `…_UNSPECIFIED` | |
| `indexingState` | `INDEXING_ALLOWED` / `BLOCKED_BY_META_TAG` / `BLOCKED_BY_HTTP_HEADER` / `…_UNSPECIFIED` | розрізняє meta-noindex і `X-Robots-Tag` |
| `googleCanonical` / `userCanonical` | URL | розбіжність = «Duplicate, Google chose different canonical» |
| `referringUrls[]` | URL | «URLs that link to the inspected URL, directly and indirectly» — Google каже, **хто лінкує на битий URL** (див. §1.4: це перевіряється нашим парсером) |
| `sitemap[]`, `lastCrawlTime`, `crawledAs` | | |

Плюс `inspectionResultLink` — deep-link у GSC (непрозорий `id`, зберігається, не будується — урок task-100 §2.5), `richResultsResult`, `ampResult` (відсутній, якщо не AMP).

**Квоти (developers.google.com/webmaster-tools/limits):** URL Inspection — **2000 QPD і 600 QPM на property**, 10 000 000 QPD / 15 000 QPM на проєкт. Search Analytics — 1200 QPM на property. Виміряно (task-100 §1.4 і тут): ~1 с/URL у 8 потоків, паралелізм лінійний.

**Довідка GSC «Page indexing report» (support.google.com/webmasters/answer/7440203)** — три речі, які модуль копіює свідомо:

1. Повна таксономія причин (§1.6) і колонка **Source**: «Website» (лагодить власник сайту) vs «Google system» (нічого не зробиш, лише чекати).
2. **Validation** («Validate fix»: Not started → Started → Looking good → Passed / Failed; «typically takes up to about two weeks») — наша власна перевірка парсерами і є цей крок, тільки миттєвий, а підтвердження Google приходить окремо, коли він перекраулить.
3. **Життєвий цикл**: «an issue's lifetime extends … until 90 days after the last instance was marked as gone». Тому наш рядок теж не зникає в момент фіксу, а живе як `resolved` з датою.

### 1.3 Живо на EFFYS (`338e0b08-6c51-4d48-b856-a368a3319ca8`, property `https://effyscafe.com/`, URL-prefix)

Всесвіт: 16 URL із sitemap (= `monitor_pages`) ∪ 32 сторінки з GSC page-dimension = **33 URL**. Повна інспекція: **32,6 с у 8 потоків**. Sitemaps API: 1 sitemap, `errors=0`, `warnings=0`, `web: submitted 16 / indexed 0` (мертве поле), `image: 870`.

| `verdict` | `coverageState` | К-сть | Що це насправді |
|---|---|---|---|
| `PASS` | `Submitted and indexed` | 16 | ок (з них 2 — PDF `2026/06/…CERT-July-2026.pdf`) |
| `NEUTRAL` | **`Not found (404)`** | **2** | `/wp-content/uploads/2025/10/Effy-Cafe-{York-Ave,W96th-St}-CERT-July-2026.pdf` — сертифікати кашруту; `pageFetchState=NOT_FOUND`; Google каже `referringUrls` = `/locations/` і `/?ref=aftership` |
| `NEUTRAL` | `Page with redirect` | 1 | `/locations/manhattan/` → `googleCanonical=/locations/columbus-ave/` |
| `NEUTRAL` | `Crawled - currently not indexed` | 2 | один пост блогу + PDF `2026/07/…Columbus-CERT…` |
| `NEUTRAL` | `URL is unknown to Google` | 12 | `/locations/` (**є в sitemap!**), `/locations/upper-ended-side/` (одруківка з 3 показами в історії) і **9 зображень** `.webp` з image-показами |
| — | rich results | 14× `Breadcrumbs` `PASS` | жодного issue; `mobileUsabilityResult` = `VERDICT_UNSPECIFIED` усюди |

**Ціна цих двох 404 — виміряна (Search Analytics, page-dimension, live):**

| URL | 16 міс | 90 днів | 28 днів |
|---|---|---|---|
| `…/2025/10/…York-Ave-CERT-July-2026.pdf` (404) | 164 кліки / 2 361 показ | 17 / 491 | **0 / 0** |
| `…/2025/10/…W96th-St-CERT-July-2026.pdf` (404) | 44 / 768 | 11 / 186 | 0 / 0 |
| `…/2026/06/…York-Ave-CERT-July-2026.pdf` (індексований наступник) | 1 / 1 | 1 / 1 | 1 / 1 |
| `/locations/manhattan/` (redirect) | 353 / 18 922 | 144 / 7 129 | 0 / 0 |
| `/locations/columbus-ave/` (ціль редіректу) | 1 609 / 36 307 | 1 609 / 36 307 | **697 / 18 254** |

Два висновки, які визначають UI: (1) **редірект спрацював** — трафік `/manhattan/` переїхав на `/columbus-ave/`, тож `Page with redirect` тут — не помилка, а `notice`; (2) **PDF-и трафік втратили, а не передали** — 208 кліків за 16 місяців пішли в 404, наступник за той самий період зібрав 1 клік. Це і є «помилка з консолі», яку оператор має побачити першою, з готовою дією: `301 старий → новий`.

### 1.4 Власні парсери живо на тих самих URL — що вони додають до відповіді Google

| URL | Google (`lastCrawlTime`) | Наш парсер зараз | Що це дає оператору |
|---|---|---|---|
| обидва PDF `2025/10/…` | `Not found (404)`, 2026-09-07/09 | **досі `404`**, тіло — WP-шаблон «Page not found» (182 277 байт, `noindex, follow`) | помилка **ще є** → рядок лишається відкритим |
| `referringUrls` цих PDF: `/locations/` | Google бачив лінк | `/locations/` → `200`, лінкує вже **третє покоління** файлів: `/2026/09/…CERT-July-2027.pdf` (×3) — старих посилань немає | причина (внутрішній лінк) **уже прибрана**, лишилась сама 404 → дія «додати 301» |
| наслідок, який Google ще не бачив | `2026/06/…` PDF — `Submitted and indexed` | на `/locations/` посилань на них **уже немає** | **наступні 404 через кілька тижнів** — модуль покаже це як `orphaned_indexed_url` (§2.3, клас `notice`), бо це передбачення, а не факт |
| `/locations/manhattan/` | `Page with redirect` | `301` → `/locations/columbus-ave/` → `200`, `canonical` = ціль, 1 хоп | ланцюг здоровий → `notice`, не помилка |
| `/locations/upper-ended-side/` | `URL is unknown to Google` | `404` | Google уже забув — **не issue** (не вигадуємо помилку там, де GSC її не показує) |
| `/blog/valentines-…-upper-east-side-ideas/` | `Crawled - currently not indexed`, 2026-07-01 | `200`, `index, follow`, canonical на себе, 302 KB | сайт «чистий» → причина на боці Google → `notice`, дія веде в Indexing Planner / контент |

Ключове: парсер відповідає не лише «чи ще 404», а й **чи ще є причина** (внутрішній лінк) і **куди тепер вести** (наступник у тій самій папці з тією самою назвою файлу — кандидат на ціль 301).

### 1.5 Всесвіт URL на портфелі — заміряно живим `searchAnalytics` (dimension `page`)

| Клієнт | property | sitemap (task-100) | GSC pages, 16 міс | з них файли (pdf/img) | GSC pages, 90 днів | час запиту |
|---|---|---|---|---|---|---|
| ALPHA | sc-domain | 287 | **5 947** | 199 | 2 211 | 0,6 с |
| SAPHNY | sc-domain | 491 | 1 298 | 1 | 437 | 0,4 с |
| 3XE | sc-domain | 715 | 985 | 0 | 773 | 0,8 с |
| SDS | sc-domain | 372 | 492 | 24 | 325 | 0,4 с |
| SHB | sc-domain | 104 | 261 | 74 | 173 | 0,3 с |
| NYCHA | sc-domain | 91 | 238 | 16 | 214 | 0,3 с |
| LAXTON | sc-domain | 173 | 229 | 8 | 185 | 0,5 с |
| GRP | URL-prefix | 34 | 74 | 17 | 54 | 0,3 с |
| SWM | sc-domain | 28 | 57 | 9 | 49 | 0,3 с |
| EFFYS | URL-prefix | 16 | 33 | 16 | 30 | 0,4 с |

Три висновки:

1. **ALPHA не влазить у квоту одним днем**: 5 947 + 287 > 2 000. Отже скан — не «все щодня», а **ротація за групами** (§2.3): відкриті issues щодня, sitemap + сторінки з показами за 90 днів щотижня, довгий хвіст — щомісяця. Для решти дев'яти клієнтів повний всесвіт ≤ 1 300 і вкладається в один день.
2. **Всесвіт береться з живого API, не з `gsc_dimension_metrics`**: у БД для ALPHA і LAXTON **0 рядків** page-dimension (збір RN там не працює), а живий запит на 16 місяців коштує 0,3–0,8 с на клієнта.
3. **Файли — окрема політика**: PDF — повноцінні документи в web-пошуку (EFFYS: 208 кліків), лишаються у всесвіті; зображення/відео/шрифти/css/js — не інспектуються (усі 9 `.webp` EFFYS дали `unknown`), але **лишаються в реєстрі** з позначкою `skipped_asset`, щоб число «N із M» не брехало.

### 1.6 Таксономія GSC → класи модуля (це і є словник)

Класифікація йде **enum-first** (`pageFetchState`/`robotsTxtState`/`indexingState`/канонікали), і лише потім по нормалізованому підрядку `coverageState` — бо текст містить типографські лапки (`Excluded by ‘noindex’ tag`, U+2018/U+2019 — task-100 §1.6) і може змінитись без попередження. Невідомий текст ніколи не губиться: клас `other`, сирий рядок зберігається.

| `issue_key` | Ознака в API | Причина в GSC UI | Source | Клас | Власна перевірка (§2.5) | Закривається, коли |
|---|---|---|---|---|---|---|
| `server_error` | `pageFetchState=SERVER_ERROR` | Server error (5xx) | Website | **error** | GET, статус; повтор через 30 с | парсер бачить 2xx двічі поспіль |
| `redirect_error` | `REDIRECT_ERROR` | Redirect error | Website | **error** | трейс ланцюга (`trace.py`): петля, > 10 хопів (ліміт Googlebot), порожній `Location`, фінал ≠ 2xx | ланцюг здоровий двічі поспіль |
| `not_found` | `NOT_FOUND` | Not found (404) | Website | **error** якщо в sitemap / є покази / є внутрішній лінк; інакше `warning` | статус; чи ще лінкують `referringUrls` свого хоста; кандидат-наступник для 301 | 2xx або 301→2xx двічі поспіль |
| `soft_404` | `SOFT_404` | Soft 404 | Website | **error** | 200 + відбиток 404-шаблону сайту (§2.5) — **підказка, не вердикт** | Google: `verdict=PASS` |
| `unauthorized_401` / `forbidden_403` / `blocked_4xx` | `ACCESS_DENIED` / `ACCESS_FORBIDDEN` / `BLOCKED_4XX` | Blocked due to … (401/403/other 4xx) | Website | **error** | статус двома UA (звичайний і Googlebot-подібний) — ловить бот-блокування | 2xx двічі поспіль |
| `robots_blocked` | `robotsTxtState=DISALLOWED` | Blocked by robots.txt | Website | **warning** (`error`, якщо в sitemap або був у індексі) | `robots.txt` → REP-матчер для `Googlebot` (§2.5) | шлях дозволений |
| `noindex` | `indexingState=BLOCKED_BY_META_TAG/HTTP_HEADER` | Excluded by 'noindex' tag | Website | **warning** (`error`, якщо в sitemap або був у індексі) | meta robots / `X-Robots-Tag` | `noindex` зник |
| `canonical_mismatch` | `googleCanonical ≠ userCanonical` (обидва є) | Duplicate, Google chose different canonical than user | Website | **warning** | canonical на сторінці зараз; чи 2xx ціль | Google: канонікали збіглись |
| `duplicate_no_canonical` | текст `Duplicate without user-selected canonical` | те саме | Website | **warning** | чи є `<link rel=canonical>` зараз | Google |
| `redirect` | текст `Page with redirect`, `SUCCESSFUL` | Page with redirect | Website | **notice**; `warning`, якщо URL у sitemap (гігієна sitemap) або ланцюг > 1 хопа / фінал ≠ 2xx | трейс ланцюга | — (notice) або ланцюг здоровий |
| `alternate_canonical` | текст `Alternate page with proper canonical tag` | те саме | Google | **notice** | — | — |
| `crawled_not_indexed` | текст | Crawled – currently not indexed | Google | **notice** | сайт чистий? (200, індексований, canonical на себе) — щоб відрізнити «Google не взяв» від «ми самі закрили» | Google: `PASS` |
| `discovered_not_indexed` | текст | Discovered – currently not indexed | Google | **notice** | — | Google |
| `unknown` | текст `URL is unknown to Google` | (не показується в Pages) | — | **none** — не issue | — | — |
| `indexed_blocked_robots` | `PASS` + `DISALLOWED` | Indexed, though blocked by robots.txt | Website | **warning** | REP-матчер | шлях дозволений |
| `dropped_from_index` | був `PASS` у нашому реєстрі → став не-`PASS` (будь-яка причина вище) | (тренд у GSC) | — | **critical** для ключової сторінки / sitemap, інакше **warning**; поєднується з причиною | парсер шукає причину на сайті | Google: знову `PASS` |
| `rich_result_error:<type>` / `rich_result_warning:<type>` | `richResultsResult…issues[].severity` | Enhancements → Invalid / Warning | Website | **warning** / **notice** | `schema_markup.validate_all` на живій сторінці: чи поле досі відсутнє | парсер: finding зник |
| `sitemap_errors` / `sitemap_pending` / `sitemap_unreachable` | `sitemaps.list` (`errors`/`warnings` > 0, `isPending`, наш fetch ≠ 200) | Sitemaps → Has errors / Couldn't fetch | Website | **warning** | fetch + parse (`app/monitor/sitemaps.py`) | лічильники 0 / fetch 200 |
| `orphaned_indexed_url` | `PASS`, але URL **не в sitemap** і жоден свій `referringUrl` більше не лінкує (перевірено парсером) | (нема в GSC) | — | **notice** (передбачення) | обхід referrers | лінк повернувся / URL у sitemap |
| `other` | будь-який невідомий `coverageState` | — | ? | **warning** | статус | Google |

`error`/`warning`/`notice` — це **клас issue** у модулі; серйозність зміни в моніторі виводиться з нього окремою таблицею (§2.4), а не тотожна йому.

### 1.7 Що в кодовій базі вже є — переюзати, не переписувати

| Потрібно | Готовий зразок | Файл |
|---|---|---|
| інспекція одного URL, `inspection_result_link` | `UrlInspectionClient` (task-88); **розширити парсер `richResultsResult…issues[]`** — сьогодні рахує лише кількість items | `app/urlinspector/google/index.py` (read-only → додати парсинг issues у власному модулі поверх сирого payload) |
| паралельний скан у 8 потоків, кап квоти, тризначний `is_indexed` | `app/indexing/inspect.py` (task-100) | зразок, не імпорт |
| sitemap-и клієнта з GSC + живий fetch/parse (index → children, `.gz`) | `GscClient.sitemaps`, `collect_sitemap_entries` | `app/integrations/google_api/gsc_client.py`, `app/indexing/sync.py`, `app/monitor/sitemaps.py` |
| SSRF-guard, throttle на хост, antibot-класифікація, browser-lane | `fetch`, `classify_block`, `fetch_via_browser` | `app/monitor/fetcher.py`, `app/monitor/browser_fetch.py` |
| meta robots / `X-Robots-Tag` / canonical / title / word_count з HTML | `extract_signals`, `is_indexable` | `app/monitor/page.py` |
| robots.txt → групи UA → правила (з інлайн-коментарями) | `parse_robots` | `app/monitor/robots.py` (**без** per-URL матчера — його пише ця таска, §2.5) |
| ланцюг редіректів хоп за хопом, петлі, `meta refresh` | `trace.py` (task-29) | `app/tools/redirects/trace.py` |
| валідатор структурованих даних (JSON-LD/microdata, профілі Google) | `extract_all` + `validate_all` | `app/tools/schema_markup/{extract,validate,vocab}.py` |
| запис у монітор, самозагасання, ack, mute по типу | `MonitorRepository.record_change/resolve_change/open_change`, `muted_change_types_json` | `app/db/repositories/monitor.py`, `app/db/models.py:MonitorSettings` |
| щонічний планувальник, ідемпотентний через леджер | `IndexingScheduler` | `app/indexing/scheduler.py` |
| картка-зведення на сторінці клієнта + під-сторінка | `renderMonitorSummaryCard`/`renderPageInventorySummaryCard`, `PAGES_ROUTE` | `frontend/js/client-page.js`, `frontend/js/router.js` |

### 1.8 Стосунок до Site Monitor і Indexing Planner — доповнення, не дубль

- **Site Monitor** бачить сайт **нашими очима** й тільки в межах sitemap + ключових сторінок: `sitemap_url_broken`, `page_noindex_appeared`, `page_redirect_appeared`, `canonical_changed`. Цей модуль бачить сайт **очима Google** і поза sitemap (PDF-и EFFYS у sitemap ніколи не були — монітор їх не знає), плюс Google-side причини, яких парсером не побачиш. Перетин існує (URL у sitemap віддає 404 — помітять обидва), і на перетині діє **дедуп** (§2.4): другий рядок у стрічці не створюється.
- **Indexing Planner** (task-100) — про те, **що нести в GSC на індексацію**; цей модуль — про те, **що зламано**. Спільне: обидва інспектують URL. Тому (а) свіжа інспекція з `indexing_urls` (< 24 год) **переюзається без витрати квоти**, (б) денний кап рахується **разом** із лічильником `indexing_scans` (§2.3), (в) `crawled_not_indexed`/`discovered_not_indexed` — `notice` тут із посиланням у планер, а не дубль його черги. Об'єднання двох реєстрів в один — follow-up (§5), не ця таска.

---

## 2. Дизайн

### 2.1 Контракт назви

Скрізь однаково (CLAUDE.md «Провайдер/метрика називаються однаково»): ключ `gsc_issues`, назва в UI — **Search Console Issues** (eyebrow `Search Console`), маршрут `#/client/<id>/gsc-issues`, префікс API `/api/v1/clients/{id}/gsc-issues`, пакет `app/gscissues/`, іконка `alert-octagon`. Причини в UI називаються **дослівно як у GSC** (`Not found (404)`, `Page with redirect`, …) — оператор звіряє з консоллю без перекладу; `issue_key` — внутрішній ключ.

### 2.2 Модель даних (3 таблиці + налаштування, одна міграція)

Нова ревізія створюється **процедурою task-03** (`op.create_table`, без `batch_alter_table`; перед `alembic upgrade` — `pg_dump -Fc`, як у task-98). Спільна тестова база `yovth_test` після нової ревізії потребує `alembic upgrade head` проти неї ([[yovth-test-schema-drift]]).

#### `gsc_issue_settings` — per-client політика

```
client_id             GUID UNIQUE FK clients ON DELETE CASCADE
enabled               bool default true      -- є GSC-інтеграція → модуль працює; нема → not_configured
daily_inspection_cap  int  default 1500      -- наша частка з 2000/property; рахується РАЗОМ із indexing_scans
push_to_monitor       bool default true
push_min_class        str(8) default "warning"   -- "error" | "warning" (notice в монітор не йде ніколи)
universe_sources_json JSON default ["sitemap","gsc_pages","monitor"]
long_tail_days        int  default 480       -- глибина page-dimension (16 міс = максимум API)
created_at / updated_at
```

`enabled=true` за замовчуванням свідомо відрізняється від планера (`false`): планер витрачає людські години, цей модуль — лише квоту, яка інакше простоює. Клієнт **без GSC-інтеграції** (GOREPORT) → `not_configured`, не нуль і не помилка (PLAN §12).

#### `gsc_issue_urls` — реєстр всесвіту й останній зріз інспекції

```
id, client_id FK, url String(2048), url_key String(2048)   -- UNIQUE (client_id, url_key); url_key за правилом task-88
sources_json         JSON      -- ["sitemap","gsc_pages","monitor","gsc_export","referrer"] — звідки знаємо URL
kind                 String(16)  -- "page" | "document" (pdf/doc) | "asset" (img/video/font/css/js → не інспектується)
in_sitemap           bool | NULL -- NULL = sitemap не читався в цьому синку
is_key_page          bool default false      -- top pages за кліками (той самий список, що в монітора)
clicks_90d / impressions_90d   int | NULL    -- NULL = запит page-dimension впав; 0 = запит ок, рядка нема
first_seen_at, last_seen_at, gone_from_universe_at (nullable)
-- останній зріз URL Inspection (усе nullable: None = не питали / Google не сказав)
verdict, coverage_state, page_fetch_state, robots_txt_state, indexing_state, crawled_as
google_canonical, user_canonical, last_crawl_time
referring_urls_json  JSON, sitemaps_json JSON
rich_results_json    JSON     -- [{type, items:[{name, issues:[{message, severity}]}]}]
inspection_result_link (nullable)   -- зберігається, не будується
inspected_at (nullable), inspection_source String(16)  -- "own" | "indexing"  (переюз із task-100)
is_indexed           bool | NULL   -- тризначний, як у task-100: NULL = ніколи не інспектували
rotation_group       String(16)   -- "open" | "core" | "tail"  (§2.3)
```

#### `gsc_issues` — екземпляр проблеми з життєвим циклом

```
id, client_id FK, url_id FK gsc_issue_urls ON DELETE CASCADE
scope                String(16)   -- "page" | "sitemap"
issue_key            String(64)   -- §1.6
issue_class          String(8)    -- "error" | "warning" | "notice"
source               String(8)    -- "website" | "google"
gsc_label            String(120)  -- дослівний coverageState / issueMessage (для UI і звірки з консоллю)
-- Google
first_detected_at, last_seen_by_google_at, google_last_crawl_time (nullable)
google_state         String(16)   -- "open" | "gone"  (gone = остання інспекція причину вже не показує)
google_confirmed_gone_at (nullable)
-- наш парсер
site_state           String(16)   -- "present" | "fixed" | "not_applicable" | "unknown"
site_checked_at (nullable), site_check_json JSON (докази останньої перевірки: статус, ланцюг, robots-правило, referrers…)
site_fixed_streak    int default 0     -- скільки поспіль перевірок кажуть fixed (антифлап, §2.4)
check_history_json   JSON default []   -- останні 20: [{at, state, status}]
-- життєвий цикл
resolved_at (nullable), resolution String(24)  -- "site_fixed" | "google_confirmed" | "gone_from_universe" | "muted"
monitor_change_id    String(64) nullable       -- рядок у monitor_changes, якщо створювали
related_monitor_change_id String(64) nullable  -- знайдений дубль монітора (дедуп §2.4)
acknowledged_at, acknowledged_note, muted_at, muted_by_user_id (nullable)
suggested_fix_json   JSON   -- напр. {"action":"redirect_301","target":"…/2026/09/…pdf","confidence":"name_match"}
created_at, updated_at
UNIQUE (url_id, issue_key, first_detected_at); INDEX (client_id, resolved_at, issue_class); INDEX (client_id, issue_key)
```

Один відкритий екземпляр на `(url, issue_key)` — гарантується репозиторієм (`open_issue(url_id, issue_key)`), не DDL. Повторна поява після `resolved_at` створює **новий** рядок: історія «404 → полагодили → знову 404» має бути видима як дві події, а не як одна з перезаписаною датою.

#### `gsc_issue_scans` — леджер прогонів (аудит, прогрес, квота)

```
id, client_id FK (nullable = портфельний), started_at, finished_at, status ("running"|"ok"|"partial"|"failed")
trigger ("scheduled"|"manual"|"cli"), mode ("rotating"|"full"|"recheck_only")
urls_total int|NULL, urls_inspected int, urls_reused int, urls_skipped_assets int, urls_failed int
inspections_used int          -- лічильник квоти цього прогону
issues_new int, issues_resolved int, issues_open int
universe_json JSON             -- {"sitemap": 16, "gsc_pages": 32, "monitor": 16, "sitemap_errors": [...]}
error_code, error_message, owner String(64), heartbeat_at   -- як archive_captures / indexing_scans
```

### 2.3 Конвеєр: п'ять кроків, кожен ізольований по клієнту

```
universe  ->  inspect  ->  classify  ->  verify  ->  lifecycle (+ notify)
```

Клітинка `(client, крок)` ізольована (PLAN §9.1): виняток на одному клієнті не валить портфельний прогін; `GoogleApiConfigError`/`GoogleApiPermissionError` → `not_configured`, як у `app/urlinspector/google_service.py`.

**1. `universe`** (`app/gscissues/universe.py`). Джерела, кожне вимикається в налаштуваннях і кожне має свій стан у `universe_json`:

- `sitemap` — `GscClient.sitemaps(site)` → `collect_sitemap_entries` (як task-100; битий sitemap → `partial` + `sitemap_unreachable`-issue, URL зі старого синку **не** позначаються зниклими);
- `gsc_pages` — `search_analytics(site, end-480d, end, dimensions=["page"])` + окремо `end-90d` для `clicks_90d/impressions_90d`. Для URL-prefix property URL з іншого хоста/схеми відсіюються **до** інспекції (API відповість помилкою «not in property»); для `sc-domain` — усі субдомени законні;
- `monitor` — `monitor_pages` клієнта (сторінки, знайдені обходом, включно з не-sitemap);
- `referrer` — свої `referringUrls` з попередніх інспекцій (URL, який лінкує на битий, теж належить всесвіту);
- `gsc_export` — етап 9 (§3), опціонально.

Розширення `.webp/.jpg/.png/.gif/.svg/.mp4/.woff*/.css/.js` → `kind="asset"`, не інспектуються; `.pdf/.doc*/.xls*` → `document`, інспектуються.

**2. `inspect`** (`app/gscissues/inspect.py`). Пул на `gsc_issues_inspect_workers` (8). Порядок — за **групою ротації**, а не за алфавітом:

| Група | Хто | Частота |
|---|---|---|
| `open` | URL з відкритим issue | **щодня** (щоб побачити, чи Google перекраулив: змінився `lastCrawlTime`) |
| `core` | в sitemap, або `impressions_90d > 0`, або ключова сторінка, або був у індексі | ≥ 1 раз на 7 днів |
| `tail` | решта (довгий хвіст 16 місяців без показів за 90 днів) | ≥ 1 раз на 30 днів |

Перед викликом — **переюз**: якщо `indexing_urls` має той самий `url_key` з `inspected_at` < 24 год, зріз береться звідти (`inspection_source="indexing"`), квота не витрачається (rich results у планера немає — для `core`-групи з rich-result-issue інспектуємо самі). **Кап квоти рахується спільно**: `used_today = indexing_scans.urls_inspected(сьогодні, client) + gsc_issue_scans.inspections_used(сьогодні, client)`; при `used_today ≥ min(daily_inspection_cap, 1800)` прогін зупиняється, стає `partial` і каже, скільки URL лишилось — не впирається в 429.

Арифметика ALPHA: `open` (десятки) + `core` (~2 500 / 7 ≈ 360/день) + `tail` (~3 700 / 30 ≈ 125/день) ≈ 500 інспекцій/день — у межах. Решта клієнтів — повний всесвіт щотижня.

**3. `classify`** (`app/gscissues/classify.py`). Чиста функція `classify(url_row, inspection, context) -> list[IssueCandidate]` за таблицею §1.6, enum-first. `context` = `in_sitemap`, `is_key_page`, `impressions_90d`, `was_indexed` (попередній `is_indexed`), власні referrers. `is_indexed IS NULL` не читається як `False` ніде (урок task-100). `unknown` ніколи не породжує issue.

**4. `verify`** (`app/gscissues/verify.py`) — власні парсери, §2.5. Запускається **в тому ж прогоні одразу після класифікації** для кожного відкритого й нового issue (кілька десятків GET на клієнта, через `fetch` з `HostThrottle`), а також окремим легким проходом `recheck_only` кожні `gsc_issues_recheck_hours` (6) для відкритих `website`-issues, і по кнопці.

**5. `lifecycle` + `notify`** (`app/gscissues/lifecycle.py`, `notify.py`) — §2.4.

### 2.4 Життєвий цикл, серйозність і запис у монітор

**Відкриття.** Кандидат без відкритого екземпляра `(url_id, issue_key)` → новий рядок `gsc_issues`, `site_state` з першої перевірки. Потім — рішення про монітор:

| Умова | Дія |
|---|---|
| прогін — **перший** для клієнта (baseline, як task-35 §3.4) | у монітор **не** пишемо нічого, крім одного `info` `gsc_issues_baseline` з лічильниками по причинах і прикладами |
| `issue_class == notice` | ніколи не в монітор |
| `push_to_monitor=false` або клас нижчий за `push_min_class` | не в монітор |
| `site_state == fixed` уже на момент відкриття (Google бачив дефект тижні тому, сайт уже полагоджений) | не в монітор; рядок живе як `awaiting Google` |
| у монітора **вже є відкрита** зміна на той самий `subject` еквівалентного типу (`sitemap_url_broken`↔`not_found`, `page_noindex_appeared`↔`noindex`, `sitemap_url_redirects`/`page_redirect_appeared`↔`redirect`, `robots_blocks_site`↔`robots_blocked`, `canonical_changed`↔`canonical_mismatch`, `sitemap_lost`/`sitemap_unavailable`↔`sitemap_*`) | **дедуп**: не створюємо, пишемо `related_monitor_change_id`, UI показує «Google confirms the monitor's finding since <date>» |
| один прогін дає > `gsc_issues_burst_threshold` (10) нових issue одного `issue_key` | **один** агрегат `gsc_issues_burst` із лічильником і 5 прикладами (як `sitemap_broken_urls`, task-35 §3.4.1), не N рядків |
| інакше | `MonitorRepository.record_change(scope="gsc", change_type=f"gsc_issue_{issue_key}", subject=url, severity=…, details={gsc_label, site_state, site_check, impressions_90d, inspection_result_link, suggested_fix})`; `monitor_change_id` зберігається |

Серйозність зміни в моніторі (`app/gscissues/severity.py` — власна таблиця, щоб не правити `app/monitor/severity.py`; якщо в реалізації виявиться, що UI/API монітора вимагає ключ у `STATIC_SEVERITY`, додається адитивно й оголошується в Completion report):

| `issue_class` × контекст | Серйозність |
|---|---|
| `error` і (URL у sitemap, або ключова сторінка, або `impressions_90d ≥ 100`) | **critical** |
| `error` інакше (сирітська 404 з кількома показами) | warning |
| `dropped_from_index` для ключової / sitemap-сторінки | **critical** |
| `warning` | warning |
| `sitemap_*` | warning |
| `*_resolved` | info (за конвенцією `resolved_change_type`) |

**Mute** по типу з `MonitorSettings.muted_change_types_json` працює автоматично, бо типи — звичайні `change_type`; плюс власний `muted_at` на екземплярі (навмисні 404 — «так, цей PDF ми прибрали свідомо»).

**Закриття** — правило залежить від класу, і це головне рішення таски:

- `source=website` (сайт лагодить власник): `resolved_at` ставиться, коли **наш парсер** бачить fixed **двічі поспіль з інтервалом ≥ 1 год** (`site_fixed_streak ≥ 2` — антифлап: короткий 5xx-фліп не має закривати й одразу відкривати рядок). Монітор-зміна отримує `resolve_change` + `gsc_issue_<key>_resolved` (info) з `details.google_confirmed=false`. Коли пізніше Google перекраулить і причина зникне — `google_confirmed_gone_at` у нашому рядку, **без** другої події в моніторі (щоб стрічка не подвоювалась).
- `source=google` (`crawled_not_indexed`, …): закривається лише за Google (`google_state=gone`), парсер не може «полагодити» те, що вирішує Google.
- URL зник зі всесвіту (`gone_from_universe_at`) і Google його не знає → `resolution="gone_from_universe"`.
- **Повторна поява** після закриття → новий екземпляр і **нова** зміна в моніторі (це справжня новина).

Усі записи в монітор — тільки через `MonitorRepository`; рядки монітора **не редагуються** (їх контракт — mutable only for ack/resolve).

### 2.5 Власні парсери — що саме перевіряється

Усе через `app/monitor/fetcher.fetch` (SSRF-guard, throttle на хост, кап тіла 3 MB, antibot-класифікація). Якщо `classify_block` каже, що нас блокують (task-50), `site_state="unknown"` — **ніколи** `fixed`. Для клієнтів із `fetch_mode="browser"` — `fetch_via_browser`.

| Клас | Перевірка | Докази в `site_check_json` |
|---|---|---|
| статусні (`server_error`, `not_found`, `4xx`) | GET (не HEAD — частина серверів бреше на HEAD, урок task-29); для 401/403 — другий запит з Googlebot-подібним UA, щоб побачити бот-блокування | `status`, `final_url`, `hop_count`, `elapsed_ms`, `ua_variant_status` |
| `not_found` додатково | для кожного `referringUrl` **свого хоста** (чужі не фетчимо — межа SSRF/скоупу): GET, парсинг `<a href>`/`<link>`/`<img src>`, чи є лінк на битий URL (з нормалізацією `url_key`) → `still_linked_from: [...]`; кандидат-наступник: той самий `basename` у `gsc_issue_urls` клієнта з `PASS` (EFFYS: `2025/10/X.pdf` → `2026/06/X.pdf` → `2026/09/X-2027.pdf`) → `suggested_fix_json` | `still_linked_from`, `suggested_target`, `confidence` |
| `redirect`, `redirect_error` | `app/tools/redirects/trace.py` (петлі, ≤ 10 хопів як у Googlebot, `meta refresh`, фінальний статус, canonical фіналу) | `chain[]`, `final_status`, `loop` |
| `robots_blocked`, `indexed_blocked_robots` | `robots.txt` → `parse_robots` → **новий REP-матчер** `is_allowed(doc, "Googlebot", path)` у `app/gscissues/rep.py`: група `Googlebot` інакше `*`; найдовше правило перемагає, при рівній довжині `Allow` б'є `Disallow`; `*` і `$` (RFC 9309 / semantics Google). `urllib.robotparser` не береться: у нього перший-збіг-виграє, і на `Allow: /wp-admin/admin-ajax.php` + `Disallow: /wp-admin/` (стандартний WordPress) він помиляється | `matched_rule`, `group` |
| `noindex` | `extract_signals` → `effective_meta_robots` + `X-Robots-Tag` | `meta_robots`, `x_robots_tag` |
| `canonical_mismatch`, `duplicate_no_canonical` | `canonical` зі сторінки, статус цілі | `canonical`, `google_canonical`, `target_status` |
| `soft_404` | 200 + **відбиток 404-шаблону сайту**: один раз на прогін фетчимо гарантовано неіснуючий `/<uuid>-yovth-probe`, беремо `title`/`text_sha256`/`word_count`; сторінка вважається «схожою на 404» при збігу title-патерну (`not found`, `nothing found`, `no results`) або тексту < 60 слів. **Підказка, не вердикт** (урок task-92: similarity ніколи не вердикт) — закриває тільки Google | `probe_title`, `page_title`, `word_count`, `looks_like_404_template` |
| `rich_result_*` | `extract_all(html)` + `validate_all(…, vocab)`; порівнюємо `issueMessage` Google («Missing field 'X'») з нашим finding по `X` | `findings[]`, `matched` |
| `sitemap_*` | fetch + `parse_sitemap` | `status`, `url_count`, `parse_error` |
| `crawled_not_indexed` | «сайт чистий?»: 200, індексований, canonical на себе, `word_count` | `clean_site_side` |

### 2.6 API (`/api/v1/clients/{id}/gsc-issues`, читання `viewer`, мутації `member`+)

| Метод | Шлях | Що робить |
|---|---|---|
| `GET` | `/summary` | для картки: стан (`not_configured`/`never_scanned`/`running`/`partial`/`ok`/`failed`), лічильники відкритих по класах і по `site_state`, покриття (`inspected_30d / universe`), останній скан, топ-5 issue з `site_state` |
| `GET` | `/` | таблиця причин, як у GSC: `issue_key`, `gsc_label`, `source`, `class`, `open_count`, `present/fixed/unknown` за парсером, тренд 30 днів, `first_detected_at` |
| `GET` | `/urls?issue_key=&class=&site_state=&in_sitemap=&has_traffic=&state=open|resolved|all&sort=` | плоский список екземплярів з URL-контекстом (пагінація) |
| `GET` | `/urls/{url_id}` | картка URL: зріз інспекції, усі issue (відкриті й закриті), історія перевірок, referrers, трафік, `inspection_result_link` |
| `POST` | `/scans` `{mode}` | ручний скан (`rotating`/`full`/`recheck_only`); другий одночасний — 409 |
| `GET` | `/scans/{scan_id}` | прогрес |
| `POST` | `/issues/{issue_id}/recheck` | наш парсер зараз (синхронно, ≤ 15 с, без квоти) |
| `POST` | `/issues/{issue_id}/reinspect` | Google зараз (1 одиниця квоти, ~7 с) |
| `POST` | `/issues/{issue_id}/ack` `{note}` / `/mute` / `/unmute` | ack не закриває; mute закриває з `resolution="muted"` і резолвить зміну в моніторі |
| `PUT` | `/settings` | §2.2 |
| `GET` | `/export.xlsx` | усі відкриті issue з доказами — з того самого DTO, що й екран |
| `POST` | `/import` (етап 9) | zip/CSV експорту GSC → URL у всесвіт (`sources_json += "gsc_export"`) + інспекція |

Усе під загальним `AuthMiddleware`; жодного нового винятку в allowlist. Мутації — у журнал дій (task-72): `client.gsc_issues.scan`, `.recheck`, `.reinspect`, `.ack`, `.mute`, `.settings`, `.import`.

### 2.7 Фронт

**Картка на `#/client/<id>`** (`gsc-issues-card.js`, секція `#cp-gsc-issues` між Site Monitor і Page Inventory): eyebrow `Search Console`, заголовок `Issues`, бейджі `N errors · M warnings · K notices`, рядок покриття `31 of 33 known URLs inspected in the last 30 days`, топ-3 issue у формі `Not found (404) · 2 URLs · still present (checked 2 h ago)`, кнопки `Open issues` / `Scan now`. Стани: `not_configured` («Search Console is not connected» — той самий текст, що в GSC-гейті сторінки), `never_scanned` (кнопка `Run first scan`), `running` (прогрес зі скану), `partial` (кап квоти: «Inspected 1 500 of 6 234 — continues tomorrow»), `ok`, `failed` (з причиною і `Retry`).

**Під-сторінка `#/client/<id>/gsc-issues`** (`gsc-issues-page.js`), вкладки:

- **Issues** (за замовчуванням) — таблиця причин у порядку клас→кількість, кожна причина розкривається в список URL. Колонки: `Reason` (дослівно GSC + бейдж `Website`/`Google`), `Class`, `Pages`, `On site now` (`present N / fixed M / unknown K` — це і є «одразу з перевіркою власними парсерами»), `Trend (30d)`, `First seen`. Рядок URL: URL, `Google saw` (`lastCrawlTime`), `On site` (чіп `Still 404 · 14:20` / `Fixed on site · awaiting Google` / `Blocked, couldn't check`), `Traffic at stake` (кліки/покази 90 днів; `—`, якщо `NULL`), `Suggested fix` (`301 → …/2026/09/….pdf`), дії `Re-check` · `Re-inspect` · `Open in GSC` · `Ack` · `Mute`. Секція `Evidence` — розкривна, з `site_check_json` людською мовою (речення будуються з полів, не зберігаються готовими).
- **URLs** — весь всесвіт із фільтрами (`in sitemap`, `indexed`, `never inspected`, `asset`), щоб «N із M» можна було перевірити очима.
- **History** — закриті issue з `resolution` і датами, повторні появи видимі як окремі рядки.
- **Settings** — §2.2, плюс `Import GSC export` (етап 9).

**Монітор** (`#/monitor`, `#/client/<id>/monitor`): нові `change_type` отримують лейбли в `CHANGE_TYPE_LABELS` (`Search Console: Not found (404)`, …), новий `scope="gsc"` — іконка `brand-google` і чип-фільтр `Search Console` (клієнтська фільтрація по `change_type.startsWith("gsc_issue")`, без зміни API монітора; ліміт стрічки `limit=100` лишається, як є). Клік по зміні веде на `#/client/<id>/gsc-issues?url=…`.

Обов'язкове з `frontend/AGENTS.md`: усі стани §6 (skeleton, empty, error, not_configured, partial, ok, running), жодного inline `style=` (CSP — [[frontend-verification-tooling]]), кожен ES-import із власним `?v=` і піднятим ланцюжком до `index.html` ([[frontend-module-cache-bust-chain]]), таблиці з `contain: paint` ([[table-scroll-contain-paint]]), уся копірайт-частина англійською.

### 2.8 Планувальник, налаштування, CLI

`GscIssuesScheduler` у `app/gscissues/scheduler.py`, зареєстрований у `app/jobs/worker.py` поруч із `IndexingScheduler`. Тік 60 с. Раз на добу після `gsc_issues_scan_at_utc` (default `03:30` — **після** планера о `02:00`, щоб переюзати його свіжі інспекції) — повний цикл §2.3 у режимі `rotating`; кожні `gsc_issues_recheck_hours` (6) — `recheck_only` для відкритих `website`-issues (лише HTTP, без квоти). Ідемпотентність через леджер `gsc_issue_scans`, не пам'ять потоку. Клієнт на паузі (task-34) — поза розкладом, як у монітора.

Нові налаштування в `app/core/config.py`: `gsc_issues_enabled` (default `false` — вмикає оператор через `.env`, як `INDEXING_ENABLED`), `gsc_issues_scan_at_utc`, `gsc_issues_recheck_hours`, `gsc_issues_inspect_workers` (8), `gsc_issues_daily_cap_default` (1500), `gsc_issues_burst_threshold` (10), `gsc_issues_critical_impressions` (100).

CLI:

```
yovth-report gsc-issues-scan   [--client SLUG] [--mode rotating|full|recheck_only]
yovth-report gsc-issues-list    --client SLUG [--class error|warning|notice] [--state open|resolved]
yovth-report gsc-issues-export  --client SLUG --out issues.xlsx
```

### 2.9 Пастки, які цей дизайн закриває свідомо

1. **«Усі помилки» ≠ всесвіт із sitemap.** Обидві 404 EFFYS — PDF-и, яких у sitemap ніколи не було (§1.3). Тому всесвіт = sitemap ∪ GSC pages ∪ monitor ∪ referrers, і UI завжди показує покриття «N із M», а не претендує на повноту.
2. **`URL is unknown to Google` — не помилка.** 12 із 33 URL EFFYS (§1.3), включно з `/locations/`, який є в sitemap. Це територія Indexing Planner; тут — `none`.
3. **Зображення з image-показами дають `unknown`** і спалюють квоту (9 із 33 у EFFYS, 199 у ALPHA) → `kind="asset"`, не інспектуються, але рахуються у всесвіті.
4. **ALPHA не влазить у 2000/добу** (§1.5) → ротація груп; кап спільний із `indexing_scans`; при досягненні — `partial` із залишком, не 429.
5. **Типографські лапки й вільний текст `coverageState`** → enum-first класифікація, невідомий текст → `other` із сирим рядком.
6. **Google бачив дефект тижні тому, а сайт уже полагоджено** → `site_state=fixed` на відкритті, у монітор не йде; UI показує `awaiting Google` (аналог «Validation started»).
7. **Флап** (короткий 5xx, миготливий CDN) → закриття лише після двох `fixed` поспіль з інтервалом ≥ 1 год.
8. **Подвійна тривога з монітором** (URL у sitemap віддає 404 — бачать обидва) → дедуп по `subject` + еквівалентному типу, крос-посилання замість другого рядка.
9. **Baseline-повінь**: перший скан 3XE дав би 17+ рядків у стрічку одразу (§task-100 1.5) → baseline-правило й агрегат при бурсті.
10. **Бот-блокування** (task-50): «не змогли перевірити» ніколи не читається як «полагоджено» — `unknown`.
11. **`urllib.robotparser` бреше на стандартному WordPress-robots** → власний REP-матчер із тестами на `Allow`/`Disallow` однакової довжини, `*`, `$`.
12. **Soft 404 і «схожість» — не вердикт** → лише підказка; закриває Google.
13. **`referringUrls` містять чужі хости** (`m.m.yelp.com`, `?ref=aftership`) → фетчимо лише свій хост; чужі показуємо як текст.
14. **URL-prefix property** (`https://effyscafe.com/`, GRP): `http://`/`www.`-варіанти з page-dimension відсіюються до інспекції, інакше API повертає помилку на кожен.
15. **Процес-глобальний планувальник у тестах** ([[global-test-singleton-db-leak]]) → вимкнений за замовчуванням у `tests/conftest.py`, вмикається маркером.
16. **Секрети**: `inspection_result_link` секретів не містить; `site_check_json` зберігає лише статуси/URL/правила, ніколи заголовки авторизації чи cookies.

---

## 3. Етапи

| # | Етап | Вихід | Перевірка |
|---|---|---|---|
| 1 | **Модель і міграція** — 3 таблиці + settings, репозиторій, DTO | `app/db/models.py` (у кінець), нова ревізія, `app/schemas/gsc_issues.py`, `app/db/repositories/gsc_issues.py` | `alembic upgrade head` на копії бекапу і на `yovth_test`; юніти репозиторію (один відкритий екземпляр на `(url, key)`, повторна поява = новий рядок) |
| 2 | **`universe`** — sitemap ∪ GSC pages (live) ∪ monitor ∪ referrers, `kind`, трафік 90 днів | `app/gscissues/universe.py` | EFFYS: 33 URL, 9 asset, 2 document-404 у всесвіті; ALPHA: > 6 000 із розкладом по групах; SWM: битий sitemap → `partial`, URL не позначені зниклими |
| 3 | **`inspect`** — пул, ротація, переюз із `indexing_urls`, спільний кап | `app/gscissues/inspect.py` | ALPHA: скан зупиняється на капі з `partial` і залишком; LAXTON: 0 власних інспекцій для URL, які планер інспектував < 24 год тому |
| 4 | **`classify`** — таблиця §1.6, enum-first, парсинг rich-result issues | `app/gscissues/classify.py` | юніти на кожен рядок §1.6, включно з типографськими лапками, `unknown → none`, `dropped_from_index`, невідомий текст → `other` |
| 5 | **`verify`** — парсери §2.5, REP-матчер, 404-відбиток, referrers, suggested fix | `app/gscissues/{verify,rep}.py` | EFFYS: обидві 404 `present`, `still_linked_from=[]`, `suggested_target=…/2026/09/…-2027.pdf`; `/locations/manhattan/` → ланцюг 1 хоп, `notice`; юніти REP на WordPress-robots |
| 6 | **`lifecycle` + `notify`** — відкриття/закриття, антифлап, baseline, бурст, дедуп, запис у монітор | `app/gscissues/{lifecycle,notify,severity}.py`, адитивно `app/schemas/monitor.py` (`"gsc"`) | другий прогін на EFFYS створює 2 зміни `gsc_issue_not_found` (critical: покази ≥ 100 за 90 днів у York-PDF; warning для W96th), перший — лише `gsc_issues_baseline`; фейковий фікс → закриття тільки після 2-ї перевірки |
| 7 | **API + audit + планувальник + config + CLI** | `app/api/client_gsc_issues.py`, `app/audit/actions.py`, `app/gscissues/scheduler.py`, `app/jobs/worker.py`, `app/core/config.py`, `app/cli.py` | contract-тести, 403 для `viewer`, два тіки → один прогін, `openapi_snapshot.json` оновлено |
| 8 | **Фронт: картка + під-сторінка + монітор** | `gsc-issues-card.js`, `gsc-issues-page.js`, `gsc-issues.css`, адитивно `client-page/router/api/formatters/monitor-*.js`, `index.html` | візуальний loop `AGENTS.md §15`, усі стани, `0 помилок консолі` браузера, e2e `.cjs` проти живого стека |
| 9 | **(опційно) Import GSC export** — zip/CSV із UI GSC → всесвіт + інспекція | `app/gscissues/importer.py`, `POST /import`, форма в Settings | формат звіряється на першому реальному експорті (`Table.csv`: `URL`, `Last crawled`); URL додаються з `sources_json=["gsc_export"]` |
| 10 | **Живий прогін end-to-end** | — | §4 Gate |

Етапи 2–6 незалежні від 7–8 за контрактом DTO — фронт кодується проти оголошених схем (CLAUDE.md §3).

---

## 4. Definition of Done / Gate

**Функціонально**

1. На `#/client/338e0b08-…` (EFFYS) картка `Search Console · Issues` показує `2 errors`, покриття `31 of 33`, обидві 404 з чіпом `Still 404` і `Suggested fix: 301 → …/2026/09/…-July-2027.pdf`; `Page with redirect` — у `notices`, не в errors.
2. Під-сторінка `#/client/<id>/gsc-issues` показує таблицю причин **дослівно як у GSC** із колонкою `On site now` (present/fixed/unknown) і розкривними URL з доказами; `Re-check` оновлює `site_state` без витрати квоти; `Re-inspect` витрачає рівно 1 одиницю.
3. Нова помилка (не baseline) з'являється в `#/monitor` і в `#/client/<id>/monitor` як зміна `gsc_issue_<key>` з правильною серйозністю; фільтр `Search Console` її відбирає; клік веде на рядок issue.
4. Після фіксу на сайті рядок закривається лише після двох `fixed` поспіль, у моніторі з'являється `gsc_issue_<key>_resolved` (info), а Google-підтвердження пізніше **не** створює другої події.
5. Клієнт без GSC (GOREPORT) → `not_configured`; клієнт з `enabled=false` → те саме, з кнопкою увімкнути; жодного нуля замість відсутності.

**Коректність (PLAN §12)**

6. `is_indexed IS NULL` ніде не читається як `False`; `unknown` не породжує issue; `impressions_90d IS NULL` показується як `—`, не `0`.
7. `sitemaps.contents[].indexed` не використовується (grep по `app/gscissues/` порожній).
8. Битий sitemap / бот-блокування / впалий page-dimension → `partial`/`unknown` із причиною, ніколи «полагоджено» або «зникло».
9. Спільний кап: сума інспекцій `indexing_scans` + `gsc_issue_scans` за добу на property ≤ 1 800 (перевірено на ALPHA двома прогонами поспіль).

**Безпека / інфраструктура**

10. Мутації — `member`+, `viewer` → 403; кожна в журналі дій. Зовнішні `referringUrls` не фетчаться. `site_check_json` без заголовків/cookies.
11. `GscIssuesScheduler` вимкнений у `tests/conftest.py`; прогін сюїти не додає рядків у бойові `gsc_issue_*`/`monitor_changes` (`count(*)` до/після).
12. `ruff check .`, `mypy app`, `pytest tests/unit/gscissues tests/unit/api/test_client_gsc_issues_api.py` — зелені у Docker.

**Фронт (`frontend/AGENTS.md`)**

13. Чекліст §15–16: усі стани, без inline `style=`, ланцюжок `?v=` до `index.html`, копірайт англійською, таблиці не скролять сторінку.

**Живий gate**

14. **EFFYS**: п. 1–2 плюс `/locations/` не в issues (`unknown → none`), 9 `.webp` — `asset`, не інспектовані.
15. **3XE**: 12 redirect / 3 robots / 1 чужий канонік / 1 404 (task-100 §4 п.20) розкладені по класах; 4 мертві sitemap-и → `sitemap_unreachable` (warning) і дедуп із `sitemap_lost`/`sitemap_unavailable` монітора, де вони вже відкриті.
16. **ALPHA**: всесвіт > 6 000, перший прогін `partial` на капі, наступного дня продовжує з `tail`, покриття росте, а не скидається.
17. **LAXTON**: 4 `noindex` WooCommerce-сторінки (`/varukorg/`, `/mitt-konto/`, фільтри) → `warning`, **не** critical (не в sitemap? — якщо в sitemap, то `error` за таблицею §1.6, і це чесна знахідка для оператора, а не хибна тривога); `Re-check` показує `noindex` присутній.
18. **SHB** (99 % покриття): картка `0 errors`, `notices` лише з `crawled_not_indexed`; жодного вигаданого рядка.

---

## 5. Свідомо поза обсягом

- **Скрейпінг GSC UI** (Playwright під Google-логіном агенції) заради самого звіту Pages — крихко, ризик блокування акаунта, і саме тому Google цього не дає через API. Якщо колись знадобиться повнота понад наш всесвіт — етап 9 (ручний експорт) закриває 95 % цього дешевше.
- **Core Web Vitals / Page Experience** — інший API (CrUX, API-ключ, інша модель даних) → окрема таска.
- **Manual actions / Security issues / Removals / Links** — API немає; монітор уже ловить втрату верифікації (`gsc_verification_lost`).
- **Автоматичне виправлення** (створити 301 через плагін Redirection, прибрати noindex) — модуль пропонує `suggested_fix`, виконує людина; запис у CMS клієнта — окрема таска з окремим ризик-аналізом.
- **Об'єднання `indexing_urls` і `gsc_issue_urls` в один реєстр інспекцій** — правильно, але потребує рефакторингу task-100; тут — переюз на читання + спільний кап, follow-up `TODO(task-10x)`.
- **Портфельна сторінка issues по всіх клієнтах** — стрічка `#/monitor` уже дає портфельний зріз нових помилок; окремий екран — після того, як модуль поживе на реальних даних місяць.
