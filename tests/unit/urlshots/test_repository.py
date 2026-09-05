"""`app.db.repositories.url_screenshots` — history-is-kept reads and the
`is_fresh` all-presets-or-stale cache rule (task-64 §2.4-2.5)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.db.repositories.url_screenshots import (
    UrlScreenshotInput,
    UrlScreenshotRepository,
    is_fresh,
)

KEY = "acme.com/a"
PRESETS = ("desktop", "tablet", "mobile")


def _input(preset: str, *, state: str = "ok") -> UrlScreenshotInput:
    return UrlScreenshotInput(
        client_id=None,
        inspector_key=KEY,
        url="https://acme.com/a",
        preset=preset,
        state=state,
        blob_sha256="a" * 64 if state == "ok" else None,
        thumb_sha256="b" * 64 if state == "ok" else None,
        width=1440,
        height=2000,
        fold_height=900,
        byte_size=1000,
        truncated=False,
        http_status=200,
        final_url="https://acme.com/a",
        error=None if state == "ok" else "boom",
    )


class TestLatestSet:
    def test_no_rows_returns_empty(self, session):
        assert UrlScreenshotRepository(session).latest_set(KEY, presets=PRESETS) == []

    def test_returns_one_row_per_preset(self, session):
        repo = UrlScreenshotRepository(session)
        for preset in PRESETS:
            repo.create(_input(preset))
        session.commit()
        rows = repo.latest_set(KEY, presets=PRESETS)
        assert {r.preset for r in rows} == set(PRESETS)

    def test_missing_preset_is_absent_not_fabricated(self, session):
        repo = UrlScreenshotRepository(session)
        repo.create(_input("desktop"))
        session.commit()
        rows = repo.latest_set(KEY, presets=PRESETS)
        assert [r.preset for r in rows] == ["desktop"]

    def test_newest_capture_wins_over_an_older_one(self, session):
        repo = UrlScreenshotRepository(session)
        first = repo.create(_input("desktop"))
        session.commit()
        second = repo.create(_input("desktop"))
        session.commit()
        rows = repo.latest_set(KEY, presets=("desktop",))
        assert rows[0].id == second.id
        assert rows[0].id != first.id

    def test_failed_row_is_returned_as_the_latest_too(self, session):
        repo = UrlScreenshotRepository(session)
        repo.create(_input("mobile", state="failed"))
        session.commit()
        rows = repo.latest_set(KEY, presets=("mobile",))
        assert rows[0].state == "failed"
        assert rows[0].blob_sha256 is None


class TestIsFresh:
    def test_all_presets_within_window_is_fresh(self, session):
        now = datetime.now(UTC)
        repo = UrlScreenshotRepository(session)
        for preset in PRESETS:
            repo.create(_input(preset))
        session.commit()
        rows = repo.latest_set(KEY, presets=PRESETS)
        assert is_fresh(rows, presets=PRESETS, now=now, max_age_seconds=86_400) is True

    def test_missing_preset_makes_the_whole_set_stale(self, session):
        now = datetime.now(UTC)
        repo = UrlScreenshotRepository(session)
        repo.create(_input("desktop"))
        repo.create(_input("tablet"))
        session.commit()
        rows = repo.latest_set(KEY, presets=PRESETS)  # "mobile" never captured
        assert is_fresh(rows, presets=PRESETS, now=now, max_age_seconds=86_400) is False

    def test_one_stale_row_makes_the_whole_set_stale(self, session):
        repo = UrlScreenshotRepository(session)
        for preset in PRESETS:
            repo.create(_input(preset))
        session.commit()
        rows = repo.latest_set(KEY, presets=PRESETS)
        far_future = datetime.now(UTC) + timedelta(days=10)
        assert is_fresh(rows, presets=PRESETS, now=far_future, max_age_seconds=86_400) is False
