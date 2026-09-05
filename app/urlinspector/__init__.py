"""URL Inspector (task-61): paste any client page URL in the header search
and see everything the system knows about it — GSC/GA4 series scoped to
that one URL, its Web Archive timeline, Site Monitor state/changes, meta +
copy QA, and an on-demand live micro technical audit.

This package never writes to another task's tables. It reads
`app.db.models` directly (same precedent as `app/api/analytics.py`,
`app/db/repositories/tech_audits.py`) and reuses other tasks' PUBLIC
functions (`app.webarchive.urls.normalize_url`, `app.monitor.fetcher.fetch`,
`app.monitor.page.extract_signals`, `app.tools.redirects.trace.trace_redirects`,
`app.contentqa.engine.run_checks`) rather than duplicating their logic —
CLAUDE.md "code against declared contracts, don't redo another module's job".

See `doc/dev/new/task-61-url-inspector.md` for the plan and the measured
findings (§0) that shaped this design — trailing-slash matching (§0.2),
last-snapshot-wins per period (§0.3), and period-length grouping (§0.4) are
not incidental choices, they are the difference between a working and a
fake module on this portfolio's real data.
"""

from __future__ import annotations
