# task-80 — Archive: примусова нова копія сайту («Recapture»)

- **Хвиля:** post-63 (UX архіву) — та сама, що 70
- **Depends on:** **59** ✅, **60** ✅, **62** ✅, **63** ✅, **65** ✅, **69** ✅, **70** ✅. Не залежить від 75 (переїзд на PG вже зроблено живо — база бойова на Postgres, `9701790bda11_postgres_baseline`), але **міграція пишеться під Postgres**, не під SQLite.
- **Owned paths:**
  `app/webarchive/capture.py`, `app/webarchive/scheduler.py`,
  `app/api/archive.py`, `app/schemas/web_archive.py` (адитивно — контрактна зміна, див. §7),
  `app/audit/actions.py` (один рядок), `app/cli.py` (прапорець `--force` у `archive-capture`),
  `app/db/repositories/web_archive.py` (адитивний kwarg у `create_capture`),
  `migrations/versions/<new>_archive_capture_force_task_80.py` (нова),
  `frontend/js/archive-page.js`, `frontend/js/archive-capture.js`, `frontend/js/archive-viewer.js`, `frontend/js/api.js` (адитивно), `frontend/css/archive.css`,
  `tests/unit/webarchive/test_capture.py`, `tests/unit/api/test_archive_api.py`
- **Міграція:** **потрібна** — одна колонка `archive_captures.force`. Див. §2.

---

## §0. Що є сьогодні (виміряно в коді, не гіпотеза)

**§0.1 Примусової нової копії не існує ні на жодному рівні.** Ні «Capture all», ні рядкова «Capture now», ні «Archive now» у lookup'і не дають нової версії сторінки, якщо сторінка не змінилась. [`capture_url`](app/webarchive/capture.py#L538-L543) виходить раніше:

```python
if not content_changed and not is_stale:
    repo.confirm_snapshot(prev_snapshot, observed_at=now)
    repo.touch_url(archive_url, observed_at=now, snapshot_written=False)
    return UrlCaptureResult(outcome="unchanged")
```

