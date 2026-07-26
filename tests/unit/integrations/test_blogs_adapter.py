"""`BlogsAdapter` (task-26): crawl, cancel, failure paths.

AISeoTools's crawl is replaced at the module's lazy seam, so nothing here
touches the network or the `seo_audit` package.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest

from app.integrations import blogs as adapter_module
from app.integrations.blogs import BlogsAdapter, post_key
from app.jobs.fake_adapter import CancelledByAdapter
from app.schemas.adapter import CollectionContext, DateRange
from app.schemas.enums import ComparisonMode, PeriodKind, Provider, Quality, SourceMode

URL_1 = "https://blog.example.com/blog/post-1"
URL_2 = "https://blog.example.com/blog/post-2"


def _context(**overrides) -> CollectionContext:
    base = dict(
        client_id="client-1",
        provider=Provider.blogs,
        source_mode=SourceMode.auto,
        period_kind=PeriodKind.month,
        comparison_mode=ComparisonMode.closed,
        current=DateRange(start=date(2026, 7, 1), end=date(2026, 7, 31)),
        previous=DateRange(start=date(2026, 6, 1), end=date(2026, 6, 30)),
        timezone="America/New_York",
        job_mode="blogs",
        integration_config={"domain": "blog.example.com"},
    )
    base.update(overrides)
    return CollectionContext(**base)


def _crawl_result(*, posts=None, warnings=None):
    return SimpleNamespace(
        domain="blog.example.com",
        blog_url="https://blog.example.com/blog/",
        crawl_completeness=1.0,
        warnings=warnings or [],
        observed_at=datetime(2026, 7, 23, tzinfo=UTC),
        posts=posts
        if posts is not None
        else [
            SimpleNamespace(
                url=URL_1, title="Post 1", description="Desc 1", word_count=500,
                published_date="2026-07-01", modified_date=None,
            ),
            SimpleNamespace(
                url=URL_2, title="Post 2", description="Desc 2", word_count=800,
                published_date="2026-07-10", modified_date=None,
            ),
        ],
    )


def _noop(_event) -> None:
    return None


# --- preflight ---------------------------------------------------------------


def test_preflight_not_configured_without_a_domain(monkeypatch):
    monkeypatch.setattr(adapter_module, "_seo_audit_available", lambda: True)
    result = BlogsAdapter().preflight(_context(integration_config={}))
    assert result.ok is False
    assert result.reason_code == "not_configured"


def test_preflight_unavailable_without_aiseotools(monkeypatch):
    monkeypatch.setattr(adapter_module, "_seo_audit_available", lambda: False)
    result = BlogsAdapter().preflight(_context())
    assert result.ok is False
    assert result.reason_code == "unavailable"


def test_preflight_ok_costs_no_units(monkeypatch):
    monkeypatch.setattr(adapter_module, "_seo_audit_available", lambda: True)
    result = BlogsAdapter().preflight(_context())
    assert result.ok is True
    assert result.units_estimated == 0


# --- crawl ---------------------------------------------------------------------


def test_collect_crawl_produces_no_metrics_and_records_the_run_dir(monkeypatch):
    captured: dict = {}

    def _fake(**kwargs):
        captured.update(kwargs)
        return _crawl_result()

    monkeypatch.setattr(adapter_module, "_collect_blogs", _fake)
    adapter = BlogsAdapter()
    context = _context()

    result = adapter.collect(context, _noop, lambda: False)

    assert captured["seed_url"] == "https://blog.example.com"
    assert result.quality == Quality.complete
    assert result.metrics == []  # a blog inventory is posts, never metric values
    assert result.provenance["run_dir"].startswith("blogs/client-1/")
    assert result.provenance["post_count"] == 2


def test_collect_datasets_emits_summary_and_posts(monkeypatch):
    monkeypatch.setattr(adapter_module, "_collect_blogs", lambda **_: _crawl_result())
    adapter = BlogsAdapter()
    context = _context()
    adapter.collect(context, _noop, lambda: False)

    datasets = adapter.collect_datasets(context, _noop)

    keys = {row.dataset_key for row in datasets.rows}
    assert keys == {"blog_summary", "blog_posts"}
    summary = next(r for r in datasets.rows if r.dataset_key == "blog_summary")
    assert summary.metrics["post_count"] == 2
    assert summary.metrics["blog_url"] == "https://blog.example.com/blog/"
    posts = [r for r in datasets.rows if r.dataset_key == "blog_posts"]
    assert {r.dimension_value for r in posts} == {URL_1, URL_2}
    row1 = next(r for r in posts if r.dimension_value == post_key(URL_1))
    assert row1.metrics["title"] == "Post 1"
    assert row1.metrics["word_count"] == 500
    assert row1.metrics["published_date"] == "2026-07-01"


def test_missing_published_date_stays_none_not_zero(monkeypatch):
    posts = [
        SimpleNamespace(
            url=URL_1, title="No date", description="", word_count=100,
            published_date=None, modified_date=None,
        ),
    ]
    monkeypatch.setattr(adapter_module, "_collect_blogs", lambda **_: _crawl_result(posts=posts))
    adapter = BlogsAdapter()
    context = _context()
    adapter.collect(context, _noop, lambda: False)

    datasets = adapter.collect_datasets(context, _noop)
    post_row = next(r for r in datasets.rows if r.dataset_key == "blog_posts")
    assert post_row.metrics["published_date"] is None


def test_collect_datasets_without_a_prior_collect_is_honest(monkeypatch):
    datasets = BlogsAdapter().collect_datasets(_context(), _noop)
    assert datasets.quality == Quality.unavailable
    assert datasets.error_code == "no_payload"
    assert datasets.rows == []


def test_duplicate_dimension_values_never_abort_the_whole_run(monkeypatch):
    """`(dataset_key, dimension_value)` is UNIQUE per snapshot. The producer already
    dedupes by canonical URL, but the adapter must also never hand the runner a
    colliding row — the exact failure class task-25 hit for `tech_findings`."""
    duped = _crawl_result()
    duped.posts.append(duped.posts[0])  # same url twice
    monkeypatch.setattr(adapter_module, "_collect_blogs", lambda **_: duped)
    adapter = BlogsAdapter()
    context = _context()
    adapter.collect(context, _noop, lambda: False)

    datasets = adapter.collect_datasets(context, _noop)

    posts = [r for r in datasets.rows if r.dataset_key == "blog_posts"]
    keys = [r.dimension_value for r in posts]
    assert len(keys) == len(set(keys)), "no colliding dimension_value reaches the DB"
    assert len(posts) == 2, "the genuine posts are still both there"


def test_crawl_reaching_nothing_still_completes(monkeypatch):
    """Zero posts on a real site is a legitimate (if unusual) result, not a failure —
    unlike tech_audit's zero-pages-crawled case, which signals the crawl itself
    never ran. `collect_blogs` always attempts the crawl and reports warnings
    separately, so an empty blog is `complete` with `post_count: 0`, not `unavailable`."""
    empty = _crawl_result(posts=[])
    monkeypatch.setattr(adapter_module, "_collect_blogs", lambda **_: empty)

    result = BlogsAdapter().collect(_context(), _noop, lambda: False)

    assert result.quality == Quality.complete
    assert result.provenance["post_count"] == 0


def test_crawl_failure_never_raises(monkeypatch):
    def _boom(**_):
        raise RuntimeError("network gone")

    monkeypatch.setattr(adapter_module, "_collect_blogs", _boom)

    result = BlogsAdapter().collect(_context(), _noop, lambda: False)

    assert result.quality == Quality.unavailable
    assert result.error_code == "crawl_failed"
    assert "network gone" not in str(result.warnings)  # no raw error text leaks


def test_cancel_before_start(monkeypatch):
    monkeypatch.setattr(adapter_module, "_collect_blogs", lambda **_: _crawl_result())
    with pytest.raises(CancelledByAdapter):
        BlogsAdapter().collect(_context(), _noop, lambda: True)


def test_mid_crawl_cancel_becomes_cancelled_by_adapter(monkeypatch):
    class BlogsCancelled(BaseException):
        """Stands in for the AISeoTools signal (matched by class name)."""

    def _cancel(**_):
        raise BlogsCancelled("stopped at a crawl checkpoint")

    monkeypatch.setattr(adapter_module, "_collect_blogs", _cancel)

    with pytest.raises(CancelledByAdapter):
        BlogsAdapter().collect(_context(), _noop, lambda: False)


def test_warnings_produce_partial_quality(monkeypatch):
    monkeypatch.setattr(
        adapter_module, "_collect_blogs",
        lambda **_: _crawl_result(warnings=["crawl incomplete: 3 URL(s) failed to fetch"]),
    )
    result = BlogsAdapter().collect(_context(), _noop, lambda: False)
    assert result.quality == Quality.partial
    assert result.warnings
