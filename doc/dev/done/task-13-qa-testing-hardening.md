# task-13 — QA, Testing Harness & Hardening/Deploy

- **Пакет:** спільний тест-harness, integration/e2e, live smoke, screenshot regression, hardening, backup/retention, observability, runbook
- **Хвиля:** D (наскрізна, весь час; фінальне змикання перед release)
- **Depends on:** усі (інтегрує їх), але harness/фікстури готує рано, щоб інші користувалися
- **PLAN.md:** §15 (тестова стратегія — повністю), §13 (безпека), §7 (UX polish/hardening), §18 (Definition of Done MVP), Етап 7

## Мета

Забезпечити наскрізну якість і продакшн-готовність: спільний тест-harness і фікстури (щоб кожна таска писала свої тести однаково), integration/e2e-набори, що перетинають модулі, контрольовані live smoke, screenshot-регресію, а також hardening/deploy (auth reverse proxy, backup/restore, retention, observability, runbook). Фінально — звірити **Definition of Done MVP (PLAN §18)**.

> Розподіл: **юніт-тести свого модуля пише власник модуля** (task-01…12) у `tests/unit/<module>/`. Ця таска володіє **спільним harness**, sanitized-фікстурами, integration/e2e, live smoke, CI-гейтами та ops-hardening.

## Обсяг

**Входить:**
- `tests/` каркас: `conftest.py`, спільні фікстури, тимчасова SQLite, підняття FastAPI+worker у тестах, реєстрація fake adapter (task-05).
- **Sanitized fixtures** з наявних Ahrefs/GSC/Semrush/Ninja artifacts (PLAN Етап 0) — без секретів; спільні для бекенду й фронту (координація з task-12 `frontend/fixtures/`).
- Integration-набори (PLAN §15): atomic claim, idempotency, lease recovery, cancel/retry, partial run, SSE replay, immutable snapshot + latest-success, importer dry-run/idempotency/schema drift.
- Contract/regression AISeoTools (PLAN §15): прогін старої suite, signature snapshots (спільно з task-08 як CI-гейт).
- Frontend/E2E harness + screenshot regression ключових станів (запускає fl-и task-12).
- **Live smoke** (поза звичайною CI): по одному контрольному клієнту на провайдера; API unit budget + browser profile lock обов'язкові; totals звіряються вручну перед release.
- **Hardening/Deploy:** app login + secure HttpOnly cookie + CSRF + audit trail (перед public exposure); auth reverse proxy profile; security headers/CORS перевірка; artifact download ownership + path-traversal тест.
- **Ops:** SQLite backup/restore скрипти, events retention cleanup (30–90 днів, setting), observability (health/metrics/logs без raw client metrics у prod), README/runbook (login/session refresh, provider failures).
- CI-гейти: lint+type+pytest на кожен модуль; окремо помічені metered live tests (не в звичайній CI). **Усе виконання тестів і CI — всередині Docker-образу** (build image → кроки в контейнері), без хостового Python (CLAUDE.md «Середовище виконання»).

**НЕ входить:** реалізація самих модулів (лише їх перевірка/склеювання), правки owned paths інших тасок (координація, не редагування).

## Owned paths
```
tests/conftest.py, tests/unit/__shared__/**, tests/integration/**, tests/contract/**, tests/e2e/**
tests/fixtures/**              # sanitized, без секретів
scripts/backup_sqlite*, scripts/restore_sqlite*, scripts/retention_cleanup*
scripts/live_smoke*            # metered, поза CI
docs/runbook.md, docs/backup-restore.md, docs/security.md
.github/workflows/** (тест-гейти — координувати з task-01, який володіє базовим CI)
```

## Consumes
- Публічні контракти/ендпоінти/CLI усіх тасок; fake adapter (task-05); OpenAPI (task-06); frontend flows (task-12).

## Provides
- Спільні фікстури й harness для всіх (щоб таски писали тести уніфіковано).
- Зелені integration/e2e-гейти; backup/restore/retention; runbook; підтверджений DoD MVP.

## Кроки реалізації
1. **Рано:** conftest + sanitized fixtures + fake-adapter-harness → віддати іншим таскам.
2. Integration-набори в міру готовності модулів (jobs → api → провайдери).
3. Frontend/e2e + screenshot regression (з task-12).
4. Live smoke скрипти (budget/lock guard) — ручний прогін перед release.
5. Hardening: auth/login/CSRF/audit, headers/CORS, artifact ownership.
6. Ops: backup/restore, retention, observability, runbook.
7. Фінал: пройтись по PLAN §18 DoD MVP чеклісту й зафіксувати статус.

