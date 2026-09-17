# task-98 — GA4 Events by Month: усі івенти за обрані місяці, 1 місяць = 1 вкладка

**Depends on:** 27 ✅ (Tools hub), 83 ✅ (прямий Google API transport), 71 ✅ (auth), 06 ✅ (REST)
**Owned paths:**
`app/tools/ga4_events/**` (новий пакет: `service.py`, `xlsx.py`, `__init__.py`),
`app/api/tools_ga4_events.py` (новий),
`app/schemas/ga4_events.py` (новий),
`app/db/repositories/ga4_event_reports.py` (новий),
`migrations/versions/<rev>_ga4_event_reports_task_98.py` (новий),
адитивно `app/db/models.py` (лише новий клас `Ga4EventMonthReport`),
адитивно `app/api/tools.py` (один запис у реєстрі + stat-функція),
адитивно `app/audit/actions.py` (один запис),
`frontend/js/ga4-events-tool.js` (новий), `frontend/css/ga4-events.css` (новий),
адитивно `frontend/js/{tools-page,api,router}.js`, `frontend/index.html`,
`tests/unit/tools/ga4_events/**`, `tests/unit/api/test_tools_ga4_events.py`,
`tests/unit/api/data/openapi_snapshot.json`,
`tests/e2e/frontend/tools-ga4-events.cjs`.

**Міграція:** одна, `op.create_table` напряму (жодного `batch_alter_table` — CLAUDE.md).
Перед `alembic upgrade head` на бойовій БД — бекап.

---

## 1. Дослідження (заміряно живо 2026-09-17, прямі виклики GA4 Data API з контейнера `api`)

### 1.1 Джерело даних: прямий Google Analytics Data API, не Reporting Ninja

У БД **14 клієнтів** мають інтеграцію `reporting_ninja`, і в кожного
`config_json["account_id"]` уже лежить у форматі Google API (`properties/<id>`) —
мапити нічого не треба (те саме, що зафіксував task-83 §0.3):

```
19CLV properties/279498125   GOREPORT properties/390203949   PJC    properties/448557831
3XE   properties/386413826   GRP      properties/489349991   SAPHNY properties/428527008
ALPHA properties/399539189   HWELL    properties/395442288   SDS    properties/309651330
EFFYS properties/513073109   LAXTON   properties/444266072   SHB    properties/402129482
NYCHA properties/355994287   SWM      properties/482139304
```

`transport=google_api` стоїть **лише** в HWELL — але тул це поле **ігнорує навмисно**:
`transport` вирішує, чим наповнюються *збережені* метрики (`#/trends`, звіти), а тул —
жива відповідь на разове питання. Перевірено предметно: один місячний запит пройшов
**на всіх 14 property** з одним портфельним refresh-токеном, 0.22–0.82 с кожен:

```
19CLV OK rows=36 events=12 0.75s | LAXTON OK rows=57 events=23 0.52s
3XE   OK rows=70 events=25 0.82s | NYCHA  OK rows=26 events= 9 0.65s
ALPHA OK rows=34 events=14 0.62s | PJC    OK rows=81 events=27 0.80s
EFFYS OK rows=26 events= 9 0.69s | SAPHNY OK rows=95 events=34 0.71s
GOREPORT OK rows=45 events=15 0.60s | SDS  OK rows=38 events=13 0.60s
GRP   OK rows=27 events= 9 0.58s | SHB    OK rows=73 events=28 0.22s
HWELL OK rows=43 events=16 0.40s | SWM    OK rows=25 events= 9 0.69s
```

Наслідок для дизайну: **RN-квота не витрачається взагалі** (пастка з пам'яті task-89:
відкриття нового періоду на сторінці клієнта коштує RN-квоти), і тул не залежить від
того, ввімкнений у клієнта `transport` чи ні.

### 1.2 Що саме GA4 віддає по івентах і які метрики сумісні з `eventName`

Дименсія `eventName`, бісекція по метриках (HWELL, серпень 2026):

