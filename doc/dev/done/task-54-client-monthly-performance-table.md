# task-54 — Monthly Performance: місячна таблиця клієнта «1 в 1» зі операторським Google Sheet

- **Пакет:** новий блок **Monthly Performance** на сторінці клієнта (`#/client/<id>`) — таблиця «місяць × 7 метрик», що структурно й візуально повторює операторський Google Sheet (скріншот користувача, 2026-08-21), із персистентними закритими місяцями та кнопкою **Refresh**.
- **Хвиля:** Q — Client Monthly Table (перша таска хвилі).
- **Depends on:** **42** ✅ (`app/reports/{ninja_api,api_source,history}.py` — RN-клієнт, дискаверинг полів, GSC day-дискримінатор), **45** ✅ (місячний блок історії — прямий предок), **18** ✅ (сторінка клієнта), **06** ✅ (REST), **13** ✅ (harness)
- **Не конфліктує** з task-51 (`app/monitor/**`) і task-53 (`app/contentqa/**`) — зони не перетинаються.
- ⚠️ **Спільні фронт-файли зайняті паралельною сесією** (перевірено 2026-08-21, `git status` нечистий): `frontend/js/client-page.js`, `frontend/js/api.js`, `frontend/index.html` — незакоммічені правки task-53 (bump `?v=20260821-spin2` по всіх модулях + Content QA). Це рівно ті три файли, куди йдуть наші адитивні дотики. **Порядок роботи:** спершу ізольована частина (бекенд + `frontend/js/monthly-table.js` + `frontend/css/monthly-table.css`), до спільних файлів вертатись, коли їх `git status` чистий (див. `multi-agent-collision-avoidance`).
- **Owned paths:** `app/reports/monthly_table.py`, `app/api/client_monthly.py`, `app/schemas/client_monthly.py`, `app/db/repositories/client_monthly.py`, `migrations/versions/<rev>_client_monthly_metrics.py`, `frontend/js/monthly-table.js`, `frontend/css/monthly-table.css`, `tests/unit/reports/test_monthly_table.py`, `tests/unit/api/test_client_monthly_api.py`, `tests/e2e/frontend/client-monthly-table.cjs`
- **Адитивні дотики (узгодити):** `app/db/models.py` (+2 моделі: `ClientMonthlyMetric`, `ClientMonthlyTableSettings`), `app/main.py` (+1 `include_router`), `app/core/config.py` (+2 налаштування), `frontend/js/client-page.js` (+1 секція у `shell()`, +1 лоадер), `frontend/js/api.js` (+4 функції), `frontend/index.html` (+1 `<link>`), `tests/unit/api/data/openapi_snapshot.json` (перегенерувати)
- **Міграцію створює сама таска** — живий прецедент task-27/29/50/51/52/53 (див. task-53 §преамбула: правило CLAUDE.md «міграції лише task-03» стосується bootstrap-етапу схеми, а не кожної наступної фічі).
- **PLAN.md:** §12 (`None` ≠ `0`; snapshot ≠ range; чесні стани), §13 (секрети не течуть у БД/логи/UI), §16 (хвиля Reports — прямий контекст)
- **frontend/AGENTS.md:** §2 (токени), §5 (правда даних), §6 (стани), §11 (адаптивність), §14 (копірайт EN), §15–16 (візуальний loop і чекліст). **Свідомі відхилення від §2/§3 — у §7 нижче, узгоджено з користувачем.**
- **AISeoTools:** змін **не потребує**.

---

## Мета (робота оператора, яку закриваємо)

Оператор веде цю таблицю **вручну в Google Sheets** — по рядку на місяць, по клієнту на аркуш. Скріншот користувача (SDS, Dec 2024 → July 2026) показує, що з цього виходить:

- **дрейф визначень:** колонка `Total organic Users` до Jan 2026 не збігається з жодною живою моделлю атрибуції, з Jan 2026 — збігається точно (7/7 місяців). Тобто метод змінювався по ходу;
- **змішані атрибуції в одному рядку:** `721 users` — це first-user атрибуція, а `Book Now 70` того ж місяця — session (обидва зійшлися точно, §1.2);
- **ручні артефакти:** `Impressions` за May/June 2026 округлені (`73,400` проти живих `73,395`; `63,700` проти `63,671`), `CTR` за July 2026 — протягнутий копіпастом із червня (`1.30%` замість фактичних `1.04%`), `Book Now` за Apr/May 2026 (`170`/`178`) не збігається ні з organic (`109`/`104`), ні з усіма каналами (`265`/`261`);
- **незворотна втрата даних:** GSC тримає 16 місяців. Рядки Dec 2024 – Mar 2025 у Sheet існують, а в API їх **уже немає** і не буде.

Ціль — щоб та сама таблиця збиралася машиною за секунди, мала одне зафіксоване визначення на колонку, і щоб **кожен закритий місяць морозився назавжди** — тоді 16-місячне вікно GSC більше ніколи не з'їсть історію.

---

## 1. Дослідження (живі виміри, 2026-08-21)

Усе нижче виміряно в контейнері (`docker compose exec api`) проти реального Reporting Ninja API. Клієнт зі скріншота — **SDS** (`shorelinedentalstudio.com`, `properties/309651330`, GSC `sc-domain:shorelinedentalstudio.com`), підтверджено користувачем.

### 1.1 Reporting Ninja віддає всі потрібні поля

`GET /fields` для `ga4` (≈380 полів) містить `yearMonth`, `totalUsers`, `activeUsers`, `keyEvents`, `conversions`, `eventName`, `eventCount`, `firstUserDefaultChannelGroup`, `sessionDefaultChannelGroup`, `sessionMedium`, `userConversionRate`. Для `google_search_console` (12 полів) — `date`, `month`, `clicks`, `impressions`, `ctr`, `query`, `page`, `device`, `country`.

Тобто **весь звіт зі скріншота збирається без жодного нового провайдера** — тим самим RN-ключем, що вже працює.

### 1.2 Звірка скріншота з живими даними — де збігається, а де ні

`sheet` = зі скріншота; `firstUser` = `firstUserDefaultChannelGroup`; `session` = `sessionDefaultChannelGroup`; обидві по каналу **точно** `Organic Search`.

