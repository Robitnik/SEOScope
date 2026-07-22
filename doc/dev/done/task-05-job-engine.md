# task-05 — Job Engine (dispatcher, per-provider lanes, cell isolation, SSE)

- **Пакет:** durable DB-черга, один worker-процес із **паралельними per-provider доріжками**, планування клітинок `(client, provider)`, lease/heartbeat/recovery, cell-ізоляція та «нуль падінь», безперервне збереження прогресу, steps/events, SSE, cancel/retry/idempotency, fake adapter
- **Хвиля:** B
- **Depends on:** task-02 (adapter contract, registry, ProgressEvent, CellStatus), task-03 (репозиторії jobs/cells/steps/events/snapshots)
- **Blocks:** провайдер-таски (їх adapter ганяє саме worker), task-06 (SSE + batch API), task-12 (live progress)
- **PLAN.md:** §5.1 (один процес, паралельні доріжки), **§9.1 (матриця клієнт × сервіс), §9.2 (стійкість + нові клієнти), §9.3 (життєвий цикл)**, §6 (`sync_jobs`/`sync_job_providers`/`steps`/`events`), Етап 3

## Мета

Надійний виконавчий рушій, що реалізує модель збору з PLAN §9: **між сервісами — паралельно, всередині сервісу — послідовно**. API лише кладе клітинки у БД; один worker-процес диспетчеризує їх по доріжках (одна доріжка на сервіс), витримує restart через lease/heartbeat/recovery, **не падає ніколи** (ізоляція клітинки + оборона в глибину) і безперервно зберігає прогрес. Плюс fake adapter для детермінованих E2E.

## Модель виконання (обов'язкова до реалізації — PLAN §9.1/§9.2)

- **Клітинка** `cell = (client, provider)` — атомарна одиниця планування/прогресу/retry (рядок `sync_job_providers`, task-03).
- **Диспетчер** тримає **одну доріжку (lane) на сервіс** (Ahrefs, Semrush, GSC, Reporting Ninja). Доріжки виконуються **одночасно**.
- **Усередині доріжки — послідовно**: наступна `queued`-клітинка береться лише після завершення попередньої. Причина — persistent browser-профіль/сесія + metered API; lock `(provider, profile_name)` (PLAN §8.3). Один активний збір на сервіс максимум.
- Реалізація: **один процес-writer**, всередині — async tasks/worker-threads, по одній на профіль сервісу; `max concurrency = кількість профілів`. Мережевий/browser-збір — **поза транзакціями**; у БД пише лише короткий atomic-persist (SQLite «один writer» збережено).
- **Ізоляція та нуль падінь:** кожна клітинка обгорнута в guard, що конвертує будь-який виняток/timeout у записаний `failed` (безпечний `error_code`, без секретів). Per-cell і per-stage timeouts. Диспетчер супервізорить доріжки: впала задача доріжки → перезапуск, in-flight клітинка → recovery через lease. Падіння однієї клітинки ніколи не валить доріжку/сервіс/запуск.
- **Нові клієнти:** preflight по `client_integrations`; немає/вимкнена/бракує ідентифікаторів → клітинка одразу `not_configured` (термінальний, не `failed`, не нуль, збір не стартує). Розрізняти `not_configured` / `auth_required` / `unavailable` / `failed`.
- **Прогрес персистентний:** події `job_events` (монотонний `seq`) + статус/прогрес клітинки пишуться після кожної значущої дії. Snapshot immutable — per-cell на успіху. Краш → втрата максимум однієї незакомієної клітинки.

## Обсяг

