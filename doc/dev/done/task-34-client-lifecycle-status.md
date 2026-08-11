# task-34 — Статус клієнта: `active` / `paused` (припинена оплата)

- **Пакет:** у клієнта з'являється **комерційний статус** (`active` | `paused`), який видно на картці дашборду й на сторінці клієнта, який перемикається з UI за два кліки і який **сам виключає паузованого клієнта з масових операцій** (Refresh data «на всіх», Run audits «на всіх», CLI-беkфіл, portfolio-агрегати).
- **Хвиля:** G — операційна ергономіка (після task-30/31/32/33).
- **Depends on:** — (адитивно розширює закриті зони task-02 / 03 / 05 / 06 / 07 / 12 / 15 / 25 / 28)
- **PLAN.md:** §6 (модель даних, `clients`), §9.1–9.2 (доріжки збору, «all» expansion, `not_configured`), §10 (REST-конвенції), §12 (`None` ≠ `0`, дані не переписуються), §13 (безпека — статус не є секретом), §21.3 (сортування дашборду)
- **frontend/AGENTS.md:** обов'язковий (особливо §3 anti-slop, §5 «Data truth», §6 «Стани», §7 «Доступність», §8 «Форми», §9 «Sync/Progress», §14 «Копірайт»)
- **AISeoTools:** **змін не потребує** — статус живе виключно в нашій БД і не впливає на жоден adapter-контракт.

---

## Мета (робота оператора, яку закриваємо)

Клієнт припинив оплату. Сьогодні в системі немає жодного сліду цього факту: він так само в сітці, так само потрапляє в «Refresh data» на всіх, так само з'їдає провайдерські юніти й час на аудит, і оператор дізнається про паузу тільки з власної пам'яті.

Закриваємо: **поставив паузу один раз → бачиш це на картці щоразу → масові операції його більше не чіпають → зняв паузу одним кліком, історія при цьому ціла**.

---

## 1. Дослідження (факти, на яких стоїть план)

### 1.1 `Client.enabled` не годиться на роль «паузи»

