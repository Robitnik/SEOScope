# task-01 — Foundation & Infra (скелет, config, Docker, CI)

- **Пакет:** каркас застосунку, конфігурація, контейнеризація, CI, health/readiness
- **Хвиля:** A (стартує одразу, розблоковує всіх)
- **Depends on:** —
- **Blocks:** task-03, task-06 (потрібен пакетний скелет і config), решта — опосередковано
- **PLAN.md:** §5 (архітектура), §5.2 (структура репозиторію), §13 (безпека), §14 (Docker/Compose), Етап 1

## Мета

Підняти порожній, але **брендований і працездатний** каркас: `docker compose up --build` стартує FastAPI, що віддає статичний фронт-shell, healthchecks зелені, CI ганяє lint/type/pytest. Жодної бізнес-логіки — лише кістяк, у який інші таски кладуть свої файли.

## Обсяг

**Входить:**
- Poetry-проєкт (`pyproject.toml`, `poetry.lock`), Python `>=3.11`.
- Пакетний скелет `app/` з усіма підпакетами як порожні `__init__.py` + `.gitkeep`, щоб інші таски одразу мали куди класти файли (`app/api/`, `app/core/`, `app/db/`, `app/integrations/`, `app/metrics/`, `app/jobs/`, `app/importers/`, `app/schemas/`).
- `app/main.py`: створення FastAPI-app, підключення роутерів через include (роутери з'являться пізніше — використати lazy/registry-паттерн або порожній список, який дозаповнюють інші таски **без правок main**), монтування статичного `frontend/`.
- `app/core/config.py`: pydantic-settings, читання `.env`, усі шляхи/таймаути/ретеншени як налаштування з дефолтами.
- `app/core/logging.py`: структуровані логи + redaction-хук (реальні redaction-патерни додає той, хто пише секрето-чутливий код; тут — інфраструктура фільтра).
- `app/core/security.py`: заготовки security headers, CORS (restrictive, same-origin), path-traversal guard helper для artifacts.
- `app/api/health.py`: `GET /healthz` (процес живий), `GET /readyz` (заглушка, яку task-03 доповнить перевіркою БД+міграцій).
- `Dockerfile` (single image), `compose.yaml` (services `api`, `worker`; named volumes `app_data`, `artifacts`, `browser_profiles`; `shm_size` для Chromium; healthchecks; restart policy), `docker/` (entrypoint, init-скрипт `alembic upgrade head`).
- **Dev-workflow «тільки Docker»** (CLAUDE.md «Середовище виконання»): source **bind-mounted** у контейнер (правки з хоста живі); poetry-залежності встановлені в образі на build-етапі у venv **поза** змонтованою текою (або `POETRY_VIRTUALENVS_CREATE=false`), щоб bind mount не перекрив venv; `api` — довготривалий сервіс, у який можна `docker compose exec api bash`; `api` і `worker` — один образ, різні команди. Уся Python-робота (app/worker/tests/alembic/lint/CLI) виконується **лише** в контейнері; хостовий Python не потрібен ніде.
- Підключення `AISeoTools` як additional build context / editable dep (локально `../../seo/AISeoTools`); для CI/deploy — pin на commit/tag. Абсолютних host-шляхів у committed compose **немає**; лише в local override.
- `.env.example` (лише **назви** змінних, без значень), `.gitignore`, `README.md` (як підняти).
- CI (`.github/workflows/ci.yml` або еквівалент): lint (ruff), type-check (mypy), `pytest` — усе виконується **всередині Docker-образу** (build image → кроки в контейнері), без хостового Python.
- `frontend/index.html` shell-заглушка + структура папок **не чіпати** — це task-12; тут лише переконатися, що static mount працює на будь-який контент у `frontend/`.

**НЕ входить:** ORM/міграції (task-03), контракти (task-02), будь-яка бізнес-логіка, реальні роутери (task-06), самі фронт-модулі (task-12).

## Owned paths

```text
pyproject.toml, poetry.lock, alembic.ini(створює task-03 — НЕ чіпати)
app/main.py
app/__init__.py + порожні __init__.py по підпакетах
app/core/{config,logging,security}.py
app/api/__init__.py, app/api/health.py
Dockerfile, compose.yaml, compose.override.example.yaml, docker/**
.env.example, .gitignore, README.md
.github/workflows/** (CI)
scripts/bootstrap*  (dev-хелпери)
```

## Consumes (contracts)

Немає. Це база.

## Provides (contracts) — інші залежать від цього

- Робочий пакет `app` і спосіб реєстрації роутерів у `app/main.py` **без його редагування** (напр. функція `register_router(router)` або автозбір з `app/api/`).
- `Settings` із `app/core/config.py` (імена полів — задокументувати в цьому плані при завершенні).
- `get_logger()` та redaction-фільтр.
- Патерн health/readiness, куди task-03 додасть DB-check.

## Кроки реалізації

1. `poetry init`, залежності: `fastapi`, `uvicorn[standard]`, `pydantic-settings`, `sqlalchemy>=2`, `alembic`, `aiosqlite`, dev: `ruff`, `mypy`, `pytest`, `pytest-asyncio`, `httpx`. (SQLAlchemy/alembic ставимо тут, конфігурує task-03.)
2. Скелет пакетів + `.gitkeep`.
3. `config.py` — Settings з `env_file=.env`; поля для DB URL, artifacts dir, browser profiles dir, event retention, CORS origins, лог-рівень.
4. `main.py` — app factory, static mount `frontend/`, реєстрація роутерів через розширюваний механізм, health-роутер.
5. Логи + security headers + CORS + path-traversal helper.
6. Dockerfile (Playwright/Chromium base для worker — узгодити з task-11/AISeoTools; можна багатоцільовий image, різні commands), compose з volumes і healthchecks, init-контейнер під міграції.
7. `.env.example`, README (запуск локально й у compose), CI.
8. Дим-тест: `docker compose up --build` → відкривається порожній branded shell, `/healthz` і `/readyz` віддають 200.

## Тести

- `tests/unit/core/` — завантаження Settings з env, path-traversal guard (блокує `../`), security headers присутні.
- `GET /healthz` → 200; `GET /readyz` → 200 (поки БД немає — повертає ready з поміткою, task-03 посилить).
- CI зелений на порожньому дереві.

## Definition of Done / Gate

- [x] Образ будується; `docker compose up -d --build` піднімає стек; `docker compose exec api poetry run uvicorn app.main:app` і `docker compose exec api poetry run pytest` працюють **у контейнері**.
- [x] venv не перекривається source bind-mount'ом; жодна Python-команда (app/worker/tests/alembic/lint/CLI) не потребує хостового Python.
- [x] `docker compose up --build` піднімає `api` (+ `worker` як no-op заглушка), healthchecks зелені.
- [x] Статичний `frontend/` віддається з того ж origin.
- [x] `.env` у `.gitignore`; `.env.example` містить лише імена змінних.
- [x] Абсолютних host-шляхів у committed `compose.yaml` немає.
- [x] CI (lint+type+pytest) зелений.
- [x] Інші таски можуть додати роутер/модуль **без редагування** `app/main.py`.

## Ризики

- Playwright/Chromium у Docker (потрібен `shm_size`, deps) — узгодь base image із провайдер-тасками (09–11), щоб worker міг ганяти браузер.
- Механізм реєстрації роутерів має бути реально безколізійним (інакше task-06 муситиме правити `main.py`).

---

## ✅ Completion report

- **Дата:** 2026-07-22
- **Гілка / комміт:** `main` (робоче дерево, не закомічено — як і решта тасок 02–11)
- **Статус:** done-with-followups

> **Примітка про архівування.** Цей план був помилково видалений з `doc/dev/new/` **без** перенесення в `doc/dev/done/` і без звіту (процесний збій — див. `CLAUDE.md`, крок «Коли таска готова»). Файл відновлено з комміту `c0ea8d3`, до нього повернуто пізніші правки «тільки Docker», а звіт нижче **реконструйовано з фактичного аудиту репозиторію**, а не з нотаток виконавця. Усі твердження нижче перевірені інструментально, а не зі слів.

### Що зроблено

Повний працездатний каркас: FastAPI-застосунок, конфіг/логування/безпека, health/readiness, single-image Docker (`api` + `worker`), CI, і **Docker-only середовище виконання**. Каркас реально прийняв усі наступні таски — на ньому зібрані 02–12 без правок `app/main.py`.

### Як зроблено (ключові рішення)

- **`app/main.py`** — app factory + **авто-дискавері роутерів**: `_discover_routers()` через `pkgutil.iter_modules` імпортує кожен модуль з `app/api/`, а той на імпорті кличе `register_router(router)`; `create_app()` робить `include_router` по зібраному списку. Саме тому таски 06 та інші додавали роутери **не торкаючись** `main.py`. Статика: `app.mount("/", StaticFiles(..., html=True))`.
- **`Dockerfile`** — `python:3.12-slim`, poetry 2.4.1, `POETRY_VIRTUALENVS_CREATE=false`: залежності лягають у system site-packages, **ніколи не під `/app`**, тому dev bind mount `.:/app` фізично не може їх перекрити (головна пастка цього підходу). `procps` — для `pgrep` у healthcheck worker'а.
- **`compose.yaml`** — один образ, дві команди; volumes `app_data`/`artifacts`/`browser_profiles`; `shm_size: 1gb` у worker; healthcheck `curl /healthz` (api) та `pgrep` (worker); `restart: unless-stopped`; порт через `${API_PORT:-6524}`. Абсолютних host-шляхів немає — лише `${AISEOTOOLS_PATH:-../../seo/AISeoTools}`.
- **`docker/entrypoint.sh`** — ставить локальний AISeoTools з bind mount (лише якщо він є), перевіряє конфіг, робить `alembic upgrade head`, далі диспетчеризує `api` / `worker` / **довільну команду** (`*) exec "$@"`) — саме це дає `docker compose run --rm api pytest`.
- **AISeoTools не запікається в образ** — свідомо: інакше в шар потрапив би його `.env` / стан browser-профілів.

### Контрактні зміни

- `Settings` (`app/core/config.py`) — джерело конфігу для всіх тасок.
- Механізм `register_router()` + авто-дискавері — публічний контракт для task-06 і будь-яких нових роутерів.
- `/readyz` пізніше посилено task-03 (перевірка БД через `app/db/health.py`) — за передбаченим тут хуком.
- `pyproject.toml` пізніше доповнювався іншими тасками через погоджений escape hatch (task-07: `openpyxl`, script `yovth-report`).

### Як перевірити (verification)

```bash
docker compose build api                      # OK (exit 0)
docker compose up -d api                      # healthcheck → healthy
curl -s localhost:6524/healthz                # {"status":"ok"}
curl -s localhost:6524/readyz                 # {"status":"ok","checks":{"database":"ok"}}
curl -s -o /dev/null -w "%{http_code}" localhost:6524/   # 200 (frontend)
docker compose run --rm api pytest -q         # 306 passed
docker compose run --rm --no-deps --entrypoint sh api -c "ruff check app tests && mypy app"
```

Фактичні результати аудиту: build exit 0; `api` healthy; `/healthz` і `/readyz` (з `database: ok`) — 200; фронт — 200; **306 passed**; ruff — `All checks passed!`; mypy strict — `no issues found in 67 source files`.

### Тести

`tests/unit/core/test_config.py`, `test_security.py`; `tests/unit/api/test_health.py`. CI-workflow: `.github/workflows/ci.yml`.

### Follow-ups / борг

1. **Playwright/Chromium base image ще не зроблено** — у `Dockerfile` стоїть явний `TODO(task-08/09/10/11)`. Наслідок: browser-driven збір (Semrush/GSC live) у контейнері поки не побіжить; провайдер-код і юніт-тести від цього не залежать (мокаються). Ризик із секції «Ризики» вище лишається **відкритим** — передати в task-13.
2. **`scripts/bootstrap*` не створено** (тека `scripts/` є, але dev-хелпера немає) — потреби не виникло, бо весь workflow звівся до `docker compose`. Свідоме спрощення, не блокер.
3. **CI/deploy pin AISeoTools на commit/tag** ще не зроблено — зараз локальний path-dep через optional-групу `local`. task-08 вже дав придатний тег `dashboard-core-v1` (`0ec56fc`); лишилось перевести `pyproject` на git-dependency. Передати в task-13.
4. **Робоче дерево не закомічено** — спільний борг тасок 02–11, не специфічний для цієї.
