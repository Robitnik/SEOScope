"""Job engine (PLAN §5.1/§9): dispatcher, per-provider lanes, cell runner,
recovery and the fake adapter used by tests. See `app.jobs.worker` for the
process entrypoint.
"""

from __future__ import annotations
