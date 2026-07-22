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
