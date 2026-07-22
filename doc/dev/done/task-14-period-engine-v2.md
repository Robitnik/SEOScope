# task-14 — Period engine v2 (квартал, рік, кастом + агрегація збереженої історії)

- **Пакет:** розширення period engine та новий агрегатор метрик над збереженою історією
- **Хвиля:** E (v2)
- **Depends on:** — (розширює завершені task-04 / task-02)
- **Blocks:** task-15 (health на розширених періодах), task-16, task-17, task-18 (сторінка клієнта)
- **PLAN.md:** **§21.1 (розширені періоди + правила агрегації)**, §3.1, §12 (correctness)

## Мета

Дати системі періоди `quarter | year | custom` **без жодного нового збору даних**: усе рахується агрегацією вже збереженої історії. Нуль API units, нуль browser-сесій. Ціна цього рішення — неповне покриття, яке рушій зобов'язаний виявляти й чесно позначати, а не маскувати.

## Обсяг

**Входить:**
- `PeriodKind` розширюється до `week | month | quarter | year | custom` (**контрактна зміна**, див. нижче).
- `resolve_periods` вчиться:
  - `quarter` — останній повний календарний квартал проти попереднього;
  - `year` — останній повний календарний рік проти попереднього;
  - `custom` — довільні `start`/`end`; previous = діапазон **тієї самої довжини**, що безпосередньо передує current.
- Локалізовані підписи діапазонів для нових видів (`Q3 2026`, `2025`, `1 квіт – 15 трав 2026`).
- **Новий `app/metrics/aggregate.py`** — агрегація збережених `metric_values` у запитаний діапазон за правилами §21.1:
  - адитивні (clicks, impressions, users, conversions) — сума підперіодів;
  - **CTR = Σclicks / Σimpressions** (ніколи не середнє від CTR);
  - **avg position** — середнє, зважене за impressions, з `is_derived=True`;
  - snapshot-метрики (Ahrefs Health, Semrush AS) **не сумуються** — останній valid snapshot у діапазоні; previous — останній у попередньому;
  - **coverage-звіт**: скільки днів діапазону реально покрито збереженими даними → `quality=complete|partial`, плюс структура `CoverageDTO(days_covered, days_total, gaps)`.

**НЕ входить:** новий збір під довільні діапазони (свідомо — §21.1), UI (task-18/19), health % (task-15).

## Owned paths

```text
app/metrics/periods.py        # розширення
app/metrics/aggregate.py      # новий
app/schemas/enums.py          # PeriodKind (контрактна зміна)
tests/unit/metrics/test_periods_v2.py
tests/unit/metrics/test_aggregate.py
```

## Контрактна зміна (обов'язково узгодити)

`PeriodKind` — спільний enum із завершеної task-02. Розширення **адитивне** (нові значення, старі не змінюються), тому week/month-споживачі не ламаються. Але:

- оновити contract-snapshot (`tests/unit/schemas/data/contract_snapshot.json`) і OpenAPI-snapshot (task-06) — інакше їхні тести впадуть;
- `CoverageDTO` — новий публічний тип; додати в `app/schemas/api.py` **узгоджено з task-15**, щоб не було колізії у файлі.

## Consumes

- `MetricSpec`/`CATALOG`, `Quality`, `PeriodRole`, `DateRange` — task-02/04.
- `MetricRepository.history(...)` — task-03 (можливо, знадобиться метод вибірки за діапазоном; запит до task-03-зони — оформити як контрактну зміну).

## Provides

- `resolve_periods(kind, anchor, tz, mode, custom_start=None, custom_end=None) -> Periods`.
- `aggregate_metrics(client_id, provider, range, catalog) -> (list[MetricValueResult], CoverageDTO)`.

## Кроки реалізації

