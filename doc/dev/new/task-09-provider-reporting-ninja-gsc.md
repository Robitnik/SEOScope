# task-09 — Provider: Reporting Ninja / GA4 + GSC (вертикальний зріз)

- **Пакет:** arbitrary-period wrapper у AISeoTools + app-адаптери RN/GA4 і GSC
- **Хвиля:** C
- **Depends on:** task-02 (adapter contract), task-08 (AISeoTools core), task-05 (worker ганяє adapter), task-04 (ключі метрик/derived)
- **PLAN.md:** §2.2, §3.2 (KPI GSC + RN/GA4), §8.2/§8.3 (source policy), §12 (correctness), Етап 4 (vertical slice, gate)

## Мета

Найнадійніший MVP-шлях: зібрати **GA4** (Users, Organic Users, Total Conversions, Organic Conversions) і **GSC** (Clicks, Impressions, CTR, Avg Position) за два **довільні** періоди (current/previous). Реалізувати новий comparison-wrapper у AISeoTools (старий six-period `build_report()` **не чіпати**) і два app-адаптери під контракт task-02.

## Обсяг

**Входить:**
- **AISeoTools** (`seo_audit/dashboard/reporting_ninja.py`, `seo_audit/dashboard/gsc.py`):
  - новий wrapper, що викликає `discover_*_fields()` **один раз**, потім `fetch_ga4_period()`/`fetch_gsc_period()` **двічі** (current + previous) через наявні fetchers (PLAN §2.2, §8.2);
  - GSC aggregate над Reporting Ninja API за замовчуванням; direct GSC browser/export для CTR, avg position і детальних даних (PLAN §8.3);
  - емісія `ProgressEvent` (task-08 core), `data_through` для GSC data lag.
- **App** (`app/integrations/reporting_ninja.py`, `app/integrations/gsc.py`):
  - два `ProviderAdapter` (`provider = reporting_ninja` та `gsc`), `preflight()` + `collect()`;
  - мапінг AISeoTools-моделей → `CollectionResult`/`MetricValueResult` з канонічними ключами метрик (task-04 catalog);
  - CTR як **derived** (`clicks/impressions`) лише коли обидва complete; `is_derived=True`;
  - реєстрація адаптерів у registry (task-02).

**НЕ входить:** старий `build_report()` (заморожений), Ahrefs/Semrush (10/11), UI.

## Owned paths
```
# AISeoTools (репо /home/elon/dev/seo/AISeoTools)
seo_audit/dashboard/reporting_ninja.py
seo_audit/dashboard/gsc.py
tests/dashboard/test_reporting_ninja.py, test_gsc.py
# App
app/integrations/reporting_ninja.py
app/integrations/gsc.py
tests/unit/integrations/test_reporting_ninja.py, test_gsc.py
```

## Consumes
- `ProviderAdapter`, `CollectionContext/Result`, `MetricValueResult`, `Provider`, `Quality`, registry — task-02.
- AISeoTools `dashboard.events/models/config` + наявні `ga4_fetcher.fetch_ga4_period`, `gsc_fetcher.fetch_gsc_period`, discovery — task-08 / наявний код.
- catalog ключі, derived-правило CTR — task-04.

## Provides
- Зареєстровані адаптери `reporting_ninja` і `gsc`, готові для worker.
- Точні current/previous діапазони + `data_through` у результаті.

## Кроки реалізації
1. AISeoTools comparison-wrapper (discover×1 → fetch×2), additive, з `on_event`.
2. GSC direct-extension для CTR/position (browser/export) з quality/`data_through`.
3. App-адаптери: preflight (credentials/session/property mapping), collect (стадії §9), мапінг у `CollectionResult`.
4. Derived CTR; None-safe (немає даних → `unavailable`, не 0).
5. Реєстрація в registry.

## Тести
- AISeoTools: wrapper зве discovery один раз і fetch двічі; старий `build_report` byte/shape-незмінний (regression, PLAN §15).
- App: мапінг у канонічні ключі; CTR derived лише при complete; `data_through` проброшено; partial/`unavailable` при відсутності; секрети не течуть.
- Живий smoke (поза CI, PLAN §15 live): один контрольний клієнт, точні дати й totals звірені вручну з GSC/Ninja UI.

## Definition of Done / Gate
- [ ] Один реальний клієнт збирається з UI; точні дати й totals збігаються з джерелом (PLAN Етап 4 gate).
- [ ] Старий `client-report`/`build_report` flow не зламаний (regression зелений).
- [ ] GSC data lag відображено через `data_through`; CTR помічений derived.
- [ ] `None` ≠ `0`; секрети не потрапляють у events/result.
- [ ] Адаптери зареєстровані й проганяються worker'ом.

## Ризики
- Розбіжність семантики періодів Ninja (PLAN §2.3.1) → періоди дає task-04 engine, не старий `get_report_periods`.
- Reporting Ninja fetcher повертає лише clicks/impressions для GSC → CTR/position беремо з direct GSC (PLAN §2.2/§8.3), позначаємо provenance.
- Browser-профіль GSC/Ninja: lock `(provider, profile_name)`; не паралелити (task-05/AISeoTools).