**Входить:**
- `app/jobs/dispatcher.py` — планувальник: розкладає запит на клітинки, тримає доріжки на сервіс, супервізорить/перезапускає доріжки, поважає profile-lock; агрегує статус job/batch із клітинок.
- `app/jobs/lane.py` — одна доріжка сервісу: цикл claim-next-cell → run-cell → release-profile; per-cell timeout.
- `app/jobs/worker.py` — процес-обгортка: старт recovery, запуск диспетчера, graceful shutdown; один writer.
- `app/jobs/runner.py` — виконання однієї клітинки: preflight → (not_configured?) → collect → normalize(hook task-04) → validate(quality gates) → atomic persist snapshot+metrics; **guard навколо всього** (нуль падінь).
- `app/jobs/events.py` — emit `ProgressEvent` (task-02) → запис `job_events` + heartbeat; redaction перед записом.
- `app/jobs/recovery.py` — на старті: expired leases (job- і cell-рівень) → ідемпотентний safe-retry або `failed_interrupted`.
- SSE-**джерело**: генератор `events since last_seq` + keepalive (HTTP-endpoint монтує task-06).
- `app/jobs/fake_adapter.py` — детермінований `ProviderAdapter` для E2E: керовані стадії, штучні warning/fail/partial/timeout/not_configured/cancel-точки, штучна тривалість для перевірки паралельності доріжок.
- Cancel кооперативний (межі stage), retry failed-only на рівні клітинок, idempotency.

**НЕ входить:** самі провайдер-адаптери (09–11 — worker працює через registry), REST-роутери/batch endpoint (task-06 — але контракт узгодити тут), нормалізаційна математика (task-04 — виклик), схема БД (task-03 — використання).

## Owned paths
```
app/jobs/dispatcher.py
app/jobs/lane.py
app/jobs/worker.py
app/jobs/runner.py
app/jobs/events.py
app/jobs/recovery.py
app/jobs/fake_adapter.py
app/jobs/__init__.py
```

## Consumes
- `ProviderAdapter`, `get_adapter`, `ProgressEvent`, `CollectionContext/Result`, `PreflightResult`, `CellStatus`, enums — task-02.
- `JobRepository`, `CellRepository`(`sync_job_providers`), `StepRepository`, `EventRepository`, `SnapshotRepository`, `MetricRepository` — task-03 (потрібні `claim_next_cell(provider, lease)`, per-cell lease/heartbeat, expired-cells).
- `normalize_result`, `assess_quality`, `resolve_periods` — task-04.

## Provides
- Запущений worker (`python -m app.jobs.worker`) як окрема compose-команда (координація з task-01 image).
- SSE stream helper: `event_stream(job_id|batch_id, last_event_id) -> AsyncIterator[bytes]` для task-06.
- Контракт клітинок/статусів/стадій (узгоджений з task-02) — фронт (task-12) рендерить матрицю прогресу по ньому.
- Fake adapter під тест-профілем для E2E (task-13).

## Кроки реалізації
1. Cell-claim + lease на рівні `sync_job_providers` (атомарна коротка транзакція, task-03), фільтр по `provider` для доріжки.
2. Lane-цикл + dispatcher з однією доріжкою на профіль сервісу; профіль-lock; паралельність доріжок.
3. runner з **повним guard** (будь-який виняток → `failed` cell) + per-cell/stage timeout; preflight→`not_configured` шлях.
4. events + heartbeat + redaction; безперервний persist прогресу.
5. recovery (job+cell leases) на старті; агрегація статусу job/batch із клітинок (`not_configured`/`skipped` ≠ провал).
6. cancel/retry-failed-only(cell)/idempotency.
7. SSE stream helper з replay за `Last-Event-ID`/`seq`.
8. fake adapter + сценарії (success/partial/fail/timeout/not_configured/cancel/slow) для перевірки доріжок.

## Тести (PLAN §15 integration + §9)
- **Паралельність/послідовність:** 4 доріжки збирають одночасно; усередині доріжки клітинки строго послідовні; той самий `(provider, profile)` ніколи не активний двічі одночасно.
- **Нуль падінь:** adapter кидає виняток/таймаутить/повертає сміття → клітинка `failed`, доріжка й решта запуску тривають; процес не падає.
- **Новий клієнт:** інтеграції нема/вимкнена → клітинка `not_configured`, збір не стартує, запуск не `failed`.
- **Прогрес переживає рестарт:** kill процесу посеред batch → recovery: done лишаються, перервана → retry/`failed_interrupted`, pending тривають; немає «вічних» running.
- Атомарний cell-claim (конкурентні claim не беруть одну клітинку); idempotency (повтор ключа не дублює); retry failed-only не чіпає успішні клітинки; partial → job/batch `partial`; SSE replay за `Last-Event-ID`.
- У events/логах немає секретів.

