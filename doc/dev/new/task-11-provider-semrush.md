# task-11 — Provider: Semrush (вертикальний зріз)

- **Пакет:** browser runner як library API у AISeoTools + app-адаптер Semrush
- **Хвиля:** C
- **Depends on:** task-02 (contract), task-08 (AISeoTools core), task-05 (worker + profile lock), task-04 (ключі/snapshot)
- **PLAN.md:** §2.2 (Semrush API/browser+ingest), §3.2 (KPI Semrush), §8.3 (source policy, profile lock), §12, Етап 6 (gate)

## Мета

Semrush snapshot (Authority Score, Organic Traffic, Organic Keywords, Referring Domains). Рекомендований шлях — **browser export + `ingest_semrush`** із unified snapshot shape; API — fallback за явною політикою/бюджетом. Incomplete manifest → `partial`; stale-файл **не** потрапляє у snapshot. Retry невдалої стадії не змішує старі downloads.

## Обсяг

**Входить:**
- **AISeoTools** (`seo_audit/dashboard/semrush.py`): high-level browser runner як **library API** з callback-прогресом (CLI лишається тонким wrapper) навколо наявних `semrush.client.SemrushClient` (нормалізовані dict) та `semrush.ingest_manual.ingest_semrush()` (читає manifest, відрізняє incomplete run). Additive `on_event`, profile lock key `(provider, profile_name)`. Окрема job-artifact directory + checksum (manifest allow-list) — щоб старі CSV у shared downloads не давали хибних цифр (PLAN §17 ризик).
- **App** (`app/integrations/semrush.py`): `ProviderAdapter(provider=semrush)`, `preflight()` (paid account/profile/session, units за API-fallback, budget policy), `collect()`; мапінг у `CollectionResult` (snapshot); completeness validation для overview/backlinks/refdomains; API fallback за політикою. Реєстрація в registry.

**НЕ входить:** інші провайдери, зміна старого Semrush CLI-контракту (лише additive), UI.

## Owned paths
```
# AISeoTools
seo_audit/dashboard/semrush.py
tests/dashboard/test_semrush.py
# App
app/integrations/semrush.py
tests/unit/integrations/test_semrush.py
```

## Consumes
- contract/registry/DTO — task-02.
- AISeoTools `dashboard.events/models/config` + наявні `SemrushClient`, `ingest_semrush` — task-08 / наявний код.
- catalog ключі Semrush, snapshot-семантика — task-04.
- profile lock від worker (task-05) — жодного паралельного запуску одного профілю.

## Provides
- Зареєстрований адаптер `semrush` (snapshot), готовий для worker.
- Unified snapshot із completeness/quality + provenance (browser/api), units metadata.

## Кроки реалізації
1. AISeoTools library runner з callback + profile lock + окрема job-artifact directory + manifest allow-list/checksum.
2. `ingest_semrush` completeness: incomplete manifest → `partial`; stale-файл відсіюється (не потрапляє у snapshot).
3. App-адаптер: preflight (account/profile/budget), collect (стадії §9), мапінг snapshot-метрик; API fallback за політикою/бюджетом.
4. Retry failed stage без змішування старих downloads (нова artifact directory на attempt).
5. Реєстрація в registry.

## Тести
- incomplete manifest → `partial`; stale-файл **не** у snapshot (PLAN Етап 6 gate + §15).
- manifest partial/truncated cases; truncated export не видається за total.
- retry не змішує старі downloads (ізольована artifact dir/attempt).
- profile lock: паралельний запуск того самого профілю неможливий.
- API fallback лише за явною політикою/бюджетом; units metadata заповнена.
- секрети не течуть; browser session-shape у provenance без секретів.
- Живий smoke (поза CI): контрольний клієнт, totals звірені з Semrush UI.

## Definition of Done / Gate
- [ ] Incomplete manifest дає `partial`, stale-файл не потрапляє у snapshot (PLAN Етап 6 gate).
- [ ] Retry невдалої стадії не змішує старі downloads.
- [ ] Profile lock `(provider, profile_name)` дотриманий; жодного паралельного запуску профілю.
- [ ] Truncated export не подається як exact total.
- [ ] Адаптер зареєстрований і проганяється worker'ом; старий Semrush CLI не зламаний.

## Ризики
- Потрібен paid account + persistent browser profile (PLAN §2.2) → preflight + documented manual login flow; протухла сесія → `auth_required`.
- Старі CSV у shared downloads (PLAN §17) → manifest allow-list + окрема artifact dir + checksum.
- Metered API units → budget policy, API лише як fallback.
