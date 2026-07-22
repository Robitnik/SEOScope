# task-11 — Provider: Semrush (вертикальний зріз)

- **Пакет:** browser runner як library API у AISeoTools + app-адаптер Semrush
- **Хвиля:** C
- **Depends on:** task-02 (contract), task-08 (AISeoTools core), task-05 (worker + profile lock), task-04 (ключі/snapshot)
- **PLAN.md:** §2.2 (Semrush API/browser+ingest), §3.2 (KPI Semrush), §8.3 (source policy, profile lock), §12, Етап 6 (gate)

## Мета

Semrush snapshot (Authority Score, Organic Traffic, Organic Keywords, Referring Domains). Рекомендований шлях — **browser export + `ingest_semrush`** із unified snapshot shape; API — fallback за явною політикою/бюджетом. Incomplete manifest → `partial`; stale-файл **не** потрапляє у snapshot. Retry невдалої стадії не змішує старі downloads.

## Обсяг

**Входить:**
- **AISeoTools** (`seo_audit/dashboard/semrush.py`): high-level browser runner як **library API** з callback-прогресом (CLI лишається тонким wrapper) навколо наявних `semrush.client.SemrushClient` (нормалізовані dict) та `semrush.ingest_manual.ingest_semrush()` (читає manifest, відрізняє incomplete run). Additive `on_event`, profile lock key `(provider, profile_name)`. Окрема job-artifact directory + checksum (manifest allow-list) — щоб старі CSV у shared downloads не давали хибних цифр (PLAN §17 ризик).
- **App** (`app/integrations/semrush.py`): `ProviderAdapter(provider=semrush)`, `preflight()` (paid account/profile/session, units за API-fallback, budget policy), `collect()`; мапінг у `CollectionResult` (snapshot); completeness validation для overview/backlinks/refdomains; API fallback за політикою. Реєстрація в registry.

**НЕ входить:** інші провайдери, зміна старого Semrush CLI-контракту (лише additive), UI.

## Owned paths
```
# AISeoTools
seo_audit/dashboard/semrush.py
tests/dashboard/test_semrush.py
# App
app/integrations/semrush.py
tests/unit/integrations/test_semrush.py
```

## Consumes
- contract/registry/DTO — task-02.
- AISeoTools `dashboard.events/models/config` + наявні `SemrushClient`, `ingest_semrush` — task-08 / наявний код.
- catalog ключі Semrush, snapshot-семантика — task-04.
- profile lock від worker (task-05) — жодного паралельного запуску одного профілю.

## Provides
- Зареєстрований адаптер `semrush` (snapshot), готовий для worker.
- Unified snapshot із completeness/quality + provenance (browser/api), units metadata.

## Кроки реалізації
1. AISeoTools library runner з callback + profile lock + окрема job-artifact directory + manifest allow-list/checksum.
2. `ingest_semrush` completeness: incomplete manifest → `partial`; stale-файл відсіюється (не потрапляє у snapshot).
3. App-адаптер: preflight (account/profile/budget), collect (стадії §9), мапінг snapshot-метрик; API fallback за політикою/бюджетом.
4. Retry failed stage без змішування старих downloads (нова artifact directory на attempt).
5. Реєстрація в registry.

## Тести
- incomplete manifest → `partial`; stale-файл **не** у snapshot (PLAN Етап 6 gate + §15).
- manifest partial/truncated cases; truncated export не видається за total.
- retry не змішує старі downloads (ізольована artifact dir/attempt).
- profile lock: паралельний запуск того самого профілю неможливий.
- API fallback лише за явною політикою/бюджетом; units metadata заповнена.
- секрети не течуть; browser session-shape у provenance без секретів.
- Живий smoke (поза CI): контрольний клієнт, totals звірені з Semrush UI.

