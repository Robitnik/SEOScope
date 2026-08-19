"""`MonitorRedirect` persistence (task-51 §3.1): upsert bumps `version_seq`

only on a real fingerprint change, `first_seen_window_start` is set on
creation and reset again on reappearance (never on an in-between update to an
already-active rule), and removal/reappearance round-trips `removed_at`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.repositories.clients import ClientRepository
from app.db.repositories.monitor import MonitorRepository

T0 = datetime(2026, 8, 19, 14, 0, tzinfo=UTC)


def _client(session: Session, *, slug: str = "acme") -> str:
    client = ClientRepository(session).create(
        name="Acme", slug=slug, domain=f"{slug}.com", timezone="America/New_York"
    )
    session.commit()
    return client.id


def _upsert(repo: MonitorRepository, client_id: str, key: str = "1487", **overrides: object):
    fields = dict(
        source_url="/old-page/",
        target_url="/new-page/",
        http_code=301,
        match_type="url",
        regex=False,
        status="enabled",
        group_name="Redirections",
        title=None,
        external_id=1487,
        rule_sha256="a" * 64,
        now=T0,
    )
    fields.update(overrides)
    return repo.upsert_redirect_state(client_id, "redirection", key, **fields)  # type: ignore[arg-type]


def test_first_upsert_creates_the_row_with_first_seen_fields(session: Session) -> None:
    client_id = _client(session)
    repo = MonitorRepository(session)
    rule = _upsert(repo, client_id, window_start=None)
    assert rule.first_seen_at == T0
    assert rule.first_seen_window_start is None
    assert rule.last_seen_at == T0
    assert rule.removed_at is None
    assert rule.version_seq == 0


def test_window_start_is_recorded_on_creation(session: Session) -> None:
    client_id = _client(session)
    repo = MonitorRepository(session)
    previous_check = T0 - timedelta(hours=1)
    rule = _upsert(repo, client_id, window_start=previous_check)
    assert rule.first_seen_window_start == previous_check


def test_reupsert_with_same_fingerprint_does_not_bump_version(session: Session) -> None:
    client_id = _client(session)
    repo = MonitorRepository(session)
    _upsert(repo, client_id, rule_sha256="a" * 64, now=T0)
    later = T0 + timedelta(hours=1)
    rule = _upsert(repo, client_id, rule_sha256="a" * 64, now=later)
    assert rule.version_seq == 0
    assert rule.last_seen_at == later


def test_reupsert_with_different_fingerprint_bumps_version(session: Session) -> None:
    client_id = _client(session)
    repo = MonitorRepository(session)
    _upsert(repo, client_id, rule_sha256="a" * 64)
    rule = _upsert(repo, client_id, target_url="/another/", rule_sha256="b" * 64)
    assert rule.version_seq == 1
    assert rule.target_url == "/another/"


def test_window_start_is_never_overwritten_by_a_later_upsert(session: Session) -> None:
    client_id = _client(session)
    repo = MonitorRepository(session)
    first_window = T0 - timedelta(hours=1)
    _upsert(repo, client_id, window_start=first_window)
    # A later upsert call passing a DIFFERENT window_start must not matter —
    # only the very first insert sets it (repository contract, task-51).
    rule = _upsert(repo, client_id, target_url="/x/", rule_sha256="c" * 64, window_start=T0)
    assert rule.first_seen_window_start == first_window


def test_get_redirect_returns_none_for_unknown_key(session: Session) -> None:
    client_id = _client(session)
    repo = MonitorRepository(session)
    assert repo.get_redirect(client_id, "redirection", "9999") is None


def test_same_rule_key_in_different_sources_does_not_collide(session: Session) -> None:
    client_id = _client(session)
    repo = MonitorRepository(session)
    repo.upsert_redirect_state(
        client_id,
        "redirection",
        "same-key",
        source_url="/a/",
        target_url="/b/",
        http_code=301,
        match_type="url",
        regex=False,
        status="enabled",
        group_name=None,
        title=None,
        external_id=1,
        rule_sha256="x" * 64,
        now=T0,
    )
    repo.upsert_redirect_state(
        client_id,
        "probe",
        "same-key",
        source_url="/c/",
        target_url="/d/",
        http_code=302,
        match_type="url",
        regex=False,
        status="enabled",
        group_name=None,
        title=None,
        external_id=None,
        rule_sha256="y" * 64,
        now=T0,
    )
    session.flush()
    assert repo.get_redirect(client_id, "redirection", "same-key").source_url == "/a/"
    assert repo.get_redirect(client_id, "probe", "same-key").source_url == "/c/"


def test_duplicate_client_source_rule_key_violates_unique_constraint(session: Session) -> None:
    from app.db.models import MonitorRedirect

    client_id = _client(session)
    session.add(
        MonitorRedirect(
            client_id=client_id,
            source="redirection",
            rule_key="1",
            source_url="/a/",
            match_type="url",
            status="enabled",
            rule_sha256="a" * 64,
            first_seen_at=T0,
            last_seen_at=T0,
        )
    )
    session.flush()
    session.add(
        MonitorRedirect(
            client_id=client_id,
            source="redirection",
            rule_key="1",
            source_url="/dupe/",
            match_type="url",
            status="enabled",
            rule_sha256="b" * 64,
            first_seen_at=T0,
            last_seen_at=T0,
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()


class TestListRedirects:
    def test_active_excludes_removed(self, session: Session) -> None:
        client_id = _client(session)
        repo = MonitorRepository(session)
        _upsert(repo, client_id, key="1")
        _upsert(repo, client_id, key="2")
        repo.mark_redirects_removed(client_id, "redirection", {"1"}, now=T0)
        active = repo.list_redirects(client_id, source="redirection", state="active")
        assert {r.rule_key for r in active} == {"1"}

    def test_removed_state_returns_only_removed(self, session: Session) -> None:
        client_id = _client(session)
        repo = MonitorRepository(session)
        _upsert(repo, client_id, key="1")
        _upsert(repo, client_id, key="2")
        repo.mark_redirects_removed(client_id, "redirection", {"1"}, now=T0)
        removed = repo.list_redirects(client_id, source="redirection", state="removed")
        assert {r.rule_key for r in removed} == {"2"}

    def test_all_state_returns_everything(self, session: Session) -> None:
        client_id = _client(session)
        repo = MonitorRepository(session)
        _upsert(repo, client_id, key="1")
        _upsert(repo, client_id, key="2")
        repo.mark_redirects_removed(client_id, "redirection", {"1"}, now=T0)
        everything = repo.list_redirects(client_id, source="redirection", state="all")
        assert {r.rule_key for r in everything} == {"1", "2"}


class TestMarkRedirectsRemoved:
    def test_a_rule_missing_from_still_present_is_removed(self, session: Session) -> None:
        client_id = _client(session)
        repo = MonitorRepository(session)
        _upsert(repo, client_id, key="1")
        removed = repo.mark_redirects_removed(client_id, "redirection", set(), now=T0)
        assert [r.rule_key for r in removed] == ["1"]
        assert repo.get_redirect(client_id, "redirection", "1").removed_at == T0

    def test_a_rule_still_present_is_left_alone(self, session: Session) -> None:
        client_id = _client(session)
        repo = MonitorRepository(session)
        _upsert(repo, client_id, key="1")
        removed = repo.mark_redirects_removed(client_id, "redirection", {"1"}, now=T0)
        assert removed == []
        assert repo.get_redirect(client_id, "redirection", "1").removed_at is None

    def test_reappearing_rule_clears_removed_at_via_upsert(self, session: Session) -> None:
        client_id = _client(session)
        repo = MonitorRepository(session)
        _upsert(repo, client_id, key="1")
        repo.mark_redirects_removed(client_id, "redirection", set(), now=T0)
        assert repo.get_redirect(client_id, "redirection", "1").removed_at is not None
        _upsert(repo, client_id, key="1", now=T0 + timedelta(hours=1))
        assert repo.get_redirect(client_id, "redirection", "1").removed_at is None

    def test_reappearing_rule_gets_a_fresh_first_seen_window_not_the_original_one(
        self, session: Session
    ) -> None:
        """A rule that vanished and came back is a new appearance for the

        operator's purposes — keeping the original (possibly baseline, i.e.
        `None`) window would silently hide that it was ever gone.
        """
        client_id = _client(session)
        repo = MonitorRepository(session)
        _upsert(repo, client_id, key="1", window_start=None, now=T0)
        repo.mark_redirects_removed(client_id, "redirection", set(), now=T0 + timedelta(hours=1))
        reappeared_at = T0 + timedelta(hours=2)
        window_start = T0 + timedelta(hours=1)
        rule = _upsert(repo, client_id, key="1", window_start=window_start, now=reappeared_at)
        assert rule.first_seen_at == reappeared_at
        assert rule.first_seen_window_start == window_start

    def test_already_removed_rule_keeps_its_original_removed_at(self, session: Session) -> None:
        client_id = _client(session)
        repo = MonitorRepository(session)
        _upsert(repo, client_id, key="1")
        repo.mark_redirects_removed(client_id, "redirection", set(), now=T0)
        again = repo.mark_redirects_removed(
            client_id, "redirection", set(), now=T0 + timedelta(days=1)
        )
        assert again == []  # not re-flagged; only newly-removed rules come back
        assert repo.get_redirect(client_id, "redirection", "1").removed_at == T0


class TestListRedirectsPage:
    def test_pagination_and_total(self, session: Session) -> None:
        client_id = _client(session)
        repo = MonitorRepository(session)
        for i in range(5):
            _upsert(repo, client_id, key=str(i), source_url=f"/p{i}/")
        page, total = repo.list_redirects_page(client_id, limit=2, offset=0, sort="source_url")
        assert total == 5
        assert len(page) == 2

    def test_search_matches_source_or_target(self, session: Session) -> None:
        client_id = _client(session)
        repo = MonitorRepository(session)
        _upsert(repo, client_id, key="1", source_url="/special-page/", target_url="/x/")
        _upsert(repo, client_id, key="2", source_url="/other/", target_url="/y/")
        page, total = repo.list_redirects_page(client_id, q="special")
        assert total == 1
        assert page[0].rule_key == "1"

    def test_sort_by_first_seen_at_descending(self, session: Session) -> None:
        client_id = _client(session)
        repo = MonitorRepository(session)
        _upsert(repo, client_id, key="1", now=T0)
        _upsert(repo, client_id, key="2", now=T0 + timedelta(hours=1))
        page, _ = repo.list_redirects_page(client_id, sort="-first_seen_at")
        assert [r.rule_key for r in page] == ["2", "1"]


class TestRedirectsSummary:
    def test_counts_enabled_and_disabled_excluding_removed(self, session: Session) -> None:
        client_id = _client(session)
        repo = MonitorRepository(session)
        _upsert(repo, client_id, key="1", status="enabled")
        _upsert(repo, client_id, key="2", status="disabled")
        _upsert(repo, client_id, key="3", status="enabled")
        # still_present_keys={"3"} means "1" and "2" fell out this run.
        repo.mark_redirects_removed(client_id, "redirection", {"3"}, now=T0)
        summary = repo.redirects_summary(client_id, source="redirection")
        assert summary == {"enabled": 1, "disabled": 0}
