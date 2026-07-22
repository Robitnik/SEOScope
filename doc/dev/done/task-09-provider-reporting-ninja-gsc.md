# task-09 — Provider: Reporting Ninja / GA4 + GSC (вертикальний зріз)

- **Пакет:** arbitrary-period wrapper у AISeoTools + app-адаптери RN/GA4 і GSC
- **Хвиля:** C
- **Depends on:** task-02 (adapter contract), task-08 (AISeoTools core), task-05 (worker ганяє adapter), task-04 (ключі метрик/derived)
- **PLAN.md:** §2.2, §3.2 (KPI GSC + RN/GA4), §8.2/§8.3 (source policy), §12 (correctness), Етап 4 (vertical slice, gate)

## Мета

Найнадійніший MVP-шлях: зібрати **GA4** (Users, Organic Users, Total Conversions, Organic Conversions) і **GSC** (Clicks, Impressions, CTR, Avg Position) за два **довільні** періоди (current/previous). Реалізувати новий comparison-wrapper у AISeoTools (старий six-period `build_report()` **не чіпати**) і два app-адаптери під контракт task-02.

## Обсяг

**Входить:**
- **AISeoTools** (`seo_audit/dashboard/reporting_ninja.py`, `seo_audit/dashboard/gsc.py`):
  - новий wrapper, що викликає `discover_*_fields()` **один раз**, потім `fetch_ga4_period()`/`fetch_gsc_period()` **двічі** (current + previous) через наявні fetchers (PLAN §2.2, §8.2);
  - GSC aggregate над Reporting Ninja API за замовчуванням; direct GSC browser/export для CTR, avg position і детальних даних (PLAN §8.3);
  - емісія `ProgressEvent` (task-08 core), `data_through` для GSC data lag.
- **App** (`app/integrations/reporting_ninja.py`, `app/integrations/gsc.py`):
  - два `ProviderAdapter` (`provider = reporting_ninja` та `gsc`), `preflight()` + `collect()`;
  - мапінг AISeoTools-моделей → `CollectionResult`/`MetricValueResult` з канонічними ключами метрик (task-04 catalog);
  - CTR як **derived** (`clicks/impressions`) лише коли обидва complete; `is_derived=True`;
  - реєстрація адаптерів у registry (task-02).

**НЕ входить:** старий `build_report()` (заморожений), Ahrefs/Semrush (10/11), UI.

## Owned paths
```
# AISeoTools (репо /home/elon/dev/seo/AISeoTools)
seo_audit/dashboard/reporting_ninja.py
seo_audit/dashboard/gsc.py
tests/dashboard/test_reporting_ninja.py, test_gsc.py
# App
app/integrations/reporting_ninja.py
app/integrations/gsc.py
tests/unit/integrations/test_reporting_ninja.py, test_gsc.py
```

## Consumes
- `ProviderAdapter`, `CollectionContext/Result`, `MetricValueResult`, `Provider`, `Quality`, registry — task-02.
- AISeoTools `dashboard.events/models/config` + наявні `ga4_fetcher.fetch_ga4_period`, `gsc_fetcher.fetch_gsc_period`, discovery — task-08 / наявний код.
- catalog ключі, derived-правило CTR — task-04.

## Provides
- Зареєстровані адаптери `reporting_ninja` і `gsc`, готові для worker.
- Точні current/previous діапазони + `data_through` у результаті.

## Кроки реалізації
1. AISeoTools comparison-wrapper (discover×1 → fetch×2), additive, з `on_event`.
2. GSC direct-extension для CTR/position (browser/export) з quality/`data_through`.
3. App-адаптери: preflight (credentials/session/property mapping), collect (стадії §9), мапінг у `CollectionResult`.
4. Derived CTR; None-safe (немає даних → `unavailable`, не 0).
5. Реєстрація в registry.

## Тести
- AISeoTools: wrapper зве discovery один раз і fetch двічі; старий `build_report` byte/shape-незмінний (regression, PLAN §15).
- App: мапінг у канонічні ключі; CTR derived лише при complete; `data_through` проброшено; partial/`unavailable` при відсутності; секрети не течуть.
- Живий smoke (поза CI, PLAN §15 live): один контрольний клієнт, точні дати й totals звірені вручну з GSC/Ninja UI.

