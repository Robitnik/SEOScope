"""Тести REST-пошуку точного WordPress `attachment ID` (task-39).

Нуль мережі: фейковий `fetcher` віддає підготовлені `RestResponse` по черзі,
одну на кожен реальний HTTP-запит, який зробив би `lookup_media`. Порядок і
кількість запитів — частина контракту (не більше трьох), тож частина тестів
звіряє саме `fetcher.urls`, а не лише кінцевий вердикт.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.tools.alt_text.wp_media import derive_upload_location
from app.tools.alt_text.wp_rest import (
    MediaConfidence,
    MediaLookupStatus,
    RestResponse,
    build_admin_url,
    fetch_rest,
    lookup_media,
)

SITE = "example.com"
WP_ROOT = f"https://{SITE}"
UPLOADS = f"{WP_ROOT}/wp-content/uploads/2024/03"


class _FakeFetcher:
    """Віддає підготовлені відповіді по черзі; записує кожен запитаний URL."""

    def __init__(self, *responses: RestResponse) -> None:
        self._responses = list(responses)
        self.urls: list[str] = []

    def __call__(self, url: str) -> RestResponse:
        self.urls.append(url)
        return self._responses.pop(0)


def _json_response(payload: Any, *, status: int = 200) -> RestResponse:
    return RestResponse(
        http_status=status, content_type="application/json", body=json.dumps(payload).encode()
    )


def _html_response(status: int = 200) -> RestResponse:
    return RestResponse(http_status=status, content_type="text/html", body=b"<html></html>")


# --- точний збіг -----------------------------------------------------------------


def test_exact_match_by_media_details_file_resolves_in_one_request() -> None:
    location = derive_upload_location(f"{UPLOADS}/hero-image-300x200.jpg")
    assert location is not None
    payload = [
        {
            "id": 42,
            "slug": "hero-image",
            "alt_text": "Team at the office",
            "source_url": f"{UPLOADS}/hero-image.jpg",
            "media_details": {"file": "2024/03/hero-image.jpg", "sizes": {}},
        }
    ]
    fetcher = _FakeFetcher(_json_response(payload))

    result = lookup_media(location, fetcher=fetcher)

    assert result.status is MediaLookupStatus.resolved
    assert result.media_id == 42
    assert result.confidence is MediaConfidence.exact
    assert result.alt_text == "Team at the office"
    assert result.admin_url == build_admin_url(WP_ROOT, 42)
    assert len(fetcher.urls) == 1
    assert "search=2024%2F03%2Fhero-image.jpg" in fetcher.urls[0]


def test_exact_match_via_a_registered_size_file() -> None:
    location = derive_upload_location(f"{UPLOADS}/hero-image-1024x683.jpg")
    assert location is not None
    payload = [
        {
            "id": 9,
            "slug": "hero-image-original",
            "alt_text": None,
            "source_url": f"{UPLOADS}/hero-image-original.jpg",
            "media_details": {
                "file": "2024/03/hero-image-original.jpg",
                "sizes": {"large": {"file": "hero-image-1024x683.jpg"}},
            },
        }
    ]
    fetcher = _FakeFetcher(_json_response(payload))

    result = lookup_media(location, fetcher=fetcher)

    assert result.status is MediaLookupStatus.resolved
    assert result.media_id == 9
    assert result.confidence is MediaConfidence.exact


def test_admin_url_is_based_on_wp_root_not_the_cdn_source_url() -> None:
    # `derive_upload_location` вже розгортає Photon-проксі до справжнього
    # хоста — admin_url має спиратись на нього, не на хост картинки з CDN.
    location = derive_upload_location(
        f"https://i0.wp.com/{SITE}/wp-content/uploads/2024/03/hero-image.jpg?ssl=1"
    )
    assert location is not None
    payload = [
        {
            "id": 7,
            "slug": "hero-image",
            "alt_text": None,
            "source_url": f"https://i0.wp.com/{SITE}/wp-content/uploads/2024/03/hero-image.jpg",
            "media_details": {"file": "2024/03/hero-image.jpg", "sizes": {}},
        }
    ]
    fetcher = _FakeFetcher(_json_response(payload))

    result = lookup_media(location, fetcher=fetcher)

    assert result.admin_url == f"{WP_ROOT}/wp-admin/upload.php?item=7"


# --- слабкий збіг (offloaded media) -----------------------------------------------


def test_filename_only_match_for_offloaded_media() -> None:
    location = derive_upload_location(f"{UPLOADS}/hero-image.jpg")
    assert location is not None
    payload = [
        {
            "id": 11,
            "slug": "hero-image",
            "alt_text": None,
            "source_url": "https://cdn.example-bucket.s3.amazonaws.com/media/2024/03/hero-image.jpg",
            "media_details": {"sizes": {}},
        }
    ]
    fetcher = _FakeFetcher(_json_response(payload))

    result = lookup_media(location, fetcher=fetcher)

    assert result.status is MediaLookupStatus.resolved
    assert result.confidence is MediaConfidence.filename


# --- 1.jpg: ambiguous і not_found, ескалація по трьох кроках --------------------


def test_bare_numeric_filename_with_multiple_candidates_is_ambiguous() -> None:
    location = derive_upload_location(f"{UPLOADS}/1.jpg")
    assert location is not None
    other_candidates = [
        {
            "id": 101,
            "slug": "1",
            "alt_text": None,
            "source_url": f"{WP_ROOT}/wp-content/uploads/2023/01/1-copy.jpg",
            "media_details": {"file": "2023/01/1-copy.jpg", "sizes": {}},
        },
        {
            "id": 102,
            "slug": "1-2",
            "alt_text": None,
            "source_url": f"{WP_ROOT}/wp-content/uploads/2022/11/1.png",
            "media_details": {"file": "2022/11/1.png", "sizes": {}},
        },
    ]
    fetcher = _FakeFetcher(
        _json_response([]),  # крок 1: точний шлях — порожньо
        _json_response([]),  # крок 2: slug — порожньо
        _json_response(other_candidates),  # крок 3: широкий пошук
    )

    result = lookup_media(location, fetcher=fetcher)

    assert result.status is MediaLookupStatus.ambiguous
    assert result.media_id is None
    assert {candidate.id for candidate in result.candidates} == {101, 102}
    assert len(fetcher.urls) == 3


def test_not_found_when_all_three_steps_are_empty() -> None:
    location = derive_upload_location(f"{UPLOADS}/ghost.jpg")
    assert location is not None
    fetcher = _FakeFetcher(_json_response([]), _json_response([]), _json_response([]))

    result = lookup_media(location, fetcher=fetcher)

    assert result.status is MediaLookupStatus.not_found
    assert result.admin_url == f"{WP_ROOT}/wp-admin/upload.php?mode=list&s=ghost"
    assert len(fetcher.urls) == 3


def test_second_step_is_skipped_once_the_first_step_has_candidates() -> None:
    # Кандидат є (хоч і не підтверджений — інша директорія, типова колізія
    # `1.jpg`), тож крок 3 не потрібен: більше запитів до сайту клієнта не
    # робимо просто тому, що перший кандидат не підтвердився.
    location = derive_upload_location(f"{UPLOADS}/1.jpg")
    assert location is not None
    payload = [
        {
            "id": 5,
            "slug": "1",
            "alt_text": None,
            "source_url": f"{WP_ROOT}/wp-content/uploads/2019/06/1.jpg",
            "media_details": {"file": "2019/06/1.jpg", "sizes": {}},
        }
    ]
    fetcher = _FakeFetcher(_json_response(payload))

    result = lookup_media(location, fetcher=fetcher)

    assert result.status is MediaLookupStatus.ambiguous
    assert len(fetcher.urls) == 1


# --- відмови --------------------------------------------------------------------


def test_html_response_is_rest_unavailable() -> None:
    location = derive_upload_location(f"{UPLOADS}/hero-image.jpg")
    assert location is not None
    fetcher = _FakeFetcher(_html_response())

    result = lookup_media(location, fetcher=fetcher)

    assert result.status is MediaLookupStatus.rest_unavailable
    assert result.detail is not None


@pytest.mark.parametrize("status", [401, 403, 404])
def test_auth_or_not_found_http_status_is_rest_unavailable(status: int) -> None:
    location = derive_upload_location(f"{UPLOADS}/hero-image.jpg")
    assert location is not None
    fetcher = _FakeFetcher(
        RestResponse(http_status=status, content_type="application/json", body=b"{}")
    )

    result = lookup_media(location, fetcher=fetcher)

    assert result.status is MediaLookupStatus.rest_unavailable


def test_server_error_is_failed_not_rest_unavailable() -> None:
    location = derive_upload_location(f"{UPLOADS}/hero-image.jpg")
    assert location is not None
    fetcher = _FakeFetcher(
        RestResponse(http_status=500, content_type="application/json", body=b"{}")
    )

    result = lookup_media(location, fetcher=fetcher)

    assert result.status is MediaLookupStatus.failed


def test_network_exception_is_failed() -> None:
    location = derive_upload_location(f"{UPLOADS}/hero-image.jpg")
    assert location is not None

    def boom(url: str) -> RestResponse:
        raise TimeoutError

    result = lookup_media(location, fetcher=boom)

    assert result.status is MediaLookupStatus.failed


def test_malformed_json_is_rest_unavailable() -> None:
    location = derive_upload_location(f"{UPLOADS}/hero-image.jpg")
    assert location is not None
    fetcher = _FakeFetcher(
        RestResponse(http_status=200, content_type="application/json", body=b"not json")
    )

    result = lookup_media(location, fetcher=fetcher)

    assert result.status is MediaLookupStatus.rest_unavailable


def test_non_list_json_payload_is_rest_unavailable() -> None:
    location = derive_upload_location(f"{UPLOADS}/hero-image.jpg")
    assert location is not None
    fetcher = _FakeFetcher(_json_response({"code": "rest_no_route"}))

    result = lookup_media(location, fetcher=fetcher)

    assert result.status is MediaLookupStatus.rest_unavailable


# --- fetch_rest: rest_route fallback ---------------------------------------------


def test_fetch_rest_falls_back_to_rest_route_once_on_html_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.tools.alt_text import wp_rest as wp_rest_module

    calls: list[str] = []

    def fake_fetch_once(url: str) -> RestResponse:
        calls.append(url)
        if "rest_route" not in url:
            return RestResponse(http_status=404, content_type="text/html", body=b"<html></html>")
        return RestResponse(http_status=200, content_type="application/json", body=b"[]")

    monkeypatch.setattr(wp_rest_module, "_fetch_once", fake_fetch_once)
    monkeypatch.setattr(wp_rest_module, "assert_public_http_url", lambda url: None)

    result = fetch_rest(f"{WP_ROOT}/wp-json/wp/v2/media?search=hero")

    assert len(calls) == 2
    assert "rest_route" in calls[1]
    assert result.http_status == 200


def test_fetch_rest_does_not_retry_on_a_clean_200(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.tools.alt_text import wp_rest as wp_rest_module

    calls: list[str] = []

    def fake_fetch_once(url: str) -> RestResponse:
        calls.append(url)
        return RestResponse(http_status=200, content_type="application/json", body=b"[]")

    monkeypatch.setattr(wp_rest_module, "_fetch_once", fake_fetch_once)
    monkeypatch.setattr(wp_rest_module, "assert_public_http_url", lambda url: None)

    fetch_rest(f"{WP_ROOT}/wp-json/wp/v2/media?search=hero")

    assert len(calls) == 1
