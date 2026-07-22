# task-20 — Browser session bridge: доступ до теплого Chrome, перевірка живості, тригер логіну

- **Пакет:** інфраструктура браузерних сесій — мережевий міст до host Chrome, session-liveness примітив, безпечний тригер логіну
- **Хвиля:** F (auth flow)
- **Depends on:** —
- **Blocks:** task-21 (preflight сесій), task-22 (кнопка «Увійти»)
- **PLAN.md:** §8.3 (browser lock/profiles), §13 (безпека — жодних довільних команд), §17 (ризик «browser auth протух»), §19.4
- **Репозиторії:** застосунок + AISeoTools

## Мета

Зробити фізично можливим те, що зараз неможливе: **щоб оператор побачив вікно логіну й увійшов**. Сьогодні збір Semrush піднімає браузер під Xvfb усередині контейнера з примонтованим профілем без сесії, впирається в логін-сторінку — а це вікно оператор не бачить і взаємодіяти з ним не може.

## Діагноз (перевірено інструментально)

- Chromium **немає** в образі; збір працює лише тому, що AISeoTools вміє `connect_over_cdp` до **теплого Chrome** (`seo_audit/browser/warm.py`).
- З контейнера **не відповідає жоден** CDP-ендпоінт хоста (`host.docker.internal`, `172.17.0.1`, `127.0.0.1` → no answer); у `compose.yaml` немає ні `network_mode: host`, ні `extra_hosts`.
- `warm.py:111` вважає теплий Chrome живим лише якщо `_pid_alive(state.pid)` — це **PID хоста**, беззмістовний у PID-неймспейсі контейнера.
- Xvfb у образі є, але його вікно невидиме для людини.
- Це матеріалізація відкритого follow-up із `doc/dev/done/task-01-foundation-infra.md` («Playwright/Chromium base image ще не зроблено»).

## Обсяг

**Входить:**

1. **Мережевий міст до теплого Chrome (рекомендований шлях).**
   - `extra_hosts: ["host.docker.internal:host-gateway"]` для `api`/`worker`; CDP-порт Chrome слухає інтерфейс, доступний контейнеру;
   - `CHROME_CDP_URL` як налаштування (`Settings`) з дефолтом на `host.docker.internal`; **жодних host-шляхів у committed compose** (PLAN §14).
2. **AISeoTools (адитивно):** режим «віддалений теплий Chrome» — коли CDP приходить ззовні, перевірка живості спирається **лише на `_cdp_alive(cdp_url)`**, а не на `_pid_alive` (PID чужого неймспейсу). Наявна поведінка за замовчуванням не змінюється (PLAN §8.1).
3. **Session-liveness примітив:** обгортка над наявним `seo_audit/browser/auth.py:244` (`«Used by browser-login --check and by preflights»`) — `check_session(provider) -> alive | expired | no_profile`, дешева, без повного збору. Це фундамент для task-21.
4. **Безпечний тригер логіну:** серверна дія `start_login(provider)` — **фіксована**, провайдер із allowlist-enum, **жодних довільних команд/флагів/шляхів з UI** (PLAN §13). Прикріплюється по CDP до теплого Chrome і відкриває сторінку логіну у **видимому** вікні; повертає `login_session_id` для опитування статусу.
5. **Документований запасний шлях:** якщо теплого Chrome немає — зрозуміла інструкція з готовою командою (не «щось пішло не так»).

**НЕ входить:** preflight-стадія в job engine (task-21), UI (task-22), noVNC (альтернатива нижче).

## Owned paths

```text
compose.yaml, compose.override.example.yaml, Dockerfile   # успадковано від task-01 — лише додатки
app/core/config.py                                        # CHROME_CDP_URL та таймаути
app/integrations/browser_session.py                       # новий: check_session / start_login
AISeoTools: seo_audit/browser/warm.py, auth.py            # адитивний remote-CDP режим
tests/unit/integrations/test_browser_session.py
```

