# task-10 — Provider: Ahrefs (Site Audit вертикальний зріз)

- **Пакет:** lightweight Site Audit snapshot wrapper у AISeoTools + app-адаптер Ahrefs
- **Хвиля:** C
- **Depends on:** task-02 (contract), task-08 (AISeoTools core), task-05 (worker), task-04 (ключі/snapshot-семантика)
- **PLAN.md:** §2.2 (Ahrefs Site Audit/Rank), §3.2 (KPI Ahrefs), §8.3 (source policy), §12, Етап 5 (gate)

## Мета

Легкий **Site Audit snapshot** (Health Score, Errors, Warnings, URLs crawled) без повного XLSX/AI-verdict pipeline. Провайдер snapshot-типу: попереднє значення береться з історії БД, а не «сумується за місяць». Відсутній health score → `unavailable`, ніколи не `0`. Не плутати crawl date з collection date.

## Обсяг

**Входить:**
- **AISeoTools** (`seo_audit/dashboard/ahrefs.py`): тонкий collect навколо наявного `ahrefs_site_audit.collect.collect()` (повертає `AhrefsMachineFindings`) + `verify.pipeline.verify_issues()` (вже вміє прогрес) — але **без** важкого повного аудиту заради 4 KPI (PLAN §2.2). Additive `on_event`. API → CSV/browser fallback з quality badge.
- **App** (`app/integrations/ahrefs.py`): `ProviderAdapter(provider=ahrefs)`, `preflight()` (project lookup, units estimate, profile/API availability), `collect()`; мапінг у `CollectionResult` (snapshot-метрики, `observed_at`, crawl-date у provenance окремо від collection-date). Реєстрація в registry.
- Обробка станів: немає project / немає units / немає health score → відповідний `Quality`/`reason_code`, не вигадані числа. Stale crawl явно позначений.

**НЕ входить:** повний Rank Movement аудит (важкий, не для кожного відкриття dashboard — PLAN §2.2), XLSX/AI verdicts, інші провайдери.

## Owned paths
```
# AISeoTools
seo_audit/dashboard/ahrefs.py
tests/dashboard/test_ahrefs.py
# App
app/integrations/ahrefs.py
tests/unit/integrations/test_ahrefs.py
```

## Consumes
- contract/registry/DTO — task-02.
- AISeoTools `dashboard.events/models/config` + наявні `ahrefs_site_audit.collect.collect`, `verify.pipeline.verify_issues` — task-08 / наявний код.
- catalog ключі Ahrefs, snapshot-семантика delta — task-04.

## Provides
- Зареєстрований адаптер `ahrefs` (snapshot), готовий для worker.
- Health/issues у канонічних ключах + provenance (API/CSV/browser, crawl date).

## Кроки реалізації
1. AISeoTools thin wrapper: project → issues → affected → (за потреби) live verify, з `on_event` після кожного етапу; **без** повного XLSX/AI.
2. API → CSV/browser fallback з quality badge (browser fallback може не мати project metadata/health score → `unavailable`).
3. App-адаптер: preflight (project/units), collect, мапінг snapshot-метрик; crawl-date у provenance, collection-date = `observed_at`.
4. Missing health / no project / no units → коректний Quality/`reason_code`.
5. Реєстрація в registry.

## Тести
- no-project / no-units / no-health cases → `unavailable`/partial, не `0` (PLAN §15).
- crawl date ≠ collection date (не плутаються; stale crawl позначений).
- API↔CSV↔browser fallback дає узгоджені ключі + правильний quality badge.
- секрети/units не течуть у events.
- Живий smoke (поза CI): контрольний клієнт, health/issues звірені з Ahrefs UI.

## Definition of Done / Gate
- [x] current/previous не плутає crawl date з collection date; stale crawl явно позначений (PLAN Етап 5 gate).
- [x] Відсутній health score → `unavailable`, не `0`.
- [x] Легкий шлях (без повного XLSX/AI) — швидкий для dashboard.
- [x] Fallback policy (API/CSV/browser) з quality badge працює.
- [x] Адаптер зареєстрований і проганяється worker'ом.

## Ризики
- Частина API-планів не має Site Audit / обмежені units (PLAN §2.2) → preflight-estimate + hard budget + явний fallback.
- Browser fallback без метаданих project → чесний `unavailable`, не вигадувати.

---

## ✅ Completion report

- **Дата:** 2026-07-22
- **Гілка / комміт:** main (застосунок) — цей коміт; AISeoTools — незакомічено, див. Follow-ups
- **Статус:** done-with-followups

### Що зроблено

Легкий Ahrefs Site Audit snapshot-адаптер, вертикальний зріз від AISeoTools до worker'а:

- **AISeoTools** (`seo_audit/dashboard/ahrefs.py`, новий): `collect_site_audit()` — тонка обгортка над наявним `ahrefs_site_audit.collect.collect()`. Викликає його з `fetch_affected=False` (без per-issue affected-URL enrichment — жоден з 4 KPI її не потребує) і мапить `AhrefsMachineFindings` у source-neutral `DashboardResult`/`DashboardMetricValue` (task-08). `run_verify=False` за замовчуванням лишає гачок для `verify.pipeline.verify_issues()` на майбутнє (детальні view), не використовується цим snapshot-шляхом.
- **AISeoTools** (`seo_audit/ahrefs_site_audit/collect.py`, точкова адитивна правка): додано `on_event: OnEvent | None = None` (keyword-only, дефолт `None`) до `collect()`/`collect_from_api()`/`collect_from_csv()` — події після project/issues/affected/collect_done (PLAN §8.2). Стара поведінка не змінена (`make_emitter(None)` — no-op); контрактний signature-снепшот (task-08) свідомо перегенеровано — єдина зміна: новий optional kwarg у кінці сигнатури `collect()`.
- **App** (`app/integrations/ahrefs.py`, новий): `AhrefsAdapter` (`ProviderAdapter`) — `preflight()` (структурна перевірка: `domain` в `integration_config`, підтримка `source_mode`, наявність `AHREFS_API_KEY`/`csv_dir`) + `collect()` (API-first з CSV-фолбеком у `auto`, обробка `UnitsExhausted`, per-metric `Quality`, crawl-vs-collection date, cooperative cancel). Зареєстрований у `registry` через `app/integrations/__init__.py` (спільний, неексклюзивний bootstrap-файл — кожна провайдер-таска додає свій власний рядок імпорту).

### Як зроблено (ключові рішення)

- **`fetch_affected=False` завжди.** 4 KPI картки (Health Score, Errors, Warnings, URLs crawled) обчислюються з `AuditTotals`/`health_score`/`project` — жоден не потребує списку affected URLs. Це одночасно і "легкий шлях" (DoD), і "hard budget" (Ризики): вартість фіксована на 2 flat-cost ендпоінти (`/site-audit/projects` + `/site-audit/issues`, по 50 units кожен) незалежно від розміру сайту, замість необмеженого per-issue циклу, який використовує повний XLSX/AI-аудит.
- **`preflight()` не робить живий API-виклик.** Обговорювана альтернатива — реальний `find_project()` у preflight для точнішого `units_estimated` — відхилена: подвоює API-вартість (project lookup двічі: preflight + collect) і плутає "конфігураційний gate" (не міняється день у день) із "результатом спроби" (може змінюватись). Замість цього: `not_configured`/`auth_required` — лише структурні перевірки (домен, ключ/csv_dir); "project не знайдено" — це `collect()`-time дані (`Quality.unavailable` на рівні метрик), персистяться як історія, а не блокують клітинку до `not_configured`.
- **`None` vs фабрикований `0` для project-only полів.** `AhrefsMachineFindings.totals.urls_crawled` за дефолтом `0` і на CSV-шляху, і коли API не знайшов project (`_totals()` викликається з порожнім `meta`-dict у обох випадках) — нерозрізнимо від справжнього нуля. Вирішено: `urls_crawled` довіряється лише коли `machine.project_id` непорожній (тобто API реально знайшов project). `totals.errors`/`.warnings` (issue-TYPE рахунки) довіряються ширше — вони обчислюються з `machine.issues`, коректні і на API-, і на CSV-шляху — окрім порожнього списку на CSV-шляху, який неможливо надійно відрізнити від "CSV-файл не знайдено" без крихкого string-matching по warning-тексту (задокументовано як limitation нижче).
- **Crawl date ≠ collection date.** `DashboardResult.data_through`/`CollectionResult.data_through` = розпарсена `crawl_date` (аналог GSC-лагу); `observed_at` = момент збору. Якщо crawl передує `context.current.start` — усі присутні метрики отримують `Quality.stale` + явний warning з обома датами. Raw+parsed crawl date також дублюються у `provenance` окремо.
- **`UnitsExhausted` (BaseException, `seo_audit.units_fallback`) навмисно не ловиться в AISeoTools-обгортці** (як і в `collect()` самому) — ловиться виключно в app-адаптері (`AhrefsAdapter.collect()`), де контракт `ProviderAdapter` вимагає "ніколи не кидати". Без CSV-фолбеку → `Quality.unavailable`/`error_code="no_units"`; з CSV (лише `auto`) → фолбек-спроба.
- **`auto` fallback — API спочатку, CSV лише якщо результат `unavailable`** (не при `partial`, щоб не губити часткові API-дані заради гіршого CSV-знімка) або якщо стався `UnitsExhausted`. Обраний результат — з кращою (нижчою за severity) агрегатною якістю; `provenance["fallback_used"]` фіксує, який шлях реально спрацював.
- **`domain` — лише через `integration_config["domain"]`**, не через `Client.domain`: контракт `CollectionContext` (task-02, read-only) не передає жодних полів `Client`, окрім `client_id`. Follow-up нижче.
- **`app/integrations/__init__.py`** — новий, навмисно неексклюзивний bootstrap-файл (не в чиємусь Owned paths): просто `from app.integrations import ahrefs`, тригерить `register_adapter()` через звичайну семантику ініціалізації пакета (перевірено: `import app.integrations.registry`/`app.jobs.worker` вже досить). Паралельний агент (task-09) додав туди ж свій рядок (`gsc, reporting_ninja`) без конфлікту — підтверджує задум.