| Набір метрик | Результат |
|---|---|
| `eventCount` | OK, 15 рядків, 0.86 с |
| `eventCount, keyEvents, totalUsers` | OK, 15 рядків, 0.51 с |
| `eventCount, keyEvents, totalUsers, sessions` | OK, 15 рядків, 0.44 с |
| +`eventValue, eventCountPerUser, activeUsers, newUsers, engagedSessions, eventsPerSession` (10) | OK, 0.38 с |

Тобто ліміт 10 метрик на `runReport` (task-88 §0.8) тут не тисне — беремо чотири
несучі: `eventCount`, `keyEvents`, `totalUsers`, `sessions`. `newUsers`/`eventValue`
для більшості івентів — структурні нулі (`newUsers=0` на всьому, крім `first_visit`),
у таблицю не йдуть: колонка нулів ≠ інформація.

Кардинальність мала й безпечна: **9–34 різні `eventName` на клієнта**, з них
**2–27 ключових** (`keyEvents > 0`):

```
SHB   31 івент / 27 ключових   3XE 30 / 23   SDS 15 / 11
HWELL 16 / 7                   GRP 10 / 6    ALPHA 14 / 2
```

Ключові івенти — це буквально бізнес-дії клієнта (`Order Now Brooklyn`,
`Catering 47th street`, `Insurance Form`, `Phone`, `purchase`), тому таблиця
«усі івенти + підсвічені ключові» одразу читається оператором.

### 1.3 Місяці: один запит `yearMonth × eventName` = по-місячні запити, байт-у-байт

Перевірено на HWELL, 2026-06…2026-08, `eventCount` і `keyEvents` на кожну пару:

```
A: 3 окремі по-місячні запити  → 2.66 с, 14/14/15 рядків
B: 1 запит yearMonth×eventName → 0.50 с, 43 рядки
порівняння A↔B: 2026-06 mismatches=0, 2026-07 mismatches=0, 2026-08 mismatches=0
```

