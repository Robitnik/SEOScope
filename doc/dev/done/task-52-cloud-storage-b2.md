# task-52 — Хмарне сховище (Backblaze B2): бекап БД, звіти клієнтів, веб-архів

- **Пакет:** один хмарний шар (`app/cloud/**`) поверх Backblaze B2, який закриває три різні потреби одним механізмом: (1) щоденний бекап SQLite **поза машиною**, (2) копія всіх звітів клієнтів (наші PDF/XLSX + PDF з Reporting Ninja), (3) наш **власний веб-архів** — тіла сторінок клієнтських сайтів, які монітор уже зберігає локально.
- **Хвиля:** O — операційна стійкість (перша таска хвилі).
- **Depends on:** **13** ✅ (`scripts/backup_sqlite.py`, `restore_sqlite.py`, `docs/backup-restore.md`), **35** ✅ (`monitor_blobs`), **42** ✅ (`report_files`)
- **Паралельність:** таска **не редагує** `app/monitor/**`, `app/reports/**`, `app/api/monitor.py`, `app/api/reports.py` — тому **безпечна поруч із task-50/51**, які зараз тримають ці файли (див. §3.4: механізм навмисно pull-based).
- **PLAN.md:** §13/§14 (секрети не потрапляють у БД/логи/UI), §18 (бекап, retention), §5.1 (один writer у SQLite)
- **frontend/AGENTS.md:** не задіяний (UI — follow-up §8)
- **AISeoTools:** змін не потребує

---

## Мета (робота оператора, яку закриваємо)

Сьогодні **вся** історія проєкту живе на одній машині: 548 МБ SQLite + 672 МБ артефактів на локальних docker-волюмах, бекап — теж на тому самому диску (`app_data/backups`, 1.3 ГБ). Втрата диска = втрата всього: метрик за всі періоди, звітів, які вже пішли клієнтам, і накопиченого веб-архіву. `scripts/backup_sqlite.py` (task-13) робить правильний узгоджений знімок — але кладе його поруч із оригіналом, тобто від жодного реального сценарію втрати не страхує.

Друга частина мети — не «сховище», а **продукт**: власний веб-архів. Монітор уже щогодини зберігає тіла сторінок у content-addressed сховище; у хмарі ця історія стає такою, яку не соромно тримати роками й на яку можна спертися в розмові з клієнтом («ось як ця сторінка виглядала 3 місяці тому, до їхніх правок»).

---

## 1. Дослідження (усе виміряно живими викликами 2026-08-19)

### 1.1 Акаунт і бакети

`b2_authorize_account` віддав:

```
accountId    edba7cedd504
apiUrl       https://api003.backblazeb2.com
downloadUrl  https://f003.backblazeb2.com
s3ApiUrl     https://s3.eu-central-003.backblazeb2.com
```

Бакети після цієї сесії:

| Бакет | Тип | Роль |
|---|---|---|
| `TheYovth` | allPublic | CDN-асети (як було). **Цією таскою не використовується.** |
| `craftstore` | allPublic | інший проєкт, поза зоною |
| **`theyovth-vault`** | **allPrivate** | **створено тут:** бекапи БД, звіти клієнтів, веб-архів |

`CDN_URL=https://cdn.theyovth.space` мапиться **на корінь бакета `TheYovth`**: `GET /blog_page_29_mobile.png` → `200` з `x-bz-file-id`/`x-bz-content-sha1` і `nel: cf-nel` (Cloudflare перед B2). Але `GET /_smoke/probe.txt` того самого бакета → **403**, тоді як прямий `https://f003.backblazeb2.com/file/TheYovth/_smoke/probe.txt` → `200`. Отже на CDN стоїть правило, що пускає не всі шляхи/розширення. **Висновок:** CDN — не канал доступу для цієї таски; читаємо через B2 API.

### 1.2 Ключ у `.env` був master-ключем акаунта — замінено на два обмежені

Первісний `B2_APPLICATION_KEY_ID` не мав обмеження по бакету і мав **усі 27 капабіліті**, включно з `deleteBuckets`, `writeKeys`, `deleteKeys`, і бачив бакет `craftstore` іншого проєкту. Витік такого ключа з контейнера = повний контроль над акаунтом.

Створено й **перевірено живим прогоном** два ключі (B2 обмежує ключ рівно одним бакетом, тому їх два):

```
yovth-report-app    id 003edba7cedd5040000000003  bucket TheYovth
yovth-report-vault  id 003edba7cedd5040000000005  bucket theyovth-vault   ← ним працює ця таска
caps (обидва): listBuckets, readBuckets, listFiles, readFiles, writeFiles, deleteFiles
```

Перевірено на `yovth-report-vault`: авторизація ✅, `get_bucket_by_name("theyovth-vault")` ✅, upload+download+delete ✅, `get_bucket_by_name("TheYovth")` → `RestrictedBucket` ✅ (тобто ключ бекапів не дістає до публічного бакета), `create_key` → `Unauthorized` ✅. Важлива деталь реалізації: `list_buckets()` **без аргументів** на обмеженому ключі кидає `RestrictedBucket` — код мусить резолвити бакет по імені, а не перелічувати акаунт.

Master-ключ у `.env` перейменовано на `B2_MASTER_KEY_ID`/`B2_MASTER_APPLICATION_KEY` (застосунок їх не читає — `Settings` має `extra="ignore"`). Їх треба перенести в password manager і прибрати з файлу; потрібні лише для адміністративних дій (створення бакетів, ротація ключів, lifecycle-правила).

