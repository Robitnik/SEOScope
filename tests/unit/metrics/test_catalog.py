from app.metrics.catalog import CATALOG, DERIVED_RATIO_RULES, metrics_for
from app.schemas.enums import BetterDirection, Provider


def test_catalog_has_expected_keys():
    assert set(CATALOG) == {
        "ahrefs_health_score",
        "ahrefs_errors",
        "ahrefs_warnings",
        "ahrefs_urls_crawled",
        "semrush_authority_score",
        "semrush_organic_traffic",
        "semrush_organic_keywords",
        "semrush_referring_domains",
        "gsc_clicks",
        "gsc_impressions",
        "gsc_ctr",
        "gsc_avg_position",
        "rn_users",
        "rn_organic_users",
        "rn_total_conversions",
        "rn_organic_conversions",
    }


def test_metrics_for_returns_four_kpis_per_provider_in_card_order():
    assert [s.key for s in metrics_for(Provider.gsc)] == [
        "gsc_clicks",
        "gsc_impressions",
        "gsc_ctr",
        "gsc_avg_position",
    ]
    assert len(metrics_for(Provider.ahrefs)) == 4
    assert len(metrics_for(Provider.semrush)) == 4
    assert len(metrics_for(Provider.reporting_ninja)) == 4


def test_ahrefs_and_semrush_are_snapshot_gsc_and_rn_are_range():
    for spec in metrics_for(Provider.ahrefs) + metrics_for(Provider.semrush):
        assert spec.comparison_kind == "snapshot"
    for spec in metrics_for(Provider.gsc) + metrics_for(Provider.reporting_ninja):
        assert spec.comparison_kind == "range"


def test_direction_semantics_do_not_follow_sign_of_change():
    # Fewer Ahrefs errors is an improvement -> better_direction=down.
    assert CATALOG["ahrefs_errors"].better_direction == BetterDirection.down
    assert CATALOG["ahrefs_warnings"].better_direction == BetterDirection.down
    # More GSC clicks is an improvement -> better_direction=up.
    assert CATALOG["gsc_clicks"].better_direction == BetterDirection.up
    # Lower average position (closer to #1) is an improvement -> down.
    assert CATALOG["gsc_avg_position"].better_direction == BetterDirection.down
    # URLs crawled is informational, not inherently good or bad.
    assert CATALOG["ahrefs_urls_crawled"].better_direction == BetterDirection.neutral


def test_only_ctr_allows_derived_values():
    for key, spec in CATALOG.items():
        assert spec.allow_derived == (key == "gsc_ctr"), key


def test_precision_for_non_integer_metrics():
    assert CATALOG["gsc_ctr"].precision == 2
    assert CATALOG["gsc_avg_position"].precision == 1
    assert CATALOG["ahrefs_health_score"].precision == 0


def test_every_metric_has_freshness_sla():
    assert all(spec.freshness_sla_hours is not None for spec in CATALOG.values())


def test_derived_rules_reference_catalog_keys():
    for derived_key, (numerator_key, denominator_key) in DERIVED_RATIO_RULES.items():
        assert derived_key in CATALOG
        assert numerator_key in CATALOG
        assert denominator_key in CATALOG
