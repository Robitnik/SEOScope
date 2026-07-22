# task-17 — Alerts engine: сторінки, що просідають і що набирають

- **Пакет:** детекція трендів по сторінках/запитах + alerts API
- **Хвиля:** E (v2)
- **Depends on:** **task-16** (без розмірних даних алерти неможливі), task-14 (періоди)
- **Blocks:** task-18 (блок алертів на сторінці клієнта)
- **PLAN.md:** **§21.5 (алерти)**, §12 (correctness), §21.4

## Мета

Дати оператору те, заради чого він відкриває дашборд: **що саме почало падати і що зростає**. Не «трафік -8%», а конкретні URL із втраченими кліками — з порогами, які не шумлять на мікрообсягах.

## Обсяг

**Входить:**
- **`app/metrics/alerts.py`** (новий) — детекція за §21.5:
  - вхід — per-page (і per-query) `clicks/impressions` за **два суміжні періоди** з `DimensionRepository.for_two_periods` (task-16);
  - спрацювання: відносна зміна ≥ порогу (**default 20%**) **І** мінімальний обсяг у базовому періоді (default ≥ 50 impressions або ≥ 10 clicks) — щоб `1→2` кліки не створювали алерт;
  - дві групи: `declining` (просідання) і `rising` (набирають);
  - **сортування за абсолютним впливом** (втрачені/здобуті кліки), не за відсотком — 5000→4000 важливіше за 2→1;
  - пороги — налаштування (`Settings`), не хардкод;
  - нові сторінки (немає в базовому періоді) — окрема категорія `new`, не «зростання на ∞».
- **`app/api/alerts.py`** (новий): `GET /api/v1/clients/{id}/alerts?period=…` — згруповані `declining`/`rising`/`new` з точними датами обох періодів.
- **Гейт якості**: якщо дані будь-якого з двох періодів `partial`/`truncated` — алерт **не будується**, віддається явний стан «недостатньо даних» із поясненням. Хибний алерт гірший за його відсутність.

**НЕ входить:** UI (task-18), збір даних (task-16), нотифікації/email (поза MVP, PLAN Етап 8).

## Owned paths

```text
app/metrics/alerts.py       # новий
app/api/alerts.py           # новий
app/core/config.py          # ТІЛЬКИ нові налаштування порогів (успадковано від task-01)
tests/unit/metrics/test_alerts.py
tests/unit/api/test_alerts_api.py
```

## Consumes

- `DimensionRepository.for_two_periods(...)` — **task-16** (узгодити сигнатуру на старті).
- `resolve_periods` — task-14.
- `Quality`, DTO — task-02.

## Provides

- `detect_alerts(pages_current, pages_previous, thresholds) -> AlertsResult(declining, rising, new, skipped_reason|None)`.
- Alerts API — база для блоку на сторінці клієнта (task-18).

## Кроки реалізації

1. Узгодити з task-16 форму даних за два періоди.
2. Ядро детекції: пороги, мінімальний обсяг, класифікація, `new`-категорія.
3. Сортування за абсолютним впливом; обмеження кількості (top-N на групу).
4. Гейт `partial`/`truncated` → «недостатньо даних» замість алерту.
5. API + налаштування порогів.

## Тести

- `1 → 2` кліки **не** створює алерт (мінімальний обсяг ріже шум).
- `5000 → 4000` стоїть **вище** за `20 → 10` попри менший відсоток (сортування за абсолютом).
- Сторінка, якої не було в базовому періоді → категорія `new`, а не нескінченне зростання.
- Ділення на нуль/`None` не дає `inf`/краш (§12).
- `partial` дані будь-якого періоду → алертів немає + явна причина.
- Пороги з `Settings` реально впливають на результат.

## Definition of Done / Gate

- [ ] Алерти показують конкретні URL із абсолютним впливом і точними датами обох періодів.
- [ ] Мікрообсяги відсіяні; `new`-сторінки в окремій категорії; жодних `inf`.
- [ ] На `partial`/`truncated` алерт не будується — явний стан «недостатньо даних».
- [ ] Пороги налаштовні, не хардкод.
- [ ] API покрито тестами; чужі зони не зачеплені.

## Ризики

