# task-25 — Technical SEO Audit (machine-only, per-client)

- **Пакет:** машинний технічний аудит сайту клієнта на базі TechAudit-краулера AISeoTools — **без ШІ-агентів**: категорії → знахідки на сторінці клієнта, повторний запуск, перепровірка окремої знахідки, експорт у XLSX (наш шаблон, лише машинні [П]-дані).
- **Хвиля:** G (розширення після MVP; усі попередні таски закриті)
- **Depends on:** — (розширює закриті зони task-02/03/04/05/06/12/18 адитивно; координація контрактних змін — за процесом task-02, як робили таски 14–24)
- **PLAN.md:** §5 (worker/short-tx), §6 (модель даних, `artifacts`, immutability), §8 (безпечне розширення AISeoTools), §9 (job flow/SSE/cancel/recovery), §12 (quality rules), §13 (path-traversal guard на download); `docs/TechAudit.md` + `seo_audit/tech_audit/**` (джерело краулера)
- **frontend/AGENTS.md:** обов'язковий для всієї роботи у `frontend/`.

Зведений таск-док (рішення користувача 2026-07-23: «один зведений task-doc», а не хвиля з 5 файлів). За потреби пізніше розбивається на під-таски по шарах — межі вже проведені в «Owned paths».

---

## Мета

Дати оператору на сторінці клієнта **технічний SEO-аудит, згенерований детерміновано** (краул + машинні детектори), **без жодного ШІ**:

- список **категорій** проблем (families) з severity-rollup і лічильником;
- у кожній категорії — список **знахідок**: 1 URL + 1 проблема + виміряні дані + машинний `how_to_fix`;
- кнопка **«Run audit»** — повний повторний краул (durable job, живий SSE-прогрес);
- на кожній знахідці — **«Re-check»** (перепровірити окремо, без повного краулу);
- кнопка **«Export to XLSX»** — наш багатовкладковий шаблон TechAudit, **лише машинні [П]-колонки** (жодних [ШІ]-текстів).

Ключова інваріанта: **краул — це і є верифікація**. Кожен рядок звіту підтверджений живим DOM на момент краулу; жодних вигаданих чи неперевірних тверджень.

---

## Архітектура (рішення зафіксовані)

