# The Yovth Clients Report

Внутрішній dashboard агенції для SEO/marketing-метрик (Ahrefs, Semrush, GSC, Reporting Ninja/GA4).

Архітектура, модель даних і повний план — у [PLAN.md](PLAN.md). Декомпозиція на таски — у [doc/dev/new/INDEX.md](doc/dev/new/INDEX.md).

## Середовище виконання — тільки Docker

Увесь Python (застосунок, worker, тести, міграції, лінт, type-check, CLI) виконується **лише всередині Docker-контейнера**, ніколи на хості — див. `CLAUDE.md` §«Середовище виконання». Хост потрібен тільки для редагування файлів і git; код bind-mounted у контейнер, тож правки з хоста живі без ребілду.

```bash
cp .env.example .env                     # заповни за потреби; дефолти вже робочі
docker compose up -d --build             # підняти стек (api + worker)
docker compose exec api bash             # інтерактивна оболонка в контейнері
docker compose exec api poetry run pytest
docker compose exec api poetry run pytest tests/unit/core -q
docker compose exec api poetry run alembic upgrade head    # міграції (task-03)
docker compose exec api poetry run ruff check .            # lint
docker compose exec api poetry run mypy app                # types
docker compose exec worker poetry run python -m app.jobs.worker   # worker (task-05)
docker compose run --rm api poetry run pytest              # ефемерно, коли стек не піднятий
docker compose logs -f api                                 # логи
docker compose down                                        # зупинити
```

`api` (порт `6524`, налаштовний через `API_PORT`) і `worker` — один образ, різні команди; для разових скриптів/CLI/тестів бери `api`. Якщо змінив залежності (`pyproject.toml`) — перебудуй образ: `docker compose build` / `up --build`.

`api`/`worker` працюють у `network_mode: host` (task-20, PLAN §8.3/§13/§17): це єдиний спосіб дістатися CDP теплого Chrome на хості для browser-сесій провайдерів (Semrush/GSC), чий CDP-порт слухає лише `127.0.0.1`. Тому Docker-`ports:`-маппінг не діє — `API_PORT` напряму задає, який порт хоста слухає uvicorn; конфлікт порту на хості — відповідальність оператора, не compose. Лише Linux.

- `http://localhost:6524/healthz` — процес живий.
- `http://localhost:6524/readyz` — готовність (поки заглушка, task-03 додасть перевірку БД).
- `http://localhost:6524/` — статичний фронт із `frontend/`.

Іменовані volumes: `app_data`, `artifacts`, `browser_profiles`. Для локальних override (абсолютні шляхи) скопіюй [compose.override.example.yaml](compose.override.example.yaml) у `compose.override.yaml` (у `.gitignore`, не комітиться).

### AISeoTools

Інтеграції спираються на зовнішній пакет [`AISeoTools`](../../seo/AISeoTools). Локально він підключається як additional build context (`../../seo/AISeoTools` відносно кореня репо); для CI/деплою — pin на конкретний commit/tag (див. `PLAN.md §8`, `PLAN.md §14`). Змінюється це в `AISeoTools` — лише **адитивно**.

## Конфігурація

Усі налаштування — через `.env` / змінні середовища, див. [`app/core/config.py`](app/core/config.py) і [`.env.example`](.env.example). Дефолти робочі "з коробки" для локальної розробки.
