# task-31 — Паралельний збір: 3 браузерні слоти на одному профілі

- **Пакет:** усередині одного сервісу збір перестає бути строго послідовним — **N=3 клітинки одного провайдера
  йдуть одночасно** на **одному** логіні/профілі: 3 окремі вікна в теплому хостовому Chrome, 3 CDP-з'єднання,
  3 lane-потоки. Профільний лок із mutex-а стає **семафором** (`capacity`), клітинка отримує `slot`, модалка
  показує «3 доріжки» живцем.
- **Хвиля:** L (розширення після MVP)
- **Depends on:** **30** (спільний модуль модалки + живий прогрес v2 — панель доріжок будується на ньому)
- **PLAN.md:** §5.1 (single writer, короткі транзакції), §8.3 (профільний лок), §9.1 (паралельність між сервісами / послідовність усередині), §9.2 (recovery, leases), §12, §13 (секрети/безпека)
- **frontend/AGENTS.md:** обов'язковий для UI-частини.
- **AISeoTools:** **потрібні адитивні зміни** у `seo_audit/browser/{profile,session}.py` + прокидання `slot=` у `seo_audit/dashboard/{ahrefs,semrush}.py` (§3.6). Тільку keyword-only з дефолтами = поточна поведінка.

---

## Мета

Повний `force_refresh` портфеля зараз коштує **~45 хвилин** (живий батч 2026-07-22: 56 клітинок,
`16:10:10 → 16:55:16`), бо всередині сервісу клітинки йдуть по одній. Semrush — довга шия: середня клітинка
**59 с**, максимум **581 с** (живі дані з `sync_job_providers`). Мета — той самий збір, той самий один логін, але
до 3 клітинок провайдера в польоті → очікувано **2–2.5×** швидше (не 3×, причина — §1.5).

---

## 1. Дослідження (факти, на яких стоїть план)

### 1.1 Де саме прошита послідовність — три різні шари

