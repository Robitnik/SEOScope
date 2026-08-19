# Backup і restore

Що саме треба берегти й як це повернути. Обидва сценарії покриті тестами
(`tests/integration/test_ops_scripts.py`), тож інструкція нижче — не теорія.

## Що бекапиться

| Дані | Де | Чи потрібен бекап |
|---|---|---|
| SQLite БД (клієнти, snapshot-и, метрики, події) | volume `app_data`, `app_data/app.db` | **так** — це вся історія |
| Artifacts (сирі вивантаження, логотипи) | volume `artifacts` | **так** — не відновлюються перезбором |
| Browser-профілі / сесії | volume `browser_profiles`, `${HOME}/.local/share/seo-audit` | ні — перелогінюються вручну; містять секрети, тож у бекап навмисно не йдуть |
| Ключі провайдерів | `.env` AISeoTools | ні — застосунок їх не зберігає (PLAN §13) |

Усе це — **локальний** бекап, на тому самому диску, що й оригінал. Off-site
копія (інша машина, інший датацентр) — окремий шар, розділ нижче.

## Off-site (Backblaze B2, task-52)

Локальний бекап вище не рятує від втрати самого диска/машини. `CloudScheduler`
(тред у worker-процесі, як `MonitorScheduler`/`ReportScheduler`) щодоби
заливає консистентний знімок БД у приватний бакет `theyovth-vault`, а окремий
sweeper — усі файли звітів клієнтів (наші PDF/XLSX + PDF з Reporting Ninja) і
нові тіла сторінок веб-архіву моніторингу (`monitor_blobs`).

### Що і як зберігається

| Що | Ретеншен | Механізм |
|---|---|---|
| Дамп БД (gzip) | GFS: 30 добових + 12 тижневих + 12 місячних | `run_daily_backup` + `prune_backups` (`app/cloud/backup.py`) |
| Звіти клієнтів | назавжди | pull-sweeper по `report_files` (`app/cloud/sweeper.py`) |
| Веб-архів (тіла сторінок) | назавжди, **лише нові** з моменту активації | pull-sweeper по `monitor_blobs`, watermark `archive_since` |

Sweeper — **pull**, не хук у точці запису: він читає вже наявні реєстри
(`monitor_blobs`, `report_files`) і доливає різницю. Збій хмари ніколи не
валить прогін монітора чи звіту; рестарт воркера сам доганяє відставання.

Бакет `theyovth-vault` — приватний (не той самий, що публічний `TheYovth` із
CDN-асетів): анонімний GET повертає `401`. Ключ застосунку (`B2_VAULT_KEY_ID`/
`B2_VAULT_APPLICATION_KEY`) обмежений саме цим бакетом і не бачить нічого
іншого на акаунті.

### Увімкнути

```bash
# .env (worker/api контейнер)
CLOUD_ENABLED=true
B2_VAULT_BUCKET_NAME=theyovth-vault
B2_VAULT_KEY_ID=...
B2_VAULT_APPLICATION_KEY=...
```

Без усіх трьох `B2_VAULT_*` разом із `CLOUD_ENABLED=true` застосунок **не
стартує** (`Settings.validate_cloud_credentials`) — «хмара увімкнена, але не
налаштована» має бути видно негайно, а не тихим no-op.

### Керування (CLI, у worker-контейнері)

```bash
docker compose exec worker poetry run yovth-report cloud-status
docker compose exec worker poetry run yovth-report cloud-backup
docker compose exec worker poetry run yovth-report cloud-sync --kind monitor_blob --limit 500
docker compose exec worker poetry run yovth-report cloud-verify --sample 20
docker compose exec worker poetry run yovth-report cloud-prune --dry-run
```

`cloud-verify` тягне випадкову вибірку вже залитих об'єктів назад із B2 і
звіряє sha256 з тим, що записано в ledger — бекап, який ніхто не прочитав,
не є перевіреним бекапом.

### Відновити з хмари

```bash
docker compose exec worker poetry run yovth-report cloud-restore --latest \
    --out /app/app_data/restore
```