### Контрактні зміни (Contract changes)

- **AISeoTools, не app:** `ahrefs_site_audit.collect.collect()`/`collect_from_api()`/`collect_from_csv()` отримали новий optional keyword-only `on_event: OnEvent | None = None` (дефолт `None`, стара поведінка байт-в-байт незмінна). Дозволений тип дрейфу за PLAN §8.1/task-08's contract-snapshot test docstring; signature-снепшот (`tests/dashboard/data/aiseotools_contract_snapshot.json`) свідомо перегенеровано після рев'ю діффу (лише цей один рядок змінився).
- **App (`app/schemas/**`, `app/integrations/contract.py`) — не чіпались**, `extra="forbid"` контракти task-02 незмінні.
- **`app/integrations/__init__.py`** — новий файл (раніше порожній), тепер імпортує провайдер-модулі для реєстрації; спільний для task-09/10/11, без ексклюзивного власника.

### Як перевірити (verification)

AISeoTools (на хості, поза Docker — окремий репозиторій зі своїм .venv, той самий флоу, що й у task-08):
```bash
cd /home/elon/dev/seo/AISeoTools
.venv/bin/python -m pytest tests/dashboard/test_ahrefs.py -q         # 10 passed
.venv/bin/python -m pytest tests/test_ahrefs_site_audit.py -q        # 81 passed (регресія collect.py незмінна)
.venv/bin/python -m pytest tests/dashboard -q                        # весь dashboard namespace
.venv/bin/python -m pytest -q                                        # повна regression suite
```

Застосунок (лише Docker, за CLAUDE.md):
```bash
docker compose build api
docker compose run --rm api poetry run ruff check app/integrations tests/unit/integrations
docker compose run --rm api poetry run mypy app                       # Success: no issues found in 62 source files
docker compose run --rm api poetry run pytest tests/unit/integrations/test_ahrefs.py -q   # 16 passed
docker compose run --rm api poetry run pytest -q                      # 255 passed (уся suite)
```

Живий смоук через реальний worker (виконано під час цієї таски):
```bash
API_PORT=8010 docker compose up -d --build
# not_configured/auth_required шлях: клієнт з ahrefs enabled, AHREFS_API_KEY не встановлено
# → cell status not_configured, error_code=auth_required (перевірено)
# success шлях: AHREFS_API_KEY встановлено + seo_audit.ahrefs_site_audit.collect.collect
# замокано (unittest.mock.patch) на реальний AhrefsMachineFindings-фікстур
# → run_cell(...) status=succeeded, snapshot.quality=complete,
#   4 метрики (health_score=77, errors=1, warnings=0, urls_crawled=42) з Quality.complete
docker compose down
```

### Тести

- **AISeoTools** `tests/dashboard/test_ahrefs.py` (10, новий, мій): complete-результат при знайденому project; "project не знайдено" → усі 4 метрики `None`/`unavailable`, ніколи `0`; CSV-шлях з розпарсеними issues довіряє issue-рахункам, але не `urls_crawled`; CSV-шлях без розпарсених issues → `unavailable`; unparseable crawl_date не падає і лишає raw-рядок; `on_event=None` — no-op; `on_event` отримує stage-події; `run_verify=True` прокидає legacy `progress(str)` у `on_event`, пропускається коли `issues` порожній; `UnitsExhausted` не ковтається обгорткою.
- **Застосунок** `tests/unit/integrations/test_ahrefs.py` (16, новий, мій): preflight (`not_configured`/`auth_required`/`pdf` unsupported/`browser` без і з `csv_dir`/`auto` без ключа з csv fallback); collect: complete-мапінг, `None`-значення → `unavailable`-метрика тягне агрегат вниз (не 0), stale-crawl → усі присутні метрики `stale` + warning, `auto`-фолбек API→CSV коли API `unavailable`, `UnitsExhausted` без CSV → `no_units`, `UnitsExhausted` з CSV → фолбек спрацьовує, cooperative cancel (`CancelledByAdapter`) до старту, відсутність будь-якого джерела → `collection_unavailable`; окремий `_bridge_emit` unit-тест на трансляцію dashboard-подій у app-подію.
- **Live E2E smoke** (поза pytest, описаний вище): повний `run_cell()` через реальні репозиторії/SQLite з замоканим `collect()` на мережевому рівні — підтверджує, що `preflight`→`collect`→`normalize`→`persist` працює наскрізно з реальним worker-кодом (task-05), не лише з юніт-моками адаптера.

