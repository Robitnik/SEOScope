"""`app.urlshots.service` — the cache-vs-capture decision and the row/DTO
shape returned by both endpoints (task-64 §2.5)."""

from __future__ import annotations

from unittest.mock import patch

from app.db.models import Client
from app.urlshots.capture import CapturedShot
from app.urlshots.service import get_cached_screenshots, get_or_capture_screenshots

URL = "https://acme.com/a"


def _seed_client(session) -> Client:
    client = Client(name="Acme", slug="acme", domain="acme.com")
    session.add(client)
    session.commit()
    return client


def _ok_shot(preset: str) -> CapturedShot:
    return CapturedShot(
        preset=preset,
        state="ok",
        image=b"jpeg-bytes",
        thumb=b"thumb-bytes",
        width=1440,
        height=2000,
        fold_height=900,
        byte_size=10,
        truncated=False,
        http_status=200,
        final_url=URL,
        error=None,
    )


def _failed_shot(preset: str) -> CapturedShot:
    return CapturedShot(
        preset=preset,
        state="failed",
        image=None,
        thumb=None,
        width=None,
        height=None,
        fold_height=None,
        byte_size=None,
        truncated=False,
        http_status=None,
        final_url=None,
        error="timeout",
    )


class TestGetCachedScreenshots:
    def test_nothing_captured_yet_returns_empty_list(self, session, tmp_path):
        dto = get_cached_screenshots(session, url=URL)
        assert dto.screenshots == []
        assert dto.cached is True


class TestGetOrCaptureScreenshots:
    def test_first_call_captures_and_persists(self, session, tmp_path):
        _seed_client(session)
        with (
            patch(
                "app.urlshots.service.capture_screenshots",
                return_value=[_ok_shot("desktop")],
            ),
            patch("app.urlshots.service.get_settings") as mock_settings,
        ):
            mock_settings.return_value.artifacts_dir = tmp_path
            dto = get_or_capture_screenshots(session, url=URL, presets=("desktop",))

        assert dto.cached is False
        assert len(dto.screenshots) == 1
        shot = dto.screenshots[0]
        assert shot.state == "ok"
        assert shot.image_url == f"/api/v1/url-screenshots/{shot.id}/image"
        assert shot.thumb_url == f"/api/v1/url-screenshots/{shot.id}/thumb"

        # Persisted for real — a plain cached read now sees it.
        cached = get_cached_screenshots(session, url=URL)
        assert len(cached.screenshots) == 1

    def test_second_call_within_the_cache_window_does_not_recapture(self, session, tmp_path):
        with (
            patch(
                "app.urlshots.service.capture_screenshots",
                return_value=[_ok_shot("desktop")],
            ) as mock_capture,
            patch("app.urlshots.service.get_settings") as mock_settings,
        ):
            mock_settings.return_value.artifacts_dir = tmp_path
            get_or_capture_screenshots(session, url=URL, presets=("desktop",))

            second = get_or_capture_screenshots(session, url=URL, presets=("desktop",))
            assert second.cached is True
            mock_capture.assert_called_once()

    def test_force_recaptures_even_when_cache_is_fresh(self, session, tmp_path):
        with (
            patch(
                "app.urlshots.service.capture_screenshots",
                return_value=[_ok_shot("desktop")],
            ) as mock_capture,
            patch("app.urlshots.service.get_settings") as mock_settings,
        ):
            mock_settings.return_value.artifacts_dir = tmp_path
            get_or_capture_screenshots(session, url=URL, presets=("desktop",))
            second = get_or_capture_screenshots(
                session, url=URL, presets=("desktop",), force=True
            )
            assert second.cached is False
            assert mock_capture.call_count == 2

    def test_failed_shot_is_persisted_with_no_image_urls(self, session, tmp_path):
        with (
            patch(
                "app.urlshots.service.capture_screenshots",
                return_value=[_failed_shot("mobile")],
            ),
            patch("app.urlshots.service.get_settings") as mock_settings,
        ):
            mock_settings.return_value.artifacts_dir = tmp_path
            dto = get_or_capture_screenshots(session, url=URL, presets=("mobile",))

        shot = dto.screenshots[0]
        assert shot.state == "failed"
        assert shot.image_url is None
        assert shot.thumb_url is None
        assert shot.error == "timeout"
