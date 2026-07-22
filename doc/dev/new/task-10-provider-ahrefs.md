# task-10 — Provider: Ahrefs (Site Audit вертикальний зріз)

- **Пакет:** lightweight Site Audit snapshot wrapper у AISeoTools + app-адаптер Ahrefs
- **Хвиля:** C
- **Depends on:** task-02 (contract), task-08 (AISeoTools core), task-05 (worker), task-04 (ключі/snapshot-семантика)
- **PLAN.md:** §2.2 (Ahrefs Site Audit/Rank), §3.2 (KPI Ahrefs), §8.3 (source policy), §12, Етап 5 (gate)

## Мета

Легкий **Site Audit snapshot** (Health Score, Errors, Warnings, URLs crawled) без повного XLSX/AI-verdict pipeline. Провайдер snapshot-типу: попереднє значення береться з історії БД, а не «сумується за місяць». Відсутній health score → `unavailable`, ніколи не `0`. Не плутати crawl date з collection date.

## Обсяг

**Входить:**
- **AISeoTools** (`seo_audit/dashboard/ahrefs.py`): тонкий collect навколо наявного `ahrefs_site_audit.collect.collect()` (повертає `AhrefsMachineFindings`) + `verify.pipeline.verify_issues()` (вже вміє прогрес) — але **без** важкого повного аудиту заради 4 KPI (PLAN §2.2). Additive `on_event`. API → CSV/browser fallback з quality badge.
- **App** (`app/integrations/ahrefs.py`): `ProviderAdapter(provider=ahrefs)`, `preflight()` (project lookup, units estimate, profile/API availability), `collect()`; мапінг у `CollectionResult` (snapshot-метрики, `observed_at`, crawl-date у provenance окремо від collection-date). Реєстрація в registry.
- Обробка станів: немає project / немає units / немає health score → відповідний `Quality`/`reason_code`, не вигадані числа. Stale crawl явно позначений.

**НЕ входить:** повний Rank Movement аудит (важкий, не для кожного відкриття dashboard — PLAN §2.2), XLSX/AI verdicts, інші провайдери.

## Owned paths
```
# AISeoTools
seo_audit/dashboard/ahrefs.py
tests/dashboard/test_ahrefs.py
# App
app/integrations/ahrefs.py
tests/unit/integrations/test_ahrefs.py
```

## Consumes
- contract/registry/DTO — task-02.
- AISeoTools `dashboard.events/models/config` + наявні `ahrefs_site_audit.collect.collect`, `verify.pipeline.verify_issues` — task-08 / наявний код.
- catalog ключі Ahrefs, snapshot-семантика delta — task-04.

## Provides
- Зареєстрований адаптер `ahrefs` (snapshot), готовий для worker.
- Health/issues у канонічних ключах + provenance (API/CSV/browser, crawl date).

## Кроки реалізації
1. AISeoTools thin wrapper: project → issues → affected → (за потреби) live verify, з `on_event` після кожного етапу; **без** повного XLSX/AI.
2. API → CSV/browser fallback з quality badge (browser fallback може не мати project metadata/health score → `unavailable`).
3. App-адаптер: preflight (project/units), collect, мапінг snapshot-метрик; crawl-date у provenance, collection-date = `observed_at`.
4. Missing health / no project / no units → коректний Quality/`reason_code`.
5. Реєстрація в registry.

## Тести
- no-project / no-units / no-health cases → `unavailable`/partial, не `0` (PLAN §15).
- crawl date ≠ collection date (не плутаються; stale crawl позначений).
- API↔CSV↔browser fallback дає узгоджені ключі + правильний quality badge.
- секрети/units не течуть у events.
- Живий smoke (поза CI): контрольний клієнт, health/issues звірені з Ahrefs UI.

## Definition of Done / Gate
- [ ] current/previous не плутає crawl date з collection date; stale crawl явно позначений (PLAN Етап 5 gate).
- [ ] Відсутній health score → `unavailable`, не `0`.
- [ ] Легкий шлях (без повного XLSX/AI) — швидкий для dashboard.
- [ ] Fallback policy (API/CSV/browser) з quality badge працює.
- [ ] Адаптер зареєстрований і проганяється worker'ом.

## Ризики
- Частина API-планів не має Site Audit / обмежені units (PLAN §2.2) → preflight-estimate + hard budget + явний fallback.
- Browser fallback без метаданих project → чесний `unavailable`, не вигадувати.
