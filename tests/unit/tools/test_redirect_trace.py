"""Трейсер ланцюжка редіректів (task-29).

Мережа інжектується фейковим fetcher-ом (`_Server`), тож кожна форма ланцюжка з
таск-плану перевіряється детерміновано й без сокетів. Це той шар, від
правильності якого залежить сенс інструмента, тому кейсів багато й вони описують
**реальні** конфігурації, а не синтетику.
"""

from __future__ import annotations

import urllib.error

import pytest

from app.tools.http_safety import CACHE_BUSTER_PARAM, UnsafeUrlError
from app.tools.redirects.trace import (
    HopKind,
    Permanence,
    RawResponse,
    TraceOptions,
    Verdict,
    describe_change,
    same_page,
    trace_redirects,
)


class _Server:
    """Мінімальний фейк: URL → відповідь, плюс журнал запитів."""

    def __init__(self, routes: dict[str, RawResponse]) -> None:
        self.routes = routes
        self.requested: list[str] = []

    def __call__(self, url: str) -> RawResponse:
        self.requested.append(url)
        try:
            return self.routes[url]
        except KeyError:
            return RawResponse(status=404, headers=(("content-type", "text/html"),), body="<h1>404")


def redirect(location: str, status: int = 301) -> RawResponse:
    return RawResponse(status=status, headers=(("location", location),))


def page(body: str = "<title>Page</title>", status: int = 200) -> RawResponse:
    return RawResponse(status=status, headers=(("content-type", "text/html"),), body=body)


class TestHappyChains:
    def test_direct_200_has_no_redirects(self) -> None:
        server = _Server({"https://a.example/": page()})
        result = trace_redirects("https://a.example/", fetcher=server)
        assert result.verdict is Verdict.ok_direct
        assert result.hop_count == 0
        assert result.final_url == "https://a.example/"
        assert result.final_status == 200
        assert len(result.hops) == 1

    def test_single_redirect(self) -> None:
        server = _Server(
            {"https://a.example/old": redirect("https://a.example/new"),
             "https://a.example/new": page()}
        )
        result = trace_redirects("https://a.example/old", fetcher=server)
        assert result.verdict is Verdict.ok_redirect
        assert result.hop_count == 1
        assert [hop.status for hop in result.hops] == [301, 200]
        assert result.hops[0].permanence is Permanence.permanent
        assert result.hops[-1].permanence is None
        assert result.hops[-1].location is None

    def test_two_redirects_are_a_chain(self) -> None:
        server = _Server(
            {
                "http://a.example/old": redirect("https://a.example/old"),
                "https://a.example/old": redirect("https://a.example/new/"),
                "https://a.example/new/": page(),
            }
        )
        result = trace_redirects("http://a.example/old", fetcher=server)
        assert result.verdict is Verdict.chain
        assert result.hop_count == 2
        assert result.final_url == "https://a.example/new/"
        assert result.hops[0].changes == ("scheme_http_to_https",)
        assert [hop.index for hop in result.hops] == [1, 2, 3]

    @pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
    def test_all_redirect_statuses_are_followed(self, status: int) -> None:
        server = _Server(
            {"https://a.example/old": redirect("https://a.example/new", status),
             "https://a.example/new": page()}
        )
        result = trace_redirects("https://a.example/old", fetcher=server)
        assert result.hop_count == 1
        assert result.hops[0].status == status

    @pytest.mark.parametrize(
        ("status", "permanence"),
        [(301, Permanence.permanent), (308, Permanence.permanent),
         (302, Permanence.temporary), (307, Permanence.temporary), (303, Permanence.temporary)],
    )
    def test_permanence_per_status(self, status: int, permanence: Permanence) -> None:
        server = _Server(
            {"https://a.example/old": redirect("https://a.example/new", status),
             "https://a.example/new": page()}
        )
        result = trace_redirects("https://a.example/old", fetcher=server)
        assert result.hops[0].permanence is permanence

    def test_relative_location_is_resolved(self) -> None:
        server = _Server(
            {"https://a.example/blog/old": redirect("/blog/new/"),
             "https://a.example/blog/new/": page()}
        )
        result = trace_redirects("https://a.example/blog/old", fetcher=server)
        assert result.final_url == "https://a.example/blog/new/"

    def test_location_relative_to_directory(self) -> None:
        server = _Server(
            {"https://a.example/blog/old": redirect("new-post"),
             "https://a.example/blog/new-post": page()}
        )
        result = trace_redirects("https://a.example/blog/old", fetcher=server)
        assert result.final_url == "https://a.example/blog/new-post"

    def test_multiple_location_headers_take_the_first_and_flag_it(self) -> None:
        server = _Server(
            {
                "https://a.example/old": RawResponse(
                    status=301,
                    headers=(("location", "https://a.example/new"), ("location", "https://a.example/other")),
                ),
                "https://a.example/new": page(),
            }
        )
        result = trace_redirects("https://a.example/old", fetcher=server)
        assert result.final_url == "https://a.example/new"
        assert "multiple_location_headers" in result.flags

    def test_only_final_hop_body_is_read(self) -> None:
        server = _Server(
            {"https://a.example/old": redirect("https://a.example/new"),
             "https://a.example/new": page("<title>Final</title>")}
        )
        result = trace_redirects("https://a.example/old", fetcher=server)
        assert result.final_page is not None
        assert result.final_page.title == "Final"


