# task-83 — Прямий Google API (GSC + GA4) як транспорт для Portfolio Trends

- **Хвиля:** пост-44 (джерела даних). Незалежна від хвиль архіву (63/70/80) і БД (66/67).
- **Depends on:** **09** ✅, **28** ✅, **30** ✅, **44** ✅. Не залежить від 75 (база вже бойово на Postgres).
- **Owned paths:**
  `app/integrations/google_api/__init__.py`, `app/integrations/google_api/auth.py`,
  `app/integrations/google_api/gsc_client.py`, `app/integrations/google_api/ga4_client.py`,
  `app/integrations/google_api/errors.py`,
  `app/integrations/gsc.py` (транспортна розвилка, §4),
  `app/integrations/reporting_ninja.py` (транспортна розвилка, §5),
  `app/core/config.py` (**адитивно**, 4 поля — §3.1),
  `app/cli.py` (дві нові команди — §7, §8),
  `scripts/google_oauth_bootstrap.py` (вже написаний, некомічений),
  `tests/unit/integrations/google_api/` (новий каталог)
- **Міграція:** **не потрібна.** Ні нового `Provider`, ні нової колонки, ні зміни схеми. Чому саме так — §2.
- **Фронт:** **не змінюється жодним файлом.** Причина — §2.3.

---

## §0. Що є сьогодні (виміряно в коді та в бойовій БД, не гіпотеза)

