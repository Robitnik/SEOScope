# task-21 — Preflight сесій, коректний `auth_required` і пауза/авто-продовження доріжки

- **Пакет:** нова стадія перевірки сесій, виправлення втраченого `auth_required`, пауза доріжки з авто-резюмом після логіну
- **Хвиля:** F (auth flow)
- **Depends on:** task-20 (`check_session` / `start_login`)
- **Blocks:** task-22 (UI спирається на ці стани й події)
- **PLAN.md:** §9.1/§9.2 (доріжки, ізоляція, нуль падінь), §12 (`last successful` окремо від `last attempt`), §7 (message ≠ джерело логіки), §17

## Мета

Зробити так, щоб мертва сесія **називалася своїм ім'ям і зупиняла лише свою доріжку**. Сьогодні поведінка неправильна у двох місцях, і саме через це Semrush виглядає як «не працює взагалі, хоча дані там є».

## Два підтверджені дефекти

**1. `auth_required` губиться → показується `unavailable`.**
Сигнал є і доживає майже до кінця: AISeoTools мапить `SessionExpired → error_code="auth_required"` (`seo_audit/dashboard/semrush.py:118`), адаптер його пробрасує (`app/integrations/semrush.py:351`), runner кладе в клітинку (`app/jobs/runner.py:277`). Але **`ProviderSnapshot` не має колонки `error_code`**, а картка/`providers-status` виводять стан зі **snapshot.quality** — тому назовні йде `unavailable` («даних немає»), хоча правда — «сесія протухла, увійди». Оператор бачить рівно протилежне до дійсності. Причина в БД зараз живе **лише текстом у `warnings_json`**, що прямо суперечить PLAN §7 («повідомлення не є джерелом логіки»).

**2. Мертва сесія перевіряється 13 разів.**
Preflight уміє перевіряти лише **наявність** профілю, не живість сесії. Тому доріжка Semrush відкрила браузер і впала на логін-сторінку **для кожного з 13 клієнтів** поспіль — 13 марних browser-раундтрипів замість однієї перевірки.

## Обсяг

**Входить:**
- **Нова стадія `Check session`** на початку доріжки (перед першою клітинкою): `check_session(provider)` з task-20, **один раз на провайдера**, а не на клієнта. Емітить `ProgressEvent`, тож модалка бачить прогрес.
- **Коректне поверхнення `auth_required`:** стан провайдера в `dashboard`/`providers-status` береться з **останньої спроби (клітинки)**, а не лише зі snapshot — це вже вимога PLAN §12 («`last successful` окремо від `last attempt`»). Дані показуються з останнього успішного snapshot, а поруч — чесний бейдж `auth_required`.
- **Пауза доріжки замість провалу:** сесія мертва → доріжка стає `awaiting_auth` (**новий, нетермінальний** `CellStatus` — контрактна зміна), її клітинки лишаються в черзі, **інші доріжки збирають далі** (§9.1).
- **Звільнення profile-lock на час очікування** — інакше логін не зможе взяти той самий профіль. Критична деталь.
- **Lease під час паузи:** очікування **не повинно** призводити до expiry→recovery→`failed`. Пауза або зупиняє годинник lease, або тримає його явним heartbeat'ом. Плюс **таймаут очікування** (напр. 15 хв) → клітинки чесно закриваються `auth_required`, без вічного висіння.
- **Авто-продовження:** після успішного логіну доріжка сама продовжує **з того самого місця**, без перезапуску job і без повторного збору вже успішних клітинок.

**НЕ входить:** сам механізм логіну (task-20), UI (task-22).

## Owned paths

```text
app/jobs/lane.py, app/jobs/dispatcher.py, app/jobs/runner.py, app/jobs/recovery.py
app/schemas/enums.py        # CellStatus.awaiting_auth (контрактна зміна)
app/api/dashboard.py, app/api/providers.py   # стан з останньої спроби
tests/unit/jobs/test_session_preflight.py, test_auth_pause_resume.py
tests/unit/api/test_auth_required_surfacing.py
```

> **Контрактна зміна.** `CellStatus.awaiting_auth` — адитивне значення; оновити contract- та OpenAPI-snapshot, інакше тести task-02/06 впадуть.

## Тести

- Мертва сесія перевіряється **рівно один раз на провайдера**, а не N разів на клієнтів (регресія на 13 раундтрипів).
- `SessionExpired` → назовні `auth_required`, **не** `unavailable`; структурований код, не парсинг тексту warning.
- Останні успішні дані **лишаються видимими** поруч із бейджем `auth_required` (§12).
- Пауза доріжки: інші провайдери продовжують збір; profile-lock звільнено.
- Lease під час паузи **не** призводить до `failed_interrupted`; таймаут закриває чесно.
- Після логіну доріжка продовжує з місця зупинки; успішні клітинки не перезбираються.
- `not_configured` / `auth_required` / `unavailable` / `failed` лишаються чотирма різними станами (§9.2).

