"""`app.urlkeywords.sitequeries.match_site_queries` — full token-coverage
matching of site-level GSC queries against one page's text (task-64 §3.3).
Deliberately NOT a per-page GSC feature — see the module docstring for the
two live probes that ruled that out.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.urlkeywords.sitequeries import match_site_queries


@dataclass
class _Row:
    dimension_value: str
    clicks: int | None
    impressions: int | None


class TestMatchSiteQueries:
    def test_no_page_tokens_matches_nothing(self):
        rows = [_Row("hearing aid cleaning", 10, 100)]
        assert match_site_queries(frozenset(), rows, brand_name="", domain="") == []

    def test_full_coverage_query_matches(self):
        page_tokens = frozenset({"hearing", "aid", "cleaning", "nyc"})
        rows = [_Row("hearing aid cleaning", 10, 100)]
        matches = match_site_queries(page_tokens, rows, brand_name="", domain="")
        assert [m.query for m in matches] == ["hearing aid cleaning"]

    def test_partial_coverage_query_is_excluded(self):
        # "price" never appears on the page — must not match on the
        # overlapping tokens alone (PLAN §12: never a fabricated match).
        page_tokens = frozenset({"hearing", "aid", "cleaning"})
        rows = [_Row("hearing aid price", 10, 100)]
        assert match_site_queries(page_tokens, rows, brand_name="", domain="") == []

    def test_results_sorted_by_impressions_descending(self):
        page_tokens = frozenset({"hearing", "aid", "cleaning", "repair", "cost"})
        rows = [
            _Row("hearing aid cleaning", 1, 50),
            _Row("hearing aid repair", 5, 500),
            _Row("hearing aid cost", 2, 200),
        ]
        matches = match_site_queries(page_tokens, rows, brand_name="", domain="")
        assert [m.query for m in matches] == [
            "hearing aid repair",
            "hearing aid cost",
            "hearing aid cleaning",
        ]

    def test_duplicate_queries_case_insensitive_are_deduped(self):
        page_tokens = frozenset({"hearing", "aid"})
        rows = [_Row("hearing aid", 1, 10), _Row("Hearing Aid", 2, 20)]
        matches = match_site_queries(page_tokens, rows, brand_name="", domain="")
        assert len(matches) == 1

    def test_limit_caps_result_count(self):
        page_tokens = frozenset({"aid"})
        rows = [_Row(f"aid {i}", 1, 100 - i) for i in range(20)]
        matches = match_site_queries(page_tokens, rows, brand_name="", domain="", limit=3)
        assert len(matches) == 3

    def test_brand_query_flag_uses_policy_brand_identity(self):
        # "hours" stems to "hour" (policy.topic_tokens) — page_tokens must
        # match the same stemmed form or full-coverage matching excludes it.
        page_tokens = frozenset({"nychearing", "hour"})
        rows = [_Row("nychearing hours", 3, 30)]
        matches = match_site_queries(
            page_tokens, rows, brand_name="NYC Hearing", domain="nychearing.com"
        )
        assert matches[0].is_brand is True

    def test_blank_and_empty_dimension_values_are_skipped(self):
        page_tokens = frozenset({"aid"})
        rows = [_Row("", 1, 10), _Row("   ", 1, 10)]
        assert match_site_queries(page_tokens, rows, brand_name="", domain="") == []