Це **знімає** пастку «дименсія `month` малює брехливі нулі» (пам'ять RN + докстрінг
[ga4_client.py:1](../../app/integrations/google_api/ga4_client.py#L1)): та пастка про
**неперервність денного ряду** в `ga4_daily`, а не про місячне групування зрізу.
`yearMonth` тут агрегує точно так само, як окремий запит на кожен місяць.

### 1.4 Історія глибша за 14 місяців доступна

SDS, запит 2023-01…2024-12 з дименсією `yearMonth`: повернулось **24 місяці**
(`202301`…`202412`, напр. `202412: eventCount=29794, totalUsers=2077`). Тобто
ретенція подієвих даних GA4 не ріже цей зріз — вибір місяців у формі можна відкривати
на всю історію property, а не на 14 місяців.

### 1.5 Латентність: холодний запит по «важкому» property — 46 секунд

Найдорожче вимірювання дослідження (SAPHNY, 34 івенти, 24 місяці):

```
24m yearMonth×eventName, холодний   : 616 рядків за 46.27 с   ← небезпека
той самий запит повторно            : 616 рядків за  1.11 с   (кеш GA4)
12 по-місячних запитів, 6 потоків   : 312 рядків за  1.95 с
той самий діапазон одним запитом    : 312 рядків за  4.81 с
для порівняння SHB, 24m одним       : 542 рядки  за  1.99 с
```

`google_api_timeout_seconds = 60` ([config.py:648](../../app/core/config.py#L648)) —
46 с це 77% бюджету на одному клієнті. Тому **запит робиться на місяць**, у
`ThreadPoolExecutor`: кожен запит малий, жоден не наближається до таймауту, і
паралельно це ще й **удвічі швидше** за один великий (1.95 с проти 4.81 с).
Бонус, який збігається з постановкою «1 місяць = 1 вкладка»: місяць, що впав, стає
своєю вкладкою у стані `failed`, а не валить увесь прогін (PLAN §9.1, застосований
до read-шляху).

### 1.6 Пастка, яка зробила б таблицю брехливою: `totalUsers` НЕ адитивні

SAPHNY, 2026-08 — сума по рядках івентів проти property-тоталу тим самим запитом
без дименсії `eventName`:

```
eventCount : сума по івентах = 364 617   property total = 364 617   ✅ збігається
totalUsers : сума по івентах = 227 237   property total =  58 556   ❌ ×3.9
```

(SHB те саме: 66 024 проти 16 806.) Один користувач тригерить багато різних івентів і
рахується в кожному рядку. Тому:
- рядок **Total** в таблиці/аркуші показує `eventCount` **сумою** (вона точна),
- а `Users`/`Sessions` у рядку Total беруться **окремим запитом** `dims=[yearMonth]`
  (1.0–1.3 с на 24 місяці), ніколи не сумою колонки,
- колонка `Users` у самих рядках лишається «скільки унікальних користувачів тригернули
  ЦЕЙ івент» — це коректна відповідь на своє питання, просто не доданок.

### 1.7 Порядок рядків GA4 не гарантує

Відповідь приходить невпорядкованою (SDS: `202412, 202404, 202407, 202306…`), а
`Ga4Client.run_report` не вміє `orderBys`. Сортуємо в Python — при 9–34 івентах це
безкоштовно; лізти в чужий `ga4_client.py` за `order_bys` немає потреби.

### 1.7a Формат таблиці оператора: три порізи на івент (уточнено мідсесією)

Користувач показав цільову таблицю (скрін GA4, клієнт SDS): рядок на кожну пару
`(івент, поріз)` — `All Users` / `Direct traffic` / `Organic traffic` — з
колонками `Event Count (%)`, `Total Users (%)`, `Event count per user`.
Визначення порізів **визначено вимірюванням**, не здогадом: знайдено місяць,
що збігається зі скріном (липень 2026), і перебрано кандидатів на фільтр.

| Кандидат | Book Now, Organic |
|---|---|
| `sessionDefaultChannelGroup` EXACT `Organic Search` | 70 (**1.22%**), 54 (**8.11%**) |
| `sessionMedium` CONTAINS `organic` | 70 (1.22%), 54 (8.13%) |
| `firstUserDefaultChannelGroup` EXACT `Organic Search` | **88** (0.92%), 65 (9.02%) |
| **`sessionDefaultChannelGroup` CONTAINS `Organic`** | **70 (1.16%), 54 (7.71%)** ✅ |
| Цільові числа зі скріна | 70 (1.16%), 54 (7.71%) |

Тобто поріз ширший за «Organic Search»: у знаменник входять також
`Organic Social` (269 подій) і `Organic Video` (12). Події й користувачі
збігаються в обох варіантах — розходяться лише **відсотки**, тож помилку
неможливо помітити без звірки саме зі знаменником. `Direct` аналогічно —
`CONTAINS "Direct"`.

Відсоток рахується від тоталів **свого** порізу (`eventCount`/`totalUsers`
запиту без дименсії з тим самим фільтром), а `Event count per user` береться
метрикою `eventCountPerUser` з GA4, а не діленням: звірено по всіх рядках
скріна (`1.61 / 1.30 / 1.30`, `1.34 / 1.18 / 1.18`).

Ціна форми: **6 запитів на місяць** (3 порізи × рядки + тотали) замість 2.
Заміряно живо на SDS: один місяць — 1.98 с; `totalUsers` не адитивні між
каналами, тож зліпити порізи з однієї дименсійної вибірки не можна — кожен
поріз мусить мати власний запит.

### 1.8 Побічна знахідка (не наша таска, але варта слова оператору)

SHB: `eventCount` по місяцях `202606: 200 902 → 202607: 240 009 → 202608: 112 274 →
202609 (1–17): 4 900`; денний зріз показує обрив саме близько 2026-08-20
(до: 6–8 тис./день, після: 100–600/день). Тул такі речі показує першим же екраном —
це і є його користь, окремої дії від task-98 не потребує.

---

## 2. Дизайн

### 2.1 Контракт назви

Ключ `ga4-events`, назва в UI **GA4 Events by Month**, маршрут `#/tools/ga4-events`,
детальний — `#/tools/ga4-events/<reportId>` (той самий двосегментний шаблон, що вже
розбирає [router.js:17](../../frontend/js/router.js#L17)). Провайдер у копірайті
називається `GA4` скрізь однаково (CLAUDE.md).

### 2.2 Бек: `app/tools/ga4_events/service.py`

```python
@dataclass(frozen=True)
class EventRow:
    event_name: str
    event_count: int | None
    key_events: int | None
    users: int | None
    sessions: int | None
    is_key_event: bool          # key_events > 0 у ЦЬОМУ місяці

@dataclass(frozen=True)
class MonthResult:
    month: str                  # "YYYY-MM"
    state: str                  # ok | no_data | failed
    is_partial: bool            # місяць ще не закінчився або <48 год від кінця
    rows: list[EventRow]
    totals: MonthTotals | None  # eventCount (сума) + users/sessions (окремий запит)
    error: str | None           # ніколи не str(exc) — лише алловлист з GoogleApiError

def run_events_report(session, *, client_id, months: list[str]) -> ReportResult
```

- Резолв property: `ClientIntegration(provider=reporting_ninja).config_json["account_id"]`.
  Немає інтеграції / немає `account_id` → **весь прогін** `not_configured` з чесним
  текстом («GA4 isn't connected for this client»), не `failed`, не нулі (PLAN §12).
- `GoogleApiConfigError` (немає refresh-токена) → `not_configured`;
  `GoogleApiPermissionError` (немає доступу до property) → окремий текст, бо це інша
  дія оператора (доступи в Google), — як у [google_service.py:1](../../app/urlinspector/google_service.py#L1).
- Один запит на місяць: `dims=["eventName"]`,
  `metrics=["eventCount","keyEvents","totalUsers","sessions"]`; плюс один запит
  `dims=["yearMonth"]` на весь діапазон для тоталів (§1.6).
  `ThreadPoolExecutor(max_workers=min(6, len(months)))`.
- Ізоляція: виняток у місяці → `state="failed"` ЦЬОГО місяця; решта вкладок живі.
- `is_partial`: місяць поточний, або з кінця місяця минуло <48 год (GA4 доуточнює дані) —
  вкладка несе бейдж `Partial`, число не ховається й не «дочищається».
- Похідні поля рахує бек, не фронт: `event_share_pct` / `user_share_pct` — частки від
  тоталів **свого порізу** (§1.7a), `per_user` — метрика GA4 як є. MoM у фінальній
  формі немає: таблиця оператора його не має, а на порізах він потроївся б.

### 2.3 Персистенція: `Ga4EventMonthReport` (одна таблиця, знімок)

Причина зберігати, хоч дані й перезапитуються за секунди: **GA4 переглядає числа**
(~48 год) і property може змінити ключові івенти — посилання на прогін має показувати
те, що бачив оператор, а не сьогоднішній перерахунок (той самий аргумент, що в
`SchemaMarkupCheck`: історія важлива, повторний прогін — новий рядок).

```
ga4_event_month_reports
  id GUID pk | client_id GUID FK clients ondelete=SET NULL | property_id str(64)
  months_json JSON  (["2026-08", ...] як обрав оператор)
  state str(16)     (ok | partial | not_configured | failed)
  rows_json JSON    (список MonthResult у jsonable-формі — знімок відповіді GA4)
  created_by_user_id / created_by_kind (task-93, адитивно як у ContentQaPackage)
  created_at
```

Без `updated_at`: рядок незмінний. Секретів у ньому немає (property id — не секрет).

### 2.4 API (усе під `/api/v1/tools/ga4-events`, роль `member`+, читання `viewer`)

| Метод | Шлях | Що робить |
|---|---|---|
| `GET` | `/clients` | клієнти з GA4-property + `available_months` (від найранішого місяця з даними до поточного) + прапорець `configured` |
| `POST` | `/reports` | `{client_id, months:[...]}` → запускає прогін, повертає повний звіт + `id` |
| `GET` | `/reports` | сторінкована історія (для картки на хабі й списку останніх) |
| `GET` | `/reports/{id}` | збережений знімок (shareable link) |
| `GET` | `/reports/{id}.xlsx` | експорт (StreamingResponse, як [client_monthly.py:427](../../app/api/client_monthly.py#L427)) |

- Ліміт: ≤24 місяці за прогін (при 6 потоках це ≈2–5 с; 46-секундний холодний кейс
  §1.5 структурно неможливий, бо запит завжди на один місяць).
- Мутація одна (`POST /reports`) — один запис в `app/audit/actions.py`:
  `("POST", "/api/v1/tools/ga4-events/reports"): ActionSpec("tool.ga4_events.run")`.
- Мережа блокуюча (`httpx.Client` усередині `GoogleApiSession`) → через
  `run_in_threadpool`, як усі інші тули.

### 2.5 Фронт: вкладка на місяць

```
GA4 Events by Month                                   [Export .xlsx]
Client [ SHB ▾ ]   Months [ Aug 2026 ×][ Jul 2026 ×][ Jun 2026 ×] [+ Add]   ( Run )

┌ Aug 2026 ─┬ Jul 2026 ─┬ Jun 2026 ─┐            ← 1 місяць = 1 таб
│ Aug 2026   112 274 events · 16 806 users · 22 298 sessions   [Partial]
│ Search events…                       ☑ Key events only
│ Event                 Count    Share   Users  Sessions  Key events   MoM
│ ● Order Now Lakewood   2 405    2.1%   1 902     2 010      2 405   +18%
│   page_view           38 643   34.4%  16 806    22 298          —      −53%
└──────────────────────────────────────────────────────────────────────────
```

- Таби — `role="tablist"`/`role="tab"`/`aria-selected`, стрілки ←/→ перемикають
  (AGENTS.md §7). Вкладка `failed` несе свій стан усередині, інші лишаються робочими.
- Ключові івенти — крапка-маркер + чип `Key`, не колір-одинак (AGENTS.md §7:
  колір ніколи не єдиний носій сенсу).
- Обов'язкові стани (AGENTS.md §6): loading-скелет під час прогону, `not_configured`
  («GA4 isn't connected for this client»), `no_data` («No events in this month»),
  `failed` із причиною, empty-стан пошуку.
- CSP: жодного інлайн-`style`, жодного `.style.top/left` з JS (пам'ять task-36/43) —
  усе класами; кожен ES-імпорт з власним `?v=` і бампом усього ланцюга до
  `index.html` (пам'ять про cache-bust chain + кеш Cloudflare на `.js`).
- `api.js`: ключ запиту — явна назва призначення (`ga4-events:run`), не похідна від
  параметрів (пам'ять task-36 — пастка ловилась двічі).

### 2.6 Excel: `Summary` + аркуш на місяць

`app/tools/ga4_events/xlsx.py`, `openpyxl`, будується **з тих самих DTO**, що і JSON
(правило [pageinventory/xlsx.py:1](../../app/pageinventory/xlsx.py#L1)).

- Аркуш 1 `Summary`: матриця `event × month` по `eventCount` (рядок на івент,
  колонка на місяць, сортування за останнім місяцем), ключові івенти зверху й
  виділені; нижче рядок `TOTAL` (`eventCount` сумою; `Users`/`Sessions` — з
  property-тоталів, з приміткою «not additive across events», §1.6).
- Далі аркуш на кожен місяць, назва `2026-08` (ISO — сортується сама, і в Excel
  назва аркуша ≤31 символ безпечно): повні колонки як у UI.
- Порожній/впалий місяць теж отримує аркуш — з одним рядком причини, а не зникає:
  вкладка, якої немає, читається як «даних 0», що неправда.

---

## 3. Етапи

1. **Схема + міграція** (`Ga4EventMonthReport`, `op.create_table`; бекап перед
   застосуванням на проді) + репозиторій.
2. **`app/tools/ga4_events/service.py`** — резолв property, по-місячний фан-аут,
   стани, тотали, MoM, `is_partial`. Юніти на фейковому `Ga4Client`.
3. **API + схеми + аудит-запис**; регенерація `openapi_snapshot.json`.
4. **XLSX** (`Summary` + аркуші місяців), юніти на структуру книги.
5. **Фронт**: сторінка, таби, таблиця, експорт, стани; реєстрація в `tools-page.js`,
   `index.html`, `router.js`.
6. **Живий прогін** через `AGENT_DEV_TOKEN` на щонайменше 3 клієнтах (SHB — 31 івент,
   ALPHA — 2 ключові, EFFYS/SWM — 9 івентів) + e2e Playwright (`.cjs`, `require`).

---

## 4. Definition of Done / Gate

1. `#/tools` показує картку **GA4 Events by Month** зі справжньою статистикою
   (`None`, доки не було прогонів, — не `0 runs`).
2. Вибір клієнта + 1…24 місяців → по вкладці на місяць; перемикання табів клавіатурою.
3. Числа звірені з GA4 UI/API вручну на одному клієнті-місяці (скрін або лог у
   Completion report): `eventCount` по івенту й property-тотал.
4. `Users` ніде не підсумовується по івентах (§1.6) — є тест, який ловить регресію:
   сума колонки ≠ тотал, і тотал приходить з окремого запиту.
5. Клієнт без GA4-property → `not_configured` на всю сторінку; впалий місяць →
   `failed` лише у своїй вкладці, сусідні вкладки з даними.
6. Поточний/несформований місяць позначений `Partial`, числа не приховані.
7. `.xlsx` відкривається в Excel і Google Sheets; аркушів = 1 + кількість обраних
   місяців; `Summary` сходиться з вкладками.
8. Повторне відкриття збереженого посилання `#/tools/ga4-events/<id>` показує ті
   самі числа, що й у момент прогону (знімок, не перерахунок).
9. `ruff check .`, `mypy app`, `pytest` свого модуля + `tests/unit/api` — зелені в Docker.
10. RN-квота не витрачається: у прогоні немає жодного виклику `reporting_ninja.py`
    (перевіряється тестом-шпигуном або логами).
11. AGENTS.md §15–16: візуальний loop, світла/темна тема, 320 px ширина, без інлайн-стилів.

---

## ✅ Completion report

- **Дата:** 2026-09-17
- **Гілка / комміт:** `feat/task-98-ga4-events-by-month` @ `11eb7af`
- **Статус:** done

### Що зроблено

Новий тул **GA4 Events by Month** на `#/tools/ga4-events`: вибір клієнта →
чекбокси місяців, які GA4 реально має → звіт, де **1 місяць = 1 вкладка** в UI
і **1 аркуш** у `.xlsx` (плюс зведений `Summary`). У кожній вкладці — таблиця у формі,
яку показав користувач: **три рядки на кожен івент** (`All Users` /
`Direct traffic` / `Organic traffic`) з `Event Count (%)`, `Total Users (%)` і
`Event count per user`; ключові івенти підсвічені, тотали кожного порізу —
окремим блоком. Прогін
зберігається знімком і відкривається посиланням
`#/tools/ga4-events/<id>`.

### Як зроблено (ключові рішення)

- **Прямий GA4 Data API, а не Reporting Ninja.** `client_integrations`
  (`provider=reporting_ninja`) уже містить `config_json["account_id"]` у
  форматі `properties/<id>` для всіх 14 клієнтів, і портфельний
  refresh-токен відкриває їх усі. `config_json["transport"]` тул **ігнорує**:
  transport вирішує, чим наповнюються збережені метрики, а тул — разове живе
  питання, за яке не має платити RN-квота.
- **Один запит на місяць у пулі потоків** (`max_workers = min(6, місяців)`).
  Причина заміряна: холодний 24-місячний `yearMonth×eventName` по «важкому»
  property — **46 с** при `google_api_timeout_seconds=60`, тоді як 12
  по-місячних запитів — 1.95 с. Побічний ефект збігається з постановкою:
  впалий місяць = своя вкладка `failed`, сусідні живі.
- **`totalUsers` ніде не сумується.** Тотали місяця — окремий запит без
  дименсії `eventName`; сума колонки Users на живих даних перевищує реальний
  property-тотал у 3.9 раза. Це закріплено юнітом, тестом Excel-експорту й
  кроком браузерного смоуку.
- **Порізи звірені з живими числами, а не вгадані** (§1.7a). Фільтр —
  `sessionDefaultChannelGroup` **CONTAINS** `Direct`/`Organic`: EXACT
  `"Organic Search"` дає ті самі 70 подій, але відсотки 1.22%/8.11% замість
  1.16%/7.71%, бо зі знаменника зникають Organic Social/Video. Відсотки —
  від тоталів свого порізу; `Event count per user` — метрика GA4
  `eventCountPerUser`, не наше ділення. Одиниця роботи в пулі — `(місяць,
  поріз)`, тож 3 місяці це 9 паралельних задач; SDS за один місяць — 1.98 с.
- **MoM у фінальній формі немає** — його немає в таблиці оператора, а на трьох
  порізах він потроївся б. Перший чорновик його мав, і він одразу показав, чому
  часткові місяці небезпечні (19CLV, вересень: рівний стовпчик −56%/−55%/−54%,
  що міряв календар, а не трафік); прапорець `Partial` і пояснення лишились.
- Головні файли: `app/tools/ga4_events/service.py` — резолв property, фан-аут
  по місяцях, стани, share/MoM; `app/tools/ga4_events/xlsx.py` — книга з того
  самого DTO, що й екран; `app/api/tools_ga4_events.py` — 5 ендпоінтів;
  `app/schemas/ga4_events.py`; `app/db/repositories/ga4_event_reports.py`;
  `frontend/js/ga4-events-tool.js` + `frontend/css/ga4-events.css`.
- **БД у мережевий шлях не заходить:** `property_id` резолвиться одним
  коротким читанням до походу в Google, далі `run_events_report` — чиста
  мережа. Це навмисна профілактика класу дефекту з CLAUDE.md (фонова робота на
  сесії, чий `DATABASE_URL` резолвиться після відкоту `monkeypatch`).

### Контрактні зміни (Contract changes)

Адитивні. Нова таблиця `ga4_event_month_reports` (міграція `eb9b03cf56b4`,
чистий `op.create_table`, бекап `app_data/backups/pre_task98_ga4_events_2026-09-17.dump`
знято перед застосуванням). Новий роутер `/api/v1/tools/ga4-events/**` і 10
нових DTO — `openapi_snapshot.json` перегенеровано, різниця **лише додавання**
(0 видалених шляхів/схем, звірено множинами). Один запис у реєстрі тулів
(`app/api/tools.py`) і один в `app/audit/actions.py`
(`tool.ga4_events.run`). Наявні контракти не змінені.

### Як перевірити (verification)

```bash
docker compose up -d --build
docker compose exec api poetry run pytest tests/unit/tools/ga4_events tests/unit/api/test_tools_ga4_events.py -q
docker compose exec api poetry run ruff check . && docker compose exec api poetry run mypy app

# живий прогін (dev-токен із .env)
TOKEN=$(grep "^AGENT_DEV_TOKEN=" .env | cut -d= -f2)
curl -s -H "Authorization: Bearer $TOKEN" localhost:6524/api/v1/tools/ga4-events/clients
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"client_id":"<id>","months":["2026-08","2026-07","2026-06"]}' \
  localhost:6524/api/v1/tools/ga4-events/reports

NODE_PATH=/tmp/yovth-e2e/node_modules BASE_URL=http://127.0.0.1:6524 \
  node tests/e2e/frontend/tools-ga4-events.cjs
```

В UI (`#/tools` → GA4 Events by Month): обрати клієнта, дочекатись списку
місяців, натиснути **Build the report**. Перевіряльник має побачити вкладку на
кожен обраний місяць, бейдж `Partial` на поточному місяці з поясненням, чому в
ньому немає MoM, тотали Users/Sessions, ЯВНО менші за суму однойменних колонок,
і `.xlsx` із `Summary` + аркушем на місяць.

Живо підтверджено 2026-09-17: **SDS, липень 2026 — усі числа збігаються зі
скріном користувача рядок у рядок** (`Book Now`: 704 (2.53%) / 437 (11.05%) /
1.61 · Direct 74 (0.61%) / 57 (3.54%) / 1.30 · Organic 70 (1.16%) / 54 (7.71%)
/ 1.30; `Phone`: 82 (0.30%) / 61 (1.54%) / 1.34 · 13 (0.11%) / 11 (0.68%) /
1.18 · 20 (0.33%) / 17 (2.43%) / 1.18). Також 19CLV (браузерний прогін, 24
доступні місяці) і SHB; Excel відтворює ту саму таблицю, тотали порізів
27 772 / 12 088 / 6 042.

### Тести

- `tests/unit/tools/ga4_events/test_service.py` (13) — три порізи на івент,
  **відсотки від тоталів свого порізу** (живі 1.16%/7.71%), `per_user` з GA4,
  **users ≠ сума колонки**, **CONTAINS, а не EXACT** (тест читає сам
  `dimensionFilter`), відсутній поріз ≠ нуль, сортування за `All Users`,
  ізоляція впалого місяця, `no_data` ≠ нуль, `Partial`, валідація й ліміт
  місяців, **форма запитів** (6 на місяць), `available_months`.
- `tests/unit/tools/ga4_events/test_xlsx.py` (5) — аркуш на місяць, три рядки
  на івент із назвою один раз, рядки TOTAL із тоталів кожного порізу (і
  перевірка, що сума колонки Users у книгу не потрапляє), аркуш для впалого
  місяця, `Summary` лише по `All Users`.
- `tests/unit/api/test_tools_ga4_events.py` (8) — каталог клієнтів із чесним
  `configured`, персистенція + читання знімка, `not_configured` (немає
  інтеграції / немає токена), валідація місяців, `/months`, експорт, 404.
- `tests/e2e/frontend/tools-ga4-events.cjs` — живий браузерний прогін:
  хаб → форма → звіт → **три порізи на івент і назва один раз** → фільтр
  ключових → перемикання вкладок клавіатурою → інваріант Users → завантаження
  `.xlsx` → shareable link → мобільна ширина.

### Follow-ups / борг

- **Не наш баг, але заміряний по дорозі:** `.header-start` (масthead,
  `frontend/css/app.css`) виходить за 390 px на **кожній** сторінці застосунку
  (39 px, однаково на `#/tools` і `#/tools/schema-markup`). Тому мобільний крок
  смоуку порівнює сторінку з хабом як базою, а не з нулем. Виправлення —
  окремою таскою у власника `app.css`.
- Набір порізів зашитий трьома (`service.SEGMENTS`). Четвертий (Paid, Referral,
  Cross-network) — це один запис у кортежі плюс колонка в UI/Excel, але кожен
  новий поріз коштує ще 2 запити на місяць.
- MoM прибрано разом зі старою формою таблиці; якщо знадобиться, повертати його
  слід **на рівні порізу** й з тим самим правилом «часткові місяці не
  порівнюються».
- Тул однокліентський за рішенням користувача; портфельний прогін
  структурно можливий (усі 14 property за ~9 с послідовно), але не робився.
