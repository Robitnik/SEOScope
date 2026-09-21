"""Відмітки й аналітика (task-100 §2.6/§2.7).

`Mark sent` — найнебезпечніша мутація тула: вона ставить 90-денний
cooldown. Тому тут перевіряється не лише щасливий шлях, а й те, що
`quota_blocked` його НЕ ставить, а `undo` знімає.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from sqlalchemy.orm import Session

from app.db.models import IndexingUrl
from app.db.repositories.indexing import IndexingRepository, median, percentile
from app.indexing.analytics import build_analytics

TODAY = date(2026, 9, 21)
NOW = datetime(2026, 9, 21, 10, 0, tzinfo=UTC)


def make_url(session: Session, client_id: str, name: str = "p") -> IndexingUrl:
    row = IndexingUrl(
        client_id=client_id,
        url=f"https://x.test/{name}",
        url_key=f"x.test/{name}",
        inspected_at=NOW,
        is_indexed=False,
        reason_code="never_indexed",
    )
    session.add(row)
    session.commit()
    return row


def plan_one(repo: IndexingRepository, url: IndexingUrl):
    row = repo.add_submission(
        client_id=url.client_id,
        url=url,
        planned_for=TODAY,
        reason_code="never_indexed",
        reason_json={"coverage_state": "URL is unknown to Google"},
    )
    repo.commit()
    return row


def test_mark_sent_is_idempotent(session: Session, client_row) -> None:
    repo = IndexingRepository(session)
    url = make_url(session, client_row.id)
    submission = plan_one(repo, url)

    assert repo.mark_sent(submission, user_id=None, now=NOW) is True
    repo.commit()
    first_time = submission.submitted_at

    assert repo.mark_sent(submission, user_id=None, now=NOW + timedelta(hours=1)) is False
    repo.commit()
    assert submission.submitted_at == first_time
    assert url.submissions_count == 1


def test_quota_blocked_does_not_arm_the_cooldown(session: Session, client_row) -> None:
    """GSC відмовляє після ~10-12 URL на добу. Якби це писалось як
    відправка, сторінка, якої ніхто не подав, зникла б на 90 днів."""
    repo = IndexingRepository(session)
    url = make_url(session, client_row.id)
    submission = plan_one(repo, url)

    assert repo.mark_quota_blocked(submission, now=NOW) is True
    repo.commit()
    assert submission.status == "quota_blocked"
    assert submission.submitted_at is None
    assert url.last_submitted_at is None
    assert url.submissions_count == 0


def test_undo_rolls_back_the_url_too(session: Session, client_row) -> None:
    repo = IndexingRepository(session)
    url = make_url(session, client_row.id)
    submission = plan_one(repo, url)
    repo.mark_sent(submission, user_id=None)
    repo.commit()

    assert repo.undo_submission(submission) is True
    repo.commit()
    assert submission.status == "planned"
    assert submission.submitted_at is None
    assert url.last_submitted_at is None
    assert url.submissions_count == 0


def test_undo_restores_the_previous_submission_time(session: Session, client_row) -> None:
    """Скасування другої відправки не має стирати слід першої."""
    repo = IndexingRepository(session)
    url = make_url(session, client_row.id)
    older = repo.add_submission(
        client_id=url.client_id,
        url=url,
        planned_for=TODAY - timedelta(days=100),
        reason_code="never_indexed",
        reason_json={},
    )
    repo.commit()
    repo.mark_sent(older, user_id=None, now=NOW - timedelta(days=100))
    repo.commit()

    newer = plan_one(repo, url)
    repo.mark_sent(newer, user_id=None, now=NOW)
    repo.commit()

    assert repo.undo_submission(newer) is True
    repo.commit()
    assert url.last_submitted_at == NOW - timedelta(days=100)
    assert url.submissions_count == 1


def test_undo_window_closes(session: Session, client_row) -> None:
    repo = IndexingRepository(session)
    url = make_url(session, client_row.id)
    submission = plan_one(repo, url)
    from app.db.base import utcnow

    repo.mark_sent(submission, user_id=None, now=utcnow() - timedelta(hours=30))
    repo.commit()
    assert repo.undo_submission(submission) is False


def test_indexing_closes_the_open_observation(session: Session, client_row) -> None:
    repo = IndexingRepository(session)
    url = make_url(session, client_row.id)
    submission = plan_one(repo, url)
    repo.mark_sent(submission, user_id=None, now=NOW)
    repo.commit()

    assert repo.close_indexed_submissions(url, indexed_at=NOW + timedelta(days=6)) == 1
    repo.commit()
    assert submission.days_to_index == 6


def test_control_group_excludes_pages_already_indexed_on_arrival(
    session: Session, client_row
) -> None:
    """Живо це намалювало медіану 0.0d на 27 сторінках — «сторінки заходять
    миттєво самі» — і на цій підставі тул сказав би, що ручна робота марна.

    Сторінка, яка вже була в індексі, коли планер її вперше побачив, не є
    спостереженням: ми не бачили переходу.
    """
    repo = IndexingRepository(session)
    already = IndexingUrl(
        client_id=client_row.id,
        url="https://x.test/already",
        url_key="x.test/already",
        first_seen_at=NOW - timedelta(days=5),
        inspected_at=NOW,
        is_indexed=True,
        first_indexed_seen_at=NOW - timedelta(days=5),
        first_not_indexed_at=None,
    )
    observed = IndexingUrl(
        client_id=client_row.id,
        url="https://x.test/observed",
        url_key="x.test/observed",
        first_seen_at=NOW - timedelta(days=20),
        inspected_at=NOW,
        is_indexed=True,
        first_not_indexed_at=NOW - timedelta(days=20),
        first_indexed_seen_at=NOW - timedelta(days=6),
    )
    session.add_all([already, observed])
    session.commit()

    samples = repo.observed_transition_samples(
        since=NOW - timedelta(days=180), client_ids=[client_row.id], submitted=False
    )
    assert samples == [14]


def test_analytics_reports_none_not_zero_on_empty_samples(
    session: Session, client_row
) -> None:
    report = build_analytics(session, window_days=180)
    assert report.lag.median_days is None
    assert report.control_group.difference_days is None
    assert report.funnel.sent == 0


def test_median_and_percentile_distinguish_empty_from_zero() -> None:
    """Нуль днів читався б як «заходить того ж дня» — найкращий можливий
    результат, тоді як насправді спостережень немає."""
    assert median([]) is None
    assert percentile([], 0.25) is None
    assert median([1, 2, 3, 4]) == 2.5
    assert median([0, 0]) == 0
