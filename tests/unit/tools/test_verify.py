"""Тести перевірки alt без кешу (task-27): вердикти, cache-buster, SSRF-гард."""

from __future__ import annotations

import socket
import urllib.error
from collections.abc import Callable

import pytest

from app.tools.alt_text.verify import (
    CACHE_BUSTER_PARAM,
    FetchedPage,
    UnsafeUrlError,
    VerifyStatus,
    assert_public_http_url,
    build_cache_busted_url,
    find_page_images,
    normalize_alt,
    verify_alt,
)

PAGE = "https://example.com/about"
IMAGE = "https://example.com/wp-content/uploads/2024/03/hero-image.jpg"
UPLOADS = "https://example.com/wp-content/uploads/2024/03"


def page(html: str, *, status: int = 200, truncated: bool = False) -> Callable[[str], FetchedPage]:
    def fetcher(url: str) -> FetchedPage:
        return FetchedPage(http_status=status, html=html, final_url=url, truncated=truncated)

    return fetcher


# --- cache-buster -------------------------------------------------------------


def test_cache_buster_is_appended_to_a_plain_url() -> None:
    assert build_cache_busted_url(PAGE, "abcd1234") == f"{PAGE}?{CACHE_BUSTER_PARAM}=abcd1234"


def test_cache_buster_preserves_an_existing_query() -> None:
    result = build_cache_busted_url(f"{PAGE}?lang=uk&page=2", "abcd1234")
    assert result == f"{PAGE}?lang=uk&page=2&{CACHE_BUSTER_PARAM}=abcd1234"


def test_cache_buster_is_not_duplicated_on_a_second_check() -> None:
    once = build_cache_busted_url(PAGE, "aaaa")
    twice = build_cache_busted_url(once, "bbbb")
    assert twice.count(CACHE_BUSTER_PARAM) == 1
    assert twice.endswith("bbbb")


def test_cache_buster_drops_the_fragment() -> None:
    assert "#" not in build_cache_busted_url(f"{PAGE}#gallery", "aaaa")


def test_each_check_uses_a_fresh_random_token() -> None:
    assert build_cache_busted_url(PAGE) != build_cache_busted_url(PAGE)


def test_verify_actually_requests_the_cache_busted_url() -> None:
    requested: list[str] = []

    def fetcher(url: str) -> FetchedPage:
        requested.append(url)
        return FetchedPage(200, f'<img src="{IMAGE}" alt="An alt">', url)

    result = verify_alt(PAGE, IMAGE, "An alt", fetcher=fetcher)
    assert requested == [result.checked_url]
    assert f"{CACHE_BUSTER_PARAM}=" in requested[0]


# --- вердикти ------------------------------------------------------------------


def test_match() -> None:
    result = verify_alt(PAGE, IMAGE, "Team at the office",
                        fetcher=page(f'<img src="{IMAGE}" alt="Team at the office">'))
    assert result.status is VerifyStatus.match
    assert result.found_alt == "Team at the office"
    assert result.http_status == 200


def test_match_ignores_whitespace_differences() -> None:
    result = verify_alt(PAGE, IMAGE, "Team at the office",
                        fetcher=page(f'<img src="{IMAGE}" alt="  Team   at the\noffice ">'))
    assert result.status is VerifyStatus.match


def test_match_is_case_sensitive_because_alt_copy_is_content() -> None:
    result = verify_alt(PAGE, IMAGE, "Team at the office",
                        fetcher=page(f'<img src="{IMAGE}" alt="team at the office">'))
    assert result.status is VerifyStatus.mismatch


def test_mismatch_reports_what_is_actually_on_the_page() -> None:
    result = verify_alt(PAGE, IMAGE, "Team at the office",
                        fetcher=page(f'<img src="{IMAGE}" alt="IMG_0042">'))
    assert result.status is VerifyStatus.mismatch
    assert result.found_alt == "IMG_0042"