- **Хибні алерти вбивають довіру** до інструменту швидше, ніж їх відсутність. Тому мінімальний обсяг і гейт на `partial` — не «приємні дрібниці», а частина визначення готовності.
- GSC data lag: свіжий період може бути неповним і виглядати як обвал — перевіряти `data_through` перед побудовою.

---

## ✅ Completion report

- **Дата:** 2026-07-22
- **Гілка / комміт:** feat/task-12-frontend @ 3929c7b (робота велась у поточній гілці за рішенням користувача)
- **Статус:** done-with-followups

### Що зроблено

Ядро детекції алертів і API повністю працюють:

- `app/metrics/alerts.py` — чиста функція `detect_alerts(pages_current, pages_previous, thresholds) -> AlertsResult(declining, rising, new, skipped_reason)` за контрактом плану. Класифікує per-page/per-query рядки за два суміжні періоди: `declining`/`rising` за відносною зміною ≥ порогу **і** мінімальним обсягом базового періоду, `new` для сторінок, яких не було в базовому періоді (без ділення на нуль). Сортування — за абсолютним впливом (втрачені/здобуті кліки), не за відсотком. Top-N-обрізаний рядок-залишок (`rank=None`) виключено з класифікації. Гейт якості: будь-який рядок `partial`/`truncated` в будь-якому періоді, або цілком порожній період → `skipped_reason` замість вигаданого алерту.
- `app/api/alerts.py` — `GET /api/v1/clients/{id}/alerts?period=…&dimension=page|query&anchor=…&comparison_mode=…` (той самий набір параметрів періоду, що й `app.api.analytics`, task-16). Читає обидва боки через `DimensionRepository.for_two_periods`, мапить ORM-рядки в `DimensionRow`, повертає точні календарні дати обох періодів завжди (незалежно від наявності даних — PLAN §21.5), 404 на невідомого клієнта.
- `app/core/config.py` — нові налаштування порогів: `alert_min_relative_change_pct` (default `20`), `alert_min_base_impressions` (`50`), `alert_min_base_clicks` (`10`), `alert_top_n_per_group` (`20`). Реально впливають на результат (перевірено тестами), не хардкод у `alerts.py`.
- `app/schemas/api.py` — додано `AlertEntryDTO`/`AlertsResponseDTO` у новій секції в кінці файлу (той самий прецедент, що й task-06/14/16).

### Як зроблено (ключові рішення)

- **`DimensionRow` decoupled від ORM** — `app.metrics.alerts` не залежить від `app.db`, той самий прецедент, що й `app.metrics.aggregate.StoredMetricValue`. API-роутер мапить `GscDimensionMetric` → `DimensionRow` перед викликом чистої функції; це і робить `detect_alerts` тривіально unit-тестованим без БД.
- **Виключення remainder-рядка за `rank is None`, а не за рядковим sentinel-ом `DIMENSION_OTHER_VALUE`.** Останній живе в `app.db.models` (успадкована зона task-16), а `rank=None` — той самий сигнал, задокументований у `DimensionRowDTO`/`GscDimensionMetric`, що не тягне залежність на `app.db` у чистий метричний модуль.
- **`0`-базові кліки → `delta_pct=None`, ніколи `inf`.** Гейт мінімального обсягу вже відсіює шум перед цим, тому запис `0 → N` кліків із достатніми impressions все одно потрапляє в `rising` без відсоткового числа — чесніше, ніж вигадувати "нескінченний" ріст.
- **Порожній період (жодного рядка) трактується як `skipped_reason="missing_period_data"`**, окремо від явного `partial`/`truncated`-гейту з плану. Без цього кожна сторінка нового клієнта без зібраних розмірних даних виглядала б як "усе нове" — оманливо, порушує §12. Це розширення гейту задокументовано тут як свідоме рішення, не сюрприз.
- **`new`-категорія теж проходить мінімальний обсяг** (за поточним періодом, оскільки базового немає) — інакше одна випадкова сторінка з 1 кліком засмічувала б `new` так само, як мікрорух засмічував би `declining`/`rising`.
- **Точні дати обох періодів завжди присутні у відповіді**, навіть коли `skipped_reason` встановлено — на відміну від `app.api.analytics`, де `period_start`/`period_end` стають `None` за відсутності даних. Для алертів дати — результат чистого календарного `resolve_periods`, не залежать від того, що зібрано (PLAN §21.5: "показуються точні дати обох періодів").
- Головні файли:
  - `app/metrics/alerts.py` — `AlertThresholds`, `DimensionRow`, `AlertEntry`, `AlertsResult`, `detect_alerts`.
  - `app/api/alerts.py` — роутер `GET /clients/{id}/alerts`.
  - `app/core/config.py` — 4 нові поля `Settings.alert_*`.
  - `app/schemas/api.py` — `AlertEntryDTO`, `AlertsResponseDTO` (нова секція в кінці файлу).