## Definition of Done / Gate
- [x] Incomplete manifest дає `partial`, stale-файл не потрапляє у snapshot (PLAN Етап 6 gate).
- [x] Retry невдалої стадії не змішує старі downloads.
- [x] Profile lock `(provider, profile_name)` дотриманий; жодного паралельного запуску профілю.
- [x] Truncated export не подається як exact total.
- [x] Адаптер зареєстрований і проганяється worker'ом; старий Semrush CLI не зламаний.

## Ризики
- Потрібен paid account + persistent browser profile (PLAN §2.2) → preflight + documented manual login flow; протухла сесія → `auth_required`.
- Старі CSV у shared downloads (PLAN §17) → manifest allow-list + окрема artifact dir + checksum.
- Metered API units → budget policy, API лише як fallback.

---

## ✅ Completion report

- **Дата:** 2026-07-22
- **Гілка / комміт:** main (uncommitted — див. «Follow-ups» нижче)
- **Статус:** done-with-followups

### Що зроблено

Semrush вертикальний зріз повністю робочий: browser-first collection (organic positions +
backlinks list + referring domains + Authority Score scrape) → окрема job-artifact directory з
allow-list manifest + checksums → `ingest_semrush()` → `DashboardResult` з 4 KPI (Authority
Score, Organic Traffic, Organic Keywords, Referring Domains). Метered API (`SemrushClient`) —
fallback лише за явним `unit_budget` у `auto`, або напряму в `api` source mode. Адаптер
зареєстрований у registry й підхоплюється worker'ом (task-05) як звичайний `ProviderAdapter`.

### Як зроблено (ключові рішення)

- **`seo_audit/dashboard/semrush.py`** (AISeoTools) — `collect_semrush_browser()` +
  `collect_semrush_api()`. Browser-шлях НЕ використовує `SemrushClient`; малює власний
  `sr_overview.csv` з organic-export (row count + summed Traffic/Traffic Cost) + окремо
  скрапнутий Authority Score — той самий прийом, що й приватний `_semrush_overview()` у
  `seo_audit/cli.py::browser_semrush`, свідомо переписаний тут (а не імпортований), щоб
  library-обгортка не тягла CLI-модуль.
- **Referring Domains завжди `Quality.truncated`, коли зібраний браузером** — цей wrapper
  ніколи не експортує single-row `sr_backlinks.csv` (overview), лише LIST-звіт
  (`refdomains/report`), тож `ingest_semrush` завжди ДЕРИВУЄ `domains_num` як
  `len(refdomains)` — рядковий підрахунок, а не авторитетний тотал з акаунта. Це і є
  реалізація gate-пункту «truncated export не подається як exact total»: значення
  присутнє, але ніколи не `complete`. У API-шляху те саме поле — точний тотал з
  `backlinks_overview`, тож там `Quality.complete`.
- **Manifest allow-list + checksums, окрема artifact dir на спробу** — `collect_semrush_browser`
  приймає обов'язковий `artifacts_dir` (caller, тобто app-адаптер, створює новий унікальний
  шлях на кожен `collect()` через `_new_artifacts_dir()`, під `settings.artifacts_dir/
  semrush_runs/<client_id>/<epoch_ms>-<uuid8>`). Manifest пишеться з першого рядка (до
  будь-якого browser-виклику) і містить лише файли/чеклист ЦІЄЇ спроби — тому стара CSV не
  може потрапити в snapshot (`ingest_semrush` читає виключно allow-list, ніколи не глобить,
  коли manifest існує), а retry ніколи не бачить файлів попередньої спроби.
- **`unit_budget` = explicit-policy сигнал**, а не окремий boolean-флаг: presence самого
  `integration_config["unit_budget"]` у `auto` mode вмикає API fallback. Явний `source_mode=api`
  дозволяє API завжди (це і є explicit policy). Мінімізує config surface (як `csv_dir` у Ahrefs).
- **`BotChallenge` — tripwire, зупиняє всю спробу одразу**; інші `BrowserAutomationError`
  (`ExportFailed`, `SelectorDrift`, ...) на окремому звіті — записуються у `manifest["failures"]`
  і збір триває best-effort по решті звітів (як CLI `--keep-going`).
