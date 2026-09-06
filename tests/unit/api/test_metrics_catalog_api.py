"""`GET /api/v1/metrics/catalog` (task-74 §8): read-only, no DB, no params —
the REST surface `app/mcp/resources.py`'s `catalog://metrics` resource
reads through, instead of importing `app.metrics.CATALOG` directly."""

from __future__ import annotations

from app.metrics import CATALOG


def test_metrics_catalog_matches_the_in_process_catalog(client):
    response = client.get("/api/v1/metrics/catalog")
    assert response.status_code == 200
    body = response.json()

    assert len(body) == len(CATALOG)
    by_key = {row["key"]: row for row in body}
    assert set(by_key) == set(CATALOG)

    # `better_direction` is the whole point of this endpoint (task-74 plan
    # §8) — a smoke check that it round-trips, not just the key list.
    sample_key = next(iter(CATALOG))
    assert by_key[sample_key]["better_direction"] == CATALOG[sample_key].better_direction.value
