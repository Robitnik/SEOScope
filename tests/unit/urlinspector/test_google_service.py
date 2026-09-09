"""`app.urlinspector.google_service` — оркестратор Google-панелей (task-88 §2.5).

Той самий інваріант, що `test_sources_isolation.py` перевіряє для збережених
джерел, застосований до мережевого шляху: збій в ОДНОМУ запиті стає станом
СВОЄЇ панелі, ніколи не 500 на всю сторінку. Плюс окрема лінія: відсутність
налаштування — це `not_configured`, а не `failed`, бо це різні дії оператора
(видати доступ проти шукати баг).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from unittest.mock import patch

import pytest

from app.db.models import Client, ClientIntegration
from app.integrations.google_api import (
    GoogleApiConfigError,
    GoogleApiPermissionError,
)
from app.schemas.enums import Provider
from app.urlinspector import google_service
from app.urlinspector.google_service import (
    GoogleTargetUnavailable,
    inspect_url_google_breakdowns,
    inspect_url_google_core,
    inspect_url_index_status,
)

URL = "https://acme.com/a/"


def _seed(session, *, gsc: bool = True, ga4: bool = True, transport: str | None = None) -> Client:
    client = Client(name="Acme", slug="acme", domain="acme.com")
    session.add(client)
    session.flush()
    if gsc:
        config: dict[str, Any] = {"account_id": "sc-domain:acme.com"}
        if transport:
            config["transport"] = transport
        session.add(
            ClientIntegration(
                client_id=client.id, provider=Provider.gsc, enabled=True, config_json=config
            )
        )
    if ga4:
        config = {"account_id": "properties/1"}
        if transport:
            config["transport"] = transport
        session.add(
            ClientIntegration(
                client_id=client.id,
                provider=Provider.reporting_ninja,
                enabled=True,
                config_json=config,
            )
        )
    session.commit()
    return client


class _NullSession:
    """`GoogleApiSession`, що на будь-який запит віддає порожнечу."""

    def __enter__(self) -> _NullSession:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def post_json(self, url: str, body: dict[str, Any]) -> dict[str, Any]:
        return {"rows": [], "rowCount": 0}

    def get_json(self, url: str, *, params: dict[str, str] | None = None) -> dict[str, Any]:
        return {"metrics": [], "sitemap": []}


@pytest.fixture
def null_api():
    with patch.object(google_service, "open_google_api_session", lambda *a, **k: _NullSession()):
        yield


class TestTargetResolution:
    def test_unknown_host_is_unknown_client_not_a_crash(self, session, null_api):
        _seed(session)
        with pytest.raises(GoogleTargetUnavailable) as exc:
            inspect_url_google_core(session, url="https://someone-else.com/a")
        assert exc.value.state == "unknown_client"

    def test_client_without_any_google_integration(self, session, null_api):
        _seed(session, gsc=False, ga4=False)
        with pytest.raises(GoogleTargetUnavailable) as exc:
            inspect_url_google_core(session, url=URL)
        assert exc.value.state == "not_configured"
        assert exc.value.detail

    def test_gsc_only_client_still_renders_with_ga4_not_configured(self, session, null_api):
        _seed(session, ga4=False)
        dto = inspect_url_google_core(session, url=URL)
        assert dto.gsc.state.state in ("has_data", "no_data")
        assert dto.ga4.state.state == "not_configured"
        assert dto.ga4.state.detail is not None
        assert "GA4" in dto.ga4.state.detail

    def test_ga4_only_client_still_renders_with_gsc_not_configured(self, session, null_api):
        _seed(session, gsc=False)
        dto = inspect_url_google_core(session, url=URL)
        assert dto.gsc.state.state == "not_configured"
        assert dto.ga4.state.state in ("has_data", "no_data")

    def test_disabled_integration_is_not_configured_not_silently_used(self, session, null_api):
        client = _seed(session, ga4=False)
        session.add(
            ClientIntegration(
                client_id=client.id,
                provider=Provider.reporting_ninja,
                enabled=False,
                config_json={"account_id": "properties/1"},
            )
        )
        session.commit()
        dto = inspect_url_google_core(session, url=URL)
        assert dto.ga4.state.state == "not_configured"
        assert "disabled" in (dto.ga4.state.detail or "")

    def test_transport_is_reported_so_the_ui_can_label_the_source(self, session, null_api):
        _seed(session, transport="google_api")
        assert inspect_url_google_core(session, url=URL).transport == "google_api"

    def test_default_transport_stays_reporting_ninja(self, session, null_api):
        # Гейт G8: клієнт без явного `transport` не змінює поведінки.
        _seed(session)
        assert inspect_url_google_core(session, url=URL).transport == "reporting_ninja"


class TestCellIsolation:
    def test_a_broken_gsc_call_leaves_ga4_alive(self, session, null_api):
        _seed(session)
        with patch.object(
            google_service, "collect_gsc_core", side_effect=RuntimeError("boom")
        ):
            dto = inspect_url_google_core(session, url=URL)
        assert dto.gsc.state.state == "failed"
        assert "boom" in (dto.gsc.state.detail or "")
        assert dto.ga4.state.state != "failed"

    def test_a_broken_ga4_call_leaves_gsc_alive(self, session, null_api):
        _seed(session)
        with patch.object(
            google_service, "collect_ga4_core", side_effect=RuntimeError("boom")
        ):
            dto = inspect_url_google_core(session, url=URL)
        assert dto.ga4.state.state == "failed"
        assert dto.gsc.state.state != "failed"

    def test_missing_refresh_token_is_not_configured_not_failed(self, session):
        # `failed` послало б оператора шукати баг там, де треба видати доступ.
        _seed(session)

        def _boom(*_a: object, **_k: object) -> None:
            raise GoogleApiConfigError("google_api_refresh_token must be set")

        with patch.object(google_service, "open_google_api_session", _boom):
            with pytest.raises(GoogleApiConfigError):
                inspect_url_google_core(session, url=URL)

    def test_permission_denied_on_one_property_is_not_configured(self, session, null_api):
        _seed(session)
        with patch.object(
            google_service,
            "collect_gsc_core",
            side_effect=GoogleApiPermissionError("403 PERMISSION_DENIED"),
        ):
            dto = inspect_url_google_core(session, url=URL)
        assert dto.gsc.state.state == "not_configured"
        assert dto.ga4.state.state != "failed"

    def test_a_broken_breakdown_group_becomes_a_row_not_a_disappearance(
        self, session, null_api
    ):
        # Оператор мусить бачити різницю між «GA4-зрізів немає» і
        # «GA4-зрізи не завантажились».
        _seed(session)
        with patch.object(
            google_service, "collect_ga4_breakdowns", side_effect=RuntimeError("boom")
        ):
            dto = inspect_url_google_breakdowns(session, url=URL)
        failed = [b for b in dto.breakdowns if b.state.state == "failed"]
        assert [b.key for b in failed] == ["ga4_breakdowns"]
        # GSC-зрізи на місці й не постраждали.
        assert any(b.key == "gsc_device" for b in dto.breakdowns)

    def test_index_status_needs_gsc_and_says_so(self, session, null_api):
        _seed(session, gsc=False)
        with pytest.raises(GoogleTargetUnavailable) as exc:
            inspect_url_index_status(session, url=URL)
        assert "Search Console" in exc.value.detail


class TestCrossCheck:
    @staticmethod
    def _run(session, gsc_totals: dict[str, Any], organic: dict[str, Any]):
        from app.schemas.url_inspector import UrlGoogleGscDTO, UrlSourceStateDTO
        from app.urlinspector.google.gsc import GscMetrics
        from app.urlinspector.sources.google_gsc import _metrics_list

        gsc_dto = UrlGoogleGscDTO(
            state=UrlSourceStateDTO(source="gsc", state="has_data"),
            totals=_metrics_list(GscMetrics(**gsc_totals)),
        )
        return google_service._cross_check(gsc_dto, organic, None)

    def test_identical_numbers_match(self, session):
        # Живий прогін: 3 / 39 / 8.897 обома шляхами.
        dto = self._run(
            session,
            {
                "clicks": Decimal(3),
                "impressions": Decimal(39),
                "ctr": Decimal("0.07"),
                "position": Decimal("8.8974358974"),
            },
            {
                "organicGoogleSearchClicks": Decimal(3),
                "organicGoogleSearchImpressions": Decimal(39),
                "organicGoogleSearchAveragePosition": Decimal("8.8974358974"),
            },
        )
        assert dto.matches is True
        assert dto.state.state == "has_data"

    def test_position_within_half_a_place_still_matches(self):
        dto = TestCrossCheck._run(
            None,
            {
                "clicks": Decimal(3),
                "impressions": Decimal(39),
                "ctr": None,
                "position": Decimal("8.9"),
            },
            {
                "organicGoogleSearchClicks": Decimal(3),
                "organicGoogleSearchImpressions": Decimal(39),
                "organicGoogleSearchAveragePosition": Decimal("9.2"),
            },
        )
        assert dto.matches is True

    def test_one_impression_of_73030_is_not_a_disagreement(self):
        # Живо на 3XE: 73 031 проти 73 030. Два шляхи агрегації Google
        # округлюють по-різному; точна рівність тут дала б хибну тривогу на
        # кожній другій сторінці.
        dto = TestCrossCheck._run(
            None,
            {
                "clicks": Decimal(7297),
                "impressions": Decimal(73031),
                "ctr": None,
                "position": Decimal("3.4571072"),
            },
            {
                "organicGoogleSearchClicks": Decimal(7297),
                "organicGoogleSearchImpressions": Decimal(73030),
                "organicGoogleSearchAveragePosition": Decimal("3.4571409"),
            },
        )
        assert dto.matches is True

    def test_a_broken_url_match_is_still_caught(self):
        # Живо на SHB до розширення regex: питали один хост, рахували обидва —
        # 15 372 проти 18 144, тобто 18 %. Саме це звірка існує щоб ловити.
        dto = TestCrossCheck._run(
            None,
            {
                "clicks": Decimal(15372),
                "impressions": Decimal(303188),
                "ctr": None,
                "position": Decimal("6.54"),
            },
            {
                "organicGoogleSearchClicks": Decimal(18144),
                "organicGoogleSearchImpressions": Decimal(359316),
                "organicGoogleSearchAveragePosition": Decimal("6.71"),
            },
        )
        assert dto.matches is False

    def test_tiny_pages_use_the_absolute_floor(self):
        # 2 проти 3 кліків — 50 %, але одиниця різниці. Підлога в 1 не дає
        # звірці кричати на сторінках, де будь-який відсоток менший за клік.
        dto = TestCrossCheck._run(
            None,
            {"clicks": Decimal(2), "impressions": Decimal(40), "ctr": None, "position": Decimal(9)},
            {
                "organicGoogleSearchClicks": Decimal(3),
                "organicGoogleSearchImpressions": Decimal(40),
                "organicGoogleSearchAveragePosition": Decimal(9),
            },
        )
        assert dto.matches is True

    def test_diverging_counts_do_not_match(self):
        dto = TestCrossCheck._run(
            None,
            {
                "clicks": Decimal(3),
                "impressions": Decimal(39),
                "ctr": None,
                "position": Decimal("8.9"),
            },
            {
                "organicGoogleSearchClicks": Decimal(3),
                "organicGoogleSearchImpressions": Decimal(12),
                "organicGoogleSearchAveragePosition": Decimal("8.9"),
            },
        )
        assert dto.matches is False

    def test_one_side_absent_is_unknown_not_a_mismatch(self):
        dto = TestCrossCheck._run(
            None,
            {
                "clicks": Decimal(3),
                "impressions": Decimal(39),
                "ctr": None,
                "position": Decimal("8.9"),
            },
            {
                "organicGoogleSearchClicks": None,
                "organicGoogleSearchImpressions": None,
                "organicGoogleSearchAveragePosition": None,
            },
        )
        assert dto.matches is None
        assert dto.state.state == "no_data"

    def test_no_gsc_ga4_link_at_all_is_no_data(self):
        dto = TestCrossCheck._run(
            None,
            {
                "clicks": Decimal(3),
                "impressions": Decimal(39),
                "ctr": None,
                "position": Decimal("8.9"),
            },
            None,  # type: ignore[arg-type]
        )
        assert dto.state.state == "no_data"
        assert dto.matches is None
        # Бік GSC усе одно видно — половина звірки корисніша за порожнечу.
        assert dto.gsc_impressions == Decimal(39)