## Definition of Done / Gate
- [ ] Один реальний клієнт збирається з UI; точні дати й totals збігаються з джерелом (PLAN Етап 4 gate).
- [ ] Старий `client-report`/`build_report` flow не зламаний (regression зелений).
- [ ] GSC data lag відображено через `data_through`; CTR помічений derived.
- [ ] `None` ≠ `0`; секрети не потрапляють у events/result.
- [ ] Адаптери зареєстровані й проганяються worker'ом.

## Ризики
- Розбіжність семантики періодів Ninja (PLAN §2.3.1) → періоди дає task-04 engine, не старий `get_report_periods`.
- Reporting Ninja fetcher повертає лише clicks/impressions для GSC → CTR/position беремо з direct GSC (PLAN §2.2/§8.3), позначаємо provenance.
- Browser-профіль GSC/Ninja: lock `(provider, profile_name)`; не паралелити (task-05/AISeoTools).

---

## ✅ Completion report

- **Дата:** 2026-07-22
- **Гілка / комміт:** `feat/task-12-frontend` @ (некомічено — див. «Follow-ups», п. 1)
- **Статус:** done-with-followups

### Що зроблено

- AISeoTools (`seo_audit/dashboard/`): новий arbitrary-period comparison wrapper для GA4
  (`reporting_ninja.py::collect_ga4`) та два незалежні GSC-джерела (`gsc.py`):
  `collect_gsc_aggregate` (clicks/impressions через Reporting Ninja API, default per PLAN
  §8.3) і `collect_gsc_direct` (avg position через живий Performance-export у браузері).
  Старий `clientreport.builder.build_report()` не чіпав — жодного імпорту з нового коду в
  нього і назад.
- App (`app/integrations/`): `ReportingNinjaAdapter` (`provider=reporting_ninja`) і
  `GscAdapter` (`provider=gsc`), обидва реалізують `ProviderAdapter` (preflight+collect),
  мапують source-neutral ключі AISeoTools на канонічні catalog-ключі (`rn_users`,
  `rn_organic_users`, `rn_total_conversions`, `rn_organic_conversions`, `gsc_clicks`,
  `gsc_impressions`, `gsc_avg_position`), зареєстровані в registry.
- Інфраструктурний блокер, виявлений і закритий до початку основної роботи: AISeoTools
  не був реально підключений у Docker-образі (пуста `local`-група, `Dockerfile` без
  `--with local`, невикористаний `additional_contexts`). Полагоджено через
  `compose.yaml`/`docker/entrypoint.sh`/`pyproject.toml` — див. «Контрактні зміни» нижче.
  Без цього жоден provider-таск (09/10/11) не міг би реально імпортувати `seo_audit`
  усередині контейнера.

### Як зроблено (ключові рішення)

- **CTR ніколи не обчислюється в task-09.** І AISeoTools-wrapper, і app-адаптер свідомо
  ніколи не емітять `gsc_ctr`/`ctr` — `app.metrics.normalize.normalize_result` (task-04,
  вже готовий) сам домальовує `gsc_ctr = gsc_clicks / gsc_impressions`, лише коли обидва
  `complete` для одного `period_role`. Дублювати цю арифметику в task-09 означало б два
  місця для розбіжності.
- **GSC — два незалежні джерела, що падають незалежно.** `GscAdapter._collect` викликає
  `collect_gsc_aggregate` (Ninja) і, якщо в `integration_config` є `property`,
  `collect_gsc_direct` (browser) окремо; quality рахується **по кожній метриці**, а не
  одним спільним прапорцем — відмова сесії GSC не тягне вниз clicks/impressions, і
  навпаки. Якщо `property` не задано — `gsc_avg_position` іде як явний `unavailable`
  (не «мовчазна відсутність», а окремий запис із причиною в `warnings`), а не блокує
  всю клітинку: Ninja-агрегат — дефолтне джерело й самодостатній (PLAN §8.3).