| Місяць | sheet users | firstUser users | session users | sheet clicks | live clicks | sheet impr | live impr | sheet conv | firstUser conv | session conv | sheet Book Now | firstUser BN | session BN |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 2024-12 | 1208 | 581 | 559 | 625 | **немає** | 73,023 | **немає** | 117 | 129 | 121 | — | 61 | 56 |
| 2025-03 | 831 | 741 | 706 | 735 | **немає** | 97,120 | **немає** | 149 | 168 | 155 | 76 | 77 | 66 |
| 2025-04 | 770 | 740 | 689 | 723 | 572 ⚠️ | 92,376 | 73,293 ⚠️ | 189 | 215 | 199 | 133 | 140 | 128 |
| 2025-10 | 788 | 804 | 750 | 759 | **759 ✓** | 66,571 | **66,571 ✓** | 145 | 162 | 134 | 68 | 73 | 62 |
| 2025-12 | 706 | 705 | 665 | 675 | **675 ✓** | 67,693 | **67,693 ✓** | 132 | 145 | 126 | 56 | 72 | 63 |
| 2026-01 | 798 | **798 ✓** | 758 | 736 | **736 ✓** | 69,086 | **69,086 ✓** | 182 | 198 | 175 | 103 | 107 | 97 |
| 2026-02 | 735 | **735 ✓** | 688 | 667 | **667 ✓** | 72,253 | **72,253 ✓** | 156 | 165 | 149 | 101 | 103 | 94 |
| 2026-03 | 791 | **791 ✓** | 749 | 719 | **719 ✓** | 82,783 | **82,783 ✓** | 181 | 190 | 174 | 109 | 114 | 104 |
| 2026-04 | 706 | **706 ✓** | 658 | 677 | **677 ✓** | 87,610 | **87,610 ✓** | 151 | 169 | **151 ✓** | 170 ⚠️ | 109 | 99 |
| 2026-05 | 771 | **771 ✓** | 719 | 667 | 677 ⚠️ | 73,400 | 73,395 ≈ | 157 | 171 | 149 | 178 ⚠️ | 104 | 94 |
| 2026-06 | 852 | **852 ✓** | 806 | 832 | **832 ✓** | 63,700 | 63,671 ≈ | 190 | 193 | 185 | — | 119 | 112 |
| 2026-07 | 721 | **721 ✓** | 666 | 717 | **717 ✓** | 69,176 | **69,176 ✓** | 111 | 132 | 109 | **70** | 88 | **70 ✓** |

**Що з цього випливає:**

1. **`Total organic Users` = `totalUsers` × `firstUserDefaultChannelGroup == "Organic Search"`.** Збіг **точний за всі 7 місяців 2026** (798/735/791/706/771/852/721). Це GA4-звіт «User acquisition». До Jan 2026 sheet розходиться — це заморожені ручні значення, а не інше визначення.
2. **`Clicks`/`Impressions` = GSC, `search_type=web`.** Збіг точний за Oct 2025 – Jul 2026, окрім двох ручних артефактів (`667` замість `677` за May 2026; округлення `73,400`/`63,700`).
3. **`Organic "Book Now"` = `keyEvents` для `eventName == "Book Now"` × `sessionDefaultChannelGroup == "Organic Search"`.** За найсвіжіший місяць збіг **точний** (70). `firstUser` дав би 88.
4. **`Organic Conversions` — найгрязніша колонка.** Sheet `111` за Jul 2026 = session Organic Search (`109`) **+ Organic Social (`2`)**; за Apr 2026 sheet `151` = session Organic Search рівно (`151`). Тобто оператор інколи додавав Organic Social, інколи ні. Жодна єдина формула не відтворює цю колонку за всі місяці — і не має відтворювати.
5. **`Conversion rate` = conversions / users**, не сесійна. Перевірено на двох краях: `117/1208 = 9.69%` (Dec 2024) і `111/721 = 15.40%` (Jul 2026) — обидва точно як у sheet.
6. **`CTR` = clicks / impressions.** Перевірено: June 2026 `832/63,700 = 1.306% → 1.30%` точно. July 2026 у sheet — протягнутий `1.30%`, фактичне `717/69,176 = 1.04%`.

### 1.3 Рішення користувача (2026-08-21, зафіксовані)

| Питання | Рішення |
|---|---|
| Атрибуція | **Налаштування на клієнта, дефолт — змішана:** `users` = first-user, `conversions` + названа подія = session. Дефолт відтворює найсвіжіший рядок скріншота; оператор може перевести клієнта на єдину модель. |
| Історія до вікна GSC | **Лише те, що дає API.** Dec 2024 – Mar 2025 показують `—`. Імпорт старого Sheet **не робимо**. |
| Вигляд | **Світлий sheet-fidelity блок** на сторінці клієнта: біле тло, ті самі кольори заголовків, той самий MoM зелений/червоний. |
| Обсяг | **Блок на сторінці клієнта + Refresh.** XLSX-експорт, Copy-to-clipboard і пакетний прогін на портфель — **не** в цій тасці (follow-ups, §9). |

### 1.4 Два живих обмеження API, які диктують форму запитів

**(а) `conversions` і `keyEvents` в одному запиті → `INTERNAL`.** Виміряно: `[yearMonth, firstUserDefaultChannelGroup, totalUsers, conversions, keyEvents]` падає з `INTERNAL: There was an internal error` навіть після 3 ретраїв, тоді як кожна з них окремо в тому ж наборі проходить. Це GA4-аліаси однієї метрики (`conversions` перейменовано на `keyEvents`), і RN на такому наборі ламається. **Ніколи не запитувати їх разом.** Наявний retry на `INTERNAL` (`ninja_api._RETRYABLE_ERROR_CODES`) тут не врятує — помилка детермінована.

**(б) GSC із дименсією `date` — єдиний чесний дискримінатор «немає даних» vs «нуль».** Виміряно на SDS:

| Запит | Dec 2024 | Mar 2025 | Apr 2025 |
|---|---|---|---|
| `[month, clicks, impressions]` | `{clicks: 0, impressions: 0}` — **брехливий нуль** | те саме | дані |
| `[date, clicks, impressions]` | **0 рядків** → `None` ✓ | 0 рядків ✓ | **23 денних рядки** із 30 |

