"""`app.urlinspector.google.gsc` — Search Analytics про один URL (task-88).

Кожен тест тут — регресія на конкретне живе вимірювання з §0 плану, а не
абстрактна перевірка форми. Головний — `TestSyntheticZeros`: без нього
середня позиція сторінки виходить **1.17** замість **9.68**.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from app.urlinspector.google.gsc import (
    GscUrlClient,
    page_regex,
)
from app.urlinspector.google.window import resolve_window
from app.urlinspector.sources.google_gsc import collect_gsc_core

URL = "https://hearwellservices.com/about-us/jessica-m-frankel/"


class FakeSession:
    """Мінімальний двійник `GoogleApiSession`: віддає заготовлені відповіді за
    порядком/формою запиту й записує тіла для перевірок."""

    def __init__(self, responses: list[dict[str, Any]] | None = None) -> None:
        self.responses = responses or []
        self.bodies: list[dict[str, Any]] = []
        self.gets: list[str] = []

    def post_json(self, url: str, body: dict[str, Any]) -> dict[str, Any]:
        self.bodies.append(body)
        if self.responses:
            return self.responses.pop(0)
        return {"rows": []}

    def get_json(self, url: str, *, params: dict[str, str] | None = None) -> dict[str, Any]:
        self.gets.append(url)
        return {"sitemap": []}


def _client(responses: list[dict[str, Any]] | None = None) -> tuple[GscUrlClient, FakeSession]:
    session = FakeSession(responses)
    return GscUrlClient(session, site_url="sc-domain:hearwellservices.com"), session  # type: ignore[arg-type]


class TestPageRegex:
    def test_matches_both_slash_spellings(self):
        # §0.4: `equals` без слеша віддає синтетичний нуль, тобто «немає
        # даних»; обидва написання одним запитом дає лише regex.
        assert page_regex(URL).endswith("/?$")
        assert page_regex(URL) == page_regex(URL.rstrip("/"))

    def test_escapes_the_domain_dot(self):
        # Без `re.escape` точка в домені зробила б `hearwellXservices.com`
        # валідним матчем — тобто чужий сайт у звіті клієнта.
        pattern = page_regex(URL)
        assert r"hearwellservices\.com" in pattern
        assert "hearwellXservices" not in pattern

    def test_escapes_regex_metacharacters_in_the_path(self):
        pattern = page_regex("https://x.com/a+b(c)?d=1")
        for meta in ("a\\+b", "\\(c\\)"):
            assert meta in pattern

    def test_root_path_matches_both_spellings(self):
        # Домашня сторінка зберігається як `https://x.com/`; голий
        # `https://x.com` теж мусить збігатися.
        pattern = re.compile(page_regex("https://x.com/"))
        assert pattern.match("https://x.com/")
        assert pattern.match("https://x.com")
        assert not pattern.match("https://x.com/a")

    def test_www_and_bare_host_are_the_same_page(self):
        # `sc-domain:` property покриває обидва написання, і GSC зберігає те,
        # що реально показувалось. Знайдено гейтом G2 на SHB: питали про один
        # хост, рахували обидва — 15 372 кліки проти 18 144 у GA4.
        pattern = re.compile(page_regex("https://www.smashhouseburgers.com/"))
        for candidate in (
            "https://www.smashhouseburgers.com/",
            "https://smashhouseburgers.com/",
            "http://smashhouseburgers.com/",
        ):
            assert pattern.match(candidate), candidate

    def test_a_different_domain_never_matches(self):
        pattern = re.compile(page_regex("https://www.smashhouseburgers.com/"))
        assert not pattern.match("https://smashhouseburgersXcom/")
        assert not pattern.match("https://evil.com/?x=https://www.smashhouseburgers.com/")


class TestSyntheticZeros:
    """§0.3 — головна пастка таски."""

    @staticmethod
    def _series_payload() -> dict[str, Any]:
        # 5 днів: два реальні, три синтетичні (як живий API на 321 із 365).
        return {
            "rows": [
                {"keys": ["2026-06-09"], "clicks": 0, "impressions": 0, "ctr": 0, "position": 0},
                {"keys": ["2026-06-10"], "clicks": 0, "impressions": 0, "ctr": 0, "position": 0},
                {
                    "keys": ["2026-06-11"],
                    "clicks": 0,
                    "impressions": 8,
                    "ctr": 0,
                    "position": 12.75,
                },
                {"keys": ["2026-06-12"], "clicks": 0, "impressions": 0, "ctr": 0, "position": 0},
                {
                    "keys": ["2026-06-13"],
                    "clicks": 1,
                    "impressions": 11,
                    "ctr": 0.0909,
                    "position": 9.4545,
                },
            ],
            "metadata": {"firstIncompleteDate": "2026-06-13"},
        }

    def test_zero_impression_rows_are_dropped_not_kept_as_zero(self):
        client, _ = _client([self._series_payload()])
        series = client.daily_series(URL, date(2026, 6, 9), date(2026, 6, 13))
        assert [row.day.isoformat() for row in series.rows] == ["2026-06-11", "2026-06-13"]

    def test_average_position_is_not_dragged_to_zero(self):
        # Це і є гейт G3: 1.17 проти 9.68 на живих даних. Тут той самий
        # ефект у мініатюрі — середнє по реальних рядках, а не по всіх.
        client, _ = _client([self._series_payload()])
        series = client.daily_series(URL, date(2026, 6, 9), date(2026, 6, 13))
        positions = [row.metrics.position for row in series.rows]
        assert all(p is not None and p > 1 for p in positions)

    def test_real_zero_clicks_survive_when_impressions_exist(self):
        # `clicks == 0` при `impressions > 0` — справжній нуль. Викидати його
        # разом із синтетичними означало б загубити день, коли сторінку
        # бачили, але не клікнули.
        client, _ = _client([self._series_payload()])
        series = client.daily_series(URL, date(2026, 6, 9), date(2026, 6, 13))
        first = series.rows[0]
        assert first.metrics.clicks == Decimal(0)
        assert first.metrics.impressions == Decimal(8)

    def test_totals_of_a_url_with_no_data_is_absence_not_zero(self):
        # Живо: `equals` без слеша віддав рівно такий рядок (§0.4).
        client, _ = _client(
            [{"rows": [{"clicks": 0, "impressions": 0, "ctr": 0, "position": 0}]}]
        )
        totals = client.totals(URL, date(2026, 6, 1), date(2026, 6, 30))
        assert totals.is_empty
        assert totals.position is None
        assert totals.clicks is None

    def test_keyed_rows_apply_the_same_rule(self):
        client, _ = _client(
            [
                {
                    "rows": [
                        {"keys": ["a"], "clicks": 0, "impressions": 0, "position": 0},
                        {"keys": ["b"], "clicks": 0, "impressions": 2, "position": 66},
                    ]
                }
            ]
        )
        rows = client.keyed(URL, date(2026, 6, 1), date(2026, 6, 30), dimensions=["query"])
        assert [row.keys for row in rows] == [("b",)]


class TestProvisionalDays:
    def test_days_from_first_incomplete_are_flagged(self):
        client, _ = _client([TestSyntheticZeros._series_payload()])
        series = client.daily_series(URL, date(2026, 6, 9), date(2026, 6, 13))
        assert series.first_incomplete_date == date(2026, 6, 13)
        by_day = {row.day.isoformat(): row.provisional for row in series.rows}
        assert by_day == {"2026-06-11": False, "2026-06-13": True}

    def test_data_state_all_is_requested(self):
        # `final` не віддає ні свіжих 2 днів, ні `firstIncompleteDate` (§0.7).
        client, session = _client([{"rows": []}])
        client.daily_series(URL, date(2026, 6, 1), date(2026, 6, 30))
        assert session.bodies[0]["dataState"] == "all"


class TestPagination:
    def test_queries_page_past_the_row_limit(self):
        # Живо: одна сторінка блогу має 692 запити (§0.2). Без пагінації
        # панель тихо показувала б перші N і рахувала їх повним набором.
        full = [
            {"keys": [f"q{i}"], "clicks": 0, "impressions": 5, "position": 3}
            for i in range(25000)
        ]
        tail = [{"keys": ["last"], "clicks": 1, "impressions": 9, "position": 2}]
        client, session = _client([{"rows": full}, {"rows": tail}])
        rows = client.queries(URL, date(2026, 6, 1), date(2026, 6, 30))
        assert len(rows) == 25001
        assert session.bodies[1]["startRow"] == 25000

    def test_single_short_page_does_not_ask_for_a_second(self):
        client, session = _client([{"rows": [{"keys": ["q"], "impressions": 1, "position": 4}]}])
        client.queries(URL, date(2026, 6, 1), date(2026, 6, 30))
        assert len(session.bodies) == 1


class TestSearchTypes:
    def test_every_type_stays_in_the_result_even_when_empty(self):
        # frontend/AGENTS.md §6: «сторінка не показується в Картинках» — це
        # відповідь; зникла картка — ні.
        client, _ = _client([{"rows": []} for _ in range(6)])
        totals = client.search_type_totals(URL, date(2026, 6, 1), date(2026, 6, 30))
        assert set(totals) == {"web", "image", "video", "news", "discover", "googleNews"}
        assert all(metrics.is_empty for metrics in totals.values())

    def test_queryless_types_return_no_queries_without_calling_the_api(self):
        # `discover`/`googleNews` не приймають дименсію `query` — питати
        # означало б гарантований 400 на кожному відкритті сторінки.
        client, session = _client()
        rows = client.queries(
            URL, date(2026, 6, 1), date(2026, 6, 30), search_type="discover"
        )
        assert rows == []
        assert session.bodies == []


class TestBucketing:
    @staticmethod
    def _rows(days: list[tuple[str, int, int, float]]) -> dict[str, Any]:
        return {
            "rows": [
                {
                    "keys": [day],
                    "clicks": clicks,
                    "impressions": impressions,
                    "ctr": 0,
                    "position": position,
                }
                for day, clicks, impressions, position in days
            ]
        }

    def test_clicks_are_summed_and_position_is_impression_weighted(self):
        # Просте середнє позиції дало б (60 + 3) / 2 = 31.5, хоча день із 200
        # показами на позиції 3 важить у 200 разів більше за день з одним
        # показом на 60-й. Саме так агрегує сам GSC.
        payload = self._rows(
            [("2026-06-01", 0, 1, 60.0), ("2026-06-02", 5, 200, 3.0)]
        )
        client, _ = _client([payload, {"rows": []}, {"rows": []}])
        window = resolve_window("90d", today=date(2026, 6, 7))
        dto = collect_gsc_core(client, url=URL, window=window)
        points = dto.series["position"].points
        assert len(points) == 1
        assert points[0].value is not None
        assert Decimal("3.2") < points[0].value < Decimal("3.4")
        assert dto.series["clicks"].points[0].value == Decimal(5)

    def test_weekly_bucket_spans_monday_to_sunday(self):
        payload = self._rows([("2026-06-03", 1, 10, 4.0)])  # середа
        client, _ = _client([payload, {"rows": []}, {"rows": []}])
        window = resolve_window("90d", today=date(2026, 6, 7))
        dto = collect_gsc_core(client, url=URL, window=window)
        point = dto.series["clicks"].points[0]
        assert point.period_start == date(2026, 6, 1)
        assert point.period_end == date(2026, 6, 7)
        assert point.period_days == 7


class TestCoreState:
    def test_no_rows_at_all_is_no_data_with_a_window_aware_detail(self):
        client, _ = _client([{"rows": []}, {"rows": []}, {"rows": []}])
        window = resolve_window("16m", today=date(2026, 9, 9))
        dto = collect_gsc_core(client, url=URL, window=window)
        assert dto.state.state == "no_data"
        assert dto.state.detail is not None
        assert "16 months" in dto.state.detail

    def test_query_cap_reports_the_full_count(self):
        rows = [
            {"keys": [f"q{i}"], "clicks": i, "impressions": 100, "position": 3}
            for i in range(10)
        ]
        client, _ = _client([{"rows": []}, {"rows": []}, {"rows": rows}])
        window = resolve_window("90d", today=date(2026, 6, 7))
        dto = collect_gsc_core(client, url=URL, window=window, query_cap=3)
        assert len(dto.queries) == 3
        assert dto.queries_total == 10
        assert dto.queries_truncated is True
        # Найклікабельніші — першими, як їх читає людина.
        assert [q.query for q in dto.queries] == ["q9", "q8", "q7"]


class TestSitemaps:
    def test_indexed_field_never_reaches_the_dto(self):
        # API завжди віддає `indexed: 0` (виміряно на сайті з 248 поданими
        # URL), тож показ нуля читався б як «нічого не проіндексовано».
        from app.schemas.url_inspector import UrlSitemapContentDTO

        assert "indexed" not in UrlSitemapContentDTO.model_fields

    def test_sitemaps_uses_the_get_verb(self):
        client, session = _client()
        client.sitemaps()
        assert session.gets == [
            "https://www.googleapis.com/webmasters/v3/sites/"
            "sc-domain%3Ahearwellservices.com/sitemaps"
        ]


@pytest.mark.parametrize(
    ("metric", "expected"),
    [
        ("clicks", "count"),
        ("impressions", "count"),
        ("ctr", "ratio"),
        ("position", "position"),
        ("engagementRate", "ratio"),
        ("bounceRate", "ratio"),
        ("averageSessionDuration", "seconds"),
        ("userEngagementDuration", "seconds"),
        ("organicGoogleSearchAveragePosition", "position"),
        ("organicGoogleSearchClickThroughRate", "ratio"),
        ("sessions", "count"),
    ],
)
def test_metric_units(metric: str, expected: str):
    """Частка `0..1` під іменем `count` намалювала б «CTR 0.06» замість
    «6.4 %», а секунди під `count` — «694» без одиниці (task-88 units.py)."""
    from app.urlinspector.google.units import unit_of

    assert unit_of(metric) == expected