[models.py:64](../../../app/db/models.py#L64) уже має `enabled: bool`, і спокуса переюзати його велика. Але семантика в коді інша: `ClientRepository.list_active()` ([clients.py:19](../../../app/db/repositories/clients.py#L19)) фільтрує саме за `enabled`, і **дашборд читає його ж** ([dashboard.py:816](../../../app/api/dashboard.py#L816)). Тобто `enabled=False` = клієнта **не видно взагалі** — а це протилежність вимозі «щоб я одразу бачив статус».

Висновок: `enabled` лишається як є (жорсткий архів/приховування рядка), статус — окреме поле. Обидва документуються поруч у моделі, щоб наступний агент не сплутав.

### 1.2 Масовий збір розгортається рівно в трьох місцях

| Точка | Файл | Що робить сьогодні |
|---|---|---|
| `POST /jobs/sync` з `client_ids="all"` | [jobs.py:200](../../../app/api/jobs.py#L200) | `clients.list_active()` |
| `POST .../tech-audit/runs` (batch) з `client_ids="all"` | [tech_audit.py:325](../../../app/api/tech_audit.py#L325) | `clients.list_active()`, а для явного списку ще й перевіряє `client.enabled` ([tech_audit.py:333](../../../app/api/tech_audit.py#L333)) |
| CLI `collect-daily-history` без `--client-ids` | [cli.py:158](../../../app/cli.py#L158) | `clients_repo.list_active()` |

Плюс два «читальні» споживачі того ж методу: `GET /analytics/portfolio-series` ([portfolio.py:123](../../../app/api/portfolio.py#L123)) і `backfill_health_history` ([dashboard.py:613](../../../app/api/dashboard.py#L613)). `imports.py:39` теж кличе `list_active()`, але лише щоб зібрати множину доменів для імпорту історії — паузованого клієнта звідти виключати не треба (імпорт нічого не витрачає й нічого не збирає наживо).

Тобто «виключити паузованих зі збору» — це **не наскрізний рефактор**, а точкова заміна методу в трьох місцях + два агрегати.

### 1.3 Фронт бере список клієнтів рівно з одного джерела

`sync-modal.js` будує обидва селектори (метрики й аудити) з `dashboard.clients` — [sync-modal.js:319](../../../frontend/js/sync-modal.js#L319) і [sync-modal.js:417](../../../frontend/js/sync-modal.js#L417), причому **всі чекбокси відмічені за замовчуванням**. Це означає: щойно `ClientCardDTO` понесе `status`, модалка зможе показати тег «Paused» і зняти галочку без жодного нового запиту.

`trends-page.js` власного пікера клієнтів не має — покладається на серверний дефолт `portfolio-series`, тож достатньо змінити дефолт на бекенді.

### 1.4 Сортування «паузовані внизу» не потребує правки `sort_clients`

`sort_clients` ([health.py:160](../../../app/metrics/health.py#L160)) віддає вже впорядкований список, а `list.sort`/`sorted` у Python **стабільні**. Тому фінальний прохід `sorted(cards, key=is_paused)` у [dashboard.py:841](../../../app/api/dashboard.py#L841) опускає паузованих униз, повністю зберігаючи порядок усередині кожної групи для всіх п'яти режимів сортування. Нуль змін у зоні task-15.

### 1.5 Взірець UI для діалогу вже є

`integration-dialog` в [index.html:184](../../../frontend/index.html#L184) — готовий патерн `<dialog>` + `dialog-shell--compact` + footer із `data-close`, з робочим поверненням фокуса на тригер ([dashboard.js:447](../../../frontend/js/dashboard.js#L447)). Діалог статусу копіює цю ж механіку, тому новий CSS зводиться до бейджа й приглушеної картки.

### 1.6 Голова міграцій

`b7d21e9f4c83` (cell_browser_slot) — головна ревізія. Нова ревізія чіпляється down_revision-ом саме до неї.

---

## 2. Обсяг (scope)

**Входить:**

1. Поле статусу в БД (+ дата зміни + необов'язкова нотатка «чому») і міграція.
2. `ClientStatus` в `app/schemas/enums.py`, статус у `ClientDTO` і `ClientCardDTO`.
3. Ендпоінт зміни статусу + фільтр `client_status` на дашборді.
4. Виключення паузованих із масових операцій (soft-політика, §3.4).
5. UI: бейдж-кнопка на картці й у шапці сторінки клієнта, спільний діалог, приглушена картка, паузовані внизу сітки, теги в sync-модалці.
6. Тести всіх рівнів + фікстури.

**Не входить (свідомо):**

- Історія змін статусу (журнал «хто і коли поставив паузу»). Достатньо `status_changed_at` + нотатки; повний аудит-лог — окрема таска, якщо знадобиться.
- Автоматична пауза за розкладом/за фактом неоплати. Інтеграції з білінгом у проєкті немає.
- Видалення чи архівація даних паузованого клієнта. Дані лишаються недоторканними (PLAN §12) — пауза стосується **збору**, не історії.
- Третій стан (`churned`/`archived`). Для цього вже є `enabled=False`; множити стани без запиту не будемо.

---

## 3. Рішення

### 3.1 Модель даних

`app/schemas/enums.py` (адитивно, поруч із `Provider`/`PeriodKind`):

```python
class ClientStatus(StrEnum):
    """Комерційний стан співпраці — НЕ технічний стан збору.

    `paused` = оплата припинена: клієнт лишається видимим і зберігає всю
    історію, але випадає з масових операцій. Технічне приховування рядка —
    це окремий `Client.enabled`, який керує видимістю взагалі.
    """

    active = "active"
    paused = "paused"
```

`app/db/models.py`, у `Client` (три адитивні колонки):

```python
status: Mapped[ClientStatus] = mapped_column(_str_enum(ClientStatus, 16), default=ClientStatus.active)
status_changed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
status_note: Mapped[str | None] = mapped_column(String(300), default=None)
```

- `status_changed_at` — щоб бейдж чесно писав «Paused · since 12 Jul», а не просто «Paused». `NULL` для клієнтів, статус яких ніколи не міняли (тоді бейдж просто «Active» без дати — `NULL` ≠ дата створення, вигадувати не будемо).
- `status_note` — вільний текст оператора («payment stopped 01.07», «pause until September»). Не секрет, тому в БД і в UI лежить відкрито (PLAN §13 стосується ключів/паролів/куків).
- Індекс на `status` **не** заводимо: у таблиці півтора десятка рядків, і кожен запит однаково читає її повністю. Це рішення фіксуємо коментарем, щоб його не «виправили» мимохідь.

**Міграція** `migrations/versions/<rev>_client_status.py`, `down_revision = "b7d21e9f4c83"`:

- `batch_alter_table("clients")` (SQLite не вміє `ALTER COLUMN`) → три `add_column`; `status` як `String(16)`, `nullable=False`, `server_default="active"`.
- Явний `UPDATE clients SET status='active' WHERE status IS NULL` після додавання — щоб міграція була самодостатньою, а не залежала лише від `server_default`.
- `downgrade()` — `drop_column` усіх трьох.

### 3.2 Репозиторій

`app/db/repositories/clients.py`:

- `list_active()` — **сигнатура й поведінка не змінюються** (`enabled=True`, включно з паузованими). Дописуємо докстрінг, щоб ім'я більше нікого не вводило в оману: «active» тут = «не прихований», а не «не на паузі».
- **Новий** `list_collectable() -> list[Client]` — `enabled=True AND status == active`. Це метод для всіх, хто розгортає «all» у реальний збір.
- **Новий** `set_status(client_id, status, *, note=None, now=None) -> Client` — пише `status`, `status_changed_at=now` **лише якщо статус справді змінився**, і `status_note` (порожній рядок → `NULL`). Один шлях запису статусу = одна точка, де ставиться дата.

### 3.3 API

**DTO** (`app/schemas/api.py`, адитивно):

- `ClientDTO` += `status: ClientStatus = active`, `status_changed_at: datetime | None = None`, `status_note: str | None = None`.
- `ClientCardDTO` += ті самі три поля — картка мусить рендеритись без другого запиту.
- `ClientCreateRequest` += `status: ClientStatus = ClientStatus.active`.
- `ClientUpdateRequest` **не** отримує `status`: інакше з'явиться другий шлях запису, який обходить логіку `status_changed_at`. Статус міняє тільки свій ендпоінт.
- Новий `ClientStatusRequest`: `status: ClientStatus`, `note: str | None = Field(default=None, max_length=300)`, `extra="forbid"`.

**Ендпоінти** (`app/api/clients.py`):

- `POST /api/v1/clients/{client_id}/status` → `ClientDTO`, `404 client_not_found`. Окрема дія замість `PATCH`, бо це зміна стану співпраці, а не редагування полів — і саме тут ставиться `status_changed_at`.
- `GET /api/v1/dashboard` += `client_status: Literal["active","paused","all"] = "all"`. Дефолт `all`, бо сітка показує паузованих завжди (рішення користувача); параметр існує, щоб фільтр Active/Paused у UI і глибокі посилання працювали.
- `_to_client_dto` / `_build_client_card` дописують три нові поля.
- Фінальний прохід у `get_dashboard`: після `sort_clients(cards, sort)` — стабільний `sorted(..., key=lambda c: c.status == ClientStatus.paused)`. Паузовані завжди внизу, у будь-якому режимі сортування.

### 3.4 Політика збору — **soft**

Правило одним реченням: **«all» ніколи не включає паузованого; явний вибір оператора — включає.**

| Точка | Було | Стає |
|---|---|---|
| `POST /jobs/sync`, `client_ids="all"` | `list_active()` | `list_collectable()` |
| `POST .../tech-audit/runs`, `client_ids="all"` | `list_active()` | `list_collectable()` |
| CLI `collect-daily-history` без `--client-ids` | `list_active()` | `list_collectable()`; новий флаг `--include-paused` повертає стару поведінку |
| `GET /analytics/portfolio-series` без `client_ids` | `list_active()` | `list_collectable()` — паузований клієнт не має тягнути портфельні агрегати вниз |
| `backfill_health_history` | `list_active()` | `list_collectable()` — нових точок для паузованого не пишемо; **наявні не чіпаємо** |
| явний `client_ids=[...]` із паузованим | дозволено | **лишається дозволеним**, без 409 |

Явний список не блокуємо навмисно: оператор іноді має подивитись свіжі дані по паузованому клієнту (перемовини про поновлення), і змушувати його спершу знімати паузу — це та сама «лишня робота». Запобіжник ставимо на рівні UI (§3.5): чекбокс знято, тег «Paused» видно, разовий підтверджувальний крок.

Відповідь `POST /jobs/sync` додає `skipped_paused_client_ids: list[str]` (порожній за замовчуванням) — коли «all» відкинуло паузованих, фронт має що показати замість тихого зникнення. Так само для tech-audit batch, де вже є взірець `skipped_active_client_ids` ([sync-modal.js:554](../../../frontend/js/sync-modal.js#L554)).

### 3.5 Frontend

Bump версії ассетів у всіх імпортах і `<link>`/`<script>`: `?v=20260803-connect4` → `?v=20260805-status1` (інакше браузер віддасть старі модулі — перевірено на task-33).

**Бейдж на картці** (`client-card.js`, у `client-card__meta` поруч зі `status-label`):

```
[⏸ Paused · since 12 Jul ▾]     ← <button>, відкриває діалог
[● Active ▾]                     ← той самий контрол для активного
```

- Не колір-онлі: іконка (`player-pause` / `circle-check`) + текст (AGENTS §7).
- `article.client-card` отримує `data-client-status="paused"` → приглушення (нижча opacity вмісту, ненасичена рамка) **без втрати читабельності чисел** (AGENTS §3: контраст тексту лишається в нормі; приглушуємо хром, не дані).
- Кнопка `Refresh` на паузованій картці стає `button--quiet` і веде через підтвердження («LAXTON is paused. Collect data anyway?»), `History` — без змін.
- Нотатка, якщо є, — рядком під бейджем (`Payment stopped 01.07`), без обрізання по середині слова.

**Діалог `#client-status-dialog`** (новий у `index.html`, за взірцем integration-dialog): radio `Active` / `Paused`, текстове поле `Note (optional)`, кнопки `Cancel` / `Save`. Логіка — новий модуль `frontend/js/client-status.js` (`openClientStatusDialog({ client, trigger, onSaved })`), щоб і дашборд, і сторінка клієнта викликали один код. Після збереження — `loadDashboard({ quiet: true })` / перерендер шапки клієнта + тост «LAXTON is paused. Bulk collections will skip it.».

**Сторінка клієнта** (`client-page.js`): той самий бейдж-кнопка в шапці поруч із `Refresh live data`; на паузі — рядок пояснення «Paused since 12 Jul — bulk collections skip this client. Its history is untouched.»

**Фільтр статусу** (`index.html`, секція `.filter-bar` + `dashboard.js` + `store.js`): сегментований контрол `All / Active / Paused` з лічильниками, за взірцем `group-filter`; стан у `store.js` як `clientStatus` з URL-параметром `client_status` (дефолт `all` не серіалізується — так само, як `group=all`). Рядок `#result-count` при активному фільтрі пише «12 clients · 3 paused hidden».

**Sync-модалка** (`sync-modal.js`): у обох селекторах (метрики [319](../../../frontend/js/sync-modal.js#L319) і аудити [417](../../../frontend/js/sync-modal.js#L417)) паузований клієнт рендериться з тегом `Paused` і **без** `checked`; пресети («all», «never audited») його не додають; підпис під списком — «3 paused clients are not selected». Ручна галочка працює (soft-політика).

---

## 4. Owned paths

| Шлях | Що робимо |
|---|---|
| `app/schemas/enums.py` | + `ClientStatus` (адитивно; зона task-14 — контрактна зміна, див. §7) |
| `app/schemas/api.py` | + поля в `ClientDTO`/`ClientCardDTO`/`ClientCreateRequest`, + `ClientStatusRequest`, + `skipped_paused_client_ids` |
| `app/db/models.py` | + 3 колонки в `Client` |
| `migrations/versions/<rev>_client_status.py` | нова ревізія |
| `app/db/repositories/clients.py` | + `list_collectable()`, + `set_status()`, докстрінг до `list_active()` |
| `app/api/clients.py` | + `POST /clients/{id}/status`, поля в DTO-мапері |
| `app/api/dashboard.py` | + `client_status`-фільтр, поля картки, паузовані внизу, `backfill_health_history` |
| `app/api/jobs.py`, `app/api/tech_audit.py`, `app/api/portfolio.py`, `app/cli.py` | заміна `list_active()` → `list_collectable()` у «all»-розгортанні (+ `--include-paused`) |
| `frontend/index.html` | + діалог статусу, + сегмент фільтра, bump `?v=` |
| `frontend/js/{client-card,dashboard,client-page,sync-modal,store,api}.js`, `frontend/js/client-status.js` (новий) | UI статусу |
| `frontend/css/app.css`, `frontend/css/client-page.css` | бейдж, приглушена картка, діалог |
| `frontend/fixtures/dashboard-all-states.json` | + паузований клієнт |
| `tests/unit/{db,api,schemas,cli}/…` | тести (див. §6) |

Поза зоною не чіпаємо нічого: `app/metrics/health.py` (`sort_clients`) лишається як є — паузованих опускає зовнішній стабільний прохід.

---

## 5. Definition of Done / Gate

- [ ] `docker compose exec api poetry run alembic upgrade head` проходить на **наявній** БД; усі існуючі клієнти після міграції — `active`, `status_changed_at IS NULL`.
- [ ] `downgrade` міграції теж проходить (перевірено на копії БД).
- [ ] `POST /clients/{id}/status` ставить паузу, повертає `ClientDTO` з `status_changed_at`; повторний виклик із тим самим статусом **не** зсуває дату.
- [ ] «Refresh data» без вибору клієнтів і «Run audits» без вибору не створюють жодної клітинки для паузованого клієнта; у відповіді видно `skipped_paused_client_ids`.
- [ ] Явно відмічений паузований клієнт **збирається** нормально (soft-політика).
- [ ] `GET /analytics/portfolio-series` без `client_ids` не містить паузованих; із явними `client_ids` — містить.
- [ ] Дашборд: паузовані завжди внизу в усіх п'яти режимах сортування; порядок усередині груп не змінився (порівняння з попереднім релізом).
- [ ] Бейдж читабельний і не колір-онлі; діалог відкривається з картки й зі сторінки клієнта, повертає фокус на тригер, закривається Esc (AGENTS §7/§8).
- [ ] Дані паузованого клієнта на місці: історія, метрики, аудити, алерти відкриваються без змін (PLAN §12).
- [ ] `ruff check .`, `mypy app`, повний `pytest` — зелені (у Docker).
- [ ] Чекліст `frontend/AGENTS.md §16` пройдено, візуальна перевірка за §15 (Playwright у контейнері `api`, polling замість `wait_for_function` — CSP).

---

## 6. Тести

| Рівень | Файл | Що покриває |
|---|---|---|
| unit/db | `tests/unit/db/test_clients.py` | дефолт `active` на `create`; `set_status` ставить/не зсуває дату; `note=""` → `NULL`; `list_collectable` виключає `paused` і `enabled=False`, `list_active` — лише `enabled=False` |
| unit/schemas | `tests/unit/schemas/test_enums.py` | вичерпний набір `ClientStatus` (там уже є цей патерн для `CellStatus`) |
| unit/api | `tests/unit/api/test_clients.py` | `POST /status` 200/404/422 (`extra="forbid"`, `note` > 300 символів); `ClientDTO` несе нові поля |
| unit/api | `tests/unit/api/test_dashboard.py` | `client_status=active\|paused\|all`; паузовані внизу в кожному режимі `sort`; картка несе `status` |
| unit/api | `tests/unit/api/test_jobs.py` | `"all"` не бере паузованого + `skipped_paused_client_ids`; явний список бере |
| unit/api | `tests/unit/api/test_tech_audit.py` | те саме для batch-аудиту |
| unit/api | `tests/unit/api/test_portfolio.py` | дефолт без паузованих; явні `client_ids` — з ними |
| unit/cli | `tests/unit/cli/…` | дефолт виключає паузованих; `--include-paused` повертає |
| міграція | `tests/unit/db/test_migrations.py` (або наявний еквівалент) | upgrade/downgrade на порожній і на заповненій БД |
| фронт | `frontend/fixtures/dashboard-all-states.json` + візуальний прогін | картка на паузі, бейдж, фільтр, sync-модалка зі знятою галочкою |

---

## 7. Контрактні зміни (оголосити наперед)

- **`app/schemas/enums.py`** — новий `ClientStatus`. Зона task-14, тому зміна фіксується тут і синхронно дописується в `task-02-shared-contracts.md` (розділ «Enums»).
- **`ClientDTO` / `ClientCardDTO`** — три нові поля з дефолтами: адитивно, наявні споживачі не ламаються.
- **`ClientUpdateRequest`** — навмисно **без** `status` (єдиний шлях запису — новий ендпоінт).
- **`SyncJobAcceptedDTO`** (і аудитний аналог) — нове поле `skipped_paused_client_ids` із дефолтом `[]`.
- **Семантика `list_active()` не змінюється** — щоб жоден із п'яти наявних викликів не змінив поведінку мовчки.

---

## 8. Ризики

| Ризик | Пом'якшення |
|---|---|
| Плутанина `enabled` vs `status` у наступних тасках | Коментар у моделі, докстрінг у `list_active`/`list_collectable`, явний абзац у Completion report |
| Оператор ставить паузу й дивується, чому «Refresh data» мовчки пропустив клієнта | `skipped_paused_client_ids` + тост у модалці + підпис «3 paused clients are not selected» |
| Приглушена картка стає нечитабельною | AGENTS §3: приглушуємо хром (рамка, іконки, фон), не цифри; контраст тексту перевіряємо на етапі §15 |
| Старі модулі з кешу браузера після релізу | Bump `?v=` в усіх імпортах — окремим першим комітом, як у task-33 |
| Пауза сприймається як «видалення даних» | Копірайт прямо каже: «History is untouched. Bulk collections skip this client.» |

---

## 9. Follow-ups / борг

- Журнал змін статусу (хто/коли/чому) — якщо знадобиться звітність по відтоку.
- Автопідказка «цей клієнт не збирався 60 днів — поставити паузу?» на дашборді.
- Фільтр статусу на `#/trends` (зараз сторінка не має власного пікера клієнтів).
- Розділити `paused` і `churned`, якщо з'явиться потреба відрізняти «тимчасово» від «пішов назавжди».

---

## ✅ Completion report

- **Дата:** 2026-08-11
- **Гілка / комміт:** `task-34-client-lifecycle-status` (на момент закриття — незакомічені зміни поверх `4e76da6`)
- **Статус:** done

### Що зроблено

Комерційний статус клієнта `active`/`paused` живе в БД (три адитивні колонки), керується окремим ендпоінтом
`POST /clients/{id}/status`, видно на картці дашборду й на сторінці клієнта (бейдж-кнопка + приглушена картка +
нотатка), і виключає паузованого клієнта з усіх п'яти «all»-розгортань (sync-джоби, batch-аудит, CLI-беkфіл,
`GET /analytics/portfolio-series`, `backfill_health_history`) — крім явного вибору оператора, який завжди
проходить. Дашборд опускає паузованих у самий низ у всіх п'яти режимах сортування й підтримує фільтр
`All / Active / Paused` з лічильниками. Sync-модалка (обидва селектори) показує паузованих із тегом і знятою
галочкою. Уся робота з §2 «Входить» закрита.

### Як зроблено (ключові рішення)

- `status` — окреме поле, `Client.enabled` не займаний: `enabled=False` ховає рядок узагалі, `status=paused`
  навпаки має лишатись помітним. Обидва задокументовані поруч у моделі й у докстрінгах `list_active`/
  `list_collectable`, щоб наступний агент не переплутав.
- Один шлях запису статусу — `ClientRepository.set_status()`; `status_changed_at` зсувається лише за фактичної
  зміни значення (повторне «paused → paused», наприклад правка нотатки, дату не чіпає). `ClientUpdateRequest`
  свідомо без `status` — інакше з'явився б другий шлях запису з тим самим ефектом.
- Soft-політика на «all»: `list_collectable()` (enabled ∧ active) підмінює `list_active()` рівно в точках
  розгортання «all» (jobs, tech_audit, cli, portfolio, backfill); явний `client_ids=[...]` із паузованим
  лишається дозволеним без 409. Відповіді `POST /sync-jobs` і `POST /tech-audit/runs` додатково повертають
  `skipped_paused_client_ids`, і коли «all» відкидає геть усіх — окремий код (`all_clients_paused`), щоб
  оператор бачив причину, а не «клієнтів немає».
- Головні файли: [app/db/repositories/clients.py](../../app/db/repositories/clients.py) — `list_collectable`/
  `set_status`; [app/api/dashboard.py](../../app/api/dashboard.py) — фільтр + стабільний прохід «паузовані
  внизу» після `sort_clients` (зона task-15 не чіпалась); [frontend/js/client-status.js](../../frontend/js/client-status.js) —
  один модуль на бейдж/діалог, який використовують і дашборд, і сторінка клієнта.
- Відхилення від плану: §6 описував тести per-endpoint файлами (`test_jobs.py`, `test_tech_audit.py`,
  `test_portfolio.py`); реалізація натомість звела наскрізні сценарії паузи в один файл
  [tests/unit/api/test_client_status_paused.py](../../tests/unit/api/test_client_status_paused.py) — одна ідея
  («all» ніколи не бере паузованого, явний вибір — бере) перевіряється по черзі для кожної точки. Модульні тести
  (`test_clients_api.py`, `test_clients.py`, `test_cli.py`, `test_enums.py`) лишились per-owner, як і планувалось.

### Контрактні зміни (Contract changes)

- `app/schemas/enums.py` — новий `ClientStatus` (`active`/`paused`). `task-02-shared-contracts.md` синхронно не
  оновлювався в межах цієї сесії — **follow-up**, якщо `task-02` ще не бачив цей enum.
- `ClientDTO` / `ClientCardDTO` — `status`, `status_changed_at`, `status_note` з дефолтами; адитивно.
- `ClientCreateRequest` += `status`; `ClientUpdateRequest` навмисно без `status`.
- Новий `ClientStatusRequest` (`extra="forbid"`).
- `SyncJobAcceptedDTO` і `TechAuditBatchAcceptedDTO` += `skipped_paused_client_ids: list[str] = []`.
- `list_active()` — сигнатура й поведінка **не змінились** (жоден із п'яти наявних викликів не зламався мовчки).

### Як перевірити (verification)

```bash
docker compose exec api poetry run pytest -q --deselect tests/contract/test_aiseotools_signatures.py::test_public_surface_matches_snapshot
docker compose exec api poetry run ruff check .
docker compose exec api poetry run mypy app
docker compose exec api poetry run alembic upgrade head   # на копії БД: downgrade b7d21e9f4c83 → upgrade head
```

`test_public_surface_matches_snapshot` падає і на `main` без цих змін — дрейф сигнатур сусіднього чекауту
AISeoTools (`BrowserSession.urls`, `TechVerdict.spelling_reviews`, `TechMachineFindings.spelling_review`),
жодного стосунку до task-34; поза owned paths цієї таски.

UI перевірено Playwright-ом усередині контейнера `api` (fixture-режим `?fixture=all-states`, live-режим проти
`GET /api/v1/dashboard`): бейдж не колір-онлі (іконка+текст), картка паузованого клієнта приглушена й опускається
в низ сітки в кожному з п'яти режимів сортування, фільтр `All/Active/Paused` з лічильниками й текстом
«N paused hidden» під фільтром «Active», sync-модалка показує тег «Paused» і знімає галочку без чекбоксу для
паузованого в обох селекторах, діалог статусу відкривається і з картки, і з шапки сторінки клієнта, закривається
`Esc`, повертає фокус на тригер. Дані паузованого клієнта (history/alerts/tech-audit) читаються без змін.

### Тести

- `tests/unit/db/test_clients.py` — дефолт `active` без дати; `set_status` ставить/не зсуває дату; `note=""` →
  `NULL`; `list_collectable` виключає `paused` і `enabled=False`; `list_active` лишає паузованих; unknown-client
  → `LookupError`.
- `tests/unit/schemas/test_enums.py` — вичерпний набір `ClientStatus`.
- `tests/unit/schemas/test_contract_snapshot.py` / `data/contract_snapshot.json` — `ClientStatus` і
  `ClientStatusRequest` тепер у знімку контракту (були імпортовані, але не зареєстровані — виправлено при
  закритті таски, інакше знімок мовчки не покривав новий контракт).
- `tests/unit/api/test_clients_api.py` — `POST /status` 200/404/422 (`extra="forbid"`, note > 300 символів,
  невідоме значення), `PATCH` не приймає `status`, зміна статусу переживає read-back.
- `tests/unit/api/test_client_status_paused.py` (новий) — sync-jobs/tech-audit `"all"` пропускають паузованого +
  `skipped_paused_client_ids`, явний список бере його; «всі паузовані» → окремий код помилки; dashboard
  фільтр/сортування/картка; portfolio-series дефолт vs явний `client_ids`.
- `tests/unit/importers/test_cli.py` — `backfill-daily-history` за замовчуванням пропускає паузованих,
  `--include-paused` повертає стару поведінку.
- `tests/unit/db/test_alembic_migrations.py` (наявний, не змінювався) — `upgrade head` → `downgrade base` →
  `upgrade head` на порожній БД; тепер проходить крізь нову ревізію `c9f45a1b3e72`. На заповненій БД (14
  клієнтів, 2 з них вручну паузовані під час розробки) upgrade/downgrade/re-upgrade перевірено вручну на копії —
  колонки додаються й дропаються чисто, `status_changed_at` після re-upgrade знову `NULL` для всіх (очікувано:
  downgrade незворотно губить поле).

### Follow-ups / борг

- Синхронізувати `ClientStatus` у `task-02-shared-contracts.md`, якщо документ ще не оновлений.
- Решта — без змін, див. §9 вище.