### 1.3 Приватність дає **тип бакета**, а не шифрування на нашому боці

Рішення користувача (2026-08-19, §8): шифрування з власним ключем (SSE-C) **не використовуємо** — ключ, втрата якого робить бекапи безповоротно нечитними, це погана угода. Натомість приватність забезпечує сам бакет. Перевірено живим прогоном на `theyovth-vault`:

| Спроба | Результат |
|---|---|
| Анонімно, прямий B2 URL (`f003.backblazeb2.com/file/theyovth-vault/…`) | **401 Unauthorized** |
| Ключем `yovth-report-vault` | ✅ байти повернулись |
| Ключем `yovth-report-vault` → бакет `TheYovth` | **`RestrictedBucket`** |

Ключова властивість цього рішення: **втратити доступ до бекапів неможливо**. Ключ застосунку — не криптоключ, а креденшел: якщо він загубився чи скомпрометований, у панелі B2 випускається новий, а дані лишаються читними. Ціна — B2 бачить дані у відкритому вигляді (шифрування at-rest на їхньому боці, керовані ними ключі); для внутрішнього дашборду агенції це прийнятний обмін, і він свідомий.

### 1.4 Обсяги й вартість (усе з живої машини)

| Дані | Локально зараз | У хмарі |
|---|---|---|
| SQLite `app.db` | **548 МБ** | gzip → **110 МБ за 9 с** (виміряно `gzip -6`) |
| `artifacts/` усього | 672 МБ | ↓ |
| ‣ `artifacts/monitor` (веб-архів) | 508 МБ у 7 902 файлах | 490 МБ gz, avg **64 КБ**/блоб |
| ‣ `artifacts/reports` | 55 МБ | 103 файли (`pdf` 72 / **53 МБ**, `client_pdf` 15, `xlsx` 16) |
| ‣ `artifacts/tech_audit`, `semrush_runs` | 64 + 44 МБ | **не в scope** (регенерується перезбором) |

Приріст веб-архіву за 8 днів спостережень: **300–800 нових блобів/добу ≈ 20–38 МБ gz/добу** (перший день 3 828 блобів — це первинне наповнення, не норма).

Вартість за обраним ретеншеном (B2 $6/ТБ·міс, ingress безкоштовний):

```
db/      GFS 30 добових + 12 тижневих + 12 місячних ≈ 54 × 110 МБ ≈ 6 ГБ ≈ $0.04/міс
archive/ ~30 МБ/добу                                ≈ 11 ГБ/рік         ≈ $0.07/міс (рік 1)
reports/ 103 файли + ~10/міс                        ≈ 0.1 ГБ            ≈ $0.001/міс
```

Тобто повний off-site бекап усього критичного коштує **центи**. Обмежувальний фактор — не гроші, а щоб механізм не був крихким.

### 1.5 Обидві точки запису вже мають реєстр у БД — правок чужих модулів не треба

- `monitor_blobs` (7 893 рядки): PK `sha256`, `size_bytes`, `gzip_size_bytes`, `path` (відносний до artifacts root), `first_seen_at`, `last_seen_at`. `store_blob()` викликається з 4 місць (`app/monitor/runner.py` ×3, `app/monitor/redirects/collect.py`) — **у них ми не заходимо**.
- `report_files` (103 рядки): `path`, `sha256`, `size_bytes`, `mime`, `kind`, `created_at`.

Обидві таблиці — готова робоча черга: «що на диску є, а в хмарі ще нема». Це дозволяє pull-модель (§3.4) і повністю знімає колізію з task-50/51.

### 1.6 Планувальник у воркері — наявний, перевірений патерн

`app/jobs/worker.py` уже підіймає `MonitorScheduler` і `ReportScheduler` — окремі треди з власним тіком у **тому самому** процесі (PLAN §5.1: другий процес = другий writer у SQLite, заборонено). `CloudScheduler` іде тим самим шляхом, а не host-cron: cron-рядки з `docs/backup-restore.md` живуть у документації й ніхто не гарантує, що вони встановлені на машині.

### 1.7 Ліба — вже встановлена й перевірена

`b2sdk = "^2.8"` (розгорнулось у **2.12.0**) додано в `pyproject.toml` + `poetry.lock`. Транзитивно тягне **лише** `logfury 1.0.1` — `requests`/`urllib3` уже були. Перевірено в контейнері: `import b2sdk.v2`, `B2Api`, upload/download/delete, обмежені ключі, створення приватного бакета. Native B2 API, не boto3/S3-compat: ключі акаунта — нативні B2-ключі, а SDK сам дає large-file upload, перевірку sha1, `copy_file` на боці сервера і серверні lifecycle-правила.

---

## 2. Обсяг (scope)

**Входить:**

1. `app/cloud/**` — клієнт B2, розкладка ключів, ledger, sweeper, планувальник.
2. Щоденний бекап БД у `theyovth-vault` з GFS-ретеншеном (30 добових / 12 тижневих / 12 місячних) і перевіркою цілісності.
3. Дзеркало `report_files` (наші PDF/XLSX **і** PDF з Reporting Ninja).
4. Веб-архів: аплоад блобів моніторингу, **тільки нових** (від моменту активації; 7 902 наявних не мігруємо — рішення користувача §8).
5. CLI: `cloud-backup`, `cloud-sync`, `cloud-verify`, `cloud-prune`, `cloud-restore`, `cloud-status`.
6. `GET /api/v1/cloud/status` — стан для оператора (без секретів).
7. Оновлення `docs/backup-restore.md` (розділ «Off-site») + `docs/runbook.md`.

