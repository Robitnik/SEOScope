"""Планування днів (task-100 §2.3, крок 4).

Три інваріанти, кожен уже ламався або мав ціну помилки:
заморожене сьогодні, cooldown як фільтр, порожній день як норма.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from app.db.models import IndexingUrl
from app.db.repositories.indexing import IndexingRepository
from app.indexing.plan import ensure_today, forecast, plan_client

TODAY = date(2026, 9, 21)
NOW = datetime(2026, 9, 21, 9, 0, tzinfo=UTC)


def seed_candidates(session: Session, client_id: str, count: int) -> list[IndexingUrl]:
    rows = []
    for index in range(count):
        row = IndexingUrl(
            client_id=client_id,
            url=f"https://x.test/p{index}",
            url_key=f"x.test/p{index}",
            inspected_at=NOW,
            is_indexed=False,
            reason_code="never_indexed",
            reason_json={"coverage_state": "URL is unknown to Google"},
            priority_score=Decimal(90 - index),
        )
        session.add(row)
        rows.append(row)
    session.commit()
    return rows


def test_today_is_frozen_across_replans(session: Session, client_row) -> None:
    """Нічний переплан не має витягати з-під оператора рядок, який він уже
    почав нести в GSC."""
    repo = IndexingRepository(session)
    settings = repo.get_or_create_settings(client_row.id)
    settings.enabled = True
    settings.daily_quota = 2
    seed_candidates(session, client_row.id, 6)

    ensure_today(repo, settings, today=TODAY, now=NOW)
    session.commit()
    before = [row.indexing_url_id for row in repo.submissions_for_day(TODAY)]
    assert len(before) == 2

    plan_client(repo, settings, today=TODAY, now=NOW)
    session.commit()
    plan_client(repo, settings, today=TODAY, now=NOW)
    session.commit()

    after = [row.indexing_url_id for row in repo.submissions_for_day(TODAY)]
    assert after == before


def test_replan_rebuilds_future_but_keeps_sent_rows(session: Session, client_row) -> None:
    repo = IndexingRepository(session)
    settings = repo.get_or_create_settings(client_row.id)
    settings.enabled = True
    settings.daily_quota = 1
    rows = seed_candidates(session, client_row.id, 4)

    plan_client(repo, settings, today=TODAY, now=NOW)
    session.commit()
    tomorrow = TODAY + timedelta(days=1)
    planned = repo.submissions_for_day(tomorrow)
    assert len(planned) == 1

    # Відмічена як відправлена клітинка в майбутньому не стирається.
    repo.mark_sent(planned[0], user_id=None, now=NOW)
    session.commit()
    plan_client(repo, settings, today=TODAY, now=NOW)
    session.commit()
    kept = repo.submissions_for_day(tomorrow)
    assert [row.status for row in kept] == ["sent"]
    assert rows  # seeded


def test_cooldown_filters_regardless_of_score(session: Session, client_row) -> None:
    """URL у cooldown не з'являється в плані за жодного балу: повторна
    відправка раніше нічого не дає, а слот витрачає."""
    repo = IndexingRepository(session)
    settings = repo.get_or_create_settings(client_row.id)
    settings.enabled = True
    settings.daily_quota = 5
    settings.resubmit_after_days = 90
    rows = seed_candidates(session, client_row.id, 2)
    rows[0].priority_score = Decimal(1000)
    rows[0].last_submitted_at = NOW - timedelta(days=10)
    session.commit()

    ensure_today(repo, settings, today=TODAY, now=NOW)
    session.commit()
    planned = {row.indexing_url_id for row in repo.submissions_for_day(TODAY)}
    assert rows[0].id not in planned
    assert rows[1].id in planned


def test_cooldown_expires(session: Session, client_row) -> None:
    repo = IndexingRepository(session)
    settings = repo.get_or_create_settings(client_row.id)
    settings.enabled = True
    settings.resubmit_after_days = 90
    rows = seed_candidates(session, client_row.id, 1)
    rows[0].last_submitted_at = NOW - timedelta(days=91)
    session.commit()

    ensure_today(repo, settings, today=TODAY, now=NOW)
    session.commit()
    assert len(repo.submissions_for_day(TODAY)) == 1


def test_empty_queue_is_not_an_error(session: Session, client_row) -> None:
    """Сім із десяти клієнтів мають беклог <= 3 URL, тож порожній день —
    типовий робочий стан."""
    repo = IndexingRepository(session)
    settings = repo.get_or_create_settings(client_row.id)
    settings.enabled = True

    created = ensure_today(repo, settings, today=TODAY, now=NOW)
    result = plan_client(repo, settings, today=TODAY, now=NOW)
    session.commit()
    assert created == 0
    assert result.created == 0
    assert result.queue_exhausted is True


def test_uninspected_urls_never_enter_the_plan(session: Session, client_row) -> None:
    repo = IndexingRepository(session)
    settings = repo.get_or_create_settings(client_row.id)
    settings.enabled = True
    session.add(
        IndexingUrl(
            client_id=client_row.id,
            url="https://x.test/unknown",
            url_key="x.test/unknown",
            inspected_at=None,
            is_indexed=None,
        )
    )
    session.commit()

    ensure_today(repo, settings, today=TODAY, now=NOW)
    session.commit()
    assert repo.submissions_for_day(TODAY) == []


def test_forecast_distinguishes_empty_from_unknown() -> None:
    assert forecast(queue_size=0, daily_capacity=10, cycle_days=90) == (None, True)
    assert forecast(queue_size=50, daily_capacity=0, cycle_days=90) == (None, None)
    assert forecast(queue_size=274, daily_capacity=100, cycle_days=90) == (3, True)
    assert forecast(queue_size=3182, daily_capacity=30, cycle_days=90) == (107, False)


def test_stale_scan_is_reclaimed_not_left_running(session: Session, client_row) -> None:
    """Обірваний прогін (kill, рестарт, таймаут CLI) не має лишатись
    `running` назавжди: він блокує наступний портфельний скан і показує
    оператору вічне «scanning…». Живо це сталось на першому ж довгому
    клієнті."""
    from datetime import timedelta

    from app.db.base import utcnow

    repo = IndexingRepository(session)
    scan = repo.start_scan(client_id=client_row.id, trigger="manual")
    scan.started_at = utcnow() - timedelta(hours=3)
    scan.heartbeat_at = utcnow() - timedelta(hours=3)
    repo.commit()

    assert repo.reclaim_stale_scans(stale_after=timedelta(minutes=30)) == 1
    repo.commit()
    assert scan.status == "failed"
    assert scan.error_code == "interrupted"
    assert scan.finished_at is not None


def test_live_scan_is_not_reclaimed(session: Session, client_row) -> None:
    from datetime import timedelta

    repo = IndexingRepository(session)
    repo.start_scan(client_id=client_row.id, trigger="manual")
    repo.commit()
    assert repo.reclaim_stale_scans(stale_after=timedelta(minutes=30)) == 0
