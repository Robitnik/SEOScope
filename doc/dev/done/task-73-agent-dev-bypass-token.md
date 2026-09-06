# task-73 — Dev-доступ без входу: агентський токен + localhost для оператора

- **Хвиля:** post-72 (розвиток auth-стеку)
- **Depends on:** **71** ✅ (`AuthMiddleware`, `Actor`), **72** ✅ (журнал дій — обидва механізми мають лишати слід)
- **Міграція:** **не потрібна** (жодної нової таблиці)
- **Нові залежності:** немає

## 0. Проблема

Дві окремі, але споріднені незручності після task-71:

1. **Агенти.** Проєкт активно розробляють паралельні агенти (CLAUDE.md §«Правила паралельності»): кожен піднімає стек, тестує зміни `curl`'ом і Playwright-скриптами проти живого `docker compose up`. Після task-71 будь-який такий запит без cookie-сесії отримує 401/303 — агент або мусить щоразу гнати повний `POST /auth/login` і тримати cookie-jar (як робив я весь час task-71/72: `curl -c cookie.txt …`), або взагалі не має облікового запису й блокується. Це тертя на кожен цикл перевірки, помножене на кількість паралельних агентів.
2. **Оператор.** Той самий фронт на `http://localhost:6524/` для людини за клавіатурою цієї машини тепер теж вимагає форми входу — навіть коли це та сама людина, що піднімає весь стек.

**Не плутати з продовим ризиком**: тунель (`seo.theyovth.space`) — це той самий застосунок, і будь-який бекдор, вкорінений у код, теоретично працює й там. Гірше того — **`network_mode: host`** (compose.yaml) означає, що Cloudflare-тунель форвардить трафік на `127.0.0.1:6524`, тобто з погляду застосунку тунельний запит і запит людини, що відкрила `http://localhost:6524` у своєму браузері, приходять з **однієї й тієї самої адреси**. Наївна перевірка «якщо адреса джерела — localhost, пускай без входу» відкрила б тунель усьому інтернету разом з оператором і звела нанівець усю роботу task-71. Обидва механізми нижче зроблено **структурно мертвими** поза локальною розробкою: перший — без явного секрету, якого прод-конфіг ніколи не отримує; другий — без ще й фізичної відсутності заголовків проксі на з'єднанні (§5).

## 1. Обране рішення (агентський токен)

Один спільний **dev-only bearer-токен**, зчитаний з `.env` (не в git, уже git-ignored — перевірено). Жодної нової таблиці, CLI-команди чи UI: токен розробник генерує собі сам (`python3 -c "import secrets; print(secrets.token_hex(32))"`) і кладе рядком у `.env`, який уже лежить у корені репо й читається і контейнером (`Settings`), і будь-яким агентом на хості (`.env` — звичайний файл у робочій директорії).

**Свідомо відхилені альтернативи** (обговорено з користувачем):
- Таблиця `agent_tokens` + CLI create/list/revoke — точніший аудит і відклична гранулярність, але для внутрішнього dev-контуру це зайва інженерія; можна взяти пізніше, якщо кількість агентів чи інцидентів це виправдає (§8).
- Постійний службовий обліковий запис через `POST /auth/login` — це не «без входу», а лише спрощений вхід; агент і так мусить тримати cookie-жар.

## 2. Механізм автентифікації

### 2.1 Налаштування (`app/core/config.py`, адитивно)

```python
# Обидва прапорці мертві, коли environment == "production" — незалежно від
# значення (перевіряється в app/core/auth/sessions.py).
agent_dev_token: str | None = None
agent_dev_token_role: UserRole = UserRole.admin
agent_dev_token_label: str = "dev-agent"
auth_localhost_bypass: bool = False
```

`UserRole.admin` за замовчуванням для обох механізмів — свідомий вибір: і агенти, і сам оператор будують і перевіряють **увесь** застосунок, включно з адмінкою й журналом дій (підтверджено на практиці — без admin-рівня довелось би заводити реальний обліковий запис під кожен цикл перевірки адмінки). Ризик прийнятний рівно тому, що обидва мертві поза `environment != "production"`.

### 2.2 `AuthenticatedSession.origin` (`app/core/auth/sessions.py`)

