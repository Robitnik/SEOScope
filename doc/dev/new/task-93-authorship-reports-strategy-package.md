# task-93 — Автори дій: Reports і Strategy Package Check (видно лише адміну)

- **Пакет:** Auth / audit — атрибуція створених артефактів
- **Depends on:** task-71 ✅ (користувачі, ролі, `Actor`), task-72 ✅ (журнал дій), task-73 ✅ (dev-токен, localhost-байпас), task-79 ✅ (особисті токени), task-42/43/45/47/49 ✅ (звіти, батчі), task-90/91/92 ✅ (Strategy Package Check)
- **Owned paths (нові):** `app/authorship/{__init__,model,registry,backfill}.py`, `app/db/repositories/authorship.py`, `app/api/authorship.py`, `app/schemas/authorship.py`, `migrations/versions/*_authorship_task_93.py`, `frontend/js/authorship.js`, `tests/unit/authorship/**`, `tests/unit/api/test_authorship_api.py`, `tests/e2e/frontend/reports-authorship.cjs`
- **Адитивні дотики чужих зон (оголосити в Completion report):** `app/db/models.py` (6 колонок), `app/db/repositories/{reports,content_qa_packages}.py` (kw-only `created_by`, фільтр у `list_*`), `app/reports/batch.py` (`create_batch`/`rebuild_run` пробрасують автора), `app/api/reports.py`, `app/api/tools_strategy_package.py` (запис автора + фільтр списку), `app/api/deps.py` (`AttributionDep`), `app/core/auth/policy.py` (префікс у `DEFAULT_ADMIN_ONLY_PATHS`), `app/audit/{middleware,actions}.py` (id створеного ресурсу), `app/cli.py` (`report-run` → `cli`, нова команда `authorship-backfill`), `frontend/js/{reports-page,reports-run,reports-batch,strategy-package-tool,admin-audit-page,router,tools-page}.js`, відповідні CSS, `tests/unit/api/data/openapi_snapshot.json`

---

## Мета (робота, яку закриваємо)

Адмін відкриває `#/reports` або список Strategy Package Check і бачить **хто
запустив кожен звіт / перевірку**, може відфільтрувати «усе, що робила Olha», а
зі сторінки звіту перейти в журнал дій і побачити решту: хто скасовував,
перезапускав період, перегенеровував клієнтський PDF.

Member і viewer **нічого з цього не бачать і не можуть витягти** — ні в UI, ні
через API, ні через MCP, ні через фільтр як оракул, ні через кеш тунелю.

---

## 1. Дослідження (заміряно живо 2026-09-14 на бойовій Postgres, лише SELECT)

### 1.1 Сьогодні автора немає ніде в доменних таблицях

