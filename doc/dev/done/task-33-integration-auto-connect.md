# task-33 — Integration auto-connect (пошук акаунта провайдера за доменом)

- **Пакет:** кнопка «Connect» у блоці «Needs attention» перестає бути формою для ручного вписування ID — вона сама шукає акаунт провайдера за доменом клієнта, підключає знайдений, показує звіт із верифікацією і одразу пропонує стягнути дані.
- **Хвиля:** G — операційна ергономіка (після task-29/30/31).
- **Depends on:** — (адитивно розширює закриті зони task-02 / 06 / 07 / 12)
- **PLAN.md:** §7 (integration_config — лише неcекретні ідентифікатори), §9.2 (`not_configured` як штатний стан), §10 (REST), §13 (безпека)
- **frontend/AGENTS.md:** обов'язковий (особливо §6 «Стани», §7 «Доступність», §8 «Форми», §9 «Sync/Progress», §14 «Копірайт»)
- **AISeoTools:** **змін не потребує** — обґрунтування в §1.1

---

## Мета (робота оператора, яку закриваємо)

Сьогодні підключення клієнта до провайдера — це ручна робота з підгляданням. Оператор тисне **Connect**, отримує модалку `#integration-dialog` ([dashboard.js:236](../../frontend/js/dashboard.js#L236)) з порожніми полями `connection_key` і `account_id`, і мусить десь узяти ці значення — зі списку `seo-audit client-report --list-connections` або скопіювати з конфіга сусіднього клієнта. Для GA4 це взагалі непідйомно: `account_id` має вигляд `properties/428527008` і з домену не виводиться ніяк.

Після таски: клік на **Connect** → система сама знаходить акаунт за доменом → підключає → показує, до чого саме підключилась і чи пройшов preflight → пропонує кнопку **Collect data now**.

---

## 1. Дослідження (факти, на яких стоїть план)

### 1.1 Примітив пошуку вже існує в AISeoTools

`ReportingNinjaClient.list_connections(integration_id)` (`seo_audit/clientreport/api_client.py:105`) повертає:

```
[{connection_key, connection_name, accounts: [{account_id, account_name}]}]
```

`integration_id` ∈ `{"google_search_console", "ga4"}`. Один `POST /v1/connections`, автентифікація — bearer `REPORTING_NINJA_API_KEY` з `.env` самого AISeoTools.

Чого в AISeoTools **немає**: жодного матчера «домен → акаунт» (перевірено пошуком по `list_accounts`, `find_account`, `resolve_account`, `match_domain` — нуль збігів). Єдиний споживач `list_connections` — CLI-прапорець `--list-connections` (`seo_audit/cli.py:2335`), який лише **друкує** таблицю.

**Висновок:** потрібен рівень матчингу, а не новий транспорт. Тому AISeoTools не чіпаємо взагалі — нульовий ризик для зовнішнього репозиторію і для його contract-snapshot тесту.

### 1.2 Матчер за доменом у нас уже частково є — але недоступний з UI

`app/integrations/aiseotools_sync.py` вміє матчити, проте:

1. запускається **лише** з `POST /api/v1/imports/ai-seo-tools` ([imports.py:64](../../app/api/imports.py#L64)), тобто під час повного імпорту ростера — не по кнопці;
2. GSC матчить коректно за доменом — [`_accounts_by_domain`:41-57](../../app/integrations/aiseotools_sync.py#L41-L57), з правильним пріоритетом `sc-domain:` над URL-prefix;
3. **GA4 матчиться через хардкод-таблицю на 11 доменів** — [`_GA4_ACCOUNT_NAMES`:20-32](../../app/integrations/aiseotools_sync.py#L20-L32).

### 1.3 Хардкод-таблиця — точна причина поточних дірок

У `_GA4_ACCOUNT_NAMES` немає `alphastyle.store`, `laxton.se`, `goreport.com`. Стан живої БД на 2026-08-03 (14 клієнтів × 4 metric-провайдери):

| Клієнт | ahrefs | semrush | gsc | reporting_ninja |
|---|---|---|---|---|
| ALPHA | ok | ok | ok *(додано вручну)* | **відсутній** |
| LAXTON | ok | ok | ok *(додано вручну)* | **відсутній** |
| GOREPORT | ok | ok | **відсутній** | **відсутній** |
| решта 11 | ok | ok | ok | ok |

Збіг рівно з тими доменами, яких немає в таблиці, — не випадковість. Кожен новий клієнт потрапляє в ту саму яму.

### 1.4 Не всім провайдерам потрібен віддалений пошук

| Провайдер | Віддалений пошук | Чому |
|---|---|---|
| `gsc` | так | `account_id` = `sc-domain:x.com`, живе в Ninja `/connections` |
| `reporting_ninja` | так | `account_id` = `properties/<id>`, з домену не виводиться |
| `ahrefs` | **ні** | ідентифікатор — сам домен, уже є в `Client.domain` |
| `semrush` | **ні** | те саме + `database` = `market.lower()` |

Форми конфігів беремо байт-у-байт із наявного writer'а ([aiseotools_sync.py:104-145](../../app/integrations/aiseotools_sync.py#L104-L145)), щоб автопідключення й імпортер не розійшлися.

### 1.5 `upsert_integration` затирає конфіг

[`clients.py:101-104`](../../app/db/repositories/clients.py#L101-L104) робить `integration.config_json = config or {}` — це **replace, не merge**. Автопідключення зобов'язане зливати конфіг вручну, інакше знесе операторські ключі (`unit_budget`, `csv_dir`).

### 1.6 Що на фронті вже готове до переюзу

- рядок уже несе `data-action="integrate"` ([client-card.js:83-101](../../frontend/js/client-card.js#L83-L101)) — розмітку міняти не треба;
- busy-стан кнопки прямо в рядку — патерн `handleProviderLogin` ([dashboard.js:184-208](../../frontend/js/dashboard.js#L184-L208));
- повернення фокуса на opener при закритті `<dialog>` — [dashboard.js:249](../../frontend/js/dashboard.js#L249);
- уся машинерія збору (preflight → SSE → job dock) — `sync-modal.js` + `sse.js` + `job-progress.js`;
- `POST /clients/{id}/integrations/{provider}/preflight` існує з task-06, але фронт його **не викликає ніде**.

---

## 2. Обсяг (scope)

**У обсязі:** пошук і автопідключення для одного `(client, provider)` — того, на якому клікнули; вибір зі списку для неоднозначного GA4; звіт із результатом preflight; перехід на збір даних.

**Поза обсягом:** bulk «Auto-connect all» по всіх клієнтах; верифікація GA4 через `hostName`; редагування/відключення вже підключених інтеграцій.

---

## 3. Рішення

### 3.1 `app/integrations/discovery.py` — чистий матчер

Без FastAPI і без БД, тому повністю юніт-тестований.

```python
@dataclass(frozen=True)
class IntegrationCandidate:
    label: str            # "sc-domain:laxton.se" / "Laxton - GA4"
    detail: str           # connection_key або properties/<id>
    confidence: Literal["exact", "strong", "weak"]
    reason: str           # "domain property match" / "name similarity 0.93"
    score: float
    source_mode: SourceMode
    integration_config: dict[str, Any]

@dataclass(frozen=True)
class DiscoveryOutcome:
    status: Literal["connected", "needs_choice", "not_found", "unavailable"]
    candidates: list[IntegrationCandidate]
    auto_apply: IntegrationCandidate | None
    message: str          # англійською, готове до показу

def discover(provider, *, domain, client_name, market) -> DiscoveryOutcome
```

Правила:

- **GSC** — `normalize_domain(account_id) == normalize_domain(domain)`; `sc-domain:` б'є URL-prefix. Збіг → `exact` → авто.
- **GA4** — три щаблі: (1) аліас-таблиця → `exact` → авто; (2) fuzzy `SequenceMatcher` за нормалізованою назвою проти імені клієнта й домену без TLD, score ≥ 0.92 **і** відрив ≥ 0.15 → `strong` → авто; (3) score ≥ 0.55 → `weak` у список вибору. Якщо нічого ≥ 0.55 — віддаємо повний список, відсортований за score. **Ніколи не глухий кут.**
- **Ahrefs / Semrush** — жодного мережевого виклику, синтетичний `exact` із рядка клієнта.

Деградація: будь-який збій Ninja → `unavailable`, у `message` лише `type(exc).__name__`. Імпорт `seo_audit` — тільки лениво, всередині функції.

Кеш: TTL 60 с на `list_connections(integration_id)` (ліміт API 300 req/min).

### 3.2 Рефактор `aiseotools_sync.py` — делегування

Матчери й аліас-таблиця переїжджають у `discovery.py`; `aiseotools_sync.py` імпортує їх, зберігаючи власні імена й сигнатури, і додає fuzzy як fallback після аліасів. Контрольний тест `tests/unit/importers/test_integration_sync.py` має лишитися зеленим **без правок**.

### 3.3 Один новий ендпоінт

Новий роутер `app/api/discovery.py` (реєструється сам через `pkgutil` — `main.py` не чіпаємо).

```
POST /api/v1/clients/{client_id}/integrations/{provider}/discover   body: {"apply": true}
```

1. 404 `client_not_found`;
2. інтеграція вже є і `enabled` → `already_configured`, нічого не чіпаємо;
3. `run_in_threadpool(discover, ...)` — блокуючий httpx, патерн із [providers.py:120-124](../../app/api/providers.py#L120-L124);
4. є `auto_apply` і `apply=true` → **merge** наявного `config_json` (§1.5) → `upsert_integration`;
5. одразу після запису — `adapter.preflight(context)`, щоб звіт казав правду;
6. інакше → `needs_choice` з кандидатами, **без запису в БД**.

Другого ендпоінта для вибору не треба: кандидат несе готовий `integration_config`, тож picker застосовує його наявним `PUT /clients/{id}/integrations/{provider}`.

### 3.4 Frontend — discovery-first

Клік → busy у рядку → `discoverIntegration()` → `#integration-dialog` у режимі звіту:

- `connected` → що підключено + вердикт preflight; футер: **Collect data now** (primary) + Done;
- `needs_choice` → radio-список кандидатів (label, detail, бейдж confidence, reason); футер: Connect (disabled до вибору) + Enter manually;
- `not_found` / `unavailable` → повідомлення + **Enter manually** розкриває наявну форму.

**Collect data now** → `syncModal.openSelection({ clientId, providers: [provider], trigger })`.

---

## 4. Owned paths

| Шлях | Що робимо |
|---|---|
| `app/integrations/discovery.py` | **новий** — матчер і кандидати |
| `app/api/discovery.py` | **новий** — ендпоінт `/discover` |
| `app/integrations/aiseotools_sync.py` | делегування матчерів, GA4 fallback на fuzzy |
| `app/schemas/api.py` | **адитивно** — `IntegrationCandidateDTO`, `IntegrationDiscoveryDTO`, `IntegrationDiscoverRequest` |
| `frontend/js/api.js` | `discoverIntegration()`; опційний `sourceMode` в `updateIntegration()` |
| `frontend/js/dashboard.js` | discovery-first потік, режим звіту, picker |
| `frontend/index.html` | секція звіту/вибору в `#integration-dialog` |
| `frontend/css/app.css` | стилі звіту, radio-списку, бейджа confidence |
| `frontend/js/sync-modal.js` | опційний преселект `providers` в `openSelection` |
| `tests/unit/integrations/test_discovery.py` | **новий** |
| `tests/unit/api/test_discovery_api.py` | **новий** |

Read-only: `app/jobs/**`, `app/db/**`, `app/integrations/{gsc,reporting_ninja,ahrefs,semrush}.py`, `app/integrations/contract.py`, `frontend/AGENTS.md`, `PLAN.md`, увесь AISeoTools.

Контракт `ProviderAdapter` **не розширюємо**: discovery стосується двох провайдерів із чотирьох і не має нічого спільного зі збором даних — йому не місце в seam'і адаптера.

---

## 5. Definition of Done / Gate

1. `POST /clients/{id}/integrations/{provider}/discover` працює для всіх чотирьох metric-провайдерів і повертає один із п'яти станів.
2. GSC для GOREPORT автопідключається на живому стенді; GA4 для LAXTON віддає осмислений список кандидатів.
3. Автозапис **зливає** конфіг, а не затирає (тест на `unit_budget`).
4. `needs_choice` не пише в БД жодного рядка.
5. У жодній відповіді немає секретів; помилки провайдера — лише клас винятку.
6. Кнопка Connect ніколи не веде в глухий кут: у кожному стані є конкретна наступна дія.
7. Після успіху доступна **Collect data now**, яка відкриває наявну sync-модалку з обраним клієнтом і провайдером.
8. `tests/unit/importers/test_integration_sync.py` зелений **без правок**.
9. Фронт: скріншоти 1440/390, прохід тільки клавіатурою, чекліст `AGENTS.md §15–16`.
10. `ruff` / `mypy app` / `pytest` — зелені **в Docker**.

---

## 6. Тести

- `tests/unit/integrations/test_discovery.py` — GSC (`sc-domain:` пріоритет, чужий домен не матчиться); GA4 (аліас → exact, чиста назва → strong+авто, дві схожі → needs_choice, нічого схожого → повний список); ahrefs/semrush конфіг збігається з `aiseotools_sync`; виняток Ninja → `unavailable` без витоку тексту.
- `tests/unit/api/test_discovery_api.py` — 404; merge не затирає `unit_budget`; `needs_choice` не пише в БД; `already_configured` не чіпає наявний рядок; у відповіді немає секретів.
- `tests/unit/importers/test_integration_sync.py` — контрольний, без правок.

---

## 7. Ризики

| Ризик | Мітигація |
|---|---|
| Fuzzy підбере не той GA4-property → дані не від того сайту | Авто лише при score ≥ 0.92 **і** відриві ≥ 0.15; решта — явний вибір оператора; після запису одразу preflight, у звіті видно, до чого підключились |
| `upsert_integration` затирає `config_json` | Merge на рівні ендпоінта + тест на `unit_budget` |
| Рефактор `aiseotools_sync` ламає імпортер | `test_integration_sync.py` лишається без правок як контроль |
| Reporting Ninja лежить / немає ключа | `unavailable` + «Enter manually» як робочий шлях |
| Ліміт API 300/хв при кліканні поспіль | TTL-кеш 60 с на `list_connections` |

---

## 8. Follow-ups / борг

- Bulk «Auto-connect all» по всіх клієнтах — матчер уже буде готовий, лишиться цикл + агрегований звіт.
- Верифікація GA4 через дименсію `hostName` — стовідсоткова певність ціною ще одного платного запиту.
- Кнопка «Test connection» для вже підключених інтеграцій — ендпоінт `preflight` існує з task-06, але фронт його не викликає ніде.

---

## ✅ Completion report

- **Дата:** 2026-08-03
- **Гілка / комміт:** `task-33-integration-auto-connect` @ `7eb9f75` (бекенд) + фронт-комміт
- **Статус:** done

### Що зроблено

«Connect» більше не форма для ручного вписування ID. Клік запускає пошук акаунта за доменом клієнта, впевнений збіг записується сам, і оператор бачить звіт: до чого підключились, з якого connection key і чи пройшла верифікація — а далі кнопку **Collect data now**, яка веде в наявну sync-модалку з уже обраним клієнтом і провайдером.

Реально закрито на живих даних: LAXTON, ALPHA і GOREPORT отримали `reporting_ninja` автоматично (`Laxton (444266072)`, `AlphaStyle - GA4 (399539189)`, `GoReport - GA4 (390203949)`), усі три з `preflight.ok = true`.

### Як зроблено (ключові рішення)

- **AISeoTools не змінювався.** Примітив `ReportingNinjaClient.list_connections()` уже публічний; бракувало саме шару матчингу, а він app-side. Нульовий ризик для зовнішнього репозиторію та його contract-snapshot.
- **Матчинг винесено в `app/integrations/discovery.py`** — без БД і без FastAPI, тому покривається юнітами без стенду. GSC матчиться за нормалізованим доменом із пріоритетом `sc-domain:` над URL-prefix; GA4 — трьома щаблями: аліас → домен, вшитий у назву акаунта → схожість рядків.
- **Хардкод-таблиця на 11 доменів більше не єдиний шлях.** Ключове відкриття: якщо нормалізувати назву (зрізати `(123456)` і токен `GA4`) і додати сигнал «входження коротшого рядка в довший», евристика **виводить кожен запис тієї таблиці сама** — 9 як `exact`, 2 як `strong`. Це закріплено параметризованим тестом по всій таблиці, тож таблиця лишилась тільки як override.
- **`decisive` замість голого score.** Пряма ознака (явний аліас або назва, що резолвиться в домен клієнта) застосовується попри нічию за схожістю. Без цього живий кейс `19 Cleveland` / `19 Cleveland - GA4` щоразу питав би оператора, хоча відповідь задекларована.
- **Автозапис консервативний:** лише `exact`/`strong` і лише за відриву ≥ 0.15 від наступного кандидата. Дублікати property (типова ситуація) ніколи не підключаються навмання — це вибір оператора.
- **Merge, а не replace.** `upsert_integration` затирає `config_json` цілком, тож ендпоінт зливає конфіг вручну — інакше повторне підключення знесло б операторський `unit_budget`/`csv_dir`.
- **Другого ендпоінта не знадобилось:** кандидат несе готовий `integration_config`, тож picker застосовує його наявним `PUT`. `syncModal.openSelection` уже приймав `provider`, тож `sync-modal.js` теж не довелося чіпати.
- Головні файли: `app/integrations/discovery.py` — матчер; `app/api/discovery.py` — ендпоінт, merge, preflight; `frontend/js/dashboard.js` — discovery-first потік і picker.

**Відхилення від плану:** busy-стан планувався на самому рядку картки, але діалог відкривається одразу зі станом «Searching…», тож окремий busy у рядку виявився зайвим і додавав би відновлення стану після закриття. `sync-modal.js` не змінювався (преселект уже підтримувався).

### Контрактні зміни (Contract changes)

Адитивно, нічого не видалено (діф OpenAPI-снапшоту — +199 рядків, 0 видалень):
- новий `POST /api/v1/clients/{client_id}/integrations/{provider}/discover`;
- нові DTO в `app/schemas/api.py`: `IntegrationDiscoverRequest`, `IntegrationCandidateDTO`, `IntegrationDiscoveryDTO`;
- `frontend/js/api.js`: нова `discoverIntegration()`, `updateIntegration()` отримав опційний `sourceMode` (дефолт `"auto"` — стара поведінка збережена).

### Як перевірити (verification)

```bash
docker compose exec api poetry run ruff check .
docker compose exec api poetry run mypy app
docker compose exec api poetry run pytest -q
```

Живий сценарій:

```bash
docker compose exec api curl -s -X POST \
  http://localhost:6524/api/v1/clients/<id>/integrations/reporting_ninja/discover \
  -H 'Content-Type: application/json' -d '{"apply": false}'
```

В UI: на картці клієнта без інтеграції тиснути **Connect** у рядку «Reporting Ninja / GA4» — має з'явитись звіт зі знайденим акаунтом, `verification: passed` і кнопка **Collect data now**. На рядку «Search Console» для домену, якого немає в Ninja, — чесне «No Search Console account matches …» плюс **Enter manually**.

### Тести

- `tests/unit/integrations/test_discovery.py` (29): пріоритет `sc-domain:`; форма конфігу збігається з імпортером; **параметризований прогін по всій аліас-таблиці** (доказ, що евристика її заміняє); аліас б'є однаково схожого сусіда; два однакові кандидати ніколи не автозастосовуються; нульова схожість усе одно дає список; ahrefs/semrush не роблять мережевих викликів; текст винятку не витікає в UI; кеш `list_connections`.
- `tests/unit/api/test_discovery_api.py` (10): 404; 400 на machine-only провайдера; merge зберігає `unit_budget`/`csv_dir`; `needs_choice` і dry run не пишуть у БД; `already_configured` не чіпає наявний рядок; збій провайдера — стан, а не 500; у відповіді немає секретів.
- `tests/unit/importers/test_integration_sync.py` — лишився зеленим **без правок**, як і планувалось.
- `tests/e2e/frontend/frontend-smoke.cjs` — оновлено під новий потік (поля ручного вводу приховані, доки є кандидати).

Разом: `1085 passed, 1 skipped`.

### Follow-ups / борг

- **Bulk «Auto-connect all»** по всіх клієнтах — матчер готовий, лишиться цикл і агрегований звіт.
- **Верифікація GA4 через `hostName`** — стовідсоткова певність ціною ще одного платного запиту.
- **Кнопка «Test connection»** для вже підключених інтеграцій: `POST .../preflight` існує з task-06, але фронт його не викликає ніде.
- **GSC-прогалини не закриваються автоматично:** `alphastyle.store`, `laxton.se`, `goreport.com` не мають Search Console property в Reporting Ninja — це дані, а не код. UI тепер каже про це прямо.
- **Докстрінг `tests/unit/api/test_openapi_snapshot.py` застарів:** радить `python -m tests.unit.api.test_openapi_snapshot`, але `tests/unit` не пакет; робоча форма — `python tests/unit/api/test_openapi_snapshot.py`.
- **e2e прогнати не вдалося:** node-харнес потребує `playwright-core`, якого в середовищі немає. Еквівалент перевірено Python-Playwright у контейнері (він і зловив баг із фокусом на прихованому полі).
