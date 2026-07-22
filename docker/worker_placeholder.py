"""No-op stand-in for the worker service until task-05 adds app/jobs/worker.py.

docker/entrypoint.sh tries to import app.jobs.worker first and only falls
back to this script when that module doesn't exist yet — no changes needed
here once task-05 lands.
"""

from __future__ import annotations

import time

print("worker placeholder: app.jobs.worker not implemented yet (task-05) — idling.")
while True:
    time.sleep(3600)
