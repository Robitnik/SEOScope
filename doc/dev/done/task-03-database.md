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
- `app/db/models.py` — усі таблиці §6: `clients`, `client_integrations`, `sync_jobs`, **`sync_job_providers`** (клітинки матриці збору `(client, provider)`, §9.1), `sync_job_steps`, `job_events`, `provider_snapshots`, `metric_values`, `artifacts`, `imported_sources`.
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
JobRepository:         create_run(clients, providers, period, mode, idempotency_key) -> розкладає на клітинки,
                       aggregate_status(job|batch з клітинок), get, list(status, client)
CellRepository:        claim_next_cell(provider, lease_owner, lease_ttl)  # атомарно, для однієї доріжки
                       heartbeat, set_status, set_progress, expired_cells, failed_cells(job|batch),
                       acquire_profile_lock(provider, profile_name), release_profile_lock(...)
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
- `sync_job_providers`: `claim_next_cell(provider)` — атомарний claim однієї клітинки для доріжки (коротка транзакція + per-cell lease); lock `(provider, profile_name)` не дає активувати той самий профіль двічі; статус job/batch — агрегат клітинок, де `not_configured`/`skipped` ≠ провал (PLAN §9.1/§9.2).
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
- [x] `alembic upgrade head` створює всю схему §6 (разом із `sync_job_providers`); `downgrade` чистий.
- [x] `claim_next_cell(provider)` атомарний і поважає profile-lock `(provider, profile_name)` (тест на конкурентність).
- [x] `/readyz` червоний, якщо БД недоступна або схема не на `head`.
- [x] Репозиторні контракти доступні для task-05/06/07 (імпортуються, типізовані).
- [x] Immutable-таблиці не оновлюються; unique-констрейнти покриті тестами.
- [x] У схемі немає полів під секрети.
- [x] WAL + foreign_keys + busy_timeout вмикаються на кожному з'єднанні.

## Ризики
- SQLite write-contention → тримати транзакції короткими, один worker-writer (PLAN §5.1). Не робити довгих транзакцій навколо мережевих викликів.
- Async(API) + sync(worker) над одним файлом — узгодити PRAGMA й таймаути; не тримати відкриту транзакцію під час browser-збору (це робота адаптера/worker, але схема має це дозволяти — persist окремою короткою транзакцією).

---

## ✅ Completion report

- **Дата:** 2026-07-22
- **Гілка / комміт:** main (не закомічено)
- **Статус:** done

### Що зроблено
Уся модель даних PLAN §6 реалізована на SQLAlchemy 2.x ORM + Alembic, з повним repository-шаром, через який працюють і API (async), і worker (sync):
- `app/db/base.py` — Declarative base з naming convention для стабільних імен констрейнтів; портативні типи `GUID` (text UUID), `UTCDateTime` (timezone-aware, відхиляє naive datetime), `DecimalString` (точний Decimal без binary-float обрізання).
- `app/db/session.py` — async engine (`aiosqlite`) для API, sync engine (`pysqlite`) для worker/Alembic, обидва з однаковим PRAGMA-хуком (`WAL`, `foreign_keys=ON`, `busy_timeout=30000`) на connect; `get_db()` FastAPI-залежність; `worker_session_scope()` context manager.
- `app/db/models.py` — усі 10 таблиць §6 (`clients`, `client_integrations`, `sync_jobs`, `sync_job_providers`, `sync_job_steps`, `job_events`, `provider_snapshots`, `metric_values`, `artifacts`, `imported_sources`) з PK/FK/unique/індексами; `provider_snapshots`/`metric_values` мають ORM-guard (`before_update` → `ValueError`), що додатково до repository-рівня забороняє мутацію після commit.
- `app/db/repositories/` — по одному файлу на агрегат: `ClientRepository`, `JobRepository`, `CellRepository`, `StepRepository`, `EventRepository`, `SnapshotRepository`, `MetricRepository`, `ArtifactRepository`, `ImportRepository`. Жодного сирого SQL вище цього шару.
- `migrations/` (Alembic env, кастомізований `script.py.mako`), `alembic.ini` — перша міграція `db1407dec7b0_initial_schema.py` створює всю схему; перевірено `upgrade head` → `downgrade base` → `upgrade head` на чистій БД.
- `/readyz` (`app/api/health.py`, доповнення дозволене планом) тепер реально перевіряє `SELECT 1` + `alembic_version.version_num == script.get_current_head()` через новий `app/db/health.py`; повертає `503` замість `200`, якщо БД недоступна або схема не на head.