`AuthenticatedSession` отримує поле `origin: str = "cookie"` (`"cookie" | "agent" | "local"`) замість окремого булевого прапорця на кожен новий спосіб автентифікації — один вимір, а не N незалежних. `email`/`session_id` стали `str | None` (раніше обов'язкові `str`): ні агент, ні localhost-байпас не мають ні пошти, ні рядка сесії в БД.

Дві нові функції-резолвери поряд із наявним `resolve_session`, обидві **без БД** — статичні перевірки, дешевші за cookie-шлях:

```python
def resolve_agent_token(request: Request, settings: Settings) -> AuthenticatedSession | None:
    if settings.environment == "production" or not settings.agent_dev_token:
        return None
    header = request.headers.get("authorization", "")
    if not header.startswith("Bearer "):
        return None
    token = header.removeprefix("Bearer ").strip()
    if not token or not hmac.compare_digest(token, settings.agent_dev_token):
        return None
    return AuthenticatedSession(
        user_id="agent", email=None, name=settings.agent_dev_token_label,
        role=settings.agent_dev_token_role, is_active=True,
        session_id=None, csrf_token="", origin="agent",
    )
```

`resolve_localhost_bypass` — та сама форма (див. §5 для `is_trusted_loopback`, на якому вона стоїть).

### 2.3 CSRF

Перевірка стала `auth.session_id is not None` замість «завжди»: cookie-сесія — єдиний спосіб автентифікації, який чужий сайт міг би підмінити (ні заголовок `Authorization`, ні саме TCP-з'єднання на localhost браузер ніколи не додає/не робить за чужим сайтом — CSRF тут структурно неаплікований). Один і той самий стан (`session_id is None`) обслуговує обидва нові шляхи — не два окремі `if`.

### 2.4 `Actor` (`app/api/deps.py`) — точковий дотик

`current_actor()` мапить `auth.origin`: `"cookie"` → `Actor.kind="user"`, інакше `kind = auth.origin` (тобто `"agent"` чи `"local"` дослівно) — журнал (task-72) одразу показує, що це не форма входу, і навіть який саме з двох механізмів.

### 2.5 Журнал дій (task-72) — точковий дотик

`AuditMiddleware._record` раніше писав `actor_kind="user" if auth else "anonymous"`; тепер так само дивиться на `auth.origin` — рядки з `#/admin/audit` показують `actor_kind=agent`/`local` окремо від людських дій без додаткової роботи.

## 3. Порядок у middleware

`resolve_agent_token` → `resolve_localhost_bypass` → cookie (`resolve_session`), у порядку зростання вартості. Обидва dev-резолвери викликаються **до** гілки `is_public_path` (не після) — деталь, без якої публічний `GET /api/v1/auth/session` не бачив би жодного з байпасів (§6).

## 4. Безпека агентського токена — чому це не відкриває task-71 заново

| Загроза | Чому закрито |
|---|---|
| Токен з .env потрапить у прод | `environment` у продовому `.env`/деплої — `"production"`; резолвер ігнорує `agent_dev_token` цілком, незалежно від значення |
| Токен опиниться в git | `.env` уже в `.gitignore`; `.env.example` отримує лише коментар-інструкцію, ніколи значення |
| Токен потрапить у логи | Заголовок `Authorization` — уже в `_DEFAULT_REDACT_KEYS` (`app/core/logging.py`) |
| Токен потрапить у журнал дій (`audit_events.details_json`) | `AuditMiddleware` не логує заголовки запиту взагалі (лише тіло) — нема що редагувати |
| Хтось лишить токен слабким/дефолтним | `agent_dev_token` за замовчуванням `None` (вимкнено); документація вимагає `secrets.token_hex(32)`, не рядок-нагадування |
| CSRF через агентський шлях | Bearer не подається браузером автоматично — сам механізм CSRF тут неапліковний (§2.3) |

## 5. Ізі вхід для оператора на `http://localhost:6524/`

### 5.1 `is_trusted_loopback` — де насправді живе безпека

```python
_PROXY_MARKER_HEADERS = (
    "cf-connecting-ip", "cf-ray", "cf-visitor", "cf-ipcountry",
    "x-forwarded-for", "x-forwarded-proto", "x-forwarded-host", "forwarded",
)

def is_trusted_loopback(request: Request) -> bool:
    host = request.client.host if request.client else None
    if host not in {"127.0.0.1", "::1"}:
        return False
    return not any(header in request.headers for header in _PROXY_MARKER_HEADERS)
```

Адреса `127.0.0.1` сама по собі **нічого не доводить** (§0 — тунель форвардить на ту саму адресу). Довіру дає **відсутність** заголовків, які фізично не можуть з'явитися на прямому TCP-з'єднанні браузер→uvicorn: між ними немає жодного проксі, отже й нікого, хто додав би `Cf-Connecting-Ip` чи `X-Forwarded-For`. Запит, що прийшов через `cloudflared`, завжди несе принаймні один з них — Cloudflare/`cloudflared` ставлять їх безумовно, не опційно.

`resolve_localhost_bypass(request, settings)` мертвий при `environment=="production"` чи вимкненому `auth_localhost_bypass`, інакше повертає `AuthenticatedSession(user_id="local", role=UserRole.admin, session_id=None, origin="local", ...)`.

### 5.2 Роль

`UserRole.admin` — той самий вибір, що для агентського токена (§2.1): особистий доступ оператора до всього, що він сам і розробляє.

## 6. `GET /api/v1/auth/session` — навіщо публічний шлях теж торкається байпасу

Цей ендпоінт — те, з чого фронт стартує (`session.js` вирішує, малювати додаток чи вести на `/login.html`), і він **публічний** (`is_public_path`), бо анонім теж має право спитати «чи я залогінений». `AuthMiddleware` раніше пропускав резолв сесії взагалі для публічних шляхів — без зміни людина на localhost проходила б крізь гейт скрізь, а на самому старті фронт усе одно бачив би «cookie немає» й вів на форму входу.

Фікс (§3): `resolve_agent_token`/`resolve_localhost_bypass` викликаються **до** гілки `is_public_path`, і кладуть результат у `request.state.auth_session` навіть для публічних шляхів — просто без примусового 401 на них. Ендпоінт `current_session()` (`app/api/auth.py`) читає `request.state.auth_session` **перед** власним cookie-запитом до БД і, якщо `origin != "cookie"`, повертає синтетичний `UserDTO` (`email` — заглушка на кшталт `local@localhost`/`agent@localhost`, за прикладом уже наявної гілки `auth_disabled@localhost`).

## 7. Owned paths

`app/core/config.py` (адитивно), `app/core/auth/middleware.py`, `app/core/auth/sessions.py` (нові `resolve_agent_token`/`resolve_localhost_bypass`/`is_trusted_loopback`, поле `origin`), `app/api/deps.py` (точковий дотик `current_actor`), `app/api/auth.py` (точковий дотик `current_session`), `app/audit/middleware.py` (точковий дотик `actor_kind`), `.env`/`.env.example` (адитивно), `CLAUDE.md` (нова секція «Доступ агентів для розробки»), `tests/unit/auth/test_dev_bypass.py` (новий, юніт), `tests/unit/api/test_dev_bypass_api.py` (новий, крізь HTTP).

## 8. Тести

**Агентський токен:**

- Валідний `Authorization: Bearer <token>` при `agent_dev_token` заданому й `environment≠production` → 200, `Actor.kind=="agent"`.
- Невірний/відсутній токен → `None`/401, звичайна гілка, не 500.
- `agent_dev_token=None` (дефолт) → заголовок ігнорується.
- Мертвий при `environment=="production"` незалежно від значення токена.
- Мутація без `X-CSRF-Token` проходить (CSRF не застосовний).
- Роль з `agent_dev_token_role` застосовується тим самим `required_role`/`role_satisfies` — `role=viewer` отримує 403 на мутацію так само, як людина.
- Дія агента з'являється в журналі з `actor_kind=agent`.

**Localhost-байпас:**

- Вимкнений за замовчуванням навіть на довіреному loopback-з'єднанні.
- Увімкнений → `GET /api/v1/auth/session` повертає `authenticated: true`, роль `admin`, і будь-який інший ендпоінт теж проходить.
- **Найважливіший тест**: той самий loopback + заголовок `Cf-Connecting-Ip` (чи будь-який інший маркер тунелю/проксі) → 401, байпас не спрацьовує — це і є доказ, що тунель разом з оператором не відкривається.
- Мертвий при `environment=="production"` незалежно від прапорця.
- Мутація без `X-CSRF-Token` проходить.

**Обидва:** `test_auth_coverage.py` (наявний, task-71) лишається зеленим без змін — жоден маршрут не відкривається публічно, лише додаються альтернативні способи довести автентифікацію на вже закритому периметрі.

## 9. Definition of Done / Gate

1. `AGENT_DEV_TOKEN=<32+ байти>` у `.env`, рестарт `api` → `curl -H "Authorization: Bearer $AGENT_DEV_TOKEN" http://127.0.0.1:6524/api/v1/dashboard` віддає 200 без жодного cookie.
2. `AUTH_LOCALHOST_BYPASS=true` у `.env`, рестарт `api` → `curl http://127.0.0.1:6524/api/v1/auth/session` без cookie повертає `authenticated:true`, роль `admin`.
3. Той самий запит із заголовком `Cf-Connecting-Ip: 8.8.8.8` → `authenticated:false` — тунель-подібний запит відхилено.
4. `environment=production` (тимчасово, локально) з обома прапорцями заданими → обидва мертві, усе повертається до звичайного 401/303.
5. Дії через обидва шляхи з'являються в журналі з правильним `actor_kind`.
6. `ruff`, `mypy app`, повний `pytest` — зелені.
7. `.env.example` містить лише коментарі-інструкції, жодного значення.
8. CLAUDE.md отримує секцію «Доступ агентів для розробки» з обома механізмами.

## 10. Follow-ups

- Якщо кількість одночасних агентів чи інцидентів (протермінований токен, потреба відкликати одного без ротації для всіх) виправдає — перейти на таблицю `agent_tokens` + CLI `agent-token create/list/revoke`, окремий `actor_id` на кожен токен.
- Опційно: `agent_dev_token_role`/роль localhost-байпасу за замовчуванням понизити до `member`, якщо практика покаже, що admin-рівень рідко потрібен, а ризик хочеться звузити ще більше.
- Якщо колись знадобиться довірений localhost-доступ і для CI/staging (не лише персональної dev-машини) — розглянути явний allowlist IP замість «localhost без проксі-заголовків», з тим самим гейтом на `environment`.

## 11. Побічна знахідка й фікс: `overflow: hidden` різав будь-який popover у шапці

Під час верифікації DoD #2 виявилось, що меню користувача («ADMINISTRATOR / … / Sign out») обрізається знизу на вужчих екранах і в короткому header-стані — «Sign out» частково чи повністю не видно. Причина не в `z-index` (комп'ютед-стилі показали `#user-menu-panel` коректно над усім, `z-index:40` усередині `.app-header` з `z-index:3`), а в `.masthead { overflow: hidden }`: попап живе в `#user-menu`, вкладеному в `.app-header` → `.masthead`, і будь-який його рендер нижче межі `.masthead` фізично обрізається — незалежно від z-index, `overflow` діє першим.

Перевірено пікселем на 1440/900/390 (`.masthead.style.overflow = "visible"` проти дефолту): рендер фірмової стрічки **ідентичний** в обох варіантах — background-image завжди сам обрізається до боксу власного елемента (`.brand-field`), `overflow` предка на це не впливає. Тобто `overflow: hidden` на `.masthead` не захищав те, що мав захищати, і лише різав будь-який майбутній popover у шапці (той самий баг чекав би на підказки пошуку URL). Видалено з `.masthead`, коментар замінено на пояснення чому це безпечно; прибрано мертвий override в `@media print`.

**Owned paths (додатково):** `frontend/css/app.css` (точковий дотик, той самий файл, що й `frontend/AGENTS.md` веде через увесь фронт — не окрема зона).

---

## ✅ Completion report

- **Дата:** 2026-09-06
- **Гілка / комміт:** `feat/task-61-url-inspector` @ (див. комміт цієї таски)
- **Статус:** done

### Що зроблено

Два dev-only способи автентифікації, обидва структурно мертві поза `environment != "production"`:

- **Агентський bearer-токен** — `AGENT_DEV_TOKEN` у `.env`, заголовок `Authorization: Bearer …`, працює без `POST /auth/login`, без CSRF.
- **Localhost-байпас для оператора** — `AUTH_LOCALHOST_BYPASS=true`, `http://localhost:6524/` без пароля, безпечний завдяки перевірці на відсутність заголовків тунелю/проксі (`is_trusted_loopback`), а не самій адресі.

Побічно виправлено: `.masthead { overflow: hidden }` різав нижню частину будь-якого popover у шапці (меню користувача) — прибрано, стрічка-бренд візуально не змінилась (перевірено пікселем).

### Як зроблено (ключові рішення)

- **`AuthenticatedSession.origin`** (`"cookie"|"agent"|"local"`) — один вимір замість окремого прапорця на кожен новий спосіб входу; `email`/`session_id` стали `str | None`.
- **CSRF-перевірка звужена до `session_id is not None`** — і агентський, і localhost-шлях не можуть бути підроблені чужим сайтом за визначенням (жоден браузер не додає `Authorization` сам і не «підробляє» TCP-з'єднання з localhost за чужим доменом).
- **`resolve_agent_token`/`resolve_localhost_bypass` викликаються ДО `is_public_path`** — інакше `GET /api/v1/auth/session` (публічний, з нього стартує фронт) не бачив би жодного байпасу, і людина на localhost проходила б крізь гейт скрізь, а сам фронт на старті все одно вів би на форму входу.
- **`is_trusted_loopback`**: адреса `127.0.0.1` сама по собі нічого не доводить (`network_mode: host` — тунель форвардить на ту саму адресу); безпеку дає відсутність `Cf-Connecting-Ip`/`X-Forwarded-For`/тощо, які фізично не можуть з'явитися на прямому TCP-з'єднанні браузер→uvicorn.
- **Роль за замовчуванням для обох — `admin`**: і агенти, і оператор розробляють увесь застосунок, включно з адмінкою.
- Побічний фікс `.masthead` overflow — знайдено емпірично (комп'ютед-стилі + пікселева звірка скріншотів на трьох шириндах), не здогадом: z-index уже був правильний, різало саме `overflow`.

Головні файли: `app/core/auth/sessions.py` (нові резолвери), `app/core/auth/middleware.py`, `app/api/deps.py`, `app/api/auth.py`, `app/audit/middleware.py`, `app/core/config.py`, `frontend/css/app.css`.

### Контрактні зміни (Contract changes)

- `AuthenticatedSession`: нове поле `origin`; `email`/`session_id` стали `str | None` (звужений тип, не розширений — існуючі конструктори лишились сумісні, бо `origin` має дефолт `"cookie"`).
- `Actor.kind` тепер може набувати значень `"agent"`/`"local"` окрім `"user"`/`"system"`/`"cli"` — споживач: журнал дій (task-72), уже читає `kind` як вільний рядок.
- Нові налаштування (адитивно): `agent_dev_token`, `agent_dev_token_role`, `agent_dev_token_label`, `auth_localhost_bypass`.

### Як перевірити (verification)

```bash
# .env: AGENT_DEV_TOKEN=<32+ байти>, AUTH_LOCALHOST_BYPASS=true, потім
docker compose restart api

# агент
curl -H "Authorization: Bearer $(grep '^AGENT_DEV_TOKEN=' .env | cut -d= -f2)" \
  http://127.0.0.1:6524/api/v1/dashboard   # 200, без cookie

# оператор
curl http://127.0.0.1:6524/api/v1/auth/session   # authenticated:true, role:admin
curl -H "Cf-Connecting-Ip: 8.8.8.8" http://127.0.0.1:6524/api/v1/auth/session  # authenticated:false

# тести
docker compose exec api poetry run pytest tests/unit/auth/test_dev_bypass.py \
  tests/unit/api/test_dev_bypass_api.py -q

# браузер
BASE_URL=http://127.0.0.1:6524 node tests/e2e/frontend/dev-bypass.cjs
```

Перевіряльник має побачити: `http://localhost:6524/` одразу малює додаток без форми входу; меню користувача відкривається повністю, «Sign out» видно; тунель-подібний запит (із `Cf-Connecting-Ip`) не проходить.

### Тести

- `tests/unit/auth/test_dev_bypass.py` — обидва резолвери юніт-рівнем: валідний/невірний токен, вимкнено за замовчуванням, мертво в `production`, `is_trusted_loopback` з кожним маркером-заголовком окремо.
- `tests/unit/api/test_dev_bypass_api.py` — крізь справжній HTTP із `TestClient(client=("127.0.0.1", …))`: вимкнено за замовчуванням, `/auth/session` бачить байпас, мутація без CSRF, тунель-подібний запит відхиляється, мертво в production, роль агента застосовується, дія в журналі має `actor_kind=agent`.
- `tests/e2e/frontend/dev-bypass.cjs` — браузерний доказ: без cookie одразу додаток, а не форма входу; дропдаун не обрізаний.

### Follow-ups / борг

- Перейти на таблицю `agent_tokens` + CLI, якщо кількість агентів/інцидентів це виправдає.
- Розглянути пониження ролі за замовчуванням до `member` для обох механізмів, якщо admin-рівень рідко буде потрібен на практиці.