**Максимальний реюз наявного рушія.** Технічний аудит — це новий «провайдер» `Provider.tech_audit`, який **не бере участі в рутинному синку** дашборду (запускається лише окремою кнопкою/ендпоінтом). Завдяки цьому безкоштовно отримуємо: `SyncJob` + `SyncJobProvider` (одна клітинка), `job_events` + SSE-ендпоінт, cancel/retry/recovery, lease/heartbeat, immutable `ProviderSnapshot`, і зберігання знахідок у наявній `AnalyticsDatasetRow` (**без нових таблиць під самі знахідки**) — точно як шлях Ahrefs Site Audit ([app/api/analytics.py:85](../../../app/api/analytics.py#L85), [app/db/models.py:380](../../../app/db/models.py#L380)).

**Фаза 1 (ця таска) — без рендеру** (рішення користувача). Краул+детект працюють на `httpx` без браузера (`run_import(..., render=False)`). Отримуємо всі категорії, **крім** `SLOW_PAGES`/Core Web Vitals (їм потрібен Playwright). Скріншоти/VISUAL_UX не робимо взагалі — вони ШІ-only. Рендер+CWV — окрема майбутня таска (task-29: `playwright install chromium` в образ; OS-бібліотеки + `xvfb` + `shm_size:1gb` + `network_mode:host` уже є).

**Де крутиться краул:** у durable-воркері (переживає рестарт, PLAN §5.1), `seo_audit` імпортується **ліниво всередині `collect()`** (на CI/деплої mount відсутній — інакше `import app.main` падає; патерн [app/integrations/ahrefs.py:105](../../../app/integrations/ahrefs.py#L105)). Run-директорія (`records.jsonl`, `tech_findings_machine.json`, `*.xlsx`) живе на персистентному артефакт-волюмі `/app/app_data/artifacts/tech_audit/<snapshot_id>/`; шлях зберігається в `ProviderSnapshot.provenance_json` — його читають recheck/redetect/export.

### Модель даних (реюз `AnalyticsDatasetRow`)

Один краул = один `SyncJob(mode="tech_audit")` → одна клітинка `(client, tech_audit)` → один `ProviderSnapshot` → рядки `AnalyticsDatasetRow` двох `dataset_key`:

- `dataset_key="tech_categories"` — 1 рядок на категорію (family). `dimension_key="category"`, `dimension_value=<FAMILY>` (напр. `HTTP_ERRORS`). `metrics_json = {name, severity(worst), priority, url_count, checked, present_count, description, how_to_fix, auto_verifiable}`. `rank` = severity-порядок для сортування.
- `dataset_key="tech_findings"` — 1 рядок на знахідку `(slug, url)`. `dimension_key="finding"`, `dimension_value=f"{slug}|{url}"`. `metrics_json = {slug, category, name, severity, url, current_value, measured{...}, how_to_fix, method, live_status, final_url, link_targets?}`.
- `period_start = period_end = crawl_date` (аудит — point-in-time, не має періоду; той самий прийом, що Ahrefs snapshot). `provider=tech_audit`, `quality` з машинних warnings.

**Immutability:** `AnalyticsDatasetRow`/`ProviderSnapshot` незмінні після commit. Тому статус перепровірки **не пишемо в них** — одна нова **мутабельна** таблиця (патерн `ClientHealthDaily`, [models.py:424](../../../app/db/models.py#L424)):

- `tech_recheck_results` — черга + результат перепровірки. Поля: `id`, `client_id(FK)`, `snapshot_id(FK)` (який прогін перепровіряємо), `slug`, `url`, `status ∈ {queued, running, fixed, still_present, unreachable, skipped, error}`, `evidence_json`, `requested_at`, `checked_at`. Unique `(snapshot_id, slug, url)` (upsert). Ця таблиця **водночас є чергою робіт** для recheck-джоба (див. нижче) і джерелом статусу для UI.

---

## Обсяг

**Входить:**

1. **AISeoTools (адитивно, PLAN §8):** новий `seo_audit/dashboard/tech_audit.py`:
   - `collect_tech_audit(url, *, client, out_dir, render=False, max_pages, max_tier, on_event, is_cancelled) -> TechAuditResult` — обгортка над `run_import()` ([pipeline.py:142](../../../../seo/AISeoTools/seo_audit/tech_audit/pipeline.py#L142)); мапить прогрес краулу/детекту в `ProgressEvent` (dashboard `events.py`, task-08), кооперативний cancel, повертає source-neutral `TechAuditResult` (categories[], findings[], totals, health_score, cms, warnings, run_dir).
   - `recheck_tech_audit(run_dir, *, targets: list[(slug,url)] | None, on_event) -> RecheckResult` — обгортка над `run_recheck()` ([recheck.py:80](../../../../seo/AISeoTools/seo_audit/tech_audit/recheck.py#L80)); за наявності `targets` перепровіряє лише їх (фільтр по `rows`), інакше — весь позначений набір. Для `_SKIP_FAMILIES` (ORPHAN/SITEMAP/VISUAL_UX) повертає `skipped`.
   - Machine-only XLSX: додати **адитивний** `machine_only: bool = False` (keyword-only, дефолт зберігає стару поведінку байт-в-байт) у `seo_audit/tech_audit/report/build_xlsx.py::build_tech_xlsx()` — при `True` фільтрує `col.ai`-колонки (`cols = [c for c in F.columns_for(fam) if not c.ai]` у `_write_issue_sheet`, [build_xlsx.py:359](../../../../seo/AISeoTools/seo_audit/tech_audit/report/build_xlsx.py#L359)), пропускає VISUAL_UX-вкладку (порожні `visual_findings`) і QA-гейт (`strict=False`). `prepare_report(TechVerdict(), machine)` уже дає всі [П]-вкладки без агента.
   - Уся стара test-suite AISeoTools зелена → новий commit/tag; пін залежності оновити (запит до task-01: git-pin замість `path`-dependency).

2. **App — enum + міграція:**
   - `Provider.tech_audit` у [app/schemas/enums.py](../../../app/schemas/enums.py) (контрактна зміна, процес task-02).
   - Розширити значення `SyncJob.mode`: `+ "tech_audit"`, `+ "tech_audit_recheck"` (міграція, якщо `mode` під CHECK-constraint).
   - Нова таблиця `tech_recheck_results` (+ Alembic-ревізія, chain до поточного head).
   - **Гарди** (щоб `tech_audit` не протікав як метрик-провайдер): каталог метрик (task-04) — жодного `MetricSpec` для `tech_audit`; health (task-15) — `tech_audit` не в наборі внесків; dashboard-картка (task-06/15/19) — перелік провайдерів картки лишається явним списком 4 метрик-провайдерів, не `tuple(Provider)`; `POST /sync-jobs` default `requested_providers` **не** містить `tech_audit`; dispatcher default-lane-set (task-05) — `tech_audit`-доріжка обробляє лише tech-audit-клітинки (рутинні джоби їх не створюють).

3. **App — адаптер + API:**
   - `app/integrations/tech_audit.py`: `TechAuditAdapter(ProviderAdapter, provider=tech_audit)`. `preflight()` — структурний gate (є `domain`, `seo_audit` імпортовний → інакше `unavailable`, не краш). `collect()` розгалужується на `job.mode`:
     - `tech_audit` → повний краул: `collect_tech_audit(...)` у run-dir під артефактами; `collect_datasets()` пише `tech_categories` + `tech_findings`; `run_dir` → `provenance_json`; більший `cell_timeout_seconds` + `max_pages` bound.
     - `tech_audit_recheck` → читає `queued`-рядки `tech_recheck_results` для клієнта, знаходить `run_dir` з останнього снапшота, `recheck_tech_audit(run_dir, targets=...)`, апсертить статуси.
   - `app/api/tech_audit.py` (автодискавер, `register_router`):
     - `POST /clients/{id}/tech-audit/runs` → створює `SyncJob(mode="tech_audit", requested_providers=[tech_audit])`; `202 {job_id, events_url}`.
     - `GET /clients/{id}/tech-audit/latest` → `TechAuditSummaryDTO` (категорії + rollup + observed_at + health + totals + warnings + run_id), стиль `get_ahrefs_site_audit`.
     - `GET /clients/{id}/tech-audit/findings?category=&q=&page=` → `TechAuditFindingsPageDTO` (пагінований, стиль `get_ahrefs_affected_urls`); підмішує `recheck_status` з `tech_recheck_results`.
     - `POST /clients/{id}/tech-audit/findings/recheck` (body: `{finding_ids | (slug,url) pairs}`) → апсертить `queued`-рядки + створює `SyncJob(mode="tech_audit_recheck")`; для graph-категорій (ORPHAN/SITEMAP) → `409 needs_full_rerun`.
     - `GET /clients/{id}/tech-audit/export.xlsx` → генерує machine-only XLSX з `run_dir` останнього снапшота, реєструє `Artifact`, віддає `FileResponse` (path-traversal guard, як [jobs.py:370](../../../app/api/jobs.py#L370)); якщо `seo_audit` недоступний → `503 unavailable`.
   - DTO у `app/schemas/api.py` (task-02 зона, адитивно): `TechAuditSummaryDTO`, `TechAuditCategoryDTO`, `TechAuditFindingDTO`, `TechAuditFindingsPageDTO`, `TechAuditRunAcceptedDTO`.

4. **Frontend (контракт `frontend/AGENTS.md`):**
   - Нова секція `#cp-audit` (`.audit-section`) у `client-page.js::shell()` + виклик рендеру в `loadPeriodData()`; новий модуль `frontend/js/audit-panel.js` — клон `ahrefs-panel.js`: список категорій (severity-rollup + count), розгортання в `<details>`-знахідки (URL + виміряні дані + `how_to_fix` + `recheck_status`), toolbar пошук/severity/пагінація; стан секції (обрана категорія, пошук, offset) у URL через `replaceClientHash`.
   - **«Run audit»** — новий `startTechAudit()` (api.js) б'є `POST .../tech-audit/runs`, далі `watchJob({id, eventsUrl})` (без змін у `watchJob` — tech-audit-джоб це звичайний `SyncJob`, `getSyncJob` працює); прогрес у `.live-collection`/`<progress>`; на terminal — перезавантаження секції.
   - **«Re-check»** на рядку знахідки — `recheckTechAuditFindings()` (api.js) → повертає `{job_id, events_url}` → `watchJob` → на terminal перечитати знахідки; кнопка показує busy-стан; для graph-категорій **задизейблена** з `title`-поясненням («Requires a full re-run»).
   - **«Export to XLSX»** — `<a class="button button--quiet" href="/api/v1/clients/${id}/tech-audit/export.xlsx" download>` + `icon("download")` (перший download у фронті; не через JSON-`request()`-обгортку).
   - Обов'язкові стани (AGENTS §6): initial loading (skeleton), **no run yet** (empty + CTA «Run audit»), running, partial, stale, failed, cancelled, `seo_audit` unavailable. `null` ≠ `0`; severity — колір + текст + іконка; Tabler-іконки, бренд-токени.

**НЕ входить (свідомо):**
- Рендер / Core Web Vitals / `SLOW_PAGES` / скріншоти / mobile-UX — окрема майбутня таска (task-29, вимагає Chromium в образі).
- Будь-які [ШІ]-тексти: `new_title/new_desc/suggested_alt/recommended_links`, VISUAL_UX — не генеруємо взагалі.
- Планове/розкладне авто-перезапускання аудиту (поза MVP).
- Зміна рутинного багатопровайдерного синку дашборду.

---

## Owned paths

```
# AISeoTools (адитивно; окремий репозиторій, свій .venv)
seo_audit/dashboard/tech_audit.py                      # новий
seo_audit/tech_audit/report/build_xlsx.py              # адитивний kwarg machine_only= (точкова правка)
tests/dashboard/test_tech_audit.py                     # новий

# App — backend
app/integrations/tech_audit.py                         # новий адаптер
app/api/tech_audit.py                                  # новий роутер (автодискавер)
app/integrations/__init__.py                           # +1 рядок import (спільний bootstrap, неексклюзивний)
tests/unit/integrations/test_tech_audit.py             # новий
tests/unit/api/test_tech_audit.py                      # новий

# App — успадковані зони (адитивно; координація за процесом task-02, як таски 14–24)
app/schemas/enums.py           (task-02/14)  # + Provider.tech_audit
app/schemas/api.py             (task-02/15)  # + TechAudit* DTO
app/db/models.py               (task-03)     # + TechRecheckResult
migrations/versions/<new>.py   (task-03)     # + tech_recheck_results, SyncJob.mode values
app/db/repositories/tech_audit.py            # новий репозиторій (patterns task-03)

# Frontend (зона task-12/18; спільні файли — read-only-додатки, узгоджуються)
frontend/js/audit-panel.js                   # новий
frontend/js/client-page.js     (task-18)     # + секція #cp-audit + рендер
frontend/js/api.js             (task-12)     # + startTechAudit/getTechAudit*/recheck (fixture-гілки!)
frontend/css/client-page.css   (task-18)     # + .audit-section*
frontend/index.html            (task-01/12)  # bump ?v= кеш-бастер
```

> Успадковані зони доповнюємо **лише своїми додатками**, наявну поведінку не міняємо (модель тасок 14–24). Кожен дотик поза власною зоною задокументувати в Completion report → «Contract changes».

---

## Consumes

- **AISeoTools:** `tech_audit.pipeline.run_import`, `tech_audit.recheck.run_recheck`, `tech_audit.rerun.redetect`, `tech_audit.report.build_xlsx.build_tech_xlsx`, `tech_audit.models.TechMachineFindings/VerifiedIssue/IssueCheck`, `tech_audit.families.*`; dashboard `events`/`models` (task-08).
- **App:** `ProviderAdapter`/`CollectionContext`/`CollectionResult`/`ProgressEvent`/`Provider` (task-02); worker cell/lane/SSE/recovery (task-05); `AnalyticsDatasetRow` + `AnalyticsDatasetRepository` + `ProviderSnapshot` + `Artifact` + path-traversal guard (task-03/06); `job_events`/SSE-ендпоінт (task-05/23); фронтові `watchJob`/`startLiveCollection`/`ahrefs-panel`/`icon`/токени (task-12/18).

## Provides

- Зареєстрований адаптер `tech_audit` (audit-тип, point-in-time), готовий для worker.
- REST: run / latest / findings / recheck / export.xlsx.
- Machine-only XLSX-експорт (перший download-ендпоінт застосунку).
- Frontend-секція «Technical SEO Audit» на сторінці клієнта.

---

## Кроки реалізації

**AISeoTools:**
1. `seo_audit/dashboard/tech_audit.py`: `collect_tech_audit` (обгортка `run_import`, `render=False`, прогрес→events, cancel), `recheck_tech_audit` (обгортка `run_recheck` з фільтром targets), `build_tech_audit_xlsx` (виклик `build_tech_xlsx(TechVerdict(), machine=..., machine_only=True, strict=False)`). Source-neutral `TechAuditResult`/`RecheckResult` (dashboard `models.py`).
2. Адитивний `machine_only=False` kwarg у `build_tech_xlsx` (+ фільтр `col.ai`, skip VISUAL_UX/QA-гейт). Стара поведінка незмінна; перегенерувати contract-snapshot після рев'ю діффу.
3. Прогнати всю AISeoTools-suite (`.venv/bin/python -m pytest -q`) — зелено; закомітити; новий tag.

**App backend:**
4. `enums.py`: `Provider.tech_audit`; вичерпні enum-тести доповнити.
5. Міграція: `tech_recheck_results`; розширення `SyncJob.mode` (якщо CHECK). Down-revision chain; `upgrade head`/`downgrade base` чисто.
6. `app/db/repositories/tech_audit.py`: запис категорій+знахідок (через `AnalyticsDatasetRepository.bulk_insert`), апсерт/вибірка `tech_recheck_results`.
7. `TechAuditAdapter`: `preflight` (structural), `collect` з розгалуженням `job.mode` (crawl / recheck); лінивий `import seo_audit.dashboard.tech_audit`; run-dir під артефактами; `provenance_json.run_dir`; `emit(ProgressEvent)` після discovery/crawl/detect; cooperative cancel; підняти `cell_timeout_seconds` для доріжки.
8. `app/api/tech_audit.py`: 5 ендпоінтів (див. Обсяг); DTO у `api.py`; реєстрація в `app/integrations/__init__.py`.
9. Гарди: перевірити всі місця `tuple(Provider)` (dispatcher default, dashboard card, health, catalog) — `tech_audit` не потрапляє в KPI/health і не запускається рутинним синком.

**Frontend:**
10. `audit-panel.js` (клон `ahrefs-panel.js`): категорії→`<details>`-знахідки, toolbar, пагінація, per-row recheck-стан.
11. `client-page.js`: секція `#cp-audit` у `shell()`, рендер у `loadPeriodData()`, стан секції в URL.
12. `api.js`: `startTechAudit`, `getTechAuditLatest`, `getTechAuditFindings`, `recheckTechAuditFindings` (+ fixture-гілки + fixtures у `frontend/fixtures/`).
13. Кнопки: Run audit (`watchJob`), Re-check (`watchJob`, disable для graph-категорій), Export (`<a download>`). Усі обов'язкові стани + toast на старт/помилку.
14. Візуальний loop (AGENTS §15–16): скріншоти desktop/mobile у всіх станах; чекліст.

---

## Тести

- **AISeoTools** `tests/dashboard/test_tech_audit.py`: `collect_tech_audit` мапить категорії/знахідки з `TechMachineFindings`-фікстура; `render=False` не потребує браузера; `on_event` отримує стадії; cancel перериває; `recheck_tech_audit` фільтрує targets і повертає `skipped` для ORPHAN/SITEMAP; `machine_only=True` XLSX не має [ШІ]-колонок і VISUAL_UX-вкладки, а `machine_only=False` — байт-в-байт стара поведінка (регресія).
- **App unit** `test_tech_audit.py` (integrations): preflight `unavailable` коли нема `domain`/`seo_audit`; collect (crawl) пише 2 dataset_key + run_dir у provenance; collect (recheck) обробляє `queued`-рядки й апсертить статус; graph-категорія → `skipped`; cooperative cancel; секрети/units не течуть у events; `None` ≠ `0`.
- **App unit** `test_tech_audit.py` (api): latest/findings форма й пагінація; фільтри category/q; recheck graph-категорії → `409`; export без снапшота → порожньо/404, без `seo_audit` → `503`; path-traversal guard на export.
- **Migration** тест: `upgrade head`/`downgrade base` чисто; immutability guard не зачіпає `tech_recheck_results` (вона мутабельна).
- **Frontend e2e** (task-13 harness/fixtures): секція рендериться в станах no-run/loading/populated/partial/failed; Run/Re-check/Export доступні клавіатурою; severity не лише кольором; download-лінк присутній.
- **Live smoke** (поза CI): контрольний клієнт → повний краул у воркері → категорії/знахідки в UI звірені з `tech_findings_machine.json`; XLSX відкривається, лише [П]-колонки.

---

## Definition of Done / Gate

- [ ] Аудит запускається кнопкою на сторінці клієнта, крутиться у durable-воркері, показує живий SSE-прогрес, переживає рестарт, скасовується кооперативно.
- [ ] На сторінці — список категорій з severity+count; розгортання дає знахідки (URL + виміряні дані + `how_to_fix`), **без жодного ШІ-тексту**.
- [ ] «Re-check» перепровіряє окрему знахідку без повного краулу й оновлює її статус (`fixed/still_present/unreachable`); graph-категорії чесно позначені як «потрібен повний перезапуск».
- [ ] «Export to XLSX» віддає наш багатовкладковий шаблон з **лише машинними [П]-колонками** (жодних порожніх/фіктивних [ШІ]-клітинок, жодної VISUAL_UX-вкладки), через path-traversal-safe download.
- [ ] `Provider.tech_audit` **не** з'являється в KPI картки, health % чи рутинному синку; новий клієнт без аудиту → чистий empty-стан, не краш/нуль.
- [ ] `seo_audit` відсутній (CI/деплой) → ендпоінти чесно `unavailable`/`503`, `import app.main` не падає (усі імпорти ліниві).
- [ ] `null` ≠ `0`; severity — колір + текст + іконка; усі обов'язкові стани (AGENTS §6) реалізовані й переглянуті на скріншотах.
- [ ] Уся стара test-suite (App + AISeoTools) зелена; нові unit/e2e зелені; ruff/mypy чисті; усе — в Docker.

---

## Ризики

- **Enum-ripple.** `Provider.tech_audit` не має протікати в метрик-шляхи. Мітигація: усі `tuple(Provider)`-місця (dispatcher default, dashboard card, health, catalog) явно ревʼюнуті; рутинний синк ніколи не створює `tech_audit`-клітинку. Перевірити першим кроком бекенду.
- **Timeout.** Повний краул великого сайту > 20-хв per-cell дефолту ([runner.py:47](../../../app/jobs/runner.py#L47)). Мітигація: окремий `cell_timeout_seconds` для `tech_audit`-доріжки + `max_pages` bound + heartbeat під час краулу.
- **Immutability.** Статус перепровірки тільки в мутабельній `tech_recheck_results`; `AnalyticsDatasetRow`/`ProviderSnapshot` не чіпаємо. Recheck-джоб бере роботу з `queued`-рядків (self-contained черга).
- **CI/деплой без `seo_audit`.** Усі імпорти `seo_audit.*` — ліниві, всередині функцій; ендпоінти й адаптер деградують у `unavailable`, не в 500.
- **Дублювання `domain`.** `CollectionContext` не несе полів `Client` (крім `client_id`) — домен береться з `integration_config["domain"]` (той самий борг, що task-10). Для tech_audit можна дефолтити на `Client.domain` при тригері (у роутері, поза контрактом) — зафіксувати як рішення.
- **`build_xlsx` — чужа зона (tech_audit mode).** `machine_only` — строго адитивний optional kwarg; регресійний тест доводить незмінність старої поведінки; contract-snapshot перегенеровано свідомо.

---

## Майбутні під-таски (поза цією)

- **task-29** — Chromium в образ (`Dockerfile`/`compose.yaml`, task-01) + `render=True` → `SLOW_PAGES`/CWV + mobile-UX категорії.
- Планове авто-перезапускання аудиту (scheduler) — поза MVP.

---

## ✅ Completion report

- **Дата:** 2026-07-23
- **Гілка / комміт:** main (робоче дерево) — не закомічено, див. Follow-ups
- **Статус:** done-with-followups

### Що зроблено

Машинний технічний SEO-аудит наскрізно, від краулера AISeoTools до секції на сторінці клієнта:

- **AISeoTools** (`seo_audit/dashboard/tech_audit.py`, новий): `collect_tech_audit()` (обгортка `run_import`, `render=False`), `recheck_tech_audit()` (обгортка `run_recheck` з фільтром `targets`), `build_tech_audit_xlsx()` (machine-only XLSX), `result_from_machine()` + source-neutral моделі (`TechAuditResult`/`TechAuditCategory`/`TechAuditFinding`/`RecheckResult`). Категорія = *family* (`families.family_of`), знахідка = `(slug, url)`.
- **AISeoTools** (`tech_audit/report/build_xlsx.py`, точкова адитивна правка): `machine_only: bool = False` — ховає всі `col.ai`-колонки, пропускає QA-гейт і VISUAL_UX-вкладку. Дефолт лишає стару поведінку байт-в-байт (є регресійний тест).
- **App**: `Provider.tech_audit` + `METRIC_PROVIDERS`; `TechAuditAdapter` (краул / recheck, гілкується на `job_mode`); роутер `app/api/tech_audit.py` (runs / latest / findings / recheck / export.xlsx); DTO у `app/schemas/api.py`; міграція `d47b9c1e5a02` (`sync_jobs.params_json`).
- **Frontend**: секція «Technical SEO audit» на сторінці клієнта (`#cp-audit`), новий `frontend/js/audit-panel.js` (категорії → розгортувані знахідки), «Run audit» + per-finding «Re-check» через наявний `watchJob`, «Export to XLSX» як `<a download>`, стан секції в URL (`cat`/`fq`/`foff`), 3 фікстури.

### Як зроблено (ключові рішення)

- **Жодної нової таблиці під знахідки й recheck.** Спочатку планувалась мутабельна `tech_recheck_results`. Виявилось непотрібним: recheck пише **append-only** рядки `dataset_key="tech_recheck"` у наявну `AnalyticsDatasetRow`, а читання бере найсвіжіший рядок на `dimension_value` (`latest_rows_by_dimension`, новий метод репозиторію). Це зберігає immutability снапшотів і водночас дає «останній вердикт по кожній знахідці», навіть коли різні recheck-джоби торкались різних підмножин. `dimension_value` спільний для знахідки та її recheck: `"<slug>\x1f<url>"`.
- **Enum-міграція виявилась непотрібною.** `_str_enum` = `sa.Enum(...)` без `create_constraint`, а в SQLAlchemy 2.0 дефолт — `False`: у міграціях **немає жодного CHECK** на provider-колонках (перевірено grep-ом по `migrations/versions/`). Тому `Provider.tech_audit` — суто Python-рівень, DDL не потрібен. `SyncJob.mode` — звичайний `String(32)`, нові значення теж без міграції.
- **Єдина реальна міграція — `sync_jobs.params_json`.** Контракт `ProviderAdapter` забороняє адаптеру торкатись БД, а recheck потребує per-job параметрів (`run_dir` + список `(slug, url)`). Вони їдуть через job → `CollectionContext.job_params`. Разом з `job_mode` це два адитивні optional-поля контракту task-02.
- **`METRIC_PROVIDERS` замість `tuple(Provider)`.** Головний ризик таски — щоб `tech_audit` не протік у метричні шляхи. Замість крапкових перевірок введено явний кортеж «метричних провайдерів» і застосовано у трьох місцях, що ітерували весь enum: `app/api/jobs.py` (розгортання `providers="all"` — інакше кожен рутинний «sync all» стартував би повний краул кожному клієнту), `app/api/dashboard.py` (KPI + health-беkфіл) і `app/api/providers.py` (ops-грид). Dispatcher **навмисно** лишено на `tuple(Provider)`: він має підняти доріжку `lane-tech_audit`, а рутинні джоби просто не створюють для неї клітинок.
- **`tech_audit` — провайдер без інтеграції.** Аудит перевіряє власний сайт клієнта, налаштовувати нічого. Тому в `runner.py` гейт `not_configured` пропускається саме для нього, `source_mode` дефолтиться на `auto`, а домен береться з `Client.domain` (клієнт не мусить дублювати його в конфіг). Альтернативу — автостворювати рядок `client_integrations` — відкинуто: вона б засвітила не-метричного провайдера в усіх UI-списках інтеграцій.
- **Краул у `collect()`, не в `collect_datasets()`.** Тільки `collect()` отримує `is_cancel_requested`, тож довгий краул має жити там, щоб його можна було скасувати. Результат передається у `collect_datasets()` через `self._pending[id(context)]`: runner викликає їх послідовно на одну клітинку (join між потоками), тож це безпечно й дешевше за подвійний краул.
- **Кооперативне скасування через log-checkpoint.** `run_import` не приймає cancel-колбек, але періодично викликає `log()`. Обгортка кидає з нього `TechAuditCancelled` — **`BaseException`**, щоб проскочити повз `except Exception` навколо рендер-стадії. Адаптер перекладає його в `CancelledByAdapter`.
- **Фронт: один cache-buster на всі модулі.** `?v=` — частина URL модуля, тож два різні суфікси = **два екземпляри** одного модуля зі своїм станом. Тому версію піднято глобально `20260723-chart1 → 20260723-audit1`, а не лише на змінених файлах.

### Контрактні зміни (Contract changes)

- **`app/schemas/enums.py`** (task-02/14 зона): `+ Provider.tech_audit`, `+ METRIC_PROVIDERS`. Споживачі: jobs/dashboard/providers API, dispatcher, фронт.
- **`app/schemas/adapter.py`** (task-02): `CollectionContext` отримав `job_mode: str = ""` і `job_params: dict = {}` (адитивно, дефолти зберігають поведінку метричних адаптерів).
- **`app/schemas/api.py`** (task-02/15): 7 нових `TechAudit*` DTO.
- **`app/db/models.py` + `migrations/`** (task-03): `SyncJob.params_json` (nullable JSON) + ревізія `d47b9c1e5a02`.
- **`app/db/repositories/jobs.py`**: `create_run(..., params=None)`. **`analytics_datasets.py`**: `+ latest_rows_by_dimension()`.
- **`app/jobs/runner.py`** (task-05): `tech_audit`-гілка (див. вище) + прокидання `job_mode`/`job_params`.
- **AISeoTools**: `build_tech_xlsx(..., machine_only=False)`; `dashboard.models.Provider` отримав `tech_audit`. Contract-снапшот AISeoTools перегенеровано (діфф — рівно новий enum-член).
- **Перегенеровано снапшоти:** `tests/unit/schemas/data/contract_snapshot.json`, `tests/unit/api/data/openapi_snapshot.json` (діффи звірені: лише `tech_audit`/`job_mode`/`job_params` + 5 нових шляхів).

### Як перевірити (verification)

AISeoTools (хост, власний .venv):
```bash
cd /home/elon/dev/seo/AISeoTools
.venv/bin/python -m pytest tests/dashboard/test_tech_audit.py -q     # 7 passed
.venv/bin/python -m pytest -q                                        # 1580 passed
```

Застосунок (тільки Docker):
```bash
docker compose run --rm --no-deps api sh -c "poetry run ruff check app tests && poetry run mypy app && poetry run pytest -q"
# All checks passed! / Success: no issues found in 79 source files / 545 passed, 1 skipped
```

Фронт (fixture-режим, статичний сервер + Playwright):
```bash
python3 -m http.server 4173 --directory frontend &
NODE_PATH=/tmp/yovth-e2e/node_modules BASE_URL=http://127.0.0.1:4173 node <audit-visual-check.cjs>
# перевіряє: 6 категорій, severity текстом (не лише кольором), export-лінк,
# розгортання категорії у 3 знахідки, cat= в URL, recheck-вердикт,
# disabled re-check для orphan + пояснення, empty-стан з CTA,
# мобільний 390px без горизонтального переповнення
```
В UI: `#/client/<id>` → секція «Technical SEO audit» → «Run audit» → категорія → «Re-check» на рядку → «Export to XLSX».

### Тести

- **AISeoTools** `tests/dashboard/test_tech_audit.py` (7, новий): мапінг machine→категорії/знахідки, пропуск `present is False`, серіалізація `link_targets`, machine-only XLSX **без** `[ШІ]`-колонок, `collect_tech_audit` з подіями, скасування, фільтр `targets` у recheck.
- **App** `tests/unit/integrations/test_tech_audit_adapter.py` (16, новий): preflight (no domain / no AISeoTools / ok), краул без метрик + `run_dir` у provenance, датасети (2 ключі, `recheckable` за категорією), «датасети без collect()» → чесний `no_payload`, нульовий краул → `unavailable` (не 0), падіння краулу не кидає й не тече текстом помилки, скасування до старту й у процесі, recheck без таргетів / без run-dir / успішний, звірка `RECHECK_SKIP_CATEGORIES` з AISeoTools.
- **App** `tests/unit/api/test_tech_audit.py` (9, новий): no-run → `has_run:false` і `health_score:null` (не 0), summary з rollup-ом, знахідки з фільтром/пошуком + **join recheck-у з іншого снапшота**, orphan лишається `null`/`recheckable:false`, запуск джоба, 404 на невідомого клієнта, 409 `needs_full_rerun`, targeted-recheck джоб несе `run_dir`+`findings`, 404 без прогону, export 404 без прогону, **export відмовляє run-dir-у, що тікає з artifacts-root**.
- **Регресія:** оновлено `tests/unit/schemas/test_enums.py` (+ окремий тест «METRIC_PROVIDERS не містить tech_audit») і `tests/unit/api/test_providers.py` (грид = метричні провайдери).

### Пост-релізні фікси (після першого живого прогону)

Перший живий запуск на SHB (smashhouseburgers.com) виявив два дефекти — обидва виправлені й перевірені наживо.

1. **Прогін зберігав 0 даних (`IntegrityError`).** Краул відпрацьовував, але падав `persist`:
   `UNIQUE constraint failed: analytics_dataset_rows.snapshot_id, dataset_key, dimension_value`.
   Причина: детектори `duplicate_title`/`duplicate_meta_desc` **легітимно** віддають той самий URL
   двічі (сторінка входить у дві групи дублікатів), а ключ знахідки — `slug\x1furl`. Колізія рвала
   **всю** транзакцію, тож не зберігалось нічого.
   Фікс на двох рівнях: (а) продюсер — `_findings_from_machine` дедуплікує за `(slug, url)`,
   **зливаючи evidence** обох груп, а `finding_count` категорії рахується з дедуплікованих знахідок,
   тож число в UI = число рядків; (б) споживач — `TechAuditAdapter._crawl_datasets` відкидає
   повторний `dimension_value` перед віддачею runner-у: одна колізія більше не може знищити прогін.
   Перевірено на **тих самих даних, що впали** (34 сирі перевірки → 32 унікальні знахідки), потім
   живим прогоном: 32 знахідки, 8 категорій, health 94.
2. **Спінер не крутився.** `icon()` застосовує анімацію лише коли передати клас:
   `icon("loader-2", "icon icon--spin")` (`.icon--spin` живе в `app.css`). Виправлено в
   `audit-panel.js` (Run audit + Re-check) і — той самий брак на тій же сторінці — у
   `client-page.js::setLiveButtonBusy`. Підтверджено в браузері: `animationName = session-spin`.

Побічно виправлено, теж з живого прогону:

- **Warning дублювався** в UI: адаптер віддавав ті самі crawl-warnings і з `collect()`, і з
  `collect_datasets()`, а runner конкатенує обидва списки → тепер датасет-результат їх не повторює.
- **Мертві колонки в XLSX**: `Len`/`New len` міряли довжину вже видаленого `[ШІ]`-тексту й ішли
  колонкою нулів. Додано `AI_COMPANION_KEYS` — при `machine_only` вони випадають разом зі своєю
  `[ШІ]`-колонкою. Експортований звіт: 9 вкладок, **0 мертвих колонок**.
- `TechAuditFinding.evidence` (нове поле) прокинуто до UI — без нього рядки `dup_titles`/`dup_descs`
  не пояснювали, у чому саме проблема.

**Операційна нотатка:** worker та api — довгоживучі python-процеси; зміни коду підхоплюються лише
після `docker compose restart worker api`. Перше «фікс не спрацював» було саме цим.

### Живий прогін (verification, 2026-07-23)

```text
POST /clients/<SHB>/tech-audit/runs      → job succeeded
GET  .../tech-audit/latest               → has_run=true, 16 pages, health 94, 8 категорій, 32 знахідки, 1 warning
POST .../findings/recheck (3xx_redirect) → recheck_status="still_present"
POST .../findings/recheck (orphan_page)  → HTTP 409 needs_full_rerun
GET  .../tech-audit/export.xlsx          → 200, SHB_Tech-Audit_20260723.xlsx, 9 вкладок, 0 [ШІ]-колонок
```

### Follow-ups / борг

- **Нічого не закомічено** (ні в застосунку, ні в AISeoTools) — комміт/пін AISeoTools на новий tag лишається свідомим кроком користувача, за зразком task-10.
- **Рендер/CWV не входив** (рішення користувача): `SLOW_PAGES`/Core Web Vitals не з'являться, доки Chromium не буде в образі — це **task-29**.
- **Per-cell timeout 20 хв може бути замалим** для великого сайту: `cell_timeout_seconds` глобальний (`Dispatcher`), окремого значення на доріжку `tech_audit` немає. Поки що обмежується `max_pages` (дефолт 1000). Варто зробити per-provider timeout.
- **Скасування посеред краулу — best-effort**: спрацьовує на checkpoint-ах прогресу `run_import`. Якщо краул зависне поза ними, межею лишається cell-timeout.
- **Наявні e2e-смоуки (`tests/e2e/frontend/*.cjs`) не проганялись** — вони падають і **без** моїх змін (перевірено через `git stash`): їм потрібен реальний застосунок як BASE_URL, а не статичний сервер. Моя перевірка фронту робилась окремим скриптом проти статики. Варто прогнати їх проти піднятого стека.
- **Живого прогону проти реального сайту не було** — краул перевірено лише на замоканому `run_import`. Перший реальний `Run audit` варто зробити під наглядом.
- **`_pending` в адаптері** тримає результат між `collect()` і `collect_datasets()`. Якщо runner колись почне викликати їх не послідовно, це треба буде переробити (є тест на «датасети без collect()»).

---

## 🔧 Follow-up (2026-07-24): live-% прогрес + фікс зависання `image_dims`

**Симптом (ALPHA / alphastyle.store):** клітинка аудиту «висіла» на `0% / Crawling site pages`, а потім гинула. Розбір живої БД: краул 280 стор. відпрацював за ~35 c, але пост-краул крок `probe_image_dimensions` (pipeline.py) тягнув повнорозмірні (`?width=4096`) Shopify-зображення для **~1945 позначених великих картинок**. Він не слав жодних подій → heartbeat замерзав → через 5 хв lease клітинки спливав і **весь краул (тисячі знахідок) губився** як `failed_interrupted`.

**Дві першопричини:**

1. **Жоден crawl-event не ніс `percent`.** Краулер слав `crawled X/Y` як вільний текст (`message`), `percent=None`; `CellRepository.set_progress` ігнорує `None` → `progress_percent` стояв на 0 весь прогін. Тому будь-який сайт висів на «0% / Crawling site pages», а не лише ALPHA.
2. **`probe_image_dimensions` не мав ні загального дедлайну, ні heartbeat.**

**Що зроблено (усе адитивно, CLI незачеплено):**

- **AISeoTools `seo_audit/techissues/probes/image_probe.py`** — `probe_image_dimensions(...)` отримав keyword-only `overall_deadline` та `on_progress(done, total)`: wall-clock-кеп на весь прохід (при таймауті лишаються часткові розміри — деградація краща за втрату аудиту) + тік на кожну картинку (heartbeat).
- **AISeoTools `seo_audit/tech_audit/pipeline.py`** — `run_import`/`finish_run`/`_crawl_progress` отримали опційний `on_progress(stage, done, total)`; стадії `crawl` / `probe` / `detect` / `image_dims` тепер репортять структуровано; `image_dims_deadline=90.0` за замовчуванням.
- **AISeoTools `seo_audit/dashboard/tech_audit.py`** — `collect_tech_audit` будує `_on_progress` із `emit`, який мапить `(stage, done, total)` на монотонний overall-percent через `_STAGE_BANDS` (crawl 5–65, probe 65–72, detect 72–80, image_dims 80–96; фінальний «ready» = 97, persist у застосунку = 100). Cancel тепер поллиться і на цьому «хвості», не лише на checkpoint-ах краулу.
- **Застосунок `frontend/js/formatters.js`** — додано `STAGE_LABELS` для `probe` / `image_dims` (адаптер уже форвардив `percent`/`completed`/`total`/`stage`, тож бек-змін не було; контракт/OpenAPI без змін).

**Тести:** `tests/test_tech_audit_progress.py` (AISeoTools) — `on_progress` монотонний і б'є 0→100; `overall_deadline` повертає часткові розміри без зависання. Уся стара tech-audit + techissues + dashboard suite зелена (148+38); app tech-audit тести зелені (32).

**Живий прогін (2026-07-24, alphastyle.store):** percent росте безперервно (1→63 crawl → 72 probe → 80 detect → 80→96 image_dims → 97), прогін **завершується** за 230 c (`pages_crawled=235, findings=2885`); `image_dims` упирається в 90-c дедлайн і виходить із частковими розмірами замість того, щоб висіти. Раніше цей самий крок висів 5+ хв.

**Ще follow-ups:**

- **Осиротіла клітинка** `719f89e3-…` (стара ALPHA) лишилась `running`/0% у БД: startup-recovery (`app.jobs.recovery`) її **не** реквелила (`requeued_interrupted=0`) попри прострочений lease — окремий баг recovery, може блокувати доріжку `tech_audit`. Треба розібрати чому `expired_cells` її не підхопив.
- **Тихі вікна поза `image_dims`:** фаза discovery (`discover_urls`, ~115 c) і блок `probe` (link/sitemap/image HEAD-проби, ~18 c) шлють heartbeat лише на межах. Для ALPHA обидва < 300 c lease, але дуже великий сайт може впертися — варто додати heartbeat і туди.
- **Комміт/пін AISeoTools** на новий tag лишається свідомим кроком користувача (як зазначено вище).
