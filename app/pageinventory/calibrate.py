"""Post-processor over `app.pageinventory.qa` output (task-57 §2.3): two
QA profiles, `calibrated` (default) and `strict`.

Neither rewrites a single `app.contentqa` rule. Two things change:

1. **Duplicate clustering.** `meta-title-duplicate`/`meta-desc-duplicate`
   fire pairwise (task-53's `check_meta_duplicates` is an O(n^2) nested
   loop written for a 9-row strategy sheet) — on a 443-row site that is
   3060 findings for what is actually 23 real clusters (task-57 §1.4,
   live-measured). `calibrated` collapses each cluster to ONE finding per
   member row; `strict` leaves every pairwise finding as `app.contentqa`
   produced it.
2. **Severity of a length FLOOR miss.** `TITLE_MIN`/`DESC_MIN` are
   `app.contentqa.rules` constants tuned for a hand-written strategy
   document, not a product catalog — 404 of 443 rows on the live e-commerce
   client "violate" the title floor (task-57 §1.4). `calibrated` downgrades
   a floor miss (`warn`) to `info`; a CEILING miss (`error` — the title/desc
   actually gets truncated in SERP) is never touched, in either profile.

Nothing is ever hidden: `calibrated` changes weight and grouping, never the
underlying finding set (task-57 §2.3) — `build_clusters()` runs
unconditionally so the UI's "N duplicate groups" stat and the XLSX legend
are correct regardless of which profile is currently displayed.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Literal

from app.contentqa.findings import Finding
from app.pageinventory.qa import FIELD_BY_CHECK, FieldFinding

Profile = Literal["calibrated", "strict"]

_DUPLICATE_CHECKS = ("meta-title-duplicate", "meta-desc-duplicate")
_LENGTH_CHECKS = ("meta-title-length", "meta-desc-length")


@dataclass(frozen=True)
class Cluster:
    check: str  # "meta-title-duplicate" | "meta-desc-duplicate"
    field: str  # "title" | "description"
    members: tuple[str, ...]  # URLs, sorted
    sample_value: str | None


class _UnionFind:
    def __init__(self) -> None:
        self._parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self._parent.setdefault(x, x)
        root = x
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[x] != root:
            self._parent[x], x = root, self._parent[x]
        return root

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[ra] = rb


def build_clusters(raw_findings: list[Finding]) -> list[Cluster]:
    clusters: list[Cluster] = []
    for check in _DUPLICATE_CHECKS:
        pairs = [
            (parts[0].strip(), parts[1].strip(), f.value)
            for f in raw_findings
            if f.check == check
            for parts in [f.ref.split(",")]
            if len(parts) == 2
        ]
        if not pairs:
            continue
        uf = _UnionFind()
        sample_by_url: dict[str, str] = {}
        for a, b, value in pairs:
            uf.union(a, b)
            if value:
                sample_by_url.setdefault(a, value)
                sample_by_url.setdefault(b, value)
        groups: dict[str, set[str]] = defaultdict(set)
        for a, b, _ in pairs:
            root = uf.find(a)
            groups[root].add(a)
            groups[root].add(b)
        for members in groups.values():
            ordered = tuple(sorted(members))
            clusters.append(
                Cluster(
                    check=check,
                    field=FIELD_BY_CHECK[check],
                    members=ordered,
                    sample_value=sample_by_url.get(ordered[0]),
                )
            )
    return clusters


def _cluster_finding(cluster: Cluster, url: str) -> Finding:
    peers = [m for m in cluster.members if m != url]
    shown = peers[:5]
    tail = f" (+{len(peers) - 5} more)" if len(peers) > 5 else ""
    field_label = "title" if cluster.field == "title" else "description"
    return Finding(
        check=cluster.check,
        severity="error",
        ref=url,
        detail=(
            f"{len(cluster.members)} pages share this {field_label}: "
            f"{', '.join(shown)}{tail}"
        ),
        value=cluster.sample_value,
    )


def calibrate(
    per_row: dict[str, list[FieldFinding]], raw_findings: list[Finding], *, profile: Profile
) -> tuple[dict[str, list[FieldFinding]], list[Cluster]]:
    clusters = build_clusters(raw_findings)
    if profile == "strict":
        return per_row, clusters

    cluster_by_member: dict[tuple[str, str], Cluster] = {}
    for cluster in clusters:
        for member in cluster.members:
            cluster_by_member[(cluster.check, member)] = cluster

    new_per_row: dict[str, list[FieldFinding]] = {}
    for url, items in per_row.items():
        kept: list[FieldFinding] = []
        emitted_cluster_checks: set[str] = set()
        for item in items:
            finding = item.finding
            if finding.check in _DUPLICATE_CHECKS:
                member_cluster = cluster_by_member.get((finding.check, url))
                if member_cluster is None or finding.check in emitted_cluster_checks:
                    continue
                emitted_cluster_checks.add(finding.check)
                kept.append(
                    FieldFinding(field=item.field, finding=_cluster_finding(member_cluster, url))
                )
                continue
            if finding.check in _LENGTH_CHECKS and finding.severity == "warn":
                kept.append(FieldFinding(
                    field=item.field,
                    finding=Finding(
                        check=finding.check, severity="info",
                        ref=finding.ref, detail=finding.detail, value=finding.value,
                    ),
                ))
                continue
            kept.append(item)
        new_per_row[url] = kept

    return new_per_row, clusters


def cluster_summary(clusters: list[Cluster]) -> dict[str, int]:
    return {
        "duplicate_groups": len(clusters),
        "pages_in_duplicate_groups": len({m for c in clusters for m in c.members}),
    }
