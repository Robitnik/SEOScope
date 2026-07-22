# task-16 — Розмірні дані GSC: сторінки та запити (модель + збір + API)

- **Пакет:** нові розмірні таблиці, збір per-page/per-query із GSC, репозиторії, analytics API
- **Хвиля:** E (v2) — **найбільший шматок, стартувати першим**
- **Depends on:** — (розширює завершені task-03 / task-09)
- **Blocks:** task-17 (алерти неможливі без цих даних), task-18 (таби аналітики)
- **PLAN.md:** **§21.4 (розмірні дані GSC)**, §6 (модель даних), §12 (truncated ≠ повний), §8.3 (source policy)

## Мета

Чинна `metric_values` зберігає **лише плоскі KPI** і не має жодної розмірності — тому аналітика по сторінках/запитах і алерти зараз технічно неможливі. Ця таска додає розмірний шар: `clicks/impressions/CTR/position` у розрізі **URL-сторінок** і **пошукових запитів** з GSC.

Джерело — **тільки GSC**: це єдиний провайдер, що дає всі три метрики консистентно (рішення §21.4).

## Обсяг

**Входить:**
- **Нові таблиці** (`app/db/models.py` + міграція):
  - `gsc_dimension_metrics`: `id`, `snapshot_id`, `client_id`, `dimension` (`page|query`), `dimension_value` (URL або запит), `period_start`, `period_end`, `clicks`, `impressions`, `ctr`, `position`, `quality`, `rank` (позиція в top-N);
  - unique `(snapshot_id, dimension, dimension_value)`; індекси `(client_id, dimension, period_start)`, `(client_id, dimension, clicks)`;
  - immutable після commit (як `metric_values`).
