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
                        monitor_repo.get_settings(client.id),
                        now=datetime.now(UTC),
                        sticky_days=settings.monitor_antibot_sticky_days,
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
        from app.core.config import get_settings
        from app.db.repositories.monitor import MonitorRepository
        from app.monitor.storage import delete_blob_file

        settings = get_settings()
        session_factory = get_sync_sessionmaker()
        with session_factory() as session:
            repo = MonitorRepository(session)

            targets = {b.sha256 for b in repo.unreferenced_blobs()}
            if args.bodies_older_than is not None:
                cutoff = datetime.now(UTC) - timedelta(days=args.bodies_older_than)
                aging_out = repo.blobs_referenced_only_by_versions_older_than(cutoff)
                print(
                    f"{len(aging_out)} version(s)' bodies are older than "
                    f"{args.bodies_older_than} day(s)"
                )
                targets |= set(aging_out)

            if not targets:
                print("nothing to prune")
                return 0

            print(f"{'would remove' if args.dry_run else 'removing'} {len(targets)} blob(s)")
            if args.dry_run:
                return 0

            for sha256 in sorted(targets):
                delete_blob_file(settings.artifacts_dir, sha256)
                repo.delete_blob(sha256)
            session.commit()
        print(f"pruned {len(targets)} blob(s)")
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

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
