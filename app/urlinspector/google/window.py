"""Вікно запиту й бакетизація серії (task-88 §2.3).

Заміняє поточну гранулярність `week|month|quarter` над **нерівними**
збереженими періодами (task-88 §0.1) на пару «вікно + бакет» над денними
рядками з API: тепер бакет — наше рішення, а не наслідок того, коли
випадково запускався збір.

Дві межі, обидві виміряні живо (§0.7) і обидві **мовчазні** з боку Google —
запит за межею історії віддає `200 OK` з нулем рядків, без жодної помилки.
Тому вікно обрізається тут, і факт обрізання доїжджає до UI окремим полем:
`No data` і `Beyond Search Console's 16-month history` — різні відповіді, і
плутати їх означає збрехати оператору (PLAN §12).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal

Window = Literal["28d", "90d", "12m", "16m"]
Bucket = Literal["day", "week", "month"]

WINDOW_KEYS: tuple[Window, ...] = ("28d", "90d", "12m", "16m")

# Скільки днів назад від `end` бере кожне вікно, і яким бакетом малюється
# серія. Бакет вибраний так, щоб на графіку лишалось 12-30 точок: 365 денних
# точок на 12-місячному вікні читаються як шум, а не як тренд.
_WINDOW_SPEC: dict[Window, tuple[int, Bucket]] = {
    "28d": (28, "day"),
    "90d": (90, "week"),
    "12m": (365, "month"),
    "16m": (486, "month"),
}

# Google не публікує день-в-день, і остання доба завжди неповна. `dataState`
# нижче все одно віддає свіжі дні з `firstIncompleteDate`, тож вікно
# закінчується "сьогодні": обрізати кінець наперед означало б викинути дані,
# які API вже має.
_GSC_HISTORY_DAYS = 16 * 30 + 6  # ~16 місяців, стеля Search Analytics API

# Наскільки близько до стелі вікно вже вважається «на межі». Причина в тому,
# що стеля рухома: Google щодня зсуває її на добу вперед, і точний день, з
# якого дані зникають, ми не знаємо. Тому «на межі» — це смуга, а не точка.
_GSC_HISTORY_EDGE_DAYS = 14


@dataclass(frozen=True)
class GoogleWindow:
    key: Window
    start: date
    end: date
    bucket: Bucket
    # `True`, коли початок вікна впирається в 16-місячну стелю Search Console
    # (або був підтягнутий вперед, якщо просили ще давніше). Поле існує рівно
    # тому, що API про стелю НЕ повідомляє: запит за неї віддає `200 OK` з
    # нулем рядків (§0.7). Без цього прапорця порожній лівий край графіка
    # виглядав би як «трафіку не було», а не як «Search Console стільки не
    # зберігає».
    at_gsc_history_edge: bool
    # `True`, лише якщо вікно ще й обрізане — тобто запитаний початок ліг за
    # стелю і `start` не той, що просили.
    clamped_to_gsc_history: bool
    # Початок, який просив оператор, до обрізання — щоб UI міг показати
    # «просили з X, GSC віддає з Y».
    requested_start: date

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1


def resolve_window(key: str | None, *, today: date | None = None) -> GoogleWindow:
    """`GoogleWindow` для ключа вікна; невідомий/відсутній ключ → `"90d"`.

    Тихий дефолт замість помилки свідомий: вікно приходить із hash-URL
    сторінки, а зламане посилання в чаті не повинно віддавати 400 замість
    сторінки.
    """
    today = today or date.today()
    window: Window = key if key in WINDOW_KEYS else "90d"  # type: ignore[assignment]
    span_days, bucket = _WINDOW_SPEC[window]
    end = today
    requested_start = end - timedelta(days=span_days - 1)
    earliest = end - timedelta(days=_GSC_HISTORY_DAYS - 1)
    clamped = requested_start < earliest
    start = earliest if clamped else requested_start
    return GoogleWindow(
        key=window,
        start=start,
        end=end,
        bucket=bucket,
        at_gsc_history_edge=start
        <= earliest + timedelta(days=_GSC_HISTORY_EDGE_DAYS),
        clamped_to_gsc_history=clamped,
        requested_start=requested_start,
    )


def bucket_of(day: date, bucket: Bucket) -> tuple[date, date]:
    """Межі бакета, у який падає `day` — [початок, кінець] включно.

    Тиждень — понеділок..неділя (ISO), місяць — календарний. Межі
    НЕ обрізаються вікном: бакет має бути стабільним ключем, інакше той самий
    тиждень отримає різні межі у двох різних вікнах і серії перестануть
    порівнюватись між собою.
    """
    if bucket == "day":
        return day, day
    if bucket == "week":
        start = day - timedelta(days=day.weekday())
        return start, start + timedelta(days=6)
    start = day.replace(day=1)
    next_month = (start + timedelta(days=31)).replace(day=1)
    return start, next_month - timedelta(days=1)