Тобто `month`-дименсія малює нулі там, де насправді немає даних (той самий капкан, що task-45 §1.6 уже задокументував для запиту без дименсії). **Використовуємо `date` і групуємо місяці локально** — так, як уже робить `api_source.fetch_gsc_daily`. Побічний, але важливий бонус: кількість денних рядків дає **детекцію неповного місяця** — Apr 2025 має 23/30 днів (край 16-місячного вікна), і подавати його як повний місяць було б брехнею (PLAN §12).

### 1.5 Названа колонка — це **набір** подій, а не одна

Прогін `[eventName, sessionDefaultChannelGroup, keyEvents]` за July 2026 по всіх 14 клієнтах:

| Клієнт | Топ-key-events (organic, Jul 2026) | Що це означає для колонки |
|---|---|---|
| SDS | `Book Now=70`, `Phone=20`, `Address=9`, `Email=7` | одна подія — колонка тривіальна |
| NYCHA | `Book Now=114`, `Address=16`, `Phone=13` | одна подія |
| **PJC** | `Book Now 3rd Avenue=1255`, `Book Now Hudson=1212`, `Book Now Lincoln Square=1083`, `Book Now Manhattan West=654` | **4 події = одна колонка `Book Now`** |
| **SHB** | `Order Now Lakewood=2312`, `order now 5 towns=1592`, `Order now Woodbourne=1130`, `Order Now=1122`, `Order now Monsey=1053`, `Order now Miami=932` | 6+ подій, різний регістр |
| **SAPHNY** | `Book now S39=255`, `Book now STSQ=244`, `Book now S60=177`, `SMS=250` | локації + окремий канал |
| EFFYS | `Order UWS=222`, `Order UES=140`, `Order Columbus Ave=124` | 3 події |
| GRP | `Order online Lexington Ave=325`, `Order online Queens=129` | 2 події |
| **LAXTON** | `cookie_consent_update=993`, `cookie_consent_marketing=743`, `cookie_consent_preferences=743`, `cookie_consent_statistics=743`, `purchase=91` | **топова подія — шум згоди на куки**; авто-вибір «найбільша подія» дав би сміття |
| 3XE | `Buy ticket btn=510`, `Location Book here btn=467`, `Address=410` | 2 «справжні» + `Address` |
| ALPHA | `add_to_cart=48`, `purchase=2` | e-commerce, не бронювання |

**Висновки, що йдуть у дизайн:**
- колонка №7 приймає **список** імен подій і сумує їх (інакше PJC/SHB/SAPHNY/EFFYS отримають чверть свого показника);
- **авто-вибір заборонений як єдиний механізм** — LAXTON доводить, що топова подія може бути шумом. Дискаверинг тільки **пропонує** відсортований список, вибір підтверджує оператор;
- лейбл колонки — окреме поле (`Book Now`, `Order Now`, `Buy ticket`), бо заголовок `Organic "Book Now"` не універсальний.

### 1.6 Глибина історії

- **GA4 через RN:** SDS має organic users щомісяця мінімум із **2023-09** (перевірено: 2023-09=638 … 2024-12=581). Колонки users/conversions/подія покриваються глибоко.
- **GSC:** 16 місяців, для SDS перший місяць із даними — Apr 2025, і той неповний (23/30 днів).
- Скріншот покриває 20 місяців → **вікно за замовчуванням 24 повні місяці** (`reports_max_months = 24` уже існує як стеля в конфізі).

### 1.7 Що вже написано і **не** переписується

| Уже є | Файл | Як використовуємо |
|---|---|---|
| RN REST-клієнт із shared token bucket (240 rpm), retry на 429/5xx/`INTERNAL`, `request_count` | `app/reports/ninja_api.py` | **read-only**, імпорт як є |
| Дискаверинг полів GA4/GSC | `app/reports/api_source.py` (`discover_ga4_fields`, `discover_gsc_fields`) | **read-only**, імпорт |
| Денний GSC-фетч із дискримінатором | `api_source.fetch_gsc_daily`, `GscDailyRow` | **read-only**, імпорт |
| Місячне групування, `month_keys`, `window_bounds`, `compute_ctr`, `group_daily_by_month` | `app/reports/history.py` | **read-only**, імпорт (не копіювати `compute_ctr` — там уже правильне `None` при нульових показах) |
| Сайзинг `limit` під денну дименсію | `api_source._row_limit` | приватна — **дублюємо** мінімальний аналог у своєму модулі з посиланням, щоб не правити чужий файл |
| Per-client lazily-created налаштування | `MonitorSettings` + `get_or_create_settings` | **шаблон** для `ClientMonthlyTableSettings` |
| Ізольована світла «паперова» поверхня зі власними токенами | `frontend/css/report-doc.css` | **прецедент** для sheet-fidelity CSS (§7) |

---

## 2. Рішення (архітектура)

### 2.1 Чому не `ReportRun`, а власна таблиця

Наявний двигун звітів (`report_runs`) — це **прогін із параметрами**: черга, lease, heartbeat, SSE, файли, крос-перевірка. Тут потрібне протилежне: **накопичувальний реєстр місяців**, який живе довше за будь-який прогін і читається сторінкою клієнта миттєво, без провайдера.

Ключова причина — **16-місячне вікно GSC**. Якщо таблиця щоразу перезбирається з API, то в березні 2027 рядок Oct 2025 просто зникне. Тому:

> **Закритий місяць, зібраний успішно, морозиться і більше ніколи не перезапитується.**

Це те, чого наявний `ReportHistory` (task-45) не робить: він щоразу тягне вікно з нуля і тримає результат у `result_json` одного прогону.

### 2.2 Схема (2 нові таблиці)

```
client_monthly_metrics          -- один рядок = один (клієнт, місяць)
  id, client_id → clients.id CASCADE
  month                     TEXT  "YYYY-MM"
  organic_users             INT | NULL
  organic_conversions       INT | NULL
  key_event_count           INT | NULL     -- сума налаштованого набору подій
  gsc_clicks                INT | NULL
  gsc_impressions           INT | NULL
  gsc_days_with_data        INT | NULL     -- скільки денних рядків віддав GSC
  days_in_month             INT            -- календарна довжина місяця
  attribution_users         TEXT           -- "first_user" | "session" (провенанс рядка)
  attribution_events        TEXT
  key_event_names_json      JSON           -- які саме події сумувались у цей рядок
  is_frozen                 BOOL           -- закритий + устоявся + фетч успішний
  ga4_state                 TEXT           -- "ok" | "no_data" | "not_configured" | "failed"
  gsc_state                 TEXT           -- те саме
  collected_at, created_at, updated_at
  UNIQUE (client_id, month)

client_monthly_table_settings   -- lazily created, шаблон MonitorSettings
  id, client_id → clients.id CASCADE, UNIQUE (client_id)
  key_event_names_json      JSON  default []      -- [] = колонка ще не налаштована
  key_event_label           TEXT | NULL           -- "Book Now"; NULL = колонка приховується
  attribution_users         TEXT  default "first_user"
  attribution_events        TEXT  default "session"
  history_months            INT | NULL            -- NULL = глобальний дефолт (24)
  created_at, updated_at
```

