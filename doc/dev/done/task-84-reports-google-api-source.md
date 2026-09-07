# task-84 — Третє джерело даних для звітів: Google API

- **Хвиля:** пост-49 (движок звітів) × пост-83 (транспорт Google API).
- **Depends on:** **42** ✅, **45** ✅, **47** ✅, **48** ✅, **49** ✅, **83** ⏳ — але **не блокуюче**: транспортний контракт оголошений у [task-83 §3.6](task-83-google-api-direct-transport.md), тож ця таска кодиться проти нього з фейком у тестах (CLAUDE.md §3), а зустрічається з реалізацією на інтеграції.
- **Owned paths:**
  `app/reports/google_source.py` (новий — дзеркало `api_source.py`),
  `app/reports/runner.py` (розвилка джерела, §3.1),
  `app/reports/scheduler.py` (третій пул, §3.3),
  `app/reports/period_retry.py` (гілка `source`, §3.1),
  `app/schemas/reports.py` (**адитивно** — одне значення `Literal`, §6),
  `app/core/config.py` (адитивно — одна ємність пулу),
  `frontend/js/reports-wizard.js`, `frontend/js/reports-page.js` (третій варіант вибору),
  `tests/unit/reports/test_google_source.py`
- **Read-only для цієї таски:** `app/integrations/google_api/**` (володіє task-83), `app/reports/document.py` + `frontend/css/report-doc.css` (володіє task-85), `app/reports/api_source.py` (еталон звірки — **не редагувати**).
- **Міграція:** **не потрібна.** `report_runs.source` — звичайна `String`-колонка; [`app/schemas/reports.py#L1-L7`](app/schemas/reports.py#L1-L7) прямо фіксує це рішення: «`Literal[...]` aliases instead of `enum.Enum` (matches the DB layer's plain-`String` columns — a new value never needs a migration)».

---

## §0. Що є сьогодні (виміряно в коді)

**§0.1 Джерел рівно два, і обидва — Reporting Ninja.**
[`ReportSource = Literal["api", "pdf"]`](app/schemas/reports.py#L17):

| Значення | Що це насправді | Модуль |
|---|---|---|
| `api` | RN REST API | [api_source.py](app/reports/api_source.py) |
| `pdf` | Кабінет RN у браузері + парс PDF | [ninja_browser.py](app/reports/ninja_browser.py), [pdf_parse.py](app/reports/pdf_parse.py) |

Тобто «два джерела» — це два **транспорти до одного постачальника**. Якщо RN відключити, звіти зникають цілком, обома шляхами. Третє джерело — перше, що дає справжню незалежність.

**§0.2 Розвилка вже існує і має рівно одну точку.**
[`runner.py#L276`](app/reports/runner.py#L276):

```python
if source == "api":
    table = _run_api(...)
else:
    table, cc_result = _run_pdf(...)
```

`else` тут означає `pdf` — тобто **додавання третього значення без правки цієї гілки мовчки відправить його в PDF-шлях**. Це перше місце, де таска зобов'язана бути явною: `if/elif/else` з `raise` на невідоме джерело, а не розширення `else`.

**§0.3 Планувальник роздає слоти по джерелу — і `pdf` там не просто ще один пул.**
[`scheduler.py#L107-L115`](app/reports/scheduler.py#L107-L115) заповнює три черги: `api` (звичайна), `api` (батчева) і `pdf`. Ємності роздільні, бо PDF-шлях тримає браузерний профіль, а API — ні. Нове джерело **мусить отримати власний пул**: якщо його не додати, `_fill` ніколи не візьме такий прогін у роботу, і він назавжди залишиться `queued` — відмова без жодної помилки.

**§0.4 `api_source.py` — це не «читалка API», а зафіксована модель атрибуції.** Її докстрінг ([api_source.py#L1-L36](app/reports/api_source.py#L1-L36)) описує чотири рішення, здобуті живими вимірами, і кожне з них нове джерело зобов'язане повторити, інакше цифри розійдуться:

1. **Тотали (`users`, `conversions`) беруться запитом БЕЗ жодної дименсії** — щоб вони не могли розійтися з сумою дименсійних рядків.
2. **Organic/Direct — окремий запит по `firstUserDefaultChannelGroup`** (first-user атрибуція, як у PDF), а не `sessionMedium`: у першоджерелі це давало розбіжність 5–15%.
3. **Збіг каналу — точний** (`== "Organic Search"`), не підрядковий: інакше в organic втрапляють «Organic Social» і «Organic Shopping».
4. **GSC читається завжди з дименсією `date` і сумується локально.** Без дименсії RN на порожній період відповідає рядком буквальних нулів — не відрізнити від справжнього нуля; з `date` він відповідає **нулем рядків**. Саме через це `Month YOY` колись малював `0` замість `—`.

Пункт 4 у прямому Google API **не потрібен** (GSC API на порожній період віддає нуль рядків сам, це його нормальна поведінка), але пункти 1–3 — потрібні всі три, бо це властивість моделі атрибуції, а не транспорту.

**§0.5 `0` — валідне значення, `None` — ні.** Там же зафіксовано: `total_users or None` з першоджерела **не перенесено**, бо перетворював справжній нуль на «немає даних». Нове джерело повторює цю дисципліну буквально (PLAN §12).

**§0.6 Цифри сьогодні правильні — перевірено на живому прогоні.**
Розбіжність, з якої почалася ця таска (SAPHNY: наш звіт vs скрін RN), виявилася **різницею періодів, а не даних**: скрін RN — липень 2026, наш звіт — серпень 2026. Звірка з бойовою БД, число в число:

| | Наш звіт (Aug) | БД, серпень | Скрін RN | БД, липень |
|---|---|---|---|---|
| Monthly Clicks | 8,598 | **8,598** | 8,728 | **8,728** |
| Monthly Impressions | 179,118 | **179,118** | 189,658 | **189,658** |
| Quarterly Clicks | 25,219 | **25,219** (Jun–Aug) | 25,113 | **25,113** (May–Jul) |
| Quarterly Impressions | 564,859 | **564,859** | 583,360 | **583,360** |
| Monthly Organic Conversions | 1,178 | **1,178** | — | — |

**Це змінює мету таски.** Google API додається **не** щоб полагодити цифри — вони не зламані. Він додається заради незалежності від платного посередника, свіжості (RN кешує) і стійкості (§0.8). Відповідно gate — не «цифри стануть правильні», а «цифри залишаться тими самими» (§5).

**§0.7 Ліміт 16 місяців стосується і звітів.** GSC Search Analytics API не віддає нічого старішого за 16 місяців. Для `kind="yearly"` і `comparator` це означає, що частина колонок фізично недосяжна прямим джерелом. Це не привід не робити таску — це привід поводитися чесно: така колонка має стати `—` з поясненням, а **не** нулем і не мовчазним фолбеком на RN (§4.3).

**§0.8 Прямий транспорт прибирає цілий клас відмов.** У Google API немає ні браузерного профілю, ні логіну в кабінет, ні кабінетного локу — того самого, через який дві паралельні RN-сесії тихо збивають одна одній фільтр пошуку і можуть підв'язати звіт іншого клієнта. Жоден із цих ризиків у третьому джерелі структурно не існує.

---

## §1. Мета і межа

**Мета:** оператор у майстрі звітів обирає третє джерело — **Google API** — і отримує той самий звіт із тими самими цифрами, без Reporting Ninja в ланцюгу взагалі.

**У обсязі:** нове джерело для `kind="monthly"`, `"yearly"`, `"custom_months"` — тобто всюди, де сьогодні працює `api`.

**Поза обсягом:**
- `kind="comparator"` — він побудований навколо звірки PDF↔API ([xlsx.py#L192](app/reports/xlsx.py#L192): колонки `PDF | API | Diff %`). Третій стовпчик — окрема задача про сам сенс порівняння, а не про джерело. Явно `raise` на спробу.
- Дизайн документа — task-85.
- Переведення `#/trends` — task-83.
- Автовибір джерела / фолбек — §4.3.

---

## §2. Що саме тягне нове джерело

Дзеркало `fetch_period_metrics` з [api_source.py#L357](app/reports/api_source.py#L357), та сама сигнатура, той самий `PeriodMetrics` ([models.py#L15-L23](app/reports/models.py#L15-L23)) — вісім полів, кожне `int | None`.

| Поле `PeriodMetrics` | Джерело | Запит |
|---|---|---|
| `clicks`, `impressions` | GSC API | `searchAnalytics/query`, `dimensions=["date"]`, локальна сума |
| `users`, `conversions` | GA4 API | `runReport`, **без дименсій**, `totalUsers` / `keyEvents` (§0.4.1) |
| `organic_users`, `organic_conversions` | GA4 API | `runReport`, `dimensions=["firstUserDefaultChannelGroup"]`, рядок **рівно** `"Organic Search"` |
| `direct_users`, `direct_conversions` | те саме | рядок **рівно** `"Direct"` |

Ідентифікатори беруться з `client_integrations.config_json.account_id` — вони вже у форматі Google API (`sc-domain:…` / `properties/…`, доведено в [task-83 §0.3](task-83-google-api-direct-transport.md)). **Нових полів конфігурації не потрібно.**


**Транспорт не пишемо — він уже є.** Викликаємо реалізований [task-83 §3.6](task-83-google-api-direct-transport.md) контракт як є:

```python
from app.integrations.google_api import Ga4Client, GscClient, open_google_api_session

with open_google_api_session(settings) as session:
    days = GscClient(session).daily_search_analytics(site_url, start, end)
    rows = Ga4Client(session).run_report(
        property_id, start, end,
        dimensions=["firstUserDefaultChannelGroup"], metrics=["totalUsers", "keyEvents"],
    )
```

⚠️ `GscDailyRow.ctr` — частка `0..1`, не відсоток (там же). У звітах CTR рахується з `clicks`/`impressions`, тож поле напряму не беремо, але при будь-якому його використанні пастка ×100 діє.

⚠️ `keyEvents` і `conversions` в одному запиті GA4 дають `INTERNAL`. Просимо **тільки** `keyEvents`.

---

## §3. Інтеграція в движок

**§3.1 Розвилка.** [`runner.py#L276`](app/reports/runner.py#L276) стає явною:

```python
if source == "api":            table = _run_api(...)
elif source == "google_api":   table = _run_google(...)
elif source == "pdf":          table, cc_result = _run_pdf(...)
else:                          raise ValueError(f"unknown report source: {source!r}")
```

`_run_google` — копія структури `_run_api` (той самий `mark_period`, ті самі `emit`, та сама поперіодна ізоляція), відрізняється лише модулем даних. Те саме в [`period_retry.py#L278`](app/reports/period_retry.py#L278).

**§3.2 Ізоляція періоду зберігається.** Збій одного періоду позначає його `failed` і не валить прогін — це вже поведінка `_run_api`, і `_run_google` не має права її послабити.

**§3.3 Третій пул планувальника.** У [`_tick`](app/reports/scheduler.py#L107) додається `self._fill("google_api", …)` з власною ємністю в `Settings`. Ємність може бути вищою за `api` (немає ні браузера, ні лімітів RN), але **не безмежною** — квота GA4 рахується на property.

**§3.4 Помилки доступу — `not_configured`, а не `failed`.** Клієнт без доступу до property (403 `PERMISSION_DENIED`) — це «не налаштовано», рівно як у [task-83 §3.4](task-83-google-api-direct-transport.md) і PLAN §9.2. Червоний прогін тут збрехав би про причину.

**§3.5 Секрети.** Refresh token живе тільки в `.env` і в `Settings`. Ні в `result_json`, ні в подіях прогону, ні в тексті помилки, показаної в UI, він не з'являється (CLAUDE.md, PLAN §7).

---

## §4. UI

**§4.1** Третій варіант у [reports-wizard.js](frontend/js/reports-wizard.js) поруч із наявними двома. Копірайт **англійською** (CLAUDE.md): `Google API` з підписом `Direct from Google — no Reporting Ninja`.

**§4.2** У картці прогону джерело видно як `google_api` — те саме значення, що в БД, без перекладу.

**§4.3 Автофолбеку на RN немає.** Якщо прямий шлях не дав періоду, прогін чесно каже це в `partial`/`failed` замість тихо підмінити джерело. Мовчазний фолбек — найгірший варіант: оператор бачить цифри, вважає, що працює прямий шлях, і дізнається правду в момент відключення RN. Те саме рішення, що [task-83 §6.1](task-83-google-api-direct-transport.md).

---

## §5. Gate: цифри мусять ЗАЛИШИТИСЯ тими самими

Через §0.6 критерій успіху перевернутий: не «дані виправляться», а «дані не зрушать».

**§5.1** Прогнати той самий клієнт і той самий період обома джерелами (`api` і `google_api`) і порівняти `result_json`.

**§5.2 Допуски:**

| Метрика | Допуск | Чому |
|---|---|---|
| `clicks`, `impressions` | **0** | RN проксює той самий GSC API. Будь-яка різниця = різна вибірка (`search_type`, тип property, TZ), і її треба зрозуміти, а не списати на округлення. |
| `conversions`, `organic_conversions`, `direct_conversions` | **0** на закритих періодах | Цілі. Різниця = різна дименсія атрибуції (§0.4.2/3). |
| `users`, `organic_users`, `direct_users` | ≤ 1% | GA4 доуточнює ~48 год. |

**§5.3 Мінімум 4 клієнти, підібрані за формою конфігурації:** SAPHNY (найдовша історія, впирається в межу 16 місяців), SHB (багато подій), EFFYS або GRP (**URL-prefix** GSC-property, не `sc-domain:`), 3XE (кілька GA4-property на акаунті — найвищий ризик, що `account_id` вказує не туди).

**§5.4** Періоди, що виходять за 16 місяців, зі звірки виключаються (§0.7) — там `api` має дані, а `google_api` фізично не може.

---

## §6. Контрактні зміни

`ReportSource = Literal["api", "pdf", "google_api"]` — одне додане значення. Міграції не потребує (§Міграція). Споживачі: `runner`, `scheduler`, `period_retry`, фронт-майстер. Наявні рядки лишаються валідними.

Після мержу синхронно оновити `doc/dev/done/task-02-shared-contracts.md` і згадати в Completion report (CLAUDE.md §5).

---

## §7. Тести

1. `PeriodMetrics`, зібраний `google_source` і `api_source` з еквівалентних фейкових відповідей, **збігається у всіх восьми полях**. Головний тест таски.
2. Точний збіг каналу: рядки `Organic Social` / `Organic Shopping` **не** потрапляють в `organic_*` (§0.4.3).
3. Нуль рядків GA4 → `None`; рядок зі значенням `0` → `0` (§0.5).
4. `keyEvents` без `conversions` у запиті (§2).
5. Невідоме джерело в `runner` → `ValueError`, не тихий PDF-шлях (§0.2).
6. Прогін із `source="google_api"` підхоплюється планувальником — регресія на §0.3 (без пулу він вічно `queued`).
7. `403 PERMISSION_DENIED` → `not_configured`, не `failed`.
8. `comparator` + `google_api` → явна відмова (§1).

Прогін тільки в Docker: `docker compose exec api poetry run pytest tests/unit/reports -q`, `ruff check .`, `mypy app`.

---

## §8. Definition of Done / Gate

- **G1.** `pytest tests/unit/reports -q` зелений; `ruff`, `mypy app` чисті.
- **G2.** Звірка §5 пройдена на 4 клієнтах §5.3 у межах допусків §5.2. Вивід — у Completion report. **Не пройдено = таска не готова.**
- **G3.** Живий прогін `monthly` з `google_api` через `#/reports` доходить до `succeeded` і віддає документ; `docker compose logs` без трейсбеків.
- **G4.** Прогін із `google_api` **справді стартує** (не залишається `queued`) — доказ, що пул §3.3 підключений.
- **G5.** Міграцій не додано; `alembic heads` — один.
- **G6.** У жодному з `result_json`, подій прогону чи текстів помилок немає токена (`grep` по вивантаженому прогону).

---

## §9. Follow-ups

- **`TODO(task-86)`** — `comparator` з третім стовпчиком (§1).
- **`TODO`** — після тижня стабільної роботи розглянути `google_api` як дефолт майстра.
- EFFYS/GRP на URL-prefix property — перевести на `sc-domain:` у Search Console (операційна дія, не код).

---

## ✅ Completion report

- **Дата:** 2026-09-07
- **Гілка / комміт:** `feat/task-83-google-api-direct-transport` @ `811fdcb` (task-84 закомічено окремим комітом одразу після цього)
- **Статус:** done

### Що зроблено

Третє джерело звітів `google_api` живе поруч з `api`/`pdf` у майстрі, планувальнику й ретраї періодів. Оператор обирає картку «Google API» на кроці 4 — звіт іде напряму через GSC Search Analytics API + GA4 Data API (транспорт task-83), без жодного звернення до Reporting Ninja. Цифри перевірені живо на 4 клієнтах і збігаються з `api`-джерелом **байт-у-байт** по всіх 48 клітинках (8 метрик × 6 періодів) — це i є критерій успіху (§0.6/§5): дані не зрушили.

### Як зроблено (ключові рішення)

- **`app/reports/google_source.py`** (новий) — дзеркало `api_source.py`: та сама модель атрибуції (тотали без дименсії, organic/direct окремим запитом по `firstUserDefaultChannelGroup`, точний збіг каналу, `0` ≠ `None`), той самий `PeriodMetrics`. Транспорт — `GscClient`/`Ga4Client`/`open_google_api_session` з task-83, використані як є. Додатково (понад буквальний §2) відтворено й місячний блок **Historical Data** через прямий API (`fetch_history`), щоб `google_api`-звіт не програвав `api`-звіту відсутньою секцією — це свідоме розширення обсягу, не в шкоду плану.
- **`app/reports/runner.py`** — розвилка `if/elif/elif/else raise ValueError` (§0.2), нова `_run_google` (копія структури `_run_api`): `comparator` явно відхиляється (`error_code="source_unsupported"`), `GoogleApiPermissionError` перериває прогін як `not_configured` (не фарбує періоди червоним, §3.4), м'яке попередження, коли GSC не може дати дані старші за ~16 місяців (§0.7/§4.3).
- **`app/reports/scheduler.py`** — четвертий пул (`_google_api_pool`/`reports_google_api_workers`, дефолт 4), один спільний `_fill("google_api", …)` для standalone і batch-дітей одразу (на відміну від `api`, тут немає ні браузера, ні ліміту 300/хв кабінету RN — розділяти на два пули поки нема сенсу; якщо колись з'явиться батч на 50+ клієнтів, розділити за прецедентом `api`/`reports_batch_client_workers`).
- **`app/reports/period_retry.py`** — `_refetch_google`, підключено в `retry_period`'s `if/elif/else`.
- **`app/schemas/reports.py`** — `ReportSource = Literal["api", "pdf", "google_api"]`.
- **Фронт:** `reports-wizard.js` — третя картка «Google API» / «Direct from Google — no Reporting Ninja», оцінка часу як в `api` (нового поля в `ReportTypeEstimateDTO` не додавали), явне блокування `comparator + google_api` в `step4Reason` (той самий гейт, що й у `runner`, але видимий ДО запуску). `reports-page.js` — `google_api` у вайтлисті фільтра джерела + виправлення `filesCountLabel` (раніше перевіряло лише `source === "api"`).

### Контрактні зміни (Contract changes)

1. `ReportSource` — додано `"google_api"`. Споживачі: `runner`, `scheduler`, `period_retry`, `app/api/reports.py` (не редагувався, приймає рядок як є), фронт-майстер. task-02 не згадує `ReportSource` (контракт з'явився пізніше, у task-42) — синхронізувати нема чого.
2. **⚠️ Відхилення від плану — знадобилась міграція, хоча план стверджував «не потрібна».** План звірив лише Python-`Literal`-шар; на живій Postgres `report_runs.source`/`report_batches.source` виявились `VARCHAR(8)` (модель `String(8)`, коментар `# api|pdf`) — `"google_api"` (10 символів) падав з `StringDataRightTruncation` на першому ж INSERT. Користувач підтвердив: розширити колонку. Міграція `b7d3f9a24e18` (`down_revision=c3a91f7e2b6d`) — `op.alter_column` обох колонок до `String(16)`, без `batch_alter_table` (Postgres, метаданна операція, без переписування таблиці). `app/db/models.py` синхронно оновлено (`String(8)` → `String(16)`, коментар виправлено). **Наступна нова міграція (task-85) має чейнитись від `b7d3f9a24e18`, не від `c3a91f7e2b6d`** — попереджено власника task-85.
3. Додатково додано `Settings.reports_google_api_workers: int = 4` (адитивно, task-84 owned path).

### Як перевірити (verification)

```bash
docker compose exec api poetry run pytest tests/unit/reports -q        # 267 passed
docker compose exec api poetry run pytest tests/unit/reports/test_google_source.py -q   # 14 passed
docker compose exec api poetry run pytest tests/unit/api tests/unit/jobs tests/unit/core -q  # 667 passed, 1 pre-existing deselected
docker compose exec api poetry run ruff check .                        # чисто
docker compose exec api poetry run mypy app                            # чисто
docker compose exec api poetry run alembic heads                       # b7d3f9a24e18 (один)
```

**Живий гейт §5/§8 (виконано на бойовому стеку, реальні клієнти):**

- **G1** — зелено (вище).
- **G2** — `compare`-скрипт по 4 клієнтах §5.3, обидва прогони через реальний REST (`POST /reports/runs`, `kind=monthly`, `force_refresh=true`), звірка `GET .../table`:
  - **SHB** (`sc-domain`) — 48/48 клітинок (8 метрик × 6 періодів) **байт-у-байт** ідентичні `google_api` vs `api`.
  - **SAPHNY** (найдовша історія, впирається у 16-місячну межу) — усі клітинки в межах вікна ідентичні; окремо перевірено `custom_months` на `2025-01` (20 міс. тому): **обидва** джерела дають `None`/`—`, не `0` — GSC-межа не створює розбіжність, лише чесну відсутність.
  - **EFFYS** (URL-prefix GSC property) — CLEAN, 0 розбіжностей.
  - **3XE** (декілька GA4-property на акаунті) — CLEAN, 0 розбіжностей; отже `account_id` резолвиться правильно.
  - Допуски §5.2 не знадобились — розбіжність усюди рівно `0`, включно з `users`/`organic_users`/`direct_users` (план дозволяв ≤1%).
- **G3** — `monthly google_api` для SHB: `queued → running → succeeded`, 6/6 періодів, 0 failed, ~10–16с. `GET .../document` → `200`, 25 КБ HTML з `.report-doc__page`; XLSX (8.8 КБ) і client PDF (42 КБ) записані як файли прогону. `docker compose logs worker` за час прогону — жодного трейсбеку з `app.reports.*`/`google_source`/`runner`.
- **G4** — `report_scheduler_started api_workers=3 google_api_workers=4 pdf_workers=1` у логах воркера після рестарту; прогін реально перейшов у `running` протягом секунд (не завис `queued`).
- **G5 (амендовано користувачем):** міграцій **додано одну** (не нуль, як планувалось) — `alembic heads` дає рівно один головний ревижн, `b7d3f9a24e18`. Рестарт `worker`/`api` після міграції обов'язковий: одразу після `ALTER COLUMN` воркер, що тримав старі з'єднання, кидав `psycopg.errors.FeatureNotSupported: cached plan must not change result type` на кожному тіку планувальника (~8 хв, самостійно не минуло) — рестарт контейнерів вирішив це миттєво. Внесено як експлуатаційну нотатку, не як дефект коду.
- **G6** — `result_json`/`periods_json`/тексти подій/помилок прогону SHB: рядковий пошук фрагментів `google_api_refresh_token`/`google_api_client_id`/літералу `"refresh_token"` — **не знайдено**.
- Фронт: живий Playwright-прогін проти `http://127.0.0.1:6524` (агентський bearer-токен) — 3 картки джерела рендеряться коректно (`API — fast` / `Google API` / `PDF — evidence`), нотатка про GSC-ліміт видима, фільтр історії має `google_api`, діалог майстра не виходить за межі viewport на 390/768/1440px, консоль без помилок. Скріншот — у розмові.
- **Бекап перед міграцією:** `pg_dump --format=custom` контейнера `db` → `app_data/backups/pre-task84-20260907-170513.dump` (121 МБ), до `ALTER TABLE`.

### Тести

`tests/unit/reports/test_google_source.py` (14 тестів, новий файл):

1. Головний тест (§7.1) — `google_source.fetch_period_metrics` і `api_source.fetch_period_metrics` з еквівалентних фейкових відповідей дають **однаковий** `PeriodMetrics` по всіх 8 полях.
2. Точний збіг каналу — `Organic Social`/`Organic Shopping` не потрапляють в `organic_*` (§7.2).
3. Нуль рядків GA4 → `None`; рядок зі значенням `0` → `0`; без каналу — `organic_*`/`direct_*` теж `None` (§7.3).
4. GSC: порожній період → `None`; справжній нуль-день лишається `0`.
5. `keyEvents` без `conversions` в одному GA4-запиті (§7.4, regression-тест на INTERNAL-пастку).
6. `gsc_site_url=None` → `clicks`/`impressions` лишаються `None`.
7. Невідоме `source` в `runner.run()` → `failed` з `"unknown report source"` в помилці, PDF-шлях НЕ викликається (мокнуто й `pytest.fail`, якщо викликаний) — §7.5.
8. `google_api`-прогін реально claim-иться `ReportScheduler._tick()` (не лишається `queued`) — регресія на §0.3/G4.
9. `403 PERMISSION_DENIED` (`GoogleApiPermissionError`) → run `error_code="not_configured"`, `periods_failed == 0` (перериває, не фарбує періоди) — §7.7.
10. `comparator` + `google_api` → `error_code="source_unsupported"` — §7.8.
11. `period_retry.retry_period` для `source="google_api"` викликає саме `_refetch_google`, не `_refetch_api`/`_refetch_pdf` (мокнуто з `pytest.fail` на неправильний шлях).
12. `_refetch_google` бере `ga4_property_id`/`gsc_site_url` з `account_id` інтеграцій, без `connection_key`.

Плюс регенеровано `tests/unit/api/data/openapi_snapshot.json` (новий `google_api` у `ReportSource`-enum на 5 схемах) і `openapi.json` знову збігається зі снепшотом.

### Follow-ups / борг

- Успадковані з плану: `TODO(task-86)` (comparator third column), дефолт майстра після тижня стабільності, EFFYS/GRP на `sc-domain:`.
- **Виявлено під час виконання, не в обсязі задачі:**
  - `tests/unit/db/test_alembic_migrations.py::test_upgrade_head_then_downgrade_base_is_clean` і `tests/unit/db/test_db_health.py::test_ready_after_migration` — червоні **до** цієї таски: SQLite-реплей ланцюга міграцій падає на `ea2070f4c343` (task-79, `agent_tokens`) через `table already exists`, бо `9701790bda11` («postgres baseline») створює схему через `Base.metadata.create_all()` з ПОТОЧНИХ моделей, а не історичних — тобто `agent_tokens` вже існує до того, як `ea2070f4c343.upgrade()` спробує його створити. Перевірено: та сама помилка на `811fdcb` без жодної правки цієї таски. Поза обсягом task-84.
  - `tests/contract/test_aiseotools_signatures.py::test_public_surface_matches_snapshot` — червоний до цієї таски: `seo_audit.tech_audit.pipeline.run_import`/`build_tech_xlsx` в AISeoTools отримали нові параметри (`headed`, `virtual_display`, `include_visual`) без оновлення знімка. Не звідси, не чіпав.
  - `tests/unit/api/test_tech_audit.py::test_dashboard_card_counts_measured_open_errors` — червоний до цієї таски (пов'язано з тим самим дрейфом AISeoTools). Не звідси.
  - `tests/unit/api/test_openapi_snapshot.py` — був червоний ДО цієї таски (дрейф від task-78/80, які не оновили знімок після мержу). Регенерація снепшota для `google_api` заразом підхопила і цей старий дрейф — снепшот зараз відповідає живому `openapi.json` повністю, тест зелений.
  - `#/reports` має preexisting горизонтальний скрол на 390px (історична таблиця, `analytics-table reports-table`, не в `.table-scroll`/`contain: paint`) — той самий клас багу, що вже ловили на `#/blogs` (task-26). Не з цієї таски (відтворено на `811fdcb` без правок task-84), але варта окремого тікета.
  - `app/api/reports.py`'s preflight (`_create_run`) досі вимагає `rn_integration.config_json.get("connection_key")` для БУДЬ-ЯКОГО джерела, хоча `google_api` потребує лише `account_id`. У практиці кожен клієнт з RN-інтеграцією має обидва поля, тож це не блокувало жоден живий прогін — але семантично зайва вимога. Файл поза owned paths цієї таски; залишено як TODO для власника `app/api/reports.py`.
  - Футер документа досі жорстко каже «via Reporting Ninja» навіть для `google_api`-прогону (видно на живому HTML) — це вже занотовано в task-85 §0 як те, що вона має виправити.
