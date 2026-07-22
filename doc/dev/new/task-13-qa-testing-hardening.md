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
- CI-гейти: lint+type+pytest на кожен модуль; окремо помічені metered live tests (не в звичайній CI).

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
