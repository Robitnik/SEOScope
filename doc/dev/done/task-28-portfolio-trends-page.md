# task-28 — Сторінка «Portfolio Trends»: один графік, лінія = клієнт

- **Пакет:** нова верхньорівнева сторінка `#/trends` — **порівняльний графік по всьому портфелю**: кожна **лінія = клієнт**, кожна **точка = число за бакет** (день/тиждень/місяць). Метрики: **GSC Clicks**, **GSC Impressions**, **GA4 Sessions**, **GA4 Users** (трафік через Reporting Ninja). Плюс бекенд-ендпоінт, який рахує ці ряди чесно (покриття, `null` ≠ `0`), і бекфіл денної історії.
- **Хвиля:** J (розширення після MVP; усі попередні таски закриті)
- **Depends on:** — (адитивно розширює закриті зони task-02/03/04/06/07/09/12)
- **PLAN.md:** §3.1 (періоди), §3.2 (каталог метрик), §5.2 (дерево `app/`), §6 (модель даних, immutability), §10 (REST-конвенції), §12 (`None` ≠ `0`, quality/стани), §21.1 (розширені періоди), §21.6 (сторінка клієнта — взірець роутингу і стану в URL).
- **frontend/AGENTS.md:** обов'язковий для всієї роботи у `frontend/`.
- **AISeoTools:** **змін не потребує** — обґрунтування у §1.3.

Зведений таск-док (як task-25/26/27). Межі під-частин проведені в «Owned paths» (§9).

---

## Мета (робота оператора, яку закриваємо)

