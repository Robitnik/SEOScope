"""`process_redirects` (task-51 §3.6) — orchestration end-to-end against a

real (in-memory) session: source fetch is monkeypatched, everything after it
(diff, severity, persistence, change feed) is real. This is the layer the
operator's actual ask ("дата та навіть межі години") lives in.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.core.config import Settings
from app.db.repositories.clients import ClientRepository
from app.db.repositories.monitor import MonitorRepository
from app.monitor.redirects import collect
from app.monitor.redirects.contract import RedirectRule, RedirectSourceResult

BASE = "https://acme.com"
T0 = datetime(2026, 8, 19, 14, 0, tzinfo=UTC)
T1 = T0 + timedelta(hours=1)
T2 = T0 + timedelta(hours=2)


def _client(session, *, slug: str = "acme"):
    client = ClientRepository(session).create(
        name="Acme", slug=slug, domain=f"{slug}.com", timezone="America/New_York"
    )
    session.commit()
    return client


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


def _rule(key: str, **overrides) -> RedirectRule:
    base = dict(
        rule_key=key,
        source_url=f"/old-{key}/",
        target_url=f"/new-{key}/",
        http_code=301,
        match_type="url",
        regex=False,
        status="enabled",
        group_name="Redirections",
        title=None,
        external_id=int(key),
    )
    base.update(overrides)
    return RedirectRule(**base)


def _run(session, client, *, kind="light"):
    repo = MonitorRepository(session)
    run = repo.create_run(client.id, kind=kind)
    session.commit()
    return run


def _process(
    session,
    client,
    *,
    monkeypatch,
    rules: tuple[RedirectRule, ...] = (),
    ok: bool = True,
    unavailable_reason: str | None = None,
    now: datetime = T0,
    tmp_path,
    settings: Settings | None = None,
):
    repo = MonitorRepository(session)
    monitor_settings = repo.get_or_create_settings(client.id)
    monitor_settings.redirect_source = "redirection"
    session.commit()
    run = _run(session, client)

    monkeypatch.setattr(
        collect,
        "_fetch_snapshot",
        lambda *a, **k: RedirectSourceResult(
            ok=ok, source="redirection", rules=rules, unavailable_reason=unavailable_reason
        ),
    )
    outcome = collect.process_redirects(
        repo,
        client,
        BASE,
        run_id=run.id,
        monitor_settings=monitor_settings,
        settings=settings or _settings(),
        artifacts_dir=tmp_path / "artifacts",
        key_pages=set(),
        now=now,
    )
    return repo, outcome


class TestBaseline:
    def test_first_successful_check_writes_rules_but_no_changes(
        self, session, monkeypatch, tmp_path
    ) -> None:
        client = _client(session)
        repo, outcome = _process(
            session,
            client,
            monkeypatch=monkeypatch,
            rules=(_rule("1"), _rule("2")),
            tmp_path=tmp_path,
        )
        assert outcome.ok is True
        assert outcome.added == 2
        assert len(repo.list_changes(client.id, state="all", limit=50)) == 0
        assert len(repo.list_redirects(client.id, source="redirection")) == 2

    def test_baseline_rules_have_no_window_start(self, session, monkeypatch, tmp_path) -> None:
        client = _client(session)
        repo, _ = _process(
            session, client, monkeypatch=monkeypatch, rules=(_rule("1"),), tmp_path=tmp_path
        )
        rule = repo.get_redirect(client.id, "redirection", "1")
        assert rule.first_seen_window_start is None
        assert rule.first_seen_at == T0

    def test_baseline_sets_redirects_last_checked_at(self, session, monkeypatch, tmp_path) -> None:
        client = _client(session)
        repo, _ = _process(
            session, client, monkeypatch=monkeypatch, rules=(_rule("1"),), tmp_path=tmp_path
        )
        settings_row = repo.get_settings(client.id)
        assert settings_row.redirects_last_checked_at == T0


class TestAppearanceWindow:
    def test_a_new_rule_after_baseline_gets_a_window_and_a_change(
        self, session, monkeypatch, tmp_path
    ) -> None:
        client = _client(session)
        _process(
            session, client, monkeypatch=monkeypatch, rules=(_rule("1"),), now=T0, tmp_path=tmp_path
        )
        repo, outcome = _process(
            session,
            client,
            monkeypatch=monkeypatch,
            rules=(_rule("1"), _rule("2")),
            now=T1,
            tmp_path=tmp_path,
        )
        assert outcome.added == 1
        rule = repo.get_redirect(client.id, "redirection", "2")
        assert rule.first_seen_window_start == T0
        assert rule.first_seen_at == T1

        changes = repo.list_changes(client.id, state="all", limit=50)
        added_change = next(c for c in changes if c.change_type == "redirect_added")
        assert added_change.details_json["window_start"] == T0.isoformat()
        assert added_change.details_json["window_end"] == T1.isoformat()

    def test_window_narrows_to_the_hour_between_two_checks(
        self, session, monkeypatch, tmp_path
    ) -> None:
        client = _client(session)
        _process(session, client, monkeypatch=monkeypatch, rules=(), now=T0, tmp_path=tmp_path)
        _process(session, client, monkeypatch=monkeypatch, rules=(), now=T1, tmp_path=tmp_path)
        repo, _ = _process(
            session, client, monkeypatch=monkeypatch, rules=(_rule("1"),), now=T2, tmp_path=tmp_path
        )
        rule = repo.get_redirect(client.id, "redirection", "1")
        # Window is bounded by the IMMEDIATELY preceding check (T1), not the
        # first one ever (T0) — an hourly check gives an hour-wide window.
        assert rule.first_seen_window_start == T1
        assert rule.first_seen_at == T2


class TestTargetChanged:
    def test_target_change_is_detected_and_persisted(self, session, monkeypatch, tmp_path) -> None:
        client = _client(session)
        _process(
            session, client, monkeypatch=monkeypatch, rules=(_rule("1"),), now=T0, tmp_path=tmp_path
        )
        repo, outcome = _process(
            session,
            client,
            monkeypatch=monkeypatch,
            rules=(_rule("1", target_url="/moved-again/"),),
            now=T1,
            tmp_path=tmp_path,
        )
        assert outcome.changed == 1
        changes = repo.list_changes(client.id, state="all", limit=50)
        change = next(c for c in changes if c.change_type == "redirect_target_changed")
        assert "/moved-again/" in change.new_value


class TestRemoved:
    def test_a_rule_that_disappears_is_marked_removed_and_reported(
        self, session, monkeypatch, tmp_path
    ) -> None:
        client = _client(session)
        _process(
            session,
            client,
            monkeypatch=monkeypatch,
            rules=(_rule("1"), _rule("2")),
            now=T0,
            tmp_path=tmp_path,
        )
        repo, outcome = _process(
            session, client, monkeypatch=monkeypatch, rules=(_rule("1"),), now=T1, tmp_path=tmp_path
        )
        assert outcome.removed == 1
        removed_rule = repo.get_redirect(client.id, "redirection", "2")
        assert removed_rule.removed_at == T1
        changes = repo.list_changes(client.id, state="all", limit=50)
        assert any(c.change_type == "redirect_removed" for c in changes)

    def test_a_removed_rule_that_reappears_is_added_again_not_silently_restored(
        self, session, monkeypatch, tmp_path
    ) -> None:
        client = _client(session)
        _process(
            session, client, monkeypatch=monkeypatch, rules=(_rule("1"),), now=T0, tmp_path=tmp_path
        )
        _process(session, client, monkeypatch=monkeypatch, rules=(), now=T1, tmp_path=tmp_path)
        repo, outcome = _process(
            session, client, monkeypatch=monkeypatch, rules=(_rule("1"),), now=T2, tmp_path=tmp_path
        )
        assert outcome.added == 1
        rule = repo.get_redirect(client.id, "redirection", "1")
        assert rule.removed_at is None
        assert rule.first_seen_window_start == T1


class TestSeverityClassification:
    def test_offsite_target_is_critical(self, session, monkeypatch, tmp_path) -> None:
        client = _client(session)
        _process(session, client, monkeypatch=monkeypatch, rules=(), now=T0, tmp_path=tmp_path)
        repo, _ = _process(
            session,
            client,
            monkeypatch=monkeypatch,
            rules=(_rule("1", target_url="https://evil-spam.example/x"),),
            now=T1,
            tmp_path=tmp_path,
        )
        changes = repo.list_changes(client.id, state="all", limit=50)
        change = next(c for c in changes if c.change_type == "redirect_offsite_added")
        assert change.severity == "critical"

    def test_plain_new_rule_is_info(self, session, monkeypatch, tmp_path) -> None:
        client = _client(session)
        _process(session, client, monkeypatch=monkeypatch, rules=(), now=T0, tmp_path=tmp_path)
        repo, _ = _process(
            session, client, monkeypatch=monkeypatch, rules=(_rule("1"),), now=T1, tmp_path=tmp_path
        )
        changes = repo.list_changes(client.id, state="all", limit=50)
        change = next(c for c in changes if c.change_type == "redirect_added")
        assert change.severity == "info"

    def test_bulk_addition_collapses_into_one_aggregate(
        self, session, monkeypatch, tmp_path
    ) -> None:
        client = _client(session)
        _process(session, client, monkeypatch=monkeypatch, rules=(), now=T0, tmp_path=tmp_path)
        many_rules = tuple(_rule(str(i)) for i in range(1, 31))  # 30 > default threshold (25)
        repo, outcome = _process(
            session, client, monkeypatch=monkeypatch, rules=many_rules, now=T1, tmp_path=tmp_path
        )
        assert outcome.added == 30
        changes = repo.list_changes(client.id, state="all", limit=50)
        bulk = [c for c in changes if c.change_type == "redirects_bulk_added"]
        individual = [c for c in changes if c.change_type == "redirect_added"]
        assert len(bulk) == 1
        assert bulk[0].details_json["count"] == 30
        assert individual == []


class TestUnavailable:
    def test_failed_fetch_opens_a_warning_and_keeps_existing_rules(
        self, session, monkeypatch, tmp_path
    ) -> None:
        client = _client(session)
        _process(
            session, client, monkeypatch=monkeypatch, rules=(_rule("1"),), now=T0, tmp_path=tmp_path
        )
        repo, outcome = _process(
            session,
            client,
            monkeypatch=monkeypatch,
            ok=False,
            unavailable_reason="auth_failed",
            now=T1,
            tmp_path=tmp_path,
        )
        assert outcome.ok is False
        assert outcome.unavailable_reason == "auth_failed"
        # Existing rule must NOT be marked removed on a fetch failure.
        assert repo.get_redirect(client.id, "redirection", "1").removed_at is None
        changes = repo.list_changes(client.id, state="all", limit=50)
        assert any(
            c.change_type == "redirects_unavailable" and c.severity == "warning" for c in changes
        )

    def test_unavailable_fires_even_on_the_very_first_check(
        self, session, monkeypatch, tmp_path
    ) -> None:
        client = _client(session)
        repo, outcome = _process(
            session,
            client,
            monkeypatch=monkeypatch,
            ok=False,
            unavailable_reason="auth_failed",
            tmp_path=tmp_path,
        )
        assert outcome.ok is False
        changes = repo.list_changes(client.id, state="all", limit=50)
        assert any(c.change_type == "redirects_unavailable" for c in changes)

    def test_recovery_resolves_the_unavailable_change(self, session, monkeypatch, tmp_path) -> None:
        client = _client(session)
        _process(
            session,
            client,
            monkeypatch=monkeypatch,
            ok=False,
            unavailable_reason="auth_failed",
            now=T0,
            tmp_path=tmp_path,
        )
        repo, outcome = _process(
            session, client, monkeypatch=monkeypatch, rules=(_rule("1"),), now=T1, tmp_path=tmp_path
        )
        assert outcome.ok is True
        changes = repo.list_changes(client.id, state="all", limit=50)
        assert any(c.change_type == "redirects_unavailable_resolved" for c in changes)
        open_ones = [
            c for c in changes if c.change_type == "redirects_unavailable" and c.resolved_at is None
        ]
        assert open_ones == []


class TestDisabled:
    def test_globally_disabled_does_nothing(self, session, monkeypatch, tmp_path) -> None:
        client = _client(session)
        repo, outcome = _process(
            session,
            client,
            monkeypatch=monkeypatch,
            rules=(_rule("1"),),
            tmp_path=tmp_path,
            settings=_settings(monitor_redirects_enabled=False),
        )
        assert outcome.ok is True
        assert outcome.added == 0
        assert repo.list_redirects(client.id, source="redirection") == []

    def test_per_client_disabled_does_nothing(self, session, monkeypatch, tmp_path) -> None:
        client = _client(session)
        repo = MonitorRepository(session)
        settings_row = repo.get_or_create_settings(client.id)
        settings_row.redirects_enabled = False
        session.commit()
        outcome = collect.process_redirects(
            repo,
            client,
            BASE,
            run_id=_run(session, client).id,
            monitor_settings=settings_row,
            settings=_settings(),
            artifacts_dir=tmp_path / "artifacts",
            key_pages=set(),
            now=T0,
        )
        assert outcome.ok is True
        assert repo.list_redirects(client.id, source="redirection") == []


class TestSourceFallback:
    """`_fetch_snapshot`'s own fallback chain (task-51 follow-up 2026-08-19,

    live finding): Application Password first (cheap, one request), then the
    browser-login fallback whenever it's configured — whether or not an
    Application Password is ALSO configured but failing — else an honest
    "nothing configured" result. Exercises the real function; only the two
    adapter entry points are monkeypatched.
    """

    def _client_and_repo(self, session):
        client = _client(session)
        return client, MonitorRepository(session)

    def test_app_password_success_short_circuits_before_browser(
        self, session, monkeypatch, tmp_path
    ) -> None:
        from app.monitor.redirects import credentials as credentials_mod
        from app.monitor.redirects import wp_browser, wp_redirection

        client, repo = self._client_and_repo(session)
        credentials_mod.write_credentials(
            tmp_path / "wp_credentials.json", client.id, user="u", app_password="p"
        )
        browser_called = False

        def fake_browser(*a, **k):
            nonlocal browser_called
            browser_called = True
            return RedirectSourceResult(ok=True, source="redirection", rules=())

        monkeypatch.setattr(
            wp_redirection,
            "fetch_redirects",
            lambda *a, **k: RedirectSourceResult(
                ok=True, source="redirection", rules=(_rule("1"),)
            ),
        )
        monkeypatch.setattr(wp_browser, "login_and_fetch_redirects", fake_browser)

        settings = _settings(wp_credentials_path=tmp_path / "wp_credentials.json")
        result = collect._fetch_snapshot(
            repo, client, BASE, "redirection", settings=settings, key_pages=set()
        )
        assert result.ok is True
        assert len(result.rules) == 1
        assert browser_called is False

    def test_app_password_failure_falls_back_to_browser_login(
        self, session, monkeypatch, tmp_path
    ) -> None:
        from app.monitor.redirects import credentials as credentials_mod
        from app.monitor.redirects import wp_browser, wp_redirection

        client, repo = self._client_and_repo(session)
        credentials_mod.write_credentials(
            tmp_path / "wp_credentials.json", client.id, user="u", app_password="p"
        )
        credentials_mod.write_login_credentials(
            tmp_path / "wp_login_credentials.json", client.id, login="admin", password="pw"
        )
        monkeypatch.setattr(
            wp_redirection,
            "fetch_redirects",
            lambda *a, **k: RedirectSourceResult(
                ok=False, source="redirection", unavailable_reason="auth_failed"
            ),
        )
        monkeypatch.setattr(
            wp_browser,
            "login_and_fetch_redirects",
            lambda *a, **k: RedirectSourceResult(
                ok=True, source="redirection", rules=(_rule("1"), _rule("2"))
            ),
        )

        settings = _settings(
            wp_credentials_path=tmp_path / "wp_credentials.json",
            wp_login_credentials_path=tmp_path / "wp_login_credentials.json",
        )
        result = collect._fetch_snapshot(
            repo, client, BASE, "redirection", settings=settings, key_pages=set()
        )
        assert result.ok is True
        assert len(result.rules) == 2

    def test_no_app_password_goes_straight_to_browser_login(
        self, session, monkeypatch, tmp_path
    ) -> None:
        from app.monitor.redirects import credentials as credentials_mod
        from app.monitor.redirects import wp_browser, wp_redirection

        client, repo = self._client_and_repo(session)
        credentials_mod.write_login_credentials(
            tmp_path / "wp_login_credentials.json", client.id, login="admin", password="pw"
        )
        app_password_called = False

        def fake_app_password(*a, **k):
            nonlocal app_password_called
            app_password_called = True
            return RedirectSourceResult(ok=False, source="redirection")

        monkeypatch.setattr(wp_redirection, "fetch_redirects", fake_app_password)
        monkeypatch.setattr(
            wp_browser,
            "login_and_fetch_redirects",
            lambda *a, **k: RedirectSourceResult(ok=True, source="redirection", rules=()),
        )

        settings = _settings(
            wp_credentials_path=tmp_path / "wp_credentials.json",  # does not exist
            wp_login_credentials_path=tmp_path / "wp_login_credentials.json",
        )
        result = collect._fetch_snapshot(
            repo, client, BASE, "redirection", settings=settings, key_pages=set()
        )
        assert result.ok is True
        assert app_password_called is False

    def test_neither_configured_is_an_honest_auth_failed(
        self, session, monkeypatch, tmp_path
    ) -> None:
        client, repo = self._client_and_repo(session)
        settings = _settings(
            wp_credentials_path=tmp_path / "wp_credentials.json",
            wp_login_credentials_path=tmp_path / "wp_login_credentials.json",
        )
        result = collect._fetch_snapshot(
            repo, client, BASE, "redirection", settings=settings, key_pages=set()
        )
        assert result.ok is False
        assert result.unavailable_reason == "auth_failed"

    def test_app_password_failure_and_no_login_credentials_returns_the_app_password_result(
        self, session, monkeypatch, tmp_path
    ) -> None:
        from app.monitor.redirects import credentials as credentials_mod
        from app.monitor.redirects import wp_redirection

        client, repo = self._client_and_repo(session)
        credentials_mod.write_credentials(
            tmp_path / "wp_credentials.json", client.id, user="u", app_password="p"
        )
        monkeypatch.setattr(
            wp_redirection,
            "fetch_redirects",
            lambda *a, **k: RedirectSourceResult(
                ok=False, source="redirection", unavailable_reason="plugin_missing"
            ),
        )
        settings = _settings(
            wp_credentials_path=tmp_path / "wp_credentials.json",
            wp_login_credentials_path=tmp_path / "wp_login_credentials.json",  # does not exist
        )
        result = collect._fetch_snapshot(
            repo, client, BASE, "redirection", settings=settings, key_pages=set()
        )
        assert result.ok is False
        assert result.unavailable_reason == "plugin_missing"