## Definition of Done / Gate

- [x] Semrush із мертвою сесією показується як **`auth_required`**, а не «немає даних».
- [x] Перевірка сесії — одна на провайдера; марних browser-раундтрипів немає.
- [x] Мертва сесія ставить на паузу **лише свою** доріжку; решта збирає далі.
- [x] Profile-lock звільнено на час очікування; lease не протухає в паузі; є таймаут.
- [x] Після логіну збір продовжується автоматично, без повторного збору успішного.
- [x] Contract/OpenAPI snapshots оновлені; тести task-02/06 зелені.

## Ризики

- **Пауза + lease + lock** — найтонше місце: помилка тут дає або «вічну» running job (проти §9.2), або втрачений профіль. Покрити тестами до реалізації.
- Спокуса лишити рішення на тексті warning — заборонено (§7): лише структурований `error_code`.

---

## ✅ Completion report

- **Дата:** 2026-07-22
- **Гілка / комміт:** feat/task-20-browser-session-bridge (working tree; не закомічено)
- **Статус:** done

### Що зроблено

- Нова стадія **check-session** у `run_cell` (`app/jobs/runner.py`): для будь-якого browser-провайдера (`BROWSER_PROVIDERS` з task-20) сесія перевіряється дешевим `check_session(provider)` до будь-якого `preflight()`/`collect()`. Якщо сесія не `alive` — клітинка одразу термінується в новий **`CellStatus.awaiting_auth`**, без відкриття браузера.
- Той самий `awaiting_auth` тепер настає і з двох інших джерел сигналу «сесія протухла», а не лише з нового check_session-гейту: `adapter.preflight()` з `reason_code == "auth_required"`, і `collect()`-результат з `error_code == "auth_required"` (це вже реальний шлях `SemrushAdapter`/`SessionExpired` — раніше він мапився в `partial` і губився).
- **Доріжка (`app/jobs/lane.py`)** більше не «пропускає» повз `awaiting_auth`-клітинку до наступного клієнта: побачивши цей статус, вона перестає клеймити нові клітинки й заходить у власний poll-цикл `_await_auth_and_resume`, що чекає на `alive` (тоді одна ця клітинка повертається в `queued` і доріжка триває природним чином — без жодного «рестарту з нуля») або на таймаут `DEFAULT_AUTH_PAUSE_TIMEOUT_SECONDS = 15 * 60` (тоді ця клітинка **і весь інший `queued`-бэклог цього провайдера** чесно закриваються `failed`/`auth_required` одним махом, замість того щоб кожна клітинка по черзі чекала свій власний 15-хвилинний таймаут).
- **`check_session` викликається рівно один раз на провайдера**, а не на клієнта: `lane.py` огортає callable у `_MemoizedCheckSession` — перший виклик кешується на все «проходження» доріжки; кеш скидається лише після паузи (resume або timeout), щоб наступна клітинка отримала свіжий результат. Реальний `collect()`-рівневий сигнал (для mid-batch протухання) працює незалежно від цього кешу.
- **Profile-lock і lease під час паузи** не потребували нового коду: `CellRepository.acquire_profile_lock`/`expired_cells` дивляться лише на `status == running`; щойно клітинка йде в `awaiting_auth`, вона автоматично перестає тримати і лок, і lease-годинник (варіант «пауза зупиняє годинник lease» з тексту задачі).
- **Startup recovery** (`app/jobs/recovery.py`) вимагав нового кроку: `awaiting_auth`-клітинка, застрягла після рестарту воркера (жодна доріжка вже не поллить її), тепер безумовно повертається в `queued` — інакше вона б лишилась «вічно» на паузі. `RecoveryReport.requeued_awaiting_auth` — нове поле.
- **Дашборд/providers-status** (`app/api/dashboard.py`, `app/api/providers.py`) виправлено у двох місцях: (1) гілка «немає успішного snapshot» тепер розпізнає і `awaiting_auth`, і `failed`+`error_code=="auth_required"` як `auth_required`, а не `unavailable`; (2) гілка «є успішний snapshot» — раніше повністю ігнорувала `latest_cell` і завжди повертала `state` зі quality снапшоту — тепер, якщо остання спроба показує `auth_required`, `state` перемикається на `auth_required`, а самі метрики/quality лишаються від останнього успішного snapshot (§12).

### Як зроблено (ключові рішення)