class TestBrokenChains:
    def test_loop_is_its_own_verdict(self) -> None:
        server = _Server(
            {"https://a.example/a": redirect("https://a.example/b"),
             "https://a.example/b": redirect("https://a.example/a")}
        )
        result = trace_redirects("https://a.example/a", fetcher=server)
        assert result.verdict is Verdict.loop
        assert result.error_code == "redirect_loop"
        # Хопи, які встигли пройти, лишаються видимими.
        assert result.hop_count == 2

    def test_self_loop(self) -> None:
        server = _Server({"https://a.example/a": redirect("https://a.example/a")})
        result = trace_redirects("https://a.example/a", fetcher=server)
        assert result.verdict is Verdict.loop

    def test_too_many_hops(self) -> None:
        routes = {
            f"https://a.example/{index}": redirect(f"https://a.example/{index + 1}")
            for index in range(40)
        }
        server = _Server(routes)
        result = trace_redirects(
            "https://a.example/0", options=TraceOptions(max_hops=5), fetcher=server
        )
        assert result.verdict is Verdict.too_many_hops
        assert result.error_code == "too_many_hops"

    def test_redirect_without_location_is_broken_chain(self) -> None:
        server = _Server({"https://a.example/old": RawResponse(status=301)})
        result = trace_redirects("https://a.example/old", fetcher=server)
        assert result.verdict is Verdict.broken_chain
        assert result.hops[-1].notes == ("missing_location",)
        assert result.final_status is None

    def test_final_404_is_broken(self) -> None:
        server = _Server(
            {"https://a.example/old": redirect("https://a.example/gone"),
             "https://a.example/gone": page("<h1>Not found", status=404)}
        )
        result = trace_redirects("https://a.example/old", fetcher=server)
        assert result.verdict is Verdict.broken
        assert result.final_status == 404
        assert result.hop_count == 1

    @pytest.mark.parametrize("status", [401, 403, 407, 429])
    def test_bot_protection_is_blocked_not_broken(self, status: int) -> None:
        server = _Server({"https://a.example/": page("<h1>Denied", status=status)})
        result = trace_redirects("https://a.example/", fetcher=server)
        assert result.verdict is Verdict.blocked

    @pytest.mark.parametrize("status", [500, 502, 503])
    def test_server_errors_are_broken(self, status: int) -> None:
        server = _Server({"https://a.example/": page("boom", status=status)})
        result = trace_redirects("https://a.example/", fetcher=server)
        assert result.verdict is Verdict.broken

    def test_redirect_to_mailto_is_unsupported_scheme(self) -> None:
        server = _Server({"https://a.example/contact": redirect("mailto:hi@a.example")})
        result = trace_redirects("https://a.example/contact", fetcher=server)
        assert result.verdict is Verdict.unsupported_scheme
        assert result.hop_count == 1

    def test_unsupported_input_scheme_never_touches_network(self) -> None:
        server = _Server({})
        result = trace_redirects("mailto:hi@a.example", fetcher=server)
        assert result.verdict is Verdict.unsupported_scheme
        assert server.requested == []

    def test_unsafe_url_is_its_own_verdict(self) -> None:
        def guard(url: str) -> RawResponse:
            raise UnsafeUrlError("127.0.0.1 resolves to a non-public address (127.0.0.1).")

        result = trace_redirects("https://localhost/admin", fetcher=guard)
        assert result.verdict is Verdict.unsafe_url
        assert result.error_code == "unsafe_url"

    def test_unsafe_redirect_target_stops_the_chain_with_hops_kept(self) -> None:
        def guard(url: str) -> RawResponse:
            if url == "https://a.example/old":
                return redirect("http://169.254.169.254/latest/meta-data/")
            raise UnsafeUrlError("169.254.169.254 resolves to a non-public address.")

        result = trace_redirects("https://a.example/old", fetcher=guard)
        assert result.verdict is Verdict.unsafe_url
        assert result.hop_count == 1
        assert result.hops[0].location == "http://169.254.169.254/latest/meta-data/"

    def test_dns_failure_is_fetch_failed_without_leaking_the_query(self) -> None:
        def broken(url: str) -> RawResponse:
            raise urllib.error.URLError("Name or service not known")

        result = trace_redirects("https://a.example/page?token=secret", fetcher=broken)
        assert result.verdict is Verdict.fetch_failed
        assert result.error_detail is not None
        assert "secret" not in result.error_detail
        assert "a.example" in result.error_detail

    def test_timeout_is_fetch_failed(self) -> None:
        def slow(url: str) -> RawResponse:
            raise TimeoutError

        result = trace_redirects("https://a.example/", fetcher=slow)
        assert result.verdict is Verdict.fetch_failed
        assert result.error_code == "timeout"