`report_runs`, `report_batches`, `content_qa_packages` не мають жодного поля про
актора ([models.py:1246](../../../app/db/models.py#L1246),
[models.py:1265](../../../app/db/models.py#L1265),
[models.py:663](../../../app/db/models.py#L663)). Єдиний `created_by_id` у схемі —
`users.created_by_id`. Жоден ендпоінт звітів чи пакета не бере `ActorDep`
([reports.py:286](../../../app/api/reports.py#L286),
[tools_strategy_package.py:426](../../../app/api/tools_strategy_package.py#L426)) —
`ActorDep` сьогодні використовують лише `auth.py`, `me.py`, `oauth_consent.py`,
`admin_*`.

### 1.2 Журнал дій уже знає «хто», але не знає «що саме створено»

`audit_events` пише `report.run`, `report.batch_start`, `report.rebuild`,
`tool.strategy_package.run` з повним актором
([actions.py:110-180](../../../app/audit/actions.py#L110-L180)). Але для
створювальних маршрутів `resource_id = NULL`: middleware бере id лише з
path-параметра ([middleware.py:168-173](../../../app/audit/middleware.py#L168-L173)),
а в `POST /reports/runs` id нового рядка ще не існує. Тобто з журналу не можна
перейти до конкретного звіту, і фільтр `#/admin/audit?resource_id=<run>` не
показує подію створення.

Журнал **не годиться як єдине джерело авторства**:
- ретенція `audit_retention_days=365` — через рік автор зникне;
- `AuditSink` — обмежена черга, при переповненні рядки відкидаються (лічильник у `/healthz`);
- `audit_enabled` можна вимкнути;
- немає id створеного ресурсу (див. вище).

### 1.3 Хто насправді діє в системі — не лише люди через форму входу

| `actor_kind` у журналі | `actor_id` | Що це | Рядків |
|---|---|---|---|
| `local` | `local` | localhost-байпас (task-73), роль admin, людини не видно | 744 |
| `agent` | `agent` | dev bearer-токен (task-73), агентські сесії | 209 |
| `agent` | GUID людини | особистий токен / MCP OAuth (task-78/79) — **реальна людина** | 194 + 16 |
| `user` | GUID людини | звичайний логін формою | решта |

Висновки для дизайну:
- `kind="agent"` сам по собі не каже, людина це чи dev-токен — розрізняє лише
  `actor_id` ([sessions.py:139](../../../app/core/auth/sessions.py#L139): dev-токен
  завжди `user_id="agent"`, особистий — справжній GUID).
- `local` і dev-`agent` **не мають рядка в `users`** → FK на `users` для них
  неможливий, потрібен окремий `kind`.
- MCP-тул `run_client_report` ходить у той самий `POST /api/v1/reports/runs` з
  токеном того, хто питає ([apiclient.py](../../../app/mcp/apiclient.py)) —
  атрибуція через MCP вийде автоматично, окремої роботи не треба.

### 1.4 Обсяги й відновлюваність заднім числом

| Таблиця | Рядків | Від | Відновлюється з журналу | Лишиться `unknown` |
|---|---|---|---|---|
| `content_qa_packages` | 44 | 2026-09-09 | **43** | 1 (перший пакет, 2026-09-09 09:36, створений у dev напряму, не через HTTP) |
| `report_runs` | 41 (23 standalone, 18 у батчах) | 2026-08-18 | **27** | 14 (створені до старту журналу 2026-09-06) |
| `report_batches` | 2 | 2026-08-19 | **1** (09-07, `local`) | 1 (08-19) |

Метод, перевірений запитом (не вгадуванням):
1. **Точний ключ** із редагованого тіла запиту в `details_json.request`:
   `file_id == content_qa_packages.source_file_id`, `client_id == report_runs.client_id`
   (тіло >64 КБ зберігається як `{"body_bytes": N}` — тоді ключа немає, лише час).
2. **Часове вікно:** `row.created_at ∈ [audit.ts − duration_ms − 2с, audit.ts + 2с]`
   (рядок створюється всередині запиту, а `ts` ставиться після відповіді).
3. **Рішення:** заповнюємо, лише якщо серед кандидатів рівно **один різний
   актор** `(actor_kind, actor_id)`. Два кандидати одного актора (агент стріляв
   паралельно в одного клієнта — 8 таких звітів) — однозначно. Різні актори —
   лишаємо `NULL`.

На живих даних **жодного** рядка з двома різними акторами. Серед відновлених
авторів названа людина одна (`olhal@theyovth.com`), решта — `Local operator` і
`dev-agent`. Це чесна картина того, як тул використовувався до сьогодні, а не
дефект атрибуції (див. §7, питання 1).

### 1.5 Ризик витоку через кеш тунелю — підтверджений раніше живо

2026-09-06 (task-79) Cloudflare-тунель віддавав **закешовану** відповідь
`GET /api/v1/me/tokens` повз `no-store` і повз унікальний query-string
(пам'ять `cloudflare_static_asset_cache_bug`). Отже, якщо вбудувати автора в
`GET /reports/runs` залежно від ролі, відповідь адміна може дістатись member'у
з кешу. API сьогодні взагалі не ставить `Cache-Control` на JSON-відповіді —
лише статиці ([main.py:49](../../../app/main.py#L49)). Звідси рішення D3.

### 1.6 Що вже є і що перевикористовуємо

- `#/admin/audit` уже приймає `?resource_type=&resource_id=` з хешу
  ([admin-audit-page.js:53](../../../frontend/js/admin-audit-page.js#L53)) —
  посилання «Activity» зі сторінки звіту нічого нового в журналі не потребує, крім
  підтримки `?actor_id=` (адитивно).
- `isAdmin()` у [session.js:21](../../../frontend/js/session.js#L21).
- Двошаровий захист адмінських маршрутів (префікс у policy + `AdminActorDep` у
  сигнатурі) — усталений патерн `admin_users.py`.
- `admin_only_tool_slugs()` дивиться лише на префікси `/api/v1/tools/`
  ([policy.py:139](../../../app/core/auth/policy.py#L139)) — новий префікс
  `/api/v1/authorship` його не зачепить.

---

## 2. Рішення

### D1. Автор — атрибут рядка, не похідне з журналу

Журнал лишається джерелом «що ще робили з цим ресурсом» (cancel, period retry,
client PDF), але «хто створив» живе в самому рядку — з причин §1.2.

### D2. Дві колонки, словник — як у журналі

На `report_runs`, `report_batches`, `content_qa_packages`:

| Колонка | Тип | Зміст |
|---|---|---|
| `created_by_user_id` | `GUID NULL`, FK `users.id ON DELETE SET NULL` | реальна людина, якщо вона є |
| `created_by_kind` | `String(16) NULL` | `user` \| `agent` \| `local` \| `cli` \| `system`; **`NULL` = невідомо** |

- `NULL` ніколи не підміняється на `system` чи будь-що «правдоподібне» — той
  самий інваріант, що «відсутність ≠ 0». UI показує `Unknown`.
- Словник `kind` збігається з `audit_events.actor_kind` — фільтр і deep-link у
  журнал мапляться 1:1.
- Ім'я **не денормалізується**: користувачів не видаляють, JOIN дає актуальне
  ім'я й `is_active` (деактивований показується з позначкою). На відміну від
  журналу, тут потрібна поточна особа, а не знімок.
- Індекс `(created_by_user_id, created_at)` на `report_runs` і
  `content_qa_packages` — під фільтр. На `report_batches` не потрібен (2 рядки).

Модуль `app/authorship/model.py`:

```python
@dataclass(frozen=True)
class Attribution:
    kind: str | None          # None = unknown
    user_id: str | None

    @classmethod
    def from_actor(cls, actor: Actor) -> Attribution: ...
    @classmethod
    def cli(cls) -> Attribution: ...
```

`from_actor` — чиста функція без I/O: `kind` береться з `Actor.kind`,
`user_id` — лише якщо це валідний GUID (`"agent"`, `"local"`, `"system"`, `"cli"`
→ `None`). `AttributionDep` в `app/api/deps.py` поруч з `ActorDep`.

### D3. Лише адміну — окремий admin-only ендпоінт, не поле в наявних DTO

```
GET /api/v1/authorship?resource_type=report_run&ids=<id>,<id>     (≤200 ids)
GET /api/v1/authorship/facets?resource_type=report_run,report_batch
```

Чому не поле `created_by` у `ReportRunDTO` з умовою на роль:
1. **Кеш тунелю** (§1.5): тіло `GET /reports/runs` лишається однаковим для всіх
   ролей — нічого рольового закешувати неможливо.
2. **MCP і OpenAPI** не змінюються: DTO звітів/пакетів ті самі для всіх.
3. UI member'а **взагалі не робить** цього запиту.

Чому не під `/api/v1/admin/`: кожен GET там — audited read
([middleware.py:97](../../../app/audit/middleware.py#L97)), і кожен рендер
`#/reports` адміном писав би рядок у журнал. Тому префікс
`/api/v1/authorship` додається в `DEFAULT_ADMIN_ONLY_PATHS` (читання й запис), а
хендлер додатково бере `AdminActorDep`.

Відповідь несе `Cache-Control: private, no-store` і `Vary: Cookie, Authorization`.

Контракт:

```jsonc
// GET /api/v1/authorship?resource_type=report_run&ids=a,b
{
  "resource_type": "report_run",
  "items": [
    { "resource_id": "a",
      "author": { "kind": "agent",
                  "user": { "id": "…", "name": "Admin2", "email": "admin2@…", "is_active": true } } },
    { "resource_id": "b", "author": { "kind": "unknown", "user": null } }
  ]
}
```

- `kind ∈ user|agent|local|cli|system|unknown`. Id, якого не існує, у `items` відсутній (а не `unknown`).
- `resource_type` — реєстр в `app/authorship/registry.py`: `report_run`, `report_batch`, `content_qa_package`. Невідомий тип або >200 id → `422`.
- `facets` → `{ "authors": [ { "kind", "user", "count" } ] }` для випадаючого фільтра.

### D4. Фільтр за автором на наявних списках — теж лише адміну

Нові query-параметри `created_by_user_id` і `created_by_kind` (включно з
`unknown`) на `GET /reports/runs`, `GET /reports/batches`,
`GET /tools/strategy-package/reports`.

Non-admin із будь-яким із них → **`403`, а не тихе ігнорування**: фільтр — це
оракул («скільки звітів лишиться, якщо відфільтрувати за Olha»), тож ігнорувати
його мовчки безпечно лише доти, доки ніхто не змінить реалізацію. Відповідь без
цих параметрів — байт-у-байт однакова для всіх ролей (тест у DoD).

### D5. Хто вважається автором — по кожній дії

| Дія | Новий рядок | Автор |
|---|---|---|
| `POST /reports/runs` (UI, MCP, скрипт) | run | той, хто викликав |
| `POST /reports/batches` | batch + N дочірніх run | викликач — на батч **і** кожен дочірній run |
| `POST /reports/runs/{id}/rebuild` | новий run | **хто натиснув rebuild**, не автор джерела |
| `POST /reports/batches/{id}/clients/{cid}/retry` | новий дочірній run | хто натиснув retry; автор батча не змінюється |
| `POST /runs/{id}/cancel`, `/periods/{label}/retry`, `/client-pdf` | немає | не «автор» — видно в Activity (журнал) |
| `yovth-report report-run` (CLI, [cli.py:1235](../../../app/cli.py#L1235)) | run | `kind="cli"` |
| `POST /tools/strategy-package/check` | package | викликач |
| `/resolve`, `/inspect` пакета | немає | лише журнал |

`auth_enabled=False` (локальна розробка без входу) → `Actor.system()` → `kind="system"` — чесно.

### D6. Журнал отримує id створеного ресурсу

Хендлер створення кладе `request.state.audit_created_resource = (type, id)`;
`AuditMiddleware._record` бере його як `resource_type/resource_id`, а path-ресурс
(для rebuild/retry — джерело) переносить у `details.parent_resource`.
`request.state` — спільний `scope["state"]` для middleware і хендлера (так уже
працює `auth_session` у зворотному напрямку). `tool.strategy_package.run`
отримує `resource_type="content_qa_package"`.

Наслідок: «Activity» зі сторінки звіту показує й подію створення, а будь-який
майбутній бекфіл стане точним без часових вікон.

### D7. Бекфіл — окрема CLI-команда, не міграція

```
docker compose exec api poetry run yovth-report authorship-backfill            # dry-run за замовчуванням
docker compose exec api poetry run yovth-report authorship-backfill --apply
```

- Алгоритм §1.4 (ключ → вікно → рівно один актор). Для дочірніх run батча — автор батча, якщо run створений у вікні `report.batch_start`; пізніші — через `report.batch_retry`/`report.rebuild`.
- Чіпає лише рядки з `created_by_kind IS NULL`; **ніколи** не перезаписує вже атрибутований рядок.
- Друкує підсумок: заповнено / неоднозначно / без доказів / до старту журналу — по кожній таблиці.
- Міграція лишається чистою схемою (оборотна, без залежності від вмісту журналу).

### D8. Автор ніколи не потрапляє в клієнтські артефакти

`matrix.xlsx`, клієнтський PDF, portfolio PDF/XLSX, ZIP-архів PDF батча — жодного
імені співробітника. Ці файли йдуть клієнтам.

### D9. Фронт: авторство — вторинна інформація, яка ніколи не ламає сторінку

Спільний `frontend/js/authorship.js`:
- `loadAuthors(resourceType, ids)` — **нічого не робить, якщо `!isAdmin()`**; кешує за id, під час live-полу догружає лише нові id;
- `renderAuthor(author)` — один формат скрізь;
- помилка або `403` (роль змінили посеред вкладки) → колонка/чип просто не малюється, без state-panel: список звітів важливіший за автора.

UI-копірайт (англійською):

| `kind` + `user` | Показ | `title` |
|---|---|---|
| `user` | `Olha L.` | email |
| `agent` + user | `Admin2 · API token` | email |
| `agent` без user | `Dev agent token` | — |
| `local` | `Local operator` | `Signed in via localhost bypass` |
| `cli` | `CLI` | — |
| `system` | `System` | — |
| `unknown` | `Unknown` (muted) | `Created before author tracking` |
| деактивований | `Olha L. (inactive)` | email |

Ім'я людини — посилання на `#/admin/audit?actor_id=<id>`.

---

## 3. Етапи

### Етап 1 — запис автора (бек, без UI)

1. **Бекап** перед міграцією (прод на Postgres — `scripts/backup_sqlite.py` з CLAUDE.md тут уже не про ту БД):
   `docker compose exec db pg_dump -U yovth -Fc yovth > app_data/backups/pre_task93_$(date +%F).dump`
2. Міграція `*_authorship_task_93.py`: `op.add_column` ×6, `op.create_foreign_key` ×3 (іменовані), `op.create_index` ×2; `down_revision` — актуальний head на момент роботи (сьогодні `bef57326e291`, але task-89 може додати свою). Без `batch_alter_table`. Перевірити `upgrade → downgrade → upgrade` на тестовій БД до бойової.
3. `app/authorship/model.py` (`Attribution`), `AttributionDep`.
4. Репозиторії: `create_run(..., created_by: Attribution | None = None)`, `create_batch(...)`, `create_package(...)` — лише kw-only з дефолтом.
5. Проводка: `_create_run`, `_create_batch` → `create_batch` → дочірні run, `_rebuild_run` → `rebuild_run`, `_retry_batch_client`, `check_package`, CLI `report-run`.
6. D6: `audit_created_resource` у трьох створювальних хендлерах звітів і в `check_package`; middleware + `actions.py`.

**Gate 1:** unit-тести зелені; живо через dev-токен створити run і пакет → у Postgres `created_by_kind='agent'`, `created_by_user_id IS NULL`; рядок журналу має `resource_id` створеного run.

### Етап 2 — читання (API)

1. `app/db/repositories/authorship.py`: lookup за типом+id (один `SELECT … LEFT JOIN users`), facets (`GROUP BY kind, user_id`).
2. `app/api/authorship.py` + `app/schemas/authorship.py`; префікс у `DEFAULT_ADMIN_ONLY_PATHS`; заголовки кешу.
3. Фільтри D4 у трьох списках + `403` для non-admin.
4. Оновити `openapi_snapshot.json`.

**Gate 2 (контракт на origin, не через тунель):** матриця `curl` на `http://127.0.0.1:6524` з `X-Forwarded-For: 203.0.113.1` (щоб не спрацював localhost-байпас) і особистими токенами з ролями `admin`/`member`/`viewer` (адмін може видати собі токен зі стелею `viewer`):
- `/authorship` → 200 / 403 / 403;
- `/reports/runs?created_by_kind=agent` → 200 / 403 / 403;
- `/reports/runs` без фільтра → **ідентичні тіла** для трьох ролей.

Окремо через `https://seo.theyovth.space`: `curl -sI` на `/api/v1/authorship…` → `cf-cache-status` має бути `DYNAMIC`/`BYPASS`, не `HIT`. Якщо `HIT` — **стоп**, спершу Cache Rule для `/api/*` у Cloudflare (конфіг дашборда, дія оператора, не код).

### Етап 3 — бекфіл

1. `app/authorship/backfill.py` + команда `authorship-backfill`.
2. Dry-run на проді → числа мають збігтись із §1.4 (± рядки, створені після 2026-09-14 уже з автором): пакети 43 заповнено / 1 без доказів; run 27 / 14 до журналу; батчі 1 / 1. Розбіжність — баг алгоритму, не привід «підкрутити вікно».
3. Бекап → `--apply` → повторний dry-run показує 0 кандидатів.

### Етап 4 — фронт

Перед роботою — `frontend/AGENTS.md` повністю; скріншоти «до» на 1440 і 390.

1. `authorship.js`.
2. `#/reports` ([reports-page.js:388](../../../frontend/js/reports-page.js#L388)): колонка **Started by** лише для адміна (позицію обрати за скріншотом «до»: кандидат — одразу перед `Created`, бо читається разом із часом); `colspan` картки батча 9→10 для адміна ([reports-page.js:480](../../../frontend/js/reports-page.js#L480)); чип автора в картці батча; фільтр **Started by** у `.reports-filters` з facets обох типів, у `persist()` і в `filtersActive()`.
3. Сторінка run ([reports-run.js:241](../../../frontend/js/reports-run.js#L241)): у хедері `Started by … · <час>` + посилання **Activity** → `#/admin/audit?resource_type=report_run&resource_id=<id>`.
4. Сторінка батча ([reports-batch.js:155](../../../frontend/js/reports-batch.js#L155)): `Started by` у хедері; у рядку клієнта `Retried by …`, лише якщо автор дочірнього run ≠ автор батча.
5. Strategy Package Check: колонка **Checked by** у списку ([strategy-package-tool.js:597](../../../frontend/js/strategy-package-tool.js#L597)) + фільтр; чип у мета-рядку звіту поруч із датою ([strategy-package-tool.js:686](../../../frontend/js/strategy-package-tool.js#L686)).
6. `admin-audit-page.js`: читати `?actor_id=` з хешу (адитивно).
7. Cache-bust: `?v=` на кожному зміненому модулі **і на всіх ребрах імпорту до `index.html`** (`router.js` → reports-*, `tools-page.js` → strategy-package-tool, сам `router.js` у `<script src>`).
8. CSP: жодного inline `style=`/`.style.*`; нові кольори/відступи — лише через токени. Ширша таблиця — `.table-scroll` з `contain: paint`.

**Gate 4:** візуальний цикл + чекліст `AGENTS.md §15–16` на 1440 і 390; скріншоти адміна й member'а.

### Етап 5 — закриття

e2e, Completion report, рядок у `INDEX.md`, `graphify update .`.

---

## 4. Ризики й пастки

| Ризик | Що робимо |
|---|---|
| Закешована відповідь адміна дістається member'у через тунель | D3: рольові дані лише в окремому ендпоінті, списки рольово-інваріантні; `no-store` + `Vary`; Gate 2 перевіряє `cf-cache-status` |
| Фільтр як оракул авторства | D4: `403`, не ігнорування; тест |
| Playwright на `127.0.0.1` завжди бачить bypass-адміна, а не member'а (task-73) | Member-половину e2e ганяти через тунель тимчасовим QA-member'ом (деактивувати після); контракт `403` — `curl` на origin із `X-Forwarded-For` |
| `AuthMiddleware`/`AuditMiddleware` у тестах вимкнені autouse і пишуть у бойову БД, якщо ввімкнути без маркера | Кожен тест із ролями — `@pytest.mark.auth` |
| Дві pytest-сесії в одному контейнері вимивають рядки одна одної | Не запускати паралельно з іншою сесією; падіння, що «переїжджає», — спершу перевірити це |
| `docker compose run` перезапускає `alembic upgrade head` і може зіткнутися з чужою міграцією | Міграції — лише `docker compose exec api`, перевірити `alembic heads` перед створенням |
| Бекфіл «вгадає» автора там, де діяло двоє | Правило «рівно один актор»; інакше `NULL`; ніколи не перезаписувати |
| Автор у клієнтському файлі | D8 + тест, що XLSX/PDF-генератори не читають нових колонок |
| Робоче дерево ділиться з паралельною сесією (task-89) | Перед кожним `git add` — `git status`; спільні файли (`models.py`, `INDEX.md`, `router.js`) комітити лише своїми hunk'ами |

---

## 5. Definition of Done

1. Кожен новий run, батч (разом із дочірніми run), rebuild, batch-retry і пакет отримує автора — через форму входу, особистий токен, MCP, dev-токен, localhost-байпас і CLI; перевірено живо щонайменше через три з цих шляхів.
2. `GET /api/v1/authorship*` і фільтри за автором: admin 200, member/viewer 403 — перевірено на origin особистими токенами трьох ролей.
3. Тіла `GET /reports/runs`, `/reports/runs/{id}`, `/reports/batches`, `/reports/batches/{id}`, `/tools/strategy-package/reports`, `/reports/{id}` **ідентичні** для admin і member (автотест + живий `diff`).
4. Через тунель `cf-cache-status` для `/api/v1/authorship*` ≠ `HIT`.
5. Бекфіл застосовано на проді після бекапу; фактичні числа записано в Completion report і збігаються з §1.4.
6. Невідомий автор показується як `Unknown`, ніколи як `System` чи порожнє місце, яке читається як «ніхто».
7. UI: адмін бачить колонки/фільтри/чипи на `#/reports`, сторінці run, батча, у списку й звіті Strategy Package Check; member на тих самих сторінках — жодного сліду і **жодного запиту** до `/api/v1/authorship` (перевірка мережі в e2e).
8. Подія створення в журналі має `resource_id` створеного рядка; «Activity» зі сторінки run показує створення.
9. Клієнтські файли без імен співробітників (тест).
10. `ruff`, `mypy app`, повний `pytest` — у контейнері, зелені; openapi-снапшот оновлено.
11. Чекліст `frontend/AGENTS.md §15–16` на 1440 і 390, скріншоти в Completion report.

---

## 6. Тести

- `tests/unit/authorship/test_model.py` — `Attribution.from_actor`: cookie → `user`+id; особистий токен → `agent`+id; dev-токен → `agent`+`None`; байпас → `local`+`None`; `system`/`cli`; невалідний GUID ніколи не потрапляє в `user_id`.
- `tests/unit/authorship/test_backfill.py` — точний ключ; лише вікно; два кандидати одного актора → заповнено; два різні актори → `NULL`; немає доказів → `NULL`; рядок до журналу не чіпається; вже атрибутований не перезаписується; dry-run нічого не пише; дочірні run батча; rebuild/batch-retry.
- `tests/unit/api/test_authorship_api.py` (`@pytest.mark.auth`) — ролі; `422` на тип і >200 id; відсутній id не стає `unknown`; заголовки кешу; facets.
- Доповнення `tests/unit/api/test_reports_api.py` і тестів strategy-package — запис автора в кожній дії з D5; фільтри D4 для admin і `403` для member; **рольова інваріантність тіл списків**.
- `tests/unit/audit/` — створювальні маршрути пишуть `resource_id` створеного рядка, `details.parent_resource` для rebuild/retry.
- Міграція: upgrade/downgrade на тестовій БД.
- `tests/e2e/frontend/reports-authorship.cjs` (Node Playwright, `.cjs`) — адмін: колонка, фільтр звужує список, чип у хедері run, посилання Activity відкриває журнал із фільтром; member: немає колонки/фільтра/чипа, 0 запитів на `/api/v1/authorship`; 0 console errors; 390px без горизонтального скролу сторінки.

---

## 7. Відкриті питання до оператора

1. **Localhost-байпас безіменний.** 744 дії в журналі — `Local operator`; усе, що оператор робить на `localhost:6524`, і далі буде без імені. Варіант: прив'язати байпас до конкретного адмін-акаунта (`AUTH_LOCALHOST_USER_EMAIL`) — тоді автор стане людиною. Це зона task-73; у цьому плані **не робиться**, доки не буде рішення.
2. **Хвиля 2** (§8) — окремою таскою після того, як ця відпрацює, чи додати найближчі (Strategy Check / Framework Check) одразу сюди?
3. **Member бачить власне авторство** («My reports»)? Вимога сьогодні — лише адмін; план її тримає строго.

---

## 8. Хвиля 2 — «де ще можливо» (поза обсягом, той самий механізм)

Механізм масштабується одним рядком реєстру + двома колонками + kw-only
параметром у місці створення. Оцінка на живих даних:

| Таблиця | Рядків | Хто створює | Примітка |
|---|---|---|---|
| `content_qa_reports` (Strategy Check, Framework Check) | 22 | людина (`tool.strategy_check.run` 7 у журналі) | та сама родина Content QA — першою |
| `alt_text_reports` | 6 | людина | + редагування елементів — через журнал |
| `schema_markup_checks` | 6 | людина | |
| `page_inventory_snapshots` | 6 | людина (`pages.rebuild`) | |
| `url_screenshots` | 34 | змішано | |
| `redirect_checks` | 0 | людина | |
| `monitor_runs` (`trigger=manual`) | 234 | людина + планувальник | планові → `system` |
| `sync_jobs` | 920 | переважно планувальник (`job.start` лише 9) | |
| `archive_captures` | 308 | переважно планувальник | |
| `clients` | — | людина (`client.create` 31) | «хто завів клієнта» |

---

## 9. Follow-ups (свідомо поза обсягом)

- Cloudflare Cache Rule «bypass cache» для `/api/*` — загальна проблема (task-79 інцидент), конфіг дашборда, дія оператора.
- Загальний `Cache-Control: no-store` на всі JSON-відповіді API — зона task-01 (`app/main.py`).
- `INDEX.md` позначає task-90 і task-91 як ✅ з посиланнями на `../done/`, але файли досі лежать у `doc/dev/new/` — посилання биті; виправляє власник тих тасок.
