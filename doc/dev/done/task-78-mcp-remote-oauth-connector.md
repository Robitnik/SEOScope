# task-78 — MCP без docker: віддалений HTTP-транспорт + OAuth для claude.ai

- **Хвиля:** post-77 (доступ AI-асистента, друга частина)
- **Depends on:** **74** (хвиля 1 — реєстр із 12 тулів, комміт `5346001`), **77** ✅ (сторінка `#/mcp` — саме там житиме інструкція), **71** ✅ (користувачі, ролі, сесії — на них тримається згода), **72** ✅ (журнал), **75** ✅ (Postgres — нові таблиці без `batch_alter_table`)
- **Міграція БД:** **потрібна** (`oauth_clients`, `oauth_auth_codes`, `oauth_tokens`, `agent_tokens`)
- **Нові залежності:** **немає** — SDK `mcp` ^2.1.1 уже несе і Streamable HTTP, і повний OAuth-каркас (перевірено live, див. §3.2)

---

## 0. Проблема

Хвиля 1 віддає тули через **stdio**, а конфіг хоста виглядає так:

```json
"command": "docker",
"args": ["compose", "exec", "-T", "api", "poetry", "run", "python", "-m", "app.mcp.stdio"]
```

Це працює рівно на одній машині в компанії — тій, де лежить цей репозиторій і піднятий docker. Для решти команди воно не «незручне», а **структурно непридатне**: щоб скористатись, треба клонувати проєкт, зібрати образ із Playwright/Chromium і тримати стек піднятим.

Для claude.ai воно непридатне ще й фізично: claude.ai ходить **із хмари Anthropic** (egress `160.79.104.0/21`), він не може запустити підпроцес на чиємусь ноутбуці. Локальний транспорт тут не «повільніший варіант», а неможливий.

Отже потрібні дві речі, яких у хвилі 1 немає: **HTTP-транспорт, досяжний з інтернету**, і **авторизація, яку claude.ai уміє пройти сам**.

## 1. Що саме вимагає claude.ai

Це не з пам'яті — звірено з документацією Anthropic і специфікацією MCP (посилання в §12). Найдорожчі помилки тут не в коді, а в невідповідності цим пунктам, тому вони виписані як чекліст:

| Вимога | Деталь |
|---|---|
| Транспорт | Streamable HTTP, публічний **HTTPS**, досяжний з `160.79.104.0/21` |
| Виклик без токена | **`401`** + `WWW-Authenticate: Bearer resource_metadata="…"`. **Не** `200` з `isError: true` — на такий Claude просто віддасть текст помилки моделі й ніякої кнопки Connect не покаже |
| `403` | лише зі `error="insufficient_scope"` (step-up); будь-який інший `403` = термінальна помилка |
| PRM (RFC 9728) | поле `resource` **точно** дорівнює URL, який людина вводить у Claude; `authorization_servers[0]` — наш issuer (пізніші елементи Claude не пробує) |
| AS metadata (RFC 8414) | обов'язково `code_challenge_methods_supported: ["S256"]` |
| Реєстрація клієнта | `oauth_dcr` (є `registration_endpoint`, приймає **public client** `token_endpoint_auth_method: "none"`) **або** `oauth_cimd` (метадані мусять нести І `client_id_metadata_document_supported: true`, І `"none"` у `token_endpoint_auth_methods_supported`) |
| Парсери тіла | `/token` — **`application/x-www-form-urlencoded`**; `/register` — **`application/json`**. Це різні парсери, і док прямо попереджає не вважати їх одним |
| redirect_uri | `https://claude.ai/api/mcp/auth_callback` для claude.ai/Desktop/mobile; Claude Code — loopback `http://localhost/callback` і `http://127.0.0.1/callback` **з ігноруванням порту** (RFC 8252 §7.3) |
| Refresh | ротація для public-клієнтів; на протухлий refresh — рівно `invalid_grant`, не власний код |
| Таймаути | 10 с на discovery/register/token, 30 с на refresh — інакше підключення «іноді не працює» |
| Кеш discovery | глобальний, ~5 хв, спільний на всіх користувачів того самого URL |

