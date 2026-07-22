"""Card health trend: the week-over-week arrow and the daily sparkline.

Both blocks above the Ahrefs section read `ClientCardDTO.health` straight from
the dashboard payload, so these lock in the contract they depend on — most of
all that "no basis for comparison" stays `None` and never becomes a 0% that
would render as "flat" when the truth is "unknown".
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from app.db.repositories.clients import ClientRepository
from app.db.repositories.health import HealthHistoryRepository
from app.schemas.enums import Provider, SourceMode


def _seed_client(db, *, slug: str = "trend") -> str:
    clients = ClientRepository(db)
    client = clients.create(
        name="Trend Co", slug=slug, domain=f"{slug}.com", timezone="America/New_York"
    )
    clients.upsert_integration(client.id, Provider.gsc, enabled=True, source_mode=SourceMode.api)
    return client.id


def _health_of(payload, client_id: str):
    return next(c for c in payload["clients"] if c["client_id"] == client_id)["health"]


def test_history_and_delta_are_exposed_on_the_card(client, db):
    client_id = _seed_client(db)
    today = date.today()
    repo = HealthHistoryRepository(db)
    repo.record(client_id, today - timedelta(days=8), Decimal(50))
    repo.record(client_id, today - timedelta(days=1), Decimal(60))
    db.commit()

    health = _health_of(client.get("/api/v1/dashboard").json(), client_id)

    days = [point["day"] for point in health["history"]]
    assert days == sorted(days), "series must be oldest-first for the sparkline"
    assert (today - timedelta(days=8)).isoformat() in days
    # Compared against the nearest point at or before D-7, i.e. the 50.
    assert health["value_previous"] == "50"


def test_no_comparison_basis_yields_none_not_zero_percent(client, db):
    """A client with no earlier point has an unknown trend, not a flat one."""
    client_id = _seed_client(db)
    db.commit()

    health = _health_of(client.get("/api/v1/dashboard").json(), client_id)

    assert health["delta_pct"] is None
    assert health["value_previous"] is None


def test_a_day_with_no_contributing_service_is_never_recorded_as_zero(client, db):
    """The seeded client has an integration but no snapshot, so `compute_health`
    returns `None`. That must leave the series empty rather than writing a 0
    the sparkline would chart as a crash (PLAN §12).
    """
    client_id = _seed_client(db)
    db.commit()

    health = _health_of(client.get("/api/v1/dashboard").json(), client_id)

    assert health["value"] is None
    assert health["history"] == []
