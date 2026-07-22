# Definition of Done — MVP

Чекліст із [PLAN.md §18](../PLAN.md), звірений task-13. Кожен пункт має
**доказ**: тест, який його ловить, або команда, якою це перевіряється руками.
Пункт, який не можна перевірити автоматично, чесно позначений як ручний і не
вважається закритим, доки людина його не пройшла.

Статус на **2026-07-22** (гілка `feat/task-12-frontend`).

| Стан | Значення |
|---|---|
| ✅ | закрито, є автоматичний доказ |
| 🟡 | закрито частково / потребує ручного проходу перед release |
| ⬜ | не закрито |

---

## Дані й дашборд

- ✅ **Усі активні клієнти — картками з logo/name/domain.**
  `GET /api/v1/dashboard` віддає 14 клієнтів із `logo_path`, `name`, `domain`;
  рендер картки — `tests/e2e/frontend/frontend-smoke.cjs` (перевіряє кількість
  карток, пошук, групи портфеля).

- ✅ **`Тиждень/Місяць` міняє точні current/previous ranges.**
  `tests/integration/test_snapshots_and_dashboard.py::test_dashboard_distinguishes_not_configured_failed_and_collected`
  (діапазони не перетинаються й впорядковані) + юніти періодів
  `tests/unit/metrics/test_periods.py`; у UI — асерт на `#period-copy` у smoke.

- ✅ **4 provider sections: KPI, delta, quality, freshness.**
  `tests/unit/api/test_dashboard.py` (склад DTO, delta, `better_direction`),
  `tests/unit/metrics/test_quality.py` (freshness/stale).

- ✅ **`null` / zero / stale / partial / truncated / failed — різні.**
  `test_dashboard_distinguishes_not_configured_failed_and_collected` вимагає
  чотири **різні** стани в одній картці; `tests/unit/db/test_none_vs_zero.py`
  і `test_derived_ctr_and_missing_values_survive_the_round_trip` стежать, що
  відсутнє значення ніколи не стає нулем.

## Збір

- ✅ **Sync одного клієнта / вибраних провайдерів із UI.**
  `POST /api/v1/sync-jobs` → worker → snapshot:
  `tests/integration/test_cancel_retry_partial.py`; у UI — sync-діалог у smoke.

- ✅ **Live progress переживає reload/reconnect; закриття modal не спиняє job.**
  SSE-replay через `Last-Event-ID`:
  `tests/integration/test_sse_replay.py::test_stream_replays_only_events_after_last_event_id`.
  Закриття діалогу лишає job у доці — асерт `#job-dock` у smoke.

- ✅ **Partial зберігає успішні + retry тільки failed.**
  `test_partial_run_keeps_successes_and_retry_touches_only_failures`: успішна
  клітинка лишається `attempt=1` і не перезбирається.

- ✅ **Сервіси паралельно, клієнти всередині сервісу послідовно.**
  `test_services_run_in_parallel_while_clients_run_sequentially` — доводиться
  перетином/неперетином інтервалів `started_at…finished_at`, без таймінг-порогів.

- ✅ **Новий клієнт без інтеграції → `not_configured`, не краш і не нуль.**
  `test_client_without_integration_is_not_configured_not_failed` +
  `test_disabled_integration_is_not_configured_not_a_failure`.

- ✅ **Помилка одного сервісу не валить запуск; прогрес переживає рестарт.**
  `test_one_failed_cell_never_touches_its_neighbours`,
  `test_hung_adapter_times_out_without_blocking_the_lane`,
  `tests/integration/test_recovery_and_restart.py` (3 сценарії).

- ✅ **Restart API/worker не псує БД і не лишає job «вічно running».**
  `test_interrupted_cell_is_requeued_and_finishes_after_restart`,
  `test_restart_leaves_no_cell_running_forever`.

## Сумісність і безпека

- ✅ **Старі AISeoTools CLI/tests не зламані.**
  `docker compose exec api bash scripts/aiseotools_regression.sh` →
  **1492 passed, 3 skipped** (прогін 2026-07-22). Плюс дешевий гейт сигнатур
  `tests/contract/` (51 символ під охороною).

- ✅ **У БД немає API keys / passwords / session cookies.**
  `tests/integration/test_security_hardening.py`: «протікаючий» адаптер +
  скан усієї БД на canary-значення; окремо — скан закомічених фікстур і
  `.env.example`.

- ✅ **Artifact download не виходить за artifacts root.**
  `test_artifact_download_refuses_paths_outside_the_root`.

## Експлуатація

- ✅ **`docker compose up --build` відтворює систему за README.**
  Стек піднімається й обидва сервіси `healthy`; `docker/entrypoint.sh` сам
  проганяє `alembic upgrade head`.

- ✅ **Є backup/restore інструкція.**
  [backup-restore.md](backup-restore.md); повний цикл backup → втрата БД →
  restore покритий `tests/integration/test_ops_scripts.py`.

- ✅ **Runbook для login/session refresh і provider failures.**
  [runbook.md](runbook.md) §3.

## Ручні пункти

- 🟡 **Keyboard navigation, responsive, contrast.**
  Автоматично: фокус повертається на opener після `Escape`, немає
  горизонтального переповнення на 390/768/1024/1440/1920, `reducedMotion`
  увімкнено в smoke. **Руками лишається контраст** (жоден автотест його не
  міряє) — пройти перед release.

- ⬜ **Live totals контрольного клієнта звірені вручну з кожним provider UI.**
  Інструмент готовий:
  `docker compose exec api poetry run python scripts/live_smoke.py --provider <p> --client <domain> --yes-spend-units --collect --report /app/app_data/live-smoke.json`.
  Витрачає units і потребує людини, тож **не** може бути закритий автоматично.
  Це єдиний пункт DoD, який лишається відкритим.

---

## Як перевірити все одним заходом

```bash
docker compose up -d --build
docker compose exec api poetry run ruff check . && docker compose exec api poetry run mypy app
docker compose exec api poetry run pytest -q                 # unit + integration + contract
docker compose exec api bash scripts/aiseotools_regression.sh
bash tests/e2e/run_frontend_smoke.sh                          # потребує node+chromium на хості
```

Поточний результат: `ruff` і `mypy` чисті, **390 passed, 1 skipped (live),
1 xfailed**; стара suite AISeoTools — 1492 passed; frontend smoke — ok.

`xfail` — це знайдена, але не власна вада: скасування клітинки, яка ще
`queued`, лишає її в `cancel_requested` до рестарту worker-а (див.
[runbook §3.3](runbook.md); follow-up до task-05).