| Шар | Місце | Що робить |
|---|---|---|
| БД | [cells.py:40](../../../app/db/repositories/cells.py#L40), [cells.py:76](../../../app/db/repositories/cells.py#L76) | `acquire_profile_lock` — **mutex**: клейм заборонено, якщо є хоч одна `running` клітинка з тим самим `profile_key` |
| Воркер | [dispatcher.py:50](../../../app/jobs/dispatcher.py#L50) | **один** потік на провайдера (`lane-<provider>`) |
| AISeoTools | [profile.py:259](/home/elon/dev/seo/AISeoTools/seo_audit/browser/profile.py#L259) | `busy_lock` keyed `{store}.{provider}` — exclusive `flock` на **весь** прогін ([session.py:430](/home/elon/dev/seo/AISeoTools/seo_audit/browser/session.py#L430)) |

Знімати треба всі три: прибрати лише БД-мутекс — і другий потік упреться у `flock` в AISeoTools (і чекатиме
тихо, «loud wait» пише лише в stdout).

### 1.2 «3 браузери» тут = 3 **вікна** в одному теплому Chrome

Контейнер **принципово** не запускає свій Chrome: `_refuse_launch_when_attach_only`
([session.py:559](/home/elon/dev/seo/AISeoTools/seo_audit/browser/session.py#L559)) — і це не примха, а наслідок
інциденту 2026-07-22 (контейнер від root зіпсував профіль оператора; профіль тепер навіть не змонтований —
[compose.yaml](../../../compose.yaml)). Три процеси Chrome на одному `user-data-dir` теж неможливі (Chrome
`SingletonLock`). Отже єдина коректна форма «3 браузери, 1 профіль» — **3 вікна** одного теплого Chrome, кожне
під своїм CDP-з'єднанням. Архітектура вже майже там: кожен провайдер має **власне вікно**, тайлене, не таб
([session.py:162](/home/elon/dev/seo/AISeoTools/seo_audit/browser/session.py#L162),
[session.py:646](/home/elon/dev/seo/AISeoTools/seo_audit/browser/session.py#L646)).

### 1.3 Що ламається при 3 вікнах одного провайдера

`find_or_open_window` шукає вікно **за хостом** (`_PROVIDER_HOSTS[provider]`) — два вікна Ahrefs нерозрізнювані,
обидва слоти схопили б **одне й те саме**. Тому потрібна slot-aware ідентифікація вікна (§3.6), а не просто ще
одне `page.goto`.

### 1.4 Auth-pause розрахований на **один** потік у доріжці

`_await_auth_and_resume` ([lane.py:138](../../../app/jobs/lane.py#L138)) паркує доріжку і, по таймауту,
закриває **весь** backlog провайдера (`queued_cells_for_provider`). Три потоки → три паркування, три
конкурентних закриття backlog-у, три спроби логіну. Потрібен один «auth gate» на провайдера (§3.4).

### 1.5 Чому виграш ~2–2.5×, а не 3× — `download_lock`

`download_lock` keyed **by STORE** ([profile.py:311](/home/elon/dev/seo/AISeoTools/seo_audit/browser/profile.py#L311)):
CDP `Browser.setDownloadBehavior` — browser-global, тож click→catch→copy експорту **мусить** лишитись
серіалізованим (LIVE-підтверджено 2026-07-23, файл падав у дефолтну теку). Паралеляться навігація, генерація
звіту на стороні провайдера, парсинг — а це якраз більшість часу; завантаження ні. Це чесна межа, а не недоробка.

### 1.6 Живі числа (база для очікувань)

| Провайдер | Клітинок | Середня | Максимум | 14 клієнтів послідовно | При 3 слотах (оцінка) |
|---|---|---|---|---|---|
| `semrush` | 70 | 59.3 с | 580.8 с | ~14 хв | ~5–6 хв |
| `ahrefs` | 106 | 20.4 с | 82.6 с | ~5 хв | ~2 хв |
| `gsc` | 221 | 12.7 с | 90.8 с | ~3 хв | ~1 хв |
| `reporting_ninja` | 136 | 12.2 с | 118.9 с | ~3 хв | ~1 хв |

`tech_audit` (34 с, до 206 с) і `blogs` (40 с, до 204 с) вже мають внутрішній паралелізм краулера — лишаються
**capacity=1** (в UI це вже обіцяно: «One site runs at a time», [sync-modal.js:337](../../../frontend/js/sync-modal.js#L337)).

### 1.7 Що вже готове й міняти не треба

- Клейм атомарний через conditional `UPDATE ... WHERE status='queued'` і `rowcount` ([cells.py:1](../../../app/db/repositories/cells.py#L1)) — рівно той механізм, що потрібен для семафора.
- Lease/heartbeat/recovery не залежать від кількості потоків ([recovery.py:1](../../../app/jobs/recovery.py#L1)).
- `run_cell` — «zero crashes» межа; збій однієї клітинки не валить доріжку ([runner.py:208](../../../app/jobs/runner.py#L208)).
- `window_lock` (короткий, keyed by store) уже робить «вибір вікна» атомарним між процесами — сюди ж заводиться slot-логіка.

---

## 2. Обсяг

**Входить:** capacity-семафор у БД; slot-пул і N потоків на провайдера у воркері; auth-gate; адитивне
`CollectionContext.browser_slot`; адитивні `slot=`-зміни в AISeoTools (`busy_lock`, `find_or_open_window`,
`BrowserSession`, dashboard-обгортки ahrefs/semrush); налаштування паралелізму; UI-панель «доріжки».

**Не входить:** зміна `download_lock` (§1.5); >1 worker-процес (PLAN §5.1 — single writer лишається); запуск
Chrome із контейнера (§1.2); паралелізм **між** різними профілями (він уже є).

---

## 3. Рішення

### 3.1 Профільний лок: mutex → семафор (`capacity`)

```python
def claim_next_cell(self, provider, *, lease_owner, lease_ttl_seconds=300, profile_capacity=1)
```

Перевірка місткості мусить бути **в тому самому `UPDATE`**, а не окремим `SELECT`:

```sql
UPDATE sync_job_providers SET status='running', ...
WHERE id = :id AND status = 'queued'
  AND (SELECT count(*) FROM sync_job_providers h
        WHERE h.profile_key = :key AND h.status = 'running') < :capacity
```

Інакше два потоки прочитають `count < N` в один момент і разом перевищать місткість. Із підзапитом усередині
`UPDATE` single-writer SQLite (PLAN §5.1) робить це атомарним — той самий аргумент, що в docstring-у
репозиторію. `acquire_profile_lock` лишається (back-compat) як `profile_slots_free(key) -> int`-обгортка;
`profile_capacity=1` за замовчуванням = **сьогоднішня поведінка байт-у-байт**.

### 3.2 Slot-пул + персистентний `slot` на клітинці

- У воркері — `SlotPool` на `profile_key`: видає найменший вільний індекс `0..N-1` на час клітинки, повертає у `finally`.
- Колонка `sync_job_providers.slot: int | None` (одна **адитивна** міграція — **запит до task-03**, не робимо самі). Потрібна не для логіки, а щоб UI/логи/recovery чесно казали «Browser 2», і щоб після рестарту було видно, що саме перервалось.
- Recovery: `requeue`/`failed_interrupted` скидає `slot = NULL` (пул у пам'яті все одно народжується заново — жоден слот із мертвого процесу не «зайнятий»).

### 3.3 Dispatcher: N потоків на провайдера

```python
provider_parallelism: dict[Provider, int]   # Settings, з env
# default: ahrefs=3, semrush=3, gsc=3, reporting_ninja=3, tech_audit=1, blogs=1
```

- Ім'я потоку `lane-<provider>-<slot>` (видно в логах).
- Супервізор рестартить **окремий** потік, а не всю доріжку ([dispatcher.py:79](../../../app/jobs/dispatcher.py#L79)).
- Жорсткий cap `1 ≤ N ≤ 4` з валідацією в `Settings` (більше — тільки бот-детект і спалені units).
- `_MemoizedCheckSession` лишається **на потік** (кеш «сесія жива» за прохід) — але з gate-ом із §3.4 зайвих проб не буде.

### 3.4 Auth gate: один паркувальник на провайдера

Спільний на провайдера об'єкт `AuthGate` (`threading.Event` + власник):

1. Потік, що отримав `awaiting_auth`, **закриває** gate і стає паркувальником — це єдиний, хто полить `check_session` і єдиний, хто по таймауту закриває backlog.
2. Інші потоки цього провайдера перед `claim_next_cell` перевіряють gate: закритий → чекають (не клеймлять, не пробують мертву сесію), а їхні **вже запущені** клітинки доїжджають нормально.
3. Логін відновився → gate відкривається, паркована клітинка `queued`, усі потоки продовжують. Таймаут → backlog закривається **один раз**, gate відкривається.
4. `cancel_requested` під час паузи обробляється як зараз ([lane.py:176](../../../app/jobs/lane.py#L176)).

### 3.5 Контракт: `CollectionContext.browser_slot`

Адитивне поле (`int = 0`) у `app/schemas/adapter.py` — **зміна спільного контракту, отже через task-02**.
`runner.py` заповнює його з клейму; адаптери `ahrefs`/`semrush` прокидають далі як `slot=context.browser_slot`.
Адаптери, яким слот не потрібен (`gsc`, `reporting_ninja`, `tech_audit`, `blogs`), поле ігнорують.

### 3.6 AISeoTools — тільки адитивно (PLAN §8)

| Символ | Зміна | Дефолт = поточна поведінка |
|---|---|---|
| `BrowserProfile.busy_lock_path(slot=0)` | `slot=0` → **існуючий** шлях `{store}.{provider}.busy.lock`; `slot>0` → `{store}.{provider}.s{N}.busy.lock` | так (хостовий CLI і далі бере slot-0 лок) |
| `BrowserProfile.busy_lock(*, slot=0)` | прокидання слоту | так |
| `find_or_open_window(ctx, provider, *, profile, slot=0)` | реєстр «вікно слоту» — `targetId` у `state/locks/{store}.{provider}.slot{N}.target`, читання/запис під наявним `window_lock`; вікно, що вже належить іншому слоту, не забираємо; загублений `targetId` → відкриваємо нове вікно + `_tile_provider_windows` | так: без slot-файлів шлях slot-0 ідентичний нинішньому (match by host) |
| `BrowserSession(provider, ..., slot=0)` | слот → `busy_lock` + `working_page` | так |
| `seo_audit.dashboard.{ahrefs,semrush}.collect_*` | keyword-only `slot: int = 0` | так |

Правила: нічого не перейменовуємо, return shapes не міняємо, **вся стара test suite має бути зелена без
правок**; `state/locks` — єдина писабельна тека контейнера, тож slot-файли живуть саме там (0 байт → кілька
байт targetId, секретів там немає — PLAN §13).

### 3.7 UI: «3 доріжки» живцем

Поверх живого прогресу з task-30: панель `Browser lanes` над матрицею — рядок на слот
(`Browser 1 · Semrush · client · stage · 62% · 01:14`), порожній слот = `Idle`. Слот беремо з
`CellProgressDTO.slot` (адитивне поле). Матриця «клієнт × сервіс» лишається як була. Анімації —
`transform`/`opacity`, `prefers-reduced-motion` (AGENTS §10); тексти англійською.

### 3.8 Захист від «наче швидше, а насправді бан»

- N конфігурується і має cap; `tech_audit`/`blogs` = 1.
- `SEO_BROWSER_QUEUE_TIMEOUT` лишається діючим (черга за слотом не має бути вічною).
- У модалці — рядок `3 browser lanes active · one shared login`, щоб оператор бачив, що це навмисно.
- Якщо провайдер починає віддавати challenge/`auth_required` частіше за поріг — **автоматично** знижуємо
  ефективну місткість цього провайдера до 1 на решту прогону і пишемо це в `job_events` (деградація, а не падіння).

---

## 4. Owned paths

| Шлях | Що робимо |
|---|---|
| `app/jobs/lane.py`, `app/jobs/dispatcher.py`, `app/jobs/runner.py`, `app/jobs/worker.py` | N потоків, slot-пул, auth-gate, прокидання слоту |
| `app/jobs/auth_gate.py`, `app/jobs/slots.py` | **нові** модулі (gate і пул — окремо від доріжки, щоб юніт-тестувались) |
| `app/db/repositories/cells.py` | `profile_capacity` у клеймі, `profile_slots_free`, запис/скидання `slot` |
| `app/core/config.py` | `provider_parallelism` + валідація cap |
| `app/schemas/adapter.py`, `app/schemas/api.py` | адитивні `browser_slot` / `slot` (**через task-02**) |
| `app/db/models.py` + міграція | колонка `slot` (**запит до task-03**) |
| `app/integrations/ahrefs.py`, `app/integrations/semrush.py` | прокидання `slot=` |
| `AISeoTools: seo_audit/browser/{profile,session}.py`, `seo_audit/dashboard/{ahrefs,semrush}.py` | §3.6, адитивно |
| `frontend/js/sync-modal.js`, `frontend/css/app.css` | панель доріжок |
| `tests/unit/{jobs,db}/`, `tests/integration/` | §6 |

Read-only: `app/api/**` (крім адитивного DTO через task-02), `app/metrics/**`, `frontend/js/trends-page.js`.

---

## 5. Definition of Done / Gate

1. `provider_parallelism=1` для всіх → поведінка **не відрізняється** від поточної; усі наявні тести зелені без правок.
2. При `N=3` три клітинки одного провайдера справді перекриваються в часі (integration-тест на fake-адаптері з таймштампами `started_at`/`finished_at`).
3. Місткість **ніколи** не перевищується: 3 потоки × 20 клітинок, `capacity=2` → у жодний момент немає 3 `running` з одним `profile_key`.
4. Один слот не видається двічі; після падіння клітинки слот повертається у пул.
5. Мертва сесія паркує провайдера **один** раз: один логін-запит, один backlog-close по таймауту, інші потоки не клеймлять цього провайдера, вже запущені клітинки доїжджають.
6. Живий прогін: повний `force_refresh` портфеля з `N=3` завершується без `ERR_ABORTED`/перехресних вікон; кожен слот працює у **своєму** вікні (перевірка по `Target.getTargets`); експорти цілі (не 0 байт).
7. Виміряний wall-clock ≤ 60 % від послідовного прогону на тій самій вибірці — цифри в Completion report.
8. AISeoTools: уся стара test suite зелена; slot-0 шлях без нових файлів у `state/locks`.
9. Профіль оператора не зачеплений: контейнер і далі attach-only, `browser-up` після прогону працює.
10. `ruff` / `mypy app` / `pytest` — зелені **в Docker**.

---

## 6. Тести

- `tests/unit/db/test_cells_capacity.py`: семафор під конкурентними потоками; `capacity=1` = старий mutex; клітинки без `profile_key` не обмежуються.
- `tests/unit/jobs/test_slots.py`: видача/повернення, відсутність дублів, повернення при винятку.
- `tests/unit/jobs/test_auth_gate.py`: один паркувальник; інші потоки не клеймлять; backlog закривається раз; cancel під паузою.
- `tests/integration/test_job_matrix.py` (додатки): «усередині сервісу — до N одночасно» (наявний
  `test_services_run_in_parallel_while_clients_run_sequentially` переписати на «≤ capacity», не видаляти сенс).
- AISeoTools: `slot=0` шлях незмінний; два слоти отримують різні `targetId`; загублене вікно перевідкривається.
- Ручний живий прогін на 4–6 клієнтах × `ahrefs`+`semrush` із `N=3` (числа → Completion report).

---

## 7. Ризики

| Ризик | Мітигація |
|---|---|
| Бот-детект/rate-limit від 3 паралельних сесій одного акаунта | N конфігурований, cap 4, авто-деградація до 1 по challenge/`auth_required` (§3.8); людський pacing AISeoTools лишається |
| Units спалюються втричі швидше в реальному часі | ніякої автоматики — прогін завжди ініціює оператор; `units_spent`/`units_estimated` уже в DTO |
| Перехоплення вікон між слотами | slot-реєстр під наявним `window_lock` + рішення читається з `Target.getTargets`, а не з `context.pages` (той самий аргумент, що в docstring `find_or_open_window`) |
| Завантаження не паралеляться (§1.5) | зафіксовано як межа; у DoD міряємо реальний, а не обіцяний виграш |
| Слот-локи в `state/locks` з-під іншого uid | контейнер уже працює під uid оператора (`compose.yaml`); нові файли — тим самим шляхом і з тим самим `_open_shared_lock` |
| SQLite «database is locked» при більшій кількості писачів | транзакції лишаються короткими, network/browser-робота — поза сесією (PLAN §5.1); подій стає ~×3 — перевірити WAL і `busy_timeout` під навантаженням |

---

## 8. Follow-ups / борг

- `TODO`: паралелізувати завантаження (потребує окремого browser-контексту на слот або per-target download behavior) — зняло б обмеження §1.5.
- `TODO`: авто-тюнінг N за спостережуваною тривалістю клітинок і частотою challenge.
- `TODO(task-32)`: планувальник — паралельні слоти роблять нічний повний refresh реалістичним за часом.

---

## ✅ Completion report

- **Дата:** 2026-07-31
- **Гілка / комміт:** `task-30-live-data-refresh` @ (комміт нижче)
- **Статус:** done-with-followups

### Що зроблено

- Профільний лок перетворено з **mutex** на **counting semaphore**: `claim_next_cell(profile_capacity=N)` дозволяє N клітинок одного `profile_key` бути `running` одночасно.
- У воркері з'явилися **слоти**: `SlotPool` видає номер доріжки, він же — ідентичність вікна браузера; номер персиститься в `sync_job_providers.slot` (міграція `b7d21e9f4c83`) і віддається в `CellProgressDTO.slot`.
- Диспетчер піднімає **N потоків на провайдера** (`provider_parallelism`), супервізор перезапускає окремий потік, а не всю доріжку.
- **Один auth-gate на провайдера**: рівно одна доріжка чекає логін, решта припиняють клеймити; на резюмі/таймауті обробляються **всі** припарковані клітинки цього провайдера, а не лише своя.
- **AISeoTools — адитивно**: `BrowserProfile(slot=…)` (окремий `busy_lock` на слот), реєстр вікон `*.window` із CDP `targetId`, slot-aware `find_or_open_window`, `BrowserSession(slot=…)`, `slot=` в `dashboard.ahrefs/semrush`. `slot=0` лишає всі старі шляхи й поведінку незмінними.
- У модалці — панель **«Browser lanes»**: рядок на вікно з клієнтом, стадією і відсотком; з'являється лише коли прогін реально використав більше однієї доріжки.

### Виміряно на живому стеку (2026-07-31)

| Прогін | Доріжок | Результат |
|---|---|---|
| GSC + Reporting Ninja, 22 клітинки (11 клієнтів) | 1 → **3** | **180 с → 60 с (×3)**, усі 22 `succeeded` |
| Ahrefs, 3 клієнти | 3 | KPI зібрані, але **всі три** детальні експорти впали в `TimeoutError` → `partial` |
| Ahrefs, 2 клієнти | 2 | обидві `succeeded`, quality `complete`, попереджень нема, ~100 с |
| Semrush, 3 клієнти | 2 | 2 м 09 с; якість (`truncated`/`unavailable`) **збігається з історичним однодоріжковим** прогоном 23.07 — це не регресія паралельності, а відома selector-drift проблема Semrush |

Перший 3-доріжковий Ahrefs-прогін також показав окремий ефект: три однакові важкі SPA-навігації, стартовані в ту саму мілісекунду, дали 2 з 3 `Page.goto` таймаутів. Звідси **`lane_start_stagger_seconds = 8`** — рознесення старту доріжок; після першої клітинки вони розходяться самі.

### Як зроблено (ключові рішення)

- **Перевірка місткості — всередині `UPDATE`**, не окремим `SELECT`: інакше дві доріжки прочитали б «є вільне місце» в один момент і разом перевищили б ліміт. Атомарність тримає single-writer SQLite, як і решта claim-логіки.
- **Слот-пул у пам'яті, не в БД**: один worker-процес (PLAN §5.1) — пул авторитетний; слот померлого процесу зникає разом із ним, тоді як персистентний пул потребував би власного recovery. Крос-процесна безпека лишається на БД-семафорі + `flock` на слот.
- **Ідентичність вікна — CDP `targetId` у файлі-претензії** (`locks/{store}.{provider}[.sN].window`). Пошук «за хостом» не розрізняє два вікна Ahrefs, тож без цього слоти 0 і 1 бралися б за одне вікно. Файли живуть у `state/locks` — єдиній писабельній для контейнера теці; вміст — лише id, без секретів.
- **Виміряні дефолти замість смаку**: `provider_parallel_slots = {"ahrefs": 2, "semrush": 2}`, решта метричних провайдерів — 3, `tech_audit`/`blogs` — 1. Причина ahrefs=2 структурна: bulk-експорт іде через store-wide `download_lock` (CDP `setDownloadBehavior` — browser-global), тож третя доріжка додає чергу до фази, яка **не** паралелиться, і виштовхує решту за Playwright-таймаути.
- Головні файли: `app/jobs/{slots,auth_gate,dispatcher,lane}.py`, `app/db/repositories/cells.py`, `app/core/config.py`, `AISeoTools: seo_audit/browser/{profile,session}.py`.

### Контрактні зміни (Contract changes)

Усе адитивне: `CollectionContext.browser_slot: int = 0`, `CellProgressDTO.slot: int | None`, колонка `sync_job_providers.slot`, три keyword-only `slot=0` в AISeoTools (`BrowserSession`, `collect_site_audit_browser`, `collect_semrush_browser`). Три знімки контрактів перегенеровано; діфи містять **лише додані** поля/параметри.

### Як перевірити (verification)

```bash
docker compose up -d --build
docker compose exec api poetry run alembic upgrade head
docker compose exec api poetry run pytest -q          # 1031 passed
docker compose logs worker | grep worker_parallelism  # {'ahrefs': 2, 'semrush': 2, 'gsc': 3, ...}
```

Живий прогін: `POST /api/v1/sync-jobs` на 3 клієнтів × `gsc,reporting_ninja` → у модалці видно панель «Browser lanes», у БД `slot` = 0/1/2 і клітинки перекриваються в часі. Для браузерного: `providers=["ahrefs"]` — у `~/.local/share/seo-audit/state/locks/` з'являються `main.ahrefs.s1.*`/`main.ahrefs.s2.*`, а `*.window` містять **різні** `targetId`.

### Тести

- `tests/unit/db/test_cells_capacity.py` (9): дефолт = 1, рівно N одночасних, звільнення місця, запис слоту, скидання слоту на retry, клітинки без профілю не лімітуються, 8 потоків проти capacity=3 не перевищують ліміт.
- `tests/unit/jobs/test_slots.py` (6) і `test_auth_gate.py` (9): унікальність слотів під конкуренцією, повернення слоту при винятку, рівно один паузер, повторне взяття після release, пробудження на release.
- `tests/unit/jobs/test_auth_pause_resume.py`: новий `test_multiple_lanes_pause_once_and_all_resume_together`.
- `tests/integration/test_job_matrix.py`: `test_provider_lanes_run_up_to_capacity_at_once` (пікова конкурентність >1 і ≤3, слоти різні) + `test_single_lane_provider_still_runs_one_cell_at_a_time` (старий контракт лишився).
- `tests/harness/worker.py`: адитивний `parallelism=` (зона task-13 — точковий, задокументований дотик).

### Follow-ups / борг

- **Ahrefs на 3 доріжках** лишається недосяжним, доки детальний експорт іде через store-wide `download_lock`. Розв'язок — окремий browser-контекст на слот або per-target download behavior; це окрема таска з власною живою перевіркою.
- Semrush selector drift (`Semrush table Export button did not resolve`) — **дошло з-перед task-31**, не чіпалось тут; варте окремої таски.
- Авто-деградація місткості по частоті challenge/`auth_required` (§3.8 плану) **не реалізована**: за живі прогони жодного challenge не було, тож поріг нема на чому калібрувати. Наразі роль запобіжника грає конфіг (`provider_parallel_slots`, cap 4).
- `lane_start_stagger_seconds` — емпіричні 8 с; авто-тюнінг за спостережуваною тривалістю клітинок лишається follow-up-ом.
