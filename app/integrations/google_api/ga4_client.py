"""GA4 Data API client — the `ga4_daily` / `ga4_daily_channels` transport
(task-83 §3.3/§5).

`POST /v1beta/{property}:runReport`, where `{property}` is the value already
stored in `client_integrations.config_json["account_id"]` (`properties/<id>`).

Two traps, both already paid for by this project and recorded in session
memory:

- `conversions` and `keyEvents` in one request error out. Only `keyEvents` is
  ever requested here — that is exactly what `ga4_daily` calls `conversions`.
- The `month` dimension draws false zeros. Only `date` is used; bucketing
  into weeks/months stays the job of `app.metrics.portfolio`.

`run_report` is a thin transport: it returns generic `Ga4Row`s and leaves the
GA4-field ➜ canonical-key mapping to the caller (`reporting_ninja.py` reuses
its own `_GA4_DATASETS` / `_CHANNEL_METRIC_FIELDS` for that).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from app.integrations.google_api.auth import GoogleApiSession

_GA4_API_BASE = "https://analyticsdata.googleapis.com/v1beta"
# GA4 Data API caps a single page at 250 000 rows; 100 000 is plenty for a
# 400-day × ~10-channel query (~4 000 rows) with headroom.
_PAGE_LIMIT = 100_000


@dataclass(frozen=True)
class Ga4Row:
    dimensions: dict[str, str]
    metrics: dict[str, Decimal | None]


class Ga4Client:
    def __init__(self, session: GoogleApiSession) -> None:
        self._session = session

    def run_report(
        self,
        property_id: str,
        start: date,
        end: date,
        *,
        dimensions: Sequence[str],
        metrics: Sequence[str],
    ) -> list[Ga4Row]:
        """One `runReport`, paged by `offset`/`limit` to `rowCount`.

        A `date` dimension value is normalized from GA4's `YYYYMMDD` to
        `YYYY-MM-DD`. A day/combination the API does not return is simply
        absent — never synthesized as a zero row (PLAN §12).
        """
        path = f"{_GA4_API_BASE}/{property_id}:runReport"
        dim_names = list(dimensions)
        met_names = list(metrics)
        out: list[Ga4Row] = []
        offset = 0
        while True:
            payload = self._session.post_json(
                path,
                {
                    "dateRanges": [
                        {"startDate": start.isoformat(), "endDate": end.isoformat()}
                    ],
                    "dimensions": [{"name": name} for name in dim_names],
                    "metrics": [{"name": name} for name in met_names],
                    "limit": _PAGE_LIMIT,
                    "offset": offset,
                },
            )
            rows = payload.get("rows") or []
            header_dims = [h.get("name") for h in payload.get("dimensionHeaders") or []]
            header_mets = [h.get("name") for h in payload.get("metricHeaders") or []]
            for row in rows:
                dim_values = [d.get("value") for d in row.get("dimensionValues") or []]
                met_values = [m.get("value") for m in row.get("metricValues") or []]
                out.append(
                    Ga4Row(
                        dimensions={
                            str(name): _normalize_dimension(str(name), value)
                            for name, value in zip(
                                header_dims or dim_names, dim_values, strict=False
                            )
                        },
                        metrics={
                            str(name): _num(value)
                            for name, value in zip(
                                header_mets or met_names, met_values, strict=False
                            )
                        },
                    )
                )
            offset += len(rows)
            row_count = _int(payload.get("rowCount"))
            if not rows or (row_count is not None and offset >= row_count):
                return out


def _normalize_dimension(name: str, value: object) -> str:
    text = str(value or "").strip()
    if name == "date" and len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return text


def _num(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _int(value: object) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None