class TestMetaRefresh:
    HTML = '<meta http-equiv="refresh" content="0;url=https://a.example/new">'

    def test_meta_refresh_continues_the_chain(self) -> None:
        server = _Server(
            {"https://a.example/old": page(self.HTML), "https://a.example/new": page()}
        )
        result = trace_redirects("https://a.example/old", fetcher=server)
        assert result.hop_count == 1
        assert result.hops[0].kind is HopKind.meta_refresh
        assert result.hops[0].permanence is None
        assert "meta_refresh_used" in result.flags
        assert result.final_url == "https://a.example/new"
        assert result.verdict is Verdict.ok_redirect

    def test_meta_refresh_can_be_switched_off(self) -> None:
        server = _Server(
            {"https://a.example/old": page(self.HTML), "https://a.example/new": page()}
        )
        result = trace_redirects(
            "https://a.example/old",
            options=TraceOptions(follow_meta_refresh=False),
            fetcher=server,
        )
        assert result.verdict is Verdict.ok_direct
        assert result.final_url == "https://a.example/old"

    def test_relative_meta_refresh(self) -> None:
        server = _Server(
            {
                "https://a.example/old": page('<meta http-equiv="refresh" content="3; url=/new/">'),
                "https://a.example/new/": page(),
            }
        )
        result = trace_redirects("https://a.example/old", fetcher=server)
        assert result.final_url == "https://a.example/new/"

    def test_meta_refresh_loop(self) -> None:
        server = _Server(
            {"https://a.example/a": page('<meta http-equiv="refresh" content="0;url=/a">')}
        )
        result = trace_redirects("https://a.example/a", fetcher=server)
        assert result.verdict is Verdict.loop

    def test_meta_refresh_on_404_is_not_followed(self) -> None:
        server = _Server({"https://a.example/old": page(self.HTML, status=404)})
        result = trace_redirects("https://a.example/old", fetcher=server)
        assert result.verdict is Verdict.broken


class TestFinalPageFacts:
    def test_title_and_canonical_are_collected(self) -> None:
        body = (
            "<title> New page </title>"
            '<link rel="canonical" href="https://a.example/new">'
        )
        server = _Server({"https://a.example/new": page(body)})
        result = trace_redirects("https://a.example/new", fetcher=server)
        assert result.final_page is not None
        assert result.final_page.title == "New page"
        assert result.final_page.canonical == "https://a.example/new"
        assert "canonical_mismatch" not in result.flags

    def test_canonical_pointing_elsewhere_is_flagged(self) -> None:
        body = '<link rel="canonical" href="https://a.example/other">'
        server = _Server({"https://a.example/new": page(body)})
        result = trace_redirects("https://a.example/new", fetcher=server)
        assert "canonical_mismatch" in result.flags

    def test_relative_canonical_matching_final_url_is_not_flagged(self) -> None:
        server = _Server({"https://a.example/new/": page('<link rel="canonical" href="/new">')})
        result = trace_redirects("https://a.example/new/", fetcher=server)
        assert "canonical_mismatch" not in result.flags

    def test_noindex_destination_is_flagged(self) -> None:
        body = '<meta name="robots" content="noindex,follow">'
        server = _Server({"https://a.example/x": page(body)})
        result = trace_redirects("https://a.example/x", fetcher=server)
        assert "noindex_destination" in result.flags

    def test_js_redirect_is_a_hint_not_a_verdict(self) -> None:
        body = '<script>window.location.href = "/y";</script>'
        server = _Server({"https://a.example/x": page(body)})
        result = trace_redirects("https://a.example/x", fetcher=server)
        assert "possible_js_redirect" in result.flags
        assert result.verdict is Verdict.ok_direct

    def test_page_without_head_facts_reports_none(self) -> None:
        server = _Server({"https://a.example/x": page("<h1>Hi</h1>")})
        result = trace_redirects("https://a.example/x", fetcher=server)
        assert result.final_page is None