Сьогодні динаміку видно **тільки по одному клієнту за раз**: графік «Clicks and impressions trend» живе на сторінці клієнта
([charts.js:49](../../../frontend/js/charts.js#L49)) і показує два ряди одного клієнта. Питання «**хто з 14 клієнтів росте, а хто
просів**» вимагає відкрити 14 сторінок і тримати числа в голові.

Сторінка `#/trends` відповідає на це одним екраном: **одна метрика — багато клієнтів**, лінія на клієнта, спільна вісь часу.
Це операційний інструмент («кому дзвонити цього тижня»), а не презентаційний графік.

---

## 1. Дослідження (факти, на яких стоїть план)

### 1.1 Дані вже збираються — нових провайдерів не треба

| Ряд | Таблиця | `dataset_key` | `dimension_key` | Поле в `metrics_json` | Хто пише |
|---|---|---|---|---|---|
| GSC Clicks | `analytics_dataset_rows` | `gsc_daily` | `date` | `clicks` | [gsc.py:324](../../../app/integrations/gsc.py#L324) |
| GSC Impressions | `analytics_dataset_rows` | `gsc_daily` | `date` | `impressions` | там само |
| GA4 Sessions | `analytics_dataset_rows` | `ga4_daily` | `date` | `sessions` | [reporting_ninja.py:53](../../../app/integrations/reporting_ninja.py#L53) |
| GA4 Users | `analytics_dataset_rows` | `ga4_daily` | `date` | `users` (`totalUsers`) | там само |

Обидва датасети пишуться **по днях** (`dimension_value` = `YYYY-MM-DD`) і **для обох ролей** періоду (`current` + `previous`),
тобто один запуск за період `custom` покриває **подвійну довжину** діапазону денними рядками.

### 1.2 Дедуплікація: «найсвіжіший рядок на день перемагає»

Один і той самий день приїжджає багато разів (кожен новий запуск, обидві ролі). Взірець уже є в
[`_gsc_clicks_trend`](../../../app/api/dashboard.py#L556) і [`_gbp_clicks_trend`](../../../app/api/dashboard.py#L507): беремо
**найновіший за `created_at` рядок на кожен `(client_id, dimension_value)`**, а не «останній snapshot». Це важливо тому, що
GSC добиває дані заднім числом — пізніший збір того ж дня достовірніший.

`AnalyticsDatasetRepository.for_period` тут **не годиться**: він вимагає точного збігу `(period_start, period_end)`, а нам
потрібне довільне вікно днів через усі знімки. Потрібен новий (адитивний) метод — §4.

### 1.3 Чому AISeoTools не змінюється

Денні датасети збираються **не** через `seo_audit.dashboard.*`, а прямим викликом `ReportingNinjaClient.query_all(...)` в
адаптерах цього репозиторію ([gsc.py:298](../../../app/integrations/gsc.py#L298),
[reporting_ninja.py:332](../../../app/integrations/reporting_ninja.py#L332)). Ліміти вже з запасом:
GSC `max_rows=2000` (365 днів = 365 рядків), GA4 `max_rows=50000`, `ga4_daily.limit=1000`.
Отже **жодного рядка в `/home/elon/dev/seo/AISeoTools` ця таска не чіпає** — ані адитивно.

### 1.4 Глибина історії — головне обмеження, і воно вирішується бекфілом

Денні рядки існують лише за ті періоди, які **реально збирали**. Локальна БД (`app_data/app.db`) зараз **порожня**
(`analytics_dataset_rows` = 0 рядків, `clients` = 0), тож без бекфілу сторінка чесно покаже порожній стан.

Шлях бекфілу вже існує і працює без нового коду збору: `POST /api/v1/sync-jobs` приймає
`period_kind=custom` + `custom_start`/`custom_end` ([jobs.py:211](../../../app/api/jobs.py#L211)), а `_custom_range`
([periods.py:234](../../../app/metrics/periods.py#L234)) робить `previous` тієї ж довжини одразу перед `current`.
**Один запуск `custom` на 90 днів дає 180 днів денних рядків.**

Ціна одного бекфілу (14 клієнтів × 2 провайдери): GSC — 2 запити/клієнт, GA4 — 6 датасетів × 2 ролі = 12 запитів/клієнт.
Разом ≈ **196 запитів** — разова, прийнятна вартість; повторювати щотижня не треба, бо рутинний week-sync далі сам добиває нові дні.

### 1.5 Обмеження, які диктує сама природа даних

- **Лаг провайдера.** GSC віддає останні ~3 дні неповними, GA4 ~2. Останній бакет майже завжди «ще добирається» — його не можна
  мовчки порівнювати з попереднім.
- **Різні порядки величин.** У портфелі поруч сайт із 100k імпресій і сайт із 500. На одній лінійній осі малий клієнт
  перетворюється на пряму лінію біля нуля.
- **14 клієнтів > 8 кольорів.** Категоріальна палітра має рівно 8 безпечних слотів; дев'ятий колір **не генерується** (§6.3).

---

## 2. Що будуємо (обсяг)

1. **Ендпоінт** `GET /api/v1/analytics/portfolio-series` — ряди по клієнтах для однієї метрики, з покриттям і якістю на кожну точку.
2. **Чистий модуль агрегації** `app/metrics/portfolio.py` — бакетизація днів, `null` ≠ `0`, часткові бакети, provisional-хвіст.
3. **Сторінка** `#/trends` (nav-пункт **Trends** поряд із Clients / Tools): графік «лінія = клієнт», таблиця під ним, контроли,
   всі стани, повний стан у URL.
4. **Бекфіл** денної історії: CLI-команда + дія в шапці сторінки (обидві створюють один і той самий `custom`-джоб).

**Поза обсягом (свідомо):** нові метрики на картках дашборду; зміна `CATALOG`; збір нових датасетів; порівняння клієнт↔клієнт
у розрізі сторінок/запитів; експорт графіка в PNG/PDF.

---

## 3. Контракт API

### 3.1 Запит

```
GET /api/v1/analytics/portfolio-series
  ?metric=gsc_clicks|gsc_impressions|rn_sessions|rn_users     # required
  &bucket=day|week|month                                       # default: week
  &days=30|90|180|365                                          # default: 90 (взаємовиключно зі start/end)
  &start=YYYY-MM-DD&end=YYYY-MM-DD                             # явне вікно
  &client_ids=<uuid>,<uuid>                                    # default: усі enabled клієнти
```

- Невідома `metric` → `404 {"error_code":"metric_not_found"}` (як `dataset_not_found` в
  [analytics.py:452](../../../app/api/analytics.py#L452)).
- Невідомий `client_id` → `404 {"error_code":"client_not_found", "details":{"client_ids":[…]}}`.
- `end < start`, `days` разом зі `start/end`, вікно > 400 днів → `422` з поясненням.

### 3.2 Відповідь (`PortfolioSeriesDTO`)

```jsonc
{
  "metric": {
    "key": "gsc_clicks", "label": "Clicks", "provider": "gsc",
    "unit": "count", "better_direction": "up",
    "dataset_key": "gsc_daily", "field": "clicks", "comparison_kind": "range"
  },
  "bucket": "week",
  "range": { "start": "2026-04-29", "end": "2026-07-27" },
  "buckets": [
    { "start": "2026-04-27", "end": "2026-05-03", "label": "27 Apr – 3 May",
      "days_expected": 7, "provisional": false }
  ],
  "series": [
    {
      "client_id": "…", "name": "LAXTON", "domain": "laxton.se",
      "state": "ok",
      "points": [
        { "value": "1234", "days_with_data": 7, "quality": "complete" },
        { "value": null,   "days_with_data": 0, "quality": "unavailable" },
        { "value": "812",  "days_with_data": 4, "quality": "partial" }
      ],
      "first_day": "2026-05-02", "last_day": "2026-07-24",
      "days_with_data": 74, "days_expected": 90
    }
  ],
  "excluded": [
    { "client_id": "…", "name": "PJC", "state": "not_configured",
      "reason": "Reporting Ninja integration is not set up for this client." }
  ],
  "generated_at": "2026-07-27T10:00:00Z"
}
```

### 3.3 Інваріанти відповіді (це і є суть таски)

| Правило | Чому |
|---|---|
| `value: null`, якщо `days_with_data == 0` | PLAN §12: відсутнє ≠ нуль. Нуль тут — брехня про «трафік упав у нуль». |
| `quality: "partial"`, якщо `0 < days_with_data < days_expected` | Тиждень із 4 днів **не** можна мовчки ставити поруч із повним. Фронт малює його інакше (§6.4). |
| `provisional: true` на бакеті, чий `end >= today − provider_lag` | Хвіст іще добирається; у підсумку дельти він не бере участі. Лаг: GSC 3 дні, GA4 2 (константи модуля). |
| Ряд, у якого **жодної** точки, не потрапляє в `series` — він іде в `excluded` зі `state` | «Клієнт без даних» і «клієнт із нулем» — різні речі, і обидві мають бути видимі. |
| `state`: `ok` \| `no_data` \| `not_configured` | `not_configured` визначається за `client_integrations` провайдера метрики, а не за порожнечею. |
| Значення — рядки (`Decimal` → JSON string), як у `AnalyticsDatasetRowDTO` | Уже усталена конвенція репозиторію ([analytics.py:414](../../../app/api/analytics.py#L414)). |
| Агрегація в бакеті — **сума** денних значень | Обидві метрики адитивні за днями. Для `rn_users` це технічно сума денних unique users (≠ unique за період) — **позначено в UI підписом**, не мовчки. |

### 3.4 Бакетизація

- `day` — бакет = день, `days_expected = 1`.
- `week` — **ISO-тиждень, понеділок→неділя**. Перший/останній бакет вікна обрізаються по вікну, і `days_expected` дорівнює
  **фактичній кількості днів бакета у вікні** (обрізаний тиждень не оголошується частковим лише через обрізання).
- `month` — календарний місяць, аналогічно.
- **Часовий пояс.** Дні беруться такими, якими їх віддав провайдер (GSC — таймзона property, GA4 — таймзона потоку). Клієнти
  живуть у різних поясах, спільного календаря не існує — тому **жодних перетворень tz не робимо** і пишемо це в футері графіка
  («Days as reported by each provider»). Вигаданий tz-зсув був би гіршим за чесну примітку.

---

## 4. Бекенд: файли й межі

### 4.1 `app/metrics/portfolio.py` (новий, чистий — без ORM)

```python
@dataclass(frozen=True)
class SeriesSpec:            # локальний реєстр рядів, НЕ CATALOG (див. 4.2)
    key: str; label: str; provider: Provider; dataset_key: str
    field: str; unit: MetricUnit; better_direction: BetterDirection; lag_days: int

PORTFOLIO_SERIES: dict[str, SeriesSpec]   # gsc_clicks, gsc_impressions, rn_sessions, rn_users

def build_buckets(start: date, end: date, bucket: Bucket) -> list[Bucket_]
def aggregate_series(days: Mapping[date, Decimal | None], buckets, *, today, lag_days) -> list[Point]
```

Дзеркалить рішення task-14/17: чиста функція над простими типами, ORM лишається в API-шарі. Уся математика §3.3/§3.4 — тут,
і саме вона покривається юніт-тестами.

### 4.2 Чому нові ряди **не** йдуть у `CATALOG`

`metrics_for(provider)` керує рядками KPI на картці клієнта ([catalog.py:185](../../../app/metrics/catalog.py#L185)). Додати
`rn_sessions` у `CATALOG` = додати **порожній рядок «Sessions» на картку кожного клієнта**, бо жоден адаптер не пише таку
`MetricValue`. Тому реєстр рядів локальний. Іменування лишається наскрізним (CLAUDE.md): `gsc_clicks`/`gsc_impressions`/`rn_users`
беруть `label`/`unit`/`better_direction` **з `CATALOG`** (щоб «Clicks» звався «Clicks» скрізь), і лише `rn_sessions` описаний
локально з коментарем-поясненням.

### 4.3 `app/db/repositories/analytics_datasets.py` (адитивний метод)

```python
def daily_series(
    self, client_ids: Sequence[str], provider: Provider, dataset_key: str,
    since: date, until: date,
) -> dict[str, dict[date, AnalyticsDatasetRow]]:
    """Найсвіжіший рядок на (client, day) у вікні — через УСІ знімки."""
```

Один `SELECT` із `client_id IN (...)`, `dataset_key == …`, `dimension_value BETWEEN` (ISO-дати сортуються як рядки),
`ORDER BY created_at DESC` + `setdefault` — та сама семантика, що в `latest_rows_by_dimension`, але обмежена вікном і
багатоклієнтська. Це чужа зона (task-03): оформити секцією-коментарем «Added by task-28», як зроблено для task-05/06/25, і
оголосити в Completion report.

**Міграція не потрібна.** Наявний індекс `ix_analytics_dataset_client_key_period` покриває префікс `(client_id, dataset_key)`;
обсяг — 14 клієнтів × ~180 днів × кілька знімків = тисячі рядків. Якщо таблиця перевалить ~100k рядків — окремий індекс на
`(client_id, dataset_key, dimension_value)` як follow-up, **не наперед**.

### 4.4 `app/api/portfolio.py` (новий роутер)

Реєструється сам через `register_router(router)` — `app/main.py` не чіпаємо ([api/__init__.py](../../../app/api/__init__.py)).
Читає клієнтів + інтеграції, тягне `daily_series` **одним** викликом на всі клієнти, віддає в `app/metrics/portfolio.py`,
збирає DTO. Жодного тригера збору (як і `analytics.py` — read-only).

### 4.5 `app/schemas/api.py` (адитивні DTO)

`PortfolioSeriesDTO`, `PortfolioBucketDTO`, `PortfolioPointDTO`, `PortfolioClientSeriesDTO`, `PortfolioExcludedClientDTO`,
`PortfolioMetricDTO`. Нічого наявного не перейменовується — контрактна зміна суто адитивна, оголосити в Completion report.

### 4.6 `app/cli.py` — `backfill-daily-history` (адитивна команда)

```bash
docker compose run --rm api poetry run yovth-report backfill-daily-history \
  --days 90 --providers gsc,reporting_ninja [--client-ids …] [--dry-run]
```

Створює **один** `custom`-джоб (`custom_end = today − 1`, `custom_start = end − days + 1`) через наявний `JobRepository.create_run`
і друкує, скільки клітинок поставлено в чергу + очікувану кількість запитів до провайдерів. Прецедент адитивної команди —
`backfill-health-history` (task-24). `--dry-run` обов'язковий, бо це платні запити.

---

## 5. Стани й якість (що фронт зобов'язаний показати)

| Ситуація | Що показуємо |
|---|---|
| Порожня БД / жодного дня | Порожній стан із конкретною дією: «Collect 90 days of daily history» (запускає бекфіл) + команда CLI поруч. Не «Something went wrong». |
| Клієнт без інтеграції | У списку `excluded` під графіком, бейдж `not configured` + CTA на інтеграцію. Ніколи не лінія на нулі. |
| Клієнт із діркою в середині | Розрив лінії (`spanGaps: false`, як уже в [charts.js:78](../../../frontend/js/charts.js#L78)), а не з'єднання через провал. |
| Частковий бакет | Порожня (hollow) точка + пунктирний сегмент + у tooltip «4 of 7 days collected». |
| Provisional хвіст | Останній сегмент пунктиром + підпис «Last bucket still filling (GSC reports with a 3-day delay)»; у дельті не бере участі. |
| Помилка мережі | Панель із текстом помилки + «Try again», попередній графік лишається на екрані (AGENTS §5). |

---

## 6. Фронтенд

### 6.1 Файли

| Файл | Роль |
|---|---|
| `frontend/js/trends-page.js` (новий) | шел сторінки, контроли, стан↔URL, запити, таблиця, стани |
| `frontend/js/trends-chart.js` (новий) | Chart.js multi-line, аллокатор кольорів, текстове резюме, small multiples |
| `frontend/css/trends.css` (новий) | лейаут сторінки (токени лише з `tokens.css`) |
| `frontend/fixtures/trends-{normal,sparse,partial,empty}.json` (нові) | обов'язкові стани для візуального лупа |
| `frontend/index.html` (адитивно) | nav-пункт **Trends**, `<main id="trends-page-root" hidden>`, підключення `trends.css`, новий `?v=` |
| `frontend/js/router.js` (адитивно) | маршрут `#/trends`, гілка `activate()`, `buildTrendsHash`/`replaceTrendsHash` |
| `frontend/js/api.js` (адитивно) | `getPortfolioSeries({…})` + fixture-режим |

`index.html`/`api.js`/`router.js` — спільні read-only файли (INDEX «Успадковані зони»): правки тільки адитивні й перелічені в
Completion report.

### 6.2 Контроли (один рядок над графіком, `interaction.md`-модель)

- **Metric** — Clicks / Impressions / Sessions / Users (радіо-група, не select: 4 опції).
- **Bucket** — Day / Week / Month.
- **Range** — 30 / 90 / 180 / 365 днів.
- **Group** — All / Vision / Vertex (перевикористати `filterByClientGroup` з
  [client-groups.js](../../../frontend/js/client-groups.js), а не дублювати мапу).
- **View** — `Compare` (до 8 ліній) / `All clients` (small multiples) / `Indexed` (перший непорожній бакет = 100).
- **Клієнти** — чекбокс-список із пошуком; вибір ≤ 8 у режимі Compare (9-й чекбокс disabled із поясненням, а не мовчки ігнорується).

Увесь стан — у хеші (`#/trends?metric=…&bucket=…&days=…&group=…&view=…&clients=…`) через `replaceState` (AGENTS §12).

### 6.3 Кольори (перевірено скриптом, не на око)

Категоріальна палітра під surface `#1e1e1e`, 8 слотів, у фіксованому порядку:

```
1 #3987e5  2 #d95926  3 #199e70  4 #c98500
5 #d55181  6 #008300  7 #9085e9  8 #e66767
```

Валідатор (`dataviz/scripts/validate_palette.js`, `--mode dark --surface #1e1e1e`): **ALL CHECKS PASS** —
lightness band, chroma floor, CVD-розділення (гірша сусідня пара ΔE 8.4 protan), normal-vision floor 19.3, контраст ≥ 3:1.

Правила, які **не** обговорюються:
- **9-й колір не генерується.** Понад 8 клієнтів → режим `All clients` (small multiples: картка-спарклайн на клієнта,
  своя шкала, один колір), а не 14 ліній однією кашею.
- **Колір належить клієнту, а не його місцю в рейтингу.** Аллокатор тримає призначені слоти: зняли клієнта — слот звільнився,
  додали — отримав найменший вільний. Зміна фільтра **не перефарбовує** тих, хто лишився.
- **Брендовий `#ff4a17` — не серійний колір.** Він лишається за наведеною/сфокусованою лінією і головною дією
  (AGENTS §2: «spend visual boldness once per view»).
- **Одна вісь Y.** Дві метрики одночасно на двох шкалах — заборонено (це рівно та помилка, що є в наявному
  [charts.js:109](../../../frontend/js/charts.js#L109) з `y`/`y1`; тут її не повторюємо). Порівняння різних порядків
  величини робиться режимом `Indexed`, а не другою віссю.

### 6.4 Специфікація графіка

- Лінії 2px, точки ≥ 8px hit-area (`pointRadius: 3`, `pointHitRadius: 10`), `spanGaps: false`.
- Частковий бакет: `pointStyle` порожнє коло + `segment.borderDash` на сегменті, що входить у нього.
- Hover: crosshair + tooltip з **усіма** рядами в цій точці, відсортованими за значенням; наведена лінія підсвічується
  (`#ff4a17`), решта притухає — стан, а не тільки колір.
- Прямі підписи на кінцях ≤ 4 ліній; легенда присутня завжди (ідентичність ніколи не лише кольором).
- Осі й сітка — рецесивні (`#2a2a2a`), числа — `Intl.NumberFormat` + tabular-nums.
- `prefers-reduced-motion` → `animation: false`.

### 6.5 Таблиця під графіком (не декор — друга форма тих самих даних)

Рядок = клієнт: current bucket, previous bucket, Δ (колір за `better_direction`, не за знаком), сума за вікно, покриття
(`74 / 90 days`), last data day. Сортується кліком по заголовку. Це одночасно accessible-альтернатива canvas (AGENTS §7) і
основний носій точних чисел — графік відповідає «хто росте», таблиця «на скільки».

### 6.6 Текстове резюме

Один абзац під графіком, як `summarize()` у [charts.js:36](../../../frontend/js/charts.js#L36): скільки клієнтів зросло/впало,
хто найбільше виріс і хто найбільше просів — **з явним виключенням provisional-бакета** і згадкою, скільки клієнтів не мають даних.

---

## 7. Бекфіл з UI

Кнопка **«Collect history»** у шапці сторінки → компактний діалог: діапазон (30/90/180/365), провайдери (GSC / GA4), список
клієнтів, **очікувана кількість запитів** і чесне попередження, що це платні виклики. Далі — наявний
`POST /api/v1/sync-jobs` (`period_kind=custom`), наявний job-dock і SSE-прогрес. Нової job-механіки не пишемо.

---

## 8. Ризики та запобіжники

| Ризик | Запобіжник |
|---|---|
| Порожня історія → «зламана» сторінка на демо | Порожній стан із конкретною дією + CLI-командою; `excluded` завжди перелічує, кого і чому немає |
| 196 платних запитів на бекфіл | `--dry-run` за замовчуванням у CLI-довідці, підрахунок запитів у діалозі, бекфіл разовий |
| Часткові тижні брешуть про тренд | `days_with_data`/`quality` на кожній точці + окремий рендер + виключення з дельти |
| GSC-лаг подає падіння там, де його немає | `provisional` на хвості, пунктир, підпис із причиною |
| 14 ліній нечитабельні | Cap 8 + small multiples + прямі підписи + підсвітка при наведенні |
| Різні порядки величин ховають малих клієнтів | Режим `Indexed` (база = перший непорожній бакет); індексація **заборонена**, якщо базовий бакет `null`/`partial` — тоді клієнт показується з поміткою «no baseline» |
| Дубль логіки з `_gsc_clicks_trend` | Картковий тренд лишається як є (чужа зона task-24/06); спільним є **репозиторний метод**, а не копія агрегації |

---

## 9. Owned paths

**Нові (повністю наші):**
`app/api/portfolio.py`, `app/metrics/portfolio.py`,
`frontend/js/trends-page.js`, `frontend/js/trends-chart.js`, `frontend/css/trends.css`,
`frontend/fixtures/trends-*.json`,
`tests/unit/metrics/test_portfolio.py`, `tests/unit/api/test_portfolio_api.py`, `tests/e2e/frontend/trends-page.cjs`

**Адитивні дотики в чужих зонах (кожен — рядок у Completion report → «Contract changes»):**
`app/schemas/api.py` (нові DTO), `app/db/repositories/analytics_datasets.py` (`daily_series`), `app/cli.py`
(`backfill-daily-history`), `frontend/index.html`, `frontend/js/router.js`, `frontend/js/api.js`, `doc/dev/new/INDEX.md`.

**Не чіпаємо:** `app/metrics/catalog.py`, `app/api/dashboard.py`, `app/jobs/**`, `migrations/**`, `pyproject.toml`,
`frontend/js/charts.js`, будь-що в `/home/elon/dev/seo/AISeoTools`.

---

## 10. Тести

**`tests/unit/metrics/test_portfolio.py`** (ядро):
порожній вхід → жодної вигаданої точки; день без даних → `null`, **ніколи `0`**; день зі значенням `0` → `0` (справжній нуль
зберігається!); тиждень 4/7 → `partial` + `days_with_data=4`; обрізаний крайній тиждень → `days_expected` за фактом, `complete`;
межі ISO-тижня (понеділок) і місяця; `provisional` рахується від `today − lag`; сума за бакет == сумі денних; ряд без жодної
точки не потрапляє в `series`.

**`tests/unit/db/test_analytics_datasets_daily_series.py`**: два знімки на той самий день → перемагає новіший `created_at`;
вікно відсікає сусідні дні; кілька клієнтів в одному запиті не змішуються; невалідний `dimension_value` ігнорується без падіння.

**`tests/unit/api/test_portfolio_api.py`**: 404 на невідому метрику/клієнта; 422 на `end < start`, `days`+`start/end` разом,
вікно > 400 днів; клієнт без інтеграції → `excluded[state=not_configured]`; форма DTO збігається з §3.2; ендпоінт **не** створює
джобів (read-only).

**E2E `tests/e2e/frontend/trends-page.cjs`** (за взірцем `tools-alt-text.cjs`): фікстури normal/sparse/partial/empty; скріншоти
390 / 768 / 1440; перевірки — немає горизонтального скролу, клавіатурний прохід по контролах і чекбоксах, 9-й клієнт disabled із
поясненням, `null` ніде не відрендерено як `0`, легенда й таблиця присутні.

Прогін — **тільки в Docker** (CLAUDE.md): `docker compose exec api poetry run pytest tests/unit/metrics/test_portfolio.py -q`.

---

## 11. Definition of Done / Gate

- [ ] `GET /api/v1/analytics/portfolio-series` віддає §3.2 для всіх 4 метрик і 3 бакетів; помилки — за §3.1.
- [ ] Жодна точка з `days_with_data == 0` не має значення `0`; справжній нуль провайдера при цьому зберігається.
- [ ] Частковий і provisional бакети позначені в API **і** візуально відрізняються в UI; дельта їх не враховує.
- [ ] Клієнти без даних/інтеграції — в `excluded` з причиною і дією, а не лініями на нулі.
- [ ] Сторінка `#/trends` відкривається з nav, стан переживає reload/back/share.
- [ ] Кольори — рівно 8 слотів §6.3, валідатор перепрогнано і **PASS**; 9-й колір не генерується; фільтр не перефарбовує тих, хто лишився.
- [ ] Одна вісь Y. Двошкального графіка немає ніде на сторінці.
- [ ] Усі стани §5 реалізовані й зняті на скріншотах (390/768/1440), AGENTS §16 пройдено пунктом за пунктом.
- [ ] Клавіатурний прохід і accessibility-дерево перевірені; таблиця дублює графік числами.
- [ ] `backfill-daily-history --dry-run` друкує план і не створює джоба; без `--dry-run` створює рівно один `custom`-джоб.
- [ ] `ruff`, `mypy app`, повний `pytest` — зелені **в контейнері**.
- [ ] План перенесено `doc/dev/new/ → doc/dev/done/` з Completion report; INDEX.md оновлено.

---

## 12. Follow-ups (свідомо поза обсягом)

- Індекс `(client_id, dataset_key, dimension_value)` — коли `analytics_dataset_rows` перевалить ~100k рядків.
- Спільна точка правди для карткових трендів (`_gsc_clicks_trend`/`_gbp_clicks_trend`) і цієї агрегації — рефакторинг у зоні task-06/24.
- Ряди Ahrefs/Semrush на цій самій сторінці (snapshot-семантика ≠ range, потребує окремого рішення про порівнюваність).
- Експорт графіка/таблиці у CSV/PNG.
- Щоденний worker-тік, який добирає вчорашній день без ручного синку (успадкована ідея з task-24).
- Діалог бекфілу з UI (§7 плану) — свідомо не реалізований цього разу, див. Completion report.
- Ручний пікер клієнтів (чекбокс-список із лімітом ≤8) — замінений на детермінований авто-кап «top 8 за сумою» + режим `All clients`, див. Completion report.

---

## ✅ Completion report

- **Дата:** 2026-07-27
- **Гілка / комміт:** working tree (main, не закомічено — користувач ще не просив коміт)
- **Статус:** done-with-followups

### Що зроблено

Повний вертикальний зріз: бекенд-агрегація + API + CLI-бекфіл + фронтенд-сторінка `#/trends`, повністю за планом
(§3–§6), з двома свідомими спрощеннями обсягу (див. «Відхилення» нижче). Усе перевірено живими скріншотами в
браузері на 4 фікстурах (normal/empty/partial/sparse) + desktop/mobile + keyboard-only прохід, і повним прогоном
бекенд-тестів у Docker.

- `GET /api/v1/analytics/portfolio-series` — ряди по клієнтах для `gsc_clicks` / `gsc_impressions` / `rn_sessions` /
  `rn_users`, з бакетизацією day/week/month, `provisional`-хвостом і чесним `partial`/`unavailable` на кожній точці.
- `app/metrics/portfolio.py` — чиста агрегація (`build_buckets`/`aggregate_client_series`), без ORM, за взірцем
  `app.metrics.alerts`.
- `AnalyticsDatasetRepository.daily_series()` — новий репозиторний метод (найсвіжіший рядок на `(client, day)` через
  усі знімки, вікном, багатоклієнтський).
- `yovth-report backfill-daily-history` — CLI-команда: друкує план (клієнти/провайдери/к-сть клітинок/оцінку
  запитів) з обов'язковим `--dry-run` за замовчуванням у документації, ставить один `custom`-джоб із
  `mode="force_refresh"` (без цього клітинка з нещодавнім snapshot мовчки пропускається freshness-гейтом
  незалежно від запитаного періоду — перевірено в коді `app/jobs/runner.py`, не з першого припущення).
- Сторінка `#/trends`: 5 сегментованих контролів (Metric/Bucket/Range/Portfolio/View), Chart.js-графік «лінія =
  клієнт» з фіксованою 8-слотовою категоріальною палітрою (провалідована `dataviz`-скриптом під `#1e1e1e`: CVD
  8.4/normal-vision 19.3/контраст ≥3:1 — усі PASS), таблиця-дублікат чисел, текстове accessible-резюме, режим
  `Indexed` (перший спільний бакет = 100), режим `All clients` (small multiples без ліміту), список «Not shown» з
  причиною для кожного виключеного клієнта, порожній стан із точною CLI-командою бекфілу.

### Як зроблено (ключові рішення)

- **`rn_sessions` не потрапив у `CATALOG`** — додавання його туди намалювало б порожній рядок «Sessions» на картці
  кожного клієнта (жоден адаптер не пише таку `MetricValue`). Реєстр рядів локальний у `app/metrics/portfolio.py`;
  `gsc_clicks`/`gsc_impressions`/`rn_users` все одно тягнуть `label`/`unit`/`better_direction` з `CATALOG`, щоб назва
  метрики не розійшлася між карткою і цією сторінкою (CLAUDE.md).
- **`force_refresh` у CLI-бекфілі — не опція, а необхідність.** Прочитав `app/jobs/runner.py` перед тим, як писати
  CLI: без нього `create_run(mode="collect_missing")` мовчки пропускає клітинку, якщо в клієнта вже є будь-який
  недавній snapshot провайдера — незалежно від того, який період насправді запитаний. Без цього факту бекфіл
  друкував би «queued», але не зібрав би жодного нового дня для клієнтів із рутинним week-sync.
- **Кольори: перевірені скриптом, не на око.** `dataviz` skill → `scripts/validate_palette.js` під
  `--mode dark --surface #1e1e1e` — ALL CHECKS PASS для восьми слотів. 9-й колір не генерується: понад 8 клієнтів
  у Compare/Indexed автоматично обрізаються до топ-8 за сумою вікна з явною позначкою «Showing top 8 of N…» і
  посиланням на `All clients` (де ліміту немає — кожна картка своя шкала/колір).
- **Одна вісь Y.** Indexed-режим (нормалізація на перший спільний бакет = 100) — це те, як портфель різних порядків
  величини порівнюється без другої шкали; двовісний графік (як у наявному `charts.js`) свідомо не повторений.
- **Кольор належить клієнту, а не місцю в рейтингу.** `ColorAllocator` у `trends-chart.js` тримає мапу
  `client_id → слот`; зняли клієнта — слот звільнився, додали — отримав найменший вільний; виклик з тим самим
  порядком ids ідемпотентний (використано і для графіка, і повторно для узгодження кольору таблиці).
- **Живий CSP-баг, знайдений і виправлений smoke-тестом:** інлайновий `style="text-align:left"` у першому варіанті
  таблиці ловився `default-src 'self'` (немає `style-src`/`unsafe-inline`) і падав у консоль браузера мовчки (сторінка
  все одно рендерилась, тому це не спливло б без перевірки `console --errors`). Замінено на наявний CSS-клас
  `.trends-table th:first-child` (уже був у `trends.css`).
  Другий баг, знайдений так само: спарклайн-канвас у `.trend-card` без `position: relative`/`width: 100%` змушував
  Chart.js падати на дефолтну ширину 300px і розтягувати картку — приховуючи delta-чіп за межами видимої картки.
  Виправлено за взірцем наявного `.chart-wrap`.
- **Головні файли:** `app/api/portfolio.py` (роутер), `app/metrics/portfolio.py` (агрегація), `app/db/repositories/
  analytics_datasets.py` (+`daily_series`), `app/schemas/api.py` (+6 DTO), `app/cli.py` (+`backfill-daily-history`),
  `frontend/js/{trends-page,trends-chart}.js`, `frontend/css/trends.css`, `frontend/fixtures/trends-*.json`.

### Контрактні зміни (Contract changes)

- **Нові, чисто адитивні:** `GET /api/v1/analytics/portfolio-series`; DTO `PortfolioSeriesDTO`/`PortfolioMetricDTO`/
  `PortfolioBucketDTO`/`PortfolioPointDTO`/`PortfolioClientSeriesDTO`/`PortfolioExcludedClientDTO` у
  `app/schemas/api.py`; `AnalyticsDatasetRepository.daily_series()`; CLI-підкоманда `backfill-daily-history`.
- **OpenAPI-снапшот перегенеровано** (`tests/unit/api/data/openapi_snapshot.json`, +391 рядок, 0 видалень — чисто
  адитивний диф) через `app.main.app.openapi()` (пряма команда з докстрінга модуля через `python -m` наштовхнулась
  на відомий конфлікт імені пакета `tests` з AISeoTools, задокументований у `tests/conftest.py`; обійдено інлайновим
  `python -c`, що не змінює сам тест).
- Нічого наявного не перейменовано, жодна форма відповіді не змінена.

### Відхилення від плану (свідомі, через обсяг)

1. **Діалог бекфілу з UI (§7 плану) не побудований.** Замість нового модального вікна з job-dock/SSE-інтеграцією
   (яка торкнулась би `sync-modal.js`/`dashboard.js` — чужих зон) порожній стан сторінки друкує готову CLI-команду
   з точним `--providers` під обрану метрику. Це узгоджено з наявним прецедентом: `backfill-health-history`
   (task-24) теж не має UI-кнопки й запускається лише через CLI. Follow-up, якщо знадобиться UI-тригер.
2. **Ручний чекбокс-пікер клієнтів (§6.2 плану) замінений на детермінований авто-відбір.** Порівняльний/Indexed
   режим бере топ-8 клієнтів за сумою метрики у вікні (з явною банерною поміткою і посиланням «See all clients»,
   якщо клієнтів більше); режим `All clients` показує всіх без ліміту через small multiples. Це прибрало окремий
   шар стану (вибір ≤8 клієнтів у URL) і водночас зберігає обидва жорсткі правила з плану (9-й колір не
   генерується; таблиця під графіком завжди показує повний, некапований список). Follow-up, якщо оператору
   знадобиться закріпити конкретний ручний набір клієнтів.

### Як перевірити (verification)

```bash
# Бекенд (усе — в контейнері):
docker compose exec api poetry run pytest tests/unit/metrics/test_portfolio.py tests/unit/db/test_analytics_datasets_daily_series.py tests/unit/api/test_portfolio_api.py -q
docker compose exec api poetry run pytest -q            # повний набір: 807 passed, 1 skipped
docker compose exec api poetry run ruff check app/ tests/
docker compose exec api poetry run mypy app

# CLI (dry-run — безпечно, не витрачає units):
docker compose exec api poetry run yovth-report backfill-daily-history --dry-run

# Фронтенд (стек уже піднятий на :6524):
# відкрити http://localhost:6524/?fixture=trends-normal#/trends (і trends-empty/trends-partial/trends-sparse)
```

Живі скріншоти (normal desktop/mobile, indexed view, all-clients view, empty state, partial/provisional state,
sparse з обрізанням до топ-8, error state) знято й переглянуто в цій сесії через `playwright-core` +
Playwright-кешований Chromium (`~/.cache/ms-playwright/chromium-1228`) — жодного нового npm-пакета в самому
застосунку не додано, це разовий tooling сесії. Console errors перевірені на 0 на всіх 5 інших маршрутах
(dashboard/tools/client/blogs/ahrefs) — регресій від правок `router.js` не виявлено.

### Тести

- `tests/unit/metrics/test_portfolio.py` — 14 тестів: бакетизація day/week/month (зокрема обрізаний edge-бакет
  без хибного «partial»), провал-вікно, `None` vs справжній `0`, `partial`-тиждень, порожній клієнт, реєстр рядів.
- `tests/unit/db/test_analytics_datasets_daily_series.py` — 5 тестів: новіший `created_at` перемагає, вікно
  відсікає сусідні дні, багато клієнтів не змішуються, порожній список client_ids, невалідний `dimension_value`
  тихо пропускається (без падіння SQL-запиту чи Python-виключення).
- `tests/unit/api/test_portfolio_api.py` — 10 тестів: 404 на metric/client, 422 на invalid_range/range_too_wide,
  `not_configured` vs `no_data` рознесені, `partial`-точка ніколи не `0`, форма DTO, ендпоінт не створює джоб.
- Живий браузерний smoke (не закомічений як файл — разовий скрипт сесії) покрив: рендер на 4 фікстурах, перемикання
  всіх 5 контролів, URL-стан переживає перемикання, keyboard-only tab+Enter по сегментованих кнопках, 0 console
  errors, no page-level horizontal scroll на 390px.

---

## Fast-follow: пікер клієнтів + позначення «дірки» (2026-07-27, того ж дня)

Користувач після першого показу (на **живих** даних, не фікстурах) попросив два доопрацювання, підтверджені явним
рішенням через `AskUserQuestion`:

1. **Ручний пікер клієнтів** — те, що в основному звіті вище явно назване «свідомим відхиленням від плану» (§6.2:
   авто-топ-8 замість чекбокс-пікера). Реалізовано: `<details>`-панель «Clients (N / M shown)» над графіком, чекбокс
   на кожного клієнта з `series` (автопозначені = ті, що зараз на графіку) + disabled-рядки для `excluded` із
   причиною. Клік по будь-якому чекбоксу вмикає **manual mode** (`current.clients` стає конкретним масивом,
   персиститься в URL як `clients=id1,id2,...`); кнопка «Reset to automatic» повертає в auto-режим. Кеп у 8 для
   Compare/Indexed реалізований як `disabled` на решті чекбоксів **лише коли вже в manual mode і вибрано ≥8** — в
   auto-режимі клік по будь-якому клієнту завжди дозволений (інакше пікер не міг би замінити автовибір на конкретного
   потрібного клієнта). У `All clients` кепа немає. Таблиця під графіком у auto-режимі й далі показує весь
   портфель (як задокументовано вище), у manual — рівно вибраних клієнтів, щоб графік і таблиця не розходились
   мовчки. Жодного нового запиту до API: перемикання чекбоксів рендериться з уже завантаженого `lastPayload`.
2. **Позначення "дірки"** — обраний варіант (не бридж пунктиром, а видима позначка на місці розриву): нова
   `firstCommonBaseline()`-похідна `buildSparseBandPlugin()` у `trends-chart.js` — Chart.js inline-плагін (без нової
   залежності), що затінює й підписує «Not all clients yet» ведучу ділянку графіка, де ще не всі показані клієнти
   мають дані (обчислюється як перший бакет, у якому дані є **у всіх**). Той самий індекс тепер керує і `Indexed`-
   baseline, і цією позначкою — раніше вважалось окремо лише для Indexed. Текстовий двійник для accessibility
   (AGENTS.md §7): `buildSummary()` додав речення «N of M client(s) have no data before {дата}».

**Реальний баг, знайдений живим smoke-тестуванням (не вигаданий заздалегідь):** тестовий скрипт спершу «зациклився»,
namagаючись `.uncheck()` на живому/рухомому Playwright-локаторі (`input:checked` `.first()`), який після кожного
кліку (full re-render) резолвився в НАСТУПНИЙ чекбокс — тест сам собі знімав усі позначки одну за одною, поки не
скінчились елементи. Це виявило дрібний, окремий реальний недолік у коді застосунку: disabled-чекбокси для
`excluded`-клієнтів не мали атрибута `value`, тож `input[value="..."]`-локатор для них не резолвився (HTML без
`value=` → DOM-властивість `"on"`, а не сам client_id). Виправлено — додано `value="${item.client_id}"` і на
disabled-рядки теж (косметично нейтрально, але прибирає цю пастку для будь-якого майбутнього тесту/скрипта).

**Перевірено живими скріншотами й Playwright-скриптами на СПРАВЖНІХ даних** (не лише фікстурах — сталося саме тому,
що фікстури не моделювали різностартові клієнти): sparse-band коректно затінює ведучу ділянку графіка на реальному
14-клієнтському портфелі; пікер: uncheck→7 обраних, docheck до кепа→8+disabled далі, reset→повернення в auto без
`clients=` у URL; клавіатура (Enter відкриває `<details>`, Space тумблить чекбокс); мобільний вигляд 390px без
горизонтального скролу з відкритим пікером. Повний бекенд-набір (`pytest` 807/807, `ruff`, `mypy`) — без змін,
не торкались бекенду цим фолоу-апом.