### Follow-ups / борг

- **`domain` для Ahrefs береться виключно з `integration_config["domain"]`**, не з `Client.domain` — `CollectionContext` (task-02 contract) не передає жодних полів `Client`, окрім `client_id`. Оператору доведеться дублювати домен у конфіг Ahrefs-інтеграції вручну (UI/API для цього — поза цією таскою, task-06/12). Не контрактна зміна (не чіпав `CollectionContext`), але зручність варто розглянути пізніше: дефолтити `integration_config["domain"]` на `Client.domain` при upsert інтеграції, якщо не задано явно.
- **`totals.errors`/`.warnings` на CSV-шляху з нуля розпарсених issues невідрізнимі від "CSV-файл не знайдено"** (`ahrefs_site_audit/collect.py::collect_from_csv`/`csv_parser.py::parse_directory` — поза owned paths цієї таски). Консервативно трактується як `unavailable`; сайт із дійсно нульовою кількістю issues на CSV-шляху буде хибно позначений як "немає даних" замість "0 issues". Виправлення вимагало б або структурованого сигналу з `parse_directory` (а не лише warning-рядка), або зміни в `collect_from_csv` — обидва поза owned paths, документується як відомий upstream-gap.
- **`units_spent` не проганяється структурно.** `collect_from_api()` рахує `units_spent`/`cache_hits` і кладе їх у `details` нового `collect_done` progress-event (структуровано, не лише у warning-рядку) — але `DashboardResult.units_spent`/`CollectionResult.units_spent` лишаються `None`, бо `collect()` не повертає ці числа у самому return-shape (лише через event-побічний ефект, і лише коли задано `on_event`). Повне рішення вимагало б повернення `units_spent` у самій `AhrefsMachineFindings`/return shape `collect()` — контрактна зміна поза цією таскою (можна підняти в майбутньому task-08-owned follow-up).
- **AISeoTools-бік не закомічено.** `seo_audit/ahrefs_site_audit/collect.py` (адитивна правка), `seo_audit/dashboard/ahrefs.py`, `tests/dashboard/test_ahrefs.py`, перегенерований `tests/dashboard/data/aiseotools_contract_snapshot.json` лишаються uncommitted у `/home/elon/dev/seo/AISeoTools` — той самий репозиторій одночасно редагують паралельні агенти (task-09: `gsc.py`/`reporting_ninja.py`), тож комітити зараз ризиковано захопити чужі незавершені файли. Комітити/пінити новий tag (за зразком `dashboard-core-v1` від task-08) — окремий, свідомий крок користувача, коли паралельна робота у AISeoTools завершиться.
- **`pyproject.toml` AISeoTools-залежність лишається `path`-dependency** (`develop = true`), не git-pin — task-08 вже позначила цей TODO(task-01) "коли застосунок реально почне імпортувати `seo_audit.dashboard`" — це сталося зараз (ця таска). Запит до task-01: замінити на `git`-залежність, запінену на конкретний AISeoTools-комміт/тег (після того, як зміни цієї таски там будуть закомічені й, можливо, отримають власний тег).
- **AISeoTools-side повний regression suite не перепрогнаний в самому кінці сесії** через транзієнтну поломку `.venv` (символічне посилання `.venv/bin/python → /usr/local/bin/python3.12`, якого не існує на хості) — очевидно побічний ефект паралельної агентської роботи в тому ж репо. Було зелено (1434 passed) одразу після контрактного regen, до цього збою; варто перепрогнати `.venv/bin/python -m pytest -q` після відновлення оточення як фінальну підстраховку.
- Немає automated browser-скрапера для Ahrefs Site Audit у AISeoTools — `SourceMode.browser` тут означає читання вручну експортованого CSV-каталогу, не live-сесію. Якщо AISeoTools колись отримає такий скрапер (аналогічно Semrush/GSC), `preflight()`/`collect()` цього адаптера доведеться розширити відповідно (адитивно, той самий owned path).
