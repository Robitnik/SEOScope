# task-51 — Site Monitor: редіректи сайту (хто/коли з'явився, що змінилося)

- **Пакет:** монітор веде **власну історію правил редіректу** кожного клієнта: щогодинний знімок → діф → запис у стрічку змін із **вікном появи** («з'явився між 14:03 і 15:04»). Закриває живий біль: «на сайті виник зайвий редірект, а коли і хто його зробив — невідомо».
- **Хвиля:** M — спостережуваність сайтів (продовження task-35/36/37/38/41).
- **Depends on:** **35** ✅, **36** ✅, **38** ✅, **41** ✅
- **⚠️ Не паралелити з task-50** (`monitor-antibot-lane`): спільна зона запису `app/monitor/**`, `app/api/monitor.py`, `app/schemas/monitor.py`, `frontend/js/monitor-client.js`. Порядок: **50 → 51**.
- **PLAN.md:** §9.1 (доріжки), §12 (чесні стани: «не змогли перевірити» ≠ «зникло»), §14 (секрети не потрапляють у БД/логи/UI)
- **frontend/AGENTS.md:** §5 (правда даних), §6 (стани), §15–16 (візуальний loop)
- **AISeoTools:** змін не потребує

---

## Мета (робота оператора, яку закриваємо)

Сьогодні на питання «звідки взявся цей 301 і коли він з'явився» відповіді немає **в принципі**: найпоширеніший у портфелі плагін (Redirection) не зберігає ані дати створення правила, ані автора (див. §1.3). Ми не можемо змінити плагін — але можемо **спостерігати ззовні щогодини** й тим самим самі стати джерелом дати: правило, якого о 14:03 не було, а о 15:04 воно є, з'явилося у цьому вікні. Далі оператор іде в access-логи хостингу або до клієнта вже з точним годинним інтервалом, а не з «десь за останні пів року».

---

## 1. Дослідження (факти, на яких стоїть план)

### 1.1 Живий зріз портфелю (виміряно 2026-08-19, `GET /wp-json/` по всіх 14 клієнтах)

| Клієнт | `redirection/v1` | Інше | Режим збору |
|---|---|---|---|
| 19cleveland.com, exxxoticaexpo.com, effyscafe.com, grillpointnyc.com, hearwellservices.com, laxton.se, pjclarkes.com, nysapphire.com, shorelinedentalstudio.com, shawarma-mia.com | **є** | `wp/v2`, `yoast/v1` | authenticated pull |
| goreport.com, nychearing.com, smashhouseburgers.com | немає | `wp/v2`, `yoast/v1` | probe |
| alphastyle.store | — | **Shopify** (`powered-by: Shopify`, `/wp-json/` → 404) | probe |

**10 із 14** сайтів віддадуть повний список правил через API — це і є основний режим. Решта 4 не лишаються сліпою зоною: для них працює blackbox-проба (§3.2.3).

### 1.2 Анонімно список не читається — і це правильно

`GET https://<site>/wp-json/redirection/v1/redirect?per_page=5` на трьох сайтах віддав однаково:

```
401 {"code":"rest_forbidden","message":"Sorry, you are not allowed to do that."}
```

Отже потрібен **Application Password** (Basic auth). Nonce при цьому не потрібен: `X-WP-Nonce` вимагається лише для cookie-автентифікації, а `permission_callback` плагіна (`Redirection_Capabilities::has_access`, дефолтна капабіліті `manage_options`) з Basic-авторизацією проходить штатно.

Побічний факт із живої перевірки: валідація `args` спрацьовує **раніше** за `permission_callback` (`per_page=1` дав `400 rest_invalid_param`, а не 401) — тобто по коду відповіді не можна судити про доступ, поки параметри невалідні. Клієнт мусить надсилати одразу коректні параметри (`per_page` 5..200).

### 1.3 Плагін Redirection **не зберігає** ані дати створення, ані автора

Схема з вихідників (`Redirection 5.9.0`, `database/schema/latest.php`, `create_items_sql`):

```
redirection_items: id, url, match_url, match_data, regex, position, last_count,
                   last_access, group_id, status, action_type, action_code,
                   action_data, match_type, title
```

Жодного `created`/`modified`/`author`. `last_access`/`last_count` — це **останнє спрацювання**, не створення. Тому:

- **єдиний спосіб дізнатися дату — знімати список самим і діфати** (це і є ця таска);
- `last_access`/`last_count` **обов'язково виключаються з фінгерпринта** (§3.1), інакше кожне спрацювання редіректу читалося б як «правило змінилося», і стрічка змін щогодини рахувала б сотні фальшивих змін.

Для порівняння: у Rank Math таблиця має `created`/`updated`, у SEOPress/Safe Redirect Manager правила — це CPT (`post_date`/`post_modified`/`post_author`). Адаптери під них — follow-up (§8), контракт джерела (§3.2.1) вже це передбачає.

### 1.4 Головне джерело «зайвих» редіректів — автоматика, а не людина

У Redirection є опція **Monitor changes to posts**: при зміні permalink або відправці посту в кошик плагін сам створює 301 і кладе його в групу **Modified Posts** (`models/monitor.php`: `action_code = 301`, `group_id = monitor_post`, для кошика ще й `status = disabled` з ціллю `/`). Тому:

- **група правила — це відповідь на «хто»**: `Modified Posts` = «створила автоматика, коли хтось редагував пост»;
- назву групи треба тягнути разом із правилами й показувати як `origin` у стрічці змін (§3.5);
- і саме тому «будь-який новий редірект = warning» зробило б стрічку непридатною на активних сайтах — звідси градація за ризиком (§3.4, підтверджено з користувачем 2026-08-19).

### 1.5 Blackbox-режим має за що зачепитися

`curl -sSI https://www.effyscafe.com/` віддає `301` + `x-redirect-by: WordPress` — заголовок `X-Redirect-By` (WP 5.1+) називає виконавця редіректу, і Redirection його підписує своїм значенням (`modules/wordpress.php`, фільтр `x_redirect_by`). Для сайтів без API це дає не лише факт редіректу, а й **джерело**.

### 1.6 Половина механіки вже написана — переюзати, не переписувати

- `app/tools/redirects/trace.py` — похоповий трейс без автослідування 3xx, зі SSRF-гардом і тротлінгом (task-29). Це готовий двигун probe-режиму.
- `app/tools/http_safety.py` — `assert_public_http_url`, `HostThrottle`, UA, ліміт тіла.
- `app/monitor/storage.py` + `monitor_blobs` — content-addressed сховище: знімок списку правил зберігається як звичайний документ, і переглядач версій/дифів із task-36 працює для нього **без нового коду**.
- `app/monitor/diff.py` уже має `page_redirect_appeared` (warning) для моніторених сторінок і `sitemap_url_redirects` — нові типи не дублюють їх, а доповнюють (§3.4).
- `frontend/js/monitor-diff.js` — переглядач дифів версій, готовий приймати ще один `kind`.

### 1.7 Секретів у БД проєкт не тримає взагалі

`ClientIntegration.config_json` явно документований як «non-secret external identifiers only». Механізму шифрованих полів у проєкті немає, `cryptography` в залежностях немає, а додавати залежність — зона task-01 (і `poetry lock` у пісочниці нестабільний). Отже Application Passwords ідуть **файлом на диску поза БД**, за прецедентом `browser_profiles_dir` (§3.3).

---

## 2. Обсяг (scope)

**Входить:**

1. Новий підмодуль `app/monitor/redirects/` — контракт джерела + два адаптери (`wp_redirection`, `probe`) + автодетект джерела.
2. Дві таблиці: `monitor_redirects` (поточний стан правила) і `monitor_redirect_versions` (незмінний ланцюг версій) + знімок усього списку як `MonitorResource(kind="redirects")`.
3. **Вікно появи** правила (`first_seen_window_start` … `first_seen_at`) — головна вимога.
4. Нові типи змін і їх серйозність (градація за ризиком), агрегат для масових додавань, self-healing.
5. Зберігання й читання Application Passwords поза БД, зі станом «configured / missing / invalid» в API (без самих значень).
6. API: список правил, версії, diff, статус джерела, разова перевірка.
7. UI: блок **Redirects** на сторінці клієнта + лейбли нових типів у стрічці змін.
8. CLI: `monitor-redirects` (стан/разова перевірка), `wp-credentials-check`.

**Не входить (свідомо):**

- Адаптери Rank Math / Yoast Premium / SEOPress / AIOSEO — контракт готовий, реалізація за потребою (§8).
- Запис/редагування редіректів на сайтах клієнтів. Ми **тільки читаємо** — інструмент нагляду, не панель керування.
- Cloudflare Rulesets / серверні `.htaccess` як джерела.
- Відповідь на «хто саме з людей це зробив» (це аудит-лог **на боці сайту**; наша межа — точне вікно часу + походження правила, §8).

---

## 3. Рішення

### 3.1 Модель даних: той самий патерн «стан + ланцюг незмінних версій»

**`monitor_redirects`** — поточний стан одного правила:

| поле | сенс |
|---|---|
| `id`, `client_id` | як у решти монітора |
| `source` | `"redirection"` \| `"probe"` — яким адаптером бачене |
| `external_id` | `id` правила в плагіні (AUTO_INCREMENT); `NULL` для probe |
| `rule_key` | ключ ідентичності: `external_id` для pull, нормалізований `source_url` для probe |
| `source_url`, `match_type`, `regex` | що матчиться |
| `target_url`, `http_code` | куди й яким кодом |
| `status` | `enabled` \| `disabled` |
| `group_name`, `title` | походження (`Modified Posts` = автоматика, §1.4) |
| `rule_sha256` | фінгерпринт значущих полів |
| `first_seen_at` | коли **ми** його вперше побачили |
| `first_seen_window_start` | час попередньої **успішної** перевірки — нижня межа вікна появи (§3.5) |
| `last_seen_at`, `removed_at` | останній раз у списку / коли зникло (`NULL` = живе) |
| `version_seq` | лічильник версій |

Унікальність: `(client_id, source, rule_key)`.

**`monitor_redirect_versions`** — незмінний після коміту рядок (той самий `before_update`-гард, що й у решти версійних таблиць): `redirect_id`, `client_id`, `observed_at`, `snapshot_json`, `rule_sha256`, `changed_fields_json`, `prev_version_id`.

**Фінгерпринт** (єдине джерело істини — `app/monitor/redirects/fingerprint.py`):

```
sha256(source_url, match_type, regex, target_url, http_code, status, group_name, title)
```

**`last_access`, `last_count`, `position` у нього не входять** (§1.3) — інакше стрічка щогодини повнилася б фальшивими «змінами». `position` — теж ні: перетягування правил у списку не змінює поведінку сайту.

**Знімок списку цілком** зберігається як блоб і `MonitorResource(kind="redirects")` з `structure_json = {total, enabled, disabled, by_code:{301:…,302:…}, by_group:{…}}`. Це дає безкоштовно: історію файлу, «View file», і diff двох знімків у переглядачі task-36 — рівно як для robots.txt/sitemap.

### 3.2 Джерела даних

#### 3.2.1 Контракт (`app/monitor/redirects/contract.py`)

```python
@dataclass(frozen=True)
class RedirectRule:
    rule_key: str
    source_url: str
    target_url: str | None
    http_code: int | None
    match_type: str
    regex: bool
    status: str            # "enabled" | "disabled"
    group_name: str | None
    title: str | None
    external_id: int | None

@dataclass(frozen=True)
class RedirectSourceResult:
    ok: bool
    source: str            # "redirection" | "probe"
    rules: tuple[RedirectRule, ...]
    raw_snapshot: bytes | None      # у блоб, як є
    unavailable_reason: str | None  # "auth_failed" | "not_found" | "timeout" | ...
```

`ok=False` **ніколи** не означає «правил немає» — це `redirects_unavailable` (§3.4), той самий принцип, що вже діє для `dns_unavailable`/`sitemap_unavailable` (task-41).

#### 3.2.2 Адаптер `wp_redirection.py` (основний, 10/14 сайтів)

- Основний виклик: `GET {base}/wp-json/redirection/v1/export/all/json` — один запит, правила **і** групи, готовий до збереження в блоб.
- Фолбек, якщо експорт віддав не-JSON: посторінковий `GET /redirection/v1/redirect?per_page=200&orderby=id&direction=desc` (+ `GET /group` для назв груп). `filterBy[id]` — це точний збіг (`id=%d`), не «більше ніж», тож інкрементального фільтра не існує; беремо повний список.
- Basic auth, **тільки HTTPS** (інакше — відмова без запиту), таймаут `monitor_request_timeout_seconds`, ліміт тіла, SSRF-гард і тротлінг — усе через `app/tools/http_safety.py`.
- Класифікація відповідей: `200` → ok; `401`/`403` → `auth_failed`; `404` з HTML → `plugin_missing`; `5xx`/timeout → `unavailable`.

#### 3.2.3 Адаптер `probe.py` (fallback, решта сайтів)

Blackbox: беремо набір URL-кандидатів і трасуємо кожен через `app/tools/redirects/trace.py`:

1. усі правила цього клієнта з `removed_at IS NULL` (щоб побачити зміну цілі або зникнення);
2. ключові сторінки (`_key_pages`, ті самі, що вже використовує light-прогін);
3. до `monitor_redirect_probe_max_urls` (дефолт 50) URL із sitemap, обраних кільцем — **без дублювання** page-перевірки: сторінки й так перевіряються, probe цікавить лише перший хоп.

Кожен `3xx` стає `RedirectRule(source="probe", rule_key=normalized_source_url)`, `X-Redirect-By` пишеться в `title` як `via: <value>` (§1.5). Probe **не бачить** правил, якими ніхто не ходить, — і чесно про це каже в UI («probe mode: only known URLs are checked»).

#### 3.2.4 Автодетект (`detect.py`)

`GET {base}/wp-json/` (анонімно, дешево) → з `namespaces` вибираємо джерело; результат кешується в `MonitorSettings.redirect_source_json` разом із датою. `MonitorSettings.redirect_source` (`auto|redirection|probe|off`) дозволяє оператору зафіксувати вручну.

### 3.3 Креденшели: файл поза БД, у БД — лише стан

- Новий шлях у налаштуваннях: `wp_credentials_path` (дефолт `./app_data/secrets/wp_credentials.json`), сам каталог — у `.gitignore`, права `0600`.
- Формат: `{"<client_id>": {"user": "...", "app_password": "..."}}`.
- Читає **тільки** монітор у worker-процесі; API повертає виключно похідний стан: `"configured" | "missing" | "invalid"`.
- Пароль **ніколи**: у БД, у `monitor_changes`, у `details_json`, у відповідях API, у тексті помилок для UI. У логах — `RedactionFilter.add_keys({"app_password", "authorization"})` + патерн на `Basic [A-Za-z0-9+/=]+`.
- **Рекомендація в документації таски (не код):** заводити на сайті клієнта окремого користувача з роллю, обмеженою фільтром `redirection_role`, а не адміна — Application Password адміна дає повний доступ до сайту, і зберігати такий у нас без потреби не можна.
- Немає файлу/запису для клієнта → це **не помилка**, а стан `not_configured` (інваріант проєкту): блок UI показує «Not connected», і клієнт мовчки падає в probe-режим, якщо той доступний.

### 3.4 Серйозність (градація за ризиком — підтверджено 2026-08-19)

Додається в `app/monitor/severity.py` (єдине джерело істини):

| `change_type` | рівень | коли |
|---|---|---|
| `redirect_offsite_added` | **critical** | ціль — інший registrable domain (ознака компрометації; тим самим правилом, що й наявний `canonical_offsite`) |
| `redirect_added_on_live_url` | **warning** | джерело редіректу — URL, який у нас відомий як живий (є в sitemap або має останню версію зі статусом 200): редірект накрив робочу сторінку |
| `redirect_target_changed` | **warning** | у наявного правила змінилась ціль або код |
| `redirect_removed` | **warning** | правило зникло зі списку (ламає старі посилання) |
| `redirect_added` | **info** | звичайне нове правило (типово — автоматика з `Modified Posts`) |
| `redirect_status_changed` | **info** | `enabled` ⇄ `disabled` |
| `redirects_bulk_added` | **info/warning** | за один прогін додано > `monitor_redirects_bulk_threshold` (25) правил: **один** агрегат із розбивкою й прикладами замість сотень рядків (патерн `sitemap_broken_urls`) |
| `redirects_unavailable` | **warning** | не змогли прочитати джерело (401/timeout/плагін зник) — «не перевірили» ≠ «правил немає» |

Self-healing як у решти монітора: коли причина зникає, відкритий запис отримує `resolved_at` + `info`-подія `<type>_resolved`. Перший збір редіректів для клієнта — **baseline**, не алертить нічого (позначка в `monitor_runs.notes_json`), інакше перший день дав би 300 рядків «новий редірект».

Оператор може глушити типи через наявний `MonitorSettings.muted_change_types_json` — нового механізму не треба.

### 3.5 Вікно появи — те, заради чого все

Кожне нове правило записує:

- `first_seen_at` = `observed_at` цього прогону (верхня межа);
- `first_seen_window_start` = `last_checked_at` **попередньої успішної** перевірки редіректів цього клієнта (нижня межа — момент, коли правила ще не було).

У `monitor_changes.details_json` для `redirect_added*`:

```json
{"window_start": "2026-08-19T14:03:11Z", "window_end": "2026-08-19T15:04:02Z",
 "window_minutes": 61, "origin": "Modified Posts", "external_id": 1487}
```

UI: `Appeared between 14:03 and 15:04 · 61 min window · origin: Modified Posts`.

**Чесність меж (PLAN §12):** якщо попередньої успішної перевірки немає (baseline, або джерело було недоступне 6 годин поспіль), `window_start = null`, і UI пише `First seen 15:04 · earlier window unknown` — а не вигадує межу. Саме тому `redirects_unavailable` важливий не лише як алерт: він розриває ланцюг і чесно розширює вікно.

**Групування одного руху.** Кілька нових правил із послідовними `external_id`, побачених в одному прогоні, — це одна дія на сайті (один збережений пост). Вони згортаються в `redirects_bulk_added` з переліком, а не N окремих записів.

### 3.6 Де це виконується

Збір редіректів — крок **light**-прогону (щогодини), одразу після robots/sitemap:

- pull-режим: 1 запит (+1 на групи у фолбеку) — дешевше за будь-яку сторінку;
- probe-режим: до 50 трейсів із тим самим `HostThrottle`, що й решта прогону.

Годинний інтервал і є обіцяна точність «межі години». Клієнт із `light_interval_hours` = 0.5 отримає півгодинні вікна без жодних змін коду.

Вимикачі: глобальний `monitor_redirects_enabled` і per-client `MonitorSettings.redirects_enabled` / `redirect_source = "off"`. Збій цього кроку **ніколи** не валить прогін: він ізольований так само, як `_process_dns`/`_process_tls`.

### 3.7 Налаштування (`app/core/config.py`, адитивно — зона task-01, задокументувати)

```
monitor_redirects_enabled: bool = True
monitor_redirects_bulk_threshold: int = 25
monitor_redirect_probe_max_urls: int = 50
monitor_redirects_max_rules: int = 20_000      # аварійний клапан, не продуктова межа
wp_credentials_path: Path = Path("./app_data/secrets/wp_credentials.json")
```

### 3.8 API (`app/api/monitor.py`, префікс `/api/v1`)

| Метод | Шлях | Віддає |
|---|---|---|
| `GET` | `/clients/{id}/monitor/redirects` | сторінковано: `state=active\|removed\|all`, `q`, `source`, `sort` |
| `GET` | `/clients/{id}/monitor/redirects/{rid}/versions` | ланцюг версій правила |
| `GET` | `/clients/{id}/monitor/redirects/{rid}/diff?from=&to=` | поле-в-поле diff двох версій |
| `GET` | `/clients/{id}/monitor/redirects/status` | `{source, credentials, last_checked_at, last_error_code, rules_total, enabled, disabled}` — **без секретів** |
| `POST` | `/clients/{id}/monitor/redirects/check` | разова перевірка (як `check-page`, з тим самим гардом від паралельного запуску) |

Історія списку цілком — наявні `/monitor/resources?kind=redirects` і `/resources/{rid}/versions`. Стрічка змін — наявний `/monitor/changes`, лише з новими `change_type`.

Іменування: `redirects` скрізь — БД, API, UI, фільтри (правило «одна назва — одна річ», CLAUDE.md). Слово `alerts` не використовується (зайнято task-17).

### 3.9 UI (`frontend/js/monitor-client.js`, `formatters.js`, `frontend/css/monitor.css`)

Новий блок **Redirects** між «Sitemap» і «Pages»:

- заголовок: `Redirects` + підпис `142 rules · Redirection plugin` \| `probe mode · known URLs only` \| `Not connected`;
- дії: `View snapshot`, `History` (переглядач дифів task-36), `Check now`;
- таблиця: `Source URL → Target`, `Code`, `Status`, `Group`, `First seen` (з вікном), `Last seen`; нові за останні 24 год — підсвічені;
- фільтр `Active / Removed / All` + пошук по URL;
- стани за AGENTS.md §6: `Not connected` (не помилка — підказка, що треба Application Password), `Could not be checked` для `redirects_unavailable`, порожній стан «No redirects found».
- лейбли нових `change_type` у `CHANGE_TYPE_LABELS` (`formatters.js`), напр. `redirect_offsite_added: "Redirect to external domain added"`.

UI-копірайт — англійською; CSP: жодних inline-`style`, класи-тони (пам'ятка з task-36).

### 3.10 CLI (`app/cli.py`, адитивно)

```
yovth-report monitor-redirects --client-id <id> [--check]   # стан + разова перевірка
yovth-report wp-credentials-check                            # по кожному клієнту: configured/missing/invalid — БЕЗ значень
```

---

## 4. Owned paths

- `app/monitor/redirects/**` (новий: `contract.py`, `wp_redirection.py`, `probe.py`, `detect.py`, `fingerprint.py`, `credentials.py`)
- `migrations/versions/<rev>_monitor_redirects.py` (**down_revision = `658f2eeb029e`** — head на 2026-08-19)
- `tests/unit/monitor/test_redirects_*.py`, `tests/unit/db/test_monitor_redirects_repository.py`

**Адитивні дотики поза власною зоною** (кожен — один блок, усі перелічити в Completion report):

| Файл | Що саме |
|---|---|
| `app/db/models.py` | дві нові таблиці + `before_update`-гард для версій |
| `app/db/repositories/monitor.py` | `upsert_redirect_state`, `create_redirect_version`, `list_redirects_page`, `redirect_versions`, `mark_redirects_removed` |
| `app/monitor/runner.py` | `_process_redirects(...)` + виклик у light-прогоні |
| `app/monitor/severity.py` | нові записи в `STATIC_SEVERITY` + `redirect_bulk_severity()` |
| `app/monitor/diff.py` | `diff_redirect_rules(old, new)` |
| `app/api/monitor.py`, `app/schemas/monitor.py` | ендпоінти §3.8 + DTO |
| `app/core/config.py` | налаштування §3.7 |
| `app/core/logging.py` | redaction-ключі/патерни §3.3 |
| `app/cli.py` | дві команди §3.10 |
| `frontend/js/monitor-client.js`, `frontend/js/formatters.js`, `frontend/css/monitor.css` | блок і лейбли §3.9 |
| `.gitignore` | `app_data/secrets/` |

Read-only: `app/tools/redirects/**`, `app/tools/http_safety.py`, `app/monitor/storage.py` — використовуються як є.

---

## 5. Тести

**Unit (`tests/unit/monitor/`):**

- `fingerprint`: зміна `last_access`/`last_count`/`position` **не** дає нової версії; зміна цілі/коду/статусу — дає.
- `diff_redirect_rules`: added / removed / changed / повернення видаленого правила (`removed_at` очищується, нової «появи» не вигадуємо).
- вікно: `window_start` = попередня успішна перевірка; після `redirects_unavailable` вікно **розширюється**, а не бреше; baseline → `window_start = null`.
- severity: offsite → critical; редірект на URL із sitemap → warning; звичайний → info; >25 нових → один агрегат.
- `wp_redirection` адаптер на фейковому `Fetcher`: 200 (експорт), не-JSON → фолбек на посторінковий список, 401 → `auth_failed`, 404-HTML → `plugin_missing`, timeout → `unavailable`; **http-URL відхиляється без мережевого виклику**.
- `probe` адаптер: 301 → правило, ланцюг 301→301→200 → перший хоп, `X-Redirect-By` у `title`.
- `credentials`: відсутній файл → `missing` без винятку; биті права/JSON → `invalid`; **тест, що пароль не з'являється в `repr`, логах і DTO**.

**API (`tests/unit/api/test_monitor_api.py`):** кожен ендпоінт §3.8, включно з «клієнта немає» (404), «перевірок ще не було» (чесний порожній стан, не 500), пагінацією й `status` без секретів.

**Integration (`tests/integration/test_monitor_redirects_end_to_end.py`):** локальний `http.server` як фейковий WP: (1) baseline — 3 правила, жодної зміни у стрічці; (2) додаємо правило + міняємо ціль іншого → `redirect_added` з вікном + `redirect_target_changed`; (3) сервер віддає 401 → `redirects_unavailable`, правила **не** позначені видаленими; (4) 401 зникає, правило прибрали → `redirects_unavailable_resolved` + `redirect_removed`, вікно наступної появи чесно розширене.

**Frontend:** візуальний loop за AGENTS.md §15–16 + e2e-скріншоти блоку (1440/390) у трьох станах: connected, `Not connected`, `Could not be checked`.

---

## 6. Ризики та запобіжники

| Ризик | Запобіжник |
|---|---|
| Application Password = ключ від сайту клієнта | файл поза БД (0600, gitignore), redaction у логах, HTTPS-only, ніколи в API/UI/помилках; рекомендація окремої обмеженої ролі замість адміна |
| Пароль протух / клієнт змінив пароль | `redirects_unavailable` (warning, self-healing) + стан `invalid` у блоці UI; правила **не** зникають зі стану |
| Шум від авто-редіректів `Modified Posts` | градація за ризиком (§3.4), агрегат `redirects_bulk_added`, `muted_change_types_json` |
| Величезний список правил | `monitor_redirects_max_rules` (20 000) як аварійний клапан + повний знімок у блоб, а не в `structure_json` |
| Плагін оновить API / клієнт перейде на інший плагін | адаптер ізольований за `RedirectSourceResult`; автодетект по `namespaces` перемикає джерело сам, `redirect_source` фіксує вручну |
| Probe дає хибне «правило зникло» на разовому збої | правило знімається лише за **успішного** збору; збій → `redirects_unavailable`, `removed_at` не ставиться (той самий урок, що task-41) |
| Конфлікт із task-50 за ті самі файли | послідовність 50 → 51, зафіксована в шапці та в INDEX |

---

## 7. Definition of Done / Gate

1. `docker compose exec api poetry run pytest tests/unit/monitor tests/unit/api/test_monitor_api.py -q` — зелено; `ruff check .` і `mypy app` — чисто.
2. Міграція піднімається й відкочується (`alembic upgrade head` / `downgrade -1`) на копії робочої БД.
3. На **живому** клієнті з Redirection (напр. `effyscafe.com`) із заведеним Application Password: перший прогін створює baseline без записів у стрічці; після ручного додавання тестового редіректу наступний light-прогін дає запис із **вікном ≤ інтервалу перевірки**; видалення правила дає `redirect_removed`.
4. На клієнті **без** плагіна (напр. `smashhouseburgers.com`) і на Shopify (`alphastyle.store`) блок показує probe-режим і не створює фальшивих алертів.
5. Немає жодного місця, де пароль потрапляє в БД, лог, подію або відповідь API — перевірено тестом і `grep` по артефактах прогону.
6. Скріншоти блоку (1440/390) у трьох станах додані; чекліст AGENTS.md §15–16 пройдено.
7. План перенесено в `doc/dev/done/` з Completion report, INDEX.md оновлено.

---

## 8. Підтверджені рішення та follow-ups

**Підтверджено з користувачем 2026-08-19:**

- Режим збору — **обидва**: authenticated pull там, де є Application Password, probe для решти; один клієнт може мати обидва.
- Серйозність — **градація за ризиком** (§3.4), а не «будь-який новий = warning».

**Follow-ups:**

- `TODO(task-NN)`: адаптери Rank Math (`wp_rank_math_redirections` має `created`/`updated`), SEOPress/Safe Redirect Manager (CPT → `post_author`/`post_date` = автор «безкоштовно»), Yoast Premium (серіалізована опція, без дат).
- `TODO(task-NN)`: mu-plugin на хуках `redirection_redirect_updated` / `redirection_redirect_deleted` / `redirection_monitor_created`, який пише **хто саме** створив правило, і читання цього журналу монітором. Це єдиний спосіб дати ім'я людини замість вікна часу.
- `TODO(task-NN)`: Cloudflare Rulesets + Audit Logs як окреме джерело (там є і автор, і час).