### Контрактні зміни (Contract changes)

- **`app/schemas/api.py`** (task-02-власний файл) доповнено секцією `AlertEntryDTO`/`AlertsResponseDTO` — за прецедентом task-06/14/16: task-02 залишив цей response-shape відкритим для owning-таски. Суто адитивно.
- Нова публічна поверхня: `GET /api/v1/clients/{id}/alerts`.
- **`app/core/config.py`** (успадкована зона task-01) доповнено лише 4 новими полями порогів — жодне наявне поле не торкнуте, як і дозволяє власний план цієї таски.

### Як перевірити (verification)

```bash
docker compose exec api poetry run pytest tests/unit/metrics/test_alerts.py tests/unit/api/test_alerts_api.py -v   # 22 passed
docker compose exec api poetry run pytest -q                       # повний unit-набір: 448 passed, 1 skipped, 1 xfailed
docker compose exec api poetry run ruff check .                    # All checks passed!
docker compose exec api poetry run mypy app                        # Success: no issues found in 73 source files
```

Ручна перевірка (стек уже піднятий, `docker compose restart api` для підхоплення нового коду без `--reload`):

```bash
curl "http://localhost:6524/api/v1/clients/<id>/alerts?period=week&dimension=page"
```

Очікується: `declining`/`rising`/`new` відсортовані за абсолютним впливом, `period_current_*`/`period_previous_*` завжди заповнені точними датами, `skipped_reason="missing_period_data"` для клієнтів без зібраних розмірних даних (жива інтеграція збору ще не підключена — успадкований follow-up task-16, нижче).

### Тести

- `tests/unit/metrics/test_alerts.py` (15 тестів): мікрообсяг не створює алерт, гейт по impressions-only, сортування за абсолютним впливом (не відсотком), нова сторінка → `new` без нескінченного росту, поріг обсягу для `new`, `0`-база кліків → `None` замість `inf`, `None`-кліки в рядку не крашать, remainder-рядок (`rank=None`) виключено з класифікації (і окремо — коли обидва періоди складаються лише з remainder), `partial`/`truncated` у будь-якому періоді → `skipped_reason` з причиною, повністю порожній період → `missing_period_data`, пороги з `AlertThresholds` реально змінюють результат (relative change, base volume, top-N).
- `tests/unit/api/test_alerts_api.py` (7 тестів): declining/rising сортування через API, `new`-категорія, мікрообсяг, truncated-гейт із причиною, порожні дані з коректними датами періодів попри `skipped_reason`, `page`/`query` розділені незалежно, 404 на невідомого клієнта.

### Follow-ups / борг

- **Живий збір розмірних даних (task-16) досі не підключено до `app/jobs`** — це успадкований follow-up із task-16, не новий борг цієї таски. `GET /alerts` для будь-якого реального клієнта сьогодні чесно повертає `skipped_reason="missing_period_data"`, доки хтось не додасть виклик `GscAdapter.collect_dimensions()` у `app/jobs/runner.py` (task-05 зона).
- **`AISeoTools`-джерело даних для GSC data lag / `data_through`** (ризик, зазначений у плані вище) не перевіряється окремо тут — `Quality.partial`/`truncated` на рівні рядка з task-16 вважається достатнім сигналом; окремий `data_through`-гейт не додавався, оскільки `GscDimensionMetric` не несе цього поля (поза owned paths цієї таски).
- Тест `tests/unit/api/test_openapi_snapshot.py` вимагав регенерації `tests/unit/api/data/openapi_snapshot.json` після додавання нового ендпоінта — зроблено в цьому ж коміті.
