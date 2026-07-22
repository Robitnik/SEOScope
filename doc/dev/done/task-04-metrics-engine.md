# task-04 — Metrics Engine (periods, catalog, normalize, quality)

- **Пакет:** period engine, catalog метрик, нормалізація, quality/freshness правила
- **Хвиля:** B
- **Depends on:** task-02 (MetricSpec, enums, CollectionResult)
- **Blocks:** task-06 (dashboard рахує delta/quality через це), опосередковано провайдери (спільна семантика)
- **PLAN.md:** §3.1 (періоди), §3.2 (metric catalog), §12 (correctness & quality), §15 unit, Етап 0/4

## Мета

Чиста, повністю протестована **бібліотека без побічних ефектів**: обчислення точних періодів, каталог метрик, нормалізація значень і правила якості/свіжості та delta. Тут живе бізнес-семантика порівнянь, щоб ні API, ні фронт її не дублювали.

## Обсяг

**Входить:**
- `app/metrics/periods.py` — period engine:
  - `week`: останній повний Пн–Нд проти попереднього; `month`: останній повний календарний місяць проти попереднього.
  - timezone-aware (default `America/New_York`), повертає точні `DateRange` current/previous + опис для UI (`13–19 липня vs 6–12 липня`).
  - режим `to_date` як окрема гілка (previous обрізається до тієї ж кількості повних днів) — реалізувати, але позначити «увімкнути після UX-рішення» (PLAN §3.1).
- `app/metrics/catalog.py` — наповнення `list[MetricSpec]` за таблицею §3.2 (Ahrefs: Health Score/Errors/Warnings/URLs; Semrush: Authority/Organic Traffic/Keywords/Referring Domains; GSC: Clicks/Impressions/CTR/Avg Position; RN/GA4: Users/Organic Users/Total Conversions/Organic Conversions). `better_direction`, `comparison_kind`, precision, `allow_derived`, freshness SLA, min quality — на метрику.
- `app/metrics/normalize.py` — приведення `CollectionResult.metrics` до канонічних одиниць/точності; derived-метрики (`CTR = clicks/impressions` лише якщо обидва complete) з `is_derived=True`.
- `app/metrics/quality.py` — freshness (SLA→`stale`), агрегація quality на рівні snapshot/card, delta:
  - `delta_abs`, `delta_pct`; при previous `0/null` → `delta_pct=None` + reason (не infinity).
  - семантика зміни через `better_direction`, **не** через знак.
  - `range` порівнюється тільки з `range` того ж типу; `snapshot` — з останнім valid snapshot попереднього періоду.

**НЕ входить:** доступ до БД (працює над DTO/значеннями; історію знімків дістає репозиторій у task-06/05 і передає сюди), збір даних (провайдери), рендер.

## Owned paths
```
app/metrics/periods.py
app/metrics/catalog.py
app/metrics/normalize.py
app/metrics/quality.py
app/metrics/__init__.py
```

## Consumes
- `MetricSpec`, `MetricUnit`, `Quality`, `BetterDirection`, `PeriodKind`, `ComparisonMode`, `PeriodRole`, `DateRange`, `MetricValueResult` — усе з task-02.

## Provides
- `resolve_periods(kind, anchor, tz, mode) -> Periods(current: DateRange, previous: DateRange, label: str, data_through_hint?)`.
- `CATALOG: dict[str, MetricSpec]` + `metrics_for(provider)`.
- `normalize_result(result, catalog) -> list[MetricValueResult]`.
- `compute_delta(spec, current, previous) -> DeltaDTO(delta_abs, delta_pct|None, reason|None, sentiment: good|bad|neutral)`.
- `assess_quality(spec, value, observed_at, now) -> Quality` (freshness→stale).

## Кроки реалізації
1. Period engine з явними тестами меж (див. нижче) — це найризикованіша частина, роби її першою й прискіпливо.
2. Catalog за §3.2; ключі метрик — канонічні, ті самі, що вживають провайдери й фронт.
3. Normalize + derived-правила.
4. Quality/freshness + delta з семантикою напрямку.

