"""task-86 — one-off cross-source proof for SAPHNY, August 2026.

Collects the two sources that can run headlessly, inside this container, with
no browser: the direct Google API (task-83's `Ga4Client`/`GscClient`) and our
own database (`analytics_dataset_rows`). The browser-dependent sources (GSC
UI, GA4 UI, Reporting Ninja PDF/API) are gathered separately via AISeoTools'
own CLI on the host and documented directly in
doc/verification/saphny-2026-08/REPORT.md — they cannot run inside this
container (no Chrome here by design; see compose.yaml).

Read-only: no `INSERT`/`UPDATE`/`DELETE`, no migration. Run via:
    docker compose exec api poetry run python scripts/verify_saphny_sources.py
"""

from __future__ import annotations

import asyncio
import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.core.config import get_settings
from app.db.models import AnalyticsDatasetRow, Client
from app.db.session import get_async_sessionmaker
from app.integrations.google_api import Ga4Client, Ga4Row, GoogleApiSession, GscClient
from app.integrations.google_api.auth import GoogleAuth

CLIENT_NAME = "SAPHNY"
GSC_PROPERTY = "sc-domain:nysapphire.com"
GA4_PROPERTY = "properties/428527008"
PERIOD_START = date(2026, 8, 1)
PERIOD_END = date(2026, 8, 31)

_CHANNEL_DIMENSION = "firstUserDefaultChannelGroup"
_USERS_METRIC = "totalUsers"
_CONVERSIONS_METRIC = "keyEvents"
_ORGANIC_SEARCH = "organic search"
_DIRECT = "direct"

OUT_DIR = Path(__file__).resolve().parent.parent / "doc/verification/saphny-2026-08/raw"


def _decimal_to_int(value: Decimal | None) -> int | None:
    if value is None:
        return None
    return int(value)


def _sum_exact_channel(rows: list[Ga4Row], target_channel: str) -> tuple[int | None, int | None]:
    matched = [
        row
        for row in rows
        if str(row.dimensions.get(_CHANNEL_DIMENSION, "")).strip().lower() == target_channel
    ]
    if not matched:
        return None, None
    users = sum(_decimal_to_int(row.metrics.get(_USERS_METRIC)) or 0 for row in matched)
    conversions = sum(_decimal_to_int(row.metrics.get(_CONVERSIONS_METRIC)) or 0 for row in matched)
    return users, conversions


def fetch_google_api_direct() -> dict[str, Any]:
    """Source #3 — `Ga4Client`/`GscClient` called directly, bypassing both
    Reporting Ninja and our own report engine. Same attribution model as
    `app/reports/google_source.py` (first-user channel group, exact
    "Organic Search"/"Direct" match) — that model is a property of the
    business question, not of the transport, so re-deriving it independently
    here would manufacture a fake discrepancy, not find a real one.
    """
    settings = get_settings()
    auth = GoogleAuth(
        client_id=settings.google_api_client_id or "",
        client_secret=settings.google_api_secret or "",
        refresh_token=settings.google_api_refresh_token or "",
        timeout_seconds=settings.google_api_timeout_seconds,
    )
    with GoogleApiSession(auth, timeout_seconds=settings.google_api_timeout_seconds) as session:
        gsc = GscClient(session)
        gsc_rows = gsc.daily_search_analytics(GSC_PROPERTY, PERIOD_START, PERIOD_END)
        clicks = sum(_decimal_to_int(r.clicks) or 0 for r in gsc_rows)
        impressions = sum(_decimal_to_int(r.impressions) or 0 for r in gsc_rows)

        ga4 = Ga4Client(session)
        channel_rows = ga4.run_report(
            GA4_PROPERTY,
            PERIOD_START,
            PERIOD_END,
            dimensions=[_CHANNEL_DIMENSION],
            metrics=[_USERS_METRIC, _CONVERSIONS_METRIC],
        )
        organic_users, organic_conversions = _sum_exact_channel(channel_rows, _ORGANIC_SEARCH)
        direct_users, direct_conversions = _sum_exact_channel(channel_rows, _DIRECT)

    return {
        "source": "google_api_direct",
        "gsc_days": len(gsc_rows),
        "clicks": clicks,
        "impressions": impressions,
        "organic_users": organic_users,
        "organic_conversions": organic_conversions,
        "direct_users": direct_users,
        "direct_conversions": direct_conversions,
    }


