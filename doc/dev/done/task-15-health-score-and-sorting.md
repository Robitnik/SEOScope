# task-15 — Health % клієнта + сортування дашборду за якістю даних

- **Пакет:** композитний health-показник, детермінований порядок клієнтів, розширення dashboard API
- **Хвиля:** E (v2)
- **Depends on:** task-14 (для health на розширених періодах — але week/month працює одразу)
- **Blocks:** task-19 (картка показує health і sort-контрол), task-18 (сторінка клієнта)
- **PLAN.md:** **§21.2 (health %), §21.3 (сортування)**, §12 (correctness), §10 (API)

## Мета

Дати оператору два бракуючі сигнали: **один композитний health %** по всіх сервісах і **осмислений порядок карток**. Зараз сортування в `app/api/dashboard.py` немає взагалі, тому клієнти без під'єднаних сервісів випадково опиняються вгорі — саме те, що треба виправити.

## Обсяг

**Входить:**
- **`app/metrics/health.py`** (новий) — health % за §21.2:
  - у розрахунок входять **лише сервіси зі `complete`** (не `partial/stale/unavailable/not_configured`);
  - метрика з повними current+previous → бал 0..100, де **50 = без змін**; напрямок з `better_direction`; відносна зміна клампиться (±50% → 0/100), щоб викиди не ламали шкалу;
  - **Ahrefs Site Health** входить напряму як абсолютний 0..100 (не через дельту);
  - sub-score сервісу = середнє його метрик; health % = **зважене середнє** sub-scores (ваги в catalog);
  - `coverage = contributing / configured`; якщо `contributing == 0` → health `None` (UI показує `—`), **ніколи `0`**.
- **Сортування** (§21.3): default `quality` = `coverage ↓ → quality rank ↓ → freshness ↓ → name ↑`; `not_configured`/без даних **завжди в кінці**. Плюс явні режими: `health`, `name`, `last_sync`, `biggest_drop`.
- **API**: `GET /api/v1/dashboard` отримує `sort=` (default `quality`); `ClientCardDTO` отримує `health` (`value: Decimal | None`, `coverage_contributing`, `coverage_configured`).
- Ваги сервісів/метрик — у metric catalog (task-04 зона: додати поле `health_weight` до `MetricSpec` — **контрактна зміна**, узгодити).

**НЕ входить:** UI (task-19), розмірні дані/алерти (task-16/17).

## Owned paths

```text
app/metrics/health.py          # новий
app/api/dashboard.py           # sort + health у відповіді
app/schemas/api.py             # health-поля, sort-параметр (+ CoverageDTO — узгодити з task-14)
tests/unit/metrics/test_health.py
tests/unit/api/test_dashboard_sorting.py
```

## Consumes

- `CATALOG`/`MetricSpec`, `compute_delta`, `assess_quality` — task-04.
- `Quality`, `CellStatus`, DTO — task-02.
- Репозиторії метрик/снапшотів — task-03.
- `resolve_periods`/`aggregate_metrics` — task-14 (для quarter/year/custom).

## Provides

- `compute_health(client_summaries, catalog) -> HealthResult(value|None, contributing, configured)`.
- `sort_clients(cards, mode) -> list[ClientCardDTO]` — детермінований, стабільний.
- Розширений dashboard-контракт для task-19.

## Кроки реалізації

1. `health.py`: нормалізація балів, кламп, `better_direction`, окремий шлях для Ahrefs Site Health.
2. Агрегація в sub-scores + зважене середнє + coverage.
3. Сортування: чисті ключі-функції, стабільність, `not_configured` в кінець.
4. API: `sort=`, health у DTO; оновити OpenAPI-snapshot.

## Тести

- Спад Ahrefs Errors **підвищує** health (напрямок `down`), спад Clicks — знижує.
- Метрика без previous або `partial` — **не** входить у розрахунок і не занижує бал.
- Жоден сервіс не `complete` → health `None`, UI-контракт `—`, **не `0`**.
- Викид (+500% трафіку) не ламає шкалу (кламп).
- Coverage рахує лише **налаштовані** сервіси (`not_configured` не псує знаменник несподівано — зафіксувати поведінку тестом).
- Сортування: клієнт без сервісів завжди останній; порядок детермінований і стабільний при рівних ключах.

