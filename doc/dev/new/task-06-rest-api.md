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
- Jobs: `POST /sync-jobs` (202 + job_id + status/events URL), `GET /sync-jobs/{id}`, `GET /sync-jobs/{id}/events` (SSE, монтує stream helper task-05), `POST /sync-jobs/{id}/cancel`, `POST /sync-jobs/{id}/retry`, `GET /sync-jobs?status=&client_id=`.
- Ops: `GET /api/v1/providers/status` (configured/auth/profile/preflight без секретів). (`/healthz`, `/readyz` — task-01/03.)
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
2. Dashboard endpoint: зібрати клієнтів, останні success-snapshots per provider/period, порахувати delta/quality через task-04, віддати DTO. Ніякої математики на фронті.
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
- [ ] Усі endpoint'и §10 реалізовані й відповідають DTO task-02.
- [ ] Dashboard самодостатній для рендеру картки (delta/quality/source/freshness/ranges всередині).
- [ ] SSE віддає `text/event-stream` з replay; є polling-fallback.
- [ ] Безпека §13: CORS, headers, safe errors, безпечний logo upload, artifact path guard.
- [ ] OpenAPI експортований і стабільний — task-12 кодує проти нього.
- [ ] Integration-тести зелені на тимчасовій БД.

## Ризики
- Спокуса рахувати бізнес-семантику на фронті — заборонено; усе через task-04, віддавати готове.
- Розбіжність назв полів DTO ↔ ORM — канон у task-02; узгоджувати там.