1. Розширити `PeriodKind` + `resolve_periods` (quarter/year/custom) з локалізованими підписами.
2. Написати агрегатор з правилами §21.1 — **спершу тести на CTR/position**, бо це найпоширеніше джерело хибних цифр.
3. Coverage-детекція (дірки в історії) → `partial` + `CoverageDTO`.
4. Оновити contract/OpenAPI snapshots.

## Тести

- Межі кварталу/року (перехід року, високосний), `custom` з непарною довжиною, previous тієї ж довжини.
- **CTR агрегації**: Σclicks/Σimpressions ≠ mean(CTR) — тест із навмисно різними обсягами.
- **Position**: зважене за impressions ≠ просте середнє; `is_derived=True`.
- Snapshot-метрики не сумуються; береться останній у діапазоні.
- Дірка в історії → `partial` + правильний `days_covered`; повне покриття → `complete`.
- Порожня історія → `unavailable`, **не `0`**.

## Definition of Done / Gate

- [ ] `quarter`/`year`/`custom` дають коректні current/previous і людські підписи.
- [ ] Агрегація не сумує snapshot-метрики й не усереднює CTR/position наївно.
- [ ] Неповне покриття завжди `partial` + `CoverageDTO`; ніколи не подається як `complete`.
- [ ] Порожня історія → `unavailable`, не нуль.
- [ ] Contract/OpenAPI snapshots оновлені; тести task-02/06 зелені.
- [ ] Нуль нових мережевих викликів (модуль чистий, без I/O крім репозиторію).

## Ризики

- Наївне середнє CTR/position — головна пастка; закрити тестами до реалізації.
- Спокуса «домалювати» відсутні дні нулями — заборонено (§12): дірка це `partial`, а не нуль.

---

## ✅ Completion report

- **Дата:** 2026-07-22
- **Гілка / комміт:** `feat/task-12-frontend` @ working tree (уся backend-частина `app/` у цьому дереві ще не закоммічена — див. «Follow-ups»)
- **Статус:** done

### Що зроблено

- `PeriodKind` розширено **адитивно** до `week | month | quarter | year | custom`.
- `resolve_periods` рахує quarter/year/custom у `closed` і (для quarter/year) у `to_date`; custom має previous **тієї самої довжини** одразу перед current.
- Локалізовані підписи: `Q2 2026 vs Q1 2026`, `2025 vs 2024`, `1 квіт – 15 трав 2026 vs 15 лют – 31 бер 2026`.
- Новий `app/metrics/aggregate.py` — агрегація вже збереженої історії у довільний діапазон за §21.1: суми, `CTR = Σclicks/Σimpressions`, зважена за impressions позиція, snapshot-метрики без сумування, чесний coverage-звіт.
- Новий публічний тип `CoverageDTO` (`days_total`, `days_covered`, `gaps`, `quality`).
- **Нуль нового збору**: модуль чистий, без I/O — рядки історії йому передає викликач.

### Як зроблено (ключові рішення)