**§0.1 Сторінка `#/trends` не має власного джерела даних — вона читає три датасети.**
[`METRICS`](frontend/js/trends-page.js#L42-L50) дає сім серій, які [`SeriesSpec`](app/metrics/portfolio.py#L37-L60) розкладає рівно на три `dataset_key`:

| Серія на кнопці | `dataset_key` | `provider` рядка | Пише сьогодні |
|---|---|---|---|
| Clicks, Impressions | `gsc_daily` | `gsc` | [gsc.py#L333](app/integrations/gsc.py#L333) |
| Sessions, Users, Total Conversions | `ga4_daily` | `reporting_ninja` | [reporting_ninja.py#L55](app/integrations/reporting_ninja.py#L55) |
| Organic / Direct Conversions | `ga4_daily_channels` | `reporting_ninja` | [reporting_ninja.py#L522](app/integrations/reporting_ninja.py#L522) |

Отже «зміна провайдера для `#/trends`» — це рівно **зміна того, хто наповнює ці три датасети**. Ні бакетизація, ні `provisional`-логіка, ні API, ні фронт до цього стосунку не мають.

**§0.2 Обидва датасети сьогодні йдуть через Reporting Ninja — включно з GSC.**
[gsc.py](app/integrations/gsc.py#L1-L8) у власному докстрінгу: `Uses Reporting Ninja exclusively for aggregate metrics, dimensions and the daily series`. Тобто «GSC-провайдер» у цьому проєкті — це назва **доріжки**, а транспорт у неї RN. Це не архітектурний борг, а свідоме рішення task-09 («a live card can never mix transports»), і воно ж дає нам готове місце для розрізу.

**§0.3 Мапінг на Google API вже існує в БД — його не треба будувати.**
`client_integrations.config_json.account_id` уже зберігається **рівно у форматі, який приймає Google API**:

```
gsc               account_id='sc-domain:smashhouseburgers.com'   → GSC siteUrl, як є
reporting_ninja   account_id='properties/402129482'              → GA4 property, як є
```

Це перевірено по всіх 15 клієнтах: 14 мають `reporting_ninja.account_id` формату `properties/<id>`, 13 мають `gsc.account_id` формату `sc-domain:` або URL-prefix. Усі до єдиного збігаються з інвентарем, який віддав живий Google API (`accountSummaries.list` / `sites.list`). **Наслідок: нових полів конфігурації для ідентифікації property не потрібно взагалі**, і «три неоднозначні дублікати» (19CLV, SWM, 3XE), які видно в GA4-інвентарі, вже давно вирішені — БД знає правильний.

**§0.4 Доступ уже перевірений наскрізь на живих даних.**
`GOOGLE_API_REFRESH_TOKEN` у `.env`, OAuth-клієнт **Internal** (тобто токен не має 7-денного строку, який Google накидає режиму `Testing`). Живий прогін 2026-09-07:

```
refresh -> access token OK (expires_in=3599)
GSC  sc-domain:smashhouseburgers.com  2026-08-01  clicks=231  impr=8723
GA4  properties/402129482  серпень: sessions=22298  users=16806
```

**§0.5 `daily_series` перемагає за `created_at`, а не за джерелом — і це робить cutover безшовним.**
[`daily_series`](app/db/repositories/analytics_datasets.py#L198-L226) сортує `created_at.desc()` і бере `setdefault` на день:

```python
.order_by(AnalyticsDatasetRow.created_at.desc())
...
out.setdefault(row.client_id, {}).setdefault(day, row)
```

Тобто **новіший рядок за той самий день автоматично витісняє старіший у відображенні, нічого не видаляючи**. Це критично: перехід на новий транспорт не потребує ні міграції даних, ні чистки, а відкат — це просто ще один збір старим транспортом. Історія обох джерел лишається в таблиці назавжди.

**§0.6 Але той самий запит фільтрує по `provider` — і це стеля для «нового провайдера».**
У тому ж запиті `AnalyticsDatasetRow.provider == provider`, де `provider` береться з [`SeriesSpec.provider`](app/metrics/portfolio.py#L51). Якби новий транспорт писав під новим значенням `Provider`, сторінка перестала б бачити дані, доки не змінити `SeriesSpec`, `CATALOG`, health-ваги і `METRIC_PROVIDERS`. §2 пояснює, чому ми цього не робимо.

**§0.7 Історія в БД місцями довша за те, що GSC API взагалі вміє віддати.**
Виміряно по `analytics_dataset_rows`:

| Клієнт | `gsc_daily` днів | Найстаріший доступний через API |
|---|---|---|
| SAPHNY | **514** | ~480 (16 місяців) |
| SWM | 217 | у межах API |
| решта | 182–249 | у межах API |

GSC Search Analytics API має жорстку стелю **16 місяців** і не віддасть нічого старішого. У SAPHNY 514 днів — тобто ~34 дні історії, яких прямий API просто не існує. `ga4_daily` ще гірший на вигляд (SAPHNY 949 днів, SWM 614), але тут проблеми немає: GA4 Data API на `date` + `sessions/users/keyEvents` не обмежений user-retention і віддає всю історію property.

**Наслідок для плану: старі рядки НЕ чіпаємо і НЕ перезбираємо.** Прямий транспорт працює тільки вперед і на вікні, яке API реально покриває. §8.3 фіксує це як інваріант, бо саме тут найлегше стерти історію клієнта.

**§0.8 Одна інтеграція вже мовчки протухла — і це аргумент, а не деталь.**
PJC: `gsc_daily` востаннє 2026-08-01, `ga4_daily` — 2026-08-02, тоді як решта портфеля має 2026-09-05/06. Тобто на `#/trends` лінія PJC уже місяць обривається. Ця таска не зобов'язана лагодити саме PJC, але прямий транспорт прибирає цілий клас таких відмов: у ньому немає ні браузерної сесії RN-кабінету, ні кабінетного локу ([`RN cabinet concurrency`](doc/dev/done/)), ні чужого кешу.

**§0.9 Два клієнти мають у Search Console лише URL-prefix property.**
EFFYS (`https://effyscafe.com/`) і GRP (`https://grillpointnyc.com/`) — не `sc-domain:`. Прямий API це проковтне без змін (той самий ендпоінт), але URL-prefix за визначенням не бачить інші піддомени/протоколи. Це **наявна властивість конфігурації**, не регресія цієї таски; фіксуємо в §12 як follow-up, а не «полагодимо мимохідь».

**§0.10 GOREPORT не має GSC-інтеграції взагалі.** Ні рядка `client_integrations` з `provider='gsc'`, ні сайту в Search Console під цим акаунтом. На `#/trends` він просто не має GSC-ліній. Прямий транспорт цього не змінює.

---

## §1. Мета і межа обсягу

**Мета:** три датасети, що живлять `#/trends`, наповнюються напряму з безкоштовних Google API, з можливістю перемкнутися назад на Reporting Ninja **на рівні окремого клієнта** і без жодної зміни у фронті, схемі БД чи публічному API.

**У обсязі:**
1. Транспортний шар до GSC Search Analytics API і GA4 Data API (§3).
2. Розвилка транспорту в `gsc.py` для `gsc_daily` (§4).
3. Розвилка транспорту в `reporting_ninja.py` для `ga4_daily` і `ga4_daily_channels` (§5).
4. Перемикач конфігурації (§6).
5. **Команда звірки** `compare-transports`, яка друкує розбіжність по днях, нічого не пишучи в БД (§7). Це gate, а не зручність.
6. Живий cutover з відкатом (§8).

**Поза обсягом (свідомо):**
- Решта датасетів RN (`ga4_channels`, `ga4_landing_pages`, `ga4_events`, `ga4_devices`, `ga4_countries`, GBP) — вони не живлять `#/trends`. Див. §12.
- `MetricValue`-и і картка клієнта — інший читач, інший ризик, інша таска.
- Перейменування `rn_*` метрик — §2.4.
- Backfill історії — §0.7/§8.3.

---

## §2. Головне рішення: транспорт ≠ провайдер

**§2.1 `Provider` лишається без змін.** Ніякого `Provider.google_api`. `Provider` у цій системі означає **доріжку збору** (PLAN §9.1: «між сервісами паралельно, усередині сервісу послідовно»), а не HTTP-клієнта. Він зашитий у `METRIC_PROVIDERS`, health-ваги, `CATALOG`, ключі метрик, фільтри фронта, збережені стани сторінок і всю історію `analytics_dataset_rows`. Нове значення enum коштувало б міграції + правок у семи місцях і не купило б нічого, що не дає §2.2.

**§2.2 Транспорт живе в `config_json` і в `provenance`.** Обидві структури для цього вже призначені: `config_json` описано в [gsc.py#L14-L20](app/integrations/gsc.py#L14-L20) як «all non-secret external identifiers», а `provenance` уже сьогодні несе `"transport": "reporting_ninja"` ([gsc.py#L373](app/integrations/gsc.py#L373)). Додаємо один необов'язковий ключ:

```jsonc
{
  "connection_key": "...",           // лишається — потрібен для fallback
  "account_id": "sc-domain:...",     // без змін, уже правильний формат
  "transport": "google_api"          // НОВЕ, необов'язкове; дефолт — "reporting_ninja"
}
```

Відсутній ключ = стара поведінка. Тобто **до першої свідомої зміни конфігу нічого не змінюється ні для кого**, і це не гілка «на майбутнє», а робочий перемикач відкату (§8.4).

**§2.3 Чому фронт не змінюється взагалі.** Рядки пишуться з тими самими `provider`, `dataset_key`, `dimension_key='date'`, `dimension_value='YYYY-MM-DD'` і тими самими іменами метрик у `metrics_json`. `daily_series` бере найновіший рядок за день (§0.5). Отже для [`portfolio.py`](app/metrics/portfolio.py) і для `trends-page.js` нічого не відбулося. **Це і є критерій правильності розрізу: якщо якийсь файл у `frontend/` довелося правити — розріз зроблено не там.**

**§2.4 Ім'я `rn_*` лишається — і це усвідомлена вада.** Після цієї таски `rn_sessions` може приходити не з Reporting Ninja, тобто префікс бреше. Альтернатива — перейменувати сім ключів метрик — ламає збережені стани сторінки, історію і CLAUDE.md-інваріант «провайдер/метрика називаються однаково скрізь: catalog → API → фронт → фільтри → помилки», бо перейменування треба робити атомарно в усіх п'яти місцях. Це окрема таска з власним gate (§12), а не побічний ефект зміни транспорту. **Підсвічую конфлікт явно, як вимагає CLAUDE.md, а не ховаю його.**

---

## §3. Транспортний шар `app/integrations/google_api/`

**§3.1 Автентифікація — `auth.py`.** Один refresh token на весь портфель (усі property на одному агенційному акаунті). Нові поля `Settings` (адитивно, §Owned paths):

```python
google_api_client_id: str | None = None
google_api_secret: str | None = None
google_api_refresh_token: str | None = None
google_api_timeout_seconds: float = 60.0
```

Правила, які тут не обговорюються:
- Токени **ніколи** не потрапляють у `warnings`, `provenance`, `ProgressEvent`, логи чи повідомлення помилок, віддані UI (CLAUDE.md, PLAN §7). Тіло помилки Google несе лише код (`invalid_grant`, `PERMISSION_DENIED`) — його пропускаємо через явний allowlist полів, а не через `str(exc)`.
- Access token кешується в пам'яті процесу з запасом 60 с до `expires_in` і оновлюється ліниво. Спільний кеш на процес, під `threading.Lock` — інакше вісім паралельних доріжок роблять вісім однакових refresh-запитів.
- **Немає** запису токенів у БД. `.env` — єдине місце.

**§3.2 `gsc_client.py`.** `POST /webmasters/v3/sites/{siteUrl}/searchAnalytics/query`, `siteUrl` — URL-encoded. Пагінація через `startRow` + `rowLimit=25000` (стеля API), цикл до порожньої сторінки. Для `dimensions=["date"]` на вікні 400 днів це один запит, але цикл пишемо одразу — інакше він з'явиться у вигляді тихого обрізання, коли додамо другу дименсію.

**§3.3 `ga4_client.py`.** `POST /v1beta/properties/{id}:runReport`. Пагінація через `offset`/`limit`, стеля `rowCount`. **Дві пастки, обидві вже коштували цьому проєкту часу** (записані в пам'яті сесій):
- `conversions` і `keyEvents` в одному запиті дають `INTERNAL`. Беремо **тільки** `keyEvents` — це те, що `ga4_daily` називає `conversions`.
- Дименсія `month` малює брехливі нулі. Використовуємо **тільки** `date`; групування в тижні/місяці робить [`portfolio.py`](app/metrics/portfolio.py), і має продовжувати робити саме воно.

**§3.4 `errors.py` і поведінка на збої.** Мапимо HTTP на існуючі класи ([`APIError`, `RateLimitError`](app/integrations/gsc.py#L318)), щоб виклична сторона не переписувалась:
- `401` після одного повторного refresh → `APIError` («re-auth required»).
- `403` — розрізняємо `PERMISSION_DENIED` (немає доступу до property) і `RATE_LIMIT_EXCEEDED`. Перше — це **`not_configured`, а не `failed`**: клієнт без доступу не повинен фарбувати доріжку червоним (PLAN §9.2).
- `429` / `RESOURCE_EXHAUSTED` → `RateLimitError`, exponential backoff з jitter, 3 спроби, стеля 30 с.
- `5xx` → 2 повтори, далі `APIError`.

Квоти для контексту: GSC — 1200 запитів/хв на сайт; GA4 — 25 000 токенів/property/добу. Наше навантаження (≈2 запити на клієнта на збір) від цих стель на порядки далі, тож троттлінг у цій тасці не будуємо — але backoff обов'язковий, бо `429` прилітає і від спільних лімітів проєкту.

**§3.5 Ізоляція клітинки зберігається.** Кожна пара `(client, provider)` ловить свої помилки і віддає `quality` + `warnings`, ніколи не валячи сусідів (PLAN §9.2). Це вже інваріант `collect_datasets`; новий транспорт нічого тут не послаблює.

---


**§3.6 Публічний контракт модуля — його споживає не лише ця таска.**
`app/integrations/google_api/` навмисно не знає нічого про датасети, адаптери й `#/trends`: це чистий транспорт. Контракт зафіксовано тут, щоб task-84 (звіти) кодила проти нього паралельно, ставлячи фейк, а не чекала мержу (CLAUDE.md §3). **Наведене нижче звірено з реалізацією, а не з ескізом:**

```python
from app.integrations.google_api import (
    open_google_api_session,   # (settings=None) -> GoogleApiSession, контекстний менеджер
    resolve_transport,         # (config_json, *, settings=None) -> Transport
    GOOGLE_API, REPORTING_NINJA,          # значення Transport
    GscClient, GscDailyRow,
    Ga4Client, Ga4Row,
    GoogleApiError,            # база; нижче — підкласи
    GoogleApiConfigError,      # немає креденшелів  -> not_configured
    GoogleApiAuthError,        # 401 після повтору  -> re-auth required
    GoogleApiPermissionError,  # 403 PERMISSION_DENIED -> not_configured, НЕ failed
    GoogleApiRateLimitError,   # 429 / RESOURCE_EXHAUSTED після backoff
)

GscClient(session).daily_search_analytics(
    site_url, start, end, *, search_type="web") -> list[GscDailyRow]
# GscDailyRow: date(str YYYY-MM-DD), clicks, impressions, ctr, position — Decimal | None

Ga4Client(session).run_report(
    property_id, start, end, *, dimensions, metrics) -> list[Ga4Row]
# Ga4Row: dimensions: dict[str,str], metrics: dict[str, Decimal | None]
# `property_id` приймається і як "properties/123", і як "123"
# дименсія `date` нормалізована з GA4-шного `YYYYMMDD` у `YYYY-MM-DD`
```

Два інваріанти, які виклична сторона **не має права послабити**:
- **День, якого API не повернув, просто відсутній у списку** — ніколи не синтезований нульовий рядок (PLAN §12).
- **`GscDailyRow.ctr` — це частка `0..1`, як віддає API.** RN повертає CTR у відсотках і `gsc.py` ділить на 100; повторити те саме тут означало б роздути кожне значення ×100, малюючи при цьому цілком правдоподібний графік (§7.3, пастка ×100).

Пагінація, backoff, кеш access-токена і мапінг помилок сховані всередині; виклична сторона їх не бачить і не дублює.

---

## §4. `gsc_daily` через прямий API

Розвилка **всередині** [`_collect_datasets`](app/integrations/gsc.py#L262), у точці, де зараз стоїть `client.query_all(...)`. Решта функції — сортування, `rank`, `quality`, `truncated`, обидві ролі періодів (`current`/`previous`) — **не чіпається**.

Відповідність полів однозначна, ніякої трансформації:

| `metrics_json` | GSC API | Примітка |
|---|---|---|
| `clicks` | `clicks` | |
| `impressions` | `impressions` | |
| `ctr` | `ctr` | API віддає частку `0..1`. Наш [`_reporting_ninja_ctr`](app/integrations/gsc.py#L340) приводить RN-формат; для API-гілки потрібна **своя** нормалізація — це найімовірніше місце тихої помилки ×100. |
| `position` | `position` | |

`settings={"search_type": "web"}` у RN-виклику відповідає `type: "web"` в API — передаємо явно, щоб вибірка збіглася.

`provenance` для нової гілки:

```python
{"transport": "google_api", "integration": "search_console",
 "datasets": ["gsc_daily"], "site_url": cfg["account_id"]}
```

`site_url` тут — не секрет, а той самий ідентифікатор, що вже лежить у `config_json`.

---

## §5. `ga4_daily` і `ga4_daily_channels` через прямий API

**§5.1 `ga4_daily`.** `dimensions=[date]`, метрики рівно за [`_GA4_DATASETS["ga4_daily"]`](app/integrations/reporting_ninja.py#L55-L64):

| `metrics_json` | GA4 API |
|---|---|
| `users` | `totalUsers` |
| `sessions` | `sessions` |
| `conversions` | `keyEvents` |
| `engagement_rate` | `engagementRate` |

**§5.2 `ga4_daily_channels`.** `dimensions=[date, firstUserDefaultChannelGroup]`, метрика `keyEvents`; півот у колонки за [`_CHANNEL_METRIC_FIELDS`](app/integrations/reporting_ninja.py#L128-L131) (`Organic Search → conversions_organic`, `Direct → conversions_direct`). Саме `firstUserDefaultChannelGroup`, не `sessionDefaultChannelGroup` — ця дименсія вже перевірена на збіг з PDF-звітом RN і зафіксована в коді task-44.

⚠️ **Кардинальність.** Розмір відповіді тут `дні × канали`, а не `дні`. На 400-денному вікні з ~10 каналами це ~4000 рядків — у межах, але **тільки з пагінацією**. Без неї високоподійний клієнт мовчки обріжеться на дефолтному ліміті, і в графіку з'явиться яма, яку ніхто не помітить як помилку. Це не теоретичний ризик: рівно цей клас дефекту вже ловили на RN-запитах з дименсією каналу.

**§5.3 Нуль ≠ відсутність.** Якщо GA4 не повернув рядка для дня — його **немає** в мапі, а не `0` (PLAN §12, [portfolio.py#L1-L9](app/metrics/portfolio.py#L1-L9)). Якщо повернув `0` — це справжній нуль і він зберігається. Це найважливіший інваріант усієї таски, бо помилка тут виглядає як реальний провал трафіку клієнта.

---

## §6. Перемикання транспорту

**§6.1 Резолюція, в порядку спадання пріоритету:**
1. `config_json["transport"]` клієнта — `"google_api"` | `"reporting_ninja"`.
2. Глобальний дефолт `Settings.google_api_default_transport` (дефолт `"reporting_ninja"`).
3. Якщо обрано `"google_api"`, але `Settings.google_api_refresh_token` порожній → **`not_configured` з явним warning**, ніколи не тихий фолбек на RN. Тихий фолбек — найгірший з варіантів: оператор бачить дані, вважає, що прямий транспорт працює, і дізнається правду в момент, коли RN відключать.

**§6.2 Немає автоматичного фолбеку між транспортами всередині одного збору.** Той самий принцип, що вже записаний у [gsc.py#L3-L6](app/integrations/gsc.py#L3-L6): «a live card can never mix transports». Змішаний збір дає рядки, чия провенанс-мітка не відповідає половині даних, і робить звірку (§7) безглуздою.

**§6.3 UI перемикача в цій тасці немає.** Перемикання — через CLI (§8) або прямим редагуванням `config_json`. Причина: це операція cutover-класу, яку роблять раз, а не щоденний контроль; кнопка в UI без ролі, аудиту й підтвердження була б гіршою за її відсутність. Follow-up у §12.

---

## §7. Звірка транспортів — це gate, а не зручність

**§7.1 Нова CLI-команда, яка нічого не пише в БД:**

```bash
docker compose run --rm api poetry run yovth-report compare-transports \
    --client SHB --days 30 --dataset gsc_daily
```

Тягне те саме вікно обома транспортами **в пам'ять** і друкує потоденну таблицю: значення A, значення B, абсолютна і відносна різниця, плюс підсумок (днів звірено, днів тільки в A, тільки в B, max/median відхилення).

**§7.2 Чому обов'язково без запису.** Через §0.5 будь-який запис новим транспортом миттєво стає тим, що бачить сторінка. Звірка, яка пише, — це вже cutover, тільки непоміченим.

**§7.3 Критерій проходження (Gate G2):**

| Метрика | Допуск | Чому саме так |
|---|---|---|
| `gsc_clicks`, `gsc_impressions` | **0%** — байт-у-байт | RN проксює той самий API; будь-яка різниця означає різну вибірку (`search_type`, тип property, часовий пояс), і її треба зрозуміти, а не «списати на округлення». |
| `ctr`, `position` | ≤ 0.5% відносно | Похідні, чутливі до округлення на боці RN. |
| `sessions`, `users` | ≤ 1% | GA4 має власне доуточнення за 48 год; на закритих днях різниця має зникати. |
| `keyEvents` (усі три конверсійні) | **0%** на днях, старших за лаг | Цілі числа. Ненульова різниця = різна дименсія атрибуції, а не шум. |

**§7.4 Дні всередині лагу зі звірки виключаються** (`GSC_LAG_DAYS = 3`, `GA4_LAG_DAYS = 2`, [portfolio.py#L28-L31](app/metrics/portfolio.py#L28-L31)) — там обидва джерела легально розходяться, бо дані ще доуточнюються.

**§7.5 Звіряємо мінімум 4 клієнти**, підібрані за різними формами конфігурації, а не «перші-ліпші»: SHB (`sc-domain`, багато подій), EFFYS або GRP (**URL-prefix** property, §0.9), SAPHNY (найдовша історія, §0.7), 3XE (кілька GA4-property на акаунті, тобто найвищий ризик, що `account_id` вказує не туди).

---

## §8. Живий cutover і відкат

**§8.1 Порядок — по одному клієнту, починаючи з найдешевшого в разі помилки.** Спершу GOREPORT (внутрішній) або DEV ALPHA, потім один бойовий, потім решта. Не «усі 15 однією командою».

**§8.2 Кроки на клієнта:**
1. `compare-transports` для всіх трьох датасетів, gate §7.3 зелений.
2. Перемкнути транспорт (§8.4).
3. Зібрати вікно 30 днів через звичайний шлях (`getJobProgress().openMetricsRefresh` з `#/trends` або CLI).
4. Відкрити `#/trends`, порівняти форму лінії до/після. Злам на стику = стоп і відкат.

**§8.3 Історію не перезбираємо. Ніколи.**
Прямий транспорт працює тільки на вікні, яке API реально покриває. Для SAPHNY це означає, що ~34 дні найстарішої історії існують **тільки** в наших рядках — Google їх уже не віддасть (§0.7). Оскільки старі рядки нікуди не діваються (§0.5), сторінка продовжує їх показувати. **Заборонено:** будь-яка операція, що видаляє або перезаписує рядки, старші за 16 місяців. Ця заборона — не стилістика: одна невдала «чистка перед перезбором» стирає єдину копію.

**§8.4 Відкат — одна зміна конфігу, без коду і без даних:**

```bash
docker compose exec api poetry run yovth-report set-transport --client SHB --transport reporting_ninja
```

Наступний збір знову пише RN-рядки, вони новіші за `created_at`, і §0.5 повертає сторінку до старого джерела автоматично. Нічого видаляти не треба.

**§8.5 Перед першим бойовим cutover — бекап.** `docker compose exec api poetry run python scripts/backup_sqlite.py ...` (або `cloud-backup`). Формально ця таска не робить міграцій, але правило CLAUDE.md стосується «будь-якої дії, що торкається бойової БД», а масовий збір нових рядків — саме вона.

---

## §9. Контрактні зміни

**Публічний API, схема БД, фронт-контракт — без змін.** Формально контрактна зміна одна: **необов'язковий ключ `transport` у `client_integrations.config_json`** для `gsc` і `reporting_ninja`. Споживач — лише самі адаптери. Відсутність ключа = сьогоднішня поведінка, тож жоден наявний рядок не стає невалідним.

Оскільки `config_json` описаний у task-02 як спільний контракт — після мержу синхронно оновити `doc/dev/done/task-02-shared-contracts.md` і згадати в Completion report (CLAUDE.md §5).

---

## §10. Тести

**Юніти (`tests/unit/integrations/google_api/`), усе на фейкових HTTP-відповідях, без мережі:**
1. `auth`: кеш access-токена; повторний refresh на `401`; **токен не з'являється в жодному `warning`/`provenance`/тексті помилки** (перевіряти рядковим пошуком по всьому результату, а не по одному полю).
2. `gsc_client`: пагінація по `startRow`; `ctr` як частка `0..1` → наш формат (регресійний тест на ×100); `403 PERMISSION_DENIED` → `not_configured`, не `failed`.
3. `ga4_client`: пагінація по `offset`; півот `[date, channel] → conversions_organic/conversions_direct`; **відсутній день лишається відсутнім, а не `0`**; `keyEvents` без `conversions` у запиті.
4. `429` → backoff → успіх з третьої спроби.
5. Резолюція транспорту: явний `config_json` > глобальний дефолт; `"google_api"` без токена → `not_configured` з warning, **не** мовчазний фолбек.
6. Форма рядка: `AnalyticsDatasetValueResult`, зібраний обома транспортами з однакового входу, збігається у всіх полях, крім `provenance.transport`. Це головний тест таски — саме він охороняє §2.3.

**Живі перевірки (не в CI):** `compare-transports` на 4 клієнтах §7.5.

Прогін — тільки в Docker: `docker compose exec api poetry run pytest tests/unit/integrations -q`, `ruff check .`, `mypy app`.

---

## §11. Definition of Done / Gate

- **G1.** `docker compose exec api poetry run pytest tests/unit/integrations -q` зелений; `ruff` і `mypy app` чисті.
- **G2.** `compare-transports` пройдено на 4 клієнтах §7.5 для всіх трьох датасетів, у межах допусків §7.3. Вивід — у Completion report. **Не пройдено = таска не готова**, навіть якщо код працює.
- **G3.** `git diff --stat` не містить жодного файлу з `frontend/` (§2.3).
- **G4.** Міграцій не додано; `Provider` не змінено; `alembic heads` — один.
- **G5.** Мінімум один клієнт живе на `google_api` ≥ 48 год, і на `#/trends` лінія не має зламу на стику транспортів.
- **G6.** Відкат §8.4 перевірений на практиці, не лише описаний.
- **G7.** Жодного нового рядка в БД зі старішою за 16 місяців датою не створено і жодного старого не видалено (`SELECT count(*) ... WHERE dimension_value < :cutoff` до і після — однакове).

---

## §12. Follow-ups / борг

- **`TODO(task-84)`** — решта GA4-датасетів (`ga4_channels`, `ga4_landing_pages`, `ga4_events`, `ga4_devices`, `ga4_countries`) на прямий API. Тільки після того, як `#/trends` відпрацює на ньому щонайменше тиждень.
- **`TODO(task-85)`** — перейменування `rn_*` → нейтральних ключів метрик (§2.4), атомарно в catalog + API + фронт + фільтри + збережені стани.
- **`TODO(task-86)`** — перемикач транспорту в UI з ролями й аудитом (§6.3).
- **EFFYS / GRP на URL-prefix property** (§0.9) — перевести на `sc-domain:` у Search Console; це операційна дія в кабінеті Google, не код.
- **PJC протух з 2026-08-01** (§0.8) — перевірити після cutover, чи прямий транспорт його оживив; якщо ні — окрема діагностика.
- **GOREPORT без GSC** (§0.10) — додати сайт у Search Console або свідомо лишити без GSC-ліній.
- **Reporting Ninja лишається** для GA4-атрибуцій і готових кабінетів. Ця таска не про відключення RN, а про те, щоб `#/trends` перестала від нього залежати.

---

## §13. Ризики

| Ризик | Чому реальний | Чим закритий |
|---|---|---|
| Тихе розходження цифр після cutover | Різна вибірка (`search_type`, тип property, TZ) виглядає як зміна трафіку, а не як баг | G2 з нульовим допуском на цілочисельні метрики (§7.3) |
| Обрізання `ga4_daily_channels` | `дні × канали` мовчки впирається в ліміт рядків; цей клас дефекту вже ловили на RN | Пагінація + тест §10.3 |
| `ctr` ×100 | RN і API віддають різні одиниці, помилка не падає, а малює правдоподібний графік | Окрема нормалізація + регресійний тест §10.2 |
| Втрата історії > 16 місяців | Її неможливо відновити з Google | §8.3 + G7 |
| Відкликаний refresh token кладе всі GA4/GSC доріжки одночасно | Один токен на весь портфель | `403/401` → `not_configured` з явним warning (§3.4), відкат §8.4 одним конфігом |
| `.env` як єдине сховище токена | Втрата `.env` = повторний OAuth | `scripts/google_oauth_bootstrap.py` відтворює його за 2 хвилини |

---

## ✅ Completion report

- **Дата:** 2026-09-07
- **Гілка / комміт:** `feat/task-83-google-api-direct-transport` @ (див. коміт цієї таски; відгалужено від `bbffba6`)
- **Статус:** done-with-followups (код + юніти + gate G2 закриті; G5 «≥48 год живо» — часова вимога, G6 живий відкат і G7 «до/після» на бойовій БД лишились операторові — див. «Follow-ups», команди готові)

### Що зроблено

Три датасети, що живлять `#/trends` (`gsc_daily`, `ga4_daily`, `ga4_daily_channels`), тепер можна наповнювати **напряму з безкоштовних Google API** замість платного Reporting Ninja — перемикачем на рівні окремого клієнта, без жодної зміни у фронті, схемі БД чи публічному API.

- **`app/integrations/google_api/`** — новий транспортний шар:
  - `auth.py` — `GoogleAuth` (один refresh token на портфель, процес-глобальний кеш access-токена під `threading.Lock`, лінивий refresh із запасом 60 с) + `GoogleApiSession` (httpx-клієнт із політикою §3.4: `401`→один forced-refresh+retry, `429`/`RESOURCE_EXHAUSTED`→jittered backoff ≤30 с ×3, `5xx`→2 повтори, `403 PERMISSION_DENIED`→`GoogleApiPermissionError`). Секрети НІКОЛИ не в тексті помилки: повідомлення будуються з allowlist-полів (`error`, `error.status`), не зі `str(exc)`.
  - `gsc_client.py` — `GscClient.daily_search_analytics()`: `POST /webmasters/v3/sites/{siteUrl}/searchAnalytics/query`, `dimensions=["date"]`, `type="web"`, пагінація по `startRow`/`rowLimit=25000`. `ctr` віддається **як частка `0..1`, без ×100**.
  - `ga4_client.py` — `Ga4Client.run_report()`: `POST /v1beta/{property}:runReport`, пагінація по `offset`/`rowCount`, нормалізація `YYYYMMDD`→`YYYY-MM-DD`. Тонкий транспорт — мапінг GA4-полів на канонічні ключі робить виклична сторона.
  - `errors.py` — `GoogleApiError` та підкласи (`Config`/`Auth`/`Permission`/`RateLimit`).
  - `__init__.py` — `resolve_transport(config_json, settings)` (§6.1: `config_json["transport"]` > `Settings.google_api_default_transport` > `"reporting_ninja"`), `open_google_api_session()`.
  - `compare.py` — чиста `compare_series()` для gate G2 (без I/O).
- **`app/integrations/gsc.py`** — розвилка в `_collect_datasets`: `_collect_datasets_google_api` (нова) або `_collect_datasets_reporting_ninja` (перейменований поточний код, **без змін**).
- **`app/integrations/reporting_ninja.py`** — розвилка в `_collect_datasets`: коли транспорт `google_api`, `ga4_daily`+`ga4_daily_channels` беруться з `_collect_datasets_google_api_trends`, а решта датасетів (`ga4_channels`, landing pages, events, devices, countries, GBP) **і далі йдуть через RN у тому ж зборі** (`skip_trends_ga4=True` в RN-гілці) — рішення користувача, щоб не було регресії на сторінці клієнта / URL Inspector до task-84. `_merge_dataset_results` зшиває обидва результати; provenance стає чесно-змішаним (`transport: reporting_ninja` + `trends_ga4_transport: google_api` + вкладені `reporting_ninja`/`google_api`).
- **`app/core/config.py`** (адитивно, **5 полів**, не 4 — §Owned paths казав «4», але §6.1 явно вимагає `google_api_default_transport`): `google_api_client_id/secret/refresh_token`, `google_api_timeout_seconds=60.0`, `google_api_default_transport="reporting_ninja"`.
- **`app/cli.py`** — дві команди:
  - `compare-transports --client X --days 30 [--dataset ...] [--show-days]` — тягне обидва транспорти **в пам'ять**, друкує потоденну таблицю + підсумок (днів звірено, тільки-в-A/B, max/median відхилення), exit 1 при виході за допуск §7.3. **Нічого не пише в БД.**
  - `set-transport --client X --transport google_api|reporting_ninja|default [--only gsc|reporting_ninja] [--dry-run]` — записує/прибирає `config_json["transport"]` на GSC та/або RN інтеграції. Оборотна.
- **`scripts/google_oauth_bootstrap.py`** — закомічений (був некомічений).

### Як зроблено (ключові рішення)

- **Транспорт ≠ провайдер (§2).** `Provider` не змінено, міграції немає, жодного файлу у `frontend/`. Рядки пишуться з тими самими `provider` (`gsc`/`reporting_ninja`), `dataset_key`, `dimension_key='date'`, іменами метрик. `daily_series` бере найновіший рядок за день (`created_at.desc()` + `setdefault`), тож cutover і відкат безшовні — новий транспорт витісняє старий у показі, нічого не видаляючи.
- **Розвилка — окремі методи, не патч усередині циклу.** `gsc.py`: `_collect_datasets` став 3-рядковим диспетчером; поточне тіло — дослівно перейменоване в `_collect_datasets_reporting_ninja`. `reporting_ninja.py`: те саме + додатковий merge, бо там розвилка часткова (2 з ~11 датасетів).
- **`engagement_rate` ×100 — знайдено живим під час G2.** GA4 Data API віддає `engagementRate` часткою `0..1`, а Reporting Ninja (і тому кожен збережений `ga4_daily`-рядок) — відсотком. Правило — **паритет транспортів** (§2.3): рядок, зібраний будь-яким шляхом, байт-у-байт однаковий, крім `provenance.transport`. Тому google-гілка множить `engagement_rate` на 100. `ctr` у `gsc_daily` — дзеркальний випадок (RN ÷100 до частки; API вже віддає частку), там нічого множити не треба. Обидва під регресійними тестами.
- **Немає тихого фолбеку (§6.1.3).** `transport=google_api` без `google_api_refresh_token` → `AnalyticsDatasetCollectionResult` з `error_code="not_configured"` + warning, ніколи не перемикання назад на RN. Тест `test_google_api_without_token_is_not_configured_never_silent_fallback` явно assert-ить, що `ReportingNinjaClient` не викликається.
- **Кардинальність `ga4_daily_channels`** (§5.2, ризик «мовчазне обрізання»): `Ga4Client.run_report` пагінує до `rowCount`, тож `дні × канали` не впирається в дефолтний ліміт.
- **`_collect_datasets_reporting_ninja(skip_trends_ga4=True)`**: `active_ga4_datasets` викидає `ga4_daily`, `if not collect_channels: continue` пропускає гілку каналів (останній стейтмент у циклі — `continue` це просто ранній вихід). Лічильники `dataset_steps`/`expected` перераховані. При `skip_trends_ga4=False` (дефолт, ключа немає) — `active_ga4_datasets == _GA4_DATASETS`, поведінка ідентична; 213 інтеграційних юнітів це підтверджують.

Головні файли:

- `app/integrations/google_api/{__init__,auth,gsc_client,ga4_client,errors,compare}.py` — транспортний шар.
- `app/integrations/gsc.py:_collect_datasets_google_api` — `gsc_daily` через API.
- `app/integrations/reporting_ninja.py:_collect_datasets_google_api_trends` + `query_ga4_daily{,_channels}_via_google_api` + `_merge_dataset_results`.
- `app/cli.py:_run_compare_transports` / `_run_set_transport`.

### Відхилення від плану

- **5 полів конфігу, не 4** — §6.1 вимагає `google_api_default_transport`, якого немає в списку «4 поля». Додано.
- **`compare.py` — новий файл**, не названий у Owned paths (там перелічені конкретні файли `google_api/`). Чиста функція для gate G2, у каталозі, що його ця таска створює.
- **RN далі збирає не-trends GA4-датасети при `transport=google_api`** («mixed», рішення користувача 2026-09-07) — §6.2 «no mixing» стосується не-змішування *у межах порівнюваної trends-поверхні* та відсутності тихого фолбеку, а не цих незалежних breakdown-датасетів; task-84 «переносить решту на прямий API» — тобто зараз вони ще збираються (через RN).

### Контрактні зміни (Contract changes)

**Необов'язковий ключ `transport` у `client_integrations.config_json`** для `gsc` і `reporting_ninja` — `"google_api"` | `"reporting_ninja"`. Відсутність = сьогоднішня поведінка, тож жоден наявний рядок не стає невалідним. Споживач — лише самі адаптери (`resolve_transport`). Публічний API, схема БД, фронт-контракт — без змін. `task-02` слід синхронно доповнити приміткою про цей ключ (окрема мінімальна PR у task-02, як велить CLAUDE.md §5).

### Як перевірити (verification)

```bash
# G1 — юніти + лінт/типи (у Docker)
docker compose exec api poetry run pytest tests/unit/integrations/google_api tests/unit/integrations/test_gsc.py tests/unit/integrations/test_reporting_ninja.py -q
docker compose exec api poetry run ruff check app/integrations/google_api app/integrations/gsc.py app/integrations/reporting_ninja.py app/cli.py app/core/config.py
docker compose exec api poetry run mypy app/integrations/google_api app/integrations/gsc.py app/integrations/reporting_ninja.py app/cli.py

# G2 — живий gate на 4 клієнтах §7.5
for c in SHB EFFYS SAPHNY 3XE; do
  docker compose exec api poetry run yovth-report compare-transports --client $c --days 30
done
# очікується: кожен друкує "GATE G2: PASS"

# G5/G6 — живий cutover одного клієнта (потребує запису в бойову БД):
docker exec theyovthclientsreport-db-1 pg_dump -U yovth -d yovth -Fc -f /tmp/pre-cutover.dump   # §8.5 бекап
docker compose exec api poetry run yovth-report set-transport --client GOREPORT --transport google_api
docker compose exec api poetry run yovth-report backfill-daily-history --client-ids GOREPORT --days 30 --providers reporting_ninja
# → відкрити #/trends, звірити форму лінії GOREPORT до/після (злам на стику = стоп)
docker compose exec api poetry run yovth-report set-transport --client GOREPORT --transport reporting_ninja   # §8.4 відкат
docker compose exec api poetry run yovth-report backfill-daily-history --client-ids GOREPORT --days 30 --providers reporting_ninja
# → наступний збір пише RN-рядки, вони новіші за created_at, лінія повертається до RN автоматично

# G7 — до і після cutover має бути ОДНАКОВО:
docker exec theyovthclientsreport-db-1 psql -U yovth -d yovth -tAc \
  "SELECT dataset_key, count(*) FROM analytics_dataset_rows WHERE dimension_value < '2025-05-01' GROUP BY dataset_key"
```

**G2 — фактичний вивід (2026-09-07, вікно 30 днів):**

| Клієнт | Форма конфігу | `gsc_daily` | `ga4_daily` | `ga4_daily_channels` | Підсумок |
|---|---|---|---|---|---|
| SHB | `sc-domain`, багато подій | clicks/impressions **0** (байт-у-байт), ctr ≤1E-17, position 0 | sessions/users/conversions **0**, engagement_rate ≤9E-15 | organic/direct **0** | **PASS** |
| EFFYS | **URL-prefix** property | clicks/impressions **0**, ctr ≤2E-17 | усі **0** | **0** | **PASS** |
| SAPHNY | найдовша історія | clicks/impressions **0** | усі **0** | **0** | **PASS** |
| 3XE | кілька GA4-property на акаунті | clicks/impressions **0** | усі **0** | **0** | **PASS** |

Кожна ціла метрика (`clicks`, `impressions`, `conversions`, `conversions_organic`, `conversions_direct`) — **байт-у-байт однакова** між RN і прямим API; похідні розходяться лише на плаваючий шум (1E-14…1E-17). 0 днів «тільки в A» / «тільки в B». Reporting Ninja — прозорий проксі над тими самими Google API.

**G7 baseline (до будь-якого cutover, 2026-09-07):** `gsc_daily` рядків із `dimension_value < 2025-05-01` — **52**; `ga4_daily` — **1092**; усього рядків — 265140.

### Тести

`tests/unit/integrations/google_api/` (40 тестів, усе на `httpx.MockTransport`, без мережі):

- `test_auth.py` (14) — кеш токена; refresh при протуханні; `401`→refresh+retry, двічі→`GoogleApiAuthError`; `invalid_grant` без витоку секрета; `403 PERMISSION_DENIED`→`GoogleApiPermissionError`; `429`→backoff→успіх з 3-ї; `429` понад бюджет→`GoogleApiRateLimitError`; `5xx`→2 повтори; **секрет/токен не в тексті помилки й не в URL** (пошук по всьому результату).
- `test_gsc_client.py` (7) — сорт+мапінг; **`ctr` лишається часткою `0..1` (регресія на ×100)**; пагінація по `startRow`; `siteUrl` URL-encoded цілком; `type=web` у тілі; `PERMISSION_DENIED` пробрасується; відсутня метрика → `None`, не `0`.
- `test_ga4_client.py` (5) — нормалізація `YYYYMMDD`; пагінація по `offset`; **`keyEvents` без `conversions` у запиті**; **відсутній день лишається відсутнім, справжній `0` зберігається**; дводименсійний рядок несе обидві дименсії.
- `test_transport.py` (10) — резолюція (явний > дефолт > фолбек); **`google_api` без токена → `not_configured` + warning, `ReportingNinjaClient` не викликається**; **паритет форми рядка GSC** (RN ctr% vs API ctr-частка → однакове збережене значення); **паритет GA4 daily+channels** (RN engagementRate% vs API-частка×100 → однакове); півот каналів (absent=`None`, real `0` зберігається, невідстежуваний день відсутній); `engagement_rate` ×100.
- `test_compare.py` (7) — тотожні цілі → pass; один клік різниці → fail на exact-метриці; малий відносний дрейф у межах допуску; дні в лагу виключені; день тільки з одного боку → fail; `None` з одного боку → fail; `None` з обох → тихо пропущено.

Плюс 213 наявних `tests/unit/integrations/` зелені (RN/GSC адаптери — дефолтний шлях без ключа `transport` не змінився).

### Follow-ups / борг

- **G5 (≥48 год живо) + G6 (живий відкат) + G7 (лічильник після)** — запис у бойову БД у цій сесії був заблокований класифікатором; команди готові (див. «Як перевірити»). Це операторський крок cutover-класу, а не код.
- **`task-02` синхронізувати** — додати примітку про необов'язковий `config_json["transport"]` (CLAUDE.md §5).
- **`TODO(task-84)`** — решта GA4-датасетів на прямий API (вже почато паралельною сесією).
- **`TODO(task-85)`** — перейменування `rn_*` ключів метрик (§2.4).
- **`TODO(task-86)`** — перемикач транспорту в UI (§6.3).
- **EFFYS / GRP** — URL-prefix property (§0.9), операційна дія в кабінеті Google.
- **`compare-transports` живі прогони** — рекомендовано розширити на GRP, SWM, PJC перед масовим cutover.
- **`app.metrics.portfolio` не має `engagement_rate` серед 7 серій** — тобто ×100-нормалізація потрібна лише для паритету датасету (споживачі: сторінка клієнта, URL Inspector), не для `#/trends`.