**Чому `attribution_*` і `key_event_names_json` дублюються в рядку метрик:** інакше зміна налаштувань **безшумно переінтерпретує** заморожені рядки — таблиця почне змішувати моделі рівно так, як це зробив ручний Sheet (§1.2). З провенансом у рядку UI може сказати «ці 8 місяців зібрані іншою моделлю — перезібрати?» замість тихої брехні.

**Правило заморозки** (`is_frozen = True`) — усі три умови разом:
1. місяць **закритий** (не поточний);
2. закритий **≥ 3 дні тому** (GA4 доуточнює дані ~48 год; 3 дні — запас);
3. фетч **успішний**, тобто `ga4_state`/`gsc_state ∈ {ok, no_data, not_configured}`. `failed` **не морозиться** ніколи.

`no_data` морозиться свідомо: місяць поза вікном GSC не «зламався», його там **немає і не буде** — і саме це треба зафіксувати назавжди, щоб не перезапитувати щоразу.

Форс-перезбір (`?force=month`) — технічний вихід із заморозки, окремо від «ручного редагування», якого користувач не замовляв.

### 2.3 Збір: 3 або 5 запитів на клієнта

`app/reports/monthly_table.py` — чистий модуль, без БД (як `history.py`):

| Режим | Запити | Набір полів |
|---|---|---|
| **єдина атрибуція** (users і events однакові) | 3 | `[date, clicks, impressions]` + `settings={"search_type":"web"}` · `[yearMonth, <channel>, totalUsers, keyEvents]` · `[yearMonth, eventName, <channel>, keyEvents]` |
| **змішана** (дефолт) | 4 | GSC · `[yearMonth, firstUserDefaultChannelGroup, totalUsers]` · `[yearMonth, sessionDefaultChannelGroup, keyEvents]` · `[yearMonth, eventName, sessionDefaultChannelGroup, keyEvents]` |

Плюс 2 запити дискаверингу полів (`/fields` × 2) при першому зверненні. Виміряно: повне вікно 20 місяців — **≈2–4 с**, 4 запити. Пакетний ризик відсутній — глобальний token bucket (240 rpm) уже спільний на процес.

**Захист від тихої недорахованості:**
- `limit` для `eventName`-запиту сайзиться від вікна (`months × 60 + 100`; живий вимір SDS — 505 рядків на 20 місяців). Якщо `len(rows) == limit` — це **можлива обрізка**: пишемо `truncated: true` у стан колонки і показуємо попередження, а не тихо занижене число;
- якщо серед `eventName` є GA4-агрегат `(other)` — теж прапорець у стан колонки (висока кардинальність могла злити рідкісні події);
- канал матчиться **точним** порівнянням (`== "organic search"`, case-insensitive), не підстрокою — інакше `Organic Search` всмоктає `Organic Social`/`Organic Shopping`/`Organic Video` (та сама причина, що в `api_source._sum_exact_channel`). Це, до речі, і є джерело розходження `111` vs `109` у скріншоті (§1.2 п.4).

### 2.4 Похідні колонки — рахує бекенд, не DOM

`Conversion rate = organic_conversions / organic_users` і `CTR = gsc_clicks / gsc_impressions` віддаються з API вже порахованими (AGENTS.md §5: «Do not calculate business meaning in the DOM»). Обидві — `None`, коли бракує будь-якої сторони **або** знаменник `0` (ділення на нуль — це не `0.00%`; те саме правило, що `history.compute_ctr` і `table.safe_pct`).

MoM-напрямок (зелений/червоний) теж рахує бекенд: `direction: "up" | "down" | "flat" | null` на клітинку. `null` — коли попереднього місяця немає або одна зі сторін `None`. Усі 7 метрик — `higher is better`, але напрямок і колір **не** виводяться зі знака в DOM: поле `direction` приходить з API (AGENTS.md §5).

### 2.5 REST (новий модуль, щоб не чіпати чужі роутери)

`app/api/client_monthly.py`, префікс `/api/v1/clients/{client_id}/monthly-table`:

| Метод | Шлях | Що робить |
|---|---|---|
| `GET` | `` | Читає **збережену** таблицю. Провайдера не торкається → миттєво. Віддає `rows`, `columns` (лейбли + провенанс), `settings`, `coverage`, `collected_at`, `stale` |
| `POST` | `/refresh` | Збирає відсутні/незаморожені місяці. `?force=YYYY-MM` — перезбір конкретного, `?force=all` — усього вікна. Синхронно, hard timeout `client_monthly_refresh_timeout_seconds` (default 90). Віддає ту саму DTO + `requests_used` |
| `GET` | `/key-events` | Дискаверинг: `[{event_name, key_events, months_seen}]` за останні 12 міс, спадно. Для селектора набору подій |
| `PUT` | `/settings` | Набір подій, лейбл, атрибуції, вікно. `409 attribution_changed` із переліком місяців, зібраних старою моделлю (не блокує — інформує) |

