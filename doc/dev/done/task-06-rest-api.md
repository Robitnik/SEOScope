# task-06 — REST API (FastAPI роутери + OpenAPI)

- **Пакет:** HTTP-шар v1: dashboard, clients+integrations, jobs, providers/ops; OpenAPI як контракт для фронту
- **Хвиля:** B
- **Depends on:** task-01 (app/config/реєстрація роутерів), task-02 (DTO), task-03 (репозиторії), task-04 (delta/quality/periods)
- **Blocks:** task-12 (фронт кодує проти OpenAPI)
- **PLAN.md:** §10 (REST API v1 — повністю), §9 (job endpoints + SSE), §13 (безпека API), Етап 2/4

## Мета

Реалізувати REST v1 із PLAN §10. Dashboard-відповідь **одразу** повертає current/previous, delta, quality, source, freshness і точні діапазони — фронт бізнес-семантику не рахує. OpenAPI — джерело контракту для integration-тестів і фронту.

## Обсяг

**Входить (усі під `/api/v1`, окрім ops):**
- Dashboard: `GET /dashboard?period=&anchor=&status=&provider=&q=`, `GET /clients/{id}/metrics?period=&history=12`.
- Clients/integrations: `GET/POST /clients`, `GET/PATCH /clients/{id}`, `POST /clients/{id}/logo`, `PUT /clients/{id}/integrations/{provider}`, `POST /clients/{id}/integrations/{provider}/preflight`.
- Jobs: `POST /sync-jobs` — приймає **одного клієнта або batch** (`client_ids: [...] | "all"`, `providers | "all"`, `mode: collect_missing|force_refresh`), 202 + `job_id`/`batch_id` + status/events URL (PLAN §9.1). `GET /sync-jobs/{id}` повертає **per-cell прогрес** клітинок `(client, provider)` для матриці. `GET /sync-jobs/{id}/events` (SSE, stream helper task-05). `POST /sync-jobs/{id}/cancel`. `POST /sync-jobs/{id}/retry` — **лише failed-клітинки**. `GET /sync-jobs?status=&client_id=&batch_id=`.
- Ops: `GET /api/v1/providers/status` (configured/`not_configured`/auth/profile/preflight без секретів). (`/healthz`, `/readyz` — task-01/03.)
- Валідація запитів (Pydantic), маппінг доменних станів у HTTP-коди, безпечні error-DTO (без секретів).
- Logo upload: MIME/signature/size/dimensions validation, generated safe filename, збереження артефакту через репозиторій.
- Реєстрація роутерів через механізм task-01 (без правок `app/main.py`).

**НЕ входить:** сам worker/збір (task-05), нормалізаційна математика (task-04 — лише виклик), фронт (task-12), ORM (task-03 — лише через репозиторії).

## Owned paths
```
app/api/dashboard.py
app/api/clients.py
app/api/jobs.py
app/api/providers.py            # /providers/status
app/api/deps.py                 # спільні залежності роутерів (session, pagination, auth-hook)
# health.py — НЕ чіпати (task-01); readiness DB-check — task-03
```

## Consumes
- API DTO з `app/schemas/api.py`, enums — task-02.
- Репозиторії — task-03.
- `resolve_periods`, `compute_delta`, `assess_quality`, `CATALOG` — task-04.
- `JobRepository.create_queued`, `event_stream` helper — task-03/05.

## Provides
- Стабільний OpenAPI v1 (експортувати `openapi.json` у `docs/` або через endpoint) — контракт для task-12 і integration-тестів task-13.
- Форма dashboard-відповіді, на яку рендериться картка клієнта.

## Кроки реалізації
1. `deps.py` (async session, пагінація, майбутній auth-hook — заглушка за PLAN §13).
2. Dashboard endpoint: зібрати клієнтів, останні success-snapshots per provider/period, порахувати delta/quality через task-04, віддати DTO. Ніякої математики на фронті. Якщо для клієнта немає інтеграції з сервісом → `ProviderSummaryDTO.state = not_configured` (з config, навіть без жодного запуску), не нуль.
3. Clients/integrations CRUD + logo upload (safe).
4. Jobs: create (idempotent, 202), get, list, cancel, retry; SSE endpoint через stream helper (task-05) з `Last-Event-ID`; fallback polling `GET /sync-jobs/{id}`.
5. providers/status без секретів.
6. Security: restrictive CORS, security headers (з task-01 core), error-DTO без секретів, artifact download через авторизований endpoint із path-traversal guard.
7. Згенерувати/зафіксувати OpenAPI.

## Тести (PLAN §15 integration)
- FastAPI + тимчасова SQLite (+ за потреби fake worker task-05).
- Dashboard повертає current/previous/delta/quality/ranges; `None` не як `0`; delta-семантика через direction.
- Валідація period/anchor/provider; безпечні помилки.
- Jobs: POST→202→get/list; cancel/retry; SSE replay через `Last-Event-ID`.
- Logo upload: відхиляє неправильний MIME/розмір/розміри; безпечне ім'я файлу.
- Немає секретів у відповідях/помилках.
- OpenAPI snapshot стабільний (diff помітний).