## Definition of Done / Gate
- [ ] Збір **паралельний між сервісами, послідовний усередині сервісу**; profile-lock дотриманий (жодного паралельного запуску одного профілю).
- [ ] **Жодне падіння** клітинки/адаптера не валить доріжку, інші сервіси чи запуск; процес не крашиться.
- [ ] `not_configured` для нового клієнта обробляється штатно (не краш, не нуль, не `failed`); стани `not_configured/auth_required/unavailable/failed` різні.
- [ ] Прогрес зберігається безперервно й переживає restart API/worker; recovery не лишає «вічних» running (PLAN Етап 3 gate).
- [ ] retry failed-only працює на рівні клітинок; idempotency дотримана.
- [ ] Один процес = один writer; browser-збір поза транзакціями; persist короткий.
- [ ] fake adapter дає детермінований E2E (task-12/13 користуються), у т.ч. перевірку паралельності доріжок.

## Ризики
- Дві репліки worker на SQLite — заборонено (PLAN §14). Паралельність — усередині одного процесу (доріжки). Масштаб → PostgreSQL+Redis за adapter boundary.
- Playwright у кількох доріжках одночасно: різні профілі/контексти на сервіс; **ніколи** не ділити один профіль між доріжками (координація з task-11/AISeoTools).
- Не парсити CLI stdout/логи для прогресу — лише структурований `ProgressEvent` (PLAN §2.3.6).
- Довга транзакція навколо browser-збору → lock errors; тримати збір поза транзакцією, persist окремо й коротко.

---

## ✅ Completion report

- **Дата:** 2026-07-22
- **Гілка / комміт:** main (не закомічено)
- **Статус:** done-with-followups

### Що зроблено
Повний job engine на потоках одного процесу (PLAN §5.1/§9), паралельний між сервісами, послідовний усередині сервісу, з нульовими падіннями:

- `app/jobs/dispatcher.py` — `Dispatcher`: один `threading.Thread` на кожен `Provider` (4 доріжки), супервізор-цикл (`run_forever`) перезапускає доріжку, чий потік завершився з винятком; `stop()` — кооперативне graceful shutdown.
- `app/jobs/lane.py` — `run_lane`: цикл `claim_next_cell(provider) -> run_cell -> repeat`, строго послідовний у межах доріжки (наступний claim лише після завершення `run_cell`).
- `app/jobs/runner.py` — `run_cell`: preflight (по `client_integrations`, до виклику adapter.preflight) → `not_configured`? → adapter preflight → freshness-skip (`collect_missing`) → `collect()` під daemon-thread з wall-clock timeout → `normalize_result` (task-04) → легка структурна валідація (warn, ніколи не дропає) → короткий atomic persist (`ProviderSnapshot` + `MetricValue*`) → `CellRepository.set_status` + `JobRepository.sync_status_from_cells`. Уся послідовність обгорнута зовнішнім `try/except Exception`, що гарантовано конвертує будь-що (включно з падінням самого persist-кроку) у записаний `failed` без падіння процесу.
- `app/jobs/events.py` — `make_emitter` (redaction ключів/патернів secrets → `job_events` + heartbeat/progress, best-effort — винятки ковтаються); `event_stream` — async SSE-генератор (polling + keepalive + `Last-Event-ID` replay), навмисно відв'язаний від конкретної сесії (приймає `fetch_since` callable) для легкого монтування в task-06.
- `app/jobs/recovery.py` — `run_startup_recovery`: `expired_cells()` → `failed_interrupted` (аудит-подія) → негайний auto-requeue (безпечно, бо persist атомарний і відбувається лише в самому кінці `run_cell`); осиротілі `cancel_requested` з попереднього процесу → фіналізуються `cancelled`.
- `app/jobs/worker.py` — entrypoint `python -m app.jobs.worker`: recovery → `Dispatcher.run_forever()`; `SIGTERM`/`SIGINT` → `dispatcher.stop()` (перевірено вручну — `docker compose stop worker` завершується миттєво, без зависання).
- `app/jobs/control.py` — `cancel_run`/`retry_failed_only`: тонка оркестрація над `CellRepository`/`JobRepository` для майбутніх `POST /sync-jobs/{id}/cancel|retry` (task-06).
- `app/jobs/fake_adapter.py` — `FakeAdapter` + `CancelledByAdapter`: сценарії `success/partial/unavailable/fail/timeout/not_configured/cancel/slow`, керовані `integration_config["scenario"]`/`stage_seconds"`.

