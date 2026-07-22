# task-03 — Database (ORM, міграції, репозиторії)

- **Пакет:** SQLAlchemy 2.x ORM, Alembic-міграції, repository-шар
- **Хвиля:** B
- **Depends on:** task-01 (скелет, config, залежності)
- **Blocks:** task-05, task-06, task-07
- **PLAN.md:** §6 (модель даних — повністю), §5.1 (SQLite WAL, один writer), §12 (quality rules — впливають на схему), Етап 2

## Мета

Реалізувати всю модель даних із PLAN §6 на SQLAlchemy 2.x + Alembic і дати **repository-контракти**, через які працюють і API (`sqlite+aiosqlite`), і worker. SQLite у режимі WAL, `foreign_keys=ON`, `busy_timeout`, короткі транзакції, один write-heavy worker.

## Обсяг

**Входить:**
- `app/db/base.py` — Declarative base, naming convention, типи (UUID/text PK, Decimal, UTC datetime, JSON).
- `app/db/session.py` — async engine/session для API + sync session для worker; PRAGMA (WAL, foreign_keys, busy_timeout) на connect.
- `app/db/models.py` — усі таблиці §6: `clients`, `client_integrations`, `sync_jobs`, `sync_job_steps`, `job_events`, `provider_snapshots`, `metric_values`, `artifacts`, `imported_sources`.
- `app/db/repositories/` — репозиторії з чіткими методами (див. нижче), інкапсулюють транзакції/запити. **Ніякого сирого SQL у верхніх шарах.**
- `migrations/` (Alembic env + перша міграція), `alembic.ini`.
- Доповнення `GET /readyz` (task-01 лишив хук): перевірка доступності БД і що `alembic` на `head`.

**НЕ входить:** бізнес-логіка нормалізації (task-04), самі роутери (task-06), імпорт (task-07 — лише **використовує** репозиторії).

## Owned paths
```
app/db/base.py
app/db/session.py
app/db/models.py
app/db/repositories/**
migrations/**
alembic.ini
# + рядок у readiness-check (узгодити з task-01 hook, без правок його інших файлів)
```

## Consumes
- `app/core/config.py` (DB URL, шляхи) — task-01.
- Enum'и з `app/schemas/enums.py` — task-02 (типи колонок статусів/quality/period_role мають збігатися зі значеннями enum).

## Provides — репозиторні контракти (споживають task-05/06/07)
Орієнтовний набір (уточнити сигнатури у процесі, але зберегти семантику):
```
ClientRepository:      list_active, get, create, update, set_logo, upsert_integration, get_integration
JobRepository:         create_queued(idempotency_key,...), claim_next(lease_owner, lease_ttl),
                       heartbeat, set_status, set_progress, expired_leases, get, list(status,client)
StepRepository:        upsert_step(job,provider,stage,attempt,...), set_step_status
EventRepository:       append(job_id, level, provider, stage, percent, message, details) -> seq,
                       since(job_id, last_seq), purge_older_than(days)
SnapshotRepository:    create_immutable(...), latest_success(client, provider, period_role, period)
MetricRepository:      bulk_insert(values), for_dashboard(clients, period, ranges), history(client, metric, n)
ArtifactRepository:    create(...), get_for_download(id) with path-traversal guard
ImportRepository:      seen(sha256), record(source, sha256, importer_version)
```
**Інваріанти:**
- `provider_snapshots` і `metric_values` — **immutable після commit** (лише insert, без update).
- `job_events.seq` — auto-increment, водночас SSE event id (монотонний).
- `metric_values`: unique `(snapshot_id, metric_key, period_role)`; Decimal без обрізання до int; `value_numeric` nullable (None ≠ 0).
- `sync_jobs`: `claim_next` — атомарний claim у короткій транзакції з lease (`lease_owner`, `lease_expires_at`, `heartbeat_at`).
- Жодних секретів у `client_integrations` (лише non-secret ідентифікатори + `config_json` для валідованих non-secret settings).
- Всі datetime у БД — UTC; period boundaries — local dates timezone клієнта.

## Кроки реалізації
1. `base.py` + типи (Decimal з фіксованою точністю, UTC-aware datetime, JSON-стовпці).
2. `session.py` — engine (async+sync), PRAGMA-хук на `connect`, dependency для FastAPI, context manager для worker.
3. `models.py` — усі 9 таблиць з §6: PK, FK (`ON`), unique-констрейнти, індекси (`(job_id, seq)`, client/provider/metric/date).
4. Репозиторії — по одному файлу на агрегат; транзакційні межі всередині.
5. Alembic init + автогенерація першої міграції; перевірити, що upgrade/downgrade проходять на чистій БД.
6. `readyz`: DB ping + `alembic current == head`.

## Тести
- `tests/unit/db/` — на тимчасовій SQLite:
  - PRAGMA застосовано (WAL, foreign_keys=ON).
  - unique/FK-констрейнти реально спрацьовують.
  - immutability snapshot/metric (спроба update → заборонено на рівні репозиторію).
  - `claim_next` атомарний: два claim не беруть одну job; expired lease повертається.
  - `event.append` монотонний seq; `since` фільтрує коректно.
  - `None` зберігається як NULL і читається як `None`, не `0`.
- Alembic: `upgrade head` → `downgrade base` чисто на порожній БД.

## Definition of Done / Gate
- [ ] `alembic upgrade head` створює всю схему §6; `downgrade` чистий.
- [ ] `/readyz` червоний, якщо БД недоступна або схема не на `head`.
- [ ] Репозиторні контракти доступні для task-05/06/07 (імпортуються, типізовані).
- [ ] Immutable-таблиці не оновлюються; unique-констрейнти покриті тестами.
- [ ] У схемі немає полів під секрети.
- [ ] WAL + foreign_keys + busy_timeout вмикаються на кожному з'єднанні.

## Ризики
- SQLite write-contention → тримати транзакції короткими, один worker-writer (PLAN §5.1). Не робити довгих транзакцій навколо мережевих викликів.
- Async(API) + sync(worker) над одним файлом — узгодити PRAGMA й таймаути; не тримати відкриту транзакцію під час browser-збору (це робота адаптера/worker, але схема має це дозволяти — persist окремою короткою транзакцією).
