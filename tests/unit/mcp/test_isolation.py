"""Статичний запобіжник плану §2: `app/mcp/**` не бачить доменного шару.

AST-обхід, а не `importlib`: імпорт `app.mcp.server` уже й так тягне
кожен tool-модуль (побічний ефект реєстрації, `app/mcp/server.py`), тому
рантайм-перевірка виявила б заборонений імпорт лише як помилку
завантаження всього сервера — пізно й незрозуміло, що саме порушило
правило. AST читає кожен файл окремо, без побічних ефектів запуску.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_MCP_ROOT = Path(__file__).resolve().parents[3] / "app" / "mcp"

# Той самий перелік, що в task-74 §10: доменний шар, БД, провайдери,
# AISeoTools. `app.core.*`/`app.schemas.*` НЕ заборонені — конфіг і чисті
# типи не є доменною логікою (план §2, §7.1 коментар до reports.py).
_FORBIDDEN_PREFIXES = (
    "app.db",
    "app.jobs",
    "app.reports",
    "app.monitor",
    "seo_audit",
    "AISeoTools",
)


def _imported_module_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def _violations(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    bad = []
    for name in _imported_module_names(tree):
        if any(name == p or name.startswith(p + ".") for p in _FORBIDDEN_PREFIXES):
            bad.append(name)
    return bad


def _mcp_source_files() -> list[Path]:
    files = sorted(_MCP_ROOT.rglob("*.py"))
    assert len(files) > 5, f"expected several files under {_MCP_ROOT}, found {len(files)}"
    return files


@pytest.mark.parametrize("path", _mcp_source_files(), ids=lambda p: str(p.relative_to(_MCP_ROOT)))
def test_mcp_module_does_not_import_domain_layer(path: Path) -> None:
    violations = _violations(path)
    assert violations == [], (
        f"{path.relative_to(_MCP_ROOT)} imports forbidden domain module(s) {violations} — "
        "app/mcp/** must talk to the domain layer only through the REST API "
        "(task-74 plan §2), never by importing it directly."
    )
