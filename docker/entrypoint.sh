#!/usr/bin/env bash
# Runs inside the runtime image, where PATH already points at the venv
# built in the Dockerfile's builder stage — no `poetry run` needed here.
set -euo pipefail

# Local AISeoTools checkout (task-08+ providers import `seo_audit`), bind-
# mounted at /seo/AISeoTools by compose.yaml when the sibling repo exists on
# the host. Absent on CI/deploy (docker/Dockerfile builds and runs fine
# without it — these providers are exercised only via live smoke, PLAN §15).
# Installed here at container start, not baked into the image, so the image
# never ships AISeoTools' `.env`/session/profile state.
if [ -d "/seo/AISeoTools/seo_audit" ] && ! python -c "import seo_audit" >/dev/null 2>&1; then
  echo "Installing local AISeoTools checkout (--with local)..."
  poetry install --no-root --with local
fi

echo "Checking configuration..."
python -c "from app.core.config import get_settings; get_settings()"

if [ -f "alembic.ini" ]; then
  echo "Running database migrations..."
  alembic upgrade head
else
  echo "alembic.ini not found yet (task-03 pending) — skipping migrations."
fi

case "${1:-api}" in
  api)
    exec uvicorn app.main:app --host 0.0.0.0 --port "${API_PORT:-6524}"
    ;;
  worker)
    if python -c "import app.jobs.worker" >/dev/null 2>&1; then
      exec python -m app.jobs.worker
    fi
    echo "app.jobs.worker not implemented yet (task-05 pending) — running no-op placeholder."
    exec python docker/worker_placeholder.py
    ;;
  *)
    exec "$@"
    ;;
esac
