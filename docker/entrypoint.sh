#!/usr/bin/env bash
# Runs inside the runtime image, where PATH already points at the venv
# built in the Dockerfile's builder stage — no `poetry run` needed here.
set -euo pipefail

# NOTE — AISeoTools is NOT installed here any more. Its dependencies are baked
# into the image at build time (see Dockerfile), and its own code arrives via
# the /seo/AISeoTools bind mount, which the build-time editable install already
# points at. Running `poetry install --with local` here re-installed 65 packages
# on every container recreate and was ~8s of the ~16s `compose up --wait` took;
# nothing about it needed to happen at runtime.

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
