from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.webarchive.scheduler import _parse_slot_times, most_recent_slot, slot_key_for

KYIV = ZoneInfo("Europe/Kyiv")
SLOTS = _parse_slot_times(["13:30", "19:30"])


def test_most_recent_slot_same_day_after_second_slot() -> None:
    now = datetime(2026, 9, 4, 20, 0, tzinfo=KYIV)
    result = most_recent_slot(now, SLOTS)
    assert (result.hour, result.minute) == (19, 30)
    assert result.date() == now.date()


def test_most_recent_slot_between_slots() -> None:
    now = datetime(2026, 9, 4, 15, 0, tzinfo=KYIV)
    result = most_recent_slot(now, SLOTS)
    assert (result.hour, result.minute) == (13, 30)
    assert result.date() == now.date()


def test_most_recent_slot_before_first_slot_falls_back_to_yesterday() -> None:
    now = datetime(2026, 9, 4, 6, 0, tzinfo=KYIV)
    result = most_recent_slot(now, SLOTS)
    assert result.date() == now.date() - timedelta(days=1)
    assert (result.hour, result.minute) == (19, 30)


def test_most_recent_slot_exact_boundary_is_inclusive() -> None:
    now = datetime(2026, 9, 4, 13, 30, tzinfo=KYIV)
    result = most_recent_slot(now, SLOTS)
    assert (result.hour, result.minute) == (13, 30)
    assert result.date() == now.date()


def test_slot_key_includes_utc_offset() -> None:
    moment = datetime(2026, 9, 4, 13, 30, tzinfo=KYIV)
    key = slot_key_for(moment)
    assert key.startswith("2026-09-04T13:30")
    assert key.endswith("+03:00")


def test_slot_key_distinguishes_dst_transition() -> None:
    # Kyiv leaves DST on the last Sunday of October — the same wall-clock
    # HH:MM either side of it must never collide onto one `slot_key`
    # (task-59 §1.4/§11: DST double-run or skipped-run risk).
    before_dst_end = datetime(2026, 10, 24, 13, 30, tzinfo=KYIV)
    after_dst_end = datetime(2026, 11, 1, 13, 30, tzinfo=KYIV)
    assert slot_key_for(before_dst_end) != slot_key_for(after_dst_end)
    assert "+03:00" in slot_key_for(before_dst_end)
    assert "+02:00" in slot_key_for(after_dst_end)
