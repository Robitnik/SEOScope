"""Нормалізація вставленого списку посилань (task-29).

Вхід тут — не чистий список, а те, що оператор скопіював, тому тести описують
саме реальні форми вставки: колонка з експорту, markdown, bullet-и, коми.
"""

from __future__ import annotations

import pytest

from app.tools.redirects.input import (
    MAX_URLS,
    UrlInputError,
    normalize_url,
    parse_url_list,
)


class TestNormalizeUrl:
    def test_keeps_a_plain_url_as_is(self) -> None:
        entry = normalize_url("https://example.com/blog/post")
        assert entry.url == "https://example.com/blog/post"
        assert entry.notes == ()

    def test_adds_https_when_scheme_is_missing(self) -> None:
        entry = normalize_url("example.com/page")
        assert entry.url == "https://example.com/page"
        assert "scheme_added" in entry.notes

    def test_adds_https_to_protocol_relative_url(self) -> None:
        assert normalize_url("//example.com/page").url == "https://example.com/page"

    def test_bare_host_gets_root_path(self) -> None:
        assert normalize_url("https://example.com").url == "https://example.com/"

    def test_lowercases_host_but_not_path(self) -> None:
        entry = normalize_url("https://Example.COM/Blog/Post")
        assert entry.url == "https://example.com/Blog/Post"

    def test_strips_wrappers_and_trailing_punctuation(self) -> None:
        entry = normalize_url("«https://example.com/page».")
        assert entry.url == "https://example.com/page"
        assert "wrapper_removed" in entry.notes
        assert "trailing_punctuation_removed" in entry.notes

    def test_drops_fragment_and_says_so(self) -> None:
        entry = normalize_url("https://example.com/page#section")
        assert entry.url == "https://example.com/page"
        assert "fragment_dropped" in entry.notes

    def test_keeps_query_including_utm(self) -> None:
        entry = normalize_url("https://example.com/page?utm_source=news&x=1")
        assert entry.url == "https://example.com/page?utm_source=news&x=1"

    def test_encodes_non_ascii_path(self) -> None:
        entry = normalize_url("https://example.com/статті/1")
        assert entry.url == "https://example.com/%D1%81%D1%82%D0%B0%D1%82%D1%82%D1%96/1"

    def test_does_not_double_encode(self) -> None:
        already = "https://example.com/%D1%84%D0%BE%D1%82%D0%BE.html"
        assert normalize_url(already).url == already

    def test_encodes_idn_host(self) -> None:
        entry = normalize_url("https://приклад.укр/page")
        assert entry.url.startswith("https://xn--")
        assert "idn_encoded" in entry.notes

    @pytest.mark.parametrize(
        "raw",
        [
            "mailto:hi@example.com",
            "tel:+380441234567",
            "javascript:void(0)",
            "ftp://example.com/file",
            "data:text/html,<p>hi</p>",
        ],
    )
    def test_rejects_non_http_schemes(self, raw: str) -> None:
        with pytest.raises(UrlInputError) as info:
            normalize_url(raw)
        assert info.value.code == "unsupported_scheme"

    @pytest.mark.parametrize("raw", ["", "   ", "not a url", "http://", "intranet/page"])
    def test_rejects_non_urls(self, raw: str) -> None:
        with pytest.raises(UrlInputError) as info:
            normalize_url(raw)
        assert info.value.code == "invalid_url"

    def test_rejects_absurdly_long_url(self) -> None:
        with pytest.raises(UrlInputError):
            normalize_url("https://example.com/" + "a" * 3000)

    def test_is_idempotent(self) -> None:
        once = normalize_url("Example.com/Page#top").url
        assert normalize_url(once).url == once


class TestParseUrlList:
    def test_one_per_line(self) -> None:
        parsed = parse_url_list("https://a.example/1\nhttps://a.example/2\n")
        assert [entry.url for entry in parsed.entries] == [
            "https://a.example/1",
            "https://a.example/2",
        ]

    def test_comma_separated_in_one_line(self) -> None:
        parsed = parse_url_list("https://a.example/1, https://a.example/2")
        assert len(parsed.entries) == 2

    def test_strips_bullets_and_numbering(self) -> None:
        parsed = parse_url_list("- https://a.example/1\n2) https://a.example/2\n• https://a.example/3")
        assert len(parsed.entries) == 3

    def test_takes_target_from_markdown_link(self) -> None:
        parsed = parse_url_list("[old page](https://a.example/old)")
        assert parsed.entries[0].url == "https://a.example/old"

    def test_dedupes_and_counts_duplicates(self) -> None:
        parsed = parse_url_list("https://a.example/1\nhttps://a.example/1\nhttps://a.example/1")
        assert len(parsed.entries) == 1
        assert parsed.duplicate_count == 2

    def test_dedupe_is_after_normalization(self) -> None:
        parsed = parse_url_list("a.example/1\nhttps://A.example/1")
        assert len(parsed.entries) == 1

    def test_keeps_input_order(self) -> None:
        parsed = parse_url_list("https://b.example/\nhttps://a.example/")
        assert [entry.url for entry in parsed.entries] == [
            "https://b.example/",
            "https://a.example/",
        ]

    def test_rejected_rows_are_reported_not_dropped(self) -> None:
        parsed = parse_url_list("https://a.example/1\nmailto:hi@a.example\ngarbage line here")
        assert len(parsed.entries) == 1
        assert {row.error_code for row in parsed.rejected} == {"unsupported_scheme", "invalid_url"}

    def test_skips_blank_lines_and_comments(self) -> None:
        parsed = parse_url_list("\n\n# a note\nhttps://a.example/1\n")
        assert len(parsed.entries) == 1
        assert parsed.rejected == ()

    def test_caps_the_list_and_says_it_truncated(self) -> None:
        text = "\n".join(f"https://a.example/{index}" for index in range(MAX_URLS + 5))
        parsed = parse_url_list(text)
        assert len(parsed.entries) == MAX_URLS
        assert parsed.truncated is True
        assert parsed.found_count == MAX_URLS + 5

    def test_empty_text_gives_empty_result(self) -> None:
        parsed = parse_url_list("   \n\n")
        assert parsed.entries == ()
        assert parsed.truncated is False
