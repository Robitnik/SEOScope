"""Daily composite-health history (`client_health_daily`).

The composite score is derived on read and used to be thrown away, so a card
could show today's number but neither "which way is this moving" nor a series
to chart. These cover the store behind the card's week-over-week arrow and its
daily sparkline — in particular that a gap stays a gap and never becomes a 0.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.db.models import Client
from app.db.repositories.health import HealthHistoryRepository


def _client(session, slug: str = "acme") -> str:
    client = Client(name=slug, slug=slug, domain=f"{slug}.com", timezone="America/New_York")
    session.add(client)
    session.flush()
    return client.id


def test_record_is_an_upsert_per_day(session):
    """A day's score summarises that day's best-known data, so a second
    collection the same day supersedes it rather than appending a duplicate.
    """
    client_id = _client(session)
    day = date(2026, 7, 20)
    repo = HealthHistoryRepository(session)

    repo.record(client_id, day, Decimal(70))
    repo.record(client_id, day, Decimal(85))

    points = repo.history(client_id, since=date(2026, 7, 1), until=date(2026, 7, 31))
    assert [(p.day, p.value) for p in points] == [(day, Decimal(85))]


def test_history_is_ordered_oldest_first_and_bounded(session):
    client_id = _client(session)
    repo = HealthHistoryRepository(session)
    for day, value in [(date(2026, 7, 10), 50), (date(2026, 7, 12), 60), (date(2026, 7, 18), 70)]:
        repo.record(client_id, day, Decimal(value))

    points = repo.history(client_id, since=date(2026, 7, 11), until=date(2026, 7, 17))

    assert [p.day for p in points] == [date(2026, 7, 12)]


def test_missing_days_stay_absent_rather_than_zero(session):
    """PLAN §12: a day with no contributing service has no health. The series
    must show a gap the UI can skip, never a 0 it would chart as a crash.
    """
    client_id = _client(session)
    repo = HealthHistoryRepository(session)
    repo.record(client_id, date(2026, 7, 10), Decimal(50))
    repo.record(client_id, date(2026, 7, 14), Decimal(52))

    points = repo.history(client_id, since=date(2026, 7, 9), until=date(2026, 7, 15))

    assert [p.day for p in points] == [date(2026, 7, 10), date(2026, 7, 14)]
    assert all(p.value != 0 for p in points)


def test_value_on_or_before_falls_back_to_the_nearest_earlier_point(session):
    """Collection is not guaranteed daily, so asking for exactly D-7 would
    usually find nothing and silently suppress the trend arrow.
    """
    client_id = _client(session)
    repo = HealthHistoryRepository(session)
    repo.record(client_id, date(2026, 7, 10), Decimal(64))

    found = repo.value_on_or_before(client_id, date(2026, 7, 15))

    assert found is not None
    assert found.value == Decimal(64)


def test_value_on_or_before_never_looks_into_the_future(session):
    client_id = _client(session)
    repo = HealthHistoryRepository(session)
    repo.record(client_id, date(2026, 7, 20), Decimal(90))

    assert repo.value_on_or_before(client_id, date(2026, 7, 10)) is None


def test_history_is_per_client(session):
    a = _client(session, "alpha")
    b = _client(session, "beta")
    repo = HealthHistoryRepository(session)
    repo.record(a, date(2026, 7, 10), Decimal(40))
    repo.record(b, date(2026, 7, 10), Decimal(90))

    points = repo.history(a, since=date(2026, 7, 1), until=date(2026, 7, 31))

    assert [p.value for p in points] == [Decimal(40)]