**Не входить (свідомо):**

- Міграція 7 902 наявних блобів (§8, follow-up).
- Read-through із хмари, коли локального блоба нема (§8 — потрібно чіпати `app/monitor/storage.py`, зона активних тасок).
- `tech_audit`/`semrush_runs` артефакти — регенеровані, не історія.
- Browser-профілі й `.env` — містять секрети, у бекап не йдуть (як і зараз, `docs/backup-restore.md`).
- Публічна віддача звітів клієнтам через CDN-лінки (§8 — продуктова фіча з правами доступу, не бекап).
- UI-сторінка стану хмари (§8).

---

## 3. Рішення

### 3.1 Розкладка ключів у бакеті `theyovth-vault`

```
db/daily/app-20260819T033000Z.db.gz          щоденний знімок
db/weekly/app-20260817T033000Z.db.gz         промоція знімка за неділю
db/monthly/app-20260801T033000Z.db.gz        промоція знімка за 1-ше число
db/daily/app-20260819T033000Z.manifest.json  sha256 і розміри до/після gzip, alembic revision
reports/<client_slug>/<yyyy>/<ім'я файла>    PDF/XLSX/Ninja-PDF (ім'я — з report_files)
archive/<sha256[:2]>/<sha256>.gz             тіло сторінки, як на диску (content-addressed)
_smoke/…                                     службовий префікс самоперевірки, чиститься
```

Три речі принципові:

- **`archive/` дзеркалить локальну content-addressed схему** (`monitor/<sha>[:2]/<sha>.gz`) один-до-одного: ключ = адреса контенту, тому повторний аплоад тих самих байтів ідемпотентний за визначенням, а відновлення — просте копіювання.
- **`manifest.json` поруч із кожним дампом** — щоб `cloud-restore` перевіряв цілісність, **не** розпаковуючи 110 МБ, і щоб було видно, під яку версію схеми (alembic revision) цей дамп.
- **GFS реалізуємо своїм `cloud-prune`, а не лише lifecycle-правилом B2.** B2 lifecycle — двокрокове (`daysFromUploadingToHiding` → `daysFromHidingToDeleting`) і працює по префіксу, тобто «12 тижневих» ним не виражається й не покривається юніт-тестами. Своє правило детерміноване й тестоване; lifecycle-правило на `_smoke/` і `db/daily/` ставимо **додатково**, як запобіжник на випадок, що воркер довго не працює (одноразово, master-ключем — у ключа застосунку нема `writeBucketLifecycleRules`).

### 3.2 Клієнт: `app/cloud/client.py`

Тонка обгортка над `b2sdk.v2`, єдине місце, що знає про B2:

```python
class CloudStore:                       # створюється з Settings, лениво авторизується
    def put_file(path, key, *, content_type=None) -> CloudObject
    def put_bytes(data, key) -> CloudObject
    def get_to_file(key, dest) -> None
    def copy(src_key, dest_key) -> CloudObject      # серверна копія, без повторного трафіку
    def exists(key) -> CloudObject | None
    def delete(key) -> None
    def list_prefix(prefix) -> Iterator[CloudObject]   # ліниво, з пагінацією
```

- **Бакет резолвиться по імені** (`get_bucket_by_name`), ніколи через `list_buckets()` — на обмеженому ключі це `RestrictedBucket` (§1.2).
- **Ретраї:** `b2sdk` сам ретраїть транспорт; ми додаємо один зовнішній рівень з експоненційним backoff на `B2ConnectionError`/`ServiceError`/429, максимум 3 спроби, і **ніколи** не ретраїмо `Unauthorized`/`BadRequest` (це конфіг, а не мережа).
- **Секрети (PLAN §13):** ключі не логуються й не потрапляють у текст помилок, відданих нагору. `app/core/logging.py` (зона task-01, адитивно) отримує `B2_VAULT_APPLICATION_KEY`, `B2_APPLICATION_KEY`, `B2_MASTER_APPLICATION_KEY` у `redact_keys` — той самий механізм, що вже маскує ключі провайдерів.
- **`cloud_enabled=False` → усі операції no-op** із одним `info`-логом. Локальна розробка без хмари мусить працювати без правок конфігу.

### 3.3 Ledger `cloud_objects` (нова таблиця, нова міграція)

Стан «що вже в хмарі» не можна тримати ані в пам'яті, ані на диску: після рестарту він мусить бути точним, інакше ми або перезаливаємо все, або тихо пропускаємо. Тому — таблиця:

```
cloud_objects
  id            GUID PK
  kind          "db_backup" | "report_file" | "monitor_blob"
  source_ref    str(200)   # sha256 блоба | report_files.id | ім'я дампа
  key           str(500)   # ключ у бакеті
  sha256        str(64)    # локальний sha256 вихідних байтів
  size_bytes    int
  status        "pending" | "uploaded" | "failed" | "deleted"
  attempts      int
  error_message str(1000) | None   # без секретів
  uploaded_at   datetime | None
  created_at    datetime
  UNIQUE (kind, source_ref)
  INDEX (kind, status)
```