class TestFlags:
    def _trace(self, routes: dict[str, RawResponse], start: str):
        return trace_redirects(start, fetcher=_Server(routes))

    def test_temporary_redirect_flag(self) -> None:
        result = self._trace(
            {"https://a.example/old": redirect("https://a.example/new", 302),
             "https://a.example/new": page()},
            "https://a.example/old",
        )
        assert "temporary_redirect" in result.flags
        assert "mixed_permanence" not in result.flags

    def test_mixed_permanence_flag(self) -> None:
        result = self._trace(
            {
                "https://a.example/1": redirect("https://a.example/2", 302),
                "https://a.example/2": redirect("https://a.example/3", 301),
                "https://a.example/3": page(),
            },
            "https://a.example/1",
        )
        assert "mixed_permanence" in result.flags

    def test_protocol_downgrade_flag(self) -> None:
        result = self._trace(
            {"https://a.example/x": redirect("http://a.example/x"), "http://a.example/x": page()},
            "https://a.example/x",
        )
        assert "protocol_downgrade" in result.flags

    def test_host_change_flag(self) -> None:
        result = self._trace(
            {"https://a.example/x": redirect("https://b.example/x"), "https://b.example/x": page()},
            "https://a.example/x",
        )
        assert "host_change" in result.flags
        assert "www_change" not in result.flags

    def test_www_change_is_not_host_change(self) -> None:
        result = self._trace(
            {"https://a.example/x": redirect("https://www.a.example/x"),
             "https://www.a.example/x": page()},
            "https://a.example/x",
        )
        assert "www_change" in result.flags
        assert "host_change" not in result.flags

    def test_trailing_slash_only_flag(self) -> None:
        result = self._trace(
            {"https://a.example/x": redirect("https://a.example/x/"),
             "https://a.example/x/": page()},
            "https://a.example/x",
        )
        assert "trailing_slash_only" in result.flags

    def test_query_dropped_flag(self) -> None:
        result = self._trace(
            {"https://a.example/x?utm_source=news": redirect("https://a.example/x"),
             "https://a.example/x": page()},
            "https://a.example/x?utm_source=news",
        )
        assert "query_dropped" in result.flags

    def test_redirect_to_homepage_flag(self) -> None:
        result = self._trace(
            {"https://a.example/blog/post": redirect("https://a.example/"),
             "https://a.example/": page()},
            "https://a.example/blog/post",
        )
        assert "redirect_to_homepage" in result.flags

    def test_homepage_to_homepage_is_not_flagged(self) -> None:
        result = self._trace(
            {"http://a.example/": redirect("https://a.example/"), "https://a.example/": page()},
            "http://a.example/",
        )
        assert "redirect_to_homepage" not in result.flags

    def test_normalized_input_flag(self) -> None:
        result = self._trace({"https://a.example/x": page()}, "a.example/x")
        assert "normalized_input" in result.flags

    def test_no_flags_for_a_clean_direct_hit(self) -> None:
        result = self._trace({"https://a.example/x": page()}, "https://a.example/x")
        assert result.flags == ()