## Тести (критичні — PLAN §15 unit)
- Межі week/month, **високосний рік**, зміна місяця/року, timezone (клієнт у `America/New_York` vs UTC), DST-переходи.
- `to_date`: previous обрізається до тієї ж кількості повних днів.
- delta: previous=0 → `delta_pct=None`+reason; від'ємна зміна Errors = `good` (бо `better_direction=down`); від'ємна зміна Clicks = `bad`.
- `None` vs `0`, precision (CTR/position/score не обрізаються до int), derived-flag ставиться лише при complete-складниках.
- freshness: значення старше за SLA → `stale`.
- Порівняння різнотипних періодів заборонено (range↔snapshot).

## Definition of Done / Gate
- [x] Period engine покритий межовими тестами (leap year, tz, DST, місяць/рік).
- [x] Catalog відповідає §3.2; кожна метрика має напрямок/тип/точність/SLA.
- [x] delta не повертає infinity; семантика через `better_direction`.
- [x] Модуль чистий (без I/O), детермінований, `mypy` зелений.
- [x] Ключі метрик збігаються з тими, що очікують провайдери й фронт.

## Ризики
- Розбіжність семантики періодів Reporting Ninja (PLAN §2.3.1) — **не** покладатися на старий `get_report_periods()`; це власний, окремо протестований engine.
- Змішування snapshot і range (PLAN §2.3.2) — жорстко розділити в типах і тестах.

---

## ✅ Completion report

- **Дата:** 2026-07-22
- **Гілка / комміт:** main (не закомічено — див. примітку в кінці)
- **Статус:** done

### Що зроблено
Чиста бібліотека без побічних ефектів у `app/metrics/`:
- `periods.py` — `resolve_periods(kind, anchor, tz, mode) -> Periods`: точні `week`/`month` межі в `closed`-режимі (останній повний період проти попереднього, рівної довжини) і в `to_date`-режимі (частковий поточний період проти того самого числа днів минулого періоду, з capping, коли попередній місяць коротший). Anchor конвертується в локальну календарну дату через `zoneinfo` до будь-якої арифметики з датами, тому DST/leap year не впливають на межі.
- `catalog.py` — `CATALOG` з 16 `MetricSpec` за §3.2 (4 KPI × 4 провайдери), `metrics_for(provider)`, `DERIVED_RATIO_RULES` (поки що лише `gsc_ctr`).
- `normalize.py` — `normalize_result`: округлення `value_numeric` до `precision` каталогу (`None` ніколи не округлюється до `0`) + обчислення derived-метрик лише коли обидва компоненти `quality == complete` і адаптер ще не повернув значення сам.
- `quality.py` — `assess_quality` (freshness → `stale`, ніколи не апгрейдить і не чіпає `unavailable`), `compute_delta` (`DeltaDTO` з `delta_abs`/`delta_pct`/`reason`/`sentiment`, семантика через `better_direction`, ніколи не infinity), `ensure_comparable_periods` (жорстко забороняє range↔snapshot порівняння).