Помилки — наявний `ErrorDTO`, коди в стилі движка звітів: `404 client_not_found`, `422 invalid_settings`, `409 attribution_changed`, `503 provider_unavailable` (із `Retry-After` на 429 від RN), `409 not_configured` (немає прив'язки `reporting_ninja`).

**Чому синхронний `POST`, а не job:** виміряно 4 запити / 2–4 с на клієнта — черга з lease/heartbeat/SSE тут коштує більше, ніж дає. Ліміт RN уже тримає спільний bucket. Якщо на живому портфелі виявиться довше — переїзд у worker описаний як follow-up (§9), схема цього не потребує.

**Секрети:** `REPORTING_NINJA_API_KEY` читається з env у місці виклику (як `runner._run_api`), у БД/DTO/логах не з'являється — ні в `params`, ні в тексті помилки (PLAN §13).

### 2.6 Стани клітинки й колонки (PLAN §12)

Жодного `0` замість відсутнього значення. Розрізняємо:

| Стан | Коли | Як показуємо |
|---|---|---|
`ok` | значення є (включно зі справжнім `0`) | число |
`no_data` | провайдер відповів, рядків немає (місяць поза вікном GSC) | `—` + tooltip «Search Console has no data for this month (16-month window)» |
`not_configured` | у клієнта немає прив'язки `gsc`/`reporting_ninja` | `—` + примітка над таблицею, які колонки недоступні і чому |
`failed` | запит упав | `—` + попередження; **останні успішні дані лишаються видимими** (AGENTS.md §5) |
`partial` | GSC віддав менше денних рядків, ніж днів у місяці (Apr 2025: 23/30) | число + бейдж `partial` + tooltip «23 of 30 days» |
`unconfigured_column` | набір подій не налаштований | колонка №7 **не рендериться**; на її місці — кнопка «Choose the conversion event» |

---

## 3. Таблиця «1 в 1»: специфікація

### 3.1 Колонки (порядок і заголовки — точно як на скріншоті)

| # | Заголовок (UI, EN) | Тло заголовка | Джерело | Визначення |
|---|---|---|---|---|
| 1 | *(порожній)* | сірий `#D9D9D9` | — | Мітка місяця, `MMM YYYY` (`Intl`, `{month:"short", year:"numeric"}`). Візуально порожній, але має `<span class="visually-hidden">Month</span>` — інакше колонка без назви для скрінрідера |
| 2 | `Total organic Users` | зелений `#93C47D` | GA4 | `totalUsers` × `<attribution_users>ChannelGroup == "Organic Search"` (точний матч) |
| 3 | `Impressions` | жовтий `#FFFF00` | GSC | `impressions`, `search_type=web`, `date`-дименсія → сума по місяцю |
| 4 | `Clicks` | синій `#6D9EEB` | GSC | `clicks`, той самий запит |
| 5 | `Organic Conversions` | червоний `#E06666` | GA4 | `keyEvents` × `<attribution_events>ChannelGroup == "Organic Search"` |
| 6 | `Conversion rate` | зелений `#93C47D` | derived | `col5 / col2`; `None` якщо бракує сторони або `col2 == 0` |
| 7 | `Organic "<label>"` | зелений `#93C47D` | GA4 | `keyEvents` для `eventName ∈ key_event_names` × `<attribution_events>` канал `Organic Search` |
| 8 | `CTR` | синій `#6D9EEB` | derived | `col4 / col3`; `None` якщо бракує сторони або `col3 == 0` |

Точні hex беруться зі стандартної палітри Google Sheets і **звіряються піксельно** зі скріншотом на етапі візуального loop (§8) — це частина гейта, а не припущення.

### 3.2 Порядок рядків і вікно

Найстаріший місяць **зверху** (як на скріншоті й як у `ReportHistory`). Вікно — останні `history_months` (default 24) **повних** місяців: `history.month_keys` уже прив'язується до останнього закритого місяця, тож поточний, відкритий місяць у таблицю не потрапляє (напівмісяць поруч із повними — фальшиве порівняння, PLAN §12).

### 3.3 Формати чисел — за колонкою, як у скріншоті

- `Impressions` — з групуванням: `73,023`;
- `Total organic Users`, `Clicks`, `Organic Conversions`, названа подія — **без** групування: `1208` (на скріншоті єдине 4-значне значення `1208` подане без коми — формат колонки в Sheet саме такий);
- `Conversion rate`, `CTR` — 2 знаки + `%`: `15.40%`, `1.30%`;
- усе через `Intl.NumberFormat("en-US")` (AGENTS.md §5), `font-variant-numeric: tabular-nums`;
- `None` → `—`, **ніколи** `0`.

Кожна колонка форматується однаково в усіх рядках — вимога AGENTS.md §5 «Format one metric consistently everywhere» виконана **на рівні метрики**, а не «однаково всі колонки».

### 3.4 MoM-кольори

Значення фарбується проти **попереднього рядка** тієї ж колонки: краще → зелений, гірше → червоний, однаково/немає бази → нейтральний (як у чорних рядках Dec 2024 – Sep 2025 на скріншоті). Усі 7 метрик — «більше краще». Джерело істини — `direction` з API (§2.4).

---

## 4. Етапи

### 4.1 Бекенд-ядро (ізольоване, стартує одразу)

1. `app/reports/monthly_table.py` — чистий збір і збірка рядків:
   - `MonthlyRow`/`MonthlyTable` dataclass-и з `to_dict()`/`from_dict()` (як `HistoryMonth`/`ReportHistory`);
   - `fetch_monthly_table(client, *, settings, months, today)` — 3–4 RN-запити (§2.3), точний матч каналу, `truncated`/`(other)`-детекція;
   - `build_rows(...)` — групування, похідні, `direction`, `partial`-детекція, стани;
   - `discover_key_events(...)` — список подій для селектора.
2. `app/db/models.py` — 2 адитивні моделі (§2.2) + міграція `<rev>_client_monthly_metrics.py` ланцюжком від `84699ec3400d` (task-53).
3. `app/db/repositories/client_monthly.py` — `get_or_create_settings` (шаблон `MonitorRepository`), `upsert_months`, `list_window`, `months_needing_collection` (реалізує правило заморозки §2.2).
4. `app/schemas/client_monthly.py` — DTO.
5. `app/api/client_monthly.py` — 4 ендпоїнти (§2.5) + `include_router` у `app/main.py`.
6. `app/core/config.py` — `client_monthly_history_months: int = 24`, `client_monthly_refresh_timeout_seconds: float = 90.0`.

### 4.2 Фронт: ізольовані модули (стартують одразу)

7. `frontend/js/monthly-table.js` — `renderMonthlyTable(container, data)`, `renderMonthlyTableError(...)`, попап налаштувань (набір подій, лейбл, атрибуції, вікно), кнопка `Refresh`. Рендер маленькими state-driven функціями, без змішування фетчу/нормалізації/HTML (AGENTS.md §12). Жодного інлайн-`style=` і жодного JS-сетінгу `.style.*` — CSP їх ріже (див. `frontend-verification-tooling`, `task43-reports-ui-done`).
8. `frontend/css/monthly-table.css` — sheet-fidelity поверхня (§7).

### 4.3 Фронт: спільні файли (**лише коли `git status` по них чистий**)

9. `frontend/js/api.js` — `getClientMonthlyTable`, `refreshClientMonthlyTable`, `getClientMonthlyKeyEvents`, `putClientMonthlyTableSettings`. **Request-key — явна іменована мета виклику**, не похідна від параметрів (двічі обпікалися, див. `task36-site-monitor-ui-done`).
10. `frontend/js/client-page.js` — `<section id="cp-monthly">` у `shell()` + лоадер, який не блокує решту сторінки.
11. `frontend/index.html` — `<link>` на `monthly-table.css`.

### 4.4 Тести

12. `tests/unit/reports/test_monthly_table.py` — групування, `None ≠ 0`, похідні при нульовому знаменнику, `direction`, `partial`, набір подій (сума кількох імен), точний матч каналу проти `Organic Social`, детекція обрізки й `(other)`, змішана vs єдина атрибуція (кількість запитів), правило заморозки.
13. `tests/unit/api/test_client_monthly_api.py` — контракти 4 ендпоїнтів, коди помилок, `not_configured` без прив'язки, `409 attribution_changed`; перегенерувати `openapi_snapshot.json`.
14. `tests/e2e/frontend/client-monthly-table.cjs` — Node Playwright, `require`-стиль (`.cjs`), як решта e2e (див. `frontend-verification-tooling`). Скріншоти 390/1440 + станові фікстури.

---

## 5. Фікстури

`frontend/fixtures/client-monthly-table.json` — SDS-подібні 20 місяців із **усіма** станами в одному наборі: 4 місяці з `no_data` по GSC, 1 `partial` (23/30), 1 рядок із `None` у названій події, 1 місяць `failed`, решта `ok`. Це та сама таблиця, що на скріншоті, тож візуальний diff можливий без живого API.

---

## 6. Definition of Done / Gate

**Живий гейт (обов'язковий):**
- [ ] `#/client/18b5d9c8-52a4-49cb-84c1-986f77e069e6` (SDS) рендерить блок Monthly Performance із 20+ рядками.
- [ ] За **Jan–Jul 2026** колонки `Total organic Users`, `Clicks`, `Impressions` збігаються зі скріншотом **точно** (798/735/791/706/771/852/721 · 736/667/719/677/**677**/832/717 · 69,086/72,253/82,783/87,610/**73,395**/**63,671**/69,176). Дві клітинки, де скріншот розходиться (`667` за May, округлення May/June `Impressions`), у Completion report фіксуються як **ручні артефакти Sheet**, а не як наш дефект.
- [ ] `Organic "Book Now"` за **Jul 2026 = 70** при дефолтній (змішаній) атрибуції.
- [ ] `Conversion rate` за Jul 2026 = `109/721 = 15.12%`; розходження зі `15.40%` у Sheet пояснене в Completion report (Sheet додав Organic Social до конверсій, §1.2 п.4).
- [ ] `CTR` за Jul 2026 = **1.04%** (а не протягнутий `1.30%` зі Sheet).
- [ ] Dec 2024 – Mar 2025: `Impressions`/`Clicks`/`CTR` = `—`, **не `0`**; `Total organic Users`/`Organic Conversions` — реальні числа з GA4.
- [ ] Apr 2025 має бейдж `partial` (23/30 днів).
- [ ] `POST /refresh` звітує `requests_used ≤ 6` і вкладається у < 10 с на клієнта.
- [ ] Повторний `POST /refresh` без `force` робить **0** провайдерських запитів по заморожених місяцях (перевірити `requests_used`).
- [ ] Клієнт без прив'язки `gsc` (ALPHA, GOREPORT, LAXTON) → 3 GSC-колонки `—` + іменована причина, решта таблиці працює.
- [ ] `PUT /settings` із набором із 4 подій (PJC) дає колонку, що дорівнює сумі 4 подій.

**Якість:**
- [ ] `ruff check .`, `mypy app`, `pytest tests/unit/reports tests/unit/api` — зелені **в контейнері**.
- [ ] OpenAPI-снапшот перегенеровано.
- [ ] Візуальний loop AGENTS.md §15: скріншоти 390/768/1024/1440 + 200% зум, стани normal/empty/loading/partial/stale/error/dense(24 рядки).
- [ ] Чекліст AGENTS.md §16 пройдено; свідомі відхилення (§7) названі явно.
- [ ] Клавіатура: `Refresh` і попап налаштувань доступні, фокус повертається, `aria-busy` на секції.
- [ ] Горизонтального скролу **сторінки** немає — таблиця скролиться у власному `overflow-x: auto`.

**Процес:**
- [ ] План перенесено `git mv doc/dev/new/task-54-*.md doc/dev/done/` + Completion report.
- [ ] Рядок у `INDEX.md` (нова «Хвиля Q»), статус ✅.

---

## 7. Свідомі відхилення від `frontend/AGENTS.md` (узгоджено з користувачем)

Користувач прямо вимагає таблицю «точно таку ж, 1 в 1» — світлий Google-Sheets-вигляд із кольоровими заголовками. Це конфліктує з §2 (dark control-room, одна оранжева акцентна) і §3 (без «кольорових плашок на кожному лейблі»). CLAUDE.md: явна вимога користувача **перемагає** `AGENTS.md`, окрім accessibility / безпеки / цілісності даних. Фіксуємо, як саме:

1. **Світла поверхня — ізольована, з власними токенами.** `monthly-table.css` заводить `--sheet-*` токени в одному місці й не протікає в `app.css`; блок на темній сторінці читається як **вкладений документ**, а не як збій теми. Прецедент у репо вже є — `report-doc.css` («a printed deliverable, not an application screen»). Нові кольори входять **через токени**, як вимагає §2, а не літералами по компонентах.
2. **Колір ≠ єдиний носій сенсу — це accessibility, і тут AGENTS.md §5/§16 не поступається.** Sheet передає MoM-напрямок **тільки** кольором; для дальтоніка це нуль інформації. Додаємо мінімальний носій: компактний `▲`/`▼` (0.75em, того ж кольору) **після** числа + `aria-label` («up from 706 in April 2026»). Це єдине візуальне доповнення поверх скріншота; перемикач **Sheet colors only** прибирає маркери, коли оператору потрібен піксельний знімок для клієнта. Контраст тексту на кольорових заголовках перевіряється (жовтий `#FFFF00` вимагає **чорного** тексту — на скріншоті так і є).
3. **`Total organic Users` без групування розрядів** (`1208`) — за скріншотом, не за загальним правилом. Формат стабільний у межах колонки, тож §5 не порушено.

Інші вимоги AGENTS.md (Intl, tabular-nums, `null ≠ 0`, стани, клавіатура, без інлайн-стилів, без CDN) виконуються **без винятків**.

---

## 8. Ризики

| Ризик | Ймовірність | Мітигація |
|---|---|---|
| `conversions` + `keyEvents` в одному запиті → детермінований `INTERNAL` | **виміряно** | Ніколи не разом (§1.4а). Юніт-тест на набір полів, щоб регресія не проїхала |
| `month`-дименсія GSC малює брехливі нулі | **виміряно** | Тільки `date`-дименсія + локальне групування (§1.4б) |
| Обрізка `eventName`-запиту лімітом → тихо занижена колонка | середня | `limit` від вікна + `truncated`-прапорець у UI, а не тихе число (§2.3) |
| GA4 `(other)` при високій кардинальності подій (SHB — 21 подія, 3XE — 16) | середня | Детекція `(other)` → прапорець стану колонки |
| Авто-вибір «топова подія» дає сміття (LAXTON — cookie-consent) | **виміряно** | Дискаверинг лише **пропонує**; вибір підтверджує оператор (§1.5) |
| Зміна атрибуції безшумно переінтерпретує заморожені місяці | висока без мітигації | Провенанс у рядку + `409 attribution_changed` зі списком місяців (§2.2) |
| Заморозили місяць, у якого фетч насправді впав | середня | Морозимо лише при `ok`/`no_data`/`not_configured`; `failed` — ніколи (§2.2) |
| Синхронний `/refresh` упирається в таймаут на повільному RN | низька (2–4 с виміряно) | Hard timeout 90 с + честний `503 provider_unavailable`; переїзд у worker — follow-up (§9) |
| Ліміт RN 300/хв при кількох одночасних Refresh | низька | Спільний process-wide token bucket (240 rpm) уже в `ninja_api.py` |
| Колізія з паралельною сесією у 3 спільних фронт-файлах | **підтверджено** | Порядок §4.2 → §4.3; до спільних файлів лише на чистому `git status` |

---

## 9. Follow-ups (свідомо поза обсягом, за рішенням користувача §1.3)

> **Оновлення:** XLSX-експорт і структура «картка на сторінці клієнта + повна таблиця на під-сторінці»
> були доведені під час цієї ж сесії за прямим запитом користувача (не в первісному обсязі §1.3) —
> див. Completion report нижче, §11.

- `TODO(task-55)` **Copy table (TSV)** у буфер — прецедент `copyHistoryTable` у `reports-run.js`.
- `TODO(task-56)` **Пакетний Refresh на портфель** (14 клієнтів × 4 запити = 56, вкладається в лімiт) — прецедент `report_batches`.
- `TODO(task-56)` Перенесення `/refresh` у worker, якщо живий портфель покаже > 10 с на клієнта.
- `TODO(task-57)` Поточний **відкритий** місяць окремим рядком із явним бейджем `in progress` (скріншот його не має; напівмісяць у тому ж стовпці був би фальшивим порівнянням).
- **Одноразовий імпорт історії до вікна GSC** — користувач відхилив (§1.3). Якщо повернеться: рядки Dec 2024 – Mar 2025 зі Sheet можна залити як `is_frozen=True, source="import"`; схема §2.2 це вже витримує без міграції.

---

## 10. Живі проби, на яких стоїть §1

Скрипти дослідження лежать у `.verify_tmp/` (gitignored) і запускаються в контейнері —
`docker compose exec -T api poetry run python .verify_tmp/<script>.py`. Виконавцю таски їх варто
перезапустити перед кодуванням: числа GA4 доуточнюються, а вікно GSC зсувається щомісяця.

| Скрипт | Що показує |
|---|---|
| `probe_fields.py` | Прив'язки клієнта + повний список полів `ga4` (≈380) і `google_search_console` (12) |
| `probe_grp2.py` | Які набори полів GA4 проходять, а які дають `INTERNAL` (§1.4а) |
| `probe_match.py` | Organic users/conversions за один місяць по всіх 14 клієнтах — так знайшли SDS |
| `probe_sds.py` | Місячні organic users/conversions + GSC `month`-дименсія (брехливі нулі, §1.4б) |
| `probe_sds_events.py` | `eventName × keyEvents` і `× eventCount` за один місяць |
| `probe_sds_attr.py` | session vs first-user на одному місяці — тут `Book Now = 70` зійшлося точно |
| `probe_sds_months.py` | Зведена таблиця §1.2: sheet vs firstUser vs session за 20 місяців |
| `probe_sds_chan.py` | Розбивка по каналах (звідки `111 = 109 Organic Search + 2 Organic Social`) |
| `probe_sds_conv.py` | `conversions` vs `keyEvents` vs `*ConversionRate` — доводить, що це аліаси однієї метрики |
| `probe_db_hist.py` | Що з місячної історії вже лежить у нашій БД (`metric_values`, `analytics_dataset_rows`) |

---

## ✅ Completion report

- **Дата:** 2026-08-21
- **Гілка / комміт:** `task-34-client-lifecycle-status` (некомічено на момент запису)
- **Статус:** done

### Що зроблено

Місячна таблиця клієнта (7 метрик × місяць, «1 в 1» до операторського Google Sheet) працює
end-to-end на реальному бекенді проти живого Reporting Ninja API, перевірена на всіх 14 клієнтах
портфеля. Під час сесії користувач розширив початковий обсяг (§1.3 планував лише інлайн-блок +
Refresh) трьома прямими запитами, усі реалізовані:

1. **Компактна картка** (4 KPI-плитки + MoM-стрілки) одразу під шапкою сторінки клієнта, **вище**
   період-панелі — не інлайнова таблиця.
2. **Кнопка «Open Monthly Performance»**, що веде на нову під-сторінку `#/client/<id>/monthly`
   (той самий патерн, що Ahrefs/Blogs — `router.js`, `monthly-performance-page.js`).
3. **Повна sheet-fidelity таблиця з Refresh/Configure/«Sheet colors only»** — на під-сторінці, не на
   сторінці клієнта.
4. **Export to Excel** — кнопка на під-сторінці, XLSX **точ в точ** як на фото (ті самі кольори
   заголовків, той самий порядок колонок, MoM кольоровим текстом без стрілки-гліфа — живо
   звірено байт-в-байт з тим, що бачить браузер).

### Як зроблено (ключові рішення)

- **Накопичувальний реєстр місяців** (`client_monthly_metrics`), не `ReportRun` — закритий,
  усталений (≥3 дні), успішно зібраний місяць **морозиться** і ніколи не перезапитується. Живо
  перевірено: 24/24 місяці заморожені після першого збору, повторний `/refresh` без `force` —
  **0 запитів**, `force=all` перезбирає все.
- **Атрибуція — налаштування на клієнта** з провенансом у кожному рядку (не тільки в
  налаштуваннях) — інакше зміна атрибуції тихо переінтерпретує вже заморожені рядки, як це
  сталося з операторським Sheet (§1.2). Дефолт (`users=first_user`, `conversions+event=session`)
  живо відтворив найсвіжіший рядок скріншота точно.
- **Названа подія («Book Now») — набір імен, не одна подія.** Живо підтверджено на PJC (4
  локації), і живо відхилено «топова подія» як авто-вибір — у LAXTON топ-4 events це
  `cookie_consent_*`, банерний шум, не конверсія.
- **Два живо виміряних капкани Reporting Ninja** задокументовані і закриті кодом:
  `conversions`+`keyEvents` в одному запиті → детермінований `INTERNAL`; GSC-дименсія `month`
  малює брехливі нулі для місяців поза 16-місячним вікном (тільки `date`-дименсія чесна).
- **Реальний ліміт запиту занижував PJC/SHB** (`_event_limit` рахував лише органічний канал, а RN
  без фільтра каналу повертає всі канали × події — 74 рядки/місяць на SHB) — знайдено й виправлено
  живо під час верифікації, не в дослідженні.
- **Реальний баг горизонтального скролу сторінки** — `overflow-x:auto`-обгортка з широкою
  `<table>` пропускала скрол на весь `<html>` у Chromium (справжній wheel-жест зносив сторінку,
  хоча власний скрол таблиці працював коректно) — `contain: paint` на `.mt-scroll` фіксить без
  побічних ефектів, підтверджено CDP-тестом і на 390px, і на справжньому 200% zoom.
- **Головні файли:**
  `app/reports/monthly_table.py` — чиста бізнес-логіка збору/деривації, без БД;
  `app/reports/monthly_xlsx.py` — XLSX-білдер (openpyxl), той самий формат, що на екрані;
  `app/db/repositories/client_monthly.py` — правило заморозки, `months_needing_collection`;
  `app/api/client_monthly.py` — 5 ендпоїнтів (`GET`/`POST /refresh`/`GET /key-events`/
  `PUT /settings`/`GET /xlsx`), self-registered router (жодної правки `app/main.py`);
  `frontend/js/monthly-table.js` — чистий render-модуль (картка + повна таблиця), перевикористаний
  і на клієнтській сторінці (частково), і на під-сторінці (повністю);
  `frontend/js/monthly-performance-page.js` — нова під-сторінка (шаблон `ahrefs-page.js`);
  `frontend/css/monthly-table.css` — ізольована світла sheet-fidelity поверхня (`contain: paint`
  фікс усередині).
- **Відхилення від початкового плану:** обсяг §1.3 («блок + Refresh») розширено користувачем
  мідсесії до картка+під-сторінка+Excel-експорт — задокументовано в §9 вище.

### Контрактні зміни (Contract changes)

Адитивні, нових read-only споживачів. Нові ендпоїнти під власним префіксом
`/api/v1/clients/{client_id}/monthly-table` (5 шляхів, включно з `/xlsx`), 2 нові моделі БД
(`ClientMonthlyMetric`, `ClientMonthlyTableSettings`), міграція `40d3a23f853d`. `app/db/models.py`
і `app/core/config.py` — точкові адитивні дотики (2 нові моделі, 2 нові налаштування). Ніщо наявне
не перейменовано й не змінило форму.

### Як перевірити (verification)

```bash
docker compose exec api poetry run pytest tests/unit/reports/test_monthly_table.py tests/unit/reports/test_monthly_xlsx.py tests/unit/api/test_client_monthly_api.py -q
docker compose exec api poetry run ruff check app/ && docker compose exec api poetry run mypy app/
```

У браузері: `#/client/<id>` → картка «Monthly Performance» одразу під шапкою → «Open Monthly
Performance» → повна таблиця, «Export to Excel», «Configure» (атрибуція + набір подій), «Refresh».

### Тести

- `tests/unit/reports/test_monthly_table.py` (26) — атрибуція, точний матч каналу, два RN-капкани,
  правило заморозки, стани провайдера, набір подій.
- `tests/unit/reports/test_monthly_xlsx.py` (8) — кольори заголовків, порядок колонок, формати
  чисел, MoM-колір без гліфа, `None` → em dash.
- `tests/unit/api/test_client_monthly_api.py` (17) — усі 5 ендпоїнтів, `409 attribution_changed`
  (не блокує), `409 not_configured`, xlsx 404/200.
- `tests/e2e/frontend/client-monthly-table.cjs` — картка вгорі сторінки, перехід на під-сторінку,
  порядок колонок, бейджі `partial`/`may be incomplete`, toggle кольорів, Export-посилання,
  Configure + live discover, Refresh busy-стан, 0px page overflow на 390/768/1024/1440 (і живо,
  окремим CDP-скриптом, на 200% zoom).
- Живий гейт: усі 14 клієнтів портфеля рефрешнуті через реальний REST API без падінь; числа SDS
  звірені з дослідженням §1 точно.

### Follow-ups / борг

- `TODO(task-55)` Copy table (TSV) у буфер.
- `TODO(task-56)` Пакетний Refresh на портфель + перенесення `/refresh` у worker, якщо портфель
  покаже >10с/клієнта.
- `TODO(task-57)` Поточний відкритий місяць окремим рядком (`in progress`).
- Одноразовий імпорт історії до вікна GSC — відхилено користувачем (§1.3).
