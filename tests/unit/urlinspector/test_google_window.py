"""`app.urlinspector.google.window` — вікно й бакети (task-88 §2.3/§0.7).

Головне, що тут перевіряється: **межа історії GSC мовчазна**. Запит за неї
віддає `200 OK` з нулем рядків, тож якщо вікно не рахує цю межу саме, UI
скаже «No data» там, де правда — «Search Console стільки не зберігає».
"""

from __future__ import annotations

from datetime import date

import pytest

from app.urlinspector.google.window import (
    WINDOW_KEYS,
    bucket_of,
    resolve_window,
)

TODAY = date(2026, 9, 9)


class TestWindowKeys:
    @pytest.mark.parametrize("key", WINDOW_KEYS)
    def test_every_advertised_key_resolves_to_itself(self, key: str):
        assert resolve_window(key, today=TODAY).key == key

    @pytest.mark.parametrize("key", [None, "", "7d", "all", "90D", "../etc/passwd"])
    def test_unknown_key_falls_back_quietly_to_90d(self, key: str | None):
        # Тихий дефолт замість 400: вікно приходить із hash-URL, і зламане
        # посилання в чаті не має віддавати помилку замість сторінки.
        assert resolve_window(key, today=TODAY).key == "90d"

    def test_buckets_keep_the_chart_readable(self):
        # 365 денних точок читаються як шум, а не як тренд.
        buckets = {key: resolve_window(key, today=TODAY).bucket for key in WINDOW_KEYS}
        assert buckets == {"28d": "day", "90d": "week", "12m": "month", "16m": "month"}

    def test_window_lengths(self):
        assert resolve_window("28d", today=TODAY).days == 28
        assert resolve_window("90d", today=TODAY).days == 90
        assert resolve_window("12m", today=TODAY).days == 365


class TestGscHistoryEdge:
    def test_short_windows_are_nowhere_near_the_limit(self):
        for key in ("28d", "90d", "12m"):
            window = resolve_window(key, today=TODAY)
            assert window.at_gsc_history_edge is False
            assert window.clamped_to_gsc_history is False

    def test_longest_window_is_flagged_as_reaching_the_limit(self):
        # Це і є гейт G5. Прапорець мусить бути `True` саме для наявного
        # найдовшого вікна — інакше текст «за межею історії» не показався б
        # ніколи, і поле було б мертвим (цю помилку вже раз зробили).
        window = resolve_window("16m", today=TODAY)
        assert window.at_gsc_history_edge is True

    def test_start_never_goes_past_the_limit(self):
        window = resolve_window("16m", today=TODAY)
        assert (TODAY - window.start).days <= 16 * 30 + 6

    def test_requested_start_is_preserved_for_the_ui(self):
        window = resolve_window("16m", today=TODAY)
        assert window.requested_start <= window.start


class TestBucketOf:
    def test_day_bucket_is_the_day_itself(self):
        assert bucket_of(date(2026, 6, 10), "day") == (date(2026, 6, 10), date(2026, 6, 10))

    def test_week_bucket_is_monday_to_sunday(self):
        # Середа 2026-06-03 → тиждень 06-01..06-07.
        assert bucket_of(date(2026, 6, 3), "week") == (date(2026, 6, 1), date(2026, 6, 7))

    def test_monday_is_its_own_week_start(self):
        assert bucket_of(date(2026, 6, 1), "week")[0] == date(2026, 6, 1)

    def test_sunday_stays_in_the_week_that_started_on_monday(self):
        assert bucket_of(date(2026, 6, 7), "week") == (date(2026, 6, 1), date(2026, 6, 7))

    @pytest.mark.parametrize(
        ("day", "expected"),
        [
            (date(2026, 2, 15), (date(2026, 2, 1), date(2026, 2, 28))),
            (date(2024, 2, 15), (date(2024, 2, 1), date(2024, 2, 29))),  # лютий у високосному
            (date(2026, 12, 31), (date(2026, 12, 1), date(2026, 12, 31))),
            (date(2026, 1, 1), (date(2026, 1, 1), date(2026, 1, 31))),
        ],
    )
    def test_month_bucket_handles_every_month_length(
        self, day: date, expected: tuple[date, date]
    ):
        assert bucket_of(day, "month") == expected

    def test_bucket_bounds_do_not_depend_on_the_window(self):
        # Той самий тиждень мусить мати ті самі межі у двох різних вікнах —
        # інакше серії перестають порівнюватись між собою.
        assert bucket_of(date(2026, 6, 3), "week") == bucket_of(date(2026, 6, 5), "week")