Команда лише завантажує й перевіряє (`sha256` + `PRAGMA integrity_check`) —
робочої бази не чіпає і друкує готову команду `restore_sqlite.py` (той самий
незворотний порядок — «Відновити» вище — з `--force` як явним другим кроком).

### Ризики

- **Втрата ключа застосунку не смертельна:** новий обмежений ключ
  перевипускається в панелі B2 без втрати даних (це і є причина, чому таска
  свідомо відмовилась від шифрування власним ключем — той ризик був би
  безповоротним).
- **Публічний бакет `TheYovth` цим шаром не використовується взагалі** —
  ключ `B2_VAULT_*` обмежений на `theyovth-vault` і не має доступу до нього.

## Зняти бекап

```bash
docker compose exec api poetry run python scripts/backup_sqlite.py \
    --out-dir /app/app_data/backups --keep 14
```

Стек **зупиняти не треба**: скрипт користується онлайн-бекап API SQLite, а не
копіює файл. Це принципово — база працює в WAL, і `cp app.db` без `-wal`/`-shm`
дав би неузгоджений знімок.

На виході:

```
app-20260722T101500Z.db          знімок БД (перевірений PRAGMA integrity_check)
app-20260722T101500Z.db.sha256   контрольна сума
artifacts-20260722T101500Z.tar.gz
```

`--keep N` лишає N найновіших комплектів і прибирає старіші (разом з їхніми
`.sha256` та архівом artifacts). `--skip-artifacts` бекапить лише БД.

Просяться в cron на хості:

```cron
30 3 * * * cd /path/to/repo && docker compose exec -T api poetry run python scripts/backup_sqlite.py --out-dir /app/app_data/backups --keep 14
45 3 * * 0 cd /path/to/repo && docker compose exec -T api poetry run python scripts/retention_cleanup.py
```

## Відновити

Відновлення — незворотна операція, тому порядок жорсткий.

```bash
# 1. Зупинити тих, хто пише. Обов'язково: writer має бути нуль.
docker compose stop api worker

# 2. Перевірити бекап (без --force скрипт нічого не змінює).
docker compose run --rm api poetry run python scripts/restore_sqlite.py \
    --from /app/app_data/backups/app-20260722T101500Z.db

# 3. Відновити.
docker compose run --rm api poetry run python scripts/restore_sqlite.py \
    --from /app/app_data/backups/app-20260722T101500Z.db \
    --artifacts-archive /app/app_data/backups/artifacts-20260722T101500Z.tar.gz \
    --force

# 4. Підняти назад — entrypoint сам прожене alembic upgrade head.
docker compose up -d
docker compose exec api curl -s localhost:8000/readyz
```

Що робить скрипт до того, як щось торкнеться робочої бази:

1. звіряє `sha256` з файлом-супутником (якщо він є);
2. проганяє `PRAGMA integrity_check` на самому бекапі;
3. переконується, що у файлі є таблиця `clients` — тобто це база **цього**
   застосунку, а не випадковий SQLite;
4. відмовляється працювати без `--force`.

Поточна база **не стирається**: вона (разом з `-wal`/`-shm`) перейменовується
в `app.db.superseded-<стамп>`. Те саме з `artifacts/`. Прибирати їх вручну —
після того, як переконаєшся, що відновлене працює.

## Перевірка після відновлення

```bash
curl -s localhost:6524/readyz
curl -s localhost:6524/api/v1/dashboard | head -c 400
docker compose logs --tail 50 worker
```

На дашборді мають бути ті самі клієнти й дати останнього збору, що й на
момент бекапу. Клітинки, які на момент бекапу були `running`, після старту
підхопить `run_startup_recovery` і поставить у чергу заново.

## Навчальна вправа (робити раз на квартал)

Відновлення, яке ніколи не перевіряли, не є відновленням:

```bash
docker compose run --rm api poetry run python scripts/restore_sqlite.py \
    --from /app/app_data/backups/<останній>.db \
    --database-url sqlite:////tmp/restore-drill/app.db --force
```

Це відновить у тимчасовий файл, не чіпаючи прод. Далі — переконатись, що
кількість клієнтів і метрик збігається з очікуваною.