## Definition of Done / Gate
- [x] Усі endpoint'и §10 реалізовані й відповідають DTO task-02.
- [x] `POST /sync-jobs` приймає одного клієнта і batch; `retry` повторює лише failed-клітинки; `GET` віддає per-cell прогрес.
- [x] `not_configured` віддається у dashboard/providers-status без запуску збору (новий клієнт → CTA, не нуль).
- [x] Dashboard самодостатній для рендеру картки (delta/quality/source/freshness/ranges всередині).
- [x] SSE віддає `text/event-stream` з replay; є polling-fallback.
- [x] Безпека §13: CORS, headers, safe errors, безпечний logo upload, artifact path guard.
- [x] OpenAPI експортований і стабільний — task-12 кодує проти нього.
- [x] Integration-тести зелені на тимчасовій БД.

## Ризики
- Спокуса рахувати бізнес-семантику на фронті — заборонено; усе через task-04, віддавати готове.
- Розбіжність назв полів DTO ↔ ORM — канон у task-02; узгоджувати там.

---

## ✅ Completion report

- **Дата:** 2026-07-22
- **Гілка / комміт:** `main` @ c0ea8d3 (working tree, не закомічено — за правилом "commit лише на явний запит")
- **Статус:** done

### Що зроблено
Усі REST v1 endpoints із PLAN §10 реалізовані та підключені через `register_router` (без правок `app/main.py`):
- **Dashboard:** `GET /api/v1/dashboard` (period/anchor/status/provider/q), `GET /api/v1/clients/{id}/metrics` (історія по метриках). Відповідь самодостатня: current/previous, delta, `is_improvement`, quality (з freshness-даунгрейдом), source, точні `current_range`/`previous_range` — фронт нічого не рахує.
- **Clients/integrations:** повний CRUD, `PUT .../integrations/{provider}`, `POST .../preflight` (ніколи не падає — `not_configured`/`adapter_unavailable`/`preflight_error` замість краху), safe logo upload/download.
- **Jobs:** `POST/GET/GET-list /sync-jobs`, `POST .../cancel`, `POST .../retry`, `GET .../events` (SSE, replay за `Last-Event-ID`), `GET /api/v1/artifacts/{id}` (raw-artifact download, path-traversal guard).
- **Ops:** `GET /api/v1/providers/status` — стан кожної `(client, provider)` клітинки без секретів.