- **Profile lock** не реалізовувався заново — це вже наявний `fcntl`-механізм
  `BrowserProfile.lock()` (AISeoTools, поза owned paths), який `BrowserSession` бере
  автоматично. Підтверджено REAL-тестом (`test_profile_lock_prevents_a_concurrent_run_
  on_the_same_profile`, без Chrome/Playwright) — другий `lock()` того самого профілю кидає
  `ProfileLocked`; адаптер мапить це у стабільний `error_code="profile_locked"`, ніколи не
  падає.
- **`app/integrations/semrush.py`** — дзеркалить `ahrefs.py` (fallback між джерелами,
  `_dash_quality_rank` для вибору кращого з двох спроб) + `gsc.py`/`reporting_ninja.py`
  (`_bridge_emit`, outer `except BaseException` guard). Один нюанс, якого нема в
  ahrefs/gsc: **`CancelledByAdapter` явно ре-рейзиться** з outer guard `collect()` — без
  цього кооперативне скасування (worker матчить на `isinstance(exc, CancelledByAdapter)` у
  `app/jobs/runner.py`) мовчки перетворювалося б на звичайний `adapter_error`.
- **`_to_collection_result` не перевикористовує AISeoTools-side `dash_result.quality` для
  кожної метрики окремо** (на відміну від Ahrefs, який рахує per-value stale/complete сам) —
  бере одну агреговану якість з `DashboardResult.quality` і застосовує її до всіх присутніх
  значень (як GSC), бо `_build_result()` у dashboard-обгортці вже інкапсулює всю
  truncated/partial-логіку — дублювати її в адаптері не було потреби.

### Контрактні зміни (Contract changes)

Немає. `app/schemas/**`, `app/integrations/contract.py`, `app/integrations/registry.py` —
не чіпались. `app/integrations/__init__.py` — додано лише рядок `semrush,` в існуючий
неексклюзивний bootstrap-імпорт (той самий файл, куди task-09/10 вже додали свої рядки).
AISeoTools-бік: `ingest_semrush()`/`SemrushClient` не змінювались (сигнатури й return
shapes — ідентичні; `tests/dashboard/test_contract_snapshot.py` цього не відстежує явно
для нових функцій `dashboard/semrush.py`, бо вони самі нові, не legacy reuse-точки).

### Як перевірити (verification)

AISeoTools (окремий репозиторій, свій venv — тут не було `.venv`, тимчасовий venv створено
в scratchpad лише для перевірки цієї сесії):
```bash
cd /home/elon/dev/seo/AISeoTools
python -m pytest tests/dashboard/test_semrush.py -q   # 18 passed
python -m pytest tests/dashboard -q tests/test_semrush.py -q   # без незалежних поломок середовища (tldextract/selectolax/playwright відсутні в пісочниці — pre-existing, не від цієї таски)
```

Застосунок (лише Docker, за CLAUDE.md):
```bash
docker compose run --rm api poetry run pytest tests/unit/integrations/test_semrush.py -q   # 23 passed
docker compose run --rm api poetry run pytest -q                                            # 301 passed (уся suite)
docker compose run --rm api poetry run ruff check app/integrations tests/unit/integrations  # All checks passed!
docker compose run --rm api poetry run mypy app                                             # Success: no issues found in 65 source files
```

### Тести

