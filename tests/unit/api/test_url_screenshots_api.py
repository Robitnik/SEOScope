"""URL Inspector screenshot endpoints (`/api/v1/url-screenshots*`, task-64
§2). `capture_screenshots` is mocked throughout — no real browser launch —
mirroring how `test_url_inspector_api.py` mocks the live-check's network
calls."""

from __future__ import annotations

from unittest.mock import patch

from app.urlshots.capture import CapturedShot

URL = "https://acme.com/a"


def _ok_shot(preset: str) -> CapturedShot:
    return CapturedShot(
        preset=preset,
        state="ok",
        image=b"\xff\xd8\xff" + b"jpeg" * 10,
        thumb=b"\xff\xd8\xff" + b"thumb" * 10,
        width=1440,
        height=2000,
        fold_height=900,
        byte_size=42,
        truncated=False,
        http_status=200,
        final_url=URL,
        error=None,
    )


class TestGetUrlScreenshots:
    def test_nothing_captured_yet_is_a_200_with_empty_list(self, client, db):
        response = client.get("/api/v1/url-screenshots", params={"url": URL})
        assert response.status_code == 200
        assert response.json()["screenshots"] == []

    def test_invalid_url_is_400(self, client, db):
        response = client.get("/api/v1/url-screenshots", params={"url": "javascript:alert(1)"})
        assert response.status_code == 400


class TestPostUrlScreenshots:
    def test_capture_persists_and_serves_image_bytes(self, client, db):
        with patch(
            "app.urlshots.service.capture_screenshots", return_value=[_ok_shot("desktop")]
        ):
            response = client.post(
                "/api/v1/url-screenshots", json={"url": URL, "presets": ["desktop"]}
            )
        assert response.status_code == 200
        body = response.json()
        assert body["cached"] is False
        shot = body["screenshots"][0]
        assert shot["state"] == "ok"

        image_response = client.get(shot["image_url"])
        assert image_response.status_code == 200
        assert image_response.headers["content-type"] == "image/jpeg"
        assert "immutable" in image_response.headers["cache-control"]

        thumb_response = client.get(shot["thumb_url"])
        assert thumb_response.status_code == 200

    def test_second_call_reads_cache_and_does_not_recapture(self, client, db):
        with patch(
            "app.urlshots.service.capture_screenshots", return_value=[_ok_shot("desktop")]
        ) as mock_capture:
            client.post("/api/v1/url-screenshots", json={"url": URL, "presets": ["desktop"]})
            second = client.post(
                "/api/v1/url-screenshots", json={"url": URL, "presets": ["desktop"]}
            )
        assert second.json()["cached"] is True
        mock_capture.assert_called_once()

    def test_unsafe_url_is_400_not_500(self, client, db):
        response = client.post(
            "/api/v1/url-screenshots", json={"url": "http://169.254.169.254/"}
        )
        assert response.status_code == 400
        assert response.json()["detail"]["error_code"] == "unsafe_url"

    def test_invalid_url_is_400(self, client, db):
        response = client.post("/api/v1/url-screenshots", json={"url": "not a url"})
        assert response.status_code == 400


class TestImageEndpoints:
    def test_unknown_screenshot_id_is_404(self, client, db):
        response = client.get("/api/v1/url-screenshots/00000000-0000-0000-0000-000000000000/image")
        assert response.status_code == 404
        response = client.get("/api/v1/url-screenshots/00000000-0000-0000-0000-000000000000/thumb")
        assert response.status_code == 404