`UNIQUE (kind, source_ref)` — це і є ідемпотентність: повторний прогін sweeper'а не створює дублікатів, а `status` каже, що робити. Міграція — **лише створення нової таблиці**, жодного `ALTER` наявних (щоб не перетнутися з task-03/50/51). Файл `migrations/versions/<rev>_cloud_objects.py` — тим самим шляхом, яким task-51 додала свою міграцію.

### 3.4 Sweeper замість хуків у точках запису — головне архітектурне рішення

Наївний варіант — вставити аплоад у `store_blob()` і в місце, де створюється `ReportFile`. Ми так **не** робимо, з трьох причин:

1. `app/monitor/**` і `app/reports/**` — зони активних task-50/51 (правило антиколізії в `CLAUDE.md`);
2. аплоад у гарячому шляху збору робить мережу B2 частиною надійності монітора: тайм-аут хмари почав би валити прогін клієнта;
3. після рестарту хук нічого не наздоганяє — те, що не залилось, не залито назавжди.

**Рішення:** `CloudSweeper` читає **наявні** реєстри й дозаливає різницю:

```
кандидати(monitor_blob) = monitor_blobs LEFT JOIN cloud_objects → status IS NULL / 'failed'
                          AND first_seen_at >= cloud_archive_since
кандидати(report_file)  = report_files  LEFT JOIN cloud_objects → status IS NULL / 'failed'
```

Властивості, які з цього беруться безкоштовно: нуль правок у чужих owned paths; збій хмари ніколи не впливає на збір; рестарт наздоганяє сам; «тільки нове» — це один рядок з watermark, а майбутній backfill — просто зсув цього watermark у минуле (`cloud-sync --since 2026-08-01`).

`cloud_archive_since` фіксується **при першій активації** окремим рядком-watermark у `cloud_objects`, а не ручним конфігом на машині.

### 3.5 Бекап БД

Один тік на добу (`cloud_backup_at_utc`, дефолт `03:30`):

1. `sqlite3.Connection.backup()` у тимчасовий файл — **той самий** узгоджений онлайн-бекап, що вже реалізований у `scripts/backup_sqlite.py` (WAL, застосунок не спиняється). Логіку переюзаємо імпортом, не копіпастом.
2. `PRAGMA integrity_check` на знімку (уже є в скрипті) — провал = нічого не заливаємо, `error`-лог і повтор наступного тіка.
3. gzip у потоці (548 МБ → 110 МБ / 9 с) + sha256 обох представлень.
4. Аплоад `db/daily/…`; `b2sdk` перевіряє sha1 на боці сервера; поруч — `…manifest.json` (sha256, розміри, alembic revision, версія застосунку).
5. Промоція **серверною копією** (`copy_file`, без повторного трафіку): неділя → `db/weekly/`, 1-ше число → `db/monthly/`.
6. `cloud-prune`: у `db/daily/` лишити 30 найновіших, у `weekly/` 12, у `monthly/` 12. **Ніколи** не видаляти, якщо в префіксі не більше за ліміт — щоб баг у сортуванні не міг зачистити останню копію.
7. Тимчасові файли прибираються в `finally` (110 МБ + 548 МБ на диску не лишаємо).

Локальний `app_data/backups` лишається як є (`--keep 14`): хмара його не замінює, вона додає off-site.

### 3.6 Веб-архів

Аплоад блоба = читання готового `.gz` із диска й `put_file` під `archive/<sha>[:2]/<sha>.gz`. Ніякої перепаковки: локальний файл уже gzip, content-addressed, і його sha256 у `monitor_blobs` — джерело для перевірки.

- Пачками (`cloud_sweep_batch_size`, дефолт 200) на тік, паралельність `cloud_upload_workers` (дефолт 4). Приріст 300–800 блобів/добу означає величезний запас навіть при одному тіку на 5 хвилин.
- **Блоба, якого нема на диску, не вважаємо помилкою назавжди:** `status="failed"` + причина `local_file_missing`. Це чесний стан (PLAN §12), а не тихий пропуск.
- Ідемпотентність — на трьох рівнях: `UNIQUE (kind, source_ref)`, content-addressed ключ, `exists()`-перевірка перед аплоадом.

### 3.7 Звіти клієнтів

Той самий sweeper, `kind="report_file"`, але ключ **людський**, бо ці файли колись читатиме людина: `reports/<client_slug>/<yyyy>/<оригінальне ім'я файла>`. `client_slug` — той самий, що вже використовує `app/reports/naming.py`; `yyyy` — з `report_files.created_at`. Колізія імен розв'язується суфіксом `-<sha256[:8]>`, а не перезаписом.

Покриває обидві просьби одним механізмом: `kind="pdf"` — це саме PDF, завантажені з Reporting Ninja, `client_pdf`/`xlsx` — наші власні.

### 3.8 Де це виконується + конфіг

`CloudScheduler` (тред у воркері, тік `60 c`, той самий патерн, що `MonitorScheduler`): раз на добу — бекап БД, кожен тік — sweeper-пачка. У процесі `api` — **нічого**, крім читання стану для `/cloud/status`.

`app/core/config.py` (зона task-01, адитивно — задокументувати в Completion report):

