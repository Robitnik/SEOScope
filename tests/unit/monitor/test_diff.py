"""`diff_sitemap_resource` (task-38 follow-up): a fetch/parse failure that is
NOT a confirmed HTTP 404 must never read as "sitemap lost" — live regression
2026-08-13/14 on hearwellservices.com, where a single flaky check on a
plugin-redirected `/sitemap.xml` opened a `critical` alert that "resolved"
itself ~12h later on the next scheduled check, even though the sitemap was
never actually removed.
"""

from __future__ import annotations

from app.monitor.diff import DetectedChange, diff_sitemap_resource
from app.monitor.severity import CRITICAL, WARNING


def test_baseline_run_never_alerts():
    assert (
        diff_sitemap_resource(
            resource_url="https://acme.com/sitemap.xml",
            new_status=None,
            confirmed_gone=True,
            is_baseline_run=True,
        )
        == []
    )


def test_confirmed_404_is_sitemap_lost_critical():
    changes = diff_sitemap_resource(
        resource_url="https://acme.com/sitemap.xml",
        new_status=None,
        confirmed_gone=True,
        is_baseline_run=False,
    )
    assert [c.change_type for c in changes] == ["sitemap_lost"]
    assert changes[0].severity == CRITICAL


def test_fetch_failure_without_a_confirmed_404_is_unavailable_not_lost():
    """Timeout / connection error / blocked / unparseable body — anything that
    isn't the server itself answering 404 — must not read as "removed".
    """
    changes = diff_sitemap_resource(
        resource_url="https://acme.com/sitemap.xml",
        new_status=None,
        confirmed_gone=False,
        is_baseline_run=False,
    )
    assert [c.change_type for c in changes] == ["sitemap_unavailable"]
    assert changes[0].severity == WARNING


def test_explicit_404_status_is_also_sitemap_lost():
    changes = diff_sitemap_resource(
        resource_url="https://acme.com/sitemap.xml",
        new_status=404,
        confirmed_gone=False,
        is_baseline_run=False,
    )
    assert [c.change_type for c in changes] == ["sitemap_lost"]


def test_healthy_status_produces_no_change():
    assert (
        diff_sitemap_resource(
            resource_url="https://acme.com/sitemap.xml",
            new_status=200,
            confirmed_gone=False,
            is_baseline_run=False,
        )
        == []
    )


def test_detected_change_related_id_defaults_to_none():
    change = DetectedChange("sitemap_lost", CRITICAL, "sitemap", "https://acme.com/sitemap.xml")
    assert change.related_id is None
