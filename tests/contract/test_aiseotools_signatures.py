"""Контрактна регресія AISeoTools (PLAN §8, §15).

Зміни в AISeoTools дозволені **тільки адитивні**. Цей гейт ловить те, що
найлегше зламати непомітно: перейменування функції, зміну порядку/імен
параметрів або зникнення значення за замовчуванням.

Знімається дві поверхні:

1. **Нова** — увесь публічний namespace `seo_audit.dashboard.*` (task-08…11);
   це прямий контракт із адаптерами `app/integrations/*`.
2. **Спожита стара** — рівно ті довколишні символи `seo_audit.*`, які
   dashboard-модулі імпортують у себе. Список збирається з AST самих
   модулів, а не руками: якщо wrapper почне спиратись на ще одну стару
   функцію, вона потрапить під охорону автоматично.

Повний прогін старої test suite — окремим кроком (`scripts/aiseotools_regression.sh`),
бо він довгий і потребує браузерних залежностей.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import json
import pkgutil
from pathlib import Path

import pytest

pytestmark = pytest.mark.contract

SNAPSHOT_PATH = Path(__file__).parent / "data" / "aiseotools_signatures.json"

seo_audit = pytest.importorskip(
    "seo_audit",
    reason="AISeoTools не встановлено (CI/deploy без sibling-checkout) — гейт лише локально",
)


def _signature_of(name: str, obj: object) -> str:
    """`name(...)` або `name(<опис недоступний>)` для builtin-подібних класів.

    Частина винятків AISeoTools не має власного `__init__`, тож
    `inspect.signature` на них падає. Для гейта важлива сама наявність
    символу, тому такий випадок фіксується, а не ламає знімок.
    """
    try:
        return f"{name}{inspect.signature(obj)}"  # type: ignore[arg-type]
    except (ValueError, TypeError):
        return f"{name}(<опис недоступний>)"


def _dashboard_modules() -> list[str]:
    from seo_audit import dashboard

    names = [f"seo_audit.dashboard.{m.name}" for m in pkgutil.iter_modules(dashboard.__path__)]
    return sorted(["seo_audit.dashboard", *names])


def _public_signatures(module_name: str) -> dict[str, str]:
    module = importlib.import_module(module_name)
    exported = getattr(module, "__all__", None)
    signatures: dict[str, str] = {}
    for name, obj in vars(module).items():
        if name.startswith("_"):
            continue
        if exported is not None and name not in exported:
            continue
        if getattr(obj, "__module__", None) != module_name:
            continue  # реекспорт — знімається у свого модуля
        if inspect.isfunction(obj) or inspect.isclass(obj):
            signatures[name] = _signature_of(name, obj)
    return signatures


def _consumed_legacy_symbols() -> dict[str, str]:
    """Старі символи `seo_audit.*`, які імпортують dashboard-модулі."""
    consumed: dict[str, str] = {}
    for module_name in _dashboard_modules():
        module = importlib.import_module(module_name)
        source_file = getattr(module, "__file__", None)
        if source_file is None:
            continue
        tree = ast.parse(Path(source_file).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.module is None:
                continue
            if not node.module.startswith("seo_audit.") or node.module.startswith(
                "seo_audit.dashboard"
            ):
                continue
            origin = importlib.import_module(node.module)
            for alias in node.names:
                obj = getattr(origin, alias.name, None)
                if obj is None or not (inspect.isfunction(obj) or inspect.isclass(obj)):
                    continue
                consumed[f"{node.module}.{alias.name}"] = _signature_of(alias.name, obj)
    return consumed


def build_surface() -> dict[str, dict[str, str]]:
    surface = {name: _public_signatures(name) for name in _dashboard_modules()}
    surface["__consumed_legacy__"] = _consumed_legacy_symbols()
    return {name: sigs for name, sigs in surface.items() if sigs}


def test_snapshot_exists() -> None:
    assert SNAPSHOT_PATH.is_file(), (
        f"немає знімка {SNAPSHOT_PATH}; перегенеруй: "
        "docker compose exec api poetry run python scripts/update_aiseotools_snapshot.py"
    )


def test_public_surface_matches_snapshot() -> None:
    expected = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    actual = build_surface()

    removed = {
        f"{module}.{name}"
        for module, signatures in expected.items()
        for name in signatures
        if name not in actual.get(module, {})
    }
    assert not removed, (
        f"публічні символи зникли або перейменовані (заборонено, PLAN §8): {sorted(removed)}"
    )

    changed = {
        f"{module}.{name}": (signature, actual[module][name])
        for module, signatures in expected.items()
        for name, signature in signatures.items()
        if actual.get(module, {}).get(name) != signature
    }
    assert not changed, f"сигнатури змінились неадитивно (PLAN §8): {changed}"


def test_new_arguments_are_optional_keyword_only() -> None:
    """Додані параметри мусять бути optional keyword-only з дефолтом (PLAN §8.2)."""
    expected = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    offenders: list[str] = []

    for module_name in _dashboard_modules():
        module = importlib.import_module(module_name)
        known = expected.get(module_name, {})
        for name, obj in vars(module).items():
            if name.startswith("_") or not inspect.isfunction(obj):
                continue
            if getattr(obj, "__module__", None) != module_name or name not in known:
                continue
            for parameter in inspect.signature(obj).parameters.values():
                if parameter.name in known[name]:
                    continue
                is_additive = (
                    parameter.kind == inspect.Parameter.KEYWORD_ONLY
                    and parameter.default is not inspect.Parameter.empty
                )
                if not is_additive:
                    offenders.append(f"{module_name}.{name}({parameter.name})")

    assert not offenders, f"неадитивно додані параметри: {offenders}"


def test_importing_the_dashboard_namespace_leaves_legacy_modules_importable() -> None:
    """Namespace адитивний: імпорт `dashboard` нічого старого не ламає.

    Саме цей сценарій ловить випадкову підміну — dashboard-модуль з іменем
    легасі-модуля або import-side-effect, після якого стара CLI-команда
    падає ще на імпорті.
    """
    importlib.import_module("seo_audit.dashboard")

    legacy = sorted(
        m.name
        for m in pkgutil.iter_modules(seo_audit.__path__)
        if m.name != "dashboard" and not m.name.startswith("_")
    )
    assert legacy, "у seo_audit має лишатись стара кодова база поруч із namespace"

    broken: dict[str, str] = {}
    for name in legacy:
        try:
            importlib.import_module(f"seo_audit.{name}")
        except Exception as exc:  # noqa: BLE001 - будь-яка поломка імпорту тут є регресією
            broken[name] = f"{type(exc).__name__}: {exc}"
    assert not broken, f"старі модулі перестали імпортуватись: {broken}"
