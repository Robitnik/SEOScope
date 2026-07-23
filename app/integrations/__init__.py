"""Provider adapter registry bootstrap.

Importing this package triggers each provider adapter module's module-level
`register_adapter(...)` call (PLAN §7). Not owned by any single provider task
— each of task-09/10/11 adds its own import line here for its own adapter(s);
these are independent, order-agnostic lines, so concurrent additions don't
collide.
"""

from __future__ import annotations

from app.integrations import (  # noqa: F401
    ahrefs,  # noqa: F401
    gsc,
    reporting_ninja,
    semrush,
    tech_audit,
)