### Як зроблено (ключові рішення)
- **Доріжки — окремі OS-потоки, не asyncio.** `run_cell` блокуючий (мережа/browser/`time.sleep` у fake adapter звільняють GIL), тож звичайний `threading.Thread` на доріжку дає справжню паралельність без асинхронного рефакторингу решти проєкту. Перевірено тестом `test_different_providers_run_concurrently` (реальні `time.monotonic()`-вікна двох доріжок перекриваються) і `test_same_provider_two_clients_never_overlap...` (у межах однієї доріжки — ні).
- **Per-cell timeout через daemon-thread, не `ThreadPoolExecutor`.** Перша версія використовувала `with ThreadPoolExecutor(...)`, але `__exit__`/`shutdown()` **чекає** на завершення воркер-потоків — це звело б timeout нанівець (weit на весь час "зависання", а не обмежений таймаут). Знайдено й виправлено до злиття: план собаки — `threading.Thread(daemon=True)` + `join(timeout=...)`; якщо потік ще живий після timeout — клітинка `failed`/`timeout`, потік-сирота лишається (daemon, не блокує вихід процесу). Це задокументований компроміс: Python не має safe preemptive thread-cancel; справжній per-*stage* timeout вимагав би deadline-поля в `CollectionContext` (контракт task-02) або self-policing на боці адаптера (task-09/10/11) — поза межами цієї таски.
- **`not_configured` — єдиний "cell-level" бакет для всіх preflight-fail причин** (`not_configured`/`auth_required`/`no_profile`/...): сам `CellStatus` enum (task-02, read-only) не розрізняє їх окремими значеннями — нюанс несе `error_code` (= `PreflightResult.reason_code`). Task-06 очікувано мапить `(CellStatus, error_code, snapshot.quality)` у більш деталізований `ProviderCardState` для UI.
- **`Quality.unavailable` → `CellStatus.partial`, не `succeeded`/`failed`.** Дані по факту немає (адаптер спробував, провайдер порожній), але спроба відбулась і не впала — не `failed`. Water line для "успіху" в проєкті вже проведена в `SnapshotRepository._SUCCESS_QUALITIES` (виключає `unavailable`), тож `partial` — послідовний з цим вибір, а не `succeeded`, який замаскував би відсутність даних.
- **Freshness-skip (`collect_missing`) — простий евристичний critereon**, а не точне порівняння покриття періоду: `latest_success(...).data_through (or observed_at.date()) >= context.current.end`. Обране свідомо просто для MVP; якщо task-04/06 захочуть точнішу семантику (наприклад звірку по `metric_values.period_end` для кожного ключа), це можна замінити без зміни контракту `run_cell`.
- **`sync_jobs.status`/`progress_percent` персистяться, не лише обчислюються.** `JobRepository.aggregate_status` (task-03) лише рахує; ніхто це не писав назад у рядок `sync_jobs`, а `JobRepository.list(status=...)` фільтрує саме по цій колонці. Додано `JobRepository.sync_status_from_cells(job_id)`, що викликається після кожного переходу статусу клітинки.

### Контрактні зміни (Contract changes)
`app/db/repositories/**` належить task-03, але для job-engine знадобились примітиви, яких там не було. Додано (адитивно, без зміни існуючих сигнатур) і узгоджено з паралельним агентом task-03 (він же зафіксував це у власному completion report як прийняте розширення):
- `CellRepository.get(cell_id)`, `.list_for_job(job_id)`, `.requeue_for_retry(cell_id)`, `.request_cancel(*, job_id=None, batch_id=None)`, `.stale_cancel_requested_cells()`.
- `JobRepository.sync_status_from_cells(job_id)` (перса аргегат у `sync_jobs.status/progress_percent/current_stage/started_at/finished_at`), `.request_cancel(*, job_id=None, batch_id=None)`.