## Тести / перевірки (PLAN §15 + §18)
- Integration: усі пункти §15 integration зелені на тимчасовій БД+worker.
- E2E: skeleton→data→refresh, filters/period, sync modal reconnect/close/retry, a11y, responsive, reduced motion, screenshot regression.
- Live smoke: totals контрольного клієнта звірені вручну з кожним provider UI.
- Security: у БД/логах/events немає секретів; artifact path guard; safe errors.
- Backup/restore: відновлення SQLite+artifacts за інструкцією.

## Definition of Done / Gate (= PLAN §18 MVP DoD)
- [ ] Усі активні клієнти — картками з logo/name/domain; `Тиждень/Місяць` міняє точні ranges.
- [ ] 4 provider sections: погоджені KPI, delta, quality, freshness; `null`/zero/stale/partial/truncated/failed візуально й семантично різні.
- [ ] Sync одного клієнта/провайдерів з UI; live progress переживає reload/reconnect; закриття modal не спиняє job; partial зберігає успішні + retry failed only.
- [ ] Restart API/worker не псує БД і не лишає job «вічно running».
- [ ] Старі AISeoTools CLI/tests не зламані; у БД немає секретів.
- [ ] `docker compose up --build` відтворює систему за README; є backup/restore інструкція.
- [ ] Keyboard/responsive/contrast перевірені; live totals контрольного клієнта звірені вручну.

## Ризики
- Metered live tests у CI спалять units/зіпсують сесії → тільки ручний прогін із budget+lock (PLAN §15 live).
- Sanitized fixtures мусять бути справді без секретів (перевірити перед комітом).
- Наскрізна таска не має «дороблювати» чужі модулі — лише перевіряти й склеювати; знайдені баги повертати у відповідну таску.

---

## ✅ Completion report

- **Дата:** 2026-07-22
- **Гілка / комміт:** `feat/task-12-frontend` @ (див. комміт цієї таски)
- **Статус:** done-with-followups

### Що зроблено

**Спільний harness.** `tests/conftest.py` + пакет `tests/harness/**` дають
кожній тасці однаковий спосіб підняти справжню систему: ізольована SQLite,
на яку націлений **весь процес** (`temp_db`), `TestClient` поверх реального
`app.main:app` (`api_client`), фабрика клієнта з інтеграціями (`seed_client`),
чистий реєстр із `FakeAdapter` (`fake_adapters`) і справжній `Dispatcher` у
фоновому потоці (`running_worker`). Плюс сканер секретів і збирач тимчасового
`--source` дерева AISeoTools.

**Sanitized fixtures.** `tests/fixtures/aiseotools_source/` — знеособлене
дерево з артефактами всіх родин, які розпізнає імпортер (Ahrefs, обидві
родини GSC, Semrush, rank-movement, кеш-директорія як «сміття»).

**Наскрізні набори — 37 тестів, `tests/integration/`:**
матриця «клієнт × сервіс» (паралельність доріжок, послідовність у доріжці,
ізоляція збою, `not_configured`, таймаут завислого адаптера), lease-recovery
й рестарт, cancel/retry/partial + ідемпотентність, SSE-replay через
`Last-Event-ID`, незмінність snapshot-ів і вибір latest-success,
backfill-імпорт (dry-run, ідемпотентність, schema drift, read-only `--source`),
hardening (секрети, редакція подій, path traversal, заголовки, безпечні
помилки) і ops-скрипти (backup → втрата БД → restore).

**Контракт AISeoTools.** `tests/contract/` знімає 51 публічний символ:
увесь namespace `seo_audit.dashboard.*` **плюс** ті легасі-символи, які
dashboard-модулі реально імпортують (список збирається з AST, не руками).
Окремо — `scripts/aiseotools_regression.sh` для повного прогону старої suite.

