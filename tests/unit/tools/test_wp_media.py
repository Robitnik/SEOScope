"""Табличні тести деривації пошукового фрагмента (task-27).

Кожен кейс — реальна форма URL, яку віддають WordPress, resize-плагіни,
WebP-конвертери та CDN. Це ядро інструмента: якщо фрагмент неправильний,
оператор не знайде файл у медіатеці, і вся решта не має сенсу.
"""

from __future__ import annotations

import pytest

from app.tools.alt_text.wp_media import (
    MAX_SEARCH_KEY_LENGTH,
    UrlKind,
    derive_search_key,
    derive_upload_location,
    strip_wp_suffixes,
)

SITE = "example.com"
UPLOADS = f"https://{SITE}/wp-content/uploads/2024/03"

# (url, очікуваний search_key, очікуваний url_kind)
CASES: list[tuple[str, str, UrlKind]] = [
    # --- оригінал і розміри WP ---
    (f"{UPLOADS}/hero-image.jpg", "hero-image", UrlKind.uploads),
    (f"{UPLOADS}/hero-image-300x200.jpg", "hero-image", UrlKind.uploads),
    (f"{UPLOADS}/hero-image-1024x683.jpeg", "hero-image", UrlKind.uploads),
    (f"{UPLOADS}/hero-image-150x150.png", "hero-image", UrlKind.uploads),
    # --- big-image / rotated / retina / WP-редактор ---
    (f"{UPLOADS}/hero-image-scaled.jpg", "hero-image", UrlKind.uploads),
    (f"{UPLOADS}/hero-image-rotated.jpg", "hero-image", UrlKind.uploads),
    (f"{UPLOADS}/hero-image@2x.png", "hero-image", UrlKind.uploads),
    (f"{UPLOADS}/hero-image-e1712345678901.jpg", "hero-image", UrlKind.uploads),
    # комбінація: розмір поверх редагованої копії поверх scaled
    (f"{UPLOADS}/hero-image-scaled-e1712345678901-768x512.jpg", "hero-image", UrlKind.uploads),
    # --- WebP-конвертери ---
    (f"{UPLOADS}/hero-image.jpg.webp", "hero-image", UrlKind.uploads),
    (f"{UPLOADS}/hero-image-300x200.jpg.webp", "hero-image", UrlKind.uploads),
    (
        f"https://{SITE}/wp-content/webp-express/doc-root/wp-content/uploads/2024/03/hero-image.jpg.webp",
        "hero-image",
        UrlKind.uploads,
    ),
    # --- CDN / resize-проксі ---
    (
        f"https://i0.wp.com/{SITE}/wp-content/uploads/2024/03/hero-image.jpg?resize=300,200&ssl=1",
        "hero-image",
        UrlKind.uploads,
    ),
    (
        f"https://{SITE}/cdn-cgi/image/width=300,quality=80/wp-content/uploads/2024/03/hero-image.jpg",
        "hero-image",
        UrlKind.uploads,
    ),
    (
        f"https://cdn.statically.io/img/{SITE}/wp-content/uploads/2024/03/hero-image.jpg",
        "hero-image",
        UrlKind.uploads,
    ),
    (
        f"https://images.weserv.nl/?url={SITE}%2Fwp-content%2Fuploads%2F2024%2F03%2Fhero-image.jpg&w=300",
        "hero-image",
        UrlKind.uploads,
    ),
    # --- query-параметри й фрагменти не входять у фрагмент ---
    (f"{UPLOADS}/hero-image.jpg?ver=1.2.3", "hero-image", UrlKind.uploads),
    (f"{UPLOADS}/hero-image.jpg#top", "hero-image", UrlKind.uploads),
    # --- percent-encoding і не-латиниця ---
    (
        f"{UPLOADS}/%D1%84%D0%BE%D1%82%D0%BE-%D0%BE%D1%84%D1%96%D1%81.jpg",
        "фото-офіс",
        UrlKind.uploads,
    ),
    (f"{UPLOADS}/photo%20with%20spaces.jpg", "photo with spaces", UrlKind.uploads),
    # --- дублікат завантаження: `-1` лишається у головному фрагменті ---
    (f"{UPLOADS}/hero-image-1.jpg", "hero-image-1", UrlKind.uploads),
    (f"{UPLOADS}/hero-image-1-300x200.jpg", "hero-image-1", UrlKind.uploads),
    # рік у назві — не лічильник дубліката (3+ цифри), фрагмент не ріжеться
    (f"{UPLOADS}/report-2024.png", "report-2024", UrlKind.uploads),
    # --- не медіатека ---
    (f"https://{SITE}/wp-content/themes/yovth/assets/img/logo.svg", "logo", UrlKind.theme_asset),
    (
        f"https://{SITE}/wp-content/plugins/slider/img/arrow-24x24.png",
        "arrow",
        UrlKind.plugin_asset,
    ),
    (f"https://{SITE}/images/static-banner.jpg", "static-banner", UrlKind.unknown),
    # --- чужий хост ---
    ("https://cdn.other-site.com/media/photo.jpg", "photo", UrlKind.external),
    # uploads-шлях, але чужий WP: фрагмент валідний, вид лишається uploads
    (
        "https://another-site.com/wp-content/uploads/2023/01/other-photo.jpg",
        "other-photo",
        UrlKind.uploads,
    ),
    # --- крайні випадки ---
    ("data:image/png;base64,iVBORw0KGgo=", "", UrlKind.unknown),
    (f"{UPLOADS}/", "", UrlKind.uploads),
    (f"{UPLOADS}/no-extension-file", "no-extension-file", UrlKind.uploads),
    # без схеми (бувають у аудитах: відносний або protocol-relative URL)
    ("/wp-content/uploads/2024/03/hero-image-400x300.jpg", "hero-image", UrlKind.uploads),
    (f"//{SITE}/wp-content/uploads/2024/03/hero-image.jpg", "hero-image", UrlKind.uploads),
    # зайві пробіли з клітинки таблиці
    (f"  {UPLOADS}/hero-image.jpg  ", "hero-image", UrlKind.uploads),
]


