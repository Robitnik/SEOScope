"""Ресурс `catalog://metrics` (план §8): семантика напрямку зміни."""

from __future__ import annotations

import json

import pytest

from app.mcp.server import mcp

_CATALOG = [
    {
        "key": "gsc_clicks",
        "label": "GSC Clicks",
        "provider": "gsc",
        "unit": "count",
        "comparison_kind": "range",
        "better_direction": "up",
        "precision": 0,
        "allow_derived": False,
        "freshness_sla_hours": 168,
        "min_quality_to_show": "partial",
        "health_weight": 1.0,
    },
    {
        "key": "gsc_bounce_rate",
        "label": "Bounce rate",
        "provider": "gsc",
        "unit": "percent",
        "comparison_kind": "range",
        "better_direction": "down",
        "precision": 1,
        "allow_derived": False,
        "freshness_sla_hours": 168,
        "min_quality_to_show": "partial",
        "health_weight": 1.0,
    },
]


@pytest.mark.asyncio
async def test_metrics_catalog_resource_carries_better_direction(fake_api) -> None:
    fake_api.json("GET", "/api/v1/metrics/catalog", _CATALOG)

    contents = await mcp.read_resource("catalog://metrics")
    body = json.loads(list(contents)[0].content)
    by_key = {row["key"]: row for row in body}

    # This is the whole point of the resource: a metric whose better_direction
    # is "down" improved when its value went down — the invariant the raw
    # report table (get_report_data) cannot express on its own.
    assert by_key["gsc_clicks"]["better_direction"] == "up"
    assert by_key["gsc_bounce_rate"]["better_direction"] == "down"