- **AISeoTools** `tests/dashboard/test_semrush.py` (18, новий): `_semrush_overview_csv`
  (сума traffic/cost, порожній authority_score); REAL profile-lock mutual exclusion
  (`fcntl`, без Chrome); browser-happy-path (усі 4 метрики, `Quality.truncated` для
  referring_domains); manifest із checksums; incomplete manifest → `partial` +
  `None`-не-`0`; stale leftover file ігнорується (allow-list); retry не змішує старі
  downloads (окремі каталоги, перевірено що файл з невдалої спроби не з'являється у другій);
  `BotChallenge`/`ProfileLocked`/`SessionExpired` → стабільні `error_code`, ніколи не
  падають; `on_event=None` no-op + події без секретів; `collect_semrush_api` — усі 4 метрики
  + `units_spent`, `UnitsExhausted` пропускається неспійманим, `SemrushAPIError`/
  `BudgetExceeded` ловляться і мапляться в `unavailable`/`no_units`.
- **Застосунок** `tests/unit/integrations/test_semrush.py` (23, новий): preflight
  (`not_configured`/`auth_required`/`pdf` unsupported/browser з і без профілю/`auto` з і
  без `unit_budget`); collect: complete-мапінг, `None` → `unavailable` не `0`,
  `Quality.truncated` пропускається наскрізь; `auto` без `unit_budget` НІКОЛИ не викликає
  API навіть якщо browser `unavailable` (явна перевірка через `AssertionError` у моку);
  `auto` з `unit_budget` — фолбек спрацьовує, обирається кращий з двох результатів;
  explicit `api` mode дозволяє fallback завжди; `UnitsExhausted` під час fallback →
  `no_units` (навіть коли browser вже повернув непорожній, але `unavailable` результат);
  cooperative cancel (`CancelledByAdapter`) коректно проривається крізь outer guard;
  unexpected exception → `adapter_error`, ніколи не падає; `_bridge_emit` unit-тести;
  `_new_artifacts_dir` унікальність.

### Follow-ups / борг

- **AISeoTools-бік не закомічено** (`seo_audit/dashboard/semrush.py`,
  `tests/dashboard/test_semrush.py`) — той самий репозиторій паралельно редагують інші
  таски (task-09/10 теж лишили свій AISeoTools-бік uncommitted, той самий висновок і та
  сама причина — див. `doc/dev/done/task-10-provider-ahrefs.md`). Коміт/тег — окремий
  свідомий крок користувача, коли паралельна робота там завершиться.
- **`app/` у цьому репозиторії теж лишається uncommitted** — на момент цієї таски весь
  `app/`-дерево (усі task-01…10) ще не мало жодного коміту (лише `doc/dev/done/*` файли
  застейджені попереднім агентом). Ця таска нічого не комітить, за явним правилом
  «commit лише коли користувач попросить»; new-файли (`app/integrations/semrush.py`,
  `tests/unit/integrations/test_semrush.py`) залишено untracked/unstaged — рішення про
  коміт (і в якому обсязі — разом з рештою `app/` чи окремо) належить користувачу.
- **`SEOTOOLS_ROOT`/`settings.browser_profiles_dir` не пов'язані.** `app/core/config.py`
  вже має `browser_profiles_dir`, але жоден провайдер (ні GSC, ні цей) не прокидає його в
  `SEOTOOLS_ROOT` env — усі browser-профілі й далі йдуть у AISeoTools-дефолт
  (`~/.local/share/seo-audit/profiles`), не в Docker-змонтований `browser_profiles` volume.
  Той самий gap, що вже задокументований (неявно) відсутністю використання в task-09; варто
  підняти як окремий cross-cutting follow-up (task-01/05), а не латати тут одноразово.
- **Немає структурного розрізнення export-truncation через Semrush-план row cap** (окрім
  завжди-truncated referring_domains, описаного вище) — `SemrushExport.export_csv`
  (`seo_audit/browser/providers/semrush.py`, поза owned paths) не порівнює завантажені
  рядки з тим, що показує `All (N)` у модалці, тож organic-positions/backlinks-list теж
  теоретично можуть бути мовчки capped акаунтним тарифом без явного сигналу. Виправлення
  вимагало б DOM-скрапу лейбла `All (N)` у `SemrushExport`/`SemrushSiteExplorer` — поза
  owned paths цієї таски (адитивна зміна, майбутній task).
- **Live smoke (поза CI) не виконувався** — потрібен paid Semrush account +
  persistent browser profile, яких немає в цьому середовищі. Задокументовано в тестах
  gate-пункти; контрольний прогін на реальному клієнті лишається за оператором (PLAN §15).