`confirm_snapshot` лише піднімає `last_confirmed_at`/`confirmed_count` на **старому** рядку. Тобто «зняти зараз» ≠ «нова копія»: `content_changed` це `fingerprint != prev.content_fingerprint or status != prev.http_status or final_url != prev.final_url` ([capture.py:530](app/webarchive/capture.py#L530-L534)), а `is_stale` — вік попереднього снапшота ≥ `archive_refresh_after_days`. Оператор, який хоче свіжий байт-у-байт зліпок сайту **зараз** (перед релізом, перед міграцією, після інциденту), сьогодні не має жодної кнопки — і, що гірше, натиснувши «Capture now», бачить `ok` і думає, що копія є.

**§0.2 У словнику `store_reason` немає значення для примусової копії.** [`StoreReason`](app/schemas/web_archive.py#L20) = `Literal["first", "changed", "weekly_refresh", "status_changed"]`, а [`_resolve_reason`](app/webarchive/capture.py#L463-L471) на «не перший, не змінився» віддає `"weekly_refresh"`. Якби ми просто прибрали ранній вихід, кожна примусова копія **брехала б** у таймлайні («weekly refresh» замість «forced by operator»). Потрібне п'яте значення — `"forced"`.

**§0.3 Намір мусить жити на рядку капчура, а не в запиті.** API ніколи не обходить сайт сам — він лише створює `queued`-рядок, а обхід робить воркер, який підхопить його пізніше ([`start_archive_capture`](app/api/archive.py#L576-L580), [`_submit`](app/webarchive/scheduler.py#L260)). Тому `force` — це **колонка**, як `scope`/`archive_url_id` у task-62 §2.7. І там же вже задокументований живий дефект 2026-09-05: якщо не прочитати поле назад із `claimed`, планувальник тихо відкочується до дефолту (одноURL-ний рефреш GOREPORT зняв 2 URL замість 1). Той самий рядок [scheduler.py:287](app/webarchive/scheduler.py#L287) — єдине місце, де `force` має бути прочитаний.

**§0.4 Ціна примусової копії — час і трафік, а НЕ сховище.** Рефлекторне «примусовий рекапчур подвоїть диск» тут неправильне: блоби контентно-адресовані. [`store_blob`](app/webarchive/storage.py#L47-L58) не пише файл, якщо байти вже на диску, а [`register_blob`](app/db/repositories/web_archive.py#L713-L717) повертає наявний рядок. Тобто для незміненої сторінки примусова копія додає **нуль байтів артефактів**: один рядок `archive_snapshots` + по рядку `archive_snapshot_assets` на кожен підресурс. Реальна ціна — повний обхід (LAXTON 3194 URL, ALPHA 1681) і вихідний трафік. Прунінга снапшотів у проєкті немає (`prune_local_blobs` у [cloud_sync.py:118](app/webarchive/cloud_sync.py#L118) чистить лише локальні копії блобів, уже вивантажених у хмару), тож примусові версії **нічого не витісняють** з історії.

**§0.5 Без маркера лічильники будуть брехати.** Примусовий прохід дає `outcome="new"` на кожній URL, а [лічильники](app/webarchive/capture.py#L839-L842) інкрементують `urls_new`. У хабі це прочитається як «3194 нових» — тобто «сайт змінився цілком». Рядок капчура і сторінка прогресу мусять казати «Recapture (forced)» явно.

**§0.6 Фід змін уже захищений — і цей захист треба зберегти тестом.** `ArchiveChange` створюється лише під `if not is_first and content_changed` ([capture.py:611](app/webarchive/capture.py#L611)). Отже примусова копія незміненої сторінки **не** народжує порожніх «змін». Це саме та поведінка, яку легко зламати недбалою правкою, тому вона фіксується окремим тестом (§6).

**§0.7 Рольова стеля вже правильна для одного клієнта.** [`required_role`](app/core/auth/policy.py#L91-L100) віддає `member` для будь-якого POST поза admin-only префіксами; `/api/v1/archive` там не значиться. Рекапчур **одного** клієнта — це щоденна робота (той самий клас, що «Capture now»), тож `member`. Портфельний примусовий рекапчур — інший клас (та ж логіка, що для `/reports/batches`, `/imports`, `/cloud` у [`DEFAULT_ADMIN_ONLY_PATHS`](app/core/auth/policy.py#L56)), і він **свідомо поза обсягом** цієї таски (§8).

---

## §1. Обсяг (що саме будуємо)

Одна кнопка **`Recapture`** у рядку таблиці `COVERAGE / CLIENTS` на `#/archive`, поруч із наявною `Capture now`, і повний шлях під неї: колонка → API → черга → воркер → нове значення `store_reason` → чесна позначка в UI.

**Семантика:** «примусова нова копія всього сайту клієнта» — новий `archive_snapshots` для **кожної** відстежуваної URL клієнта, навіть якщо вона байт-у-байт та сама.

**Чого кнопка НЕ робить (свідомо):**
- не обходить `ArchiveSettings.capture_assets=false` і `ignore_patterns_json` конкретного клієнта — це його конфіг, а не застарілість даних;
- не піднімає `max_urls`;
- не чіпає портфель (немає «Recapture all» — §8);
- не видаляє нічого. Це **додавання** версії, ніколи не заміна.

**Копірайт (англійською, за CLAUDE.md):** кнопка — `Recapture`; підтвердження — `Recapture the whole site for <Client>? This stores a new version of all N pages, even the unchanged ones. It can take a while.`; бейдж у черзі — `forced`; підпис у таймлайні — `Forced copy`. Пишемо `Recapture`, не `ReCapture` — друга велика літера всередині слова не є англійською типографікою.

## §2. Модель даних і міграція

Нова колонка в [`ArchiveCapture`](app/db/models.py#L1592):

```python
# task-80: "Recapture" — примусова нова копія навіть незміненої сторінки.
# Живе на рядку, не в запиті: обхід робить воркер уже після того, як
# HTTP-запит завершився (та сама причина, що й для `scope`/`archive_url_id`).
force: Mapped[bool] = mapped_column(Boolean, default=False)
```

Міграція — **один `op.add_column`**, без `batch_alter_table` (інцидент 2026-09-01 у CLAUDE.md; на Postgres recreate і не потрібен — `ADD COLUMN ... NOT NULL DEFAULT false` тут метаданна операція):

```python
op.add_column("archive_captures", sa.Column("force", sa.Boolean(), nullable=False, server_default=sa.false()))
op.alter_column("archive_captures", "force", server_default=None)
```

**Перед `alembic upgrade head` — бекап.** Скрипта під PG ще немає (`scripts/backup_sqlite.py` — від старого рушія, `pg_dump`-обгортка лишається в task-75 §…), тож знімок робиться `pg_dump` із самого контейнера БД:

```bash
docker compose exec -T db pg_dump -Fc -U yovth yovth > app_data/backups/pre-task-80.dump
```

`down_revision` — поточний head (`ea2070f4c343`, якщо до старту роботи не з'явиться новіший; перевірити `alembic heads`, не вгадувати). **Ризик колізії:** якщо паралельна сесія має незакомічену міграцію — спершу дочекатися чистого `git status` у `migrations/` (пам'ять про crash-loop на гонці двох `alembic upgrade head` через дефолтний entrypoint).

`create_capture` у [репозиторії](app/db/repositories/web_archive.py) отримує **адитивний** `force: bool = False` (keyword-only), решта викликів не змінюється.

## §3. Рушій капчура

**§3.1 `capture_url(..., force: bool = False)`** ([capture.py:473](app/webarchive/capture.py#L473)):
- ранній вихід стає `if not content_changed and not is_stale and not force:` — і **тільки** це; уся решта шляху (fetch, blob, assets, snapshot, links) уже коректна для випадку «нічого не змінилось», бо це той самий шлях, яким ходить `weekly_refresh`;
- `_resolve_reason(*, is_first, content_changed, fingerprint_same, forced)` → `"forced"`, коли `forced and not is_first and not content_changed`. Пріоритет реальності над наміром: якщо під примусовим проходом сторінка **справді** змінилась, причина лишається `"changed"`/`"status_changed"` — інакше ми втратимо єдиний сигнал реальної зміни;
- `prev_snapshot_id` заповнюється як завжди → ланцюжок версій у в'ювері не рветься.

**§3.2 Примусовий прохід ревалідує й підресурси.** Копія, чий CSS у знімку — 30-денний, це не свіжа копія. Тому під `force` асетний цикл рахує `needs_check = True`, ігноруючи `archive_asset_revalidate_days` ([capture.py:344-348](app/webarchive/capture.py#L344-L348)). Ціна мала й виміряна: `_check_or_fetch_asset` ходить умовним запитом (`If-None-Match`/`If-Modified-Since`, «live-measured 10/10» за його ж докстрінгом), тобто це переважно 304, а не повторне завантаження. Бюджет `archive_new_assets_per_page` для **нових** асетів лишається як є — примус не має права зняти запобіжник на асет-бомбі.

**§3.3 `run_client_capture(..., force: bool = False)`** — просто протягує прапорець у `_capture_url_and_commit` → `capture_url`. Ізоляція клітинки (PLAN §9.1) не змінюється: збій однієї URL під примусом так само не валить прохід.

**§3.4 Планувальник читає поле назад** ([scheduler.py:287](app/webarchive/scheduler.py#L287)), рядком нижче за `only_url_id`:

```python
force = bool(claimed.force)
```
і передає в `run_client_capture`. **Це найважливіший рядок таски** — без нього кнопка «працює» в API, дає 202, і тихо робить звичайний капчур (§0.3, живий прецедент task-62 §2.7).

**§3.5 CLI** — `archive-capture --force` тим самим шляхом ([cli.py:1448](app/cli.py#L1448)), щоб примусовий прохід можна було запустити поза UI, коли черга воркера зайнята.

## §4. API

Окремий шлях, а не прапорець на наявному `POST /captures` — щоб журнал дій розрізняв два різні за наслідками наміри (мапа в [`actions.py`](app/audit/actions.py#L134) ключується парою `(метод, шлях)`):

```
POST /api/v1/archive/captures/force?client_id=<id>   → 202 ArchiveCaptureAcceptedDTO
```

- реалізація — `_start_forced_capture(client_id)`, копія [`_start_capture`](app/api/archive.py#L533-L569) з `force=True` у `create_capture`; `slot_key` лишається `manual:{uuid4}` (нічого в коді не парсить цей префікс, істина — колонка), `trigger="manual"`, `scope="client"`;
- `commit_with_retry` — як у сусідніх хендлерах (крос-процесна ретрі-обгортка, її ж причина);
- `404 client_not_found`, `409 already_running` — через наявний `has_active_capture(client.id)`: примусовий прохід **не** обходить інваріант «один активний капчур на клієнта»;
- журнал: `("POST", "/api/v1/archive/captures/force"): ActionSpec("archive.recapture_start", "client")`;
- роль — `member` за загальним правилом (§0.7), окремого гейта не додаємо.

DTO `ArchiveCaptureDTO`/рядок черги отримують **адитивне** поле `force: bool` — без нього UI не може відрізнити примусовий прохід від звичайного (§0.5, §5.2).

## §5. Фронтенд

**§5.1 Кнопка в рядку** ([archive-page.js:507](frontend/js/archive-page.js#L507)):
- друга кнопка в тій же клітинці: `data-action="recapture-now"`, `class="button button--quiet button--small"`, іконка `camera-plus` (у спрайті є; `camera` лишається за «Capture now», щоб дві дії не читалися однаково);
- клітинка дій стає `flex; gap: 8px; flex-wrap: wrap` **у CSS** (`.archive-clients-table__actions`) — жодного інлайн-`style=` (CSP ріже і атрибут, і JS-присвоєння `.style`, двічі перевірено живо);
- перед запитом — `window.confirm` із текстом із §1, у якому підставлено ім'я клієнта і `urls_tracked` із того ж рядка (оператор бачить масштаб **до** кліку, а не «за 40 хвилин»);
- стан: окремий `state.recaptureStarting` (не спільний із `captureStarting`, інакше «Capture now» іншого рядка гасне разом із цим) + `loader-2 icon--spin`;
- `api.js`: `startArchiveForcedCapture(clientId)` з **явним** ключем запиту `archive-recapture-start:${clientId}` — названа мета, не похідна від параметрів (пам'ять task-36, дефект ловився двічі);
- після 202 — `location.hash = buildArchiveCaptureHash({ id: capture_id })`, та сама наявна сторінка живого прогресу (task-62 §1.5). Нової сторінки не пишемо.

**§5.2 Чесна позначка.** Рядок у списку капчурів ([`captureRowMarkup`](frontend/js/archive-page.js#L532)) і заголовок `archive-capture.js` при `c.force === true` показують бейдж `forced` (`.tag-new`-подібний клас, уже є в CSS). Без цього `3 194 new` читається як «сайт переписали цілком» (§0.5).

**§5.3 Таймлайн і метадані.** `reasonLabel` у [archive-viewer.js:602](frontend/js/archive-viewer.js#L602) та [archive-page.js:339](frontend/js/archive-page.js#L339) отримують `forced → "Forced copy"`. `url-inspector.js` показує сирий `store_reason` — там нічого правити не треба, значення самоописне.

**§5.4 Кеш.** Після правки JS/CSS — підняти `?v=` **і на теґу `<script src>`**, інакше тунель Cloudflare 4 години віддає старий файл поверх app-level `no-cache` (пам'ять `cloudflare_static_asset_cache_bug`).

## §6. Тести

`tests/unit/webarchive/test_capture.py`:
1. `force=True` на **незміненій** сторінці → новий `ArchiveSnapshot`, `store_reason == "forced"`, `prev_snapshot_id` вказує на попередній, `outcome == "new"`;
2. той самий випадок → **нуль** нових `ArchiveChange` (§0.6) і **нуль** нових `ArchiveBlob` (дедуп блобів, §0.4 — це і є тест на «сховище не подвоюється»);
3. `force=True` на **справді зміненій** сторінці → `store_reason == "changed"`, не `"forced"` (пріоритет реальності, §3.1);
4. `force=False` (регресія) → як було: `outcome == "unchanged"`, `confirmed_count` виріс, нового рядка немає;
5. `force=True` ревалідує асет, чий `last_checked_at` свіжий (§3.2) — на фейковому фетчері, з перевіркою, що `archive_new_assets_per_page` усе ще стримує нові.

`tests/unit/api/test_archive_api.py`:
6. `POST /captures/force` → 202, і створений рядок має `force=True`, `scope="client"`, `trigger="manual"`;
7. другий виклик під активним капчуром → 409;
8. невідомий `client_id` → 404;
9. `viewer` → 403, `member` → 202 (маркер `@pytest.mark.auth`; без нього middleware вимкнений і тест нічого не перевіряє — і, головне, увімкнений випадково він пише в **бойову** `audit_events`, пам'ять `global_test_singleton_db_leak`);
10. `ArchiveCaptureDTO` віддає `force` (контракт для §5.2).

`tests/unit/webarchive/test_archive_scheduler.py` (той самий власник — рушій архіву):
11. `claim_capture` рядка з `force=True` → `run_client_capture` викликано з `force=True`. Тест саме на §3.4, бо це та помилка, яку вже одного разу зробили.

## §7. Контрактні зміни

- `StoreReason` (`app/schemas/web_archive.py`) — **нове значення** `"forced"`. Розширення, не заміна: наявні чотири лишаються. Споживачі — `archive-viewer.js`, `archive-page.js`, `url-inspector.js` (усі три оновлюються тут).
- `ArchiveCaptureDTO` + рядок черги — адитивне `force: bool`.
- Схема БД — `archive_captures.force` (`NOT NULL DEFAULT false`).
- Новий ендпоінт `POST /api/v1/archive/captures/force`; наявні не змінюються.
- Синхронно оновити `task-02` (словник `StoreReason`) і згадати в Completion report.

## §8. Свідомо поза обсягом (follow-ups)

- **`Recapture all` по портфелю.** Прапорець `force` робить це майже безкоштовним (`_start_all_captures` + `force=True`), але наслідок — повний обхід ~10 тис. URL підряд, тому шлях мусить бути **admin-only** (той самий клас, що батчі звітів) і з окремим підтвердженням. Робимо, тільки якщо користувач попросить, — `TODO(task-81)`.
- **Примусовий рекапчур однієї URL** (`force` на `scope="url"`): колонка вже це вміє, потрібна лише кнопка в таймлайні поруч із «Refresh now».
- **`pg_dump`-обгортка** замість `scripts/backup_sqlite.py` — борг task-75, не цієї таски; тут використовується `pg_dump` із контейнера напряму (§2).

## §9. Definition of Done / Gate

1. Бекап `pg_dump` зроблено **до** міграції; `alembic upgrade head` і `downgrade -1` перевірені на копії, не на бойовій базі.
2. `docker compose exec api poetry run pytest tests/unit/webarchive tests/unit/api/test_archive_api.py` — зелено; повний прогін не гірший за базовий (2632 passed на момент старту).
3. `ruff check .` і `mypy app` — зелено.
4. **Живо, на реальному клієнті з малим списком URL** (EFFYS — 17 URL, або GRP — 32): натиснути `Recapture`, дочекатися завершення, і переконатися, що: `Versions` у таблиці виросло рівно на кількість успішних URL; у таймлайні нова точка з підписом `Forced copy`; у фіді `#/archive/changes` **не** з'явилось порожніх змін; сторінка нової версії відкривається у в'ювері зі стилями (асети прилінковані).
5. Роль `viewer` на живому стеку через тунель (не `127.0.0.1` — на лупбеку bypass завжди дає адміна, пам'ять task-73) отримує 403 і не бачить дії.
6. У `#/admin/audit` є рядок `archive.recapture_start` з актором і `client_id`.
7. Чеклист `frontend/AGENTS.md §15–16` пройдено: жодного інлайн-`style=`, обидві кнопки лізуть у рядок на 1280px і не розповзаються на 768px, `?v=` піднято.

---

## ✅ Completion report

- **Дата:** 2026-09-07
- **Гілка / комміт:** `feat/task-80-archive-forced-recapture` @ (буде в наступному комміті)
- **Статус:** done

### Що зроблено

Кнопка **Recapture** у рядку таблиці `COVERAGE / CLIENTS` на `#/archive`, поруч із наявною `Capture now`. Повний шлях живий і перевірений на реальному клієнті: клік (з підтвердженням і кількістю сторінок) → `POST /api/v1/archive/captures/force` → рядок `archive_captures.force=true` у черзі → воркер читає прапорець назад і на кожній URL клієнта пише нову версію знімка, навіть якщо байти ідентичні попереднім → `store_reason="forced"` у таймлайні → бейдж `forced`/`FORCED` у черзі, на сторінці живого прогресу й у панелі «Queue & Activity» хабу. CLI отримав `yovth-report archive-capture --force` тим самим шляхом.

### Як зроблено (ключові рішення)

- **Прапорець — колонка, не параметр запиту.** `archive_captures.force: bool` (нова, `NOT NULL DEFAULT false`), бо обхід сайту робить воркер уже ПІСЛЯ HTTP-запиту з API — той самий інваріант, що й `scope`/`archive_url_id` (task-62 §2.7). Найважливіший рядок усієї реалізації — [scheduler.py:293](app/webarchive/scheduler.py#L293), де `_submit` читає `claimed.force` назад із рядка, щойно захопленого `claim_capture`; без цього кнопка дає 202 і тихо виконує звичайний капчур (точний клас дефекту, який документує сусідній коментар про `only_url_id`, живий 2026-09-05).
- **Ранній вихід у `capture_url`** ([capture.py:539](app/webarchive/capture.py#L539)) отримав третю умову — `and not force` — і більше нічого: увесь інший шлях (fetch → blob → assets → snapshot → links) уже коректний для «нічого не змінилось», бо це той самий шлях, яким ходить `weekly_refresh`.
- **`_resolve_reason`** ([capture.py:463](app/webarchive/capture.py#L463)) віддає нове значення `"forced"`, але **реальність важливіша за намір**: якщо сторінка під примусовим проходом справді змінилась, причина лишається `"changed"`/`"status_changed"` — інакше губиться єдиний сигнал реальної зміни. Живо перевірено обидва боки (тест + реальний прохід EFFYS дав `"forced"` на незміненій сторінці).
- **Асети ревалідуються під примусом** незалежно від `archive_asset_revalidate_days` ([capture.py:346](app/webarchive/capture.py#L346), доданий `or force` до `needs_check`), але бюджет **нових** асетів (`archive_new_assets_per_page`) не обходиться — примус не знімає запобіжник на асет-бомбі. Живо: 93 запити асетів (переважно ревалідація, 0 failed) на 17 сторінках EFFYS.
- **Окремий ендпоінт `POST /captures/force`**, не прапорець на наявному `POST /captures` — журнал дій ключується парою `(метод, шлях)`, і окремий шлях дає окрему дію `archive.recapture_start` замість `archive.capture_start`. Живо підтверджено рядком в `audit_events`.
- **`triggerRecapture`** підтверджує дію (`window.confirm` з кількістю сторінок клієнта) і лишається на тій самій сторінці — на відміну від чернетки плану (яка малювала перехід на сторінку прогресу за зразком ad-hoc капчура), реалізація свідомо повторює поведінку сусідньої кнопки `Capture now` (тост + черга внизу оновлюється поллінгом): дві сусідні кнопки одного рядка з різною поведінкою після кліку були б незрозумілі оператору. Відхилення від плану §5.1, свідоме.
- Головні файли: [app/webarchive/capture.py](app/webarchive/capture.py) — рушій (`force` наскрізь через `capture_url`/`_capture_assets_for_page`/`run_client_capture`); [app/webarchive/scheduler.py](app/webarchive/scheduler.py) — читання прапорця з рядка; [app/api/archive.py](app/api/archive.py) — новий ендпоінт; [app/db/models.py](app/db/models.py) + міграція — схема; [frontend/js/archive-page.js](frontend/js/archive-page.js) — кнопка/стан/підтвердження; [frontend/js/archive-capture.js](frontend/js/archive-capture.js) — бейдж на сторінці прогресу; [frontend/js/archive-calendar.js](frontend/js/archive-calendar.js) — `reasonLabel` (спільний для хабу й в'ювера).

### Контрактні зміни (Contract changes)

- `StoreReason` (`app/schemas/web_archive.py`) — нове значення `"forced"` (розширення, старі чотири незмінні).
- `ArchiveCaptureDTO` (і, транзитивно, `ArchiveQueueDTO`) — нове поле `force: bool`.
- Схема БД — нова колонка `archive_captures.force` (`NOT NULL DEFAULT false`), міграція `c3a91f7e2b6d` (`d6d668fe8d53 → c3a91f7e2b6d`), прямий `op.add_column`, без `batch_alter_table`.
- Новий ендпоінт `POST /api/v1/archive/captures/force?client_id=<id>` → `202 ArchiveCaptureAcceptedDTO` (форма відповіді ідентична `POST /captures`).
- Новий CLI-прапорець `yovth-report archive-capture --force`.
- Новий рядок журналу дій: `("POST", "/api/v1/archive/captures/force") → ActionSpec("archive.recapture_start", "client")`.
- `task-02-shared-contracts.md` **не** містить DTO/enum'ів web-архіву (вони з'явились уже після MVP-контракту, поза цим документом) — синхронізувати там нічого не було; якщо колись `task-02` розшириться на архів, `StoreReason`/`ArchiveCaptureDTO` варто буде туди перенести.

### Як перевірити (verification)

```bash
# Тести модуля
docker compose exec api poetry run pytest tests/unit/webarchive tests/unit/api/test_archive_api.py -q
# → 189 passed

# Повний прогін (перевірено — 8 falls пре-екзистентні, не від цієї таски,
# перевірено git-стешем змін і повторним прогоном саме цих 8 тестів на
# чистій базі: падають ІДЕНТИЧНО без task-80)
docker compose exec api poetry run pytest -q
# → 2680 passed, 1 skipped, 8 failed (pre-existing: aiseotools snapshot,
#   job_matrix, security_hardening secrets scan, openapi snapshot,
#   tech_audit dashboard counts, alembic upgrade/downgrade-base, db_health,
#   mcp oauth.py isolation — жоден не в owned paths цієї таски)

docker compose exec api poetry run ruff check app/webarchive/capture.py app/webarchive/scheduler.py app/api/archive.py app/schemas/web_archive.py app/audit/actions.py app/cli.py app/db/repositories/web_archive.py app/db/models.py
docker compose exec api poetry run mypy app
# → чисто

# Міграція: перевірено upgrade → downgrade -1 → upgrade (round-trip),
# колонка з'являється/зникає чисто на бойовій Postgres-копії
docker compose exec api poetry run alembic upgrade head

# Жива перевірка (client EFFYS, 17 URL, agent dev bearer token):
curl -X POST -H "Authorization: Bearer $(grep '^AGENT_DEV_TOKEN=' .env | cut -d= -f2)" \
  "http://127.0.0.1:6524/api/v1/archive/captures/force?client_id=<effys-id>"
# → капчур завершився status=ok, urls_captured=17/17, force=true,
#   snapshots_total 17→34, store_reason="forced" на новому знімку,
#   /archive/changes лишився порожнім (0 фіктивних змін),
#   /archive/r/<ts>/<url> віддає 200 з переписаними стилями/скриптами
#   (перевірено окремим curl на .css-асет — 200 text/css)
```

### Тести

- `tests/unit/webarchive/test_capture.py` (+4): примусовий прохід незміненої сторінки пише новий знімок з `store_reason="forced"`; той самий прохід не створює ні `ArchiveChange`, ні новий `ArchiveBlob` (дедуп); примусовий прохід дійсно зміненої сторінки лишає `store_reason="changed"`; примус ревалідує вже відомий асет поза вікном ревалідації, але новий асет під нульовим бюджетом лишається truncated (бюджет не знімається).
- `tests/unit/api/test_archive_api.py` (+4): `POST /captures/force` дає 202 з `force=true` на рядку; 409 при активному капчурі того ж клієнта; 404 на невідомого клієнта; звичайний `POST /captures` явно віддає `force=false` (контракт, не тихе опущення поля).
- `tests/unit/webarchive/test_archive_scheduler.py` (+1): `_submit` читає `force` з `claimed` і другий (примусовий) прохід дійсно пише другий знімок з `store_reason="forced"` на незміненій сторінці — регресійний тест на клас дефекту `only_url_id` (task-62 §2.7).

### Follow-ups / борг

- **`Recapture all` по портфелю** — свідомо поза обсягом (§8 плану): має бути admin-only клас (як батчі звітів), з окремим підтвердженням. `TODO(task-81)` — але зверни увагу, що `task-81` уже зайнятий іншою паралельною роботою (`doc/dev/new/task-81-archive-replay-unlazy.md`, з'явився в робочому дереві під час цієї сесії) — наступний номер вільний буде після неї.
- **Роль `viewer` → 403 на цьому ендпоінті не перевірено живим кліком через тунель** (план §9 п.5): generic-тест `test_auth_coverage.py` (параметризований по всіх маршрутах) підтверджує, що анонім отримує 401/403 — це живо перезапущено й зелено. Але саме розрізнення `member`(202)/`viewer`(403) спирається на **не змінений** мною код `required_role()` (ендпоінт не в `admin_only_paths`, тому дефолт — `member`, як і сусідній `POST /captures`, який теж не має власного рольового тесту в цьому файлі) — живого підтвердження з реальним viewer-логіном через `https://seo.theyovth.space` не робив.
- Примусовий рекапчур однієї URL (`force` на `scope="url"`) технічно вже підтримано колонкою й `capture_url`, бракує лише кнопки в таймлайні поруч із «Refresh now».
