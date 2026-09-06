# task-71 — Авторизація, користувачі та адмінка

- **Хвиля:** post-70 (рел-гейт «здати тул компанії»)
- **Depends on:** **03** ✅ (схема/міграції), **06** ✅ (REST), **12** ✅ (фронт-шел), **13** ✅ (харнес)
- **Blocks:** **72** (audit log — потребує реального актора)
- **Міграція:** **потрібна** (нові таблиці `users`, `user_sessions`) — див. §9
- **Нові залежності:** **немає** (усе на stdlib: `hashlib.scrypt`, `secrets`, `hmac`)

## 0. Стан «як зараз» (виміряно, не припущення)

| Факт | Де |
|---|---|
| Аутентифікації немає ні на одному рівні | `app/main.py` — лише `configure_security` (headers + CORS) |
| `Actor` — заглушка `id="system"`, **0 використань** поза `deps.py` | [app/api/deps.py:71](app/api/deps.py#L71) |
| 164 HTTP-ендпоінти, усі відкриті | `grep -c @router` по `app/api/*.py` |
| Фронт роздається `StaticFiles(directory=frontend, html=True)` на `/` | [app/main.py](app/main.py) |
| Replay архіву віддає сирий HTML клієнтських сайтів на тому ж origin | `app/api/archive_replay.py`, `/archive/view/...` |
| PLAN §13 сам це фіксує як борг | «перед public exposure обов'язкові app login, secure HttpOnly cookie, CSRF protection і audit trail» |

> ⚠️ **Терміново.** Інсталяція вже проксується назовні через Cloudflare-тунель (`https://seo.theyovth.space`). Поки цей таск не закритий — **будь-хто з URL бачить усі дані всіх клієнтів**. До релізу тримати перед тулом Cloudflare Access / basic-auth на тунелі, і знімати його лише після §11.

## 1. Скоуп

**У скоупі:** модель користувача й ролей, логін/логаут cookie-сесіями, глобальний gate на API + статику + replay, адмін-сторінка керування користувачами з генерацією облікових записів, bootstrap адміна, зміна власного пароля, CLI для операторських дій, тести.

**Поза скоупом (свідомо):** SSO/OAuth/LDAP (окрема таска, якщо компанія на Google Workspace — тоді `72`+), 2FA (follow-up, seam закладено), per-client ACL (усі авторизовані бачать усіх клієнтів; поле `role` дає гак на майбутнє), audit-log (task-72).

## 2. Ролі

| Роль | Може |
|---|---|
| `admin` | усе + `/api/v1/admin/*` (користувачі, сесії, у 72 — audit) |
| `member` | усе, крім `admin/*` і батч-операцій: читання, запуски збору, звіти й інструменти по одному клієнту |
| `viewer` | лише читання: будь-який `POST/PUT/PATCH/DELETE` → **403** |

Один чекпоінт на роль — залежність `require_role(...)` + method-клас у middleware; не розсипати перевірки по 164 хендлерах.

## 3. Модель даних

### `users`
`id` GUID PK · `email` (unique, зберігати lowercase; `uq_users_email`) · `name` · `role` (`_str_enum(UserRole)`) · `password_hash` TEXT · `is_active` BOOL · `must_change_password` BOOL · `password_changed_at` · `failed_attempts` INT · `locked_until` UTCDateTime|NULL · `last_login_at` · `created_at` · `created_by_id` (FK `users.id`, nullable — bootstrap-адмін нікого не має).

### `user_sessions`
`id` GUID PK · `user_id` FK → `users.id` (`ondelete="CASCADE"`) · `token_hash` (unique, sha256 від токена; **сам токен у БД не потрапляє ніколи**) · `created_at` · `last_seen_at` · `expires_at` · `revoked_at` NULL · `ip` · `user_agent` (обрізаний до 256).

**Інваріант:** користувача **не видаляємо**, лише `is_active=false` + відкликання сесій — інакше audit-trail (72) втрачає авторів. `DELETE /admin/users/{id}` свідомо відсутній.

### Хешування пароля (без нових залежностей)
`hashlib.scrypt(n=2**15, r=8, p=1, dklen=32)`, формат рядка `scrypt$32768$8$1$<b64salt>$<b64hash>`, звірка через `hmac.compare_digest`. Параметри — у `Settings`, щоб підняти вартість без міграції; при логіні, якщо хеш зроблено старішими параметрами — прозорий re-hash. Свідома відмова від `passlib`/`argon2-cffi`: `poetry lock` у цьому середовищі не проходить (сусідній чекаут AISeoTools), а stdlib-scrypt — memory-hard і достатній для внутрішнього тулу.

## 4. Сесії та cookie

- Токен: `secrets.token_urlsafe(32)`; у БД лише `sha256`.
- Cookie `yovth_session`: `HttpOnly`, `SameSite=Lax`, `Path=/`, `Secure` = `settings.auth_cookie_secure` (true у проді за тунелем, false для `http://localhost`).
- Термін: **абсолютний** `session_absolute_ttl_days=14` + **idle** `session_idle_ttl_hours=72`. Обидва перевіряються на кожному запиті.
- `last_seen_at` оновлюється **не частіше ніж раз на `session_touch_interval_seconds=300`** і йде через `DbWriteGate` асинхронно. Причина не косметична: після task-66/67 кожен зайвий запис у гарячому шляху — це writer-лок на всю систему; «оновлювати last_seen на кожен GET» дало б +N записів на кожен рендер дашборда.
- Логаут — `revoked_at`; «вийти з усіх пристроїв» — масове відкликання по `user_id`.
- Протухлі сесії чистить `DbMaintenanceScheduler` (task-68) щодобовим тіком.

## 5. CSRF

`SameSite=Lax` уже вбиває класичний cross-site form POST, але тримаємо **double-submit** як другий шар:
- при логіні ставимо другий cookie `yovth_csrf` (не HttpOnly, 32 байти);
- будь-який `POST/PUT/PATCH/DELETE` до `/api/*` вимагає заголовок `X-CSRF-Token`, що збігається з cookie (`compare_digest`); інакше **403 `csrf_failed`**;
- `frontend/js/api.js` додає заголовок централізовано в `request()` — по одному місцю, не по 100 викликах;
- `CORSMiddleware` лишається `allow_credentials=False` + same-origin — cross-origin XHR із cookie неможливий за побудовою.

## 6. Точка входу: `AuthMiddleware`

Файл `app/core/auth/middleware.py`, вішається в `configure_security` **першим викликом `add_middleware`**. Порядок важливий: Starlette робить останній доданий middleware зовнішнім, тож auth має бути доданий ДО `SecurityHeadersMiddleware`/`CORSMiddleware` — інакше 401-відповідь піде без security-заголовків і без CORS-обробки preflight.

Логіка:
1. `OPTIONS` → пропустити (preflight).
2. Шлях у публічному allowlist → пропустити.
3. Немає/протухла/відкликана сесія:
   - шлях під `/api/` або `Accept: application/json` → **401** `{"detail": {"message": "Authentication required", "error_code": "auth_required"}}`;
   - навігаційний GET (HTML) → **302** на `/login.html?next=<url-encoded>`.
4. Роль `viewer` + мутуючий метод → **403** `read_only_role`.
5. Кладе `request.state.actor` (див. §7) і пропускає далі.

**Публічний allowlist (закритий, явний):** `/login.html`, `/css/login.css`, `/js/login.js`, `/js/api.js`?—ні, лише те, що потрібно логін-сторінці: `/assets/**`, `/vendor/**`, `/css/tokens.css`, `/api/v1/auth/login`, `/api/v1/auth/session`, `/healthz`, `/favicon.ico`. **Усе інше закрите за замовчуванням** — включно з `/index.html`, `/js/**`, `/api/v1/**`, `/archive/view/**`, вивантаженням артефактів і звітів.

Прапорець `settings.auth_enabled` (default **true**) дозволяє вимкнути gate лише для локальної розробки/тестів; у логах при `false` — WARNING на кожному старті.

## 7. `Actor` стає справжнім

`app/api/deps.py` — розширити наявний seam, **не заводити другий**:

```python
@dataclass(frozen=True)
class Actor:
    id: str                  # user id або "system"
    kind: str                # "user" | "system" | "cli" | "job"
    email: str | None = None
    role: UserRole | None = None
    session_id: str | None = None
    ip: str | None = None
```
`current_actor()` читає `request.state.actor`; воркер/CLI/шедулери користуються `Actor.system()`/`Actor.cli()`. Це та сама точка, на яку task-72 повісить audit — тому підпис фіксуємо тут і в task-02.

## 8. API

| Метод | Шлях | Роль | Опис |
|---|---|---|---|
| POST | `/api/v1/auth/login` | public | `{email, password}` → cookie + `{user, must_change_password}` |
| POST | `/api/v1/auth/logout` | будь-хто | відкликати поточну сесію |
| GET | `/api/v1/auth/session` | public | `{authenticated: bool, user?}` — фронт стартує з цього |
| POST | `/api/v1/auth/logout-all` | будь-хто | відкликати всі свої сесії |
| GET | `/api/v1/admin/users` | admin | список + активні сесії, `last_login_at` |
| POST | `/api/v1/admin/users` | admin | **генерація користувача**: `{email, name, role}` → створює + повертає **одноразовий пароль** |
| PATCH | `/api/v1/admin/users/{id}` | admin | `name`, `role`, `is_active` |
| POST | `/api/v1/admin/users/{id}/reset-password` | admin | новий одноразовий пароль |
| POST | `/api/v1/admin/users/{id}/revoke-sessions` | admin | вигнати з усіх пристроїв |
| GET | `/api/v1/admin/users/{id}/sessions` | admin | активні сесії (ip, ua, last_seen) |

**Генерація користувача.** Пароль генерує сервер (16 символів з алфавіту на 31 без візуально неоднозначних знаків, ~79 біт, формат `k7fq-2mtx-9pld-r4wn`), повертається **рівно один раз** у відповіді на створення/скидання. **Користувач його не змінює** — рішення оператора: так працюють інші тули компанії, тому `must_change_password` і форми зміни пароля в системі немає взагалі; загублений пароль не «згадують», а перевидають (`/reset-password`, який заодно відкликає всі сесії). Ніде більше — ні в БД, ні в логах, ні в 72-audit — plaintext не з'являється. UI показує його в панелі з кнопкою «Copy» і попередженням, що вдруге його не покажуть.

**Інваріанти помилок:** невірний email і невірний пароль дають **однакову** відповідь `invalid_credentials` і однаковий час (при невідомому email усе одно виконуємо фіктивний scrypt) — інакше форма стає оракулом переліку співробітників. Заблокований/неактивний користувач → той самий `invalid_credentials`, деталь лише в лог.

**Rate limit:** `failed_attempts` у БД + `locked_until`; після `login_max_failures=10` — блок на `login_lockout_minutes=15`. Плюс дешевий in-memory лічильник по IP, щоб не писати в БД на кожну невдачу (та сама причина, що в §4).

**Самозахист адмінки:** адмін не може зняти з себе роль admin, деактивувати себе або відкликати власну сесію через `/admin/*`; має лишатися ≥1 активний admin (409 `last_admin`).

## 9. Міграція (зона task-03)

Одна ревізія `create users and user_sessions`, **тільки `op.create_table`**, жодного `batch_alter_table` (CLAUDE.md: саме він стер `client_integrations` на проді). Перед прогоном на бойовій:
```bash
docker compose exec api poetry run python scripts/backup_sqlite.py --out-dir /app/app_data/backups --keep 14
```
Downgrade — `op.drop_table` обох таблиць.

## 10. Bootstrap першого адміна

- CLI: `yovth-report user-create --email … --name … --role admin [--password -]`, `user-list`, `user-reset-password --email …`, `user-deactivate --email …`.
- `docker/entrypoint.sh` після `alembic upgrade head` викликає `yovth-report ensure-admin`: якщо таблиця `users` **порожня** і задані `BOOTSTRAP_ADMIN_EMAIL` (+ опційно `BOOTSTRAP_ADMIN_PASSWORD`) — створює адміна з `must_change_password=true`; якщо пароль не заданий — генерує і **друкує в лог рівно один раз**. Якщо користувачі вже є — no-op (щоб рестарт не воскрешав видаленого адміна).

## 11. Фронтенд (читати `frontend/AGENTS.md` перед стартом)

Нове:
- `frontend/login.html` + `css/login.css` + `js/login.js` — окрема сторінка поза шелом (шел підвантажує пів-додатку, логіну це не треба). Форма email+пароль, помилки з API без перекладу, редірект на `next` або `/`.
- `js/session.js` — на старті `GET /auth/session`, кладе користувача в `store`, віддає `requireAuth()`.
- `js/admin-users-page.js` + маршрут `#/admin/users` у `router.js`; пункт нав-меню рендериться **лише** для `role === "admin"`.
- Меню користувача в хедері (`index.html`): ім'я, роль, «Manage people» (лише admin), «Sign out».

Правки наявного:
- `js/api.js` — `credentials: "same-origin"`, заголовок `X-CSRF-Token`, і **централізована обробка 401**: редірект на `/login.html?next=…` замість тихої помилки в кожній панелі. Ключі запитів (`request-key`) для нових викликів — явні іменовані, не похідні від параметрів (граблі з task-36).
- `index.html` — контейнер меню користувача, bump `?v=` **і на плейн-теґах `<script src>`, і на CSS** (Cloudflare кешує статику 4 год поверх наших no-cache).
- **CSP:** жодних inline-`style=`/inline-`<script>` на новій сторінці — усе класами й окремими файлами.

## 12. Взаємодія з рештою системи

- **SSE** (`js/sse.js`, `EventSource`) заголовків не вміє — cookie-аутентифікація тут єдино робочий варіант; це одна з причин відмови від `Authorization: Bearer`.
- **Replay архіву** (`/archive/view/**`) закривається тим самим middleware; iframe той самий origin і `SameSite=Lax`, тож cookie доходить (sandbox без `allow-same-origin` цьому не заважає — правило про site, а не про origin).
- **Воркер/шедулери** нікуди не логіняться: працюють під `Actor.system()`, HTTP не ходять.
- **Логи:** додати `password`, `new_password`, `token`, `yovth_session`, `set-cookie` у `add_redact_keys()` [app/core/logging.py:79](app/core/logging.py#L79).

## 13. Owned paths

`app/core/auth/` (новий пакет: `passwords.py`, `sessions.py`, `middleware.py`), `app/db/repositories/users.py` (новий), `app/api/auth.py`, `app/api/admin_users.py` (нові), `app/schemas/auth.py` (новий), `app/api/deps.py`, `app/core/security.py`, `app/core/config.py` (адитивно), `app/core/logging.py` (адитивно), `app/cli.py` (адитивно), `docker/entrypoint.sh`, `migrations/versions/<new>.py` (за погодженням із зоною task-03), `frontend/login.html`, `frontend/js/{login,session,admin-users-page}.js`, `frontend/css/login.css`, `frontend/js/{api,router}.js` + `frontend/index.html` (**точкові дотики**), `tests/unit/auth/**`, `tests/unit/api/test_{auth,admin_users}_api.py`.

⚠️ `frontend/js/api.js` та `index.html` одночасно чіпає **task-70**. Не починати з них: спершу зробити ізольовану частину (бек + login-сторінка), а спільні файли брати лише коли `git status` по них чистий.

## 14. Тести

- `tests/unit/auth/test_passwords.py` — формат хеша, звірка, невірний пароль, re-hash при зміні параметрів, стійкість до підробленого рядка.
- `tests/unit/auth/test_sessions.py` — видача/звірка токена, idle- й absolute-протухання, відкликання, троттлінг `last_seen_at`.
- `tests/unit/api/test_auth_api.py` — логін/логаут, однакова відповідь на невідомий email і невірний пароль, лок-аут після N спроб, зміна пароля, `must_change_password` блокує решту API.
- **`test_auth_coverage.py` — головний регресійний запобіжник:** параметризований прохід по **всіх** маршрутах `app.routes`; кожен, крім явного allowlist, має віддати 401 без cookie. Новий відкритий ендпоінт від будь-якої майбутньої таски впаде тут, а не на проді.
- `test_admin_users_api.py` — рольовий гейт, генерація пароля рівно один раз, `last_admin` 409, viewer не може мутувати.
- CSRF: мутація без заголовка → 403; з невірним → 403.
- Секрети: жодна відповідь/лог не містить plaintext-пароля чи `token_hash` (перевірка по тілу відповіді й перехопленому логу).
- Фронт: Playwright-скрипт `.cjs` (див. memory про харнес) — редірект неавторизованого на login, успішний логін, поява пункту Admin лише для адміна.

## 15. Definition of Done / Gate

1. `curl` без cookie на **будь-який** з 164 ендпоінтів → 401; на `/` → 302 на `/login.html`. Перевірено тестом `test_auth_coverage.py`, а не вибірково.
2. Логін реальним обліковим записом у Docker → дашборд працює як раніше, SSE-прогрес живий, replay архіву відкривається.
3. Адмін створює користувача в UI → одноразовий пароль показано один раз → новий користувач логіниться цим паролем, бачить дані, **не** бачить `#/admin/users` і отримує 403 на `/api/v1/admin/*`.
4. `viewer` отримує 403 на будь-яку мутацію, читання працює.
5. Деактивація й `revoke-sessions` вибивають користувача **в межах наступного запиту**.
6. `ruff`, `mypy app`, повний `pytest` — зелені (крім 2 передіснуючих падінь).
7. Плейн-текст пароля відсутній у БД, логах, відповідях (крім одноразової видачі) — перевірено `grep` по свіжому логу й дампу таблиці.
8. `alembic upgrade head` + `downgrade -1` відпрацьовують на **копії бекапу**, не на бойовій.
9. `p95` дашборда не деградував (auth додає рівно один індексований SELECT сесії на запит) — зміряти `scripts/db_bench.py`/таймінгами task-68 до і після.

## 16. Follow-ups

- One-time invite-link замість тимчасового пароля (`user_invites`, TTL 72 год) — приємніше для роздачі в Slack.
- 2FA (TOTP, stdlib `hmac`+`base64`) — seam у `users` уже є.
- Google Workspace SSO, якщо компанія на ньому.
- Per-client ACL (`user_client_access`), якщо частина команди має бачити лише свій пул.

---

## ✅ Completion report

- **Дата:** 2026-09-06
- **Гілка / комміт:** `feat/task-61-url-inspector` @ (див. комміт цієї таски)
- **Статус:** done-with-followups

### Що зроблено

Тул більше не можна відкрити без облікового запису — жодним шляхом. Живо працює:

- **Логін** сторінкою `/login.html` з cookie-сесією; вихід, вихід на всіх пристроях.
- **Глобальний гейт** `AuthMiddleware`: усі 164 REST-ендпоінти, статика фронта, replay архіву та вивантаження артефактів закриті **за замовчуванням**; публічні — лише те, без чого не намалюється форма входу.
- **Три ролі**: `admin` / `member` / `viewer`. `viewer` не виконує жодної мутації; `member` не бачить адмінки і **не запускає батч-операції** (batch reports, imports, cloud) — вони палять зовнішні квоти по всьому портфелю або незворотні.
- **Адмінка `#/admin/users`**: створення акаунта з **згенерованим сервером паролем**, показаним рівно один раз, зміна ролі, вмикання/вимикання, перевидача пароля, «sign out everywhere», лічильник активних сесій, дата останнього входу.
- **CLI**: `user-create`, `user-list`, `user-reset-password`, `user-deactivate`, `ensure-admin` (bootstrap із `BOOTSTRAP_ADMIN_EMAIL` у `docker/entrypoint.sh`, спрацьовує лише на порожню таблицю).

### Як зроблено (ключові рішення)

- **`hashlib.scrypt`, а не `passlib`/`argon2-cffi`** — `poetry lock` у цьому середовищі не проходить (сусідній чекаут AISeoTools). Параметри KDF зберігаються поряд із хешем, тож їх можна підняти без міграції: старий хеш прозоро переобчислюється при наступному вдалому логіні. `app/core/auth/passwords.py`.
- **Cookie-сесія з opaque-токеном, не JWT.** `EventSource` (SSE) не вміє заголовків, а живий прогрес побудований на ньому. У БД лежить лише `sha256` токена — дамп таблиці нікому не дає представитись користувачем.
- **Гейт у middleware, не в роутерах.** Перевірка в кожному хендлері була б обіцянкою, що жоден із 164 наявних і всіх майбутніх ендпоінтів не забуде декоратор. `AuthMiddleware` додається **першим** у `configure_security`, тобто виконується найглибше — 401/403 усе одно проходять крізь security-заголовки й CORS.
- **`last_seen_at` троттлиться до 1/5 хв** і його провал не валить запит. Після task-66/67 (writer-лок був зайнятий 97% часу) запис на кожен запит був би податком на всю систему.
- **Однакова відповідь на невідомий email і невірний пароль**, включно з часом (для неіснуючого користувача виконується фіктивний scrypt) — інакше форма входу стає довідником співробітників агенції.
- **Користувачів не видаляють**, лише деактивують; деактивація й перевидача пароля відкликають сесії негайно.
- **Захист від самоблоку**: адмін не знімає роль/активність із себе, останнього активного адміна прибрати не можна.
- **Політика ролей — у конфізі, не в коді**: `auth_admin_only_paths` дозволяє закрити ще один інструмент без правок; той самий список віддається фронту (`/auth/session` → `admin_only_tools`), тож UI і сервер не можуть розійтися.
- **Тести решти набору свідомо йдуть із вимкненим гейтом** (autouse-фікстура в кореневому conftest + маркер `auth`): вмикати логін у 2400 тестах, які перевіряють поведінку ендпоінтів, а не доступ до них, означало б переписати їх заради нуля впевненості. Замість цього доступ покриває один параметризований тест по **всіх** `app.routes`.

Головні файли: `app/core/auth/{passwords,sessions,policy,middleware}.py`, `app/db/repositories/users.py`, `app/api/{auth,admin_users}.py`, `app/schemas/auth.py`, `migrations/versions/a1c72f4b9d30_users_and_sessions.py`, `frontend/{login.html,js/login.js,js/session.js,js/admin-users-page.js,css/{login,admin}.css}`.

**Відхилення від плану:** прибрано зміну пароля користувачем і `must_change_password` — на вимогу оператора пароль генерує система і він не змінюється (так працюють інші тули компанії). Перевидати може лише адмін.

### Контрактні зміни (Contract changes)

- `app/schemas/enums.py`: **новий** `UserRole` + `ROLE_ORDER` (адитивно).
- `app/api/deps.py`: `Actor` із заглушки став реальним (`id`, `kind`, `email`, `name`, `role`, `session_id`, `ip`), додано `require_role()` / `AdminActorDep`. Дефолти лишились сумісними: `Actor()` = `Actor.system()`, тож наявні виклики не змінювались.
- Нові таблиці `users`, `user_sessions` (міграція `a1c72f4b9d30`).
- Нові ендпоінти `/api/v1/auth/*` і `/api/v1/admin/users*`.
- Споживач наступний: **task-72** (audit log) — пише саме цього `Actor`.

### Як перевірити (verification)

```bash
# 1. Бекап + міграція (обов'язково в такому порядку)
docker compose exec api poetry run python scripts/backup_sqlite.py --out-dir /app/app_data/backups --keep 14
docker compose exec api poetry run alembic upgrade head

# 2. Перший адмін
docker compose exec api poetry run yovth-report user-create --email you@theyovth.com --name "Your Name" --role admin
docker compose restart api

# 3. Гейт (усе без cookie)
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:6524/api/v1/dashboard   # 401
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:6524/                    # 303 -> /login.html

# 4. Тести
docker compose exec api poetry run pytest tests/unit/auth tests/unit/api/test_auth_api.py \
  tests/unit/api/test_admin_users_api.py tests/unit/api/test_auth_coverage.py -q

# 5. Браузерний прогін (логін, ролі, адмінка, вихід)
BASE_URL=http://127.0.0.1:6524 CHROMIUM_PATH=/snap/bin/chromium NODE_PATH=/tmp/yovth-e2e/node_modules \
  ADMIN_EMAIL=… ADMIN_PASSWORD=… VIEWER_EMAIL=… VIEWER_PASSWORD=… \
  node tests/e2e/frontend/auth-smoke.cjs
```

Перевіряльник має побачити: форму входу замість будь-якої сторінки; після входу — додаток без змін у поведінці; вкладку **People** лише в адміна; згенерований пароль в окремій панелі з кнопкою Copy; `viewer` із вимкненими «Refresh data»/«Run audits».

### Тести

- `tests/unit/auth/test_passwords.py` — round-trip, відмова на зіпсований хеш без винятку, різна сіль, форма згенерованого пароля.
- `tests/unit/auth/test_sessions.py` — absolute/idle протухання, відкликання, деактивація, **троттлінг `last_seen_at`**, лок-аут, purge.
- `tests/unit/api/test_auth_api.py` — 401/303, публічні шляхи, HttpOnly+SameSite, нерозрізненність невдач, лок-аут, CSRF (без токена й із підробленим), рольові заборони, відсутність пароля в логах.
- `tests/unit/api/test_admin_users_api.py` — видача пароля рівно один раз, робочий пароль, 409 на дубль, скидання пароля, миттєвий вихід при деактивації, `self_demotion`/`last_admin`, 404.
- `tests/unit/api/test_auth_coverage.py` — **параметризований прохід по всіх `app.routes`**: кожен, крім явного списку, віддає 401/403 без сесії; окремо закритість статики й replay архіву.
- `tests/e2e/frontend/auth-smoke.cjs` — браузерний сценарій, скриншоти `auth-login-1440.png`, `auth-admin-users-1440.png`, `auth-viewer-dashboard-1440.png`.

### Follow-ups / борг

- **Зняти зовнішній гейт із тунелю** (Cloudflare Access / basic-auth) лише після того, як цю таску викотили.
- `AUTH_COOKIE_SECURE=true` у проді — за тунелем https; перевірити, що в `.env` це так.
- **task-72** — журнал дій; `Actor` уже несе все потрібне.
- 2FA (TOTP на stdlib) — окремих полів у `users` ще немає, але seam на місці.
- Invite-посилання замість пароля в чаті — свідомо не робили (оператор: пароль).
- Періодичне прибирання протухлих сесій підключити до `DbMaintenanceScheduler` (task-68), зараз `purge_expired_sessions()` є, але шедулер його не викликає — `TODO(task-68)`.
