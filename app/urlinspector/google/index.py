"""URL Inspection API (task-88 §3.3) — стан індексації одного URL.

Це закриває рядок, який task-61 сам позначив як «ніде — тільки живий curl»:
чи сторінка в індексі, коли Google її бачив останній раз, який канонікал він
обрав, і які rich results на ній розпізнав.

**Ціна — 6641 мс на виклик** (виміряно, §0.5), тобто на порядок більше за всі
інші запити панелі разом. Тому він живе за окремим ендпоінтом і окремою
кнопкою, а не в автозавантаженні: у спільному пулі саме він визначав би час
відповіді всієї сторінки (§0.6).

Квота (за документацією Google) — 2000 запитів на добу і 600 на хвилину на
property. Одна перевірка на клік оператора від цієї межі далеко, але саме
тому це має лишатись кліком.

⚠️ Відсутній блок ≠ «усе гаразд». `mobileUsabilityResult.verdict` на живому
прогоні прийшов як `VERDICT_UNSPECIFIED` (Google згорнув цей звіт), і
`richResultsResult` може не прийти взагалі. Жодне з цих полів не має
підмінятись на `PASS` — `None` тут означає «Google не сказав», і UI мусить
казати те саме.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.integrations.google_api.auth import GoogleApiSession

_INSPECT_URL = "https://searchconsole.googleapis.com/v1/urlInspection/index:inspect"


@dataclass(frozen=True)
class RichResultType:
    rich_result_type: str
    items: int


@dataclass(frozen=True)
class UrlInspection:
    # --- indexStatusResult ---
    verdict: str | None = None
    coverage_state: str | None = None
    robots_txt_state: str | None = None
    indexing_state: str | None = None
    page_fetch_state: str | None = None
    last_crawl_time: datetime | None = None
    crawled_as: str | None = None
    google_canonical: str | None = None
    user_canonical: str | None = None
    referring_urls: list[str] = field(default_factory=list)
    sitemaps: list[str] = field(default_factory=list)
    # --- інші блоки ---
    mobile_usability_verdict: str | None = None
    mobile_usability_issues: list[str] = field(default_factory=list)
    rich_results_verdict: str | None = None
    rich_result_types: list[RichResultType] = field(default_factory=list)
    # Глибокий лінк у сам GSC UI на цей URL (§4, п. 8).
    inspection_result_link: str | None = None

    @property
    def canonical_mismatch(self) -> bool | None:
        """`True`, якщо Google обрав інший канонікал, ніж заявила сторінка.

        `None`, коли хоч одного з двох немає — це «не знаємо», і показувати
        його як «збігається» означало б заспокоїти оператора вигаданим
        фактом.
        """
        if not self.google_canonical or not self.user_canonical:
            return None
        return self.google_canonical.rstrip("/") != self.user_canonical.rstrip("/")


class UrlInspectionClient:
    def __init__(self, session: GoogleApiSession, *, site_url: str) -> None:
        self._session = session
        self._site_url = site_url

    def inspect(self, url: str, *, language_code: str = "en-US") -> UrlInspection:
        payload = self._session.post_json(
            _INSPECT_URL,
            {
                "inspectionUrl": url,
                "siteUrl": self._site_url,
                "languageCode": language_code,
            },
        )
        return _parse(payload)


def _parse(payload: dict[str, Any]) -> UrlInspection:
    result = payload.get("inspectionResult")
    if not isinstance(result, dict):
        return UrlInspection()
    status = result.get("indexStatusResult")
    status = status if isinstance(status, dict) else {}
    mobile = result.get("mobileUsabilityResult")
    mobile = mobile if isinstance(mobile, dict) else {}
    rich = result.get("richResultsResult")
    rich = rich if isinstance(rich, dict) else {}

    return UrlInspection(
        verdict=_text(status.get("verdict")),
        coverage_state=_text(status.get("coverageState")),
        robots_txt_state=_text(status.get("robotsTxtState")),
        indexing_state=_text(status.get("indexingState")),
        page_fetch_state=_text(status.get("pageFetchState")),
        last_crawl_time=_timestamp(status.get("lastCrawlTime")),
        crawled_as=_text(status.get("crawledAs")),
        google_canonical=_text(status.get("googleCanonical")),
        user_canonical=_text(status.get("userCanonical")),
        referring_urls=_str_list(status.get("referringUrls")),
        sitemaps=_str_list(status.get("sitemap")),
        mobile_usability_verdict=_text(mobile.get("verdict")),
        mobile_usability_issues=[
            text
            for issue in (mobile.get("issues") or [])
            if isinstance(issue, dict) and (text := _text(issue.get("message")))
        ],
        rich_results_verdict=_text(rich.get("verdict")),
        rich_result_types=[
            RichResultType(
                rich_result_type=name,
                items=len(item.get("items") or []),
            )
            for item in (rich.get("detectedItems") or [])
            if isinstance(item, dict) and (name := _text(item.get("richResultType")))
        ],
        inspection_result_link=_text(result.get("inspectionResultLink")),
    )


def _text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    # `VERDICT_UNSPECIFIED` — це буквально «Google не має вердикту», і
    # пропускати його далі як значення означало б малювати сірий бейдж з
    # текстом протоколу замість чесного «не сказав».
    if not text or text == "VERDICT_UNSPECIFIED":
        return None
    return text


def _str_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [text for item in value if (text := _text(item))]


def _timestamp(value: object) -> datetime | None:
    text = _text(value)
    if text is None:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
