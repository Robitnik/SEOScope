# task-05 — Job Engine (worker, черга, lease/recovery, events, SSE)

- **Пакет:** durable DB-черга, один worker, lease/heartbeat/recovery, steps/events, SSE-потік, cancel/retry/idempotency, fake adapter
- **Хвиля:** B
- **Depends on:** task-02 (adapter contract, registry, ProgressEvent), task-03 (репозиторії jobs/steps/events/snapshots)
- **Blocks:** провайдер-таски (їх adapter ганяє саме worker), task-06 (SSE endpoint читає events), task-12 (live progress)
- **PLAN.md:** §5.1 (чому окремий worker), §9 (job flow + SSE), §6 (sync_jobs/steps/events), Етап 3

## Мета

Надійний виконавчий рушій: API лише кладе job у БД, а **один** worker атомарно claim-ить її, проганяє провайдери через `ProviderAdapter`, пише прогрес у `job_events`, витримує restart через lease/heartbeat/recovery, підтримує cancel/retry/idempotency. Плюс **fake adapter** для детермінованих E2E без реальних провайдерів.

## Обсяг

**Входить:**
- `app/jobs/worker.py` — цикл: `claim_next` → lease → runner → heartbeat → фініш; один процес, один writer.
- `app/jobs/runner.py` — на кожен provider: `preflight → collect → normalize(hook у task-04) → validate(quality gates) → atomic persist snapshot+metrics`. Partial: один провайдер впав → інші успішні зберігаються, job = `partial`.
- `app/jobs/events.py` — emit-функція, що конвертує `ProgressEvent` (task-02) у запис `job_events` + heartbeat; redaction перед записом.
- `app/jobs/recovery.py` — на старті: expired leases → safe-retry stage або `failed_interrupted`; ідемпотентні стадії.
- SSE-**джерело**: генератор подій `events since last_seq` + keepalive (сам HTTP-endpoint `GET /sync-jobs/{id}/events` монтує task-06, але логіку стріму/replay бере звідси).
- `app/jobs/fake_adapter.py` — детермінований `ProviderAdapter` для E2E (керовані стадії, штучні warning/fail/partial, cancel-точки).
- Cooperative cancel: `is_cancel_requested()` на межах stage; активний HTTP/download не вбивається посеред запису.
- Idempotency: `idempotency_key` не створює дубль job; retry створює нову attempt/job лише для failed-провайдерів із посиланням на parent.

**НЕ входить:** самі провайдер-адаптери (09–11 — worker працює через registry), REST-роутери (task-06), нормалізаційна математика (task-04 — лише виклик).

## Owned paths
```
app/jobs/worker.py
app/jobs/runner.py
app/jobs/events.py
app/jobs/recovery.py
app/jobs/fake_adapter.py
app/jobs/__init__.py
```

## Consumes
- `ProviderAdapter`, `get_adapter`, `ProgressEvent`, `CollectionContext/Result`, enums — task-02.
- `JobRepository/StepRepository/EventRepository/SnapshotRepository/MetricRepository` — task-03.
- `normalize_result`, `assess_quality`, `resolve_periods` — task-04.

## Provides
- Запущений worker (`python -m app.jobs.worker`) як окрема compose-команда (координація з task-01 image).
- SSE stream helper: `event_stream(job_id, last_event_id) -> AsyncIterator[bytes]` (text/event-stream, keepalive) для task-06.
- Контракт стадій і статусів job/step (узгоджений з task-02 enums) — фронт (task-12) на нього спирається.
- Fake adapter, зареєстрований під тест-профілем, для E2E (task-13).

## Кроки реалізації
1. `claim_next` + lease (атомарна коротка транзакція, task-03 репозиторій).
2. Runner: послідовність стадій PLAN §9; atomic persist окремою короткою транзакцією (не тримати транзакцію під час мережевого/browser-збору).
3. events + heartbeat + redaction.
4. recovery на старті.
5. cancel/retry/idempotency.
6. SSE stream helper з replay за `Last-Event-ID`/`seq`.
7. fake adapter + сценарії (success/partial/fail/cancel/slow).

## Тести (PLAN §15 integration)
- Атомарний claim: конкурентні claim не беруть одну job.
- idempotency: повтор із тим самим ключем не дублює job.
- lease recovery: «завислу» running job після expiry повертає у queue або `failed_interrupted`.
- cancel кооперативний: зупинка на межі stage, без пошкодження часткового запису.
- retry: нова attempt лише для failed-провайдерів, посилання на parent.
- partial: успішні провайдери збережені, job=`partial`.
- SSE replay: підключення з `Last-Event-ID` віддає лише `seq > last`.
- restart worker під час job → recovery коректний (немає «вічної» running).

## Definition of Done / Gate
- [ ] Restart API не губить job/progress; restart worker відновлює або коректно завершує interrupted job (PLAN Етап 3 gate).
- [ ] Один worker = один writer; транзакції короткі, browser-збір поза транзакцією.
- [ ] Events монотонні; SSE replay детермінований.
- [ ] cancel/retry/idempotency покриті тестами.
- [ ] fake adapter дає повністю детермінований E2E-прогін (task-13/12 користуються).
- [ ] У events/логах немає секретів.

## Ризики
- Дві репліки worker на SQLite — заборонено (PLAN §14). Масштаб → PostgreSQL+Redis за adapter boundary.
- Browser profile lock `(provider, profile_name)`: worker не запускає паралельно один профіль (координація з task-11/AISeoTools).
- Не парсити CLI stdout/логи для прогресу — лише структурований `ProgressEvent` (PLAN §2.3.6).
