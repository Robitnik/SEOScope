# Runbook — експлуатація The Yovth Clients Report

Операційний довідник: як підняти, як перевірити, що ламається найчастіше і що
з цим робити. Установка й конфігурація — у [README.md](../README.md);
архітектура — у [PLAN.md](../PLAN.md).

Уся Python-робота — **всередині контейнера** (`CLAUDE.md` §«Середовище
виконання»). Єдиний виняток — браузерний e2e фронту, якому потрібен Node на
хості (див. §6).

---

## 1. Запуск і зупинка

```bash
docker compose up -d --build      # api + worker
docker compose ps                 # обидва мають бути healthy
docker compose logs -f worker     # доріжки збору
docker compose down               # зупинити (дані у volumes лишаються)
```

Порядок старту контейнера жорсткий і задається [`docker/entrypoint.sh`](../docker/entrypoint.sh):
перевірка конфігу → `alembic upgrade head` → запуск процесу. Якщо міграція
впала, процес не стартує — це навмисно (PLAN §14: readiness не green, доки
схема не актуальна).

**Ніколи не піднімай другий `worker`.** SQLite тримає одного writer-а
(PLAN §5.1); дві репліки дадуть `database is locked` і биту чергу.

### Швидка перевірка живості

```bash
curl -s localhost:6524/healthz            # процес живий
curl -s localhost:6524/readyz             # + БД відповідає
curl -s localhost:6524/api/v1/providers/status | head
```

---

## 2. Щоденні операції

| Задача | Команда |
|---|---|
| Бекап БД + artifacts | `docker compose exec api poetry run python scripts/backup_sqlite.py --out-dir /app/app_data/backups --keep 14` |
| Відновлення | див. [backup-restore.md](backup-restore.md) |
| Чистка старих подій | `docker compose exec api poetry run python scripts/retention_cleanup.py` |
| Імпорт історії AISeoTools | `docker compose run --rm api poetry run yovth-report import-ai-seo-history --source /seo/AISeoTools --dry-run` |
| Live smoke перед release | `docker compose exec api poetry run python scripts/live_smoke.py --list` |

Бекап і retention просяться в cron на хості (обидві команди ідемпотентні й
безпечні для працюючого стеку — бекап іде через online-backup API SQLite).

---

## 3. Типові інциденти

### 3.1 Провайдер повертає `auth_required` / `not_configured`

**Симптом:** картка клієнта показує `auth_required`, у подіях — `preflight`
з відповідним кодом.

`not_configured` ≠ помилка: клієнт просто не заведений у цьому сервісі
(PLAN §9.2). Заводиться через UI (кнопка інтеграції на картці) або
`PUT /api/v1/clients/{id}/integrations/{provider}`.

`auth_required` — протухла browser-сесія AISeoTools. Лікується разовим
логіном у профіль (профілі змонтовані з `${HOME}/.local/share/seo-audit`),
далі — `Retry failed only` на картці. Ключі провайдерів застосунок **не
зберігає**: вони живуть у `.env` AISeoTools і підключаються лише в рантаймі.

### 3.2 Job «завис» у `running`

Спершу подивись, чи він справді живий:

```bash
docker compose exec api poetry run python - <<'PY'
from app.db.session import worker_session_scope
from app.db.repositories.cells import CellRepository
with worker_session_scope() as s:
    for cell in CellRepository(s).expired_cells():
        print(cell.id, cell.provider, cell.lease_owner, cell.lease_expires_at)
PY
```

Порожній вивід = клітинка справді працює (у неї свіжий lease і heartbeat),
просто збір довгий. Непорожній = процес помер, не відпустивши lease. Лікує
рестарт worker-а: `run_startup_recovery` на старті переведе такі клітинки в
`failed_interrupted` і поставить у чергу знову (PLAN §9.2).

```bash
docker compose restart worker
```

Перезапуск безпечний за побудовою: snapshot пишеться однією короткою
транзакцією в самому кінці збору, тож перерваний збір нічого часткового не
комітить.

### 3.3 Скасування не завершується

Клітинка в `cancel_requested`, яка на момент скасування ще стояла в черзі
(`queued`), лишиться в цьому стані до рестарту worker-а — доріжка забирає
тільки `queued`. Обхід: `docker compose restart worker` (recovery доведе її
до `cancelled`). Це відома вада, зафіксована тестом
`tests/integration/test_cancel_retry_partial.py::test_cancel_finalizes_a_queued_cell`
(`xfail`) і винесена у follow-up до task-05.

### 3.4 `database is locked`

Ознака того, що writer не один. Перевір `docker compose ps`: `worker` має
бути рівно один. Якщо запускали разовий скрипт, який пише в БД, паралельно
з worker-ом — дочекайся його завершення. `busy_timeout` = 30 с, тож короткі
перетини система переживає сама.

### 3.5 Фронт показує «Dashboard could not be loaded»

Перевір `curl localhost:6524/api/v1/dashboard`. Якщо API відповідає, а фронт
ні — майже завжди CSP: сторінка не має інлайнових скриптів і зовнішніх
хостів (заголовки в [`app/core/security.py`](../app/core/security.py)).
Дивись консоль браузера.

---

## 4. Логи й спостережуваність

- `docker compose logs -f api|worker` — структуровані рядки виду
  `cell_failed_unexpectedly cell_id=… provider=…`.
- Прогрес кожного збору персистентний: `job_events` (SSE-стрічка) + поля
  прогресу на клітинці. Історія переживає рестарт і reconnect.
- Сирі метрики клієнтів у логи **не** пишуться (PLAN §13); подія проходить
  через редакцію (`app/jobs/events.py`) перед записом.
- Помилка, віддана в UI, завжди узагальнена (`collection failed; see server
  logs for detail`) — деталі шукай у логах контейнера за `cell_id`.

---

## 5. Тести й гейти

```bash
docker compose exec api poetry run pytest              # усе, крім live
docker compose exec api poetry run pytest tests/integration -q
docker compose exec api poetry run pytest tests/contract -q     # сигнатури AISeoTools
docker compose exec api poetry run ruff check . && docker compose exec api poetry run mypy app
docker compose exec api bash scripts/aiseotools_regression.sh   # стара suite AISeoTools
```

`live`-тести пропускаються завжди, доки не переданий `--live`. Це навмисно:
вони витрачають API units і чіпають справжні browser-сесії.

## 6. Браузерний e2e фронту

Єдиний крок поза контейнером застосунку — потрібен Node і Chromium на хості:

```bash
docker compose up -d                        # api віддає фронт
bash tests/e2e/run_frontend_smoke.sh
```

Скрипт сам поставить `playwright-core` у `/tmp/yovth-e2e` (в застосунок
JS-залежність не додається), прожене сценарій і покаже, які базові знімки
змінились. Порівняння знімків байт-у-байт **не** є автоматичним гейтом:
растеризація шрифтів різниться між машинами. Дифф дивиться людина в рев'ю.

---

## 7. Перед релізом

1. `docker compose up -d --build` з нуля за README.
2. `pytest` (unit + integration + contract) — зелено.
3. `scripts/aiseotools_regression.sh` — стара suite AISeoTools зелена.
4. `tests/e2e/run_frontend_smoke.sh` — зелено, знімки переглянуті.
5. `scripts/live_smoke.py` по одному контрольному клієнту на провайдера,
   totals **звірені руками** з UI провайдера.
6. Свіжий бекап знято й перевірено відновленням.
7. Пройдено чеклист [dod-mvp.md](dod-mvp.md).