- **Чому дефолт `check_session` — заглушка, а не реальний `browser_session.check_session`.** `run_cell`/`run_lane` викликаються напряму наявними тестами (`test_runner.py`, `test_dispatcher_concurrency.py`) з `Provider.ahrefs`/`semrush` без теплого Chrome у пісочниці — реальний примітив побачив би там суцільний `expired` і перетворив би всю стару test suite на хибні паузи. Тож дефолт — `app.jobs.runner.assume_session_alive` (завжди `alive`); реальний check_session підключається **лише в одному місці** — `app/jobs/worker.py`'s `Dispatcher(...)`.
- **Чому «закрити весь бэклог» на таймауті, а не чекати по колу.** Просте рішення «кожна клітинка сама паузиться на 15 хв» дало б 13-й клієнту фактичний простій у ~3 години (13×15хв) перш ніж хтось взагалі побачив стан `failed`. Замість цього одна пауза на провайдера й один таймаут закриває решту черги одразу.
- Головні файли: `app/jobs/runner.py` (check_session-гейт + auth_required-мапінг), `app/jobs/lane.py` (`_MemoizedCheckSession`, `_await_auth_and_resume`), `app/jobs/recovery.py` (requeue awaiting_auth), `app/jobs/dispatcher.py`/`app/jobs/worker.py` (проводка параметрів), `app/api/dashboard.py`/`app/api/providers.py` (`_auth_state_from_cell`).
- Відхилення від плану: жодних істотних — реалізовано точно за узгодженим планом (`/home/elon/.claude/plans/iterative-orbiting-music.md`).

### Контрактні зміни (Contract changes)

- **`CellStatus.awaiting_auth`** (адитивне значення, `app/schemas/enums.py`) — нетерміналний статус клітинки. Пропагується у OpenAPI/contract snapshots без ручних правок DTO: `ProviderCardState` (`app/schemas/api.py`) уже містив `"auth_required"`, а `CellProgressDTO.status` типізований напряму через `CellStatus`.
- Дві нові read-only методи в **`app/db/repositories/cells.py`** (файл поза owned paths цієї таски, як і `app/jobs/worker.py` нижче — обидва торкнуті мінімально й навмисно): `queued_cells_for_provider(provider)` і `awaiting_auth_cells()`. Патерн ідентичний уже наявним «Added by task-05/06» секціям у цьому ж файлі.
- **`app/jobs/worker.py`** (task-05, поза owned paths): одна проводка `check_session=check_session` у виклик `Dispatcher(...)` — без неї фіча лишилась би неактивною в реальному воркері.
- **`RecoveryReport.requeued_awaiting_auth: int = 0`** — нове поле (backward-compatible default).
- Оновлено `tests/unit/api/data/openapi_snapshot.json` та `tests/unit/schemas/data/contract_snapshot.json` (regenerated); `tests/unit/schemas/test_enums.py`'s exhaustive `CellStatus` set доповнено `"awaiting_auth"` (це саме контракт task-02, явно передбачений текстом задачі: «оновити contract- та OpenAPI-snapshot, інакше тести task-02/06 впадуть»).

### Як перевірити (verification)

```bash
docker compose run --rm api poetry run pytest -q                                    # 471 passed, 1 skipped, 1 xfailed
docker compose run --rm api poetry run ruff check app tests                         # All checks passed!
docker compose run --rm api poetry run mypy app                                     # Success: no issues found in 74 source files
docker compose run --rm api poetry run pytest tests/unit/jobs/test_session_preflight.py tests/unit/jobs/test_auth_pause_resume.py tests/unit/api/test_auth_required_surfacing.py -q
```

### Тести

- `tests/unit/jobs/test_session_preflight.py` — check_session викликається рівно один раз для здорової 3-клієнтної доріжки; мертва сесія жодного разу не відкриває fake-адаптерів `preflight()`/`collect()`; `reporting_ninja` (немає browser-сесії) ніколи не викликає check_session.
- `tests/unit/jobs/test_auth_pause_resume.py` — resume після логіну продовжує з того самого місця без повторного збору успішної клітинки; profile-lock вільний під час паузи; таймаут чесно закриває весь бэклог провайдера; інший провайдер (доріжка) продовжує збір під час паузи; startup recovery повертає застряглу `awaiting_auth`-клітинку в `queued`.
- `tests/unit/api/test_auth_required_surfacing.py` — dashboard/`providers/status` показують `auth_required` замість `unavailable` за відсутності snapshot; останній успішний snapshot лишається видимим (metrics/quality незмінні) поруч із бейджем `auth_required`, коли остання спроба протухла.

### Follow-ups / борг

- `JobStatus` не має власного «paused»-значення; `app/db/repositories/jobs.py` (task-05, не займали) не трактує `awaiting_auth` як «running-подібний» стан для агрегації — одиночна клітинка-джоба на паузі показує job-level `queued`, хоч насправді чекає логіну. Косметично, не впливає на те, що реально бачить дашборд (cell-level стан коректний). `TODO(task-22)`, бо саме ця таска споживає ці стани в UI.
- Проєкт мав велику кількість незакомічених змін (весь `app/`, `tests/`, `Dockerfile`, etc. — виявилось untracked ще до початку цієї роботи); коміт цієї таски свідомо не робився без прямого запиту користувача.
