from __future__ import annotations

from datetime import date

from app.importers.parsers import ParseSkip
from app.importers.parsers.rank_movement import parse


def test_recognized_file_always_skipped_as_info(tmp_path):
    path = tmp_path / "rank_movement_machine.json"
    path.write_text("{}")
    result = parse({"rank_movement_machine.json": path}, fallback_date=date(2026, 1, 1))
    assert isinstance(result, ParseSkip)
    assert result.level == "info"


def test_missing_file_also_skipped_as_info():
    result = parse({}, fallback_date=date(2026, 1, 1))
    assert isinstance(result, ParseSkip)
    assert result.level == "info"
