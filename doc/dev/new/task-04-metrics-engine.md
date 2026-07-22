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
- [ ] Period engine покритий межовими тестами (leap year, tz, DST, місяць/рік).
- [ ] Catalog відповідає §3.2; кожна метрика має напрямок/тип/точність/SLA.
- [ ] delta не повертає infinity; семантика через `better_direction`.
- [ ] Модуль чистий (без I/O), детермінований, `mypy` зелений.
- [ ] Ключі метрик збігаються з тими, що очікують провайдери й фронт.

## Ризики
- Розбіжність семантики періодів Reporting Ninja (PLAN §2.3.1) — **не** покладатися на старий `get_report_periods()`; це власний, окремо протестований engine.
- Змішування snapshot і range (PLAN §2.3.2) — жорстко розділити в типах і тестах.
