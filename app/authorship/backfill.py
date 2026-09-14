"""Backfill authorship for rows created before task-93 (plan §2 D7, §1.4).

Runs entirely over already-loaded ORM rows in plain Python — once
SQLAlchemy deserializes `AuditEvent.details_json`, matching is just dict/
datetime comparisons, portable across Postgres (prod) and SQLite (tests)
alike. This reimplements, in code, exactly the `psql` queries task-93's
plan §1.4 used to prove the method live against production before writing
a single line of it: an EXACT KEY from the audited request body (when the
action has one) narrowed by a TIME WINDOW around when the row was created,
resolved only when every surviving candidate shares one actor.

Never overwrites a row that already has `created_by_kind` set (the caller
only ever selects rows where it `IS NULL`), and never guesses when more
than one distinct actor is a plausible match — those rows stay `unknown`,
same as a row created before the audit log even existed.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import Actor
from app.authorship.model import Attribution
from app.db.models import AuditEvent, ContentQaPackage, ReportBatch, ReportRun

# A row is created INSIDE the request that produces its audit line, and
# `AuditEvent.ts` is stamped AFTER the response is sent — so the row's
# `created_at` always falls a little before `ts`. `duration_ms` accounts
# for slow requests; the extra 2s is slack for clock/flush jitter, matched
# to what plan §1.4's live queries measured as sufficient with zero
# multi-actor false positives on the full production history.
_WINDOW_SLACK = timedelta(seconds=2)

Reason = Literal["matched", "ambiguous", "no_evidence"]

# Maps a report's `table` label to the model `apply_backfill` writes to —
# `report_run_standalone`/`report_run_batch_child` share one model, the
# split is only for a clearer CLI summary (plan §1.4's own table separates
# them the same way).
_MODEL_BY_TABLE: dict[str, type] = {
    "report_run_standalone": ReportRun,
    "report_run_batch_child": ReportRun,
    "report_batch": ReportBatch,
    "content_qa_package": ContentQaPackage,
}


@dataclass(frozen=True)
class BackfillDecision:
    resource_id: str
    reason: Reason
    attribution: Attribution | None = None


@dataclass
class BackfillReport:
    table: str
    decisions: list[BackfillDecision] = field(default_factory=list)

    @property
    def matched(self) -> int:
        return sum(1 for d in self.decisions if d.reason == "matched")

    @property
    def ambiguous(self) -> int:
        return sum(1 for d in self.decisions if d.reason == "ambiguous")

    @property
    def no_evidence(self) -> int:
        return sum(1 for d in self.decisions if d.reason == "no_evidence")

    @property
    def total(self) -> int:
        return len(self.decisions)


def _within_window(row_created_at: datetime, event: AuditEvent) -> bool:
    slack = timedelta(seconds=(event.duration_ms or 0) / 1000.0) + _WINDOW_SLACK
    return bool((event.ts - slack) <= row_created_at <= (event.ts + _WINDOW_SLACK))


def _attribution_from_event(event: AuditEvent) -> Attribution:
    # `AuditEvent.actor_id`/`actor_kind` already ARE the resolved
    # `(kind, id)` pair `Attribution.from_actor` produces at write time
    # (`app/audit/middleware.py`) — reusing it here, via a minimal `Actor`,
    # keeps exactly one place that knows "which ids are real users" (plan
    # §2 D2), instead of a second copy of that judgment call.
    return Attribution.from_actor(Actor(id=event.actor_id or "system", kind=event.actor_kind))


def _decide(
    row_created_at: datetime, candidates: list[AuditEvent]
) -> tuple[Reason, Attribution | None]:
    windowed = [e for e in candidates if _within_window(row_created_at, e)]
    if not windowed:
        return "no_evidence", None
    actor_keys = {(e.actor_kind, e.actor_id) for e in windowed}
    if len(actor_keys) != 1:
        return "ambiguous", None
    return "matched", _attribution_from_event(windowed[0])


def _request_field(event: AuditEvent, field_name: str) -> str | None:
    request = (event.details_json or {}).get("request")
    if not isinstance(request, dict):
        return None
    value = request.get(field_name)
    return value if isinstance(value, str) else None


def _group_by_field(events: list[AuditEvent], field_name: str) -> dict[str, list[AuditEvent]]:
    grouped: dict[str, list[AuditEvent]] = defaultdict(list)
    for event in events:
        value = _request_field(event, field_name)
        if value:
            grouped[value].append(event)
    return grouped


def _success_events(session: Session, actions: tuple[str, ...]) -> list[AuditEvent]:
    stmt = select(AuditEvent).where(
        AuditEvent.action.in_(actions), AuditEvent.outcome == "success"
    )
    return list(session.scalars(stmt))


def backfill_content_qa_packages(session: Session) -> BackfillReport:
    """Exact key: `details.request.file_id` == `source_file_id`."""
    report = BackfillReport(table="content_qa_package")
    packages = list(
        session.scalars(
            select(ContentQaPackage).where(ContentQaPackage.created_by_kind.is_(None))
        )
    )
    if not packages:
        return report
    events = _success_events(session, ("tool.strategy_package.run",))
    by_file_id = _group_by_field(events, "file_id")
    for package in packages:
        candidates = by_file_id.get(package.source_file_id or "", [])
        reason, attribution = _decide(package.created_at, candidates)
        report.decisions.append(BackfillDecision(package.id, reason, attribution))
    return report


def backfill_standalone_report_runs(session: Session) -> BackfillReport:
    """Exact key: `details.request.client_id` == `client_id`. Standalone
    only (`batch_id IS NULL`) — a batch child has no `client_id` in its
    OWN creating request (the batch's `client_ids` is a list; a retry's
    `client_id` is a path parameter, never captured in `details.request`,
    task-93 plan §1.4)."""
    report = BackfillReport(table="report_run_standalone")
    runs = list(
        session.scalars(
            select(ReportRun).where(
                ReportRun.created_by_kind.is_(None), ReportRun.batch_id.is_(None)
            )
        )
    )
    if not runs:
        return report
    by_client_id = _group_by_field(
        _success_events(session, ("report.run", "report.rebuild")), "client_id"
    )
    for run in runs:
        candidates = by_client_id.get(run.client_id, [])
        reason, attribution = _decide(run.created_at, candidates)
        report.decisions.append(BackfillDecision(run.id, reason, attribution))
    return report


def backfill_batch_child_report_runs(session: Session) -> BackfillReport:
    """No exact key available (see above) — time window alone, against
    every batch-shaped creating action."""
    report = BackfillReport(table="report_run_batch_child")
    runs = list(
        session.scalars(
            select(ReportRun).where(
                ReportRun.created_by_kind.is_(None), ReportRun.batch_id.is_not(None)
            )
        )
    )
    if not runs:
        return report
    events = _success_events(
        session, ("report.batch_start", "report.batch_retry", "report.rebuild")
    )
    for run in runs:
        reason, attribution = _decide(run.created_at, events)
        report.decisions.append(BackfillDecision(run.id, reason, attribution))
    return report


def backfill_report_batches(session: Session) -> BackfillReport:
    """No exact key (a batch's own creating body has no single-row id to
    match against) — time window against `report.batch_start` alone."""
    report = BackfillReport(table="report_batch")
    batches = list(
        session.scalars(select(ReportBatch).where(ReportBatch.created_by_kind.is_(None)))
    )
    if not batches:
        return report
    events = _success_events(session, ("report.batch_start",))
    for batch in batches:
        reason, attribution = _decide(batch.created_at, events)
        report.decisions.append(BackfillDecision(batch.id, reason, attribution))
    return report


def run_backfill_scan(session: Session) -> list[BackfillReport]:
    """Dry-run: resolves every candidate, writes nothing."""
    return [
        backfill_content_qa_packages(session),
        backfill_standalone_report_runs(session),
        backfill_batch_child_report_runs(session),
        backfill_report_batches(session),
    ]


def apply_backfill(session: Session, reports: list[BackfillReport]) -> int:
    """Writes every `matched` decision. Re-checks `created_by_kind IS NULL`
    per row at write time (not just at scan time) — if something else
    attributed the row in between (a live request, a previous partial
    apply), this never clobbers it. Returns how many rows were actually
    written. Caller commits.
    """
    written = 0
    for report in reports:
        model = _MODEL_BY_TABLE[report.table]
        for decision in report.decisions:
            if decision.reason != "matched" or decision.attribution is None:
                continue
            row = session.get(model, decision.resource_id)
            if row is None or row.created_by_kind is not None:
                continue
            row.created_by_kind = decision.attribution.kind
            row.created_by_user_id = decision.attribution.user_id
            written += 1
    session.flush()
    return written