### Як зроблено (ключові рішення)
- **Два стилі доступу до БД, свідомо різні:** `dashboard.py`/`clients.py`/`providers.py` використовують request-scoped `AsyncSession` (`app.api.deps.SessionDep`) через `session.run_sync(...)`, з явним `await session.commit()` на кожному мутуючому шляху (сам `get_db()` не комітить). `jobs.py` натомість наскрізь використовує `worker_session_scope` (той самий синхронний контракт, що і job engine/task-05), бриджований у event loop через `run_in_threadpool` — це узгоджено з тим, як уже написані `app.jobs.control.cancel_run`/`retry_failed_only`/`event_stream`, і уникає читання засталого SQLite-снепшота, який стався б, якби один і той самий `AsyncSession`-transaction тримався відкритим між "перевірити існування" і "викликати cancel_run в іншому з'єднанні".
- **Провайдер-рядок картки:** `state` береться з якості СНЕПШОТА колекції (freshness-assessed через будь-який `MetricSpec` провайдера — усі на MVP мають однаковий SLA), а не як worst-of-all-metrics rollup; інакше один не заповнений опційний catalog-метрик (напр. `gsc_avg_position`) завжди тягнув би картку в `unavailable`, навіть коли збір реально пройшов `complete`. Кожна окрема `MetricCardValue.quality` лишається чесною (`unavailable`, якщо саме цього значення нема).
- **Range vs snapshot метрики:** для `comparison_kind="range"` current+previous читаються з ОДНОГО снепшота (адаптер повертає обидва в одному `collect()`); для `comparison_kind="snapshot"` current і previous — це два РІЗНІ `ProviderSnapshot` (поточний і останній перед ним), обидва з `period_role=snapshot`.
- **Logo upload без multipart:** ендпоінт приймає сирі байти зображення як тіло запиту (`Content-Type: image/png|jpeg`) замість `multipart/form-data` — це усунуло потребу в нових залежностях (`python-multipart`), яку я не міг залокати в цьому середовищі (немає сусіднього checkout AISeoTools, від якого залежить `poetry lock` навіть для непов'язаного group; див. Follow-ups). MIME/сигнатура/розміри валідуються вручну (PNG IHDR chunk, JPEG SOF marker) без зовнішньої image-бібліотеки; SVG свідомо не підтримується (XXE-ризик, немає чітких "dimensions").
- **Головні файли:** `app/api/deps.py` — спільні `Annotated`-залежності (сесія, пагінація, settings, auth-стаб); `app/api/dashboard.py`, `app/api/clients.py`, `app/api/jobs.py`, `app/api/providers.py` — самі роутери.
- **Відхилення від плану:** artifact-download ендпоінт (`GET /api/v1/artifacts/{id}`) не був явно перелічений у PLAN §10, але DoD-гейт цієї таски вимагає "artifact path guard" — додав мінімальний, добре обмежений ендпоінт поверх уже готового `ArtifactRepository.get_for_download`.

### Контрактні зміни (Contract changes)
Усі — суто адитивні (нові поля/методи, жодного існуючого сигнатури/DTO не змінено):
- `app/db/repositories/metrics.py`: `+ for_snapshot(snapshot_id)`.
- `app/db/repositories/cells.py`: `+ latest_for_client_provider(client_id, provider)`, `+ list_for_batch(batch_id)`.
- `app/db/repositories/clients.py`: `+ list_all()`.
- `app/db/repositories/jobs.py`: `+ list_for_batch(batch_id)`.
- `app/db/repositories/steps.py`: `+ list_for_job(job_id)`.
- `app/schemas/api.py`: `+ ClientMetricsHistoryDTO`, `+ MetricHistorySeriesDTO`, `+ MetricHistoryPointDTO` (форма `GET /clients/{id}/metrics`, яку task-02 свідомо залишив невизначеною для цієї таски).
- **task-12 (фронт):** кодує проти `docs`/`/openapi.json` цього застосунку; знімок зафіксовано в `tests/unit/api/data/openapi_snapshot.json`.

### Як перевірити (verification)
```bash
docker compose build api
docker compose run --rm api poetry run ruff check app/api app/db/repositories app/schemas tests/unit/api
docker compose run --rm api poetry run mypy app/api app/db/repositories app/schemas
docker compose run --rm api poetry run pytest -q                      # 185 passed (увесь проєкт)
docker compose run --rm api poetry run pytest tests/unit/api -q       # 39 passed (ця таска)

# Live smoke (як і було реально прогнано):
API_PORT=8091 docker compose up -d api
curl -s localhost:8091/healthz && curl -s localhost:8091/readyz
curl -s localhost:8091/api/v1/dashboard | python3 -m json.tool
curl -s localhost:8091/api/v1/providers/status | python3 -m json.tool
# create client -> PUT integration -> POST preflight (adapter_unavailable, не краш)
# -> POST /sync-jobs -> GET status -> POST cancel -> перевірити cancel_requested
# -> POST logo (raw PNG bytes) -> GET logo (200 image/png)
docker compose stop api && docker compose rm -f api
```

### Тести
`tests/unit/api/` (новий, 39 тестів, увесь мій):
- `test_clients_api.py` (перейменовано з `test_clients.py` — конфлікт basename з `tests/unit/db/test_clients.py` без `__init__.py`-пакетів у test-дереві) — CRUD, 409 на дублікат slug/domain, safe 404, upsert integration (без secret-полів у відповіді), preflight `not_configured`/`adapter_unavailable`, logo: bad signature/undersized/oversized/success (PNG+JPEG)/download/404.
- `test_dashboard.py` — новий клієнт → `not_configured`, ніколи не 0; complete-снепшот → delta/`is_improvement`/точні ranges; відсутній метрик → `None`+`delta_reason`; provider/search-фільтри; `/clients/{id}/metrics` історія + 404.
- `test_jobs.py` — single vs batch (`job_id` XOR `batch_id`), 404 на невідомого клієнта, per-cell progress, list-фільтри, cancel (`cancel_requested`), retry (лише failed, `attempt+=1`), SSE replay + завершення при termінальному job, redaction через `make_emitter` (не сирий `EventRepository.append`), artifact download roundtrip + 404.
- `test_providers.py` — `not_configured` для нового клієнта, `failed`+`reason_code`, `complete` зі снепшотом, немає секретів у відповіді.
- `test_openapi_snapshot.py` — знімок `/openapi.json`, diff помітний при зміні контракту.

### Follow-ups / борг
- `python-multipart` навмисно НЕ додано в `pyproject.toml`: спроба `poetry lock` у цьому середовищі падає ще на етапі валідації `tool.poetry.group.local` (сусідній checkout `../../seo/AISeoTools` відсутній), тож дистрибутив не вдалося перелокати. Logo upload натомість приймає сирі байти (`Content-Type` замість `multipart/form-data`) — працює і покрито тестами, але якщо колись знадобиться справжній multipart (кілька файлів/полів в одному запиті), це вимагатиме `task-01`-запиту на нову залежність і перелокання на машині з доступним AISeoTools checkout.
- Provider-level `state` для "ще нічого не збирали" (немає снепшота, немає жодної клітинки) повертає `unavailable`; якщо продукту потрібен окремий `pending_first_sync`-стан — це контрактна зміна `ProviderCardState` (task-02).
- `GET /clients` не має явного `q`/`enabled`-фільтра (лише пагінація) — дашборд-пошук уже є на `/dashboard`; додати за потреби окремим PR у свій owned path.
- Header-рівня `current_range`/`previous_range` у `DashboardResponse` рахується у фіксованій `America/New_York` (лише для лейбла сторінки) — кожна картка/метрика однаково рахує точні діапазони у СВОЄМУ timezone клієнта, тож це не впливає на коректність цифр.