- **`seo_audit` імпортується лише лениво**, всередині тіл методів `preflight`/`collect`
  обох адаптерів — ніколи на рівні модуля. Причина: `app/integrations/__init__.py`
  імпортує кожен provider-модуль безумовно (щоб `register_adapter()` спрацював), і цей
  імпорт відбувається транзитивно скрізь, де worker чіпає `app.integrations.registry`
  (уже в `app/jobs/runner.py`). Модульний імпорт `seo_audit` зробив би саму наявність
  `app.integrations` пакету залежною від локального AISeoTools checkout — і зламав би
  геть усе (включно з тестами інших провайдерів) у будь-якому середовищі без нього
  (CI, deploy). Лениві імпорти означають: відсутній AISeoTools → `preflight()` чесно
  повертає `not_configured`, а не крашить процес імпорту.
- **`collect()` ловить `BaseException`, не лише `Exception`.** AISeoTools-виключення на
  зразок `SessionExpired`/`ProfileLocked`/`BotChallenge` навмисно успадковують
  `BaseException` (не `Exception`) — читай docstring `seo_audit/browser/errors.py`: вони
  мають лишатись термінальними для CLI. Але `app/jobs/runner.py::_collect_with_timeout`
  (task-05, вже готовий) перехоплює й ретранслює `BaseException` із robочого потоку, а
  зовнішній guard у `run_cell` ловить лише `Exception` — тобто необроблений
  `BrowserAutomationError` пройшов би крізь «нуль падінь» межу і впав би цілу lane.
  Тому і AISeoTools-wrapper (`collect_gsc_direct`), і app-адаптер (`collect()`) явно
  ловлять ці випадки й повертають структурований `unavailable` + стабільний
  `error_code` (`auth_required`, `profile_locked`, `bot_challenge`, `export_failed`, …).
- **Direct-extension відкриває ОДНУ browser-сесію на обидва періоди** (не дві) —
  запуск Chrome і визначення Google-акаунту (`resolve_authuser`) дорожчі за сам export;
  помилка браузера в середині циклу — термінальна для всього direct-attempt (без
  часткового «одна дата вийшла, друга ні» з browser-джерела), відповідно до власної
  філософії AISeoTools (`browser/errors.py`: «a silent partial here would ship a report
  with quietly missing rows»).
- **`data_through` — консервативна константа (`GSC_DATA_LAG_DAYS = 2`)**, застосована
  однаково в обох GSC-джерелах (`min(period_end, today - lag)`), а не жива перевірка
  банера GSC — властивість пайплайну GSC, а не транспорту. App-адаптер бере
  `min(aggregate.data_through, direct.data_through)`, коли обидва присутні.
- Головні файли:
  - `AISeoTools/seo_audit/dashboard/reporting_ninja.py` — GA4 comparison wrapper.
  - `AISeoTools/seo_audit/dashboard/gsc.py` — GSC aggregate + direct wrappers,
    `_avg_position_from_csv` (плоске середнє по `Position`-колонці, без ваги — GSC-export
    не дає per-row ваги, як і `browser.providers.gsc._perf_totals`, але переізольовано
    тут, а не імпортовано з того модуля, аби не тягнути залежність на CLI-internal).
  - `app/integrations/reporting_ninja.py`, `app/integrations/gsc.py` — адаптери +
    `register_adapter(...)` виклик у кінці кожного файлу.
  - `app/integrations/__init__.py` — спільна точка реєстрації (не в «Owned paths»
    жодної таски; task-09/10 вже додають сюди свої import-рядки незалежно, без
    конфлікту — узгоджено з паралельною сесією task-10 під час роботи).

### Контрактні зміни (Contract changes)

Публічний `app.schemas`/`app.integrations.contract` контракт — **без змін**. Але щоб
provider-таски взагалі могли імпортувати `seo_audit` у Docker, довелось торкнутись
файлів поза «Owned paths» task-09 (`Dockerfile`, `compose.yaml`,
`compose.override.example.yaml`, `pyproject.toml`, `.env.example`, `docker/entrypoint.sh`
— усі task-01-owned). Це свідома, узгоджена з користувачем дія (заблокований старт без
неї — жоден provider-таск не міг реально запустити тести в Docker), задокументована тут
per CLAUDE.md «Потрібна нова залежність → запит до task-01, можна тимчасово як
follow-up у completion report»:

