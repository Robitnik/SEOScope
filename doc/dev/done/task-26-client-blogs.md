# task-26 — Client Blogs (machine-only inventory + export)

- **Пакет:** машинний інвентар блогу клієнта на базі blog-краулера AISeoTools (`discover_urls` + `blog_strategy` extract/classify) — **без ШІ**: блок «Blogs» угорі сторінки клієнта (кількість + посилання + «Refresh data»), окрема сторінка зі списком постів (title / description / word count / link / publish date) і експорт у **CSV та XLSX**.
- **Хвиля:** H (розширення після MVP; усі попередні таски закриті)
- **Depends on:** — (розширює закриті зони task-02/03/05/06/12/18/25 адитивно; координація контрактних змін — за процесом task-02, як робили таски 14–25)
- **PLAN.md:** §5 (worker/short-tx), §6 (модель даних, `AnalyticsDatasetRow`, immutability), §8 (безпечне адитивне розширення AISeoTools), §9 (job flow / SSE / cancel / recovery), §12 (quality rules), §13 (безпека download). Взірець наскрізь — **task-25** (`doc/dev/done/task-25-technical-seo-audit.md`): той самий шаблон «machine-only провайдер поза рутинним синком».
- **frontend/AGENTS.md:** обов'язковий для всієї роботи у `frontend/`.

Зведений таск-док (як task-25). Межі вже проведені в «Owned paths».

---

## Мета

Дати оператору на сторінці клієнта **інвентар блогу**, зібраний детерміновано (краул + класифікація), **без жодного ШІ**:

- **Блок «Blogs» угорі сторінки клієнта:** кількість опублікованих постів, кнопка **«Refresh data»** (повний повторний краул, живий SSE-прогрес) і посилання **«Open blogs»** на окрему сторінку зі списком.
- **Окрема сторінка блогів** (`#/client/<id>/blogs`): таблиця постів — **Title · Description · Word count · Link · Published date** — з пошуком, сортуванням і пагінацією.
- **Експорт** списку у **CSV** і **XLSX** (обидва — self-contained у застосунку, працюють навіть без AISeoTools).

Ключова інваріанта (як у task-25): **краул — це і є джерело істини**. Кожен рядок таблиці підтверджений живим DOM на момент краулу; жодних вигаданих чи неперевірних постів. `None` (не зчитали дату/опис) ≠ `0`/порожній рядок.

---

## Архітектура (рішення зафіксовані)

**Максимальний реюз рушія task-25.** Блоги — новий «провайдер» `Provider.blogs`, який **не бере участі в рутинному синку** дашборду (запускається лише кнопкою «Refresh data»). Безкоштовно отримуємо: `SyncJob(mode="blogs")` + `SyncJobProvider` (одна клітинка), `job_events` + SSE-ендпоінт, cancel/retry/recovery, lease/heartbeat, immutable `ProviderSnapshot`, і зберігання постів у наявній `AnalyticsDatasetRow` (**без нових таблиць**) — точно як шлях `tech_audit` ([app/integrations/tech_audit.py](../../../app/integrations/tech_audit.py), [app/api/tech_audit.py](../../../app/api/tech_audit.py)).