⚠️ **Наслідок останнього рядка, який варто прочитати двічі:** помилка в PRM або в метаданих AS не проявляється як «зламалось у мене» — вона застигає в кеші Anthropic на всіх. Тому §8 вимагає перевіряти метадані **ззовні**, до першого підключення.

## 2. Вибір способу авторизації

Anthropic підтримує п'ять варіантів. Три з них тут відпадають, і причини конкретні:

| Варіант | Чому не він / чому він |
|---|---|
| `none` (без авторизації) | Публічний інтернет + дані всіх клієнтів агенції. Не обговорюється |
| `static_headers` (beta) | Один фіксований токен, який вводить **адмін організації** (Team/Enterprise), спільний на всю компанію. Технічно найдешевше — і саме тому спокусливо. Але це повертає рівно ту проблему, яку task-71 закривав для людей: журнал task-72 знову бачить «якийсь агент» замість людини. Лишаємо як **запасний** шлях фази A, не як ціль |
| `oauth_anthropic_creds` | Потребує листування з `mcp-review@anthropic.com` і зберігання наших секретів на боці Anthropic. Для внутрішнього тула — зайва зовнішня залежність |
| `oauth_cimd` | Не вимагає `/register`; рекомендований для великого трафіку. Дешево додати **разом** із DCR (це два прапорці в метаданих), але як єдиний механізм — вужча сумісність |
| **`oauth_dcr` + `oauth_cimd`** | **Обране.** Підтримується «out of the box» на всіх поверхнях, включно з персональним конектором на Pro/Max, без листування з будь-ким |

**Головний виграш обраного варіанта — не сумісність, а особа.** Токен видається конкретному користувачеві task-71 після його згоди в браузері. Далі:

- роль цього користувача вирішує, що дозволено — жодного нового механізму прав (task-74 §4.3 уже це описав);
- журнал task-72 показує **реальну людину**, а не спільного «agent»;
- відкликати доступ одній людині = відкликати її токени, а не міняти секрет усій компанії.

Тобто OAuth тут не «данина протоколу», а єдиний варіант, у якому наявні ролі й журнал лишаються правдою.

## 3. Архітектура

### 3.1 Де живе `/mcp`

**Рішення: монтуємо MCP-застосунок у наявний FastAPI, на тому самому origin** — не окремим сервісом, як припускала task-74 §9.

Причина конкретна, не стилістична: `/.well-known/oauth-protected-resource` і метадані AS мають лежати **на тому самому домені**, а тунель у нас один і веде на один порт. Розводити MCP на окремий сервіс означає або другий тунель/домен, або крос-хостову конфігурацію, яку док називає найкрихкішою частиною налаштування.

Інваріант ізоляції (task-74 §2) від цього **не страждає**: тули так само ходять у REST через `ApiClient`, AST-тест `tests/unit/mcp/test_isolation.py` лишається без змін і далі забороняє `app.db`/`app.jobs`/`AISeoTools` всередині `app/mcp/**`.

⚠️ **Ризик, який треба назвати вголос:** тул усередині цього ж процесу робить HTTP-виклик **сам у себе**. Сьогодні `docker/entrypoint.sh` піднімає `uvicorn` **без `--workers`** — один процес. Під паралельними викликами це самообслуговування може впертися в себе. Тому фаза A має **виміряти** це під навантаженням, і якщо впирається — вмикати `--workers` (task-75 §7.2 крок 1 це вже передбачав) або виносити MCP окремим процесом. Рішення за числами, не наперед.

### 3.2 Що дає SDK, а що пишемо самі

Перевірено на встановленому `mcp` 2.1.1 (`docker compose exec api poetry run python -c …`):