- `compose.yaml`: обидва сервіси (`api`, `worker`) отримали bind-mount
  `${AISEOTOOLS_PATH:-../../seo/AISeoTools}:/seo/AISeoTools` (весь checkout, включно з
  його власним `.env`/сесіями — вони потрібні `ReportingNinjaClient`/browser-профілям
  живцем, і НЕ печуться в образ). `additional_contexts.aiseotools` лишився (для
  майбутнього pinned-build), просто зараз не використовується `Dockerfile`.
- `docker/entrypoint.sh`: на старті контейнера, якщо `/seo/AISeoTools/seo_audit`
  змонтовано і `seo_audit` ще не імпортується — виконує
  `poetry install --no-root --with local`. Якщо каталогу немає (CI/deploy) — пропускає,
  без помилки.
- `pyproject.toml`: коментар над `[tool.poetry.group.local]` оновлено (більше не
  «нічого не встановлює», а «встановлюється entrypoint-ом за наявності checkout-у»).
  Сам список залежностей у групі не чіпав.
- `.env.example`, `compose.override.example.yaml`: додано/задокументовано
  `AISEOTOOLS_PATH` — один env var керує і build-context, і volume-mount.
- `Dockerfile` **не чіпав** — свідомо: CI (`docker build .` без сусіднього checkout-у)
  мусить лишитись робочим для не-provider тестів (див. Follow-ups, п. 2).

### Як перевірити (verification)

```bash
# AISeoTools — нові + повний regression (respx-залежні ahrefs-тести виключено,
# вони не встановлені в local-групі й не стосуються цієї таски):
docker compose run --rm api poetry run pytest /seo/AISeoTools/tests/dashboard/ -q
docker compose run --rm api poetry run pytest /seo/AISeoTools/tests/ -q \
  --ignore=/seo/AISeoTools/tests/test_ahrefs_api.py \
  --ignore=/seo/AISeoTools/tests/test_ahrefs_audit.py
# → 55 passed (dashboard); 1421 passed, 6 skipped (full suite)

# App — цільові тести, повний suite, лінт, типи:
docker compose run --rm api poetry run pytest tests/unit/integrations/ -q
docker compose run --rm api poetry run pytest -q
docker compose run --rm api poetry run ruff check .
docker compose run --rm api poetry run mypy app
# → 278 passed; ruff/mypy чисті

# Реєстрація адаптерів у реальному пакеті (разом із паралельним task-10/ahrefs):
docker compose run --rm api poetry run python -c "
import app.integrations
from app.integrations.registry import get_adapter, available_providers
from app.schemas.enums import Provider
print(sorted(p.value for p in available_providers()))
assert get_adapter(Provider.gsc).provider == Provider.gsc
assert get_adapter(Provider.reporting_ninja).provider == Provider.reporting_ninja
"
# → ['ahrefs', 'gsc', 'reporting_ninja']; OK
```

Перевіряльник має побачити: усі команди вище зелені без ручного втручання (образ уже
збирає entrypoint-ом `--with local` за наявності `/seo/AISeoTools`).

### Тести

- `AISeoTools/tests/dashboard/test_reporting_ninja.py` (7 тестів): discover×1/fetch×2,
  точні periods у виклику fetch, мапінг `conversions→total_conversions`, `None` лишається
  `None`, `quality=unavailable` при відмові discovery, `partial` при відмові одного
  періоду, `on_event=None` — no-op, події несуть правильні stage-мітки, і regression-тест
  на незмінність `build_report()` (discover×1/fetch×6, ті самі 6 legacy-періодів).
- `AISeoTools/tests/dashboard/test_gsc.py` (24 тести): `collect_gsc_aggregate` (те саме,
  що і GA4, +`gsc_ctr` ніколи не в values, +`data_through` capped by GSC-lag тільки для
  свіжих періодів); чиста unit-перевірка `_avg_position_from_csv` (mean, відсутній файл,
  порожні рядки, варіанти назви колонки, пропуск непарсованих значень); `collect_gsc_direct`
  (одна browser-сесія на обидва періоди, коректний mean по періоду, `None` при
  відсутніх рядках, `BrowserAutomationError` (`SessionExpired`/`ExportFailed`) ніколи не
  проривається назовні — мапиться в стабільний `error_code`).
