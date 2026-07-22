"""Enums shared across adapters, worker, metrics engine, API and frontend.

Canon for these names (PLAN.md §6/§7/§9/§10): provider and metric names must
match everywhere — catalog, API, frontend, filters, error messages.
"""

from __future__ import annotations

from enum import StrEnum


class Provider(StrEnum):
    ahrefs = "ahrefs"
    semrush = "semrush"
    gsc = "gsc"
    reporting_ninja = "reporting_ninja"


class PeriodKind(StrEnum):
    """Період порівняння (PLAN §3.1, §21.1).

    `week`/`month` — базові періоди дашборду. `quarter`/`year`/`custom`
    додані task-14 і рахуються **агрегацією вже збереженої історії** (нуль
    нового збору), тому доступні лише там, де UI чесно показує покриття —
    на сторінці клієнта (§21.1/§21.6), не на головному дашборді.
    """

    week = "week"
    month = "month"
    quarter = "quarter"
    year = "year"
    custom = "custom"


class ComparisonMode(StrEnum):
    closed = "closed"
    to_date = "to_date"


class PeriodRole(StrEnum):
    current = "current"
    previous = "previous"
    snapshot = "snapshot"


class Quality(StrEnum):
    complete = "complete"
    partial = "partial"
    truncated = "truncated"
    stale = "stale"
    unavailable = "unavailable"


class BetterDirection(StrEnum):
    up = "up"
    down = "down"
    neutral = "neutral"


class SourceMode(StrEnum):
    auto = "auto"
    api = "api"
    browser = "browser"
    pdf = "pdf"


class JobStatus(StrEnum):
    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    partial = "partial"
    failed = "failed"
    cancel_requested = "cancel_requested"
    cancelled = "cancelled"


class StageStatus(StrEnum):
    pending = "pending"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    skipped = "skipped"


class CellStatus(StrEnum):
    """Status of one `(client, provider)` collection cell (PLAN §9.1/§9.2).

    `not_configured` is terminal and distinct from `failed`: the integration
    was never set up for this client, nothing was attempted, and it must
    never be rendered as a failure or as zero. It is also distinct from
    `unavailable` (attempted, provider has no data) and `auth_required`
    (configured, but the session/credentials expired).

    `awaiting_auth` (task-21) is **not terminal**: a live session check (or
    an adapter reporting `error_code="auth_required"`) found the session for
    this provider dead. The cell is parked here — releasing its browser
    profile lock and stopping its lease clock, since neither `claim_next_cell`
    nor startup recovery watch anything but `running` — while its lane waits
    for the operator to log back in, then auto-resumes by requeuing this same
    cell. It only ever becomes terminal (`failed`, `error_code="auth_required"`)
    if the wait times out.
    """

    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    partial = "partial"
    failed = "failed"
    failed_interrupted = "failed_interrupted"
    not_configured = "not_configured"
    awaiting_auth = "awaiting_auth"
    skipped = "skipped"
    cancel_requested = "cancel_requested"
    cancelled = "cancelled"


class MetricUnit(StrEnum):
    count = "count"  # type: ignore[assignment]  # mypy: StrEnum member name shadows str.count
    percent = "percent"
    score = "score"
    position = "position"
    currency = "currency"
    ratio = "ratio"


# Stable stage order for a single collection cell (PLAN §9). `stage` on
# ProgressEvent/JobStepDTO is a free-form string, not an enum, because
# provider adapters may add their own sub-stages; this tuple is the
# canonical top-level order the UI and worker key off of.
STAGE_ORDER: tuple[str, ...] = (
    "queued",
    "preflight",
    "authenticate",
    "fetch_current",
    "fetch_previous",
    "normalize",
    "validate",
    "persist",
    "complete",
)
