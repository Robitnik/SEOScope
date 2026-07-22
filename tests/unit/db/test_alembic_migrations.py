"""`alembic upgrade head` / `downgrade base` run clean on an empty database."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config

from app.core.config import get_settings

_REPO_ROOT = Path(__file__).resolve().parents[3]


def test_upgrade_head_then_downgrade_base_is_clean(tmp_path, monkeypatch):
    db_path = tmp_path / "alembic_test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    get_settings.cache_clear()
    try:
        cfg = Config(str(_REPO_ROOT / "alembic.ini"))
        command.upgrade(cfg, "head")
        assert db_path.exists()

        command.downgrade(cfg, "base")
        command.upgrade(cfg, "head")  # re-applying from scratch must also succeed
    finally:
        get_settings.cache_clear()
