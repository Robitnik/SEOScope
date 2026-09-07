"""CLI entrypoint (task-07): `poetry run yovth-report <command>`.

Registered in `pyproject.toml` as `[tool.poetry.scripts] yovth-report =
"app.cli:main"`.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from app.db.session import get_sync_sessionmaker
from app.importers.ai_seo_tools import render_report, run_import


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="yovth-report")
    subparsers = parser.add_subparsers(dest="command", required=True)

    import_cmd = subparsers.add_parser(
        "import-ai-seo-history",
        help=(
            "Read-only, idempotent backfill of provider_snapshots/metric_values "
            "from AISeoTools history (PLAN §11)"
        ),
    )
    import_cmd.add_argument(
        "--source",
        required=True,
        type=Path,
        help="Path to the AISeoTools checkout root (contains clients.json and out/)",
    )
    import_cmd.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be imported without writing to the database",
    )

    backfill_cmd = subparsers.add_parser(
        "backfill-health-history",
        help=(
            "Rebuild the daily composite-health series from snapshots already "
            "stored, so the dashboard card's trend works without waiting for "
            "new history to accumulate (idempotent)"
        ),
    )
    backfill_cmd.add_argument(
        "--days",
        type=int,
        default=30,
        help="How many days back to reconstruct (default: 30)",
    )

    daily_backfill_cmd = subparsers.add_parser(
        "backfill-daily-history",
        help=(
            "Queue a custom-period sync so day-level GSC/GA4 history exists for "
            "the Portfolio Trends page (`#/trends`, task-28). METERED: this "
            "creates real provider requests through the normal job engine, not "
            "a local recomputation."
        ),
    )
    daily_backfill_cmd.add_argument(
        "--days", type=int, default=90, help="How many days back to collect (default: 90)"
    )
    daily_backfill_cmd.add_argument(
        "--providers",
        default="gsc,reporting_ninja",
        help="Comma-separated providers to collect (default: gsc,reporting_ninja)",
    )
    daily_backfill_cmd.add_argument(
        "--client-ids",
        default=None,
        help="Comma-separated client IDs (default: every enabled, non-paused client)",
    )
    daily_backfill_cmd.add_argument(
        "--include-paused",
        action="store_true",
        help="Also collect clients whose status is paused (default: skip them)",
    )
    daily_backfill_cmd.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the plan (range, clients, estimated requests) without creating a job",
    )

    channels_backfill_cmd = subparsers.add_parser(
        "backfill-ga4-channels",
        help=(
            "One-off backfill of `ga4_daily_channels` (Organic/Direct Conversions, "
            "task-44): one two-dimension GA4 query per client via Reporting Ninja. "
            "METERED but cheap (one request per client, ~3s for a 400-day window). "
            "Idempotent: days already stored for a client are not re-inserted."
        ),
    )
    channels_backfill_cmd.add_argument(
        "--days", type=int, default=400, help="How many days back to collect (default: 400)"
    )
    channels_backfill_cmd.add_argument(
        "--client",
        default=None,
        help="One client ID (default: every client with an enabled Reporting Ninja integration)",
    )
    channels_backfill_cmd.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the plan (range, clients) without making requests or writing",
    )

    monitor_run_cmd = subparsers.add_parser(
        "monitor-run",
        help="Run a Site Monitor check now, without waiting for the schedule (task-35)",
    )
    monitor_run_cmd.add_argument("--client-id", default=None, help="One client to check")
    monitor_run_cmd.add_argument(
        "--all", action="store_true", help="Check every collectable (non-paused) client"
    )
    monitor_run_cmd.add_argument(
        "--kind", choices=["light", "full"], default="light", help="Check depth (default: light)"
    )
    monitor_run_cmd.add_argument(
        "--fetch-mode",
        choices=["auto", "http", "browser"],
        default=None,
        help=(
            "Force a transport for this run (task-50). Default: resolve each "
            "client's own MonitorSettings.fetch_mode, same as the scheduler"
        ),
    )

    monitor_status_cmd = subparsers.add_parser(
        "monitor-status",
        help="Portfolio-wide Site Monitor state; non-zero exit if any client has an open critical",
    )
    monitor_status_cmd.add_argument(
        "--severity",
        choices=["critical", "warning", "info"],
        default=None,
        help="Only list open changes at this severity",
    )

    monitor_prune_cmd = subparsers.add_parser(
        "monitor-prune",
        help=(
            "Remove Site Monitor blobs no version references any more. "
            "History itself is never touched unless --bodies-older-than is given"
        ),
    )
    monitor_prune_cmd.add_argument(
        "--dry-run", action="store_true", help="Report what would be removed without deleting"
    )
    monitor_prune_cmd.add_argument(
        "--bodies-older-than",
        type=int,
        default=None,
        metavar="DAYS",
        help=(
            "ALSO drop stored bodies for versions older than DAYS whose blob backs no "
            "recent version either (default: keep every body forever, task-35 §3.2.1)"
        ),
    )
    monitor_prune_cmd.add_argument(
        "--ignore-cloud",
        action="store_true",
        help=(
            "With --bodies-older-than: drop a body by age alone, even if this install has "
            "never confirmed it reached B2 (task-52). Default is cloud-gated — a body is "
            "only dropped locally once the ledger says the cloud has that exact blob too."
        ),
    )

    monitor_resolve_blocked_cmd = subparsers.add_parser(
        "monitor-resolve-blocked",
        help=(
            "Close open monitor changes opened by a run that hit an antibot wall "
            "(task-50) — a one-time cleanup for damage done before this fix landed"
        ),
    )
    monitor_resolve_blocked_cmd.add_argument("--client-id", required=True)
    monitor_resolve_blocked_cmd.add_argument(
        "--run-id",
        default=None,
        help="Also require this run_id (default: every currently-blocked page's open changes)",
    )
    monitor_resolve_blocked_cmd.add_argument(
        "--dry-run", action="store_true", help="Report what would be resolved without touching it"
    )

    monitor_redirects_cmd = subparsers.add_parser(
        "monitor-redirects",
        help="Redirect monitoring state for one client, optionally checking now (task-51)",
    )
    monitor_redirects_cmd.add_argument("--client-id", required=True)
    monitor_redirects_cmd.add_argument(
        "--check", action="store_true", help="Collect right now instead of just reporting state"
    )

    wp_credentials_check_cmd = subparsers.add_parser(  # noqa: F841 - no extra arguments
        "wp-credentials-check",
        help=(
            "Per-client Application Password status (configured/missing/invalid) — "
            "never prints a value (task-51 §3.3)"
        ),
    )

    audit_bindings_cmd = subparsers.add_parser(
        "audit-ninja-bindings",
        help=(
            "Check every client's saved Reporting Ninja report name against "
            "its own name and the cabinet (task-48 §2.2)"
        ),
    )
    audit_bindings_cmd.add_argument(
        "--fix", action="store_true",
        help=(
            "Clear a binding whose report name belongs to another client "
            "(never guesses a new one)"
        ),
    )

    accuracy_report_cmd = subparsers.add_parser(  # noqa: F841 - no extra arguments
        "accuracy-report",
        help=(
            "Portfolio-wide PDF/API accuracy summary: bindings, cross-check "
            "verdicts, GSC coverage (task-48 §2.4)"
        ),
    )

    reports_run_cmd = subparsers.add_parser(
        "reports-run",
        help=(
            "Generate one client report immediately, without waiting for the "
            "scheduler (task-42) — manual diagnostics, e.g. the live gate"
        ),
    )
    reports_run_cmd.add_argument("--client-id", required=True, help="Client to report on")
    reports_run_cmd.add_argument(
        "--kind", choices=["monthly", "yearly", "custom_months", "comparator"], default="monthly"
    )
    reports_run_cmd.add_argument("--source", choices=["api", "pdf"], default="api")
    reports_run_cmd.add_argument(
        "--years", default=None, help="Comma-separated years, e.g. 2025,2026 (kind=yearly)"
    )
    reports_run_cmd.add_argument(
        "--span", choices=["ytd", "full"], default="full", help="kind=yearly only"
    )
    reports_run_cmd.add_argument(
        "--months",
        default=None,
        help="Comma-separated YYYY-MM, e.g. 2026-07,2025-07 (kind=custom_months/comparator)",
    )
    reports_run_cmd.add_argument(
        "--force-refresh", action="store_true", help="Ignore the closed-period PDF cache (§3.5)"
    )

    subparsers.add_parser(  # noqa: F841 - no extra arguments
        "cloud-status",
        help="Cloud storage (B2) state: last backup, GFS tier counts, sync queue (task-52)",
    )

    subparsers.add_parser(  # noqa: F841 - no extra arguments
        "cloud-backup",
        help=(
            "Run the daily off-site DB backup now, without waiting for the scheduler's "
            "tick (task-52) — idempotent, a no-op if today's backup already exists"
        ),
    )

    cloud_sync_cmd = subparsers.add_parser(
        "cloud-sync",
        help="Run one cloud-storage sweep batch now (task-52), or backfill via --since",
    )
    cloud_sync_cmd.add_argument(
        "--kind",
        choices=["monitor_blob", "report_file"],
        default=None,
        help="Sync only this kind (default: both)",
    )
    cloud_sync_cmd.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max objects per kind (default: cloud_sweep_batch_size)",
    )
    cloud_sync_cmd.add_argument(
        "--since",
        default=None,
        metavar="YYYY-MM-DD",
        help=(
            "Move the web-archive watermark back to this date and sync from there — "
            "a deliberate one-off backfill (task-52 §8 follow-up), never done implicitly"
        ),
    )

    cloud_verify_cmd = subparsers.add_parser(
        "cloud-verify",
        help="Download a random sample of uploaded objects and compare sha256 (task-52)",
    )
    cloud_verify_cmd.add_argument(
        "--kind", choices=["db_backup", "monitor_blob", "report_file"], default=None
    )
    cloud_verify_cmd.add_argument("--sample", type=int, default=20)

    cloud_prune_cmd = subparsers.add_parser(
        "cloud-prune",
        help="Apply GFS retention to DB backups in the cloud (task-52 §3.5)",
    )
    cloud_prune_cmd.add_argument(
        "--dry-run", action="store_true", help="Report what would be removed without deleting"
    )

    cloud_restore_cmd = subparsers.add_parser(
        "cloud-restore",
        help=(
            "Download and verify the latest cloud DB backup onto disk (task-52) — "
            "does NOT touch the running database; prints the restore_sqlite.py command"
        ),
    )
    cloud_restore_cmd.add_argument(
        "--latest", action="store_true", required=True, help="Restore the newest daily backup"
    )
    cloud_restore_cmd.add_argument(
        "--out", required=True, type=Path, help="Directory to write the decompressed .db into"
    )

    archive_capture_cmd = subparsers.add_parser(
        "archive-capture",
        help="Run a Web Archive capture now, without waiting for the 13:30/19:30 schedule",
    )
    archive_capture_cmd.add_argument("--client-id", default=None, help="One client to capture")
    archive_capture_cmd.add_argument(
        "--all", action="store_true", help="Capture every collectable (non-paused) client"
    )
    archive_capture_cmd.add_argument(
        "--no-assets", action="store_true", help="Skip subresources — HTML only, for a quick check"
    )
    archive_capture_cmd.add_argument(
        "--force", action="store_true",
        help="Recapture: store a new snapshot for every URL even if byte-for-byte unchanged",
    )

    archive_backfill_assets_cmd = subparsers.add_parser(
        "archive-backfill-assets",
        help=(
            "Retroactively fetch images/fonts referenced only from CSS "
            "(task-60 §1.3) — predates the discovery fix, or missed by it"
        ),
    )
    archive_backfill_assets_cmd.add_argument(
        "--client-id", default=None, help="Scope to one client's own stored snapshots/CSS"
    )
    archive_backfill_assets_cmd.add_argument(
        "--all", action="store_true", help="Scan the whole portfolio"
    )
    archive_backfill_assets_cmd.add_argument(
        "--dry-run", action="store_true", help="Report what's missing without fetching anything"
    )

    # ── Користувачі (task-71) ────────────────────────────────────────────
    user_create_cmd = subparsers.add_parser(
        "user-create",
        help="Create a user account and print its generated password ONCE",
    )
    user_create_cmd.add_argument("--email", required=True)
    user_create_cmd.add_argument("--name", required=True)
    user_create_cmd.add_argument(
        "--role", default="member", choices=["admin", "member", "viewer"]
    )

    subparsers.add_parser(  # noqa: F841 - no extra arguments
        "user-list", help="List accounts: email, name, role, active, last login"
    )

    user_reset_cmd = subparsers.add_parser(
        "user-reset-password",
        help="Issue a new generated password (revokes that user's sessions)",
    )
    user_reset_cmd.add_argument("--email", required=True)

    user_deactivate_cmd = subparsers.add_parser(
        "user-deactivate", help="Deactivate an account and revoke its sessions"
    )
    user_deactivate_cmd.add_argument("--email", required=True)

    # ── Журнал дій (task-72) ─────────────────────────────────────────────
    audit_tail_cmd = subparsers.add_parser(
        "audit-tail", help="Print the most recent audit events (who did what, when)"
    )
    audit_tail_cmd.add_argument("--limit", type=int, default=30)
    audit_tail_cmd.add_argument("--action", default=None, help="Filter by exact action name")
    audit_tail_cmd.add_argument("--actor", default=None, help="Filter by actor email")

    subparsers.add_parser(  # noqa: F841 - no extra arguments
        "audit-retention",
        help=(
            "Export audit rows older than AUDIT_RETENTION_DAYS to JSONL, delete them, "
            "and drop expired sessions. Runs daily on its own; this is the manual door"
        ),
    )

    subparsers.add_parser(  # noqa: F841 - no extra arguments
        "ensure-admin",
        help=(
            "Create the bootstrap administrator from BOOTSTRAP_ADMIN_EMAIL if, and only "
            "if, the users table is empty. No-op otherwise — a restart must never "
            "resurrect an account someone deliberately deactivated"
        ),
    )

    subparsers.add_parser(  # noqa: F841 - no extra arguments
        "db-maintenance",
        help=(
            "ANALYZE + wal_checkpoint(TRUNCATE) + PRAGMA optimize (task-66 §1.4) — "
            "sqlite_stat1 was empty in production, meaning the query planner had "
            "never seen table/index statistics for a 761MB database"
        ),
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "import-ai-seo-history":
        source: Path = args.source
        if not source.is_dir():
            print(f"error: --source is not a directory: {source}", file=sys.stderr)
            return 2

        report = run_import(
            source=source, dry_run=args.dry_run, session_factory=get_sync_sessionmaker()
        )
        print(render_report(report, dry_run=args.dry_run))
        return 0

    if args.command in {"audit-tail", "audit-retention"}:
        return _run_audit_command(args)

    if args.command in {
        "user-create",
        "user-list",
        "user-reset-password",
        "user-deactivate",
        "ensure-admin",
    }:
        return _run_user_command(args)

    if args.command == "backfill-health-history":
        # Imported here, not at module scope: this pulls in the API layer,
        # which the import command has no reason to load.
        from app.api.dashboard import backfill_health_history

        if args.days < 1:
            print("error: --days must be >= 1", file=sys.stderr)
            return 2

        session_factory = get_sync_sessionmaker()
        with session_factory() as session:
            written = backfill_health_history(session, days=args.days, now=datetime.now(UTC))
            session.commit()
        print(f"recorded {written} daily health point(s) across the last {args.days} day(s)")
        return 0

    if args.command == "backfill-daily-history":
        # Imported here, not at module scope: same rationale as the health
        # backfill above — this pulls in the DB/job-engine layer, which the
        # import command has no reason to load.
        from app.db.repositories.clients import ClientRepository
        from app.db.repositories.jobs import JobRepository
        from app.schemas.enums import ComparisonMode, PeriodKind, Provider

        if args.days < 1:
            print("error: --days must be >= 1", file=sys.stderr)
            return 2

        provider_names = [p.strip() for p in args.providers.split(",") if p.strip()]
        if not provider_names:
            print("error: --providers must name at least one provider", file=sys.stderr)
            return 2
        try:
            providers = [Provider(name) for name in provider_names]
        except ValueError as exc:
            print(f"error: unknown provider in --providers: {exc}", file=sys.stderr)
            return 2

        session_factory = get_sync_sessionmaker()
        with session_factory() as session:
            clients_repo = ClientRepository(session)
            if args.client_ids:
                requested = [c.strip() for c in args.client_ids.split(",") if c.strip()]
                found = [clients_repo.get(cid) for cid in requested]
                pairs = zip(requested, found, strict=True)
                missing = [cid for cid, client in pairs if client is None]
                if missing:
                    print(f"error: unknown --client-ids: {', '.join(missing)}", file=sys.stderr)
                    return 2
                clients = [client for client in found if client is not None]
            else:
                # Без явного --client-ids збираємо лише активних (task-34);
                # --include-paused повертає стару поведінку одним прапорцем.
                visible = clients_repo.list_active()
                clients = visible if args.include_paused else clients_repo.list_collectable()
                skipped_paused = len(visible) - len(clients)
                if skipped_paused:
                    # Мовчазний пропуск виглядав би як «клієнт зник» — кажемо прямо.
                    print(
                        f"skipping {skipped_paused} paused client(s); "
                        "pass --include-paused to collect them too"
                    )

            if not clients:
                print("error: no clients to collect (empty client list)", file=sys.stderr)
                return 2

            window_end = datetime.now(UTC).date() - timedelta(days=1)
            window_start = window_end - timedelta(days=args.days - 1)
            cell_count = len(clients) * len(providers)

            print(f"range: {window_start} .. {window_end} ({args.days} day(s))")
            print(f"clients: {len(clients)}  providers: {', '.join(p.value for p in providers)}")
            print(f"cells to queue: {cell_count}")
            print(
                "note: each cell issues multiple provider requests (GSC ~2 queries, "
                "GA4 ~12 queries per client) — this is a metered live collection, "
                "not a local computation. --dry-run does not spend units."
            )
            if args.dry_run:
                print("dry run: no job created")
                return 0

            # `force_refresh`: without it, a cell whose provider already has a
            # recent snapshot (the normal weekly sync keeps one) is skipped by
            # the freshness gate regardless of which period was requested —
            # silently producing zero new daily rows despite "queued" below.
            jobs = JobRepository(session).create_run(
                client_ids=[client.id for client in clients],
                providers=providers,
                period_kind=PeriodKind.custom,
                comparison_mode=ComparisonMode.closed,
                anchor_date=window_end,
                custom_start=window_start,
                custom_end=window_end,
                mode="force_refresh",
            )
            session.commit()
        print(f"queued {len(jobs)} job(s); the running worker will collect them")
        return 0

    if args.command == "backfill-ga4-channels":
        # Imported here, not at module scope: same rationale as the other
        # backfill commands — this pulls in the DB/adapter layer, which the
        # import command has no reason to load.
        from app.db.repositories.analytics_datasets import (
            AnalyticsDatasetRepository,
            AnalyticsDatasetRowInput,
        )
        from app.db.repositories.clients import ClientRepository
        from app.db.repositories.jobs import JobRepository
        from app.db.repositories.snapshots import SnapshotRepository
        from app.integrations.reporting_ninja import query_daily_channels
        from app.schemas.enums import PeriodKind, Provider, Quality, SourceMode

        if args.days < 1:
            print("error: --days must be >= 1", file=sys.stderr)
            return 2

        session_factory = get_sync_sessionmaker()
        with session_factory() as session:
            clients_repo = ClientRepository(session)
            if args.client:
                client = clients_repo.get(args.client)
                if client is None:
                    print(f"error: unknown --client: {args.client}", file=sys.stderr)
                    return 2
                candidates = [client]
            else:
                # Mirrors `backfill-daily-history`'s default (task-34): a
                # metered live collection should not spend requests on a
                # paused client unless it is named explicitly via --client.
                candidates = clients_repo.list_collectable()

            channel_targets = []
            for candidate in candidates:
                integration = clients_repo.get_integration(candidate.id, Provider.reporting_ninja)
                if integration is None or not integration.enabled:
                    continue
                cfg = integration.config_json or {}
                if not cfg.get("connection_key") or not cfg.get("account_id"):
                    continue
                channel_targets.append((candidate, cfg))

            if not channel_targets:
                print(
                    "error: no clients with an enabled, configured Reporting Ninja integration",
                    file=sys.stderr,
                )
                return 2

            window_end = datetime.now(UTC).date() - timedelta(days=1)
            window_start = window_end - timedelta(days=args.days - 1)

            print(f"range: {window_start} .. {window_end} ({args.days} day(s))")
            names = ", ".join(c.name for c, _ in channel_targets)
            print(f"clients: {len(channel_targets)} ({names})")
            print(
                "note: one GA4 query per client — a metered live request, "
                "not a local computation."
            )
            if args.dry_run:
                print("dry run: no requests made, nothing written")
                return 0

            from seo_audit.clientreport.api_client import (
                APIError,
                RateLimitError,
                ReportingNinjaClient,
            )

            analytics_repo = AnalyticsDatasetRepository(session)
            job_repo = JobRepository(session)
            snapshot_repo = SnapshotRepository(session)
            observed_at = datetime.now(UTC)
            total_written = 0
            with ReportingNinjaClient() as rn_client:
                for candidate, cfg in channel_targets:
                    existing = analytics_repo.daily_series(
                        [candidate.id],
                        Provider.reporting_ninja,
                        "ga4_daily_channels",
                        window_start,
                        window_end,
                    ).get(candidate.id, {})
                    try:
                        rows, truncated = query_daily_channels(
                            rn_client,
                            connection_key=cfg["connection_key"],
                            account_id=cfg["account_id"],
                            start=window_start,
                            end=window_end,
                        )
                    except (APIError, RateLimitError, ValueError) as exc:
                        print(f"{candidate.name}: failed — {exc}")
                        continue

                    fresh = [
                        (day, metrics)
                        for day, metrics in rows
                        if date.fromisoformat(day) not in existing
                    ]
                    if not fresh:
                        print(f"{candidate.name}: {len(rows)} day(s) fetched, already up to date")
                        continue

                    job, _cell = job_repo.create_imported_run(
                        client_id=candidate.id,
                        provider=Provider.reporting_ninja,
                        observed_at=observed_at,
                        period_kind=PeriodKind.custom,
                    )
                    snapshot = snapshot_repo.create_immutable(
                        job_id=job.id,
                        client_id=candidate.id,
                        provider=Provider.reporting_ninja,
                        source_mode=SourceMode.api,
                        observed_at=observed_at,
                        quality=Quality.truncated if truncated else Quality.complete,
                        data_through=window_end,
                        provenance={"transport": "reporting_ninja", "cli": "backfill-ga4-channels"},
                    )
                    analytics_repo.bulk_insert(
                        snapshot_id=snapshot.id,
                        client_id=candidate.id,
                        provider=Provider.reporting_ninja,
                        period_start=window_start,
                        period_end=window_end,
                        rows=[
                            AnalyticsDatasetRowInput(
                                dataset_key="ga4_daily_channels",
                                dimension_key="date",
                                dimension_value=day,
                                metrics=metrics,
                                quality=Quality.truncated if truncated else Quality.complete,
                                rank=None,
                            )
                            for day, metrics in fresh
                        ],
                    )
                    session.commit()
                    total_written += len(fresh)
                    already = len(rows) - len(fresh)
                    print(
                        f"{candidate.name}: wrote {len(fresh)} day(s) "
                        f"({already} already present)"
                    )
        print(f"done: {total_written} day(s) written across {len(channel_targets)} client(s)")
        return 0

    if args.command == "monitor-run":
        # Imported here, not at module scope: pulls in the monitor engine
        # (fetch/parse/diff), which the other commands have no reason to load.
        from app.core.config import get_settings
        from app.db.repositories.clients import ClientRepository
        from app.db.repositories.monitor import MonitorRepository
        from app.monitor.runner import effective_fetch_mode, run_client_monitor

        if bool(args.client_id) == bool(args.all):
            print("error: pass exactly one of --client-id or --all", file=sys.stderr)
            return 2

        settings = get_settings()
        session_factory = get_sync_sessionmaker()
        with session_factory() as session:
            clients_repo = ClientRepository(session)
            monitor_repo = MonitorRepository(session)
            if args.client_id:
                client = clients_repo.get(args.client_id)
                if client is None:
                    print(f"error: unknown --client-id: {args.client_id}", file=sys.stderr)
                    return 2
                clients = [client]
            else:
                clients = clients_repo.list_collectable()

            if not clients:
                print("error: no clients to check (empty client list)", file=sys.stderr)
                return 2

            exit_code = 0
            for client in clients:
                if args.fetch_mode and args.fetch_mode != "auto":
                    mode = args.fetch_mode
                else:
                    mode = effective_fetch_mode(
                        monitor_repo.get_settings(client.id), now=datetime.now(UTC)
                    )
                outcome = run_client_monitor(
                    session,
                    client,
                    kind=args.kind,
                    settings=settings,
                    artifacts_dir=settings.artifacts_dir,
                    trigger="manual",
                    fetch_mode=mode,
                )
                print(
                    f"{client.name}: {outcome.status} [{mode}] — checked {outcome.urls_checked} "
                    f"url(s){' (truncated)' if outcome.truncated else ''}"
                )
                if outcome.status != "succeeded":
                    exit_code = 1
        return exit_code

    if args.command == "monitor-status":
        from app.db.repositories.clients import ClientRepository
        from app.db.repositories.monitor import MonitorRepository

        session_factory = get_sync_sessionmaker()
        with session_factory() as session:
            clients_repo = ClientRepository(session)
            monitor_repo = MonitorRepository(session)
            client_map = {c.id: c for c in clients_repo.list_active()}
            counts = monitor_repo.portfolio_open_severity_counts(list(client_map))

            any_critical = False
            for client_id, client in sorted(client_map.items(), key=lambda item: item[1].name):
                client_counts = counts.get(client_id, {"critical": 0, "warning": 0, "info": 0})
                if args.severity and client_counts.get(args.severity, 0) == 0:
                    continue
                if client_counts["critical"]:
                    any_critical = True
                last_full = monitor_repo.latest_finished_run(client_id, kind="full")
                last_seen = last_full.started_at.isoformat() if last_full else "never"
                print(
                    f"{client.name:30s}  critical={client_counts['critical']:<3d} "
                    f"warning={client_counts['warning']:<3d} info={client_counts['info']:<3d} "
                    f"last_full={last_seen}"
                )
        return 1 if any_critical else 0

    if args.command == "monitor-prune":
        # One implementation, shared with the daily scheduled pass
        # (`CloudScheduler._run_local_retention_if_due`): chunked, committed
        # rows-before-files, and cloud-gated unless --ignore-cloud. Doing it
        # here by hand is what left 7,341 rows pointing at deleted files on
        # 2026-09-04 — see `prune_monitor_bodies`'s docstring.
        from app.cloud.client import CloudStore
        from app.cloud.sweeper import prune_monitor_bodies
        from app.core.config import get_settings

        settings = get_settings()
        session_factory = get_sync_sessionmaker()
        store = None if args.ignore_cloud else CloudStore(settings)
        # Without --bodies-older-than the command keeps its original,
        # narrower job: only blobs no version references at all.
        older_than = args.bodies_older_than if args.bodies_older_than is not None else 0
        with session_factory() as session:
            prune_result = prune_monitor_bodies(
                session,
                settings,
                older_than_days=older_than,
                store=store,
                dry_run=args.dry_run,
                include_unreferenced=True,
            )

        if prune_result.candidates == 0:
            print("nothing to prune")
            return 0
        print(f"{prune_result.candidates} blob(s) are prune candidates")
        if prune_result.deferred_not_uploaded:
            print(
                f"  deferring {prune_result.deferred_not_uploaded}: not yet confirmed uploaded "
                "to the cloud (pass --ignore-cloud to drop anyway)"
            )
        if args.dry_run:
            print(f"would remove {prune_result.removed} blob(s)")
            return 0
        print(f"pruned {prune_result.removed} blob(s)")
        if prune_result.chunks_failed:
            print(
                f"  {prune_result.chunks_failed} chunk(s) skipped: database was locked by the "
                "monitor mid-write — rerun later, nothing was left inconsistent"
            )
        return 0

    if args.command == "monitor-resolve-blocked":
        from collections import Counter

        from app.core.config import get_settings
        from app.db.repositories.clients import ClientRepository
        from app.db.repositories.monitor import MonitorRepository
        from app.monitor.fetcher import Headers, classify_block
        from app.monitor.storage import read_blob

        settings = get_settings()
        session_factory = get_sync_sessionmaker()
        with session_factory() as session:
            clients_repo = ClientRepository(session)
            client = clients_repo.get(args.client_id)
            if client is None:
                print(f"error: unknown --client-id: {args.client_id}", file=sys.stderr)
                return 2
            repo = MonitorRepository(session)

            # Ground truth is each page's CURRENT stored state, not
            # `MonitorRun.notes_json.blocked_count` (task-50 §A onward only
            # tags that going forward — every run that actually produced this
            # damage predates the fix and has nothing there). A page sitting
            # at a block-shaped status whose stored body still reads as a
            # challenge page is unambiguous: after this fix a real fetch can
            # never write that combination again.
            blocked_page_ids: set[str] = set()
            for page in repo.all_pages(client.id):
                if page.status not in (403, 429, 503):
                    continue
                version = repo.latest_page_version(page.id)
                if version is None or not version.body_blob_sha256:
                    continue
                try:
                    body = read_blob(settings.artifacts_dir, version.body_blob_sha256)
                except FileNotFoundError:
                    continue
                error, _ = classify_block(page.status, Headers(), body)
                if error == "blocked":
                    blocked_page_ids.add(page.id)

            if not blocked_page_ids:
                print(f"nothing to resolve: no page for {client.name} currently reads as blocked")
                return 0

            # Every open change this corrupted observation could plausibly
            # have produced carries `related_id == page.id`
            # (`_persist_page_result` stamps it on every page-scope diff, and
            # on the per-page sitemap-status branch too — task-41). The one
            # portfolio-wide exception is the `sitemap_broken_urls` aggregate,
            # which has no single page to point at but is derived from the
            # exact same corrupted counts.
            open_changes = repo.list_changes(client.id, state="open", limit=10000)
            changes_to_resolve = [
                change
                for change in open_changes
                if change.change_type != "monitor_blocked"
                and (args.run_id is None or change.run_id == args.run_id)
                and (
                    change.related_id in blocked_page_ids
                    or (
                        change.change_type == "sitemap_broken_urls"
                        and change.subject == f"{client.domain}/sitemap"
                    )
                )
            ]
            by_type = Counter(change.change_type for change in changes_to_resolve)
            verb = "would resolve" if args.dry_run else "resolving"
            print(
                f"{len(blocked_page_ids)} page(s) for {client.name} currently read as blocked; "
                f"{verb} {len(changes_to_resolve)} open change(s)"
            )
            for change_type, count in sorted(by_type.items(), key=lambda kv: -kv[1]):
                print(f"  {change_type}: {count}")
            if args.dry_run:
                return 0

            now = datetime.now(UTC)
            for change in changes_to_resolve:
                repo.resolve_change(change, now=now)
                change.acknowledged_at = now
                change.acknowledged_note = "resolved: antibot block, not a real change (task-50)"
            session.commit()
        print(f"resolved {len(changes_to_resolve)} change(s)")
        return 0

    if args.command == "monitor-redirects":
        from app.core.config import get_settings
        from app.db.repositories.clients import ClientRepository
        from app.db.repositories.monitor import MonitorRepository
        from app.monitor.redirects.collect import process_redirects
        from app.monitor.redirects.credentials import (
            credentials_status,
            login_credentials_status,
        )
        from app.monitor.runner import resolve_base_url

        settings = get_settings()
        session_factory = get_sync_sessionmaker()
        with session_factory() as session:
            clients_repo = ClientRepository(session)
            client = clients_repo.get(args.client_id)
            if client is None:
                print(f"error: unknown --client-id: {args.client_id}", file=sys.stderr)
                return 2
            repo = MonitorRepository(session)
            monitor_settings = repo.get_or_create_settings(client.id)

            if args.check:
                base_url = resolve_base_url(
                    client, monitor_settings.base_url_override, monitor_settings.canonical_www
                )
                redirect_run = repo.create_run(client.id, kind="light", trigger="manual")
                session.commit()
                redirects_outcome = process_redirects(
                    repo,
                    client,
                    base_url,
                    run_id=redirect_run.id,
                    monitor_settings=monitor_settings,
                    settings=settings,
                    artifacts_dir=settings.artifacts_dir,
                    key_pages=set(),
                    now=datetime.now(UTC),
                )
                repo.finish_run(redirect_run.id, status="succeeded", truncated=False)
                session.commit()
                if not redirects_outcome.ok:
                    print(f"check failed: {redirects_outcome.unavailable_reason}")
                else:
                    print(
                        f"checked: +{redirects_outcome.added} added, "
                        f"~{redirects_outcome.changed} changed, "
                        f"-{redirects_outcome.removed} removed"
                    )

            app_password_status = credentials_status(settings.wp_credentials_path, client.id)
            login_status = login_credentials_status(
                settings.wp_login_credentials_path, client.id
            )
            summary = repo.redirects_summary(client.id)
            last_checked = monitor_settings.redirects_last_checked_at
            print(f"{client.name} ({client.domain})")
            print(f"  redirect_source:   {monitor_settings.redirect_source}")
            print(f"  app password:      {app_password_status.value}")
            print(f"  wp-admin login:    {login_status.value}")
            print(f"  last checked:      {last_checked.isoformat() if last_checked else 'never'}")
            print(
                f"  rules:             {summary.get('enabled', 0)} enabled, "
                f"{summary.get('disabled', 0)} disabled"
            )
        return 0

    if args.command == "wp-credentials-check":
        from app.core.config import get_settings
        from app.db.repositories.clients import ClientRepository
        from app.monitor.redirects.credentials import (
            credentials_status,
            login_credentials_status,
        )

        settings = get_settings()
        session_factory = get_sync_sessionmaker()
        with session_factory() as session:
            clients = ClientRepository(session).list_active()
            print(f"{'client':30s} {'domain':35s} {'app_password':14s} wp_login")
            for c in sorted(clients, key=lambda item: item.name):
                app_password_status = credentials_status(settings.wp_credentials_path, c.id)
                login_status = login_credentials_status(settings.wp_login_credentials_path, c.id)
                print(
                    f"{c.name:30s} {c.domain:35s} "
                    f"{app_password_status.value:14s} {login_status.value}"
                )
        return 0

    if args.command == "audit-ninja-bindings":
        # Imported here, not at module scope: pulls in the reports engine's
        # browser client, which the other commands have no reason to load.
        from app.core.config import get_settings
        from app.db.repositories.clients import ClientRepository
        from app.reports import ninja_browser
        from app.schemas.enums import Provider

        settings = get_settings()
        session_factory = get_sync_sessionmaker()
        with session_factory() as session:
            clients_repo = ClientRepository(session)
            clients = clients_repo.list_all()

            email = os.environ.get("REPORTING_NINJA_EMAIL", "")
            password = os.environ.get("REPORTING_NINJA_PASSWORD", "")
            cabinet_names: list[str] | None = None
            cabinet_truncated = True
            if email and password:
                try:
                    cabinet_names, cabinet_truncated = ninja_browser.list_report_names(
                        email=email, password=password, headless=settings.reports_headless,
                    )
                except Exception as exc:  # noqa: BLE001 - the audit still reports bindings without cabinet verification
                    print(
                        f"warning: could not enumerate the Reporting Ninja cabinet: {exc}",
                        file=sys.stderr,
                    )
            else:
                print(
                    "warning: REPORTING_NINJA_EMAIL/PASSWORD not set — bindings are checked "
                    "by name only, not against the cabinet",
                    file=sys.stderr,
                )

            any_unfixed_foreign = False
            for client in sorted(clients, key=lambda c: c.name):
                integration = clients_repo.get_integration(client.id, Provider.reporting_ninja)
                name = integration.config_json.get("ninja_report_name") if integration else None
                if not name:
                    print(f"{client.name:20s}  no binding")
                    continue

                foreign = not name.upper().startswith(client.name.upper())
                not_in_cabinet = (
                    cabinet_names is not None
                    and not cabinet_truncated
                    and name not in cabinet_names
                )
                note = "  [not found in cabinet]" if not_in_cabinet else ""

                if not foreign:
                    print(f"{client.name:20s}  ok: {name!r}{note}")
                    continue

                if args.fix:
                    assert integration is not None
                    config = dict(integration.config_json)
                    del config["ninja_report_name"]
                    clients_repo.upsert_integration(
                        client.id, Provider.reporting_ninja,
                        enabled=integration.enabled, source_mode=integration.source_mode,
                        config=config,
                    )
                    session.commit()
                    print(f"{client.name:20s}  FOREIGN BINDING: {name!r} — cleared{note}")
                else:
                    any_unfixed_foreign = True
                    print(f"{client.name:20s}  FOREIGN BINDING: {name!r}{note}")
        return 1 if any_unfixed_foreign else 0

    if args.command == "accuracy-report":
        # Imported here, not at module scope: pulls in the reports API
        # module (and, transitively, the whole reports engine), which the
        # other commands have no reason to load.
        from app.api.reports import list_client_accuracy
        from app.db.session import worker_session_scope

        with worker_session_scope() as session:
            accuracy_rows = list_client_accuracy(session)

        any_issue = False
        for row in sorted(accuracy_rows, key=lambda r: r.client_name):
            if row.binding_status == "foreign":
                any_issue = True
            if row.latest_pdf_run_id is None:
                cc_text = "no PDF run yet"
            else:
                cc_text = (
                    f"{row.match_count} match · {row.no_data_count} no data · "
                    f"{row.differ_count} differ"
                )
                if row.differ_count:
                    any_issue = True
            print(
                f"{row.client_name:20s}  binding={row.binding_status:14s} "
                f"gsc={'yes' if row.has_gsc else 'no':3s}  {cc_text}"
            )
        return 1 if any_issue else 0

    if args.command == "reports-run":
        # Imported here, not at module scope: pulls in the reports engine
        # (periods/API-or-browser fetch/table/xlsx), which the other
        # commands have no reason to load.
        from app.core.config import get_settings
        from app.db.repositories.clients import ClientRepository
        from app.db.repositories.reports import ReportRepository
        from app.db.session import worker_session_scope
        from app.reports.periods import InvalidComparatorMonthsError, InvalidPeriodError
        from app.reports.runner import build_periods
        from app.reports.runner import run as run_report

        settings = get_settings()
        session_factory = get_sync_sessionmaker()
        with session_factory() as session:
            client = ClientRepository(session).get(args.client_id)
            if client is None:
                print(f"error: unknown --client-id: {args.client_id}", file=sys.stderr)
                return 2
            client_name = client.name

            params: dict[str, object] = {"force_refresh": args.force_refresh}
            if args.kind == "yearly":
                if not args.years:
                    print("error: --years is required for kind=yearly", file=sys.stderr)
                    return 2
                params["years"] = [int(y.strip()) for y in args.years.split(",") if y.strip()]
                params["span"] = args.span
            if args.kind in ("custom_months", "comparator"):
                if not args.months:
                    print(f"error: --months is required for kind={args.kind}", file=sys.stderr)
                    return 2
                params["months"] = [m.strip() for m in args.months.split(",") if m.strip()]

            try:
                periods = build_periods(
                    args.kind, params,
                    max_months=settings.reports_max_months,
                    max_comparator_years=settings.reports_max_comparator_years,
                )
            except (InvalidPeriodError, InvalidComparatorMonthsError, ValueError) as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 2

            run = ReportRepository(session).create_run(
                args.client_id, kind=args.kind, source=args.source,
                params=params, period_count=len(periods),
            )
            session.commit()
            run_id = run.id

        run_report(worker_session_scope, settings, run_id)

        with session_factory() as session:
            finished = ReportRepository(session).get(run_id)
            assert finished is not None
            print(
                f"{client_name}: {finished.status} "
                f"({finished.periods_done}/{finished.period_count} periods, "
                f"{finished.periods_failed} failed)"
            )
            if finished.cross_check_status:
                print(f"  cross-check: {finished.cross_check_status}")
            if finished.error_code:
                print(f"  error: {finished.error_code} — {finished.error_message}")
            status = finished.status
        return 0 if status == "succeeded" else 1

    if args.command == "cloud-status":
        from app.cloud.sweeper import ARCHIVE_WATERMARK
        from app.core.config import get_settings
        from app.db.repositories.cloud import CloudLedgerRepository

        settings = get_settings()
        if not settings.cloud_enabled:
            print("cloud storage: disabled (CLOUD_ENABLED=false)")
            return 0

        session_factory = get_sync_sessionmaker()
        with session_factory() as session:
            ledger = CloudLedgerRepository(session)
            last = ledger.latest_backup()
            archive_since = ledger.get_watermark(ARCHIVE_WATERMARK)

            print(f"bucket: {settings.b2_vault_bucket_name}")
            if last is not None and last.uploaded_at is not None:
                age_hours = (datetime.now(UTC) - last.uploaded_at).total_seconds() / 3600
                print(f"last backup: {last.key} ({last.size_bytes} bytes, {age_hours:.1f}h ago)")
            else:
                print("last backup: none yet")

            for tier in ("daily", "weekly", "monthly"):
                print(f"  {tier}: {len(ledger.backups_by_tier(tier))} kept")

            print(f"archive since: {archive_since.isoformat() if archive_since else 'not started'}")

            for kind in ("monitor_blob", "report_file"):
                kind_totals = ledger.kind_counts(kind)
                if kind == "monitor_blob":
                    total = ledger.monitor_blob_total(since=archive_since) if archive_since else 0
                else:
                    total = ledger.report_file_total()
                pending = max(0, total - kind_totals["uploaded"] - kind_totals["failed"])
                print(
                    f"  {kind}: uploaded={kind_totals['uploaded']} failed={kind_totals['failed']} "
                    f"pending={pending}"
                )
        return 0

    if args.command == "cloud-backup":
        from app.cloud.backup import prune_backups, run_daily_backup
        from app.cloud.client import CloudStore
        from app.core.config import get_settings

        settings = get_settings()
        if not settings.cloud_enabled:
            print("error: cloud storage is disabled (CLOUD_ENABLED=false)", file=sys.stderr)
            return 2
        store = CloudStore(settings)
        session_factory = get_sync_sessionmaker()
        with session_factory() as session:
            backup_outcome = run_daily_backup(session, store, settings)
            if backup_outcome is None:
                print("today's backup already exists — nothing to do")
                return 0
            prune_backups(session, store, settings)
        print(
            f"backup uploaded: {backup_outcome.key} ({backup_outcome.size_bytes} bytes, "
            f"sha256 {backup_outcome.sha256[:12]}…)"
        )
        if backup_outcome.promoted_tiers:
            print(f"promoted to: {', '.join(backup_outcome.promoted_tiers)}")
        return 0

    if args.command == "cloud-sync":
        from app.cloud.client import CloudStore
        from app.cloud.sweeper import ARCHIVE_WATERMARK, sweep_monitor_blobs, sweep_report_files
        from app.core.config import get_settings
        from app.db.repositories.cloud import CloudLedgerRepository

        settings = get_settings()
        if not settings.cloud_enabled:
            print("error: cloud storage is disabled (CLOUD_ENABLED=false)", file=sys.stderr)
            return 2
        store = CloudStore(settings)
        session_factory = get_sync_sessionmaker()

        if args.since:
            try:
                since_date = datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=UTC)
            except ValueError:
                print(f"error: --since must be YYYY-MM-DD, got {args.since!r}", file=sys.stderr)
                return 2
            with session_factory() as session:
                CloudLedgerRepository(session).set_watermark(ARCHIVE_WATERMARK, since_date)
                session.commit()
            print(
                f"watermark moved to {since_date.date().isoformat()} — "
                "next sync includes blobs from then on"
            )

        kinds = [args.kind] if args.kind else ["monitor_blob", "report_file"]
        with session_factory() as session:
            for kind in kinds:
                if kind == "monitor_blob":
                    result = sweep_monitor_blobs(session, store, settings, limit=args.limit)
                else:
                    result = sweep_report_files(session, store, settings, limit=args.limit)
                print(
                    f"{kind}: attempted={result.attempted} uploaded={result.uploaded} "
                    f"failed={result.failed}"
                )
                if result.reasons:
                    print(f"  reasons: {result.reasons}")
        return 0

    if args.command == "cloud-verify":
        from app.cloud.client import CloudStore
        from app.cloud.verify import verify_sample
        from app.core.config import get_settings

        settings = get_settings()
        if not settings.cloud_enabled:
            print("error: cloud storage is disabled (CLOUD_ENABLED=false)", file=sys.stderr)
            return 2
        store = CloudStore(settings)
        session_factory = get_sync_sessionmaker()
        with session_factory() as session:
            outcomes = verify_sample(session, store, kind=args.kind, sample=args.sample)

        if not outcomes:
            print("nothing to verify (no uploaded objects yet)")
            return 0

        failed = [o for o in outcomes if not o.ok]
        for verify_outcome in outcomes:
            status = "OK" if verify_outcome.ok else f"FAIL ({verify_outcome.detail})"
            print(f"{verify_outcome.kind:14s} {verify_outcome.source_ref:40s} {status}")
        print(f"\n{len(outcomes) - len(failed)}/{len(outcomes)} verified OK")
        return 1 if failed else 0

    if args.command == "cloud-prune":
        from app.cloud.backup import prune_backups
        from app.cloud.client import CloudStore
        from app.core.config import get_settings

        settings = get_settings()
        if not settings.cloud_enabled:
            print("error: cloud storage is disabled (CLOUD_ENABLED=false)", file=sys.stderr)
            return 2
        store = CloudStore(settings)
        session_factory = get_sync_sessionmaker()
        with session_factory() as session:
            removed = prune_backups(session, store, settings, dry_run=args.dry_run)
        verb = "would remove" if args.dry_run else "removed"
        for tier, count in removed.items():
            print(f"{tier}: {verb} {count}")
        return 0

    if args.command == "cloud-restore":
        import gzip
        import hashlib
        import json
        import sqlite3

        from app.cloud.client import CloudStore
        from app.core.config import get_settings
        from app.db.repositories.cloud import CloudLedgerRepository

        settings = get_settings()
        if not settings.cloud_enabled:
            print("error: cloud storage is disabled (CLOUD_ENABLED=false)", file=sys.stderr)
            return 2
        store = CloudStore(settings)
        session_factory = get_sync_sessionmaker()
        with session_factory() as session:
            latest = CloudLedgerRepository(session).latest_backup()
        if latest is None:
            print("error: no backup found in the cloud", file=sys.stderr)
            return 2

        manifest_key = latest.key.replace(".db.gz", ".manifest.json")
        compressed = store.get_bytes(latest.key)
        actual_sha256 = hashlib.sha256(compressed).hexdigest()
        if latest.sha256 and actual_sha256 != latest.sha256:
            print(f"error: sha256 mismatch downloading {latest.key}", file=sys.stderr)
            return 1

        try:
            manifest = json.loads(store.get_bytes(manifest_key))
        except Exception:  # noqa: BLE001 - the manifest is a diagnostic extra, never load-bearing
            manifest = {}

        raw = gzip.decompress(compressed)
        args.out.mkdir(parents=True, exist_ok=True)
        dest = args.out / Path(latest.key).name.removesuffix(".gz")
        dest.write_bytes(raw)

        verify_conn = sqlite3.connect(f"file:{dest}?mode=ro", uri=True)
        try:
            integrity = verify_conn.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            verify_conn.close()

        print(f"downloaded: {dest} ({len(raw)} bytes)")
        print(f"integrity_check: {integrity}")
        if manifest.get("alembic_revision"):
            print(f"alembic revision at backup time: {manifest['alembic_revision']}")
        if integrity != "ok":
            print("error: integrity_check failed — do NOT restore this file", file=sys.stderr)
            return 1

        print("\nTo restore (STOPS THE WRITER — see docs/backup-restore.md):")
        print("  docker compose stop api worker")
        print(
            "  docker compose run --rm api poetry run python scripts/restore_sqlite.py "
            f"--from {dest}"
        )
        print(
            "  docker compose run --rm api poetry run python scripts/restore_sqlite.py "
            f"--from {dest} --force"
        )
        print("  docker compose up -d")
        return 0

    if args.command == "archive-capture":
        # Imported here, not at module scope: pulls in the archive engine
        # (fetch/fingerprint/rewrite), which the other commands have no
        # reason to load — same convention as `monitor-run` above.
        import uuid

        from app.core.config import get_settings
        from app.db.repositories.clients import ClientRepository
        from app.db.repositories.web_archive import WebArchiveRepository
        from app.webarchive.capture import run_client_capture
        from app.webarchive.writequeue import ArchiveWriteController

        if bool(args.client_id) == bool(args.all):
            print("error: pass exactly one of --client-id or --all", file=sys.stderr)
            return 2

        settings = get_settings()
        session_factory = get_sync_sessionmaker()
        # This CLI invocation runs in its own OS process (task-62 §2.3): the
        # worker's `ArchiveScheduler` cannot see this controller either, so
        # cross-process write contention still falls back to SQLite's own
        # busy_timeout + `commit_with_retry`'s backoff — fine, a rare tail,
        # not the in-process pileup this controller exists to remove.
        write_controller = ArchiveWriteController()
        write_controller.start()
        try:
            with session_factory() as session:
                clients_repo = ClientRepository(session)
                archive_repo = WebArchiveRepository(session)
                if args.client_id:
                    client = clients_repo.get(args.client_id)
                    if client is None:
                        print(f"error: unknown --client-id: {args.client_id}", file=sys.stderr)
                        return 2
                    clients = [client]
                else:
                    clients = clients_repo.list_collectable()

                if not clients:
                    print("error: no clients to capture (empty client list)", file=sys.stderr)
                    return 2

                if args.no_assets:
                    for client in clients:
                        archive_repo.update_settings(client.id, capture_assets=False)
                    session.commit()

                exit_code = 0
                for client in clients:
                    # Mirrors `ArchiveScheduler._tick`'s own guard: without this,
                    # a manual run can race a still-running scheduled capture
                    # for the same client (live-observed, task-59 2026-09-04) —
                    # wasted duplicate work, not a data-safety issue (cells are
                    # isolated), but confusing and worth refusing outright.
                    if archive_repo.has_running_capture(client.id):
                        print(
                            f"{client.name}: skipped — a capture is already running",
                            file=sys.stderr,
                        )
                        exit_code = 1
                        continue
                    capture = archive_repo.create_capture(
                        client.id,
                        slot_key=f"manual:{uuid.uuid4()}",
                        scheduled_for=datetime.now(UTC),
                        trigger="manual",
                        force=args.force,
                    )
                    session.commit()
                    archive_repo.claim_capture(capture.id, owner="cli")
                    session.commit()
                    capture_outcome = run_client_capture(
                        session, client, capture_id=capture.id, settings=settings,
                        artifacts_dir=settings.artifacts_dir,
                        write_controller=write_controller, session_scope=session_factory,
                        force=args.force,
                    )
                    archive_repo.finish_capture(
                        capture.id, status=capture_outcome.status  # type: ignore[arg-type]
                    )
                    session.commit()
                    print(
                        f"{client.name}: {capture_outcome.status} — "
                        f"{capture_outcome.urls_new} new, "
                        f"{capture_outcome.urls_unchanged} unchanged, "
                        f"{capture_outcome.urls_failed} failed, "
                        f"{capture_outcome.urls_blocked} blocked (of {capture_outcome.urls_total})"
                    )
                    if capture_outcome.status not in ("ok", "partial"):
                        exit_code = 1
            return exit_code
        finally:
            write_controller.stop()

    if args.command == "archive-backfill-assets":
        # Imported here, not at module scope — same convention as
        # `archive-capture` above.
        from app.core.config import get_settings
        from app.db.repositories.clients import ClientRepository
        from app.db.repositories.web_archive import WebArchiveRepository
        from app.webarchive.capture import backfill_css_assets

        if bool(args.client_id) == bool(args.all):
            print("error: pass exactly one of --client-id or --all", file=sys.stderr)
            return 2

        settings = get_settings()
        session_factory = get_sync_sessionmaker()
        with session_factory() as session:
            backfill_client_id: str | None = None
            scope_label = "all clients"
            if args.client_id:
                client = ClientRepository(session).get(args.client_id)
                if client is None:
                    print(f"error: unknown --client-id: {args.client_id}", file=sys.stderr)
                    return 2
                backfill_client_id = client.id
                scope_label = client.name

            archive_repo = WebArchiveRepository(session)
            backfill_outcome = backfill_css_assets(
                session, archive_repo, client_id=backfill_client_id, settings=settings,
                artifacts_dir=settings.artifacts_dir, dry_run=args.dry_run,
            )

            print(
                f"{scope_label}: scanned {backfill_outcome.scanned_snapshots} snapshots, "
                f"{backfill_outcome.scanned_css_files} CSS files"
            )
            for asset_kind, count in sorted(backfill_outcome.missing_by_kind.items()):
                print(f"  missing {asset_kind}: {count}")
            print(f"missing total: {backfill_outcome.missing_total}")
            if args.dry_run:
                print("dry-run: nothing fetched")
            else:
                print(f"fetched: {backfill_outcome.fetched}, failed: {backfill_outcome.failed}")
        return 0

    if args.command == "db-maintenance":
        # task-68 §1: extracted into `app.db.maintenance.run_maintenance` so
        # the scheduled version (`DbMaintenanceScheduler`, once/day) and this
        # manual CLI invocation never diverge.
        from app.db.maintenance import run_maintenance

        session_factory = get_sync_sessionmaker()
        with session_factory() as session:
            maintenance_result = run_maintenance(session)
        print("ANALYZE: done (refreshed sqlite_stat1 for the query planner)")
        state = "blocked by a live reader/writer" if maintenance_result.checkpoint_busy else "clean"
        print(
            f"wal_checkpoint(TRUNCATE): busy={maintenance_result.checkpoint_busy} "
            f"checkpointed={maintenance_result.checkpoint_pages} "
            f"total={maintenance_result.checkpoint_total_pages} pages ({state})"
        )
        print("PRAGMA optimize: done")
        return 0

    parser.error(f"unknown command: {args.command}")
    return 2



def _run_user_command(args: argparse.Namespace) -> int:
    """Операторські дії з обліковими записами (task-71).

    Локальні імпорти: решта CLI-команд не має причини тягнути auth-шар, а
    `import-ai-seo-history` взагалі працює без нього.
    """
    from app.core.auth.passwords import generate_password
    from app.core.config import get_settings
    from app.db.repositories.users import UserRepository
    from app.schemas.auth import normalize_email
    from app.schemas.enums import UserRole

    session_factory = get_sync_sessionmaker()

    with session_factory() as session:
        repo = UserRepository(session)

        if args.command == "user-list":
            users = repo.list_all()
            if not users:
                print("no accounts yet — run `yovth-report user-create` or `ensure-admin`")
                return 0
            counts = repo.active_session_counts()
            for user in users:
                state = "active" if user.is_active else "disabled"
                last = (
                    user.last_login_at.isoformat(timespec="seconds")
                    if user.last_login_at
                    else "never"
                )
                print(
                    f"{user.email:40} {user.role.value:7} {state:8} "
                    f"sessions={counts.get(user.id, 0):<3} last_login={last}"
                )
            return 0

        if args.command == "ensure-admin":
            settings = get_settings()
            if repo.count() > 0:
                print("users already exist — nothing to bootstrap")
                return 0
            if not settings.bootstrap_admin_email:
                print(
                    "no accounts and BOOTSTRAP_ADMIN_EMAIL is not set — "
                    "run `yovth-report user-create --role admin ...` manually",
                    file=sys.stderr,
                )
                return 0
            password = os.environ.get("BOOTSTRAP_ADMIN_PASSWORD") or generate_password()
            user = repo.create(
                email=settings.bootstrap_admin_email,
                name=settings.bootstrap_admin_name,
                role=UserRole.admin,
                password=password,
            )
            session.commit()
            print("=" * 72)
            print(f"bootstrap administrator created: {user.email}")
            if not os.environ.get("BOOTSTRAP_ADMIN_PASSWORD"):
                print(f"password (shown once, store it now): {password}")
            print("=" * 72)
            return 0

        if args.command == "user-create":
            try:
                email = normalize_email(args.email)
            except ValueError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 2
            if repo.get_by_email(email) is not None:
                print(f"error: {email} already has an account", file=sys.stderr)
                return 2
            password = generate_password()
            user = repo.create(
                email=email, name=args.name, role=UserRole(args.role), password=password
            )
            session.commit()
            print(f"created {user.email} ({user.role.value})")
            print(f"password (shown once, store it now): {password}")
            return 0

        existing = repo.get_by_email(args.email)
        if existing is None:
            print(f"error: no account for {args.email}", file=sys.stderr)
            return 2
        user = existing

        if args.command == "user-reset-password":
            password = generate_password()
            repo.set_password(user, password)
            revoked = repo.revoke_all_sessions(user.id)
            session.commit()
            print(f"new password for {user.email} (shown once): {password}")
            print(f"revoked {revoked} active session(s)")
            return 0

        if args.command == "user-deactivate":
            if user.role == UserRole.admin and repo.count_active_admins(excluding=user.id) == 0:
                print("error: this is the last active administrator", file=sys.stderr)
                return 2
            repo.update_profile(user, is_active=False)
            revoked = repo.revoke_all_sessions(user.id)
            session.commit()
            print(f"deactivated {user.email}, revoked {revoked} session(s)")
            return 0

    return 2

if __name__ == "__main__":
    raise SystemExit(main())


def _run_audit_command(args: argparse.Namespace) -> int:
    """Журнал дій із командного рядка (task-72) — щоб подивитись «хто що зробив»
    не піднімаючи UI, і щоб запустити прибирання руками."""
    from app.audit.retention import run_retention
    from app.db.repositories.audit import AuditRepository

    session_factory = get_sync_sessionmaker()
    with session_factory() as session:
        if args.command == "audit-retention":
            result = run_retention(session)
            print(
                f"exported={result['exported']} purged={result['purged']} "
                f"expired_sessions={result['expired_sessions']}"
            )
            return 0

        events = AuditRepository(session).query(
            limit=args.limit, action=args.action, search=args.actor
        )
        if not events:
            print("no audit events match")
            return 0
        for event in events:
            who = event.actor_email or event.actor_kind
            print(
                f"{event.ts.isoformat(timespec='seconds')}  {who:32} {event.action:26} "
                f"{event.outcome:8} {event.status_code or '-':>4}  {event.path or ''}"
            )
        return 0