- **`aggregate.py` не ходить у БД.** Замість `MetricRepository` він приймає `list[StoredMetricValue]` — DTO «рядок `metric_values` + `observed_at` свого снапшота» (сам `metric_values` `observed_at` не має, він на `provider_snapshots`). Так `app/metrics` лишається чистою бібліотекою, а task-03 не треба чіпати заради нового методу вибірки. Тому в сигнатурі немає сесії, але лишились `client_id`/`provider`: набір рядків можна прочитати один раз на весь дашборд і агрегувати клітинку за клітинкою.
- **Один спільний набір підперіодів на всю клітинку.** Збережена історія перекриває сама себе: один і той самий тиждень пишеться як `current` одним джобом і як `previous` наступним, а поверх тижнів може лежати місяць. Тому спершу будується **непересічне покриття** (раніший старт → довший діапазон при однаковому старті), і **всі** метрики сумуються по одному й тому ж набору — саме це тримає похідні значення консистентними (Σclicks і Σimpressions завжди по однакових днях).
- **Дедуплікація повторних збирань:** для однакового `(metric, period)` перемагає найновіший `observed_at`, при рівності — краща quality, потім не-derived.
- **Часткове перекриття діапазону не «підрізається».** Тиждень 30 бер – 5 квіт не входить у квартальний підрахунок і не пропорціюється: розділити його кліки по межі кварталу неможливо, тож його дні лишаються дірою. Краще чесна діра, ніж вигадане число.
- **Похідні:** `gsc_ctr` рахується з сум компонентів (fallback на збережене значення — лише коли діапазон покритий рівно одним підперіодом, де агрегації взагалі немає). `gsc_avg_position` — середнє, зважене за `gsc_impressions` (`WEIGHTED_AVERAGE_RULES`), `is_derived=True`; **без ваг метрика йде як `unavailable`, а не як наївне середнє**.
- **Метрика без правила агрегації** (unit `percent/ratio/position/score` і немає ні ratio-, ні weighted-правила) → `unavailable`. Сумувати відсотки заборонено навіть «тимчасово».
- **Snapshot-метрики:** останній valid снапшот, чий `observed_at` потрапляє в діапазон; `PeriodRole.snapshot`, `period_start/end = None` — щоб `ensure_comparable_periods` лишалась вірною. Для snapshot-only провайдерів (Ahrefs, Semrush) day-coverage не має сенсу, тому coverage там означає «чи був у діапазоні придатний снапшот».
- **Coverage:** `days_covered`, точні `gaps` (максимальні непокриті підвідрізки) і `quality`: `complete` лише за нульових дірок, `unavailable` — коли не покрито нічого. Quality метрики = найгірше з (quality рядків, повнота по підперіодах, coverage).
- **Підписи:** скорочення `Q3 2026` / `2025` вживаються **лише** у `closed`, бо в `to_date` вони б заявляли повний квартал/рік, якого ще немає; там і в `custom` — явні дати.
- Головні файли: `app/metrics/periods.py` — розширення періодів і підписи; `app/metrics/aggregate.py` — агрегатор і `CoverageDTO`-звіт; `app/schemas/enums.py` — `PeriodKind`; `app/schemas/api.py` — `CoverageDTO`.

### Контрактні зміни (Contract changes)

1. **`PeriodKind`** — додано `quarter | year | custom`. Зміна адитивна: `week`/`month` не змінили ні значення, ні семантику, тож наявні споживачі (task-05/06/12) не ламаються.
2. **`CoverageDTO`** — новий публічний тип у `app/schemas/api.py` (окрема секція в кінці файлу, щоб не колідувати з правками task-15), експортований з `app/schemas/__init__.py`.
3. **`resolve_periods`** — два нові **keyword-only** аргументи з дефолтами: `custom_start`, `custom_end`. Позиційні виклики не зачеплені.
4. Оновлено `tests/unit/schemas/data/contract_snapshot.json` (+ `CoverageDTO` у `_MODELS`), `tests/unit/api/data/openapi_snapshot.json` і `tests/unit/schemas/test_enums.py::test_period_kind_values`.

Споживачі: task-15 (health на розширених періодах + `CoverageDTO`), task-16/17, task-18 (сторінка клієнта).

### Як перевірити (verification)

```bash
docker compose exec api poetry run pytest tests/unit/metrics/test_periods_v2.py tests/unit/metrics/test_aggregate.py -q
docker compose exec api poetry run pytest tests/unit --confcutdir=tests/unit -q   # 348 passed
docker compose exec api poetry run ruff check app
docker compose exec api poetry run mypy app
```

Перевіряльник має побачити: 46 нових тестів зелені, увесь `tests/unit` зелений, lint і mypy без зауважень.

> `pytest` без `--confcutdir` зараз падає на `tests/conftest.py` (`ModuleNotFoundError: No module named 'tests.harness'`) — це нова, ще не дороблена спільна фікстура **task-13**, не пов'язана з цією таскою. Причина: пакет `tests` із примонтованого `AISeoTools` перекриває неймспейс-пакет `/app/tests` (звичайний пакет завжди виграє в неймспейсного, незалежно від порядку `sys.path`), тож `tests/harness` потребує або `tests/__init__.py`, або імпорту без префікса `tests.`.

