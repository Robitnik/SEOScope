"""Одиниця кожної метрики Google API (task-88).

Одне місце на всі панелі, бо помилка тут не видна на графіку. Три пастки,
за які проєкт уже платив:

- `ctr`, `engagementRate`, `bounceRate` API віддає **часткою `0..1`**, а
  Reporting Ninja у БД — **відсотком**. Позначити частку як `count` означало б
  намалювати «CTR 0.06» замість «6.4 %», а помножити відсоток удруге —
  «638 %» (task-83 §7.3, пастка ×100).
- `position` менша = краща, тобто напрямок «краще» протилежний решті —
  саме тому це власна одиниця, а не `count` (PLAN §12: колір визначає
  `better_direction`, не знак).
- `averageSessionDuration`/`userEngagementDuration` — **секунди**, і як
  `count` вони читалися б як кількість чогось.
"""

from __future__ import annotations

from typing import Literal

MetricUnit = Literal["count", "ratio", "position", "seconds"]

_RATIO_METRICS = frozenset(
    {
        "ctr",
        "engagementRate",
        "bounceRate",
        "sessionKeyEventRate",
        "userKeyEventRate",
        "organicGoogleSearchClickThroughRate",
    }
)

_POSITION_METRICS = frozenset({"position", "organicGoogleSearchAveragePosition"})

_SECONDS_METRICS = frozenset({"averageSessionDuration", "userEngagementDuration"})


def unit_of(metric: str) -> MetricUnit:
    if metric in _RATIO_METRICS:
        return "ratio"
    if metric in _POSITION_METRICS:
        return "position"
    if metric in _SECONDS_METRICS:
        return "seconds"
    return "count"