@pytest.mark.parametrize(("url", "expected_key", "expected_kind"), CASES)
def test_derive_search_key(url: str, expected_key: str, expected_kind: UrlKind) -> None:
    result = derive_search_key(url, site_host=SITE)
    assert result.search_key == expected_key
    assert result.url_kind is expected_kind


def test_empty_url_is_flagged_not_crashed() -> None:
    result = derive_search_key("   ")
    assert result.search_key == ""
    assert result.url_kind is UrlKind.unknown
    assert "empty_url" in result.warnings


def test_stripped_suffixes_are_reported_to_the_operator() -> None:
    result = derive_search_key(f"{UPLOADS}/hero-image-scaled-1024x683.jpg", site_host=SITE)
    assert result.search_key == "hero-image"
    assert "-1024x683" in result.stripped
    assert "-scaled" in result.stripped
    assert result.extension == "jpg"
    assert result.filename == "hero-image-scaled-1024x683.jpg"


def test_double_extension_is_reported() -> None:
    result = derive_search_key(f"{UPLOADS}/hero-image.jpg.webp", site_host=SITE)
    assert ".webp" in result.stripped
    # Розширення справжнього файлу — те, під яким він лежить у медіатеці.
    assert result.extension == "jpg"


def test_duplicate_counter_yields_a_shorter_fallback() -> None:
    result = derive_search_key(f"{UPLOADS}/hero-image-2-300x200.jpg", site_host=SITE)
    assert result.search_key == "hero-image-2"
    assert result.search_key_fallback == "hero-image"
    assert "possible_duplicate_counter" in result.warnings


def test_no_fallback_when_nothing_looks_like_a_counter() -> None:
    result = derive_search_key(f"{UPLOADS}/hero-image.jpg", site_host=SITE)
    assert result.search_key_fallback is None
    assert result.warnings == ()


def test_external_host_on_uploads_path_is_warned_but_still_searchable() -> None:
    result = derive_search_key(
        "https://another-site.com/wp-content/uploads/2023/01/photo.jpg", site_host=SITE
    )
    assert result.url_kind is UrlKind.uploads
    assert "external_host" in result.warnings
    assert result.is_searchable is True


def test_www_prefix_does_not_make_the_host_foreign() -> None:
    result = derive_search_key(f"https://www.{SITE}/wp-content/uploads/2024/03/a-photo.jpg",
                               site_host=SITE)
    assert "external_host" not in result.warnings


def test_subdomain_of_the_client_site_is_not_foreign() -> None:
    result = derive_search_key(
        f"https://cdn.{SITE}/wp-content/uploads/2024/03/a-photo.jpg", site_host=SITE
    )
    assert "external_host" not in result.warnings


def test_without_site_host_foreign_hosts_are_not_guessed() -> None:
    # Немає з чим порівнювати → не вигадуємо «external», лише чесний unknown.
    result = derive_search_key("https://cdn.other.com/media/photo.jpg")
    assert result.url_kind is UrlKind.unknown
    assert "not_a_media_library_path" in result.warnings


def test_theme_asset_is_not_searchable_in_the_media_library() -> None:
    result = derive_search_key(
        f"https://{SITE}/wp-content/themes/yovth/img/logo.png", site_host=SITE
    )
    assert result.url_kind is UrlKind.theme_asset
    assert result.is_searchable is False


def test_resize_query_is_reported_as_dropped() -> None:
    result = derive_search_key(f"{UPLOADS}/hero-image.jpg?w=300&h=200", site_host=SITE)
    assert "resize_query_dropped" in result.warnings