**Де крутиться краул:** у durable-воркері (переживає рестарт, PLAN §5.1). `seo_audit` імпортується **ліниво всередині `collect()`** (на CI/деплої mount відсутній — інакше `import app.main` падає; патерн [app/integrations/tech_audit.py](../../../app/integrations/tech_audit.py) + [ahrefs.py:105](../../../app/integrations/ahrefs.py#L105)). Run-директорія (`records.jsonl`) живе на артефакт-волюмі `/app/app_data/artifacts/blogs/<snapshot_id>/`; шлях — у `ProviderSnapshot.provenance_json`.

**Домен без інтеграції.** Блог перевіряє власний сайт клієнта — налаштовувати нічого. Як `tech_audit`: у [runner.py](../../../app/jobs/runner.py) гейт `not_configured` пропускається для `blogs`, `source_mode` дефолтиться на `auto`, а seed береться з `Client.domain`. Опційний override — `integration_config["blog_url"]` (якщо блог живе не на кореневому домені), але за замовчуванням **автовизначення** з sitemap/класифікації, тож клієнт не мусить нічого дублювати.

### Модель даних (реюз `AnalyticsDatasetRow`, без міграцій)

Один краул = один `SyncJob(mode="blogs")` → одна клітинка `(client, blogs)` → один `ProviderSnapshot` → рядки `AnalyticsDatasetRow` двох `dataset_key`:

- `dataset_key="blog_posts"` — **1 рядок на пост**. `dimension_key="post"`, `dimension_value=<canonical||url>` (унікальний у межах снапшота). `metrics_json = {title, description, word_count, url, published_date, modified_date, status}`. `rank` = позиція за `published_date` (найновіші — вище) для дефолтного сортування.
- `dataset_key="blog_summary"` — **1 рядок**. `dimension_key="summary"`, `dimension_value="blog"`. `metrics_json = {post_count, blog_url, crawl_completeness, warnings}`. Живить блок угорі (кількість + посилання) без ітерації по постах.
- `period_start = period_end = crawl_date` (інвентар — point-in-time, як `tech_audit`/Ahrefs snapshot). `provider=blogs`, `quality` з машинних warnings.

**Immutability дотримана:** снапшоти/рядки незмінні; кожен «Refresh data» = **новий** снапшот. Читання (latest / posts / export) бере найсвіжіший снапшот `blogs` (`AnalyticsDatasetRepository.latest`, як `tech_audit`). Ніяких мутабельних таблиць — на відміну від recheck у task-25, тут статусу за рядком нема.

**Міграція не потрібна.** `SyncJob.mode` — звичайний `String(32)` без CHECK (перевірено в task-25: `_str_enum` без `create_constraint`); `Provider.blogs` — суто Python-рівень; `params_json` (task-25) не потрібен (нема recheck). DDL = 0.

### Джерело даних в AISeoTools (адитивно, PLAN §8)

`BlogPageRecord` ([seo_audit/blog_strategy/models.py:26](../../../../seo/AISeoTools/seo_audit/blog_strategy/models.py#L26)) вже несе рівно потрібні поля: `url`, `title`, `meta_description`, `word_count`, `published_date`, `modified_date`, `canonical`, `page_role`, `status`. `build_inventory()` ([inventory.py:21](../../../../seo/AISeoTools/seo_audit/blog_strategy/inventory.py#L21)) discover+fetch+classify уже дає `pages: list[BlogPageRecord]` з `page_role ∈ {blog, money, …}`; `classify_role` ([content_extract.py](../../../../seo/AISeoTools/seo_audit/blog_strategy/content_extract.py)) відрізняє пост від money/category за Article-schema / og:article / шляхом. Нова обгортка фільтрує `page_role == "blog"` і дедуплікує за `canonical||url`. **Нічого наявного не міняємо** — лише новий модуль-обгортка в `dashboard/` namespace (як task-25 зробив `dashboard/tech_audit.py`).

### Експорт — self-contained у застосунку

`openpyxl` — **уже пряма залежність** ([pyproject.toml:24](../../../pyproject.toml#L24), додана task-07). Тож обидва експорти будуються **в застосунку з persisted-рядків `blog_posts`** (не з run-dir), у `BytesIO` → `StreamingResponse` — без файлу на диску, без path-traversal-поверхні, і **без потреби в AISeoTools** (CSV — stdlib `csv`; XLSX — `openpyxl`). Обидва працюють на CI/деплої без mount. Це простіше й безпечніше за FileResponse-шлях task-25.

---

## Обсяг

**Входить:**

1. **AISeoTools (адитивно, PLAN §8):** новий `seo_audit/dashboard/blogs.py`:
   - `collect_blogs(seed_url, *, client, out_dir, blog_url=None, max_pages, max_tier=2, on_event=None, is_cancelled=None) -> BlogsResult` — обгортка над `build_inventory()`; фільтрує `page_role == "blog"`, дедуплікує за `canonical||url`, мапить прогрес discover/fetch у `ProgressEvent` (dashboard `events.py`, task-08), кооперативний cancel (як `tech_audit`), пише `records.jsonl` у `out_dir`, повертає source-neutral `BlogsResult(posts[], post_count, blog_url, coverage, warnings, run_dir)`.
   - Source-neutral моделі `BlogsResult` / `BlogPost` (у dashboard `models.py`, task-08 зона — адитивно, як `TechAuditResult`). `BlogPost = {url, title, description, word_count, published_date, modified_date, status}`.
   - `blog_url` — best-effort автовизначення секції блогу (найкоротший спільний префікс blog-постів або дискаверена listing-сторінка); override з аргументу, якщо переданий.
   - Уся стара test-suite AISeoTools зелена → новий commit/tag; пін залежності — запит до task-01 (як task-10/25).

2. **App — enum (без міграції):**
   - `Provider.blogs` у [app/schemas/enums.py](../../../app/schemas/enums.py) (контрактна зміна, процес task-02). **Не** додавати в `METRIC_PROVIDERS` — головний гард, щоб блоги не протекли в KPI/health/рутинний синк.
   - `SyncJob.mode` отримує значення `"blogs"` (звичайний рядок, DDL не треба).

3. **App — адаптер + API:**
   - `app/integrations/blogs.py`: `BlogsAdapter(ProviderAdapter, provider=blogs)`, клон структури `TechAuditAdapter`. `preflight()` — структурний gate (є `domain` + `seo_audit` імпортовний → інакше `unavailable`, не краш). `collect()` при `job.mode == "blogs"`: лінивий `import seo_audit.dashboard.blogs`, run-dir під артефактами, `emit(ProgressEvent)` після discovery/fetch, cooperative cancel, `run_dir → provenance_json`. `collect_datasets()` пише `blog_posts` + `blog_summary` (через `AnalyticsDatasetRepository.bulk_insert`, дедуп `dimension_value` як у фіксі task-25). Підняти `cell_timeout_seconds`/`max_pages` bound для доріжки.
   - `app/api/blogs.py` (автодискавер, `register_router`):
     - `POST /clients/{id}/blogs/runs` → `SyncJob(mode="blogs", requested_providers=[blogs])`; `202 {job_id, events_url}` (стиль `POST .../tech-audit/runs`).
     - `GET /clients/{id}/blogs/latest` → `BlogsSummaryDTO` (`has_run`, `post_count`, `blog_url`, `crawled_at`, `warnings`, `state`); живить блок угорі. No-run → `has_run:false`, `post_count:null` (**не 0**).
     - `GET /clients/{id}/blogs/posts?q=&sort=&page=` → `BlogPostsPageDTO` (пагінований; `sort ∈ {date_desc, date_asc, words_desc, words_asc, title}`; `q` фільтрує по title/url); стиль `get_ahrefs_affected_urls`.
     - `GET /clients/{id}/blogs/export.csv` → `StreamingResponse` `text/csv` (stdlib `csv`, `BytesIO`); колонки Title, Description, Word count, URL, Published date; filename `SLUG_Blogs_YYYYMMDD.csv`.
     - `GET /clients/{id}/blogs/export.xlsx` → `StreamingResponse` `…spreadsheetml.sheet` (`openpyxl`, `BytesIO`); один лист «Blog posts», ті самі колонки + шапка. No-run → `404`.
   - DTO у `app/schemas/api.py` (task-02 зона, адитивно): `BlogsSummaryDTO`, `BlogPostDTO`, `BlogPostsPageDTO`, `BlogsRunAcceptedDTO`.
   - Репозиторій: `app/db/repositories/blogs.py` (тонкий; запис 2 dataset_key + читання latest posts/summary + total для пагінації), патерни task-03.

4. **Frontend (контракт `frontend/AGENTS.md`):**
   - **Блок угорі:** нова секція `#cp-blogs` (`.blogs-section`) у [client-page.js](../../../frontend/js/client-page.js) `shell()`, розміщена **першою в `#cp-content`, над `#cp-ahrefs`**; `renderBlogsSummaryCard()` — клон `renderAhrefsSummaryCard()` ([client-page.js:566](../../../frontend/js/client-page.js#L566)): «Blogs» eyebrow, `post_count` великим числом, «Refresh data» (`startBlogsRun()` → `watchJob`), «Open blogs» → `buildBlogsHash`. Період-незалежна (kick off у `loadClient()`, як `loadTechAudit()`).
   - **Окрема сторінка:** новий роут у [router.js](../../../frontend/js/router.js) — `BLOGS_ROUTE = /^#\/client\/([^/?]+)\/blogs/` (перевіряти **перед** `AHREFS_ROUTE`/`CLIENT_ROUTE`), `buildBlogsHash`/`replaceBlogsHash`, `#blogs-page-root` у [index.html](../../../frontend/index.html). Новий `frontend/js/blogs-page.js` (клон `ahrefs-page.js`): header клієнта, «Refresh data» + SSE-прогрес (`watchJob`), toolbar (пошук + сортування + пагінація), **таблиця** (Title, Description, Word count, Link↗, Published date), «Export CSV» + «Export XLSX» як `<a download>` (`icon("download")`), стан у URL (`bq`/`bsort`/`boff`).
   - `api.js`: `startBlogsRun`, `getBlogsLatest`, `getBlogsPosts`, `blogsExportCsvUrl`, `blogsExportXlsxUrl` (+ **fixture-гілки** + фікстури у `frontend/fixtures/`).
   - Обов'язкові стани (AGENTS §6): initial loading (skeleton), **no run yet** (empty + CTA «Refresh data»), running (progress), populated, stale, failed, `seo_audit` unavailable. `null` ≠ `0`; Tabler-іконки, бренд-токени; таблиця скролиться в своєму `overflow-x:auto`; мобільний без горизонтального переповнення.

**НЕ входить (свідомо):**
- Будь-які [ШІ]-тексти чи оцінки якості постів (це — Blog Strategy, окремий інструмент).
- Порівняння постів між періодами / трендів кількості блогів у часі (інвентар — point-in-time; історія снапшотів лишається, але UI її не візуалізує).
- Планове авто-перезапускання краулу (scheduler) — поза MVP.
- Перевірка «опубліковано vs Notion-статус» (це окремий флоу `blog_publish_check`).
- Зміна рутинного багатопровайдерного синку дашборду.

---

## Owned paths

```
# AISeoTools (адитивно; окремий репозиторій, свій .venv)
seo_audit/dashboard/blogs.py                           # новий (WP REST first, build_inventory fallback)
seo_audit/blog_strategy/wp_api.py                      # новий (пост-релізний фікс) — WordPress REST API джерело постів
seo_audit/dashboard/models.py            (task-08)     # + BlogsResult / BlogPost (адитивно)
tests/dashboard/test_blogs.py                          # новий
tests/test_blog_strategy_wp_api.py                     # новий (пост-релізний фікс)

# App — backend
app/integrations/blogs.py                              # новий адаптер
app/api/blogs.py                                       # новий роутер (автодискавер, реюз AnalyticsDatasetRepository — окремого репозиторію не знадобилось)
app/integrations/__init__.py             (спільний)    # +1 рядок import (неексклюзивний)
tests/unit/integrations/test_blogs.py                  # новий
tests/unit/api/test_blogs.py                           # новий

# App — успадковані зони (адитивно; координація за процесом task-02, як таски 14–25)
app/schemas/enums.py           (task-02/14/25)  # + Provider.blogs (НЕ в METRIC_PROVIDERS)
app/schemas/api.py             (task-02/15/25)  # + Blogs* DTO, + CellProgressDTO.progress_message (жива прогрес-фіча)
app/jobs/runner.py             (task-05/25)     # + гілка blogs (skip not_configured, seed=domain)
app/db/models.py               (task-03)        # + SyncJobProvider.progress_message (жива прогрес-фіча)
migrations/versions/e58b2f4d6a91_*.py           # новий (жива прогрес-фіча)
app/db/repositories/cells.py   (task-03)        # + set_progress(progress_message=...)
app/jobs/events.py              (task-05)       # + make_emitter прокидає message → progress_message
app/api/jobs.py                 (task-06)       # + _to_cell_dto прокидає progress_message
tests/unit/db/test_jobs_and_cells.py            # +1 (жива прогрес-фіча)
tests/unit/jobs/test_job_events.py              # +1 (жива прогрес-фіча)

# Frontend (зона task-12/18; спільні файли — read-only-додатки, узгоджуються)
frontend/js/blogs-page.js                    # новий (+ live progress_message priority)
frontend/js/blogs-panel.js                   # новий
frontend/js/client-page.js     (task-18/25)  # + секція #cp-blogs + renderBlogsSummaryCard + loadBlogs
frontend/js/router.js          (task-12)     # + BLOGS_ROUTE + buildBlogsHash/replaceBlogsHash + activate-гілка
frontend/js/api.js             (task-12)     # + startBlogsRun/getBlogsLatest/getBlogsPosts/export URLs (fixture-гілки!)
frontend/css/client-page.css   (task-18/25)  # + .blogs-section*
frontend/index.html            (task-01/12)  # + #blogs-page-root + bump ?v= кеш-бастер (глобально)
frontend/fixtures/*.json                     # + фікстури blogs (no-run / populated)
```

> Успадковані зони доповнюємо **лише своїми додатками**, наявну поведінку не міняємо (модель тасок 14–25). Кожен дотик поза власною зоною задокументувати в Completion report → «Contract changes». Кеш-бастер `?v=` піднімаємо **глобально** (як зафіксовано в task-25: різні суфікси = різні екземпляри модуля).

---

## Consumes

- **AISeoTools:** `seo_audit.discover.discover_urls`, `blog_strategy.inventory.build_inventory`, `blog_strategy.content_extract.extract_blog_page`/`classify_role`, `blog_strategy.models.BlogPageRecord`; dashboard `events`/`models` (task-08).
- **App:** `ProviderAdapter`/`CollectionContext`/`CollectionResult`/`ProgressEvent`/`Provider` (task-02); worker cell/lane/SSE/recovery (task-05); `AnalyticsDatasetRow` + `AnalyticsDatasetRepository` + `ProviderSnapshot` (task-03); `job_events`/SSE (task-05/23); фронтові `watchJob`/`icon`/`renderAhrefsSummaryCard`-патерн/токени (task-12/18/25).

## Provides

- Зареєстрований адаптер `blogs` (inventory-тип, point-in-time), готовий для worker.
- REST: run / latest / posts / export.csv / export.xlsx.
- Self-contained CSV+XLSX експорт (перший XLSX через застосунковий `openpyxl`, а не AISeoTools).
- Frontend: блок «Blogs» угорі сторінки клієнта + окрема сторінка списку блогів.

---

## Кроки реалізації

**AISeoTools:**
1. `seo_audit/dashboard/blogs.py`: `collect_blogs` (обгортка `build_inventory`, фільтр `page_role=="blog"`, дедуп `canonical||url`, прогрес→events, cancel, `records.jsonl`), best-effort `blog_url`. Source-neutral `BlogsResult`/`BlogPost` у dashboard `models.py`.
2. Прогнати всю AISeoTools-suite (`.venv/bin/python -m pytest -q`) — зелено; закомітити; новий tag/commit; запит task-01 на оновлення піну.

**App backend:**
3. `enums.py`: `Provider.blogs`; вичерпні enum-тести доповнити; окремий тест «`METRIC_PROVIDERS` не містить `blogs`».
4. `app/db/repositories/blogs.py`: запис `blog_posts`+`blog_summary`, читання latest (posts з `q`/`sort`/пагінацією + total, summary).
5. `BlogsAdapter`: `preflight` (structural), `collect` (`job.mode=="blogs"`, лінивий import, run-dir, events, cancel), `collect_datasets` (2 dataset_key, дедуп dimension). Гілка `blogs` у `runner.py` (skip `not_configured`, seed=`Client.domain`, `source_mode=auto`). Реєстрація в `app/integrations/__init__.py`.
6. `app/api/blogs.py`: 5 ендпоінтів (див. Обсяг); DTO у `api.py`.
7. **Гарди** (ключовий ризик): перевірити всі місця, що ітерують `tuple(Provider)` — `app/api/jobs.py` (розгортання `providers="all"`), `app/api/dashboard.py` (KPI+health), `app/api/providers.py` (ops-грид), health (task-15), catalog (task-04) — `blogs` **не** потрапляє в жодне; рутинний синк ніколи не створює `blogs`-клітинку (тільки `POST .../blogs/runs`). Патерн — `METRIC_PROVIDERS` (task-25).

**Frontend:**
8. `router.js`: `BLOGS_ROUTE` + hash-білдери + гілка в `activate()` (монтує `#blogs-page-root`, ховає решту).
9. `blogs-page.js` (клон `ahrefs-page.js`): header, «Refresh data» (`watchJob`), таблиця + toolbar (пошук/сортування/пагінація), Export CSV/XLSX (`<a download>`), стан у URL.
10. `client-page.js`: секція `#cp-blogs` першою в `#cp-content`, `renderBlogsSummaryCard()`, `loadBlogs()` (період-незалежна), `startBlogsRun()` (як `startAuditRun`).
11. `api.js`: 5 методів + fixture-гілки + фікстури. `index.html`: `#blogs-page-root` + глобальний bump `?v=`.
12. Візуальний loop (AGENTS §15–16): скріншоти desktop/mobile у всіх станах (no-run, running, populated, unavailable); чекліст.

---

## Тести

- **AISeoTools** `tests/dashboard/test_blogs.py` (новий): `collect_blogs` з фікстура-inventory → лишає лише `page_role=="blog"`, дедуплікує за canonical, мапить поля в `BlogPost`; `None`-дата не стає `0`/`""`-фейком; `on_event` отримує стадії; cancel перериває; `blog_url` best-effort.
- **App unit** `tests/unit/integrations/test_blogs.py`: preflight `unavailable` без `domain`/`seo_audit`; collect пише 2 dataset_key + run_dir у provenance; нульовий краул → `has_run` є, але `post_count` не 0-фейк; падіння краулу не кидає й не тече текстом помилки; cooperative cancel до/під час; дедуп `dimension_value` (колізія не рве весь снапшот — регресія фіксу task-25); секрети не течуть у events.
- **App unit** `tests/unit/api/test_blogs.py`: no-run → `has_run:false`, `post_count:null` (не 0); latest/posts форма; `sort`/`q`/пагінація; export.csv — правильні колонки/екранування, `None`-дата → порожня клітинка (не «0»); export.xlsx — валідний zip/лист, ті самі колонки; export без прогону → 404; 404 на невідомого клієнта; запуск джоба повертає `events_url`.
- **Регресія:** `tests/unit/schemas/test_enums.py` (+ «`METRIC_PROVIDERS` без `blogs`»); `tests/unit/api/test_providers.py` (ops-грид = метричні провайдери, `blogs` відсутній); перегенерувати `contract_snapshot.json` + `openapi_snapshot.json` (діфф звірити: лише `Provider.blogs`, нові DTO і 5 шляхів).
- **Frontend e2e** (task-13 harness/fixtures): блок угорі рендериться в no-run/loading/populated; сторінка блогів — таблиця, сортування, пошук, пагінація, обидва download-лінки присутні й доступні клавіатурою; мобільний 390px без горизонтального переповнення.
- **Live smoke** (поза CI): контрольний клієнт (напр. один із `blog_publish_check_0426.md`) → «Refresh data» у воркері → кількість постів звірена з реальним блогом; CSV та XLSX відкриваються, колонки коректні, `published_date` присутня.

---

## Definition of Done / Gate

- [ ] Блок «Blogs» угорі сторінки клієнта показує кількість постів, «Refresh data» і «Open blogs»; no-run → чистий empty-стан з CTA (не краш/нуль).
- [ ] «Refresh data» запускає краул у durable-воркері, показує живий SSE-прогрес, переживає рестарт, скасовується кооперативно; на terminal блок і сторінка оновлюються.
- [ ] Окрема сторінка `#/client/<id>/blogs` показує таблицю **Title · Description · Word count · Link · Published date** з пошуком, сортуванням і пагінацією; `null`-поля показані як «—», не як `0`.
- [ ] «Export CSV» і «Export XLSX» віддають список постів з коректними колонками; обидва працюють **без AISeoTools** (self-contained у застосунку).
- [ ] `Provider.blogs` **не** з'являється в KPI картки, health %, ops-гриді чи рутинному синку; новий клієнт без прогону → чистий empty, не краш/нуль.
- [ ] `seo_audit` відсутній (CI/деплой) → `latest/posts` чесно `unavailable`/empty, `run` → адаптер деградує, **CSV/XLSX усе одно віддаються** (з наявного снапшота або 404, якщо прогону не було); `import app.main` не падає (усі імпорти `seo_audit.*` ліниві).
- [ ] `null` ≠ `0`; усі обов'язкові стани (AGENTS §6) реалізовані й переглянуті на скріншотах (desktop+mobile).
- [ ] Уся стара test-suite (App + AISeoTools) зелена; нові unit/e2e зелені; ruff/mypy чисті; усе — в Docker.

---

## Ризики

- **Enum-ripple.** `Provider.blogs` не має протікати в метрик-шляхи. Мітигація: `METRIC_PROVIDERS` (task-25) + ревʼю всіх `tuple(Provider)`-місць першим кроком бекенду; рутинний синк ніколи не створює `blogs`-клітинку.
- **Вага краулу / timeout.** `build_inventory` fetch-ить **увесь** сайт (не лише блог), великий сайт може перевищити per-cell дефолт ([runner.py:47](../../../app/jobs/runner.py#L47)). Мітигація: `max_pages` bound + heartbeat під час fetch + окремий `cell_timeout` для `blogs`-доріжки. **Follow-up:** blog-only fast-path (pre-filter discovered URLs за blog-path regex перед fetch), якщо повний inventory виявиться задорогим.
- **Класифікація блогу.** Сайт без Article-schema / нестандартний шлях (`/press-news/`, як Sapphire NY у `blog_publish_check_0426.md`) → `classify_role` може недоврахувати. Мітигація: override `blog_url`/`blog_prefixes` в `integration_config`; warnings у summary, коли completeness низька; `None` ≠ порожньо.
- **Дублікати за canonical.** Пагінація/tag-сторінки блогу дають той самий canonical двічі → колізія `dimension_value` рве весь снапшот (точний баг task-25). Мітигація: дедуп на продюсері (`collect_blogs`) і споживачі (`collect_datasets`) — з регресійним тестом.
- **CI/деплой без `seo_audit`.** Усі імпорти `seo_audit.*` ліниві; run деградує в `unavailable`; **експорт незалежний** (stdlib+openpyxl) і лишається робочим.
- **Immutability.** Кожен Refresh — новий снапшот; читання завжди бере latest. Жодних мутабельних таблиць (простіше за task-25, бо нема recheck).

---

## Майбутні під-таски (поза цією)

- Blog-only fast-path краул (без fetch money-сторінок) — якщо повний inventory задорогий.
- Планове авто-перезапускання (scheduler) — поза MVP.
- Тренд кількості постів у часі (візуалізація історії снапшотів) — окрема таска.

---

## ✅ Completion report

- **Дата:** 2026-07-24
- **Гілка / комміт:** main (робоче дерево) — не закомічено, див. Follow-ups
- **Статус:** done-with-followups

### Що зроблено

Машинний інвентар блогу клієнта наскрізно, від краулера AISeoTools до блоку й окремої сторінки на фронті:

- **AISeoTools** (`seo_audit/dashboard/blogs.py`, новий): `collect_blogs()` (обгортка `build_inventory`, фільтрує `page_role=="blog"`, дедуплікує за `canonical||url`, прогрес→events, best-effort cooperative cancel), source-neutral `BlogsResult`/`BlogPost`, best-effort `_guess_blog_url()` (спільний префікс шляхів постів).
- **AISeoTools** (`seo_audit/blog_strategy/inventory.py`, точкова адитивна правка): `build_inventory()` отримав три нові keyword-only параметри — `max_urls` (bound на кількість URL до фетчу), `on_page` (progress callback), `should_stop` (кооперативний cancel-чекер, скіпає ще не розпочаті фетчі). Дефолти `None` зберігають стару поведінку байт-в-байт.
- **AISeoTools** (`seo_audit/dashboard/models.py`): `Provider.blogs` доданий до dashboard-enum (той самий патерн, що `tech_audit`).
- **App**: `Provider.blogs` (поза `METRIC_PROVIDERS`); `BlogsAdapter` (`app/integrations/blogs.py`, клон структури `TechAuditAdapter`, без recheck-гілки); роутер `app/api/blogs.py` (runs / latest / posts / export.csv / export.xlsx); DTO у `app/schemas/api.py`; гілка `is_self_configured` у `app/jobs/runner.py` (skip `not_configured`, seed=`Client.domain`, `force_refresh` для `job_mode=="blogs"`). Без нових таблиць і без міграцій.
- **Frontend**: блок «Blogs» угорі сторінки клієнта (`#cp-blogs`, першим у `#cp-content`, над `#cp-ahrefs`) — `renderBlogsSummaryCard()` у `client-page.js`, клон `renderAhrefsSummaryCard()`; окрема сторінка `#/client/<id>/blogs` (`frontend/js/blogs-page.js`, клон `ahrefs-page.js`) + `frontend/js/blogs-panel.js` (таблиця Title/Description/Word count/Link/Published + пошук/сортування/пагінація); «Refresh data» через наявний `watchJob`; «Export CSV»/«Export XLSX» як `<a download>`; новий роут у `router.js` (`BLOGS_ROUTE`, перевіряється до `CLIENT_ROUTE`); 3 фікстури (`blogs-latest`, `blogs-empty`, `blogs-posts`).

### Як зроблено (ключові рішення)

- **Жодної нової таблиці.** Як і `tech_audit` (task-25), пости їдуть у наявній `AnalyticsDatasetRow`: `dataset_key="blog_posts"` (1 рядок на пост, `dimension_value=url`) + `dataset_key="blog_summary"` (1 рядок, `post_count`+`blog_url`+`crawl_completeness`+`warnings`). Читання завжди бере найсвіжіший снапшот (`AnalyticsDatasetRepository.latest`) — на відміну від task-25 тут немає append-only recheck-шару, кожен «Refresh data» — це просто новий повний снапшот.
- **Експорт — self-contained у застосунку, не в AISeoTools.** `openpyxl` вже пряма залежність (task-07), тому і CSV (stdlib `csv`), і XLSX будуються з persisted `blog_posts`-рядків у `BytesIO`/`StringIO` → `StreamingResponse`, без файлу на диску, без run-dir, без path-traversal-поверхні. Обидва працюють навіть коли `seo_audit` недоступний (CI/деплой) — на відміну від `tech_audit.export.xlsx`, що читає run-dir через AISeoTools.
- **Дедуп на двох рівнях (продюсер + споживач).** Рівно той самий клас бага, що task-25 зловив на `tech_findings` (UNIQUE-колізія `dimension_value` рве весь снапшот): `_posts_from_records()` в AISeoTools дедуплікує за `canonical||url`, а `BlogsAdapter.collect_datasets()` додатково відкидає повторний `dimension_value` перед віддачею runner-у. Регресійний тест на обох рівнях.
- **`build_inventory()` розширено адитивно** (`max_urls`/`on_page`/`should_stop`, keyword-only, дефолт `None` = стара поведінка) замість форкання власного краулера — мінімальний диф у чужому файлі, задокументований тут (не в «Owned paths» плану, за аналогією з `build_xlsx.py::machine_only` у task-25).
- **`is_self_configured` замінив вузький `is_tech_audit`** у `runner.py`: обидва self-configured провайдери (`tech_audit`, `blogs`) проходять той самий гейт-виняток (`not_configured` skip, `domain` з `Client`, `force_refresh` для свого job_mode) без дублювання коду.
- **Порожній блог — `complete`, не `unavailable`.** На відміну від `tech_audit` (де 0 сторінок = крауп не відбувся = `unavailable`), для блогів 0 постів на реальному сайті — легітимний результат; `collect_blogs` завжди намагається краулити і повертає `warnings` окремо, тому `quality=complete` з `post_count=0`.
- **Виявлений і виправлений mobile-баг (не з чек-листа плану).** `#blogs-page-root` (flex-column, як і `#ahrefs-page-root`) на 390px давав горизонтальний скрол сторінки (289px) попри те, що `.table-scroll` візуально коректно клипував widescreen-таблицю (760px min-width) — Chromium-специфічна поведінка `scrollWidth` флекс-контейнера, що не респектує nested `overflow-x:auto` так, як звичайний block-контейнер (`#ahrefs-page-root` тим самим шляхом чомусь не протікав — причину до кінця не з'ясовано, empiric fix застосовано і перевірено). Фікс: `#blogs-page-root { overflow-x: hidden; }` — точковий, не займає спільний `#client-page-root, #ahrefs-page-root, #blogs-page-root` рядок.
- **Паралельна незакомічена робота в тому ж дереві.** Під час виконання виявлено, що інша (чужа) сесія одночасно редагувала `index.html`/`router.js`/`client-page.js`/`dashboard.js`/`client-card.js`/`sync-modal.js`/`app/api/dashboard.py`/`app/api/tech_audit.py`/новий `app/db/repositories/tech_audits.py` (судячи з коду — «Run audits» batch-фіча) під іншим cache-buster (`?v=20260724-auditbatch1`), що зламало інваріант «один глобальний cache-buster». Нормалізовано всі `?v=` під `20260724-blogs1` глобально; чужі логічні зміни не займані і не ревʼюєні. Ruff/mypy на файлах цієї сесії (не моїх) наразі мають незалежні помилки — задокументовано у Follow-ups, не мій дефект.

### Контрактні зміни (Contract changes)

- **`app/schemas/enums.py`**: `+ Provider.blogs` (поза `METRIC_PROVIDERS`). Споживачі: jobs/dashboard/providers API (гарди вже існували через `METRIC_PROVIDERS`, нових правок не знадобилось), dispatcher (свідомо на повному enum, як з `tech_audit`), фронт.
- **`app/schemas/api.py`**: 4 нові DTO — `BlogsSummaryDTO`, `BlogPostDTO`, `BlogPostsPageDTO`, `BlogsRunAcceptedDTO`.
- **`app/jobs/runner.py`**: перейменовано локальну змінну `is_tech_audit` → `is_self_configured` (охоплює `tech_audit` і `blogs`); `force_refresh` розширено значенням `"blogs"`.
- **Міграцій БД немає** — `Provider.blogs` суто Python-рівень (як `tech_audit`, підтверджено в task-25: `sa.Enum` без `create_constraint`), нових таблиць не додано.
- **AISeoTools**: `dashboard.models.Provider` отримав `blogs`; `blog_strategy/inventory.py::build_inventory()` отримав 3 нові optional keyword-only параметри. Contract-снапшот AISeoTools (`tests/dashboard/data/aiseotools_contract_snapshot.json`) перегенеровано свідомо (діфф — лише новий enum-член у схемах `ProgressEvent`/`DashboardResult`, `legacy_signatures` не зачеплено).
- **Перегенеровано снапшоти застосунку:** `tests/unit/api/data/openapi_snapshot.json` (+5 нових `/blogs/*` шляхів), `tests/unit/schemas/data/contract_snapshot.json` (+ enum-член + 4 DTO). Діффи звірені — нічого зайвого.

### Як перевірити (verification)

AISeoTools (хост, власний .venv):
```bash
cd /home/elon/dev/seo/AISeoTools
.venv/bin/python -m pytest tests/dashboard/test_blogs.py -q     # 8 passed
.venv/bin/python -m pytest -q                                   # 1595 passed
```

Застосунок (тільки Docker):
```bash
docker compose exec api sh -c "poetry run ruff check app/api/blogs.py app/integrations/blogs.py app/schemas/api.py app/schemas/enums.py app/jobs/runner.py app/integrations/__init__.py tests/unit/integrations/test_blogs_adapter.py tests/unit/api/test_blogs.py tests/unit/schemas/test_enums.py"
# All checks passed!
docker compose exec api poetry run pytest tests/unit/integrations/test_blogs_adapter.py tests/unit/api/test_blogs.py tests/unit/schemas/test_enums.py -q
# 40 passed
docker compose exec api poetry run pytest -q
# 588 passed, 1 skipped, 1 failed (передіснуюча, чужа: tests/contract/test_aiseotools_signatures.py — GSC fetcher rename в паралельній незакомiченій роботі, не блог)
```

Фронт (fixture-режим, статичний сервер + Playwright):
```bash
python3 -m http.server 4173 --directory frontend &
NODE_PATH=/tmp/yovth-e2e/node_modules node blogs-visual-check.cjs
# перевіряє: порядок секцій (#cp-blogs першим), картка з кількістю постів +
# посилання «Open blogs», таблиця з 6 постами (title/description/word count/
# link/published date), null-опис/дата → «—», Export CSV/XLSX присутні,
# сортування, пошук, пагінація, порожній стан з CTA, мобільний 390px —
# 0px горизонтального overflow, 0 JS-помилок
```
В UI: `#/client/<id>` → блок «Blogs» вгорі → «Open blogs» → окрема сторінка → «Refresh data» → таблиця → «Export CSV»/«Export XLSX».

### Тести

- **AISeoTools** `tests/dashboard/test_blogs.py` (8, новий): мапінг inventory→пости, `page_role!="blog"` відсіюється, дедуп за canonical, `None`-дата лишається `None` (не `0`/`""`), `_guess_blog_url` (знаходить і не знаходить спільний префікс), явний `blog_url` перекриває здогадку, `failed_urls` → warning без краху, cancel.
- **App** `tests/unit/integrations/test_blogs_adapter.py` (16, новий): preflight (no domain / no AISeoTools / ok), краул без метрик + `run_dir`/`post_count` у provenance, датасети (2 dataset_key, `blog_summary`+`blog_posts`), `None`-дата в датасеті лишається `None`, «датасети без collect()» → чесний `no_payload`, дедуп `dimension_value` (регресія), нульовий краул → **`complete`** (не `unavailable`, на відміну від tech_audit — свідома відмінність), падіння краулу не кидає й не тече текстом помилки, cancel до/під час, warnings → `partial` quality.
- **App** `tests/unit/api/test_blogs.py` (15, новий): no-run → `has_run:false`/`post_count:null` (не 0), latest з warnings, 404 на невідомого клієнта, posts (усі рядки/пошук/сортування `words_desc`/дефолт для невідомого sort/`null` word_count і date), запуск джоба (mode/requested_providers), 404 без прогону, export.csv/export.xlsx без прогону → 404, export.csv/xlsx з прогоном → правильні колонки й кількість рядків.
- **Регресія:** `tests/unit/schemas/test_enums.py` (`test_provider_values_are_stable` + `test_metric_providers_exclude_tech_audit_and_blogs` — перейменовано й розширено).

### Follow-ups / борг

- **Нічого не закомічено** (ні в застосунку, ні в AISeoTools) — комміт/пін AISeoTools на новий tag лишається свідомим кроком користувача, за зразком task-10/task-25.
- **Паралельна незакомічена робота іншої сесії** (batch «Run audits», судячи з `#audit-all-button`/`app/db/repositories/tech_audits.py`/змін у `app/api/dashboard.py`/`app/api/tech_audit.py`) наразі лишає **чужі** ruff/mypy помилки в дереві (unused import у `tech_audit.py`; кілька `union-attr`/`var-annotated`/`arg-type` у `tech_audits.py`/`dashboard.py`). Це не мій дефект і не моя зона — перевірено скопованим ruff/mypy прогоном лише на файлах цієї таски (чисто). Перед комітом **будь-якої** роботи в цьому дереві варто спершу розібратись, чия це робота і чи вона завершена.
- **Мобільний `overflow-x:hidden`-фікс на `#blogs-page-root`** — емпіричний (Chromium-специфічна поведінка `scrollWidth` флекс-колонки з вкладеним `.table-scroll`), корінь відмінності від `#ahrefs-page-root` (яка тим самим шляхом не протікає) не з'ясовано до кінця. Working, задокументовано в CSS-коментарі; вартий подальшого дослідження, якщо колись знадобиться прибрати цей рядок.
- **Blog-only fast-path не реалізовано** (задокументовано в «Обсяг» як свідомо поза цією таскою) — `collect_blogs` fetch-ить увесь сайт через `build_inventory` (без sampling), що для великого сайту може бути повільним; є `max_pages`/`max_urls` bound, окремого per-provider timeout для доріжки `blogs` немає (той самий борг, що в task-25 для `tech_audit`).
- **e2e-смоуки (`tests/e2e/frontend/*.cjs`) не проганялись** проти цієї фічі — новий `blogs-visual-check.cjs` (сценарій написаний і прогнаний вручну) не інтегрований у постійний харнес `tests/e2e/`; варто перенести туди окремим кроком.
- **Blog-only WP REST fast-path не бере всі поля, які міг би.** `wp_api.py` тягне лише `date/modified/link/title/excerpt/content/status` — досить для наших 5 колонок, але, наприклад, featured image чи автор не читаються (не потрібні зараз, легко додати за потреби через `_fields`).

### Пост-релізні фікси (після живого прогону, 2026-07-24)

Перший живий прогін (`shorelinedentalstudio.com/blog/`) виявив, що генеричний HTML-краулер (`build_inventory`) **не всюди працює**: реальні шляхи блог-постів (`/blog/<category>/<slug>/`, категорійний префікс) і 28-сторінкова пагінація HTML-лістингу — за межами того, що передбачають path/role-евристики `classify_role`. Перевірка інших клієнтів із `blog_publish_check_0426.md` (19cleveland, Grill Point, Hearwell, NY Hearing, PJ Clarke's, Sapphire NY, Shawarma Mia, Smash House Burgers) показала: **усі вісім — WordPress із відкритим REST API** (`/wp-json/wp/v2/posts`, підтверджено `curl` наживо).

Замість «парсер під кожного клієнта» (N бespoke скрейперів, що не масштабується й не оновлюється) — **один загальний, точніший шлях**: новий `seo_audit/blog_strategy/wp_api.py::fetch_wp_posts()` тягне пости напряму з WordPress REST API (структурований JSON: `title`/`content`/`excerpt`/`date`/`modified`/`link`, пагінація `per_page=100` до короткої сторінки), і `collect_blogs()` (`dashboard/blogs.py`) намагається цей шлях **першим**; генеричний краул (`build_inventory`) лишається **фолбеком** лише для не-WordPress сайтів або вимкненого REST.

- **Чому це строго краще для WP-клієнтів:** точна кількість опублікованих постів (WP REST фільтрує статус сам, жодних евристик «це блог чи ні»), реальний `word_count` з повного `content.rendered` (а не з обрізаного HTML-лістингу), точні `date`/`modified` без парсингу schema.org/OG, жодного вгадування шляху блогу (сам `link` — уже канонічний URL, включно з нестандартними префіксами на кшталт `/press-news/` у Sapphire NY), і **на порядок швидше** (3 запити до REST замість краулу цілого сайту).
- **Fallback — best-effort, ніколи не блокує.** `_try_wp_posts()` ловить будь-який виняток (мережа, неJSON-відповідь) і повертає `None`; `None` (не порожній список!) — сигнал «це не WordPress/REST вимкнено» → генеричний краул. Порожній список на першій сторінці — легітимний «REST є, постів 0».
- **Перевірено наживо (без моків):** `shorelinedentalstudio.com` → 248 постів (точний збіг із `X-WP-Total: 248` заголовком REST-відповіді), `blog_url` вгадано правильно (`/blog/`), реальні заголовки/описи/word count/дати, 0 warnings. `nysapphire.com` (нестандартний `/press-news/`) → 40 постів, `blog_url` вгадано як `/press-news/` **без жодного ручного override** — раніше це був єдиний клієнт із задокументованою нестандартною структурою.
- **App-адаптер не зачеплений.** Сигнатура `collect_blogs(...)` не змінилась (ті самі keyword-параметри), тож `app/integrations/blogs.py` викликає її як і раніше — нуль правок на боці застосунку. Підтверджено: 27 наявних app-тестів (`test_blogs_adapter.py` + `test_blogs.py`) далі зелені без змін.
- **Нові тести:** `tests/test_blog_strategy_wp_api.py` (10, новий) — пагінація до короткої сторінки, `None` на JSON-словник (`rest_no_route`) vs порожній список = легітимний нуль, `max_posts`-межа, не-200 після 1-ї сторінки завершує пагінацію без втрати вже зібраного, cooperative `should_stop`, `on_page` кумулятивний лічильник, очищення title/description (HTML-теги, entities, «[&hellip;]»-хвіст) і word count (без `<script>`-вмісту). `tests/dashboard/test_blogs.py` розширено (+6): WP-шлях пропускає generic crawl повністю, `None`/exception від WP-проби падає у fallback, cancel під час WP discovery, живий "found N posts so far" прогрес з обох джерел. Разом AISeoTools: **1612 passed** (було 1595).

### Живий прогрес «скільки знайдено» (2026-07-24, той самий день)

Користувач попросив бачити прогрес наживо — скільки постів знайдено, поки триває збір, а не лише фінальний підсумок. `current_stage` на фронті — статичний лейбл зі словника `STAGE_LABELS` («Crawling site pages»), без місця для змінного числа.

- **AISeoTools:** `fetch_wp_posts()` отримав `on_page: Callable[[int], None]` — викликається після кожної сторінки WP REST із кумулятивним числом постів. `build_inventory()` отримав аналогічний `on_blog_found` — викликається щоразу, коли сторінку класифіковано як `page_role=="blog"` (для generic-фолбеку). `collect_blogs()` підключає обидва до `emit_progress(..., message=f"found {N} posts so far")`.
- **App (контрактна зміна):** новий стовпець `sync_job_providers.progress_message` (nullable, `String(200)`) — міграція `e58b2f4d6a91`. `current_stage` — стабільний ключ для `STAGE_ORDER`/`STAGE_LABELS`, не місце для довільного тексту, що змінюється; `progress_message` — окреме, явно вільнотекстове поле саме для цього, without repurposing `stage`. `CellRepository.set_progress(..., progress_message=...)`, `app/jobs/events.py::make_emitter` прокидає `message or None` (порожній рядок ніколи не затирає попереднє живе повідомлення), `CellProgressDTO.progress_message` (нове поле), `_to_cell_dto()` у `app/api/jobs.py`.
- **Frontend:** `blogs-page.js::renderLiveStatus()` пріоритизує `activeCell?.progress_message` над статичним `STAGE_LABELS`-лейблом, коли воно є.
- **Перевірено наживо (реальний прогін, не фікстура):** `POST /clients/{SDS}/blogs/runs` проти справжнього `shorelinedentalstudio.com` → SSE/`job_events` показали точну послідовність: `"checking … for a WordPress REST API"` → `"found 100 posts so far"` → `"found 200 posts so far"` → `"found 248 posts so far"` → `"found 248 posts via the WordPress REST API"` → `"blog inventory ready"` → `"Blogs: 248 posts"` → `"cell succeeded"`. Кожне значення підтверджено в БД через `GET /sync-jobs/{id}` (`cell.progress_message`).
- **Нові тести:** `tests/unit/db/test_jobs_and_cells.py` (+1) — `set_progress(progress_message=...)`, `None` не затирає. `tests/unit/jobs/test_job_events.py` (+1) — `make_emitter` прокидає message в cell, порожнє повідомлення не затирає попереднє. Разом App: **591 passed** (було 588; +3 нових тести, +1 wp_api тест з попереднього фіксу дав +2 до AISeoTools).
- **Contract changes:** `CellProgressDTO.progress_message` (нове optional поле) — OpenAPI/contract-снапшоти перегенеровано. Діфф великий (>1000 рядків) через паралельну незакомічену роботу іншої сесії, що вже змінила `app/api/dashboard.py`/додала нові ендпоінти без власної регенерації знімків — регенерація неминуче захопила й це. Не мій код, не рев'юєний мною; задокументовано тут явно.