## Альтернатива (якщо host Chrome не підходить)

**noVNC у контейнер**: справжній Chromium у образі + VNC поверх Xvfb, логін прямо у вкладці дашборду. Самодостатньо й працює віддалено, але дорожче: VNC-стек, доступ, окремий контур безпеки. Обрано **міст до host Chrome** як найдешевший шлях до наміру «сервер відкриває вікно, я входжу».

## Definition of Done / Gate

- [x] З контейнера `check_session(provider)` коректно розрізняє `alive` / `expired` / `no_profile` **без** повного збору.
- [x] `start_login(provider)` відкриває **видиме** оператору вікно логіну; після входу `check_session` повертає `alive`.
- [x] Провайдер береться лише з allowlist; довільні команди/шляхи з UI неможливі (PLAN §13).
- [x] Немає абсолютних host-шляхів у committed compose; усе через `Settings`/env.
- [x] AISeoTools-зміни адитивні, стара test suite зелена, дефолтна поведінка не змінена.
- [x] Якщо теплого Chrome немає — зрозуміла дія для оператора, а не тупикова помилка.

## Ризики

- **Мережевий доступ контейнер→хост** різниться між Linux/Mac/CI. Тримати як налаштування з явним дефолтом і чесною діагностикою, а не «магією».
- CDP-порт — це повний контроль над браузером: слухати лише локальний інтерфейс, ніколи не публікувати назовні.
- Спокуса дозволити UI передавати CDP-URL/шлях профілю — **заборонено** (PLAN §13): лише ім'я провайдера з enum.

---

## ✅ Completion report

- **Дата:** 2026-07-22
- **Гілка / комміт:** `feat/task-20-browser-session-bridge` (working; AISeoTools checkout @ `0ec56fcc4d1e7692fb2f2183fb82bcc30e2c980a` + uncommitted additive diff described below)
- **Статус:** done

### Що зроблено

Контейнер `api`/`worker` тепер бачить і використовує теплий Chrome, запущений на хості:

- `check_session(provider)` — дешева перевірка `alive` / `expired` / `no_profile`, без повного збору.
- `start_login(provider)` — прикріплюється по CDP до вже запущеного теплого Chrome і відкриває сторінку логіну в його **видимому** (на хості) вікні; повертає `login_session_id`, який `login_status(id)` дозволяє опитувати.
- Якщо теплого Chrome нема — `NoWarmChrome` називає точну команду (`seo-audit browser-up --provider …`), а не тупикову помилку.

### Як зроблено (ключові рішення)