def test_unsupported_scheme_is_unknown() -> None:
    result = derive_search_key("ftp://example.com/photo.jpg", site_host=SITE)
    assert result.url_kind is UrlKind.unknown
    assert "unsupported_scheme" in result.warnings


def test_very_long_stem_is_truncated_with_a_warning() -> None:
    long_stem = "a" * (MAX_SEARCH_KEY_LENGTH + 50)
    result = derive_search_key(f"{UPLOADS}/{long_stem}.jpg", site_host=SITE)
    assert len(result.search_key) == MAX_SEARCH_KEY_LENGTH
    assert "search_key_truncated" in result.warnings


def test_too_short_key_is_flagged() -> None:
    result = derive_search_key(f"{UPLOADS}/x1-300x200.jpg", site_host=SITE)
    assert result.search_key == "x1"
    assert "search_key_too_short" in result.warnings
    assert result.is_searchable is False


@pytest.mark.parametrize(
    "stem",
    [
        "hero-image-300x200",
        "hero-image-scaled",
        "hero-image@2x",
        "hero-image-e1712345678901",
        "hero-image-scaled-e1712345678901-768x512",
        "hero-image",
    ],
)
def test_strip_wp_suffixes_is_idempotent(stem: str) -> None:
    once, first = strip_wp_suffixes(stem)
    twice, second = strip_wp_suffixes(once)
    assert twice == once
    assert second == []
    assert once == "hero-image"
    assert all(marker for marker in first)


def test_strip_never_eats_the_whole_name() -> None:
    # Файл, назва якого — суто розмір: знімати нічого, інакше шукати нічим.
    stem, stripped = strip_wp_suffixes("300x200")
    assert stem == "300x200"
    assert stripped == []


# --- derive_upload_location (task-39) ------------------------------------------


def test_upload_location_for_a_plain_original() -> None:
    location = derive_upload_location(f"{UPLOADS}/hero-image.jpg")
    assert location is not None
    assert location.wp_root == f"https://{SITE}"
    assert location.relative_path == "2024/03/hero-image.jpg"
    assert location.original_relative_path == "2024/03/hero-image.jpg"
    assert location.basename == "hero-image.jpg"
    assert location.original_basename == "hero-image.jpg"


def test_upload_location_strips_resize_suffix_from_the_original_path_only() -> None:
    location = derive_upload_location(f"{UPLOADS}/hero-image-300x200.jpg")
    assert location is not None
    # `relative_path` — те, що реально в URL (з суфіксом розміру).
    assert location.relative_path == "2024/03/hero-image-300x200.jpg"
    # `original_relative_path` — те, що WP тримає в `_wp_attached_file`.
    assert location.original_relative_path == "2024/03/hero-image.jpg"
    assert location.basename == "hero-image-300x200.jpg"
    assert location.original_basename == "hero-image.jpg"


def test_upload_location_root_is_flattened_for_subdirectory_installs() -> None:
    # Відомий ліміт (докстрінг `UploadLocation`): спільне з `derive_search_key`
    # розгортання «вкладеного uploads-шляху» не відрізняє підкаталог
    # інсталяції від WebP-Express-обгортки — обидва відсікаються однаково.
    location = derive_upload_location(
        f"https://{SITE}/blog/wp-content/uploads/2024/03/1-e1712345678901-1024x683.jpg"
    )
    assert location is not None
    assert location.wp_root == f"https://{SITE}"
    assert location.original_relative_path == "2024/03/1.jpg"


def test_upload_location_unwraps_cdn_proxy() -> None:
    location = derive_upload_location(
        f"https://i0.wp.com/{SITE}/wp-content/uploads/2024/03/hero-image.jpg?resize=300,200&ssl=1"
    )
    assert location is not None
    assert location.wp_root == f"https://{SITE}"
    assert location.original_relative_path == "2024/03/hero-image.jpg"


def test_upload_location_is_none_for_theme_assets() -> None:
    assert derive_upload_location(f"https://{SITE}/wp-content/themes/yovth/img/logo.png") is None


def test_upload_location_is_none_for_non_uploads_urls() -> None:
    assert derive_upload_location(f"https://{SITE}/images/static-banner.jpg") is None


def test_upload_location_is_none_for_empty_input() -> None:
    assert derive_upload_location("   ") is None


def test_upload_location_is_none_when_the_upload_root_is_bare_size() -> None:
    # Файл, назва якого — суто розмір: `strip_wp_suffixes` не знімає нічого,
    # тож `original_stem` лишається валідним, і фрагмент усе одно є.
    location = derive_upload_location(f"{UPLOADS}/300x200.jpg")
    assert location is not None
    assert location.original_relative_path == "2024/03/300x200.jpg"