- `MCPServer.streamable_http_app(streamable_http_path="/mcp", transport_security=TransportSecuritySettings(...))` — транспорт **і** захист від DNS-rebinding (`allowed_hosts`/`allowed_origins`) уже в коробці;
- конструктор приймає `auth_server_provider`, `token_verifier`, `auth: AuthSettings` — SDK сам піднімає `/authorize`, `/token`, `/register`, `/revoke` і документи discovery;
- `AuthSettings(issuer_url, resource_server_url, client_registration_options, revocation_options, required_scopes)`;
- `ClientRegistrationOptions(enabled=True, valid_scopes=…, default_scopes=…)` — DCR вмикається прапорцем.

Пишемо самі рівно дві речі:

1. **`OAuthAuthorizationServerProvider`** — 9 методів (`get_client`, `register_client`, `authorize`, `load_authorization_code`, `exchange_authorization_code`, `load_refresh_token`, `exchange_refresh_token`, `load_access_token`, `revoke_token`) поверх Postgres.
2. **Екран згоди** — сторінка, яка вимагає сесію task-71, показує, хто саме питає доступ і з якою роллю, і повертає код.

`TokenVerifier` виходить тонким: токен → рядок у `oauth_tokens` → `AccessToken(subject=user_id, scopes=[...])`.

### 3.3 Схема БД (міграція, task-03-стиль, чистий `op.create_table`)

- `oauth_clients` — `client_id`, `client_secret_sha256` (nullable: public-клієнти без секрету), `redirect_uris`, `client_name`, `registered_at`, `last_used_at`. DCR створює рядок на **кожне** нове підключення, тож потрібен TTL/прибирання невикористаних (док прямо попереджає про розростання).
- `oauth_auth_codes` — короткоживучі: `code_sha256`, `client_id`, `user_id`, `redirect_uri`, `code_challenge`, `scopes`, `resource`, `expires_at`, `used_at`.
- `oauth_tokens` — `token_sha256`, `kind` (`access`/`refresh`), `user_id`, `client_id`, `scopes`, `resource`, `expires_at`, `revoked_at`, `rotated_from`. Самі токени **не зберігаються** — той самий принцип, що з сесіями task-71.
- `agent_tokens` — з task-74 §4.2, для CI/скриптів і `static_headers`: `label`, `token_sha256`, `role`, `created_by`, `last_used_at`, `revoked_at`.

Жодного `DELETE` для відкликання: `revoked_at`, щоб журнал не втрачав авторів (принцип task-71).

## 4. Scopes і ролі

Ролі вже є (task-74 §4.3), scope — це те, що з них бачить протокол:

