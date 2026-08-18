"""One filename convention, shared by the writer (`ninja_browser.py`) and the
reader (`pdf_parse.py`) — task-42 §3.7.7: `{slug}__{period}__{from}_{to}.pdf`,
deterministic per `(report_name, period_label, date_from, date_to)`. Sharing
this function means a period's PDF is looked up by building its exact
expected name, never by globbing for a fuzzy match.
"""

from __future__ import annotations

import re
from datetime import date

_SLUG_RE = re.compile(r"[^\w]+")


def slugify(text: str) -> str:
    return _SLUG_RE.sub("_", text.strip().lower()).strip("_") or "x"


def pdf_filename(report_name: str, period_label: str, date_from: date, date_to: date) -> str:
    return (
        f"{slugify(report_name)}__{slugify(period_label)}__"
        f"{date_from.isoformat()}_{date_to.isoformat()}.pdf"
    )