Ручна перевірка агрегації (без БД):

```bash
docker compose exec api poetry run python -c "
from datetime import date, datetime, timezone
from decimal import Decimal
from app.metrics import aggregate_metrics, StoredMetricValue
from app.schemas import DateRange, Provider
rows=[StoredMetricValue(client_id='c', provider=Provider.gsc, metric_key=k, period_role='current',
      period_start=s, period_end=e, value_numeric=Decimal(v), unit='count', quality='complete',
      observed_at=datetime(2026,4,30,tzinfo=timezone.utc))
      for k,s,e,v in [('gsc_clicks',date(2026,4,1),date(2026,4,7),100),
                      ('gsc_impressions',date(2026,4,1),date(2026,4,7),1000),
                      ('gsc_clicks',date(2026,4,8),date(2026,4,14),10),
                      ('gsc_impressions',date(2026,4,8),date(2026,4,14),10000)]]
m,c = aggregate_metrics(rows, client_id='c', provider=Provider.gsc,
                        date_range=DateRange(start=date(2026,4,1), end=date(2026,4,28)))
print({x.metric_key:(x.value_numeric, x.quality) for x in m}); print(c)"
```

Очікувано: `gsc_ctr = 1.00` (а не 5.05 — середнє від CTR), `quality=partial`, `days_covered=14 з 28`, одна діра `15–28 квіт`.

### Тести

`tests/unit/metrics/test_periods_v2.py` (20) — межі кварталу (перехід року, високосний Q1, граничні дні), рік у локальному tz, custom (довжина previous, один день, перехід року, ігнорування anchor, помилки валідації), `to_date` для quarter/year із клампом, чесність підписів.

`tests/unit/metrics/test_aggregate.py` (26) — суми; **CTR = Σ/Σ ≠ mean(CTR)**; **зважена позиція ≠ просте середнє** + `is_derived`; позиція без ваг → `unavailable`; snapshot береться останній у діапазоні й не сумується; снапшот поза діапазоном → `unavailable`; діра → `partial` + точні `gaps`/`days_covered`; дірки з обох боків; частково перекритий рядок не рахується; порожня історія → `unavailable`, не нуль; збережений `None` не покриває свої дні; найгірша quality перемагає; повторний збір і `current`/`previous` того самого тижня рахуються один раз; місяць поверх тижнів без подвійного рахунку; фільтрація чужих клієнтів/провайдерів; range-метрика без правила агрегації не сумується.

### Follow-ups / борг

- **Гілка/комміт:** у цьому робочому дереві `app/**` і `tests/unit/**` ще ніде не закоммічені (жодна гілка їх не має), тому окремий атомарний комміт лише по owned paths дав би зламаний HEAD. Рішення про обсяг комміту — за користувачем.
- `PeriodKind.quarter|year|custom` **свідомо не підключені до `GET /api/v1/dashboard`** (§21.1: розширені періоди — на сторінці клієнта). Зараз ендпоінт прийме `period=quarter` і поверне порожні дані, а `period=custom` без меж дасть `ValueError` → 500. Валідацію «дашборд приймає лише week/month» і підключення агрегатора має зробити власник роутера — `TODO(task-18)` спільно з task-06.
- `MetricRepository` не має вибірки за діапазоном із `observed_at`; поки що склеювання `metric_values` × `provider_snapshots` — на боці викликача. Якщо task-15/18 захочуть це в репозиторії — запит до task-03.
- Docstring-команда регенерації снапшотів (`python -m tests.unit...`) не працює через ту саму колізію пакета `tests`; запускати як `poetry run python tests/unit/schemas/test_contract_snapshot.py`. Виправлення тексту — у зоні task-06/13.
