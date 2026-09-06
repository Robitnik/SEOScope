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
    # Machine-only Technical SEO Audit collector (task-25). NOT a metric
    # provider: it produces categories + per-URL findings (AnalyticsDatasetRow),
    # never MetricValues, has no catalog entry / health weight, and is never
    # part of the routine multi-provider sync — it runs only on an explicit
    # per-client "Run audit" request. See METRIC_PROVIDERS below.
    tech_audit = "tech_audit"
    # Machine-only Client Blogs inventory collector (task-26). Same non-metric
    # shape as tech_audit: produces a deduplicated post list (AnalyticsDatasetRow),
    # never MetricValues, no catalog entry / health weight, runs only on an
    # explicit per-client "Refresh data" request. See METRIC_PROVIDERS below.
    blogs = "blogs"


# The four metric-bearing providers — everything the dashboard KPIs, health %,
# metric catalog and the routine "sync all" expansion iterate over. `tech_audit`
# is deliberately excluded (it carries no metrics); callers that mean "the
# providers a routine sync collects" MUST use this, not `tuple(Provider)`.
METRIC_PROVIDERS: tuple[Provider, ...] = (
    Provider.ahrefs,
    Provider.semrush,
    Provider.gsc,
    Provider.reporting_ninja,
)


class ClientStatus(StrEnum):
    """Комерційний стан співпраці з клієнтом (task-34) — **не** технічний стан збору.

    `paused` = оплата припинена: клієнт лишається видимим на дашборді й
    зберігає всю історію, але випадає з масових операцій («Refresh data» на
    всіх, «Run audits» на всіх, CLI-беkфіл, портфельні агрегати). Явно
    відмічений оператором — збирається як звичайно.

    Не плутати з `Client.enabled`: той керує видимістю рядка взагалі
    (`enabled=False` ховає клієнта з дашборду), тоді як статус саме
    показується — у цьому вся його суть.
    """

    active = "active"
    paused = "paused"


class PeriodKind(StrEnum):
    """Comparison period (PLAN §3.1, §21.1).

    `week`/`month` are the dashboard's base periods. `quarter`/`year`/`custom`
    were added by task-14 and are computed **by aggregating already-stored
    history** (zero new collection), so they are only available where the UI
    honestly shows coverage — on the client page (§21.1/§21.6), not on the
    main dashboard.
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


class UserRole(StrEnum):
    """Роль співробітника в тулі (task-71).

    Три рівні, свідомо без per-client ACL: всі авторизовані бачать усіх
    клієнтів, різниця лише в тому, що вони можуть **робити**.

    - `admin`  — усе, плюс керування користувачами й батч-операції
                 (batch reports, імпорти, cloud): вони або дорогі за
                 зовнішніми квотами, або незворотні.
    - `member` — щоденна робота: збір, інструменти, звіти по одному клієнту.
    - `viewer` — лише читання: будь-який мутуючий метод → 403.
    """

    admin = "admin"
    member = "member"
    viewer = "viewer"


# Порядок за спаданням прав — використовується `role_satisfies()` у
# `app/core/auth/policy.py`; не сортувати за алфавітом.
ROLE_ORDER: tuple[UserRole, ...] = (UserRole.viewer, UserRole.member, UserRole.admin)