async def fetch_db_dataset() -> dict[str, Any]:
    """Source #8 — our own `analytics_dataset_rows`, newest row per day.

    Multiple collection runs can each leave a row for the same day (the
    unique constraint is on `(snapshot_id, dataset_key, dimension_value)`,
    not on the day alone) — this is exactly what the task's own §0.4 hypothesis
    used ("найновіший рядок на день"), so the newest `created_at` per day is
    kept and older duplicates are discarded before summing.
    """
    factory = get_async_sessionmaker()
    async with factory() as s:
        client_row = (
            await s.execute(select(Client.id).where(Client.name == CLIENT_NAME))
        ).first()
        if client_row is None:
            raise SystemExit(f"client {CLIENT_NAME!r} not found in DB")
        client_id = client_row[0]

        day_values = [f"2026-08-{d:02d}" for d in range(1, 32)]

        async def newest_per_day(dataset_key: str) -> dict[str, dict[str, str]]:
            res = await s.execute(
                select(
                    AnalyticsDatasetRow.dimension_value,
                    AnalyticsDatasetRow.metrics_json,
                    AnalyticsDatasetRow.created_at,
                ).where(
                    AnalyticsDatasetRow.client_id == client_id,
                    AnalyticsDatasetRow.dataset_key == dataset_key,
                    AnalyticsDatasetRow.dimension_value.in_(day_values),
                )
            )
            newest: dict[str, tuple[Any, Any]] = {}
            for dimension_value, metrics_json, created_at in res.all():
                prior = newest.get(dimension_value)
                if prior is None or created_at > prior[1]:
                    newest[dimension_value] = (metrics_json, created_at)
            return {day: metrics for day, (metrics, _) in newest.items()}

        gsc_by_day = await newest_per_day("gsc_daily")
        ga4ch_by_day = await newest_per_day("ga4_daily_channels")

    missing_gsc_days = sorted(set(day_values) - set(gsc_by_day))
    missing_ga4_days = sorted(set(day_values) - set(ga4ch_by_day))

    clicks = sum(int(m.get("clicks") or 0) for m in gsc_by_day.values())
    impressions = sum(int(m.get("impressions") or 0) for m in gsc_by_day.values())
    conversions_organic = sum(int(m.get("conversions_organic") or 0) for m in ga4ch_by_day.values())
    conversions_direct = sum(int(m.get("conversions_direct") or 0) for m in ga4ch_by_day.values())

    return {
        "source": "our_db_analytics_dataset_rows",
        "client_id": str(client_id),
        "days_gsc": len(gsc_by_day),
        "days_ga4_channels": len(ga4ch_by_day),
        "missing_gsc_days": missing_gsc_days,
        "missing_ga4_channels_days": missing_ga4_days,
        "clicks": clicks,
        "impressions": impressions,
        "conversions_organic": conversions_organic,
        "conversions_direct": conversions_direct,
        "note": "organic_users is not carried by ga4_daily_channels rows — "
        "this dataset only ever stored conversions_organic/conversions_direct, "
        "never a users split (see REPORT.md finding).",
    }


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    google_api = fetch_google_api_direct()
    (OUT_DIR / "03-google-api-direct.json").write_text(
        json.dumps(google_api, indent=2, default=str), encoding="utf-8"
    )
    print("google_api_direct:", google_api)

    db_dataset = asyncio.run(fetch_db_dataset())
    (OUT_DIR / "08-db-analytics-dataset-rows.json").write_text(
        json.dumps(db_dataset, indent=2, default=str), encoding="utf-8"
    )
    print("db_dataset:", db_dataset)


if __name__ == "__main__":
    main()
