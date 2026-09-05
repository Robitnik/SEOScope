"""`app.urlinspector.urlkey` — the trailing-slash fold that fixes 96% of
GSC<->GA4 matches on the real portfolio (task-61 §0.2), and the variant sets
each source query is built from."""

from __future__ import annotations

from app.urlinspector.urlkey import (
    ga4_landing_variants,
    inspector_key,
    lookup_variants,
)


class TestInspectorKey:
    def test_trailing_slash_folds_together(self):
        assert inspector_key("https://www.x.com/a/") == inspector_key("https://www.x.com/a")

    def test_root_path_stays_root(self):
        assert inspector_key("https://www.x.com/") == "x.com/"
        assert inspector_key("https://x.com") == "x.com/"

    def test_www_and_scheme_dropped(self):
        assert inspector_key("http://www.x.com/a") == inspector_key("https://x.com/a/")

    def test_query_string_preserved_not_folded_away(self):
        # LAXTON's real `?taxonomy=...` pages are distinct pages (task-59 §2) —
        # the slash fold must never also drop or reorder the query.
        a = inspector_key("https://x.com/a?taxonomy=1&term=2")
        b = inspector_key("https://x.com/a/?term=2&taxonomy=1")
        assert a == b  # normalize_url already sorts params; only slash differs
        assert "taxonomy=1" in a

    def test_tracking_params_dropped(self):
        assert inspector_key("https://x.com/a?utm_source=fb") == inspector_key("https://x.com/a")

    def test_different_pages_stay_different(self):
        assert inspector_key("https://x.com/a") != inspector_key("https://x.com/b")


class TestLookupVariants:
    def test_produces_eight_forms_for_non_root(self):
        variants = lookup_variants("https://www.x.com/a")
        assert len(variants) == 8
        assert "https://www.x.com/a" in variants
        assert "https://x.com/a" in variants
        assert "http://www.x.com/a/" in variants
        assert "http://x.com/a/" in variants

    def test_root_path_has_no_slash_duplicate(self):
        variants = lookup_variants("https://x.com/")
        # root is already "/" both ways -> {http,https} x {host,www.host} x {"/"} = 4
        assert len(variants) == 4
        assert all(v.endswith("/") and v.count("/") == 3 for v in variants)

    def test_query_string_kept_verbatim_not_multiplied(self):
        variants = lookup_variants("https://x.com/a?b=1")
        assert all(v.endswith("?b=1") for v in variants)
        assert len(variants) == 8

    def test_real_portfolio_pair_matches(self):
        # task-61 §0.2's own measured example.
        gsc_stored = "https://www.nysapphire.com/ny-sapphire-gentlemens-club-locations/sapphire-times-square/"
        operator_input = "https://nysapphire.com/ny-sapphire-gentlemens-club-locations/sapphire-times-square"
        assert gsc_stored in lookup_variants(operator_input)


class TestGa4LandingVariants:
    def test_root_is_single_slash(self):
        assert ga4_landing_variants("https://x.com/") == ["/"]
        assert ga4_landing_variants("https://x.com") == ["/"]

    def test_non_root_gives_both_slash_forms(self):
        variants = ga4_landing_variants("https://x.com/a")
        assert set(variants) == {"/a", "/a/"}

    def test_ignores_host_entirely(self):
        # GA4's own dimension is host-less; different hosts, same path.
        assert ga4_landing_variants("https://a.com/x") == ga4_landing_variants("https://b.com/x")
