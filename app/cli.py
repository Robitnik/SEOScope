"""CLI entrypoint (task-07): `poetry run yovth-report <command>`.

Registered in `pyproject.toml` as `[tool.poetry.scripts] yovth-report =
"app.cli:main"`.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime, timedelta
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

    if args.command == "monitor-run":
        # Imported here, not at module scope: pulls in the monitor engine
        # (fetch/parse/diff), which the other commands have no reason to load.
        from app.core.config import get_settings
        from app.db.repositories.clients import ClientRepository
        from app.monitor.runner import run_client_monitor

        if bool(args.client_id) == bool(args.all):
            print("error: pass exactly one of --client-id or --all", file=sys.stderr)
            return 2

        settings = get_settings()
        session_factory = get_sync_sessionmaker()
        with session_factory() as session:
            clients_repo = ClientRepository(session)
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
                outcome = run_client_monitor(
                    session,
                    client,
                    kind=args.kind,
                    settings=settings,
                    artifacts_dir=settings.artifacts_dir,
                    trigger="manual",
                )
                print(
                    f"{client.name}: {outcome.status} — checked {outcome.urls_checked} url(s)"
                    f"{' (truncated)' if outcome.truncated else ''}"
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

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