### Як зроблено (ключові рішення)
- **Repository-шар написаний проти sync `Session`, а не дубльований sync/async.** API викликає ті самі методи через `AsyncSession.run_sync(...)` (задокументовано в `app/db/repositories/__init__.py`) — офіційний SQLAlchemy-патерн для перевикористання sync ORM-коду з async сесії. Це прибирає ризик розбіжності бізнес-логіки між API та worker, які PLAN явно вимагає тримати консистентними.
- **`profile_key` обчислюється при створенні клітинки** (`JobRepository._profile_key`), з `client_integrations.config_json["profile_name"]`; якщо профіль не налаштовано (пр. чисто API-провайдер), `profile_key=None` і lock не застосовується. `CellRepository.claim_next_cell` атомарний через conditional `UPDATE ... WHERE id=:id AND status='queued'` (перевірка `rowcount==1`) — SQLite не має `SELECT FOR UPDATE SKIP LOCKED`, але єдиний writer + коротка транзакція дають ту саму гарантію.
- **`JobRepository.aggregate_status`** явно виключає `not_configured`/`skipped` з підрахунку провалів (PLAN §9.2): `succeeded`, якщо всі *спробувані* клітинки успішні; `partial`, якщо суміш; `failed`, лише якщо всі спроби провалились.
- **Alembic autogenerate не імпортує кастомні типи автоматично** — `app.db.base.GUID`/`UTCDateTime`/`DecimalString` рендеряться повним шляхом, але без `import app.db.base`. Виправлено додаванням цього імпорту в `script.py.mako` (всі майбутні міграції) і в уже згенеровану міграцію.
- **`ruff format` увімкнено як Alembic `post_write_hook`** (`alembic.ini`), бо автогенеровані міграції з багатьма колонками систематично перевищують `line-length=100`; `ruff check --fix` тут не рятує (E501 не автофіксується), а форматування — рятує.
- **`database_ready()` приймає опційний `engine`** параметр (за замовчуванням — глобальний singleton) саме для того, щоб юніт-тести могли перевірити і "не готово", і "готово" без мутації глобального стану процесу між тестами.

### Контрактні зміни (Contract changes)
- `app/api/health.py`: `/readyz` більше не завжди повертає `200`. Це саме та зміна, яку task-01 залишив як `TODO(task-03)`; існуючий `tests/unit/api/test_health.py::test_readyz_ok` лишається зеленим лише тому, що канонічний dev-workflow (`docker compose run/exec`) прогонює `alembic upgrade head` в entrypoint **перед** pytest — тобто БД уже на head, коли тест звертається до `/readyz`. Якщо колись pytest запускатиметься way, що обходить entrypoint, цей тест почне падати доти, доки БД не мігрована.
- Додано поле `sync_jobs.mode` (`"collect_missing" | "force_refresh"`), якого немає в explicit-переліку §6 і немає в `JobDTO` (task-02) — воно потрібне worker'у (task-05), щоб знати, чи можна пропустити клітинку зі свіжим snapshot, без повторного проходу через API. DTO свідомо не чіпали.

### Як перевірити (verification)
```bash
docker compose build api
docker compose run --rm api poetry run alembic upgrade head
docker compose run --rm api poetry run alembic downgrade base
docker compose run --rm api poetry run alembic upgrade head
docker compose run --rm api poetry run ruff check app/db migrations tests/unit/db tests/unit/api
docker compose run --rm api poetry run mypy app/db
docker compose run --rm api poetry run pytest -q
curl -f http://localhost:8000/readyz   # 200 після міграції; 503, якщо БД видалити/розсинхронізувати
```
На момент закриття: `ruff`/`mypy` — 0 помилок у власних owned paths (`app/db/**`, `migrations/**`); `pytest -q` — 122 passed (уключно з тестами інших уже закритих тасок).

### Тести
`tests/unit/db/` (новий, повністю мій):
- `test_pragma.py` — WAL/foreign_keys/busy_timeout застосовані на кожному connect.
- `test_constraints.py` — unique (`slug`, `domain`, `(client_id, provider)`), FK enforcement, cascade delete.
- `test_immutability.py` — update `ProviderSnapshot`/`MetricValue` після commit кидає `ValueError`.
- `test_none_vs_zero.py` — `value_numeric=None` лишається `None`, не `0`; Decimal-точність зберігається.
- `test_jobs_and_cells.py` — декомпозиція `create_run` у клітинки; атомарний `claim_next_cell` між двома сесіями; expired-lease виявлення; profile-lock блокує паралельний claim того самого профілю; `aggregate_status` ігнорує `not_configured`/`skipped`, дає `partial` при суміші.
- `test_events.py` — монотонний `seq`; `since` фільтрація; `purge_older_than`.
- `test_snapshots_and_metrics.py` — `latest_success` (порядок, виключення `unavailable`); `bulk_insert` зберігає `None`/Decimal; `history` ліміт.
- `test_artifacts_and_imports.py` — path-traversal guard на write і read; ідемпотентність `ImportRepository`.
- `test_clients.py` — CRUD, `list_active` виключає disabled, upsert інтеграції.
- `test_alembic_migrations.py`, `test_db_health.py` — `upgrade head` → `downgrade base` → `upgrade head` на чистій тимчасовій БД; `database_ready()` false/true до/після міграції.

### Follow-ups / борг
- `app/db/repositories/cells.py` і `app/db/repositories/jobs.py` отримали додаткові методи (`get`, `list_for_job`, `requeue_for_retry`, `request_cancel` на обох) від паралельного агента task-05 (job engine), позначені коментарем `# --- Added by task-05` у самих файлах — це нормальне розширення репозиторного контракту, яке я перевірив (ruff/mypy/pytest зелені) у межах свого DoD-проходу, але не рецензував на предмет бізнес-коректності job-engine логіки, яка їх викликає.
- Під час роботи виявлено, що `doc/dev/new/task-01-foundation-infra.md` відсутній на диску і не з'явився в `doc/dev/done/` — файл, судячи з git-історії, мав там бути. Це не входить у owned paths task-03; залишено як є, повідомлено користувачу окремо.
- Не закомічено. Комміт — на розсуд користувача (harness-правило "commit лише на явний запит").