**Ops.** `scripts/backup_sqlite.py` (онлайн-бекап API + integrity_check +
sha256 + ротація), `scripts/restore_sqlite.py` (перевірка до дотику до
робочої бази, стара база відкладається, а не стирається),
`scripts/retention_cleanup.py`, `scripts/live_smoke.py` (metered, з
обов'язковим `--yes-spend-units`, budget і profile-lock).

**Документація.** `docs/runbook.md`, `docs/backup-restore.md`,
`docs/security.md`, `docs/dod-mvp.md` (стан кожного пункту PLAN §18 з доказом).

**CI.** `.github/workflows/ci.yml`: lint → mypy → unit → integration →
contract → окремий крок, який доводить, що metered тести лишились
пропущеними.

### Як зроблено (ключові рішення)

- **Harness перемикає процесні глобали, а не роздає власний engine.**
  `app/api/jobs.py` і job-engine ходять у БД через модульний
  `worker_session_scope()`, тож тест, який просто передав би свій engine,
  писав би в одну базу, а застосунок читав з іншої. `harness/db.py` натомість
  ставить `DATABASE_URL` і скидає кеші `app.db.session`/`get_settings`.
- **У тестах працює справжній `Dispatcher`, а не його імітація.** Інакше
  перевірялась би переписана модель конкурентності, а не сама конкурентність.
- **Паралельність доводиться інтервалами, а не таймінгами.** Клітинки однієї
  доріжки не мають перетинатись у `started_at…finished_at`, різних — мають
  хоч раз перетнутись. Ніяких порогів на кшталт «швидше за N секунд», тож
  тест не флакає на повільній машині.
- **Секрети ловляться canary-значеннями.** «Протікаючий» адаптер навмисно
  порушує контракт (Bearer у повідомленні, `api_key`/`cookie` в `details`,
  секрет у тексті винятку); далі сканується **весь файл БД**. Збіг = реальний
  витік, а не схожий на ключ рядок.
- **`live` вимикається в conftest, а не в CI-конфізі.** Гейт у CI ловив би
  лише CI; локальний `pytest` спалив би units. Скіп ставиться по **маркеру**,
  а не по `item.keywords` — там є ще й імена батьківських вузлів, через що
  спершу пропускалась уся тека `tests/live/` незалежно від маркера.
- **Harness імпортується як `harness`, а не `tests.harness`.** AISeoTools має
  власний пакет `tests`, який у контейнері стоїть у site-packages і перекриває
  наш (перевірено: `import tests` резолвиться в `/seo/AISeoTools/tests`).
  `tests/conftest.py` кладе `tests/` у `sys.path` явно.
- **Бекап — через онлайн-бекап API SQLite.** База в WAL і під живим writer-ом;
  `cp app.db` без `-wal`/`-shm` дав би неузгоджений знімок.
- **Скріншот-регресія — не автоматичний гейт.** Растеризація шрифтів різниться
  між машинами, тож байтове порівняння давало б шум. Раннер прогонить сценарій,
  перевіряє, що знімки створені й непорожні, і показує `git diff` для очей.

**Головні файли:** `tests/conftest.py`, `tests/harness/{db,fakes,worker,fixtures,secrets}.py`,
`tests/integration/*` (8 файлів), `tests/contract/test_aiseotools_signatures.py`,
`tests/e2e/run_frontend_smoke.sh`, `scripts/{backup_sqlite,restore_sqlite,retention_cleanup,live_smoke,update_aiseotools_snapshot}.py`,
`scripts/aiseotools_regression.sh`, `docs/*.md`, `.github/workflows/ci.yml`.

**Відхилення від плану:**
- harness лежить у `tests/harness/**`, а не `tests/unit/__shared__/**` — він
  потрібен `tests/integration` і `tests/contract`, а не лише юнітам, і місце
  всередині `tests/unit/` вводило б в оману;
- `tests/integration/test_security.py` перейменовано на
  `test_security_hardening.py`: pytest не дозволяє два тест-модулі з однаковим
  іменем поза пакетами (конфлікт із `tests/unit/core/test_security.py`);
- backup/restore — Python-скрипти, а не shell: у образі немає бінарника
  `sqlite3`, а онлайн-бекап доступний саме з Python.

### Контрактні зміни (Contract changes)

У спільних типах, API і схемі БД — **немає**. Жоден owned path іншої таски не
редагувався.

Додано (нове, для всіх):
- фікстури `temp_db`, `migrated_db`, `api_client`, `seed_client`,
  `fake_adapters` у кореневому `tests/conftest.py`;
- маркери `integration`, `contract`, `e2e`, `live` і прапорець `--live`;
- `scripts/aiseotools_regression.sh` ставить dev-екстра AISeoTools **у
  контейнер** (`pip install -e "$AISEOTOOLS_ROOT[dev]"`), бо наш образ ставить
  її лише з рантайм-залежностями. `pyproject.toml` (task-01) не змінювався.

### Як перевірити (verification)

```bash
docker compose up -d --build
docker compose exec api poetry run ruff check .
docker compose exec api poetry run mypy app
docker compose exec api poetry run pytest -q
docker compose exec api poetry run pytest -q -m live          # має бути лише skipped
docker compose exec api bash scripts/aiseotools_regression.sh
bash tests/e2e/run_frontend_smoke.sh                          # node+chromium на хості
docker compose exec api poetry run python scripts/backup_sqlite.py --out-dir /tmp/bk --keep 2
docker compose exec api poetry run python scripts/retention_cleanup.py --dry-run
docker compose exec api poetry run python scripts/live_smoke.py --list
```

**Фактичні результати прогону 2026-07-22:** `ruff` і `mypy` — чисто;
`pytest` — **390 passed, 1 skipped (live), 1 xfailed**; стара suite
AISeoTools — **1492 passed, 3 skipped**; frontend smoke — `ok`;
backup → видалення БД → restore відновлює клієнтів і метрики;
`docker compose up -d --build` з нуля піднімає `api` і `worker` у `healthy`,
`GET /readyz` → `{"status":"ok","checks":{"database":"ok"}}`.

### Тести

| Набір | Кількість | Що покриває |
|---|---|---|
| `tests/integration/test_job_matrix.py` | 4 | паралельність доріжок, ізоляція збою, `not_configured`, таймаут |
| `tests/integration/test_recovery_and_restart.py` | 3 | протухла lease, осиротілий cancel, «жодної вічної running» |
| `tests/integration/test_cancel_retry_partial.py` | 4 (1 xfail) | partial + retry-failed-only, cancel у польоті, ідемпотентність |
| `tests/integration/test_sse_replay.py` | 3 | `Last-Event-ID`, 404, стрім батча |
| `tests/integration/test_snapshots_and_dashboard.py` | 4 | незмінність snapshot, latest-success, freshness-skip, різні стани на дашборді |
| `tests/integration/test_importer_backfill.py` | 6 | dry-run, усі родини, похідні метрики, ідемпотентність, schema drift, битий JSON |
| `tests/integration/test_security_hardening.py` | 8 | секрети в БД/подіях/помилках, path traversal, заголовки, CORS, фікстури, `.env.example` |
| `tests/integration/test_ops_scripts.py` | 5 | backup/restore round-trip, ротація, retention, відмова на битому файлі |
| `tests/contract/test_aiseotools_signatures.py` | 4 | знімок 51 публічного символу, адитивність нових аргументів, імпортованість легасі |
| `tests/live/test_live_smoke.py` | 3 (1 live) | відмова без згоди на витрати, гейт маркера, metered preflight |
| `tests/e2e/frontend/frontend-smoke.cjs` | 1 сценарій | рендер, фільтри, sync-діалог, фокус, історія, порожні/помилкові стани, знімки |

### Follow-ups / борг

1. **`TODO(task-05)`: cancel клітинки, яка ще `queued`.** Вона лишається в
   `cancel_requested` до рестарту worker-а — доріжка забирає лише `queued`,
   а `cancel_requested` не входить у `_TERMINAL`, тож job такого запуску
   ніколи не стає термінальним і SSE-стрім не закривається. Зафіксовано
   `xfail(strict=True)` у `test_cancel_finalizes_a_queued_cell`: коли task-05
   це полагодить, тест впаде як XPASS і його треба буде зняти. Обхід —
   у [`docs/runbook.md`](../../../docs/runbook.md) §3.3.
2. **`TODO(task-01)`: посилання на `docs/` із README.** README — owned path
   task-01, тому не чіпався; варто додати рядок на runbook/backup-restore.
3. **`TODO(task-01)`: pin AISeoTools за commit/tag.** Доки цього немає,
   `tests/contract` і `scripts/aiseotools_regression.sh` на CI лише
   само-пропускаються — гейт працює тільки локально.
4. **Ручні пункти DoD.** Контраст і звірка live-totals із UI провайдерів
   потребують людини; стан — у [`docs/dod-mvp.md`](../../../docs/dod-mvp.md).
5. **Auth/CSRF/audit trail перед public exposure.** Поза MVP-скоупом
   (приватна мережа), чекліст — у [`docs/security.md`](../../../docs/security.md) §5.
6. **Скріншот-регресія без людини.** Потрібен перцептивний дифф із
   толерантністю до растеризації; поки що дифф читає рев'ювер.