```python
cloud_enabled: bool = False              # вимикач; True вимагає повного набору креденшелів
b2_vault_bucket_name: str = ""           # theyovth-vault (приватний)
b2_vault_key_id: str = ""
b2_vault_application_key: str = ""       # секрет: у БД/логи/UI не потрапляє
cloud_backup_at_utc: str = "03:30"
cloud_backup_keep_daily: int = 30
cloud_backup_keep_weekly: int = 12
cloud_backup_keep_monthly: int = 12
cloud_sync_reports: bool = True
cloud_sync_monitor_blobs: bool = True
cloud_sweep_batch_size: int = 200
cloud_upload_workers: int = 4
cloud_request_timeout_seconds: float = 60.0
```

Публічний бакет і CDN (`B2_BUKET_NAME`, `CDN_URL`) цією таскою **не читаються** — вони лишаються в `.env` для майбутньої публічної віддачі (§8).

`cloud_enabled=True` без ключів = **відмова на старті з явним повідомленням**, а не тихий no-op: «хмара увімкнена, але не налаштована» — це стан, який мусить бути видно негайно.

### 3.9 CLI (`app/cli.py`, адитивно — нові підкоманди)

```bash
docker compose exec worker poetry run yovth-report cloud-status
docker compose exec worker poetry run yovth-report cloud-backup            # позачерговий бекап
docker compose exec worker poetry run yovth-report cloud-sync --kind monitor_blob --limit 500
docker compose exec worker poetry run yovth-report cloud-sync --since 2026-08-01   # backfill
docker compose exec worker poetry run yovth-report cloud-verify --sample 20
docker compose exec worker poetry run yovth-report cloud-prune --dry-run
docker compose exec api    poetry run yovth-report cloud-restore --latest --out /app/app_data/restore
```

`cloud-restore` **не** торкається робочої БД: він лише кладе перевірений розпакований дамп на диск і друкує готову команду `scripts/restore_sqlite.py` (той уже має `--force`, перевірку sha256 і `integrity_check`). Дві незворотні операції в одній команді — це те, чого в runbook бути не має.

`cloud-verify` бере випадкову вибірку об'єктів кожного `kind`, тягне їх із хмари й **порівнює sha256 з локальним**. Бекап, який ніхто не читав, — не бекап.

### 3.10 API стану

`GET /api/v1/cloud/status` (новий роутер `app/api/cloud.py`):

```json
{
  "enabled": true,
  "bucket": "theyovth-vault",
  "last_backup": {"key": "db/daily/app-20260819T033000Z.db.gz",
                  "uploaded_at": "2026-08-19T03:30:41Z",
                  "size_bytes": 115343360, "status": "uploaded"},
  "backups": {"daily": 30, "weekly": 12, "monthly": 9},
  "queue": {"monitor_blob": {"pending": 12, "failed": 0, "uploaded": 4821},
            "report_file":  {"pending": 0,  "failed": 0, "uploaded": 103}},
  "archive_since": "2026-08-19"
}
```

Жодного ключа, жодного підписаного URL, жодного шляху хоста — тільки стан. Схема — `app/schemas/cloud.py` (нова, нічого наявного не змінює).

---

## 4. Owned paths

**Нові (повністю наші):**

```
app/cloud/__init__.py
app/cloud/client.py         CloudStore — єдине місце з b2sdk
app/cloud/keys.py           розкладка ключів + slug/ім'я файла
app/cloud/backup.py         дамп → gzip → аплоад → промоція → prune
app/cloud/sweeper.py        outbox по monitor_blobs / report_files
app/cloud/scheduler.py      CloudScheduler (тред у воркері)
app/cloud/ledger.py         репозиторій cloud_objects
app/db/repositories/cloud.py
app/api/cloud.py
app/schemas/cloud.py
migrations/versions/<rev>_cloud_objects.py
tests/unit/cloud/**
tests/unit/api/test_cloud_api.py
doc/dev/new/task-52-cloud-storage-b2.md   (цей файл → done)
```

**Дотик до чужих зон — мінімальний, задокументований:**

| Файл | Зона | Зміна |
|---|---|---|
| `pyproject.toml`, `poetry.lock` | task-01 | `b2sdk = "^2.8"` — **уже зроблено**, з коментарем-обґрунтуванням |
| `app/core/config.py` | task-01 | лише **додані** поля (§3.8) |
| `app/core/logging.py` | task-01 | `redact_keys` += ключі B2 |
| `app/db/models.py` | task-03 | **нова** модель `CloudObject`, наявні не змінюються |
| `app/jobs/worker.py` | task-05 | один рядок: підняти `CloudScheduler` (патерн `MonitorScheduler`) |
| `app/main.py` | task-01 | реєстрація роутера `cloud` |
| `app/cli.py` | task-07 | нові підкоманди |
| `docs/backup-restore.md`, `docs/runbook.md` | task-13 | розділ «Off-site» |

**Не торкаємось:** `app/monitor/**`, `app/reports/**`, `app/api/monitor.py`, `app/api/reports.py`, `app/schemas/monitor.py`, `app/schemas/reports.py`, `frontend/**` — саме тому таска сумісна з task-50/51.

---

## 5. Тести

