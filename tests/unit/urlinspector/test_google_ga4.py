"""`app.urlinspector.google.ga4` — GA4 Data API про один URL (task-88).

Регресії на §0.4 (кінцевий слеш), §0.8 (ліміти запиту, назви конверсій із
пробілом) і §0.10 (вбудована звірка). Числа в очікуваннях — з живого прогону
на HWELL, а не вигадані.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from app.urlinspector.google.ga4 import (
    LANDING_METRICS,
    MAX_DIMENSIONS_PER_REQUEST,
    MAX_METRICS_PER_REQUEST,
    Ga4UrlClient,
)
from app.urlinspector.google.window import resolve_window
from app.urlinspector.sources.google_ga4 import collect_ga4_core

PATH_VARIANTS = ["/about-us/jessica-m-frankel", "/about-us/jessica-m-frankel/"]


class FakeSession:
    def __init__(self, responses: list[dict[str, Any]] | None = None) -> None:
        self.responses = responses or []
        self.bodies: list[dict[str, Any]] = []
        self.gets: list[str] = []
        self.get_payload: dict[str, Any] = {"metrics": []}

    def post_json(self, url: str, body: dict[str, Any]) -> dict[str, Any]:
        self.bodies.append(body)
        if self.responses:
            return self.responses.pop(0)
        return {"rows": [], "rowCount": 0}

    def get_json(self, url: str, *, params: dict[str, str] | None = None) -> dict[str, Any]:
        self.gets.append(url)
        return self.get_payload


def _client(
    responses: list[dict[str, Any]] | None = None,
) -> tuple[Ga4UrlClient, FakeSession]:
    session = FakeSession(responses)
    client = Ga4UrlClient(
        session,  # type: ignore[arg-type]
        property_id="properties/395442288",
        path_variants=PATH_VARIANTS,
    )
    return client, session


def _row(dims: list[str], mets: list[str]) -> dict[str, Any]:
    return {
        "dimensionValues": [{"value": v} for v in dims],
        "metricValues": [{"value": v} for v in mets],
    }


def _payload(
    dim_names: list[str], met_names: list[str], rows: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        "dimensionHeaders": [{"name": n} for n in dim_names],
        "metricHeaders": [{"name": n} for n in met_names],
        "rows": rows,
        "rowCount": len(rows),
    }


class TestTrailingSlash:
    """§0.4 — `EXACT` по одному написанню губить 13-19 % даних."""

    def test_every_filter_lists_both_path_spellings(self):
        client, session = _client()
        client.page_totals(date(2026, 6, 1), date(2026, 6, 30))
        values = session.bodies[0]["dimensionFilter"]["filter"]["inListFilter"]["values"]
        assert values == PATH_VARIANTS

    def test_landing_and_page_scopes_use_different_dimensions(self):
        # Два різні питання: `landingPage` — про сесії входу, `pagePath` —
        # про перегляди. Числа різні, і жодне не «правильніше».
        client, session = _client()
        client.landing_totals(date(2026, 6, 1), date(2026, 6, 30))
        client.page_totals(date(2026, 6, 1), date(2026, 6, 30))
        fields = [
            body["dimensionFilter"]["filter"]["fieldName"] for body in session.bodies
        ]
        assert "landingPage" in fields
        assert "pagePath" in fields

    def test_in_list_filter_not_string_filter(self):
        # `stringFilter`/`EXACT` — саме те, що губило 6 із 32 сесій.
        client, session = _client()
        client.daily_series(date(2026, 6, 1), date(2026, 6, 30))
        assert "inListFilter" in session.bodies[0]["dimensionFilter"]["filter"]
        assert "stringFilter" not in session.bodies[0]["dimensionFilter"]["filter"]


class TestRequestLimits:
    """§0.8 — 11-та метрика падає непрозорим 400 INVALID_ARGUMENT."""

    def test_landing_metric_set_does_not_fit_one_request(self):
        # Якщо цей assert колись стане неправдою, `_batched` можна прибрати.
        # Поки він правдивий — пачки обов'язкові.
        assert len(LANDING_METRICS) > MAX_METRICS_PER_REQUEST

    def test_landing_totals_is_split_into_batches_within_the_limit(self):
        client, session = _client()
        client.landing_totals(date(2026, 6, 1), date(2026, 6, 30))
        assert len(session.bodies) == 2
        for body in session.bodies:
            assert len(body["metrics"]) <= MAX_METRICS_PER_REQUEST

    def test_batches_are_merged_into_one_result(self):
        first = LANDING_METRICS[:MAX_METRICS_PER_REQUEST]
        second = LANDING_METRICS[MAX_METRICS_PER_REQUEST:]
        client, _ = _client(
            [
                _payload([], list(first), [_row([], ["1"] * len(first))]),
                _payload([], list(second), [_row([], ["2"] * len(second))]),
            ]
        )
        totals = client.landing_totals(date(2026, 6, 1), date(2026, 6, 30))
        assert set(totals) == set(LANDING_METRICS)
        assert totals["sessions"] == Decimal(1)
        assert totals[second[0]] == Decimal(2)

    def test_too_many_metrics_raises_locally_instead_of_a_google_400(self):
        # Помилка мусить прийти з нашого коду в тесті, а не з Google у проді.
        client, _ = _client()
        with pytest.raises(ValueError, match="at most 10 metrics"):
            client.breakdown(
                date(2026, 6, 1),
                date(2026, 6, 30),
                dimensions=["date"],
                metrics=[f"m{i}" for i in range(MAX_METRICS_PER_REQUEST + 1)],
            )

    def test_too_many_dimensions_raises_locally(self):
        client, _ = _client()
        with pytest.raises(ValueError, match="at most 8 dimensions"):
            client.breakdown(
                date(2026, 6, 1),
                date(2026, 6, 30),
                dimensions=[f"d{i}" for i in range(MAX_DIMENSIONS_PER_REQUEST + 1)],
                metrics=["sessions"],
            )


class TestKeyEvents:
    """§0.8 — `keyEvents:Contact Us` падає через пробіл, і `%20` не рятує."""

    def test_conversions_go_through_the_event_name_dimension(self):
        client, session = _client()
        client.key_events_by_name(date(2026, 6, 1), date(2026, 6, 30))
        body = session.bodies[0]
        assert [d["name"] for d in body["dimensions"]] == ["eventName"]
        assert [m["name"] for m in body["metrics"]] == ["keyEvents", "eventCount"]

    def test_no_request_ever_names_a_metric_with_a_colon(self):
        # `keyEvents:<name>` не використовується взагалі — інакше набір
        # конверсій клієнта тихо різався б за наявністю пробілу в назві.
        client, session = _client()
        client.key_events_by_name(date(2026, 6, 1), date(2026, 6, 30))
        client.landing_totals(date(2026, 6, 1), date(2026, 6, 30))
        for body in session.bodies:
            for metric in body["metrics"]:
                assert ":" not in metric["name"]

    def test_configured_names_are_read_from_property_metadata(self):
        client, session = _client()
        session.get_payload = {
            "metrics": [
                {"apiName": "sessions"},
                {"apiName": "keyEvents:Contact Us"},
                {"apiName": "keyEvents:Phone"},
                {"apiName": "sessionKeyEventRate:Phone"},
                {"apiName": "keyEvents:"},
            ]
        }
        assert client.key_event_names() == ["Contact Us", "Phone"]

    def test_no_configured_conversions_is_a_valid_empty_answer(self):
        client, _ = _client()
        assert client.key_event_names() == []

    def test_core_keeps_only_conversions_that_actually_happened(self):
        # Живо: єдина конверсія на цій сторінці — `Address`, і її НЕМА в
        # metadata-списку property. Тобто той список не авторитетний для
        # «що тут сталося», а рядок із `0` вигадувати заборонено (PLAN §12).
        events = _payload(
            ["eventName"],
            ["keyEvents", "eventCount"],
            [
                _row(["Address"], ["1", "1"]),
                _row(["page_view"], ["0", "56"]),
            ],
        )
        client, _ = _client(
            [
                _payload([], list(LANDING_METRICS[:10]), []),
                _payload([], list(LANDING_METRICS[10:]), []),
                _payload([], [], []),
                _payload(["date"], [], []),
                events,
            ]
        )
        dto = collect_ga4_core(
            client, window=resolve_window("90d", today=date(2026, 6, 30))
        )
        assert [k.name for k in dto.key_events] == ["Address"]


class TestOrganicCrossCheck:
    """§0.10 — Google проти Google."""

    def test_requires_the_landing_page_plus_query_string_dimension(self):
        # Без дименсії / з `landingPage` / з `date` разом — 400 (§0.8).
        client, session = _client()
        client.organic_search_totals(date(2026, 6, 1), date(2026, 6, 30))
        body = session.bodies[0]
        assert [d["name"] for d in body["dimensions"]] == ["landingPagePlusQueryString"]

    def test_counts_are_summed_across_slash_variants(self):
        # Дименсія обов'язкова, тож рядків може бути два — по одному на
        # написання шляху; кліки/покази складаються.
        payload = _payload(
            ["landingPagePlusQueryString"],
            ["organicGoogleSearchClicks", "organicGoogleSearchImpressions"],
            [_row(["/a/"], ["2", "30"]), _row(["/a"], ["1", "9"])],
        )
        client, _ = _client([payload])
        totals = client.organic_search_totals(date(2026, 6, 1), date(2026, 6, 30))
        assert totals["organicGoogleSearchClicks"] == Decimal(3)
        assert totals["organicGoogleSearchImpressions"] == Decimal(39)

    def test_position_across_several_rows_is_none_not_a_mean_of_means(self):
        payload = _payload(
            ["landingPagePlusQueryString"],
            ["organicGoogleSearchAveragePosition"],
            [_row(["/a/"], ["3"]), _row(["/a"], ["60"])],
        )
        client, _ = _client([payload])
        totals = client.organic_search_totals(date(2026, 6, 1), date(2026, 6, 30))
        assert totals["organicGoogleSearchAveragePosition"] is None

    def test_single_row_keeps_position_verbatim(self):
        payload = _payload(
            ["landingPagePlusQueryString"],
            ["organicGoogleSearchAveragePosition"],
            [_row(["/a/"], ["9.5531948"])],
        )
        client, _ = _client([payload])
        totals = client.organic_search_totals(date(2026, 6, 1), date(2026, 6, 30))
        assert totals["organicGoogleSearchAveragePosition"] == Decimal("9.5531948")


class TestSeriesAndAbsence:
    def test_date_dimension_is_normalized_from_ga4_compact_form(self):
        payload = _payload(
            ["date"], ["sessions"], [_row(["20260610"], ["5"])]
        )
        client, _ = _client([payload])
        rows = client.daily_series(date(2026, 6, 1), date(2026, 6, 30))
        assert rows[0].keys == ("2026-06-10",)

    def test_absent_metric_stays_none_never_zero(self):
        payload = _payload([], ["scrolledUsers"], [_row([], [""])])
        client, _ = _client([payload, _payload([], [], [])])
        totals = client.page_totals(date(2026, 6, 1), date(2026, 6, 30))
        assert totals["scrolledUsers"] is None

    def test_engagement_rate_is_kept_as_a_fraction(self):
        # API дає частку `0..1`; у БД той самий показник лежить відсотком.
        # Помножити тут удруге — це «638 %» (пастка ×100, task-83 §7.3).
        payload = _payload(
            [], list(LANDING_METRICS[:10]), [_row([], ["31"] + ["0.29032258"] * 9)]
        )
        client, _ = _client([payload, _payload([], list(LANDING_METRICS[10:]), [])])
        totals = client.landing_totals(date(2026, 6, 1), date(2026, 6, 30))
        assert totals["engagementRate"] < Decimal(1)


class TestNextPages:
    def test_referrer_filter_is_contains_on_the_longest_variant(self):
        # Реферер зберігається повним URL зі схемою й хостом, тож `EXACT` по
        # шляху не спрацював би ніколи.
        client, session = _client()
        client.next_pages(date(2026, 6, 1), date(2026, 6, 30))
        f = session.bodies[0]["dimensionFilter"]["filter"]
        assert f["fieldName"] == "pageReferrer"
        assert f["stringFilter"]["matchType"] == "CONTAINS"
        assert f["stringFilter"]["value"] == "/about-us/jessica-m-frankel/"