### Як зроблено (ключові рішення)
- **Ключі метрик провайдер-префіксовані** (`ahrefs_health_score`, `gsc_clicks`, `rn_users`, ...) — уникає майбутніх колізій і самодокументується в API/фільтрах; вже узгоджується з прикладом ключа в `tests/unit/schemas/test_metrics.py` (`"ahrefs_health_score"`).
- **`Periods.data_through_hint`** лишений `None` тут навмисно: period engine не має I/O і не знає про лаг конкретного провайдера (напр. GSC) — це поле заповнює викликач після збору.
- **`DeltaDTO`** — власний тип `app/metrics/quality.py` (не `app/schemas/`), бо `sentiment: good|bad|neutral` — це внутрішня семантика metrics-engine; `task-06` транслює її у `MetricCardValue.is_improvement`/`delta_reason` при побудові API DTO.
- **`ensure_comparable_periods`** — окрема функція (не частина `compute_delta`, який навмисно лишився з мінімальним сигнатурою `(spec, current, previous) -> DeltaDTO` за задумом таски): guard приймає `PeriodRole` обох сторін і кидає `ValueError` при невідповідності `comparison_kind`, замість мовчазного порівняння різнотипних періодів.
- **Derived CTR** — генерична ratio-таблиця `DERIVED_RATIO_RULES` у `catalog.py` замість хардкоду в `normalize.py`, щоб майбутні derived-метрики додавались декларативно.
- **Округлення** — `Decimal.quantize(..., rounding=ROUND_HALF_UP)` скрізь (замість дефолтного banker's rounding), для передбачуваного відображення в UI.
- Freshness SLA: єдиний MVP-дефолт 168 год (тиждень) для всіх метрик — задокументовано в `catalog.py` як свідоме спрощення до появи потреби в per-metric SLA.

### Контрактні зміни (Contract changes)
Немає — жодних правок `app/schemas/`/`app/integrations/` (task-02 owned, read-only).

### Як перевірити (verification)
```bash
docker compose build api
docker compose run --rm --entrypoint bash api -lc "poetry run ruff check app/metrics tests/unit/metrics"
docker compose run --rm --entrypoint bash api -lc "poetry run mypy app/metrics"
docker compose run --rm --entrypoint bash api -lc "poetry run pytest tests/unit/metrics -q"
```
Усі три — чисто (ruff: 0, mypy: "Success: no issues found in 5 source files", pytest: 42 passed).

**Примітка щодо запуску:** `docker compose run --rm api ...` (без `--entrypoint bash`) зараз падає на кроці `alembic upgrade head` в `docker/entrypoint.sh` — `migrations/versions/db1407dec7b0_initial_schema.py:25` використовує `app.db.base.GUID` без імпорту `app` (NameError). Це файл task-03 (`migrations/**`), поза owned paths цієї таски — не чіпав, лише обійшов через `--entrypoint bash`, щоб мати змогу verify свій модуль. Варто повідомити власника task-03.

### Тести
`tests/unit/metrics/` (42 тести):
- `test_periods.py` — closed week/month (включно з прикладом з PLAN §4.1: "13–19 Jul vs 6–12 Jul"), перетин року (тиждень і місяць), leap year (2028) і non-leap (2027) лютий, timezone-конверсія навколо півночі UTC, DST spring-forward 2026-03-08 (обидва боки переходу), naive anchor → `ValueError`, `to_date` для тижня і місяця (включно з capping, коли лютий коротший за elapsed days).
- `test_catalog.py` — повний набір з 16 ключів, порядок `metrics_for`, snapshot vs range за провайдером, напрямок семантики (Errors=down, Clicks=up, URLs crawled=neutral) — не за знаком, `allow_derived` лише в `gsc_ctr`, precision, freshness SLA присутній скрізь.
- `test_normalize.py` — округлення precision, `None` не перетворюється на `0`, derived CTR обчислюється лише при `complete`+`complete`, пропускається при `partial`, не дублюється, якщо адаптер уже повернув значення сам; невідомий ключ проходить без змін.
- `test_quality.py` — freshness → `stale`, `unavailable` не апгрейдиться, без SLA ніколи не stale; delta: `previous=0` → `None`+`"previous_zero"`, `previous=None`/`current=None` → відповідні reason-рядки, зменшення Errors=`good`, зменшення Clicks=`bad`, neutral-метрика завжди `neutral`, нульова зміна = `neutral`, округлення `delta_pct` до 2 знаків; `ensure_comparable_periods` приймає правильні ролі й кидає на невідповідних.

### Follow-ups / борг
- Freshness SLA — єдиний дефолт 168 год для всіх метрик (MVP-спрощення, §12 не вимагає per-metric значень наперед); коли з'явиться реальна потреба в різних SLA (напр. GSC з іншим лагом) — правка `catalog.py` в межах цієї ж owned path, без контрактних наслідків.
- `ComparisonMode.to_date` реалізований повністю і протестований, але, як і зазначено в задачі, вмикати в UI/API лише після окремого UX-рішення (PLAN §3.1) — не блокер для цієї таски.
- Не закомічено. За CLAUDE.md таска після DoD комітиться логічним коммітом (без push); цей крок лишається на розсуд користувача.
