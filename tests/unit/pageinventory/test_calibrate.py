"""`app.pageinventory.calibrate` — duplicate clustering and length-floor
severity downgrade, `calibrated` vs `strict` (task-57 §2.3)."""

from __future__ import annotations

from app.contentqa.findings import Finding
from app.pageinventory.calibrate import build_clusters, calibrate
from app.pageinventory.qa import FieldFinding


def _dup_finding(ref: str, value: str = "shared text") -> Finding:
    return Finding(
        check="meta-desc-duplicate", severity="error", ref=ref,
        detail="Descriptions 1.00 similar.", value=value,
    )


def test_three_way_cluster_collapses_to_one_finding_per_row_in_calibrated_mode() -> None:
    # 3 pages sharing one description produce 3 PAIRWISE findings from
    # `app.contentqa` (a-b, a-c, b-c) — exactly the O(n^2) shape task-57 §1.4
    # measured live (3060 findings for 23 real clusters).
    raw = [
        _dup_finding("https://acme.com/a/, https://acme.com/b/"),
        _dup_finding("https://acme.com/a/, https://acme.com/c/"),
        _dup_finding("https://acme.com/b/, https://acme.com/c/"),
    ]
    per_row = {
        "https://acme.com/a/": [FieldFinding(field="description", finding=raw[0]),
                                 FieldFinding(field="description", finding=raw[1])],
        "https://acme.com/b/": [FieldFinding(field="description", finding=raw[0]),
                                 FieldFinding(field="description", finding=raw[2])],
        "https://acme.com/c/": [FieldFinding(field="description", finding=raw[1]),
                                 FieldFinding(field="description", finding=raw[2])],
    }

    calibrated_rows, clusters = calibrate(per_row, raw, profile="calibrated")

    assert len(clusters) == 1
    assert clusters[0].members == (
        "https://acme.com/a/", "https://acme.com/b/", "https://acme.com/c/",
    )
    for url in per_row:
        dup_findings = [
            ff for ff in calibrated_rows[url] if ff.finding.check == "meta-desc-duplicate"
        ]
        assert len(dup_findings) == 1
        assert "3 pages share this description" in dup_findings[0].finding.detail


def test_strict_mode_leaves_every_pairwise_finding_untouched() -> None:
    raw = [
        _dup_finding("https://acme.com/a/, https://acme.com/b/"),
        _dup_finding("https://acme.com/a/, https://acme.com/c/"),
        _dup_finding("https://acme.com/b/, https://acme.com/c/"),
    ]
    per_row = {
        "https://acme.com/a/": [FieldFinding(field="description", finding=raw[0]),
                                 FieldFinding(field="description", finding=raw[1])],
    }
    strict_rows, clusters = calibrate(per_row, raw, profile="strict")
    assert len(strict_rows["https://acme.com/a/"]) == 2
    # Cluster count is profile-independent — it's the ground truth of how
    # many DISTINCT duplicate groups exist, computed unconditionally
    # (task-57 §2.3), even though strict never shows the collapsed form.
    assert len(clusters) == 1


def test_title_floor_miss_downgraded_to_info_in_calibrated_only() -> None:
    short_title = Finding(
        check="meta-title-length", severity="warn", ref="https://acme.com/",
        detail="Title is 30 chars; floor is 50.", value="Short title",
    )
    per_row = {"https://acme.com/": [FieldFinding(field="title", finding=short_title)]}

    calibrated_rows, _ = calibrate(per_row, [], profile="calibrated")
    strict_rows, _ = calibrate(per_row, [], profile="strict")

    assert calibrated_rows["https://acme.com/"][0].finding.severity == "info"
    assert strict_rows["https://acme.com/"][0].finding.severity == "warn"


def test_title_ceiling_miss_stays_error_in_both_profiles() -> None:
    long_title = Finding(
        check="meta-title-length", severity="error", ref="https://acme.com/",
        detail="Title is 90 chars; ceiling is 60.", value="x" * 90,
    )
    per_row = {"https://acme.com/": [FieldFinding(field="title", finding=long_title)]}

    calibrated_rows, _ = calibrate(per_row, [], profile="calibrated")
    strict_rows, _ = calibrate(per_row, [], profile="strict")

    assert calibrated_rows["https://acme.com/"][0].finding.severity == "error"
    assert strict_rows["https://acme.com/"][0].finding.severity == "error"


def test_build_clusters_ignores_non_duplicate_checks() -> None:
    other = Finding(
        check="slop-phrase", severity="warn", ref="https://acme.com/a/, https://acme.com/b/",
        detail="not a duplicate check", value=None,
    )
    assert build_clusters([other]) == []
