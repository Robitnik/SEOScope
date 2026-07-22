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
- Підключення `AISeoTools` як additional build context / editable dep (локально `../../seo/AISeoTools`); для CI/deploy — pin на commit/tag. Абсолютних host-шляхів у committed compose **немає**; лише в local override.
- `.env.example` (лише **назви** змінних, без значень), `.gitignore`, `README.md` (як підняти).
- CI (`.github/workflows/ci.yml` або еквівалент): lint (ruff), type-check (mypy), `pytest`.
- `frontend/index.html` shell-заглушка + структура папок **не чіпати** — це task-12; тут лише переконатися, що static mount працює на будь-який контент у `frontend/`.

**НЕ входить:** ORM/міграції (task-03), контракти (task-02), будь-яка бізнес-логіка, реальні роутери (task-06), самі фронт-модулі (task-12).

## Owned paths

```
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
- [ ] `poetry install` та `poetry run uvicorn app.main:app` стартують без помилок.
- [ ] `docker compose up --build` піднімає `api` (+ `worker` як no-op заглушка), healthchecks зелені.
- [ ] Статичний `frontend/` віддається з того ж origin.
- [ ] `.env` у `.gitignore`; `.env.example` містить лише імена змінних.
- [ ] Абсолютних host-шляхів у committed `compose.yaml` немає.
- [ ] CI (lint+type+pytest) зелений.
- [ ] Інші таски можуть додати роутер/модуль **без редагування** `app/main.py`.

## Ризики
- Playwright/Chromium у Docker (потрібен `shm_size`, deps) — узгодь base image із провайдер-тасками (09–11), щоб worker міг ганяти браузер.
- Механізм реєстрації роутерів має бути реально безколізійним (інакше task-06 муситиме правити `main.py`).