class TestOptions:
    def test_cache_buster_is_off_by_default(self) -> None:
        server = _Server({"https://a.example/x": page()})
        trace_redirects("https://a.example/x", fetcher=server)
        assert server.requested == ["https://a.example/x"]

    def test_cache_buster_can_be_enabled(self) -> None:
        busted = f"https://a.example/x?{CACHE_BUSTER_PARAM}=abcd1234"
        server = _Server({busted: page()})
        result = trace_redirects(
            "https://a.example/x",
            options=TraceOptions(cache_buster=True),
            fetcher=server,
            nonce="abcd1234",
        )
        assert server.requested == [busted]
        assert "cache_buster_used" in result.flags
        assert result.verdict is Verdict.ok_direct

    def test_cache_buster_is_dropped_when_it_breaks_the_url(self) -> None:
        """Реальний кейс: сайт віддає 404 на будь-який невідомий query."""
        clean = "https://a.example/vip/bachelor-parties/"

        def picky(url: str) -> RawResponse:
            if "?" in url:
                return page("<title>Page not found</title>", status=404)
            return page("<title>Bachelor parties</title>")

        result = trace_redirects(
            clean, options=TraceOptions(cache_buster=True), fetcher=picky, nonce="abcd1234"
        )
        assert result.verdict is Verdict.ok_direct
        assert result.final_status == 200
        assert result.final_url == clean
        # Чесно кажемо, що саме cache-buster дав хибний 404.
        assert "cache_buster_broke_the_url" in result.flags
        assert "cache_buster_used" not in result.flags

    def test_a_real_404_survives_the_fallback(self) -> None:
        """Якщо сторінка мертва й без параметра — вердикт лишається `broken`."""
        server = _Server({})  # усе відповідає 404
        result = trace_redirects(
            "https://a.example/gone", options=TraceOptions(cache_buster=True), fetcher=server
        )
        assert result.verdict is Verdict.broken
        assert "cache_buster_broke_the_url" not in result.flags
        assert "cache_buster_used" in result.flags

    def test_no_fallback_when_the_buster_is_off(self) -> None:
        server = _Server({})
        result = trace_redirects("https://a.example/gone", fetcher=server)
        assert result.verdict is Verdict.broken
        assert server.requested == ["https://a.example/gone"]

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [({}, 20), ({"max_hops": 3}, 3), ({"max_hops": 999}, 25), ({"max_hops": 0}, 1),
         ({"max_hops": "x"}, 20)],
    )
    def test_max_hops_is_clamped(self, raw: dict[str, object], expected: int) -> None:
        assert TraceOptions.clamped(raw).max_hops == expected

    def test_options_round_trip_through_as_dict(self) -> None:
        options = TraceOptions.clamped({"max_hops": 7, "follow_meta_refresh": False})
        assert TraceOptions.clamped(options.as_dict()) == options


class TestChangeDescription:
    @pytest.mark.parametrize(
        ("source", "target", "expected"),
        [
            ("http://a.example/x", "https://a.example/x", "scheme_http_to_https"),
            ("https://a.example/x", "http://a.example/x", "scheme_https_to_http"),
            ("https://a.example/x", "https://www.a.example/x", "www_added"),
            ("https://www.a.example/x", "https://a.example/x", "www_removed"),
            ("https://a.example/x", "https://b.example/x", "host_change"),
            ("https://a.example/x", "https://a.example/x/", "trailing_slash_added"),
            ("https://a.example/x/", "https://a.example/x", "trailing_slash_removed"),
            ("https://a.example/X", "https://a.example/x", "case_changed"),
            ("https://a.example/x", "https://a.example/y", "path_change"),
            ("https://a.example/x?a=1", "https://a.example/x", "query_dropped"),
            ("https://a.example/x", "https://a.example/x?a=1", "query_added"),
            ("https://a.example/x?a=1", "https://a.example/x?a=2", "query_changed"),
        ],
    )
    def test_single_changes(self, source: str, target: str, expected: str) -> None:
        assert expected in describe_change(source, target)

    def test_identical_urls_have_no_changes(self) -> None:
        assert describe_change("https://a.example/x", "https://a.example/x") == ()

    def test_several_changes_at_once(self) -> None:
        changes = describe_change("http://a.example/x?a=1", "https://www.a.example/x/")
        assert set(changes) == {
            "scheme_http_to_https",
            "www_added",
            "trailing_slash_added",
            "query_dropped",
        }


class TestSamePage:
    @pytest.mark.parametrize(
        ("left", "right"),
        [
            ("https://a.example/x", "https://a.example/x/"),
            ("https://a.example/x", "https://www.a.example/x"),
            ("http://a.example/x", "https://a.example/x"),
        ],
    )
    def test_equivalent_urls(self, left: str, right: str) -> None:
        assert same_page(left, right) is True

    @pytest.mark.parametrize(
        ("left", "right"),
        [
            ("https://a.example/x", "https://a.example/y"),
            ("https://a.example/x", "https://b.example/x"),
            ("https://a.example/x?a=1", "https://a.example/x"),
            (None, "https://a.example/x"),
        ],
    )
    def test_different_urls(self, left: str | None, right: str) -> None:
        assert same_page(left, right) is False


def test_trace_is_deterministic_for_the_same_server() -> None:
    routes = {
        "https://a.example/old": redirect("https://a.example/new"),
        "https://a.example/new": page("<title>New</title>"),
    }
    first = trace_redirects("https://a.example/old", fetcher=_Server(routes))
    second = trace_redirects("https://a.example/old", fetcher=_Server(routes))
    assert [hop.as_dict() | {"elapsed_ms": 0} for hop in first.hops] == [
        hop.as_dict() | {"elapsed_ms": 0} for hop in second.hops
    ]
    assert (first.verdict, first.flags, first.final_url) == (
        second.verdict,
        second.flags,
        second.final_url,
    )