def test_empty_alt_is_its_own_state() -> None:
    result = verify_alt(PAGE, IMAGE, "Team at the office",
                        fetcher=page(f'<img src="{IMAGE}" alt="">'))
    assert result.status is VerifyStatus.empty_alt
    assert result.found_alt == ""


def test_missing_alt_attribute_is_empty_alt_too() -> None:
    result = verify_alt(PAGE, IMAGE, "Team at the office",
                        fetcher=page(f'<img src="{IMAGE}">'))
    assert result.status is VerifyStatus.empty_alt


def test_image_not_found_on_page() -> None:
    result = verify_alt(PAGE, IMAGE, "Team at the office",
                        fetcher=page('<img src="/other/photo.jpg" alt="Other">'))
    assert result.status is VerifyStatus.image_not_found_on_page
    assert result.detail is not None and "JavaScript" in result.detail
    assert result.images_on_page == 1


def test_truncated_page_says_so_instead_of_claiming_absence() -> None:
    result = verify_alt(PAGE, IMAGE, "An alt",
                        fetcher=page("<img src='/x.jpg' alt='x'>", truncated=True))
    assert result.status is VerifyStatus.image_not_found_on_page
    assert result.detail is not None and "5 MB" in result.detail


# --- матчинг картинки ---------------------------------------------------------


def test_resized_variant_on_the_page_matches_the_original_in_the_report() -> None:
    html = f'<img src="{UPLOADS}/hero-image-1024x683.jpg" alt="Team at the office">'
    result = verify_alt(PAGE, IMAGE, "Team at the office", fetcher=page(html))
    assert result.status is VerifyStatus.match


def test_webp_converted_variant_matches() -> None:
    html = f'<img src="{UPLOADS}/hero-image-300x200.jpg.webp" alt="Team at the office">'
    assert verify_alt(PAGE, IMAGE, "Team at the office",
                      fetcher=page(html)).status is VerifyStatus.match


def test_relative_src_is_resolved_against_the_page() -> None:
    html = '<img src="/wp-content/uploads/2024/03/hero-image-150x150.jpg" alt="Team at the office">'
    assert verify_alt(PAGE, IMAGE, "Team at the office",
                      fetcher=page(html)).status is VerifyStatus.match


def test_lazy_loaded_src_attributes_are_checked() -> None:
    html = (
        '<img src="data:image/gif;base64,R0lGOD" '
        f'data-lazy-src="{UPLOADS}/hero-image.jpg" alt="Team at the office">'
    )
    assert verify_alt(PAGE, IMAGE, "Team at the office",
                      fetcher=page(html)).status is VerifyStatus.match


def test_srcset_entries_are_checked() -> None:
    html = (
        f'<img srcset="{UPLOADS}/hero-image-300x200.jpg 300w, '
        f'{UPLOADS}/hero-image-768x512.jpg 768w" alt="Team at the office">'
    )
    assert verify_alt(PAGE, IMAGE, "Team at the office",
                      fetcher=page(html)).status is VerifyStatus.match


def test_picture_source_without_img_cannot_confirm_an_alt() -> None:
    html = f'<picture><source srcset="{UPLOADS}/hero-image.webp"></picture>'
    result = verify_alt(PAGE, IMAGE, "Team at the office", fetcher=page(html))
    assert result.status is VerifyStatus.empty_alt
    assert result.detail is not None and "<source>" in result.detail


def test_repeated_image_counts_the_occurrence_that_already_has_the_alt() -> None:
    html = (
        f'<img src="{UPLOADS}/hero-image-150x150.jpg" alt="">'
        f'<img src="{UPLOADS}/hero-image.jpg" alt="Team at the office">'
    )
    result = verify_alt(PAGE, IMAGE, "Team at the office", fetcher=page(html))
    assert result.status is VerifyStatus.match
    assert result.candidates_matched == 2