Немає змін у `app/integrations/contract.py`, `app/schemas/**` (task-02) чи `app/db/models.py`/`migrations/**` (схема) — лише нові методи в repository-класах task-03.

### Як перевірити (verification)
```bash
docker compose build api
docker compose run --rm api poetry run ruff check app/jobs app/db/repositories tests/unit/jobs
docker compose run --rm api poetry run mypy app/jobs app/db/repositories
docker compose run --rm api poetry run pytest -q                     # 148 passed
docker compose run --rm api poetry run pytest tests/unit/jobs -q     # 26 passed (this task's own suite)

# Live smoke test (as actually run during this task):
API_PORT=8010 docker compose up -d
docker compose exec api poetry run python -c "
from datetime import date
from app.db.session import worker_session_scope
from app.db.repositories.clients import ClientRepository
from app.db.repositories.jobs import JobRepository
from app.schemas.enums import Provider, PeriodKind, ComparisonMode
with worker_session_scope() as session:
    c = ClientRepository(session).create(name='Smoke', slug='smoke', domain='smoke.com', timezone='America/New_York')
    ClientRepository(session).upsert_integration(c.id, Provider.ahrefs, enabled=True, config={'scenario': 'success'})
    print(JobRepository(session).create_run(client_ids=[c.id], providers=[Provider.ahrefs], period_kind=PeriodKind.week, comparison_mode=ComparisonMode.closed, anchor_date=date(2026,7,20))[0].id)
"
docker compose logs worker   # dispatcher_started lanes=[...4 providers...]
# cell resolves to failed/adapter_not_registered (no real provider adapter exists yet — expected until task-09/10/11)
docker compose stop worker   # verify graceful shutdown does not hang
docker compose down
```

### Тести
`tests/unit/jobs/` (новий, 26 тестів, увесь мій):
- `test_runner.py` — success/partial/unavailable/adapter-exception/timeout/cancel/not_configured (missing+disabled integration+adapter reason_code)/unregistered-adapter/mixed-batch-partial.
- `test_recovery.py` — expired lease → `failed_interrupted` (аудит-подія) → auto-requeue; жодних вічних `running`; stale `cancel_requested` → `cancelled`; no-op, коли нічого не зависло.
- `test_control.py` — `cancel_run` не чіпає вже успішні клітинки; `retry_failed_only` requeue лише `failed`, лишає успішні.
- `test_job_events.py` — redaction message/details; emit ніколи не кидає навіть при зламаній `session_scope`; SSE `event_stream` — replay за `last_event_id`, keepalive при простої.
- `test_dispatcher_concurrency.py` — реальні паралельні доріжки (різні провайдери перекриваються в часі), послідовність у межах однієї доріжки (не перекриваються), доріжка переживає падіння свого потоку (супервізор перезапускає).

### Follow-ups / борг
- Справжній per-*stage* timeout (не лише per-cell) вимагає deadline-поля в `CollectionContext` — контрактна зміна task-02, поза межами цієї таски.
- Freshness-skip для `collect_missing` — спрощена евристика (`data_through`/`observed_at.date() >= current.end`); task-04/06 можуть захотіти точнішу перевірку по конкретних `metric_values.period_end`.
- `tests/unit/db/test_jobs_and_cells.py::test_claim_next_cell_is_atomic_across_two_callers` мав пропущений `session.commit()` між створенням клітинки й паралельним claim — побачено на старті цієї таски; виправлено паралельним агентом task-03 під час моєї роботи (не мій owned path, лише зафіксовано тут для трасування).
- Комміт — на розсуд користувача (harness-правило "commit лише на явний запит"); гілка лишається `main`, бо решта паралельних тасок так само працює прямо на `main` без комітів на момент закриття.