## Definition of Done / Gate

- [ ] Health % рахується лише з `complete`-даних; за їх відсутності — `None`, ніколи `0`.
- [ ] Напрямок метрики (`better_direction`) визначає знак внеску, а не знак числа.
- [ ] Coverage видно поруч зі значенням (`72% · 3/4`).
- [ ] Клієнти без під'єднаних сервісів **більше не вгорі** — default sort `quality`.
- [ ] Сортування детерміноване й стабільне; усі режими покриті тестами.
- [ ] OpenAPI-snapshot оновлено, тести task-06 зелені.

## Ризики

- Health % легко перетворити на «красиве, але брехливе» число. Головний запобіжник — **вхід лише `complete`** і явний coverage поруч.
- Ваги в catalog — продуктове рішення; почати з рівних і задокументувати в completion report.

---

## ✅ Completion report

- **Дата:** 2026-07-22
- **Гілка / комміт:** `feat/task-12-frontend` @ `3929c7b` (робоче дерево; коміт — окремим кроком на прохання користувача)
- **Статус:** done

### Що зроблено

- `app/metrics/health.py` — `compute_health()`: композитний health % по `ProviderSummaryDTO`, і `sort_clients()` — п'ять режимів сортування дашборду (`quality` за замовчуванням, `health`, `name`, `last_sync`, `biggest_drop`).
- `GET /api/v1/dashboard` повертає `health` на кожній картці та приймає `sort=` (default `quality`).
- Клієнти без під'єднаних сервісів більше не спливають угору — coverage-ratio `0` природно опиняється в кінці descending-сортування, без окремого спецвипадку.

### Як зроблено (ключові рішення)