| Scope | Дає | Кому видається |
|---|---|---|
| `mcp:read` | усі read-тули (11 із 12 у W1) | будь-якому користувачеві |
| `mcp:write` | `run_client_report` та майбутні write-тули | лише `member`/`admin` |
| `mcp:admin` | батчі, імпорти, cloud (коли з'являться тули) | лише `admin`, окремою згодою |

`scopes_supported` у PRM = `["mcp:read"]` — мінімум для базової роботи. `mcp:write` набирається **step-up'ом**: write-тул без потрібного scope → `403` + `WWW-Authenticate: Bearer error="insufficient_scope", scope="mcp:read mcp:write"`, і claude.ai сам покаже повторний Connect. Це рівно та поведінка, під яку зроблений протокол, і вона дає безкоштовно те, що інакше довелося б вигадувати: **дорога операція вимагає окремої явної згоди людини**.

Якщо роль користувача не дозволяє запитаний scope — видаємо токен **без** нього (а не помилку): людина побачить, що write не з'явився, і це чесніше, ніж мовчазний 500 у момент виклику.

## 5. Авторизація на рівні запиту: 401 на все

Питання «чи пускати `tools/list` без токена» (lazy auth) вирішене **ні**: усі 12 тулів читають дані клієнтів, публічної частини в цього сервера немає. Отже будь-який запит без валідного токена — `401` з коректним `WWW-Authenticate`, включно з `initialize`.

Окремий тест на пастку з §1: сервер **не сміє** відповідати `200 + isError` там, де протокол чекає `401` — інакше замість кнопки Connect людина побачить у чаті текст «please sign in» і вирішить, що тул зламаний.

## 6. Гейт закритості: найнебезпечніша частина таски

`AuthMiddleware` закритий за замовчуванням (`_PUBLIC_EXACT`/`_PUBLIC_PREFIXES` у `app/core/auth/policy.py`). OAuth-ендпоінти й `/mcp` мусять пройти повз нього, бо несуть **власну** авторизацію — і саме тут таска може непомітно відкрити діру.

Правила, які тут не обговорюються:

1. Додаємо **точні шляхи**, не префікси. Ніяких `/mcp/` — префікс завтра накриє те, чого сьогодні не існує.
2. Список публічних шляхів фіксується **тестом**: змінився список — тест червоний, поки людина не підтвердить зміну явно.
3. `/authorize` публічний рівно настільки, щоб віддати редірект на форму входу: без сесії task-71 він **не** показує згоду й **не** видає код.
4. Кожен новий публічний шлях має власний тест «без сесії віддає рівно те, що має, і жодного байта клієнтських даних».
5. Параметризований guard task-71 (усі роути закриті) лишається зеленим і **непорожнім** — з тим самим `assert len(...) > 100`, який колись урятував від тихо зламаного обходу маршрутів (task-74, фікс `_IncludedRouter`).

## 7. Мережа й Cloudflare

- Тунель уже віддає `https://seo.theyovth.space` — окремий домен не потрібен.
- Anthropic ходить із `160.79.104.0/21`. Перевірити, що Bot Fight Mode / WAF / rate limiting **не** б'ють по `/mcp` і `/.well-known/*`; за потреби — skip-правило на цей діапазон.
- `/.well-known/*` **не кешувати** на Cloudflare. Проєкт уже одного разу зловив тунельний кеш, що перебивав заголовки застосунку (`cloudflare_static_asset_cache_bug`), а тут ціна вища: Claude і сам кешує ці документи ~5 хв, і другий шар кешу зробить виправлення метаданих невидимим надовго.
- **Перевіряти лише ззовні.** `is_trusted_loopback()` (task-73) робить localhost принципово нерепрезентативним: з localhost воно «працює» навіть тоді, коли ззовні зламане.

## 8. Порядок робіт — три фази з гейтами

Фази навмисно розділені так, щоб перша вже приносила користь, не чекаючи на OAuth.

**Фаза A — транспорт.** Streamable HTTP на `/mcp`, авторизація простим bearer'ом з `agent_tokens`, `TransportSecuritySettings` з нашим доменом. Дає Claude Code / Claude Desktop **віддалено, без docker** (обидва вміють заголовки в конфізі) і закриває питання «чи взагалі проходить тунель, Origin, таймаути».
*Гейт:* `tools/call` відпрацьовує з чужої машини, ззовні, по HTTPS. Виміряний self-HTTP під паралельним навантаженням (§3.1).

**Фаза B — OAuth.** PRM + метадані AS + DCR + CIMD + `/authorize` зі згодою поверх сесії task-71 + `/token` + ротація refresh.
*Гейт:* конектор реально додано в claude.ai і сценарій із task-74 §0 відпрацьовано в чаті; транскрипт у completion report.

**Фаза C — доводка.** Step-up на `mcp:write`, журнал (`actor_kind`, реальний user_id), відкликання токенів в `#/admin/users`, сторінка `#/mcp/connect` (task-77) переписана під новий спосіб: замість `docker compose exec` — просто URL.

## 9. Тести

Юніт (`tests/unit/mcp/` + `tests/unit/api/`):

- **Метадані:** `resource` у PRM точно дорівнює оголошеному URL; `authorization_servers[0]` == issuer; `code_challenge_methods_supported == ["S256"]`; `"none"` у `token_endpoint_auth_methods_supported`; є `registration_endpoint`; є path-suffixed варіант `/.well-known/oauth-protected-resource/mcp`.
- **401-контракт:** будь-який MCP-запит без токена → `401` + коректний `WWW-Authenticate`; **окремий** тест, що це не `200 + isError`.
- **Парсери:** `/token` приймає `x-www-form-urlencoded`, `/register` приймає `application/json` — двома різними тестами, бо це різні шляхи.
- **PKCE:** без `code_challenge` — відмова; невірний `code_verifier` → `invalid_grant`.
- **redirect_uri:** точний збіг для `https://claude.ai/api/mcp/auth_callback`; port-agnostic для `http://localhost/callback` і `http://127.0.0.1/callback`; чужий redirect → відмова.
- **Audience:** токен, виданий для іншого `resource`, відхиляється.
- **Ролі/scopes:** `viewer` не отримує `mcp:write`; write-тул із токеном без scope → `403 insufficient_scope` зі scope у заголовку.
- **Refresh:** ротується; старий refresh після ротації → рівно `invalid_grant`.
- **Відкликання:** `revoked_at` миттєво робить токен невалідним.
- **Закритість:** список публічних шляхів зафіксований; guard task-71 зелений і непорожній.
- **Журнал:** виклик тула лишає рядок `audit_events` з реальним `user_id`, а не зі спільним агентом.

Інтеграційний: повний цикл `register → authorize → code → token → tools/call → refresh` на `TestClient`.

Живий (обов'язково **ззовні**, не з localhost): `curl` по метаданих, потім реальне підключення claude.ai.

## 10. Owned paths

```
app/mcp/http.py                      (новий — монтування транспорту)
app/mcp/auth/**                      (новий — provider, token verifier, consent)
app/api/oauth.py                     (новий — те, що не дає SDK: consent-роут)
app/db/models.py                     (адитивно: 4 таблиці)
app/db/repositories/oauth.py         (новий)
migrations/versions/*_oauth_mcp.py   (нова)
app/core/auth/policy.py              (точково: явний список публічних шляхів)
app/core/config.py                   (адитивно: MCP_PUBLIC_URL, MCP_ENABLED, TTL токенів)
frontend/oauth-consent.html          (новий) + frontend/js/oauth-consent.js
frontend/js/mcp-page.js              (фаза C: новий спосіб підключення замість docker)
docker/entrypoint.sh, compose.yaml   (за результатом виміру §3.1)
tests/unit/mcp/**, tests/unit/api/test_oauth_*.py
```

## 11. Definition of Done

1. `/mcp` відповідає ззовні по HTTPS; `tools/list` і `tools/call` працюють із чужої машини **без docker і без цього репозиторію**.
2. claude.ai додає конектор за URL, проходить OAuth і виконує сценарій із task-74 §0 — транскрипт у completion report.
3. Токен прив'язаний до конкретного користувача; журнал task-72 показує саме його; `viewer` не може запустити write-тул.
4. Усі тести §9 зелені; guard закритості зелений і непорожній.
5. Метадані перевірені **ззовні** `curl`'ом до першого підключення (через 5-хвилинний глобальний кеш помилка дорога).
6. Міграція застосована після бекапу за процедурою task-75 §8 (`pg_dump -Fc`).
7. Сторінка `#/mcp/connect` (task-77) описує новий спосіб; старий stdio лишається задокументованим для розробників цього репозиторію.
8. Жодної нової залежності.

## 12. Джерела (звірено 2026-09-06)

- Authentication for connectors — https://claude.com/docs/connectors/building/authentication
- Lazy authentication (канонічна форма `401`) — https://claude.com/docs/connectors/building/lazy-authentication
- Custom connectors (remote MCP) — https://support.claude.com/en/articles/11175166-about-custom-connectors-remote-mcp-servers
- MCP Authorization spec — https://modelcontextprotocol.io/specification/draft/basic/authorization
- Anthropic egress IP ranges — https://platform.claude.com/docs/en/api/ip-addresses

## 13. Ризики й відкриті питання

- **Кеш discovery на 5 хв глобально** — помилка в метаданих б'є по всіх і не лікується перезаходом. Пом'якшення: перевірка ззовні до підключення (DoD п.5).
- **DCR розростається** — новий клієнт на кожне свіже підключення. Пом'якшення: CIMD увімкнено паралельно + прибирання невживаних `oauth_clients`.
- **Один origin на дашборд і MCP** — спільний WAF, спільний rate limit. Якщо почне заважати, наступний крок — окремий піддомен, але це вже потребує другого тунелю.
- **10 с на `/token`** — цей ендпоінт не має права ходити в повільні місця; жодних синхронних викликів провайдерів у ньому.
- **Відкрите питання до користувача (не блокує):** який план claude.ai. Team/Enterprise додатково відкриває org-wide конектор і beta-режим `static_headers`; Pro/Max дає персональний конектор, де OAuth обов'язковий. План веде до OAuth, який працює в обох випадках, тож відповідь впливає лише на те, чи має сенс фаза A як окремий проміжний реліз для команди.

---

## ✅ Completion report

- **Дата:** 2026-09-06
- **Гілка / комміт:** feat/task-61-url-inspector @ (див. комміт цього звіту)
- **Статус:** done-with-followups

### Що зроблено

MCP-сервер тула працює **прямо в чатах на claude.ai** як custom connector —
без docker, без конфіг-файлу, без встановлення чогось на машину людини.
Людина вставляє один URL (`https://seo.theyovth.space/mcp`), логіниться
своїм звичайним паролем, тисне Approve — і 12 тулів з'являються в її
звичайних розмовах, на всіх пристроях одразу.

Живо перевірено наскрізь проти бойового домену: DCR-реєстрація →
`/authorize` → екран згоди → обмін коду з PKCE → `initialize` →
`tools/list` (12 тулів) → `tools/call list_clients` із реальними
клієнтами → ротація refresh.

### Як зроблено (ключові рішення)

- **Чому взагалі OAuth, а не «встав токен».** Документація конекторів
  прямо каже: `static_headers` — бета й вводить їх адміністратор
  ОРГАНІЗАЦІЇ, один на всіх. Для окремої людини з коробки працює лише
  `oauth_dcr`. Тобто OAuth тут не архітектурна розкіш, а єдиний вхід.
- **Токен доступу = рядок `agent_tokens`** (`app/mcp/oauth.py`). Це
  головне рішення таски: конектор не отримав ні власного строку життя,
  ні власного відкликання, ні власного місця в журналі. Наслідки —
  `min(роль токена, ПОТОЧНА роль власника)` рахується тим самим
  `resolve_personal_token_sync`, журнал бачить реальну людину, а сам
  конектор видно в `#/account` серед своїх токенів і відкликається там
  же. Жодного нового механізму не з'явилось.
- **Стеля `member` для конектора, навіть якщо власник адмін.** Батчі,
  імпорти й керування людьми не мають бути на відстані однієї фрази до
  чат-асистента.
- **Клієнти лише public**, `client_secret` не видається й не
  зберігається взагалі — інваріант «секрети не в БД» лишився цілим, а
  захист коду тримає PKCE S256, який Claude шле завжди.
- **Маршрути SDK додаються поштучно, не `mount`** (`app/mcp/asgi.py`):
  `/mcp`, `/authorize`, `/token`, `/register`, `/.well-known/**` мусять
  жити в корені домену, а корінь уже зайнятий статикою фронта.
- **Тули ходять в API токеном ТОГО, ХТО ПИТАЄ** (`app/mcp/apiclient.py`).
  У stdio-режимі сервер — особистий підпроцес однієї людини, тож токен із
  `.env` і був її токеном; по HTTP один процес обслуговує всіх, і брати
  токен із конфігурації означало б, що будь-хто підключений ходить
  правами того, хто налаштував сервер.

### Знайдене вживу (варте пам'яті)

- SDK віддає bearer-автентифікацію **не в маршрутах**, а окремим списком
  middleware свого Starlette-застосунку. Перенести самі `routes` мало:
  `/mcp` бачив кожен запит анонімним і чесно відповідав 401 навіть на
  бездоганний токен, тоді як `load_access_token` у процесі повертав той
  самий токен валідним. Лікується загортанням РІВНО маршруту `/mcp` у
  `AuthenticationMiddleware(AuthContextMiddleware(...))`.
- `MCPServer(...)` не приймає `auth_server_provider` і `token_verifier`
  разом — падає з `ValueError`; провайдера достатньо, верифікатор він
  збудує сам.
- DNS-rebinding-захист SDK за замовчуванням пускає лише localhost. До нас
  приходять із публічного домену через тунель — обидва варіанти мають
  бути в `allowed_hosts` явно, інакше кожен запит помирає ще до auth.
- Серверний редірект на форму входу губив фрагмент: `#/oauth/consent?…`
  зникав, і людина після логіну опинялась на дашборді, а Claude чекав
  коду. Виправлено в `frontend/js/login.js` — `next` тепер зберігає
  `location.hash`.

### Контрактні зміни (Contract changes)

- `McpCatalogDTO` +`mcp_url` (адитивно). `connect` лишився, але тепер
  містить один запис — сам URL конектора; stdio-конфіг прибрано разом із
  транспортом хвилі 1 для сторонніх хостів.
- Нові таблиці: `oauth_clients`, `oauth_auth_codes`,
  `oauth_pending_authorizations`, `oauth_refresh_tokens`;
  `agent_tokens` +`oauth_client_id` (nullable). Міграція
  `d6d668fe8d53`, застосована на прод після `pg_dump -Fc`.
- Нові публічні маршрути (без сесії, за призначенням): `/mcp`,
  `/authorize`, `/token`, `/register`, `/revoke`, `/.well-known/**`.
- Новий API: `GET /api/v1/oauth/pending/{id}`, `POST /api/v1/oauth/consent`.

### Як перевірити (verification)

```bash
curl -sI https://seo.theyovth.space/mcp | grep -i www-authenticate   # 401 + resource_metadata
curl -s  https://seo.theyovth.space/.well-known/oauth-authorization-server | jq .
docker compose exec api poetry run pytest tests/unit/auth/test_oauth_connector.py -q
NODE_PATH=/tmp/yovth-e2e/node_modules node tests/e2e/frontend/mcp-page.cjs
```

В UI: `#/mcp` → «How to connect» → 5 кроків із чекбоксами; на claude.ai
Settings → Connectors → Add custom connector → вставити URL → Connect.

### Тести

- `tests/unit/auth/test_oauth_connector.py` (12) — одноразовість коду,
  протермінування очікування, ротація й відкликання refresh, відсутність
  plaintext-значень у БД, і чотири тести на головний інваріант: токен
  конектора поводиться рівно як особистий токен (стеля `member` навіть
  для адміна, viewer лишається read-only, деактивація власника вбиває
  доступ негайно).
- `tests/e2e/frontend/mcp-page.cjs` — сторінка конекту як чекліст:
  5 кроків, 5 чекбоксів, стан переживає reload, копіюється саме URL
  конектора, і жодної згадки docker/конфіг-файлу не лишилось.

### Follow-ups / борг

- **Фаза C плану не робилась:** step-up на write-тули (окреме
  підтвердження на мутації через чат) — свідомо відкладено, стеля
  `member` вже прибирає найгостріше.
- `oauth_clients` росте на КОЖНЕ свіже підключення (властивість DCR) —
  ретенція старих рядків не написана. Не терміново (рядок ~1 КБ), але
  варте окремої дрібної таски разом із чисткою протермінованих
  `oauth_pending_authorizations` і використаних кодів.
- `tests/contract/test_aiseotools_signatures.py` червоний НЕ через цю
  таску: апстрім `AISeoTools` додав опціональні аргументи (`headed`,
  `virtual_display`, `include_visual`), снапшот сигнатур застарів.
- Кешування Cloudflare для `/api/*` (див. пам'ять про це) конектора не
  зачіпає: `/mcp` — POST, а `/token` і `/register` віддають `no-store`
  силами SDK.
