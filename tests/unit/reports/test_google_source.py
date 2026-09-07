"""task-84 — the direct Google API report source.

Two layers:

- `google_source` against a fake `GoogleApiSession` (`post_json` only), and a
  head-to-head with `api_source` proving the two transports produce the SAME
  eight `PeriodMetrics` fields from equivalent inputs (§7.1, the main test);
- `runner`/`scheduler`/`period_retry` wiring: the source fork is explicit
  (an unknown source raises, it is never a silent PDF run), a
  `source="google_api"` run is actually claimed by the scheduler, a
  `403 PERMISSION_DENIED` is `not_configured` and a `comparator` is refused.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.core.config import get_settings
from app.db.models import Client, ClientIntegration
from app.db.repositories.reports import ReportRepository
from app.integrations.google_api import GoogleApiPermissionError
from app.reports import google_source, period_retry, runner
from app.reports.api_source import fetch_period_metrics as api_fetch_period_metrics
from app.reports.history import ReportHistory
from app.reports.models import PeriodMetrics
from app.reports.periods import ReportPeriod
from app.reports.scheduler import ReportScheduler
from app.schemas.enums import Provider, SourceMode

_FROM, _TO = date(2026, 7, 1), date(2026, 7, 31)

_GSA_QUERY_SUFFIX = "/searchAnalytics/query"
_RUN_REPORT_SUFFIX = ":runReport"
_CHANNEL = "firstUserDefaultChannelGroup"


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------


def _ga4_row(dims: dict[str, str], **metrics: str) -> dict:
    return {"dims": dims, "metrics": metrics}


def _channel_row(channel: str, *, users: str, events: str) -> dict:
    return _ga4_row({_CHANNEL: channel}, totalUsers=users, keyEvents=events)


class FakeGoogleSession:
    """Duck-types `GoogleApiSession.post_json` with scripted payloads, keyed by
    endpoint and — for GA4 — by the tuple of requested dimension names.
    """

    def __init__(
        self,
        *,
        gsc_rows: list[dict] | None = None,
        ga4_by_dims: dict[tuple[str, ...], list[dict]] | None = None,
    ) -> None:
        self.gsc_rows = gsc_rows or []
        self.ga4_by_dims = ga4_by_dims or {}
        self.calls: list[tuple[str, dict]] = []

    def __enter__(self) -> FakeGoogleSession:
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False

    def post_json(self, url: str, body: dict) -> dict:
        self.calls.append((url, body))
        if url.endswith(_GSA_QUERY_SUFFIX):
            if body.get("startRow", 0) > 0:
                return {"rows": []}
            return {
                "rows": [
                    {
                        "keys": [row["date"]],
                        "clicks": row.get("clicks"),
                        "impressions": row.get("impressions"),
                        "ctr": row.get("ctr"),
                        "position": row.get("position"),
                    }
                    for row in self.gsc_rows
                ]
            }
        if url.endswith(_RUN_REPORT_SUFFIX):
            dim_names = tuple(d["name"] for d in body.get("dimensions", []))
            met_names = [m["name"] for m in body.get("metrics", [])]
            spec = self.ga4_by_dims.get(dim_names, [])
            if body.get("offset", 0) > 0:
                return {"rows": [], "rowCount": len(spec)}
            rows = [
                {
                    "dimensionValues": [{"value": e["dims"][name]} for name in dim_names],
                    "metricValues": [
                        {"value": str(e["metrics"].get(name, "0"))} for name in met_names
                    ],
                }
                for e in spec
            ]
            return {
                "dimensionHeaders": [{"name": name} for name in dim_names],
                "metricHeaders": [{"name": name} for name in met_names],
                "rows": rows,
                "rowCount": len(rows),
            }
        raise AssertionError(f"unexpected url {url}")


class FakeNinjaClient:
    """The same `ReportingNinjaClient.query` duck-type `test_api_source` uses."""

    def __init__(self, queries: dict[str, list[dict]]):
        self._queries = queries

    def query(  # noqa: PLR0913 - matches the real signature
        self, integration_id, connection_key, account_id, fields, date_start, date_end,
        *, limit=100, settings=None,
    ):
        return self._queries.get(f"{integration_id}:{len(fields)}", [])


def _ninja_channel_row(channel: str, *, users: str, conv: str) -> dict:
    return {_CHANNEL: channel, "totalUsers": users, "conversions": conv}


# --------------------------------------------------------------------------
# §7.1 — the two transports agree on all eight fields
# --------------------------------------------------------------------------


def test_both_transports_produce_the_same_eight_fields():
    daily = [
        {"date": "2026-07-01", "clicks": 100, "impressions": 1000},
        {"date": "2026-07-02", "clicks": 131, "impressions": 1564},
    ]
    ninja = FakeNinjaClient(
        {
            "ga4:2": [{"totalUsers": "6251", "conversions": "1099"}],
            "ga4:3": [
                _ninja_channel_row("Organic Search", users="3061", conv="654"),
                _ninja_channel_row("Direct", users="2858", conv="391"),
                _ninja_channel_row("Organic Social", users="137", conv="26"),
            ],
            "google_search_console:3": daily,
        }
    )
    api_metrics = api_fetch_period_metrics(
        ninja,
        ga4_connection_key="seo@x",
        ga4_account_id="properties/1",
        ga4_fields={
            "users": "totalUsers",
            "conversions": "conversions",
            "channel": _CHANNEL,
        },
        gsc_ref=("seo@x", "sc-domain:x.com"),
        gsc_fields={"clicks": "clicks", "impressions": "impressions", "date": "date"},
        date_from=_FROM,
        date_to=_TO,
    )

    google = FakeGoogleSession(
        gsc_rows=daily,
        ga4_by_dims={
            (): [_ga4_row({}, totalUsers="6251", keyEvents="1099")],
            (_CHANNEL,): [
                _channel_row("Organic Search", users="3061", events="654"),
                _channel_row("Direct", users="2858", events="391"),
                _channel_row("Organic Social", users="137", events="26"),
            ],
        },
    )
    google_metrics = google_source.fetch_period_metrics(
        google,
        ga4_property_id="properties/1",
        gsc_site_url="sc-domain:x.com",
        date_from=_FROM,
        date_to=_TO,
    )

    assert google_metrics == api_metrics
    assert google_metrics == PeriodMetrics(
        users=6251,
        organic_users=3061,
        direct_users=2858,
        conversions=1099,
        organic_conversions=654,
        direct_conversions=391,
        clicks=231,
        impressions=2564,
    )


# --------------------------------------------------------------------------
# §7.2 / §7.3 / §7.4 — attribution model invariants
# --------------------------------------------------------------------------


def test_organic_search_never_absorbs_organic_social_or_shopping():
    google = FakeGoogleSession(
        ga4_by_dims={
            (): [_ga4_row({}, totalUsers="200", keyEvents="0")],
            (_CHANNEL,): [
                _channel_row("Organic Search", users="100", events="5"),
                _channel_row("Organic Social", users="50", events="1"),
                _channel_row("Organic Shopping", users="50", events="2"),
            ],
        }
    )
    result = google_source.fetch_ga4_period(google, "properties/1", _FROM, _TO)
    assert result["organic_users"] == 100  # not 100 + 50 + 50
    assert result["organic_conversions"] == 5  # not 5 + 1 + 2


def test_no_ga4_rows_is_none_but_a_real_zero_stays_zero():
    empty = FakeGoogleSession(ga4_by_dims={})
    assert google_source.fetch_ga4_period(empty, "properties/1", _FROM, _TO)["users"] is None

    zero = FakeGoogleSession(
        ga4_by_dims={(): [_ga4_row({}, totalUsers="0", keyEvents="0")]}
    )
    result = google_source.fetch_ga4_period(zero, "properties/1", _FROM, _TO)
    assert result["users"] == 0
    assert result["conversions"] == 0
    # No channel row at all -> organic/direct are None, never 0.
    assert result["organic_users"] is None
    assert result["direct_conversions"] is None


def test_gsc_empty_period_is_none_not_zero():
    google = FakeGoogleSession(gsc_rows=[])
    assert google_source.fetch_gsc_period(google, "sc-domain:x.com", _FROM, _TO) == {
        "clicks": None,
        "impressions": None,
    }


def test_gsc_keeps_a_genuinely_zero_day():
    google = FakeGoogleSession(gsc_rows=[{"date": "2026-07-01", "clicks": 0, "impressions": 0}])
    assert google_source.fetch_gsc_period(google, "sc-domain:x.com", _FROM, _TO) == {
        "clicks": 0,
        "impressions": 0,
    }


def test_only_keyevents_is_requested_never_conversions():
    """`conversions` + `keyEvents` in one GA4 request errors out (task-84 §2)."""
    google = FakeGoogleSession(
        ga4_by_dims={(): [_ga4_row({}, totalUsers="1", keyEvents="1")]}
    )
    google_source.fetch_ga4_period(google, "properties/1", _FROM, _TO)
    run_report_calls = [b for url, b in google.calls if url.endswith(_RUN_REPORT_SUFFIX)]
    assert run_report_calls
    for body in run_report_calls:
        metric_names = {m["name"] for m in body["metrics"]}
        assert "conversions" not in metric_names
        assert "keyEvents" in metric_names


def test_no_gsc_site_leaves_clicks_and_impressions_none():
    google = FakeGoogleSession(
        ga4_by_dims={(): [_ga4_row({}, totalUsers="10", keyEvents="1")]}
    )
    metrics = google_source.fetch_period_metrics(
        google, ga4_property_id="properties/1", gsc_site_url=None, date_from=_FROM, date_to=_TO
    )
    assert metrics.users == 10
    assert metrics.clicks is None
    assert metrics.impressions is None


# --------------------------------------------------------------------------
# Runner / scheduler / period_retry wiring
# --------------------------------------------------------------------------


def _seed_client(temp_db) -> str:
    with temp_db.session_scope() as session:
        client = Client(name="SAPHNY", slug="saphny", domain="saphny.example.com")
        session.add(client)
        session.flush()
        session.add(
            ClientIntegration(
                client_id=client.id, provider=Provider.reporting_ninja,
                source_mode=SourceMode.api,
                config_json={"connection_key": "seo@x", "account_id": "properties/1"},
            )
        )
        session.add(
            ClientIntegration(
                client_id=client.id, provider=Provider.gsc, source_mode=SourceMode.api,
                config_json={"connection_key": "seo@x", "account_id": "sc-domain:saphny.com"},
            )
        )
        return client.id


def _create_run(temp_db, client_id, *, kind="monthly", source="google_api", params=None) -> str:
    with temp_db.session_scope() as session:
        run = ReportRepository(session).create_run(
            client_id, kind=kind, source=source, params=params or {}, period_count=6,
        )
        return run.id


def _run_of(temp_db, run_id):
    with temp_db.session_scope() as session:
        return ReportRepository(session).get(run_id)


class _FakeSession:
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def _fake_google_transport(monkeypatch, *, metrics=None, raises=None):
    monkeypatch.setattr(runner, "open_google_api_session", lambda *_a, **_k: _FakeSession())

    def _fetch(*_a, **_k):
        if raises is not None:
            raise raises
        return metrics if metrics is not None else PeriodMetrics(users=10, clicks=5)

    monkeypatch.setattr(runner.google_source, "fetch_period_metrics", _fetch)
    monkeypatch.setattr(
        runner.google_source, "fetch_history", lambda *_a, **_k: ReportHistory(months=[])
    )


def test_a_google_api_run_reaches_succeeded_and_stores_the_table(temp_db, monkeypatch):
    client_id = _seed_client(temp_db)
    run_id = _create_run(temp_db, client_id)
    _fake_google_transport(monkeypatch, metrics=PeriodMetrics(users=42, clicks=7, impressions=99))

    runner.run(temp_db.session_scope, get_settings(), run_id)

    run = _run_of(temp_db, run_id)
    assert run.status == "succeeded"
    assert run.source == "google_api"
    assert run.result_json["rows"]


def test_an_unknown_source_raises_and_never_falls_through_to_the_pdf_path(temp_db, monkeypatch):
    client_id = _seed_client(temp_db)
    run_id = _create_run(temp_db, client_id, source="totally-unknown")
    monkeypatch.setattr(
        runner, "_run_pdf",
        lambda *a, **k: pytest.fail("an unknown source fell through to the PDF path"),
    )

    runner.run(temp_db.session_scope, get_settings(), run_id)

    run = _run_of(temp_db, run_id)
    assert run.status == "failed"
    assert "unknown report source" in (run.error_message or "")


def test_permission_denied_is_not_configured_not_a_red_failure(temp_db, monkeypatch):
    client_id = _seed_client(temp_db)
    run_id = _create_run(temp_db, client_id)
    _fake_google_transport(
        monkeypatch, raises=GoogleApiPermissionError("403 PERMISSION_DENIED — no access")
    )

    runner.run(temp_db.session_scope, get_settings(), run_id)

    run = _run_of(temp_db, run_id)
    assert run.error_code == "not_configured"
    # Every period is the same property — it aborts, it does not mark 6 reds.
    assert run.periods_failed == 0


def test_comparator_with_google_api_is_refused(temp_db, monkeypatch):
    client_id = _seed_client(temp_db)
    run_id = _create_run(
        temp_db, client_id, kind="comparator", params={"months": ["2025-06", "2024-06"]}
    )
    _fake_google_transport(monkeypatch)

    runner.run(temp_db.session_scope, get_settings(), run_id)

    run = _run_of(temp_db, run_id)
    assert run.status == "failed"
    assert run.error_code == "source_unsupported"


def test_the_scheduler_actually_claims_a_google_api_run(temp_db):
    """§0.3 regression: `claim_next_queued` filters `source` exactly, so
    without its own `_fill("google_api", ...)` the run sits `queued` forever
    with no error at all.
    """
    client_id = _seed_client(temp_db)
    run_id = _create_run(temp_db, client_id)

    scheduler = ReportScheduler(session_scope=temp_db.session_scope, settings=get_settings())
    scheduler._api_pool.submit = lambda *a, **k: None  # type: ignore[method-assign]
    scheduler._batch_api_pool.submit = lambda *a, **k: None  # type: ignore[method-assign]
    scheduler._google_api_pool.submit = lambda *a, **k: None  # type: ignore[method-assign]
    scheduler._pdf_pool.submit = lambda *a, **k: None  # type: ignore[method-assign]
    try:
        scheduler._tick()
    finally:
        scheduler.stop()

    assert _run_of(temp_db, run_id).status == "running"


def test_period_retry_routes_google_api_runs_to_the_direct_transport(temp_db, monkeypatch):
    captured: dict[str, object] = {}

    def _fake_refetch_google(period, rn_integration, gsc_integration, settings):
        captured["called"] = True
        return PeriodMetrics(users=1, clicks=1)

    monkeypatch.setattr(period_retry, "_refetch_google", _fake_refetch_google)
    monkeypatch.setattr(
        period_retry, "_refetch_pdf",
        lambda *a, **k: pytest.fail("a google_api retry used the PDF path"),
    )
    monkeypatch.setattr(
        period_retry, "_refetch_api",
        lambda *a, **k: pytest.fail("a google_api retry used the Reporting Ninja API path"),
    )
    monkeypatch.setattr(period_retry, "write_run_xlsx", lambda *a, **k: None)
    monkeypatch.setattr(period_retry, "write_run_client_pdf", lambda *a, **k: None)

    client_id = _seed_client(temp_db)
    with temp_db.session_scope() as session:
        run = ReportRepository(session).create_run(
            client_id, kind="monthly", source="google_api", params={}, period_count=1,
            periods=[
                {
                    "label": "This Month", "date_from": "2026-07-01", "date_to": "2026-07-31",
                    "is_partial": False, "state": "failed", "started_at": None,
                    "finished_at": None, "duration_seconds": None, "from_cache": False,
                    "error": "boom",
                }
            ],
        )
        run.status = "failed"
        run.result_json = {
            "rows": [{"key": "users", "label": "Users", "values": [None], "changes": []}],
            "change_labels": [], "periods": [], "sources": {},
        }
        run_id = run.id

    error = period_retry.retry_period(
        temp_db.session_scope, get_settings(), run_id, "This Month"
    )
    assert error is None
    assert captured.get("called") is True


def test_refetch_google_reads_the_property_id_from_the_rn_integration(temp_db, monkeypatch):
    """`_refetch_google` needs only `account_id`, never `connection_key`."""
    seen: dict[str, object] = {}

    def _fetch(session, *, ga4_property_id, gsc_site_url, date_from, date_to):
        seen["ga4_property_id"] = ga4_property_id
        seen["gsc_site_url"] = gsc_site_url
        return PeriodMetrics(users=3)

    monkeypatch.setattr(period_retry, "open_google_api_session", lambda *_a, **_k: _FakeSession())
    monkeypatch.setattr(period_retry.google_source, "fetch_period_metrics", _fetch)

    client_id = _seed_client(temp_db)
    with temp_db.session_scope() as session:
        from app.db.repositories.clients import ClientRepository

        repo = ClientRepository(session)
        rn = repo.get_integration(client_id, Provider.reporting_ninja)
        gsc = repo.get_integration(client_id, Provider.gsc)

    period = ReportPeriod(label="This Month", date_from=_FROM, date_to=_TO, is_partial=False)
    period_retry._refetch_google(period, rn, gsc, get_settings())
    assert seen == {"ga4_property_id": "properties/1", "gsc_site_url": "sc-domain:saphny.com"}
