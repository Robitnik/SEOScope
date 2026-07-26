#!/usr/bin/env bash
# Runs inside the runtime image, where PATH already points at the venv
# built in the Dockerfile's builder stage — no `poetry run` needed here.
set -euo pipefail

# Group-writable by default for everything we create. We run as the operator's own uid (compose
# `user:`), so this is belt-and-braces rather than the load-bearing fix — but AISeoTools' shared
# `state/locks/` files live on a mount the HOST user also has to open O_RDWR, and a default 022
# would silently strip the write bit off any lock we happen to create first.
umask 0002

# The container is not root any more, so a volume whose contents predate that switch is still
# root-owned and we cannot chown our way out of it. Say so HERE, naming the fix, instead of
# letting alembic fail three lines later with a SQLite "unable to open database file" that reads
# like a corrupt database.
if [ ! -w /app/app_data ]; then
  echo "FATAL: /app/app_data is not writable by $(id -u):$(id -g)." >&2
  echo "  The volume was created while this container still ran as root. Fix it once:" >&2
  echo "    docker compose down" >&2
  echo "    docker run --rm -v theyovthclientsreport_app_data:/v -v theyovthclientsreport_artifacts:/a \\" >&2
  echo "      -v theyovthclientsreport_browser_profiles:/b alpine chown -R $(id -u):$(id -g) /v /a /b" >&2
  exit 1
fi

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