- `tests/unit/integrations/test_reporting_ninja.py` (10 тестів): preflight
  (not_configured/auth_required/ok), мапінг у канонічні ключі, синтез `unavailable`
  для відсутнього періоду (не 0 і не мовчазний пропуск), передача помилки discovery,
  `except BaseException` у `collect()` ніколи не пробиває межу, event-bridge (включно з
  «зламаний callback ніколи не рве collect»).
- `tests/unit/integrations/test_gsc.py` (18 тестів): preflight (property НЕ обов'язковий
  для ok), мапінг обох джерел разом, `gsc_ctr` ніколи не емітиться, відсутність `property`
  → `gsc_avg_position` explicit-unavailable з попередженням (а не тиха відсутність),
  незалежний відмова кожного джерела (direct падає → aggregate лишається complete, і
  навпаки), `data_through` = мінімум із двох джерел, adapter-guard на неочікуваний виняток,
  event-bridge.
- Живий smoke (PLAN §15, поза CI) **не виконано** — див. Follow-ups, п. 3.

### Follow-ups / борг

1. **Нічого із зробленого в цій сесії не закомічено.** Репозиторій на момент старту вже
   мав величезний некомічений стек (tasks 02–08, 10 — staged/unstaged, лише один старий
   комміт `c0ea8d3 Docs` у всій історії), і паралельна сесія одночасно дописувала task-10
   в тому ж робочому дереві. Комітити зараз означало б або зібрати чужу незавершену
   роботу в один коміт разом зі своєю, або чіпати спільні файли (`app/integrations/__init__.py`)
   в момент, коли інша сесія могла їх ще редагувати. Залишено як явне рішення користувача:
   що і як комітити (одним великим чи розкладеним по тасках), і чи вже можна.
2. **CI (`​.github/workflows/ci.yml`) зараз зламаний для будь-якого провайдерного тестового
   прогону**, включно з тими, що вже існують від task-10. `docker build .` у CI не має
   доступу до сусіднього AISeoTools checkout (`additional_contexts` там не заданий і не
   може бути — CI runner фізично не має цього каталогу), тож `seo_audit` там ніколи не
   встановиться. Task-09 свідомо тримає власні провайдер-модулі library-safe (лениві
   імпорти — див. «Як зроблено»), але `app/integrations/ahrefs.py` (task-10, чужий owned
   path — не чіпав) імпортує `seo_audit` на рівні модуля; оскільки `app/integrations/__init__.py`
   тепер безумовно імпортує і його, **весь** пакет `app.integrations` — а отже і
   `app/jobs/runner.py`, і практично весь test suite — впаде на колекції тестів у CI.
   Це не наслідок роботи цієї таски, але видно тільки після неї (registry тепер має 3
   провайдери одночасно). Потребує рішення на рівні task-01/13: або лінива import-політика
   для всіх provider-модулів (як у task-09), або CI отримує реальний pinned git-доступ до
   AISeoTools (PLAN §8.1 «pin до commit/tag» — наразі це лише path-залежність), або
   provider-тести позначаються `skipif` без AISeoTools і виключаються з CI explicitly.
3. **Живий smoke-тест (PLAN Етап 4 gate: «один реальний клієнт... точні дати й totals
   збігаються з джерелом») не виконано** — немає ні реальних GSC/Ninja credentials у цьому
   середовищі, ні готового UI (`task-12-frontend` ще не в `done/`), через який його можна
   було б прогнати вручну per PLAN. Увесь інший DoD-чекліст задоволено (regression
   зелений, `data_through`/None-safety/секрети/реєстрація — перевірені тестами вище).
4. `collect_gsc_direct` не приймає готову `GSCConsole`/`BrowserSession` ззовні (лише
   `profile`/`account`) — достатньо для MVP, але ускладнює повторне використання вже
   відкритої сесії, якщо колись знадобиться (наприклад, майбутній ahrefs+gsc спільний
   профіль-раннер). Не проблема зараз: PLAN §9.1 і так тримає по одній клітинці на
   провайдер послідовно.