- **Мережевий міст — `network_mode: host`, не `extra_hosts`.** Інструментально підтверджено: AISeoTools' `IsolatedChrome` слухає CDP лише на `127.0.0.1` (`seo_audit/browser/chrome.py:42,146` — поза owned paths цієї таски, не змінювався). `extra_hosts: host.docker.internal:host-gateway` дає контейнеру ЛИШЕ адресу docker-мосту хоста (напр. `172.17.0.1`); сокет, прив'язаний до `127.0.0.1`, з'єднання на цю адресу не приймає — це фізичне обмеження, не конфігураційне. Тому `api`/`worker` в `compose.yaml` тепер піднімаються з `network_mode: host`: контейнерний `127.0.0.1` буквально є хостовим loopback. Свідомо обраний компроміс (підтверджено з користувачем): Linux-only (ціль проєкту), `ports:`/`API_PORT` більше не діють (uvicorn на `0.0.0.0:8000` — це напряму хостовий порт 8000), `compose.override.example.yaml` більше не показує приклад з `ports:` (несумісно з `network_mode: host`).
- **AISeoTools (адитивно), тільки `seo_audit/browser/warm.py`:** новий `SEOTOOLS_REMOTE_CDP_HOST` env — коли встановлено, `warm_cdp_url` (а через нього `status`, і все, що на ній стоїть) перестає довіряти `_pid_alive` (це PID хоста, безглуздий у PID-неймспейсі контейнера) і покладається лише на `_cdp_alive`; заразом переписує знайдений `cdp_url` на цей host. Без змінної — поведінка ідентична попередній (перевірено: стара `test_browser_warm.py` + вся AISeoTools test suite, 1482 тести, зелені). `auth.py` не чіпався — виявилось непотрібним: `check_session`/`start_login` в новому модулі напряму читають `seo_audit.browser.warm`/`profile`, не через `BrowserSession` (той у `_attach_warm`-failure падає в `_launch_ephemeral()`, якого в контейнері без Chrome-бінарника бути не може).
- **Головні файли:**
  - [app/integrations/browser_session.py](app/integrations/browser_session.py) — `check_session`, `start_login`, `login_status`, `SessionState`, `NoWarmChrome`, `BROWSER_PROVIDERS` allowlist (`ahrefs`/`semrush`/`gsc`; `reporting_ninja` явно відхиляється — авторизується API-ключем, профілю браузера не має).
  - [app/core/config.py](app/core/config.py#L26) — `chrome_cdp_host` (документаційний, дефолт `127.0.0.1`), `browser_login_timeout_seconds`.
  - `AISeoTools/seo_audit/browser/warm.py` — `_remote_host()`, `_rewrite_remote()`, `warm_cdp_url` тепер remote-aware.
  - [compose.yaml](compose.yaml) — `network_mode: host` + `CHROME_CDP_HOST`/`SEOTOOLS_REMOTE_CDP_HOST` env для `api` і `worker`.
- **Відхилення від плану:** пункт «мережевий міст» у плані називав `extra_hosts` рекомендованим шляхом; замінено на `network_mode: host` з наведеної вище фізичної причини (узгоджено з користувачем перед реалізацією).

### Контрактні зміни (Contract changes)

Немає публічного API/DB-контракту — `app/integrations/browser_session.py` це нова, самодостатня точка входу для task-21/22.

### Як перевірити (verification)

```bash
docker compose run --rm api poetry run pytest tests/unit/integrations/test_browser_session.py -q
docker compose run --rm api poetry run pytest -q               # весь застосунок — 460 passed, 1 skipped, 1 xfailed
docker compose run --rm api poetry run ruff check app/integrations/browser_session.py app/core/config.py
docker compose run --rm api poetry run mypy app/integrations/browser_session.py app/core/config.py
cd /home/elon/dev/seo/AISeoTools && .venv/bin/python -m pytest -q   # 1482 passed — стара поведінка не зламана
```

Живий шлях (потребує хоста з `seo-audit` і Chrome): `seo-audit browser-up --provider ahrefs` на хості → `docker compose up -d` → з контейнера `check_session(Provider.ahrefs)` має повернути `alive` після логіну, `no_profile` до першого логіну взагалі, `expired` якщо тепла сесія не піднята.

### Тести

`tests/unit/integrations/test_browser_session.py` (12 тестів, AISeoTools-модулі замокано): allowlist (`reporting_ninja` відхиляється для обох функцій), `check_session` — `no_profile`/`expired`(not running)/`expired`(probe raises)/`alive`(busy)/`alive`(authenticated)/`expired`(not authenticated), `start_login` — `NoWarmChrome` з точною командою, happy-path (busy_lock узятий рівно один раз, `goto` на `login_url` з таймаутом із `Settings`, `login_session_id` повертається і опитується), `login_status` на невідомий id → `KeyError`.

### Follow-ups / борг

- task-21 (preflight у job engine) і task-22 (кнопка «Увійти» в UI) — розблоковані, споживають `check_session`/`start_login`/`login_status`.
- `network_mode: host` не портується на Mac/Windows Docker Desktop без подальшої роботи — задокументовано як свідомий Linux-only компроміс у самому `compose.yaml`.
- `_active_logins` живе в пам'яті одного процесу (як `app.integrations.registry`) — рестарт `api` губить активні `login_session_id`; прийнятно, бо `check_session(provider)` і без нього дає ту саму відповідь.
