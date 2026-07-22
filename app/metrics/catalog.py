"""MVP metric catalog (PLAN §3.2): one `MetricSpec` per card KPI.

Keys are canonical and provider-prefixed (`ahrefs_health_score`, ...) — the
same strings are used end-to-end (catalog → API → frontend → filters →
error messages, per CLAUDE.md). Ordering within `CATALOG` is the display
order for a provider's row on the client card.
"""

from __future__ import annotations

from app.schemas.enums import BetterDirection, MetricUnit, Provider
from app.schemas.metrics import MetricSpec

# MVP default: collection is on-demand (week/month cadence), not continuous
# monitoring, so a flat 7-day freshness window applies to every metric until
# a later task motivates per-metric SLAs (PLAN §12).
_DEFAULT_FRESHNESS_SLA_HOURS = 24 * 7

CATALOG: dict[str, MetricSpec] = {
    spec.key: spec
    for spec in (
        # --- Ahrefs (snapshot: last collected value within the period) ----
        MetricSpec(
            key="ahrefs_health_score",
            label="Health Score",
            provider=Provider.ahrefs,
            unit=MetricUnit.score,
            comparison_kind="snapshot",
            better_direction=BetterDirection.up,
            freshness_sla_hours=_DEFAULT_FRESHNESS_SLA_HOURS,
        ),
        MetricSpec(
            key="ahrefs_errors",
            label="Errors",
            provider=Provider.ahrefs,
            unit=MetricUnit.count,
            comparison_kind="snapshot",
            better_direction=BetterDirection.down,
            freshness_sla_hours=_DEFAULT_FRESHNESS_SLA_HOURS,
        ),
        MetricSpec(
            key="ahrefs_warnings",
            label="Warnings",
            provider=Provider.ahrefs,
            unit=MetricUnit.count,
            comparison_kind="snapshot",
            better_direction=BetterDirection.down,
            freshness_sla_hours=_DEFAULT_FRESHNESS_SLA_HOURS,
        ),
        MetricSpec(
            key="ahrefs_urls_crawled",
            label="URLs Crawled",
            provider=Provider.ahrefs,
            unit=MetricUnit.count,
            comparison_kind="snapshot",
            better_direction=BetterDirection.neutral,
            freshness_sla_hours=_DEFAULT_FRESHNESS_SLA_HOURS,
        ),
        # --- Semrush (snapshot) --------------------------------------------
        MetricSpec(
            key="semrush_authority_score",
            label="Authority Score",
            provider=Provider.semrush,
            unit=MetricUnit.score,
            comparison_kind="snapshot",
            better_direction=BetterDirection.up,
            freshness_sla_hours=_DEFAULT_FRESHNESS_SLA_HOURS,
        ),
        MetricSpec(
            key="semrush_organic_traffic",
            label="Organic Traffic",
            provider=Provider.semrush,
            unit=MetricUnit.count,
            comparison_kind="snapshot",
            better_direction=BetterDirection.up,
            freshness_sla_hours=_DEFAULT_FRESHNESS_SLA_HOURS,
        ),
        MetricSpec(
            key="semrush_organic_keywords",
            label="Organic Keywords",
            provider=Provider.semrush,
            unit=MetricUnit.count,
            comparison_kind="snapshot",
            better_direction=BetterDirection.up,
            freshness_sla_hours=_DEFAULT_FRESHNESS_SLA_HOURS,
        ),
        MetricSpec(
            key="semrush_referring_domains",
            label="Referring Domains",
            provider=Provider.semrush,
            unit=MetricUnit.count,
            comparison_kind="snapshot",
            better_direction=BetterDirection.up,
            freshness_sla_hours=_DEFAULT_FRESHNESS_SLA_HOURS,
        ),
        # --- GSC (range) -----------------------------------------------------
        MetricSpec(
            key="gsc_clicks",
            label="Clicks",
            provider=Provider.gsc,
            unit=MetricUnit.count,
            comparison_kind="range",
            better_direction=BetterDirection.up,
            freshness_sla_hours=_DEFAULT_FRESHNESS_SLA_HOURS,
        ),
        MetricSpec(
            key="gsc_impressions",
            label="Impressions",
            provider=Provider.gsc,
            unit=MetricUnit.count,
            comparison_kind="range",
            better_direction=BetterDirection.up,
            freshness_sla_hours=_DEFAULT_FRESHNESS_SLA_HOURS,
        ),
        MetricSpec(
            key="gsc_ctr",
            label="CTR",
            provider=Provider.gsc,
            unit=MetricUnit.percent,
            comparison_kind="range",
            better_direction=BetterDirection.up,
            precision=2,
            allow_derived=True,
            freshness_sla_hours=_DEFAULT_FRESHNESS_SLA_HOURS,
        ),
        MetricSpec(
            key="gsc_avg_position",
            label="Avg Position",
            provider=Provider.gsc,
            unit=MetricUnit.position,
            comparison_kind="range",
            better_direction=BetterDirection.down,
            precision=1,
            freshness_sla_hours=_DEFAULT_FRESHNESS_SLA_HOURS,
        ),
        # --- Reporting Ninja / GA4 (range) -----------------------------------
        MetricSpec(
            key="rn_users",
            label="Users",
            provider=Provider.reporting_ninja,
            unit=MetricUnit.count,
            comparison_kind="range",
            better_direction=BetterDirection.up,
            freshness_sla_hours=_DEFAULT_FRESHNESS_SLA_HOURS,
        ),
        MetricSpec(
            key="rn_organic_users",
            label="Organic Users",
            provider=Provider.reporting_ninja,
            unit=MetricUnit.count,
            comparison_kind="range",
            better_direction=BetterDirection.up,
            freshness_sla_hours=_DEFAULT_FRESHNESS_SLA_HOURS,
        ),
        MetricSpec(
            key="rn_total_conversions",
            label="Total Conversions",
            provider=Provider.reporting_ninja,
            unit=MetricUnit.count,
            comparison_kind="range",
            better_direction=BetterDirection.up,
            freshness_sla_hours=_DEFAULT_FRESHNESS_SLA_HOURS,
        ),
        MetricSpec(
            key="rn_organic_conversions",
            label="Organic Conversions",
            provider=Provider.reporting_ninja,
            unit=MetricUnit.count,
            comparison_kind="range",
            better_direction=BetterDirection.up,
            freshness_sla_hours=_DEFAULT_FRESHNESS_SLA_HOURS,
        ),
    )
}

# Derived-metric rules: `derived_key -> (numerator_key, denominator_key)`.
# Only computed by `normalize.normalize_result` when both components are
# present with `quality == complete` for the same `period_role` (PLAN §12:
# "CTR = clicks / impressions лише якщо обидва complete").
DERIVED_RATIO_RULES: dict[str, tuple[str, str]] = {
    "gsc_ctr": ("gsc_clicks", "gsc_impressions"),
}


def metrics_for(provider: Provider) -> list[MetricSpec]:
    """Catalog rows for one provider, in card display order."""
    return [spec for spec in CATALOG.values() if spec.provider == provider]
