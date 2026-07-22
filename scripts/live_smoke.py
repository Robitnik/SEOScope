#!/usr/bin/env python
"""Live smoke: один контрольний клієнт на провайдера (PLAN §15 «Live smoke»).

**Ніколи не запускається в CI.** Це справжній збір: витрачаються Semrush/
Ahrefs API units, чіпаються автентифіковані browser-профілі, а провайдер
може відповісти капчею. Тому:

- потрібен явний прапорець `--yes-spend-units`;
- перед збором завжди йде `preflight()`, і без `--collect` далі нічого не
  відбувається (сухий режим за замовчуванням);
- browser-профіль береться під той самий lock, що й у worker-а
  (`profile_key`), тож два профілі одного провайдера паралельно не стартують;
- на кожен провайдер збирається **рівно один** клієнт.

Отримані totals треба **вручну** звірити з UI провайдера перед release
(DoD PLAN §18). Скрипт друкує їх у вигляді таблиці й, за бажанням, зберігає
JSON для порівняння з попереднім прогоном.

    docker compose exec api poetry run python scripts/live_smoke.py --list
    docker compose exec api poetry run python scripts/live_smoke.py \
        --provider semrush --client acme.com --yes-spend-units
    docker compose exec api poetry run python scripts/live_smoke.py \
        --provider semrush --client acme.com --yes-spend-units --collect \
        --unit-budget 50 --report /app/app_data/live-smoke.json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path


def _load_client(session, needle: str):
    from sqlalchemy import or_, select

    from app.db.models import Client

    return session.scalars(
        select(Client).where(
            or_(Client.domain == needle, Client.slug == needle, Client.id == needle)
        )
    ).first()


def _print_table(rows: list[tuple[str, str, str]]) -> None:
    if not rows:
        print("  (жодного значення — це теж результат: дивись quality/warnings вище)")
        return
    width = max(len(row[0]) for row in rows)
    for key, value, quality in rows:
        print(f"  {key.ljust(width)}  {value:>14}  {quality}")


def main(argv: list[str] | None = None) -> int:  # noqa: PLR0911 - лінійний сценарій із гардами
    import app.integrations  # noqa: F401 - реєструє справжні адаптери
    from app.core.config import get_settings
    from app.db.models import MetricValue, ProviderSnapshot
    from app.db.repositories.cells import CellRepository
    from app.db.repositories.jobs import JobRepository
    from app.db.session import worker_session_scope
    from app.integrations import registry
    from app.integrations.registry import get_adapter
    from app.jobs.runner import CellHandle, run_cell
    from app.metrics import resolve_periods
    from app.schemas.adapter import CollectionContext
    from app.schemas.enums import CellStatus, ComparisonMode, PeriodKind, Provider

    parser = argparse.ArgumentParser(prog="live_smoke")
    parser.add_argument("--provider", choices=[p.value for p in Provider])
    parser.add_argument("--client", help="domain, slug або id контрольного клієнта")
    parser.add_argument("--period", choices=["week", "month"], default="week")
    parser.add_argument("--anchor", type=date.fromisoformat, default=None)
    parser.add_argument("--unit-budget", type=int, default=None, help="жорсткий ліміт API units")
    parser.add_argument("--collect", action="store_true", help="після preflight реально зібрати")
    parser.add_argument("--yes-spend-units", action="store_true")
    parser.add_argument("--report", type=Path, default=None, help="куди дописати JSON-звіт")
    parser.add_argument("--list", action="store_true", help="показати клієнтів і вийти")
    args = parser.parse_args(argv)

    if args.list:
        with worker_session_scope() as session:
            from sqlalchemy import select

            from app.db.models import Client, ClientIntegration

            for client in session.scalars(select(Client).order_by(Client.name)):
                providers = sorted(
                    i.provider.value
                    for i in session.scalars(
                        select(ClientIntegration).where(
                            ClientIntegration.client_id == client.id,
                            ClientIntegration.enabled.is_(True),
                        )
                    )
                )
                print(f"{client.domain:<40} {client.name:<28} {', '.join(providers) or '—'}")
        return 0

    if not args.provider or not args.client:
        parser.error("потрібні --provider і --client (або --list)")
    if not args.yes_spend_units:
        print(
            "відмова: live smoke витрачає API units і чіпає справжні сесії.\n"
            "Додай --yes-spend-units, коли справді цього хочеш.",
            file=sys.stderr,
        )
        return 2

    provider = Provider(args.provider)
    registered = registry.available_providers()
    if provider not in registered:
        print(f"адаптер {provider.value} не зареєстрований: {registered}", file=sys.stderr)
        return 2

    settings = get_settings()
    anchor = args.anchor or datetime.now(UTC).date()

    with worker_session_scope() as session:
        client = _load_client(session, args.client)
        if client is None:
            print(f"клієнта не знайдено: {args.client!r}", file=sys.stderr)
            return 2
        client_id, client_name, timezone = client.id, client.name, client.timezone

        integration = None
        from sqlalchemy import select

        from app.db.models import ClientIntegration, SyncJobProvider

        integration = session.scalars(
            select(ClientIntegration).where(
                ClientIntegration.client_id == client_id,
                ClientIntegration.provider == provider,
            )
        ).first()
        if integration is None or not integration.enabled:
            print(f"{client_name}: інтеграція {provider.value} не налаштована", file=sys.stderr)
            return 2

        config = dict(integration.config_json or {})
        if args.unit_budget is not None:
            config["unit_budget"] = args.unit_budget
        source_mode = integration.source_mode

        # Той самий lock, що й у worker-а: два browser-профілі одного
        # провайдера паралельно псують сесію (CLAUDE.md, PLAN §9.1).
        profile_key = JobRepository(session)._profile_key(client_id, provider)  # noqa: SLF001
        if profile_key is not None:
            holder = session.scalars(
                select(SyncJobProvider).where(
                    SyncJobProvider.profile_key == profile_key,
                    SyncJobProvider.status == CellStatus.running,
                )
            ).first()
            if holder is not None:
                print(
                    f"відмова: профіль {profile_key} зайнятий клітинкою {holder.id} "
                    "(зупини worker або дочекайся завершення)",
                    file=sys.stderr,
                )
                return 3

    anchor_dt = datetime(anchor.year, anchor.month, anchor.day, 12, tzinfo=UTC)
    periods = resolve_periods(
        PeriodKind(args.period), anchor_dt, tz=timezone, mode=ComparisonMode.closed
    )
    context = CollectionContext(
        client_id=client_id,
        provider=provider,
        source_mode=source_mode,
        period_kind=PeriodKind(args.period),
        comparison_mode=ComparisonMode.closed,
        current=periods.current,
        previous=periods.previous,
        timezone=timezone,
        force_refresh=True,
        integration_config=config,
    )

    print(f"клієнт:    {client_name} ({client_id})")
    print(f"провайдер: {provider.value}   період: {periods.current.start} … {periods.current.end}")
    print(f"units:     {config.get('unit_budget', 'без явного ліміту')}")

    preflight = get_adapter(provider).preflight(context)
    print(f"preflight: ok={preflight.ok} code={preflight.reason_code} {preflight.message or ''}")
    if not preflight.ok:
        return 4
    if not args.collect:
        print("сухий режим: додай --collect, щоб реально зібрати дані")
        return 0

    with worker_session_scope() as session:
        job = JobRepository(session).create_run(
            client_ids=[client_id],
            providers=[provider],
            period_kind=PeriodKind(args.period),
            comparison_mode=ComparisonMode.closed,
            anchor_date=anchor,
            mode="force_refresh",
        )[0]
        cell = CellRepository(session).list_for_job(job.id)[0]
        job_id, cell_id = job.id, cell.id

    # Через `run_cell`, а не напряму через adapter.collect: так live-прогін
    # проходить рівно ті самі guard/persist кроки, що й прод.
    with worker_session_scope() as session:
        CellRepository(session).claim_next_cell(
            provider, lease_owner="live-smoke", lease_ttl_seconds=settings.job_lease_seconds
        )
    status = run_cell(
        CellHandle(cell_id=cell_id, job_id=job_id, client_id=client_id, provider=provider),
        session_scope=worker_session_scope,
        lease_owner="live-smoke",
        lease_ttl_seconds=settings.job_lease_seconds,
    )
    print(f"статус клітинки: {status.value}")

    with worker_session_scope() as session:
        from sqlalchemy import select

        snapshot = session.scalars(
            select(ProviderSnapshot)
            .where(ProviderSnapshot.job_id == job_id)
            .order_by(ProviderSnapshot.created_at.desc())
        ).first()
        if snapshot is None:
            print("snapshot не створено — звіряти нічого")
            return 5
        values = list(
            session.scalars(select(MetricValue).where(MetricValue.snapshot_id == snapshot.id))
        )
        payload = {
            "checked_at": datetime.now(UTC).isoformat(),
            "client": client_name,
            "provider": provider.value,
            "period": [periods.current.start.isoformat(), periods.current.end.isoformat()],
            "quality": snapshot.quality.value,
            "data_through": snapshot.data_through.isoformat() if snapshot.data_through else None,
            "warnings": list(snapshot.warnings_json or []),
            "metrics": {
                f"{v.metric_key}:{v.period_role.value}": (
                    str(v.value_numeric) if v.value_numeric is not None else None
                )
                for v in values
            },
        }
        rows = [
            (
                f"{v.metric_key} ({v.period_role.value})",
                "—" if v.value_numeric is None else str(v.value_numeric),
                v.quality.value,
            )
            for v in sorted(values, key=lambda m: (m.metric_key, m.period_role.value))
        ]

    print(f"quality: {payload['quality']}  data_through: {payload['data_through']}")
    for warning in payload["warnings"]:
        print(f"  warning: {warning}")
    _print_table(rows)
    print("\nЗвір ці значення вручну з UI провайдера перед release (PLAN §18).")

    if args.report is not None:
        history = []
        if args.report.is_file():
            history = json.loads(args.report.read_text(encoding="utf-8"))
        history.append(payload)
        args.report.write_text(
            json.dumps(history, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(f"звіт дописано -> {args.report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