def test_html_entities_in_alt_are_decoded_before_comparison() -> None:
    html = f'<img src="{IMAGE}" alt="Bob &amp; Alice&#39;s office">'
    result = verify_alt(PAGE, IMAGE, "Bob & Alice's office", fetcher=page(html))
    assert result.status is VerifyStatus.match


# --- помилки завантаження -----------------------------------------------------


def test_http_error_is_fetch_failed_with_the_status() -> None:
    def fetcher(url: str) -> FetchedPage:
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)  # type: ignore[arg-type]

    result = verify_alt(PAGE, IMAGE, "An alt", fetcher=fetcher)
    assert result.status is VerifyStatus.fetch_failed
    assert result.http_status == 404
    assert result.detail is not None and "404" in result.detail


def test_network_error_is_fetch_failed() -> None:
    def fetcher(url: str) -> FetchedPage:
        raise urllib.error.URLError("name or service not known")

    result = verify_alt(PAGE, IMAGE, "An alt", fetcher=fetcher)
    assert result.status is VerifyStatus.fetch_failed
    assert result.http_status is None


def test_timeout_is_fetch_failed() -> None:
    def fetcher(url: str) -> FetchedPage:
        raise TimeoutError

    result = verify_alt(PAGE, IMAGE, "An alt", fetcher=fetcher)
    assert result.status is VerifyStatus.fetch_failed
    assert result.detail is not None and "time" in result.detail


def test_unsafe_url_is_reported_not_requested() -> None:
    def fetcher(url: str) -> FetchedPage:
        raise UnsafeUrlError("127.0.0.1 resolves to a non-public address")

    result = verify_alt(PAGE, IMAGE, "An alt", fetcher=fetcher)
    assert result.status is VerifyStatus.fetch_failed


def test_image_url_without_a_filename_cannot_be_verified() -> None:
    result = verify_alt(PAGE, "data:image/png;base64,AAA", "An alt",
                        fetcher=page('<img src="/x.jpg" alt="x">'))
    assert result.status is VerifyStatus.fetch_failed
    assert result.detail is not None and "filename" in result.detail


def test_verify_requires_an_expected_alt() -> None:
    with pytest.raises(ValueError):
        verify_alt(PAGE, IMAGE, "   ", fetcher=page(""))


# --- SSRF-гард ----------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/x",
        "gopher://example.com",
        "https:///no-host",
    ],
)
def test_non_http_or_hostless_urls_are_refused(url: str) -> None:
    with pytest.raises(UnsafeUrlError):
        assert_public_http_url(url)


@pytest.mark.parametrize(
    "host",
    ["127.0.0.1", "localhost", "10.0.0.5", "192.168.1.10", "169.254.169.254", "[::1]"],
)
def test_private_and_loopback_targets_are_refused(
    host: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    literal = "127.0.0.1" if host == "localhost" else host.strip("[]")

    def fake_getaddrinfo(*_args: object, **_kwargs: object) -> list[tuple[object, ...]]:
        family = socket.AF_INET6 if ":" in literal else socket.AF_INET
        return [(family, socket.SOCK_STREAM, 6, "", (literal, 443))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(UnsafeUrlError):
        assert_public_http_url(f"https://{host}/page")


def test_public_target_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_getaddrinfo(*_args: object, **_kwargs: object) -> list[tuple[object, ...]]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    assert_public_http_url("https://example.com/page")


def test_unresolvable_host_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_getaddrinfo(*_args: object, **_kwargs: object) -> list[tuple[object, ...]]:
        raise OSError("nope")

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(UnsafeUrlError):
        assert_public_http_url("https://nowhere.invalid/page")


# --- допоміжне ----------------------------------------------------------------


def test_normalize_alt_collapses_whitespace() -> None:
    assert normalize_alt("  a \n b\tc ") == "a b c"


def test_find_page_images_reads_self_closing_and_uppercase_tags() -> None:
    images = find_page_images('<IMG SRC="/a.jpg" ALT="A" /><img src="/b.jpg">')
    assert [image.alt for image in images] == ["A", None]
