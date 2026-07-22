#!/usr/bin/env python
"""Перегенерувати знімок публічних сигнатур AISeoTools (task-13).

Запускати **лише** свідомо, після адитивної зміни в AISeoTools, і показувати
дифф у рев'ю: цей файл — межа, яка ловить неадитивні зміни (PLAN §8).

    docker compose exec api poetry run python scripts/update_aiseotools_snapshot.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "tests"))
sys.path.insert(0, str(REPO_ROOT / "tests" / "contract"))


def main() -> int:
    try:
        from test_aiseotools_signatures import SNAPSHOT_PATH, build_surface
    except ImportError as exc:  # pragma: no cover - діагностика для людини
        print(f"не вдалось імпортувати контрактний модуль: {exc}", file=sys.stderr)
        return 1

    surface = build_surface()
    SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_PATH.write_text(
        json.dumps(surface, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    symbols = sum(len(values) for values in surface.values())
    print(f"знято {symbols} символів у {len(surface)} групах -> {SNAPSHOT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
