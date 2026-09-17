"""Фейковий GA4-транспорт для тестів тула (task-98).

Мережі немає взагалі: підміняється `GoogleApiSession.post_json`, тож
`Ga4Client` будує справжнє тіло запиту й парсить справжню форму відповіді —
фейковим лишається лише сокет (той самий підхід, що у `FakeGoogle`).

Маршрут — `(місяць, дименсії, поріз)`: поріз розпізнається з `dimensionFilter`
ТІЛЬКИ так, як його будує `service._channel_filter`, тому тест ловить і зміну
дименсії каналу, і підміну CONTAINS на EXACT — на живих даних це рівно та
різниця, через яку відсотки Organic стають 1.22% замість 1.16%.
"""

from __future__ import annotations

from typing import Any


def ga4_response(
    rows: list[tuple[list[str], list[str]]],
    *,
    dimensions: list[str],
    metrics: list[str],
) -> dict[str, Any]:
    return {
        "dimensionHeaders": [{"name": name} for name in dimensions],
        "metricHeaders": [{"name": name} for name in metrics],
        "rows": [
            {
                "dimensionValues": [{"value": value} for value in dims],
                "metricValues": [{"value": value} for value in mets],
            }
            for dims, mets in rows
        ],
        "rowCount": len(rows),
    }


def segment_of(body: dict[str, Any]) -> str:
    """Який поріз питає цей запит — за його власним `dimensionFilter`."""
    payload = body.get("dimensionFilter")
    if payload is None:
        return "all"
    string_filter = payload.get("filter", {}).get("stringFilter", {})
    field = payload.get("filter", {}).get("fieldName")
    if (
        field != "sessionDefaultChannelGroup"
        or string_filter.get("matchType") != "CONTAINS"
    ):
        return f"unexpected:{field}:{string_filter.get('matchType')}"
    return str(string_filter.get("value", "")).lower()


class FakeGa4Session:
    """`post_json(path, body)` -> відповідь із програмованої черги."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._routes: dict[tuple[str, tuple[str, ...], str], Any] = {}

    def route(
        self,
        month: str,
        dimensions: tuple[str, ...],
        payload: Any,
        *,
        segment: str = "all",
    ) -> FakeGa4Session:
        self._routes[(month, dimensions, segment)] = payload
        return self

    def post_json(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        self.calls.append({"path": path, "body": body})
        dimensions = tuple(item["name"] for item in body.get("dimensions") or [])
        month = str(body["dateRanges"][0]["startDate"])[:7]
        payload = self._routes.get((month, dimensions, segment_of(body)))
        if payload is None:
            return {"rows": [], "rowCount": 0}
        if isinstance(payload, Exception):
            raise payload
        return payload

    def get_json(self, path: str) -> dict[str, Any]:  # pragma: no cover - не використовується
        raise AssertionError("ga4 events tool never calls GET")

    def close(self) -> None:
        return None
