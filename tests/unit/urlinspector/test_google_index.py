"""`app.urlinspector.google.index` + `sources/google_index` — стан індексації
(task-88 §3.3).

Центральна теза цих тестів: **відсутнє поле ≠ «усе гаразд»**. Живий прогін
віддав `mobileUsabilityResult.verdict = "VERDICT_UNSPECIFIED"` (Google згорнув
цей звіт), і показати це як `PASS` означало б заспокоїти оператора вигаданим
фактом.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.urlinspector.google.gsc import GscUrlClient
from app.urlinspector.google.index import UrlInspectionClient
from app.urlinspector.sources.google_index import collect_index_status

URL = "https://hearwellservices.com/about-us/jessica-m-frankel/"

# Дослівна відповідь живого API на URL із запиту (task-88 §0.5).
LIVE_PAYLOAD: dict[str, Any] = {
    "inspectionResult": {
        "inspectionResultLink": "https://search.google.com/search-console/inspect?x=1",
        "indexStatusResult": {
            "verdict": "PASS",
            "coverageState": "Submitted and indexed",
            "robotsTxtState": "ALLOWED",
            "indexingState": "INDEXING_ALLOWED",
            "lastCrawlTime": "2026-08-25T01:19:08Z",
            "pageFetchState": "SUCCESSFUL",
            "googleCanonical": "https://hearwellservices.com/about-us/jessica-m-frankel/",
            "userCanonical": "https://hearwellservices.com/about-us/jessica-m-frankel/",
            "referringUrls": [
                "https://hearwellservices.com/blog/why-hearing-aids-stop-sounding-clear/",
                "https://hearwellservices.com/about-us/",
            ],
            "crawledAs": "MOBILE",
        },
        "mobileUsabilityResult": {"verdict": "VERDICT_UNSPECIFIED"},
        "richResultsResult": {
            "verdict": "PASS",
            "detectedItems": [
                {"richResultType": "Breadcrumbs", "items": [{"name": "Unnamed item"}]}
            ],
        },
    }
}

LIVE_SITEMAPS: dict[str, Any] = {
    "sitemap": [
        {
            "path": "https://hearwellservices.com/sitemap_index.xml",
            "lastSubmitted": "2025-02-18T06:15:31.796Z",
            "isPending": False,
            "isSitemapsIndex": True,
            "lastDownloaded": "2026-09-08T01:40:34.202Z",
            "warnings": "1",
            "errors": "0",
            "contents": [
                {"type": "web", "submitted": "248", "indexed": "0"},
                {"type": "image", "submitted": "515", "indexed": "0"},
            ],
        }
    ]
}


class FakeSession:
    def __init__(self, post: dict[str, Any], get: dict[str, Any] | None = None) -> None:
        self._post = post
        self._get = get if get is not None else {"sitemap": []}
        self.get_failure: Exception | None = None

    def post_json(self, url: str, body: dict[str, Any]) -> dict[str, Any]:
        self.body = body
        return self._post

    def get_json(self, url: str, *, params: dict[str, str] | None = None) -> dict[str, Any]:
        if self.get_failure is not None:
            raise self.get_failure
        return self._get


def _collect(
    post: dict[str, Any] = LIVE_PAYLOAD, get: dict[str, Any] | None = LIVE_SITEMAPS
):
    session = FakeSession(post, get)
    inspector = UrlInspectionClient(session, site_url="sc-domain:x.com")  # type: ignore[arg-type]
    gsc = GscUrlClient(session, site_url="sc-domain:x.com")  # type: ignore[arg-type]
    return collect_index_status(inspector, gsc, url=URL), session


class TestLiveShape:
    def test_every_index_status_field_survives(self):
        dto, _ = _collect()
        assert dto.state.state == "has_data"
        assert dto.verdict == "PASS"
        assert dto.coverage_state == "Submitted and indexed"
        assert dto.robots_txt_state == "ALLOWED"
        assert dto.indexing_state == "INDEXING_ALLOWED"
        assert dto.page_fetch_state == "SUCCESSFUL"
        assert dto.crawled_as == "MOBILE"

    def test_last_crawl_time_is_parsed_as_an_aware_datetime(self):
        dto, _ = _collect()
        assert isinstance(dto.last_crawl_time, datetime)
        assert dto.last_crawl_time.tzinfo is not None

    def test_referring_urls_are_kept_in_order(self):
        dto, _ = _collect()
        assert len(dto.referring_urls) == 2
        assert dto.referring_urls[0].endswith("why-hearing-aids-stop-sounding-clear/")

    def test_rich_result_types_carry_their_item_counts(self):
        dto, _ = _collect()
        assert [(r.rich_result_type, r.items) for r in dto.rich_result_types] == [
            ("Breadcrumbs", 1)
        ]

    def test_deep_link_into_the_gsc_ui_is_passed_through(self):
        dto, _ = _collect()
        assert dto.inspection_result_link is not None
        assert dto.inspection_result_link.startswith("https://search.google.com/")

    def test_the_request_names_both_the_url_and_the_property(self):
        _, session = _collect()
        assert session.body["inspectionUrl"] == URL
        assert session.body["siteUrl"] == "sc-domain:x.com"


class TestUnspecifiedIsNotPass:
    def test_verdict_unspecified_becomes_none(self):
        # Живо: Google згорнув mobile-usability звіт.
        dto, _ = _collect()
        assert dto.mobile_usability_verdict is None

    def test_missing_rich_results_block_is_none_not_pass(self):
        payload = {"inspectionResult": {"indexStatusResult": {"verdict": "PASS"}}}
        dto, _ = _collect(post=payload)
        assert dto.rich_results_verdict is None
        assert dto.rich_result_types == []

    def test_empty_response_is_no_data_not_a_clean_bill(self):
        dto, _ = _collect(post={})
        assert dto.state.state == "no_data"
        assert dto.verdict is None
        assert dto.state.detail is not None


class TestCanonicalMismatch:
    def test_matching_canonicals(self):
        dto, _ = _collect()
        assert dto.canonical_mismatch is False

    def test_differing_canonicals_are_flagged(self):
        payload = {
            "inspectionResult": {
                "indexStatusResult": {
                    "verdict": "PASS",
                    "googleCanonical": "https://x.com/other/",
                    "userCanonical": "https://x.com/a/",
                }
            }
        }
        dto, _ = _collect(post=payload)
        assert dto.canonical_mismatch is True

    def test_trailing_slash_alone_is_not_a_mismatch(self):
        payload = {
            "inspectionResult": {
                "indexStatusResult": {
                    "verdict": "PASS",
                    "googleCanonical": "https://x.com/a",
                    "userCanonical": "https://x.com/a/",
                }
            }
        }
        dto, _ = _collect(post=payload)
        assert dto.canonical_mismatch is False

    def test_one_side_missing_is_unknown_not_a_match(self):
        payload = {
            "inspectionResult": {
                "indexStatusResult": {
                    "verdict": "PASS",
                    "googleCanonical": "https://x.com/a/",
                }
            }
        }
        dto, _ = _collect(post=payload)
        assert dto.canonical_mismatch is None


class TestSitemaps:
    def test_counts_and_timestamps_are_parsed_from_strings(self):
        dto, _ = _collect()
        assert dto.sitemaps_state.state == "has_data"
        sitemap = dto.sitemaps[0]
        assert sitemap.warnings == 1
        assert sitemap.errors == 0
        assert sitemap.is_sitemaps_index is True
        assert sitemap.is_pending is False
        assert sitemap.last_downloaded is not None

    def test_submitted_counts_survive_but_indexed_is_dropped(self):
        # API завжди віддає `indexed: 0` — показ нуля читався б як «нічого
        # не проіндексовано» на сайті з 248 поданими URL.
        dto, _ = _collect()
        contents = dto.sitemaps[0].contents
        assert [(c.type, c.submitted) for c in contents] == [("web", 248), ("image", 515)]
        assert not any(hasattr(c, "indexed") for c in contents)

    def test_a_sitemap_failure_does_not_cost_the_index_verdict(self):
        # Ізоляція клітинки (§2.5): дешевий сусідній запит не має гасити
        # відповідь, за яку заплатили 6.6 с.
        session = FakeSession(LIVE_PAYLOAD, LIVE_SITEMAPS)
        session.get_failure = RuntimeError("boom")
        inspector = UrlInspectionClient(session, site_url="sc-domain:x.com")  # type: ignore[arg-type]
        gsc = GscUrlClient(session, site_url="sc-domain:x.com")  # type: ignore[arg-type]
        dto = collect_index_status(inspector, gsc, url=URL)
        assert dto.verdict == "PASS"
        assert dto.sitemaps_state.state == "failed"
        assert dto.sitemaps == []

    def test_no_sitemaps_is_no_data(self):
        dto, _ = _collect(get={"sitemap": []})
        assert dto.sitemaps_state.state == "no_data"