- **Скоринг метрики (0..100, 50 = без змін):** relative change з `current`/`previous`, знак задає `better_direction` (крім `neutral` — такі метрики виключені з розрахунку повністю, бо для них немає поняття «краще»), кламп ±50% → 0/100. `ahrefs_health_score` — єдиний виняток, входить як абсолютне значення (вже 0..100), а не через дельту; список таких метрик — приватна константа `_ABSOLUTE_SCORE_METRICS` у `health.py`, не в catalog (мінімізує контрактну поверхню).
- **Coverage:** `configured` рахує провайдери з `state != "not_configured"`; `contributing` — ті, що дали хоча б одну скорувану метрику зі `state == "complete"`. `not_configured` ніколи не потрапляє в знаменник (навмисно, per DoD).
- **`health_weight` на `MetricSpec`** (`app/schemas/metrics.py`) — адитивне поле з дефолтом `1.0`, як попередньо узгоджено в самому плані таски (task-04 зона). Каталог (`app/metrics/catalog.py`) не чіпався — старт з рівних ваг для кожної метрики це і дає без додаткових правок.
- **Сортування:** кожен режим — окремий стабільний `sorted()`-прохід з tie-break на `name` (casefold), без збірного tuple-ключа зі змішаним напрямком по всіх режимах одразу — так простіше читати й тестувати. Default `quality`: `-coverage_ratio → -quality_rank → freshness_key → name`; `quality_rank` мапить `overall_status` (`ok=3 > stale=2 > attention=1 > failed_all=0`).
- **`biggest_drop`** — по «найгіршій» окремій метриці клієнта (мінімум з тих самих per-metric скорів, без прив'язки до провайдер-гейту `complete`-стану, бо просідання може ховатись у будь-якій скорованій метриці); визначення продуктове, задокументовано тут, а не виведене з PLAN буквально.
- Головні файли: `app/metrics/health.py` (нова логіка), `app/api/dashboard.py` (`sort=`, `health` у відповіді), `app/schemas/api.py` (`ClientHealthDTO`, `DashboardSortMode`), `app/schemas/metrics.py` (`health_weight`), `app/metrics/__init__.py` (експорт `compute_health`/`sort_clients`/`HealthResult`, за конвенцією модуля).
- **Відхилення від плану:** тестовий файл `tests/unit/metrics/test_health.py` перейменовано на `tests/unit/metrics/test_health_score.py` — під тим самим базовим ім'ям `test_health.py` уже існує `tests/unit/api/test_health.py` (тест `/healthz`, task-01), і pytest без пакетних `__init__.py` в `tests/unit/**` колізує однакові basenames між модулями (`import file mismatch`). Перейменування — мінімальна правка в межах owned paths цієї таски, не чіпає спільну test-інфраструктуру.

### Контрактні зміни (Contract changes)

- `ClientCardDTO` отримав обов'язкове поле `health: ClientHealthDTO` (`value: Decimal | None`, `coverage_contributing: int`, `coverage_configured: int`) — споживачі: task-12/19 (фронт), task-13 (contract/OpenAPI snapshot-и).
- `GET /api/v1/dashboard` отримав query-параметр `sort: DashboardSortMode = "quality"` (`"quality" | "health" | "name" | "last_sync" | "biggest_drop"`).
- `MetricSpec` (`app/schemas/metrics.py`, task-04 зона) отримав адитивне поле `health_weight: float = 1.0` — не ламає наявні виклики конструктора (дефолт).
- `app/metrics/__init__.py` (task-04 зона) — додано реекспорт `compute_health`, `sort_clients`, `HealthResult`, за вже наявною конвенцією модуля («Import from here, not from the submodules directly»).
- OpenAPI- та schema-contract snapshot-и (`tests/unit/api/data/openapi_snapshot.json`, `tests/unit/schemas/data/contract_snapshot.json`) регенеровано під нову форму.

### Як перевірити (verification)

```bash
AISEOTOOLS_PATH=/home/elon/dev/seo/AISeoTools docker compose run --rm api poetry run pytest tests/unit -q
AISEOTOOLS_PATH=/home/elon/dev/seo/AISeoTools docker compose run --rm api poetry run ruff check app/metrics/health.py app/api/dashboard.py app/schemas/api.py app/schemas/metrics.py
AISEOTOOLS_PATH=/home/elon/dev/seo/AISeoTools docker compose run --rm api poetry run mypy app
```

Очікується: `384 passed`, ruff `All checks passed!`, mypy — 0 помилок у зачеплених файлах (лишається 1 передіснуюча помилка в `app/db/repositories/dimensions.py:207`, зона task-16, не зачеплена цією таскою).

Вручну: `GET /api/v1/dashboard` і `GET /api/v1/dashboard?sort=health` — картки містять `health.value`/`health.coverage_*`, порядок клієнтів змінюється відповідно до `sort`.

### Тести

- `tests/unit/metrics/test_health_score.py` — `compute_health()`: напрямок метрики (спад Errors покращує, спад Clicks погіршує), виключення `partial`/без `previous`/`neutral`-метрик без заниження бала, `None` замість `0` коли жоден сервіс не `complete`, кламп викиду (+500%), `ahrefs_health_score` як абсолютне значення, coverage рахує лише налаштовані сервіси.
- `tests/unit/api/test_dashboard_sorting.py` — наскрізно через `GET /api/v1/dashboard`: default `quality` ставить клієнта без сервісів останнім і впорядковує за coverage; `sort=name/last_sync/health/biggest_drop`; детермінованість/стабільність default-сортування на рівних ключах (tie-break за назвою, не за порядком вставки).

### Follow-ups / борг

- Ваги сервісів/метрик (`health_weight`) зараз рівні (`1.0`) для всіх — продуктове рішення про диференціацію (наприклад, GSC vs Ahrefs) відкладено; поле вже є, зміна — суто дані в catalog, без зміни шейпу.
- `biggest_drop` — власна інтерпретація «найгіршої метрики», не описана в PLAN буквально; варто звірити з продуктом, якщо очікування інші (наприклад, зважувати за обсягом, а не лише напрямком).
