# Single image shared by the `api` and `worker` compose services; only the
# command (see docker/entrypoint.sh) differs between them. All Python work
# (app, worker, tests, alembic, lint, type-check, CLI) runs inside this
# image — never on the host (see CLAUDE.md "Середовище виконання").
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    POETRY_NO_INTERACTION=1 \
    POETRY_VIRTUALENVS_CREATE=false \
    PYTHONPATH=/app

WORKDIR /app

# procps: `pgrep` for the worker healthcheck. Dependencies install into
# system site-packages (POETRY_VIRTUALENVS_CREATE=false) — never under
# /app — so compose's dev bind mount of the repo over /app can never
# shadow them.
# TODO(task-08/09/10/11): swap in a Playwright/Chromium-capable base once
# AISeoTools browser-driven providers land — see PLAN.md risks in task-01.
RUN apt-get update && apt-get install --no-install-recommends -y \
      ca-certificates \
      curl \
      fonts-liberation \
      fonts-noto-color-emoji \
      libasound2 \
      libatk-bridge2.0-0 \
      libatk1.0-0 \
      libatspi2.0-0 \
      libcairo2 \
      libcups2 \
      libdbus-1-3 \
      libexpat1 \
      libgbm1 \
      libglib2.0-0 \
      libnspr4 \
      libnss3 \
      libpango-1.0-0 \
      libx11-6 \
      libxcb1 \
      libxcomposite1 \
      libxdamage1 \
      libxext6 \
      libxfixes3 \
      libxkbcommon0 \
      libxrandr2 \
      procps \
      xauth \
      xdg-utils \
      xvfb \
    && rm -rf /var/lib/apt/lists/* \
    && pip install "poetry==2.4.1"

COPY pyproject.toml poetry.lock ./

# AISeoTools itself is never baked in — it carries `.env`, browser profiles and
# session state, and compose bind-mounts the real checkout at /seo/AISeoTools.
# Its 65 locked DEPENDENCIES, though, are ordinary PyPI packages with nothing
# secret about them, and installing them at *container start* (the old
# entrypoint step) cost ~8s on every `compose up` that recreated the container.
#
# `seo-audit` is a `develop = true` path dependency resolving to /seo/AISeoTools,
# so `--with local` only needs *something* at that path to install the whole
# dependency set from the lock. This stub is that something; at runtime the bind
# mount covers it and the editable install resolves to the real code.
#
# If the mount is ever missing (CI/deploy), the stub raises on import with a
# clear message instead of silently importing an empty package.
RUN mkdir -p /seo/AISeoTools/seo_audit \
 && printf '%s\n' \
      '[project]' \
      'name = "seo-audit"' \
      'version = "0.1.0"' \
      'description = "build-time stub, replaced by the bind-mounted checkout"' \
      'requires-python = ">=3.11"' \
      '' \
      '[tool.poetry]' \
      'packages = [{ include = "seo_audit" }]' \
      '' \
      '[build-system]' \
      'requires = ["poetry-core>=2.0.0"]' \
      'build-backend = "poetry.core.masonry.api"' \
    > /seo/AISeoTools/pyproject.toml \
 && printf '%s\n' \
      'raise ImportError(' \
      '    "seo_audit is the build-time stub: the AISeoTools checkout is not mounted at "' \
      '    "/seo/AISeoTools. Start via compose (which bind-mounts it) or set AISEOTOOLS_PATH."' \
      ')' \
    > /seo/AISeoTools/seo_audit/__init__.py

RUN poetry install --no-root --with local

COPY app ./app
COPY frontend ./frontend
COPY docker ./docker

RUN chmod +x docker/entrypoint.sh

# Mount points for the three named volumes, pre-created 0777 so an ARBITRARY uid can write them.
#
# compose runs api/worker as the operator's own uid, never root (see `user:` there, and the
# incidents it prevents). A named volume Docker has to create from scratch is seeded from the
# image's content AT THAT PATH — including its mode — so if these directories do not exist here,
# Docker makes them root:root 0755 and a non-root container cannot write its own database.
# VERIFIED, not assumed: the seeding still happens (and the mode is preserved) even though
# compose bind-mounts the repo over `/app` first.
#
# 0777 rather than a baked-in owner because the uid is not knowable at build time — it comes from
# whoever runs compose. The directories only ever hold this container's own state (SQLite DB,
# artifacts), never anything of the operator's, and they are inside a volume rather than on a
# shared path, so the wide mode is not a boundary anyone relies on.
RUN mkdir -p /app/app_data/artifacts /app/app_data/browser_profiles \
 && chmod -R 0777 /app/app_data

# `SEOTOOLS_ROOT` (compose: /seo/runtime) must exist HERE, writable, for the same reason —
# but with a sharper failure mode. Docker creates the missing PARENT of a bind-mount path
# (`/seo/runtime/state`) as root:root 0755 at container start, and AISeoTools' browser layer
# does `LOG_DIR.mkdir()` at *module import* time. A non-root container therefore could not
# import `seo_audit.browser.profile` at all — every provider integration dead, while the
# healthcheck (which imports none of it) still reported healthy.
#
# Pre-creating the directory means Docker finds it already there and mounts `state` inside it
# instead of inventing a root-owned parent. `logs/` is pre-created too so nothing has to mkdir
# at runtime.
RUN mkdir -p /seo/runtime/logs \
 && chmod -R 0777 /seo/runtime

ENTRYPOINT ["docker/entrypoint.sh"]
CMD ["api"]