`tests/unit/cloud/` — з фейковим сховищем (`FakeCloudStore` у пам'яті), без мережі:

- `test_keys.py` — розкладка ключів: content-addressed `archive/`, людські `reports/<slug>/<yyyy>/`, колізія імен → суфікс sha, жодного шляху за межі свого префікса.
- `test_backup.py` — знімок → gzip → аплоад → manifest; `integrity_check` провалено → **нічого не залито**; промоція в `weekly`/`monthly` за календарем через `copy`; тимчасові файли прибрані.
- `test_prune.py` — GFS-межі точно; **не більше за ліміт → нуль видалень**; `--dry-run` не видаляє; порожній префікс не падає.
- `test_sweeper.py` — ідемпотентність (два прогони = один об'єкт), watermark «тільки нове», `failed` → повтор наступного тіка, відсутній локальний файл → `failed(local_file_missing)`, збій хмари не кидає виняток нагору.
- `test_client.py` — бакет резолвиться по імені (не `list_buckets()`); ретрай на 429/`ServiceError` і **відсутність** ретраю на `Unauthorized`; ключі не потрапляють у `str(exception)`.
- `test_scheduler.py` — бекап один раз на добу навіть при 1440 тіках; sweeper обмежений `batch_size`; `cloud_enabled=False` → жодного виклику.
- `test_cloud_api.py` — форма `/cloud/status`, відсутність секретів у відповіді, стан при `enabled=false`; **оновлення `openapi_snapshot.json`**.
- `test_config.py` (адитивно) — `cloud_enabled=True` без ключів → помилка старту.

**Live smoke (руками, у контейнері, префікс `_smoke/`):** авторизація ключем `yovth-report-vault` → аплоад → анонімний GET мусить дати **401** → авторизований мусить дати байти → delete. Це рівно той прогін, яким перевірено §1.3; лишається в `scripts/live_smoke.py` окремою секцією.

---

## 6. Ризики та запобіжники

| Ризик | Запобіжник |
|---|---|
| Аплоад з'їдає диск (тимчасові 548 МБ + 110 МБ) | стрімовий gzip, `finally`-прибирання, тест на прибирання |
| Хмара «падає» й тягне за собою збір | pull-модель (§3.4): sweeper ізольований від прогонів монітора/звітів; помилки — у ledger, не у виняток |
| `cloud-prune` зачищає останню копію через баг у сортуванні | ніколи не видаляти при кількості ≤ ліміту; `--dry-run`; тест на «менше за ліміт» |
| Ключ застосунку витік | обмежений одним бакетом і 6 капабіліті (§1.2); без `writeKeys`/`deleteBuckets`; ротація — одна команда master-ключем, дані при цьому не втрачаються |
| Тихий «успіх» без реального бекапу | `cloud-verify` порівнює sha з хмари з локальним; `/cloud/status` показує вік останнього бекапу; вік > 48 год = видима аномалія |
| Дані лежать у B2 нешифровані нашим ключем | свідомий обмін (§1.3): бакет приватний, ключ обмежений; шифрування з власним ключем відхилене через ризик безповоротної втрати |
| Випадковий аплоад у публічний `TheYovth` | ключ `yovth-report-vault` до нього **не дістає** (`RestrictedBucket`) — структурно, а не за домовленістю |
| CDN пускає не всі шляхи (§1.1) | CDN не використовується як канал доступу; читання — через B2 API |

---

## 7. Definition of Done / Gate

1. `cloud_enabled=True` на реальній машині: **живий** прогін `cloud-backup` заливає `db/daily/…` + manifest; `cloud-verify --sample 5` зелений; `cloud-restore --latest` дає файл, який проходить `integrity_check` і перевірку `restore_sqlite.py` **без** `--force`.
2. Анонімний GET будь-якого об'єкта з `theyovth-vault` віддає **401** (перевірено вручну, лог у Completion report).
3. Sweeper за добу довів `report_file` до `pending=0`, а `monitor_blob` тримає `pending` близько нуля; `failed=0` або кожен `failed` має зрозумілу причину.
4. Промоція `weekly`/`monthly` перевірена (штучні дати в тестах + один живий тиждень).
5. `cloud_enabled=False` → застосунок працює як раніше; жодного мережевого виклику (тест).
6. `ruff check .`, `mypy app`, `pytest tests/unit/cloud tests/unit/api -q` — зелені **в контейнері**; `openapi_snapshot.json` оновлений.
7. У логах і в `/cloud/status` **немає** ключів (перевірено `grep` по логах за прогін).
8. Master-ключ B2 прибраний з `.env` і лежить у password manager користувача.
9. `docs/backup-restore.md` містить робочий off-site розділ: як зняти, як перевірити, як відновити з нуля на новій машині.
10. План перенесено в `doc/dev/done/` з Completion report; `INDEX.md` оновлено.

---

## 8. Підтверджені рішення користувача та follow-ups

**Рішення 2026-08-19 (зафіксовані):**

- **Без шифрування з нашим ключем.** SSE-C відхилено: ключ, втрата якого робить бекапи нечитними, — неприйнятний ризик. Приватність дає окремий **приватний бакет** `theyovth-vault` (створено, перевірено: анонімно 401).
- **Публічний `TheYovth` лишається як є** — CDN-асети, поза зоною цієї таски.
- **Веб-архів: тільки нове**, наявні 7 902 блоби (508 МБ) не мігруємо.
- **Ретеншен бекапів: GFS** 30 добових + 12 тижневих + 12 місячних (~6 ГБ, ~$0.04/міс).
- **Обмежені ключі створено** (`yovth-report-app` → `TheYovth`, `yovth-report-vault` → `theyovth-vault`); master перейменовано на `B2_MASTER_*`.

**Follow-ups:**

- `TODO(task-53)` **read-through веб-архіву**: коли локального блоба нема — тягнути з B2 у локальний кеш. Потребує правки `app/monitor/storage.py` (зона активних task-50/51), тому окремою таскою. Це ж розблокує локальний prune (`monitor-prune --bodies-older-than`) без втрати історії.
- `TODO(task-53)` **backfill 7 902 блобів** — механізм уже буде (`cloud-sync --since`), лишиться один прогін на ~490 МБ.
- UI-картка стану хмари (вік останнього бекапу, черга) — поки що `cloud-status` + `/cloud/status`.
- Публічні share-лінки на звіти для клієнтів через `CDN_URL` — окрема продуктова таска: потрібні права доступу, TTL і рішення, що саме публічне (плюс з'ясувати правило CDN із §1.1).
- `tech_audit`/`semrush_runs` артефакти — якщо колись знадобиться їх історія, це той самий sweeper з новим `kind`.
- Lifecycle-правило B2 на `_smoke/` і `db/daily/` як запобіжник на випадок довгого простою воркера (одноразово, master-ключем).

---

## ✅ Completion report

- **Дата:** 2026-08-19
- **Гілка:** `task-34-client-lifecycle-status` (не закомічено — комітить користувач)
- **Статус:** done-with-followups

### Що зроблено

Повний хмарний шар `app/cloud/**` над Backblaze B2: щоденний off-site бекап SQLite з GFS-ретеншеном (30 добових / 12 тижневих / 12 місячних) + manifest, дзеркало `report_files` (наші PDF/XLSX і PDF з Reporting Ninja) та pull-sweeper для веб-архіву моніторингу (`monitor_blobs`, «тільки нове» від watermark). `CloudScheduler` — тред у worker-процесі (як `MonitorScheduler`/`ReportScheduler`), CLI (`cloud-status/-backup/-sync/-verify/-prune/-restore`), `GET /api/v1/cloud/status`, повний набір юніт-тестів.

**Живий E2E-прогін проти реального акаунта (не мок):**

```
docker compose exec -T -e CLOUD_ENABLED=true api poetry run yovth-report cloud-backup
→ backup uploaded: db/daily/app-20260819T195613Z.db.gz (110453752 bytes, sha256 670e29fe3c7b…)

docker compose exec -T -e CLOUD_ENABLED=true api poetry run yovth-report cloud-verify --sample 3
→ db_backup daily/2026-08-19 OK — 1/1 verified OK

docker compose exec -T -e CLOUD_ENABLED=true api poetry run yovth-report cloud-sync --kind report_file --limit 5
→ report_file: attempted=5 uploaded=5 failed=0

docker compose exec -T -e CLOUD_ENABLED=true api poetry run yovth-report cloud-restore --latest --out /app/app_data/restore-test
→ downloaded 552722432 bytes, integrity_check: ok, alembic revision: 08824e25f060
```

Anонімний GET на `theyovth-vault` (дослідження §1.3) підтверджено окремо: **401**. `cloud-restore` навмисно НЕ чіпав робочу базу — лише завантажив/перевірив і надрукував готову команду `restore_sqlite.py`; тестовий каталог прибрано (`rm -rf /app/app_data/restore-test`).

**Не робилося навмисно** (див. «Follow-ups нижче»): повний backfill решти 98 `report_files` і старт watermark веб-архіву для реального обсягу — це і є момент «першої активації» (§3.4/§8), який має бути свідомою дією оператора (постійний `CLOUD_ENABLED=true` в `.env` + рестарт worker), а не побічним ефектом реалізації. У бакеті зараз лежить **1 реальний бекап БД + 5 реальних звітів** — це не тестове сміття, а перший справжній off-site бекап проєкту.

### Як зроблено (ключові рішення)

- **Приватний бакет замість шифрування власним ключем.** Первісний план (SSE-C у публічному `TheYovth`) користувач відхилив мід-сесії: ключ, втрата якого робить бекапи нечитними назавжди, — гірший ризик, ніж окремий бакет. Створено `theyovth-vault` (`allPrivate`) + окремий обмежений ключ `yovth-report-vault` (6 капабіліті, прив'язаний лише до цього бакета — `RestrictedBucket` на спробі дістати `TheYovth`, перевірено живим прогоном).
- **Master-ключ акаунта, що був у `.env` до цієї таски, замінено на обмежені.** Оригінальний ключ мав усі 27 капабіліті (`deleteBuckets`, `writeKeys`) і бачив чужий бакет `craftstore`. Перейменовано на `B2_MASTER_*` (застосунок їх не читає) — користувачу лишається перенести в password manager і прибрати з файлу.
- **Pull-sweeper, не хуки в точках запису** (`app/cloud/sweeper.py`): читає наявні `monitor_blobs`/`report_files`, доливає різницю проти власного ledger (`cloud_objects`). Нуль правок у `app/monitor/**`/`app/reports/**` — тому таска безпечна поруч із активними task-50/51. Кожен об'єкт комітиться в ledger окремо (не батчем) — крах посеред sweep нічого не втрачає, просто передивляється ті самі кандидати на наступному тіку.
- **`cloud_objects` — один ledger на всі три типи** (`db_backup`/`report_file`/`monitor_blob`/`watermark`), `UNIQUE(kind, source_ref)` — ідемпотентність без окремих таблиць. GFS-ярус закодовано в `source_ref` (`"daily/2026-08-19"`), а не окремою колонкою — інший ярус не потребує міграції.
- **Головні файли:** `app/cloud/client.py` (єдине місце з `b2sdk`, ретраї з backoff, ніколи не ретраїть `Unauthorized`/`BadRequest`), `app/cloud/keys.py` (розкладка ключів, content-addressed для архіву, людська для звітів із suffix на колізію), `app/cloud/backup.py` (онлайн-бекап + gzip + manifest + серверна `copy()`-промоція GFS), `app/cloud/sweeper.py`, `app/cloud/scheduler.py`, `app/cloud/verify.py`, `app/db/repositories/cloud.py`, `app/api/cloud.py`, `app/schemas/cloud.py`.
- **Відхилення від плану:** `scripts/backup_sqlite.py`'s online-backup логіку не імпортовано (план §3.5 казав «переюзаємо імпортом») — `scripts/` не пакет (нема `__init__.py`) і належить task-13; переписано compact-версією (~15 рядків) замість перетворення чужої зони на пакет заради імпорту.
- **`app/main.py` фактично НЕ чіпався**, хоча план це передбачав — роутери реєструються авто-дискавері (`app/api/__init__.py`), тож `app/api/cloud.py` підхопився сам.

### Контрактні зміни (Contract changes)

- **Нова таблиця** `cloud_objects` (міграція `08824e25f060`, `down_revision=a7c4e91f2b6d`) — жодних змін наявних таблиць.
- **Нові поля `Settings`** (адитивно): `cloud_enabled`, `b2_vault_bucket_name`, `b2_vault_key_id`, `b2_vault_application_key`, `cloud_backup_at_utc`, `cloud_backup_keep_{daily,weekly,monthly}`, `cloud_sync_{reports,monitor_blobs}`, `cloud_sweep_batch_size`, `cloud_upload_workers`, `cloud_request_timeout_seconds` + `model_validator` (`cloud_enabled=True` без повних креденшелів → відмова на старті).
- **Нова публічна DTO-схема** `app/schemas/cloud.py` (`CloudStatusDTO` та інші) — новий, ні на що не впливає ендпоінт `GET /api/v1/cloud/status`.
- **`pyproject.toml`**: `b2sdk = "^2.8"` (розгорнулось у 2.12.0), транзитивно тільки `logfury`.
- Ніщо в `app/monitor/**`, `app/reports/**`, `app/api/monitor.py`, `app/api/reports.py`, `app/schemas/{monitor,reports}.py` не змінено.

### Як перевірити (verification)

```bash
docker compose exec api poetry run alembic upgrade head        # 08824e25f060 (head)
docker compose exec api poetry run ruff check app/cloud app/api/cloud.py app/schemas/cloud.py app/db/repositories/cloud.py
docker compose exec api poetry run mypy app                    # 168 файлів, 0 помилок
docker compose exec api poetry run pytest tests/unit -q        # 1809 passed
docker compose exec worker poetry run yovth-report cloud-status
```

Живий прогін (потребує `CLOUD_ENABLED=true` + `B2_VAULT_*` — вже лежать у `.env`, окрім самого вмикача): `cloud-backup` → `cloud-verify --sample 5` → `cloud-restore --latest --out <tmp>`. Анонімний `GET https://f003.backblazeb2.com/file/theyovth-vault/<будь-який ключ>` мусить дати **401**.

### Тести

`tests/unit/cloud/` (56 тестів): `test_client.py` (disabled/misconfigured guards, retry policy, ретрай НЕ на `Unauthorized`/`BadRequest`, секрети не в тексті помилки), `test_keys.py` (розкладка ключів, колізія), `test_ledger.py` (ідемпотентність upsert, candidates виключають `uploaded`/включають `failed`, watermark, GFS tiers), `test_sweeper.py` (upload + idempotent + `local_file_missing` + retry-next-tick + watermark + limit + name-collision), `test_backup.py` (реальний SQLite через `sqlite3.Connection.backup()`, ідемпотентність «один раз на добу», провал `integrity_check` → нічого не залито, промоція weekly/monthly за датою, GFS prune ніколи не видаляє при кількості ≤ ліміту), `test_verify.py`, `test_scheduler.py` (disabled → жодного треду, backup рівно один раз при 5 викликах, sweep обох kind на одному тіку). Плюс 4 тести в `tests/unit/core/test_config.py` (валідатор креденшелів) і 3 в `tests/unit/api/test_cloud_api.py` (форма відповіді, немає секретів, ізоляція від реального `.env`).

### Follow-ups / борг

- `TODO(task-53)` read-through веб-архіву з B2 при відсутньому локальному блобі (чіпає `app/monitor/storage.py`, зона task-50/51).
- `TODO(task-53)` backfill 7 902 наявних блобів веб-архіву — механізм (`cloud-sync --since`) уже готовий, лишається один прогін.
- **Активація в проді:** `CLOUD_ENABLED=true` в `.env` наразі НЕ виставлено постійно (лише перевірено через `-e CLOUD_ENABLED=true` на одноразових викликах) — постійне увімкнення й рестарт `worker` лишені на рішення користувача, оскільки це запускає безперервний щоденний аплоад.
- Master-ключ B2 (`B2_MASTER_KEY_ID`/`B2_MASTER_APPLICATION_KEY`) досі в `.env` — перенести в password manager і прибрати звідти.
- UI-картка стану хмари — коли буде куди повісити.