- **Контроль обсягу (обов'язково)**: top-N рядків за impressions на період (default 1000, налаштування), решта згортається в один рядок-залишок `«інше»` з сумами. Набір позначається `truncated`.
- **Окремий retention** — коротший за агрегати (розмірні дані ростуть на порядки швидше); прибирання разом із подіями.
- **Збір**: розширення GSC-вертикалі — AISeoTools `seo_audit/dashboard/gsc.py` вчиться віддавати pages/queries за період; app-адаптер `app/integrations/gsc.py` мапить це у розмірні рядки. Обидва — **адитивно**, наявні агрегати не змінюються.
- **`app/api/analytics.py`** (новий): `GET /api/v1/clients/{id}/analytics/pages` і `.../queries` з фільтром періоду (week/month/quarter/year/custom через task-14), сортуванням і пагінацією.

**НЕ входить:** алерти (task-17), UI (task-18), ключі з Semrush/Ahrefs (свідомо поза обсягом — §21.4).

## Owned paths

```text
app/db/models.py                    # ТІЛЬКИ нові розмірні таблиці (успадковано від завершеної task-03)
migrations/versions/<new>_gsc_dimensions.py
app/db/repositories/dimensions.py   # новий
app/api/analytics.py                # новий
app/integrations/gsc.py             # адитивне розширення
AISeoTools: seo_audit/dashboard/gsc.py   # адитивне розширення
tests/unit/db/test_dimensions.py, tests/unit/api/test_analytics.py
tests/unit/integrations/test_gsc_dimensions.py
```

> **Успадкована зона.** `app/db/models.py`, `migrations/` і `app/integrations/gsc.py` належали завершеним task-03/09. Ця таска бере на себе відповідальність **лише за свої додатки** і не чіпає наявні таблиці/поля. Правило «міграції створює лише task-03» переходить сюди для цієї міграції.

## Consumes

- `resolve_periods` — task-14 (для фільтрів періоду в API).
- Наявний GSC-збір, `CollectionContext/Result`, `Quality` — task-09/02.
- Патерн immutable-снапшотів — task-03.

## Provides

- `DimensionRepository`: `bulk_insert`, `top_pages(client, range, n)`, `top_queries(...)`, `for_two_periods(...)` (потрібне task-17).
- Analytics endpoints — база для task-18.

## Кроки реалізації

1. Схема + міграція + репозиторій (спершу, бо це розблоковує 17/18).
2. AISeoTools-розширення GSC: pages/queries за довільний період, адитивно.
3. App-адаптер: мапінг у розмірні рядки, top-N + залишок, `truncated`.
4. Analytics API з фільтрами періодів і пагінацією.
5. Retention/cleanup для розмірних даних.

## Тести

- top-N: понад ліміт → рівно N рядків + рядок-залишок, набір `truncated`; сума залишку коректна.
- **`truncated` не подається як повний список** (§12) — перевірити прапор у відповіді API.
- CTR/position з GSC зберігаються з власною точністю (не обрізаються до int).
- `None` ≠ `0` для відсутніх метрик рядка.
- Unique-констрейнт `(snapshot_id, dimension, dimension_value)`; immutability.
- Наявні агрегатні GSC-тести (task-09) лишаються зеленими — регресія.
- Обсяг: вставка 1000 рядків × кілька періодів не деградує (індекси працюють).

## Definition of Done / Gate

- [ ] Розмірні дані GSC зберігаються й читаються за періодами; міграція `upgrade`/`downgrade` чиста.
- [ ] Top-N + залишок + `truncated` працюють; обрізане не видається за повне.
- [ ] Агрегатний GSC-збір (task-09) не зламано; AISeoTools-зміни адитивні, стара suite зелена.
- [ ] Retention для розмірних даних налаштовний і працює.
- [ ] Analytics API віддає сторінки/запити з фільтром періоду й пагінацією.

## Ризики

- **Обсяг даних** — головний ризик для SQLite: без top-N і retention таблиця вибухне. Ліміт закласти з першого дня, не «потім».
- GSC data lag → `data_through` має проброшуватись і в розмірні рядки, інакше алерти (task-17) порівняють неповний період із повним.
- Спокуса додати ключі з Semrush/Ahrefs — поза обсягом; змішування джерел дасть неспівставні цифри.

---

## ✅ Completion report

- **Дата:** 2026-07-22
- **Гілка / комміт:** feat/task-12-frontend (робота велась у поточній гілці за рішенням користувача)
- **Статус:** done-with-followups

### Що зроблено

Розмірний шар GSC (сторінки/запити) повністю працює на рівні моделі/збору/API:

- Нова таблиця `gsc_dimension_metrics` (immutable, top-N + залишок-«інше», `quality` на кожен рядок) + чиста Alembic-міграція (upgrade/downgrade перевірені).
- `DimensionRepository`: `bulk_insert`, `for_period`, `top_pages`/`top_queries`, `for_two_periods` (для task-17), `purge_older_than` (retention).
- AISeoTools-розширення `seo_audit/dashboard/gsc.py`: `collect_gsc_dimensions()` — per-page/per-query clicks/impressions/CTR/position за два періоди через той самий browser-експорт, що вже відкриває `collect_gsc_direct` (одна ZIP-експортна дія дає обидві вкладки — Pages і Queries — тому нема окремого browser-раунду на вимір). Top-N + агрегований залишок (`Σclicks/Σimpressions` для CTR, зважене на impressions середнє для position — ніколи плоске середнє).
- App-адаптер `app/integrations/gsc.py`: `GscAdapter.collect_dimensions()` мапить AISeoTools-результат у `DimensionValueResult` (dimension/rank/quality на рядок), never-raises як і `collect()`.
- `app/api/analytics.py`: `GET /clients/{id}/analytics/pages` і `.../queries` — фільтр періоду (week/month/quarter/year/custom через `resolve_periods`), сортування (`clicks|impressions|ctr|position`), пагінація, чесний `truncated`-прапор і `total_rows_available` окремо від сторінки відповіді.

### Як зроблено (ключові рішення)

- **Один `ProviderSnapshot` = один період.** Унікальний констрейнт `(snapshot_id, dimension, dimension_value)` навмисно без `period_role` (на відміну від `metric_values`) — тому current/previous завжди пишуться як ДВІ окремі снапшот-строки, не одна. Це узгоджено з тим, як `for_period`/`for_two_periods` і так резолвлять кожен бік порівняння незалежно за точним `(period_start, period_end)`.
- **`truncated` живе в `quality`, не в окремій колонці.** Кожен рядок періоду, який був обрізаний до top-N (включно з рядком-залишком), позначається `Quality.truncated`; API виводить прапор `truncated` із фактично збережених рядків, а не з підрахунку кількості — не можна випадково сплутати «короткий, але повний» список із «обрізаним» (PLAN §12).
- **Один browser-експорт на період покриває обидва виміри.** `GSCConsole.export_performance` завжди повертає ОБИДВІ вкладки (`performance_pages.csv` + `performance_queries.csv`) в одному ZIP — тому `collect_gsc_dimensions` відкриває ОДНУ browser-сесію на весь виклик (як і `collect_gsc_direct`), а не по сесії на вимір.
- **Збір НЕ підключений до `app/jobs`-конвеєра.** `GscAdapter.collect_dimensions()` — новий метод, свідомо поза `ProviderAdapter`-протоколом (інша форма результату, ніж `CollectionResult.metrics`). У «Кроки реалізації» плану нема пункту про wiring у диспетчер, а `app/jobs/**` — чужа (task-05) зона; це задокументовано як follow-up нижче.
- Головні файли:
  - `app/db/models.py` — `DimensionKind`, `GscDimensionMetric`, `DIMENSION_OTHER_VALUE` (додатково до успадкованої зони).
  - `migrations/versions/8cc2c6bcd3a9_gsc_dimensions.py` — нова таблиця + індекси.
  - `app/db/repositories/dimensions.py` — новий репозиторій.
  - `app/integrations/gsc.py` — `collect_dimensions()`, `DimensionValueResult`, `DimensionCollectionResult`, `_map_dimension_periods` (додатково до успадкованої зони).
  - `AISeoTools/seo_audit/dashboard/gsc.py` — `collect_gsc_dimensions()`, `DimensionValue`, `DimensionPeriodResult`, `DashboardDimensionResult`, `_cap_top_n`, `_dimension_rows_from_csv`.
  - `app/schemas/api.py` — `DimensionRowDTO`, `DimensionAnalyticsResponseDTO` (нова секція в кінці файлу, за прецедентом task-06/task-14).
  - `app/api/analytics.py` — новий роутер.

### Контрактні зміни (Contract changes)

- **`app/schemas/api.py`** (task-02-власний файл) доповнено секцією `DimensionRowDTO`/`DimensionAnalyticsResponseDTO` — за тим самим прецедентом, що й task-06 (`ClientMetricsHistoryDTO`) і task-14 (`CoverageDTO`): task-02 залишив ці response-shapes відкритими для owning-таски. Суто адитивно, нічого наявного не змінено.
- Нова публічна поверхня: `GET /api/v1/clients/{id}/analytics/pages`, `GET /api/v1/clients/{id}/analytics/queries`.
- `app/db/models.py`/`migrations/` доповнені лише новою таблицею — жодна наявна таблиця/поле не торкнуті (успадкована зона з дозволом у самому плані таски).

### Як перевірити (verification)

```bash
docker compose build api
docker compose run --rm api poetry run alembic upgrade head
docker compose run --rm api poetry run pytest tests/unit/db/test_dimensions.py tests/unit/integrations/test_gsc_dimensions.py tests/unit/api/test_analytics.py -v
docker compose run --rm api poetry run pytest -q                       # повний unit-набір застосунку: 426 passed, 1 skipped, 1 xfailed
docker compose run --rm api poetry run ruff check .                    # All checks passed!
docker compose run --rm api poetry run mypy app                        # Success: no issues found in 71 source files
docker compose run --rm api bash scripts/aiseotools_regression.sh      # 1511 passed, 3 skipped (стара AISeoTools suite)
```

Ручна перевірка API (після сідування клієнта + розмірних рядків через `DimensionRepository` напряму, оскільки live-збір не підключено — див. follow-ups):

```bash
curl "http://localhost:6524/api/v1/clients/<id>/analytics/pages?period=week&anchor=2026-01-12&sort_by=clicks&limit=20"
```

Очікується: `rows` відсортовані за `sort_by` спадно, `truncated`/`total_rows_available` відображають реальний стан збереженого періоду, `period_start`/`period_end` — `null`, якщо точного збереженого періоду нема (чесна відповідь, не вигадана агрегація).

### Тести

- `tests/unit/db/test_dimensions.py` (11 тестів): top-N + залишок + `truncated`, `None` ≠ `0`, Decimal-точність CTR/position, unique-констрейнт, immutability, порожня відповідь без точного періоду, `top_pages`/`top_queries`/`for_two_periods`, retention (`purge_older_than` — видаляє лише старе, відмовляється нижче безпечного мінімуму).
- `tests/unit/integrations/test_gsc_dimensions.py` (5 тестів): `not_configured` без `property`, мапінг page/query з rank/quality, truncated-прапор на кожному рядку періоду (включно з рештою), never-raises на неочікуваному винятку.
- `tests/unit/api/test_analytics.py` (6 тестів): сортування, розділення page/query, `truncated` не видається за повний список, порожня відповідь без падіння на невідповідному періоді, 404 на невідомому клієнті, пагінація `limit`/`offset`.
- `AISeoTools/tests/dashboard/test_gsc.py` (+16 нових тестів): `_cap_top_n` (сортування, обрізка, Σclicks/Σimpressions для CTR, зважене на impressions середнє для position, `None` замість `0` при нульових impressions), `_dimension_rows_from_csv`, `collect_gsc_dimensions` (одна browser-сесія на обидва періоди, мапінг pages+queries, truncated-прапор, browser-помилка → `unavailable` без винятку). Наявні GSC-тести (aggregate/direct) лишились зеленими без змін.

### Follow-ups / борг

- **Wiring живого збору в `app/jobs`-конвеєр не зроблено** — `GscAdapter.collect_dimensions()` існує та протестований, але ніщо в `run_cell`/dispatcher (task-05, чужа зона) його ще не викликає. Розмірні дані з'являться в БД лише через прямий виклик репозиторію/адаптера (як у тестах), доки хтось не додасть виклик у `app/jobs/runner.py` — саме тому задокументовано тут, а не приховано.
- **Retention не підключено до `scripts/retention_cleanup.py`** (task-13-власний файл) і немає `Settings.dimension_retention_days` (task-01-власний файл) — `DimensionRepository.purge_older_than(days)` повністю робочий і протестований, лишилось додати виклик в існуючий cron-скрипт:
  ```python
  # scripts/retention_cleanup.py, поруч із EventRepository.purge_older_than:
  from app.db.repositories.dimensions import DEFAULT_DIMENSION_RETENTION_DAYS, DimensionRepository
  deleted_dims = DimensionRepository(session).purge_older_than(
      getattr(settings, "dimension_retention_days", DEFAULT_DIMENSION_RETENTION_DAYS)
  )
  ```
- **Розширені періоди (quarter/year/custom) без точного збереженого діапазону повертають порожній результат**, а не крос-періодну агрегацію — на відміну від `app.metrics.aggregate` для плоских KPI. Агрегація per-page/per-query рядків через межі періодів (з коректним top-N/ranking на об'єднаному наборі) — суттєво складніша задача, поза обсягом цієї таски; якщо знадобиться, це окрема таска.
- Ownership-файл `doc/dev/new/INDEX.md` потребує позначки ✅ для task-16 (зроблено в цьому ж комміті).
