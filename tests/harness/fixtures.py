"""Sanitized fixtures (PLAN Етап 0) і збирання тимчасового `--source` дерева.

Усі файли в `tests/fixtures/` — знеособлені: домени з зарезервованого
`example.com`, коди клієнтів вигадані, `connection_key` — неіснуючий
`seo@example.com`. Жоден реальний ключ, сесія чи адреса клієнта сюди не
потрапляє (PLAN §13); за цим стежить `tests/integration/test_security.py`.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"
AISEOTOOLS_FIXTURE_SOURCE = FIXTURES_DIR / "aiseotools_source"


def load_fixture(relative_path: str) -> Any:
    """Прочитати JSON-фікстуру за шляхом відносно `tests/fixtures/`."""
    return json.loads((FIXTURES_DIR / relative_path).read_text(encoding="utf-8"))


def build_aiseotools_source(destination: Path, *, with_client_report: bool = True) -> Path:
    """Розгорнути копію sanitized `--source` дерева у тимчасову директорію.

    Копія, а не сам `tests/fixtures/`: імпортер зобов'язаний не писати в
    `--source`, і тест має право це порушити навмисно (schema drift), не
    псуючи закомічені фікстури.
    """
    destination.mkdir(parents=True, exist_ok=True)
    shutil.copytree(AISEOTOOLS_FIXTURE_SOURCE, destination, dirs_exist_ok=True)
    if with_client_report:
        write_client_report(
            destination / "out" / "client_report_ACME-DENTAL_2026-06-01.xlsx",
            title="ACME-DENTAL — Monthly History",
            rows=[
                ("2026-03-01", 900, 21000, "4.3%", 12),
                ("2026-04-01", 1010, 23400, "4.3%", 15),
                ("2026-05-01", 1180, 25100, "4.7%", 18),
            ],
        )
    return destination


def write_client_report(
    path: Path, *, title: str, rows: list[tuple[Any, ...]]
) -> Path:
    """Згенерувати `client_report_<CODE>_<date>.xlsx` з аркушем «Historical Data».

    Генерується, а не комітиться: xlsx — бінарник, у якому неможливо на очі
    перевірити відсутність секретів перед комітом (ризик із плану таски).
    """
    from openpyxl import Workbook

    workbook = Workbook()
    workbook.remove(workbook.active)
    sheet = workbook.create_sheet("Historical Data")
    sheet.append([title])  # у реальних вивантаженнях над шапкою є рядок заголовка
    sheet.append(["Date", "Clicks", "Impressions", "CTR", "Organic Conversions"])
    for row in rows:
        sheet.append(list(row))
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(path)
    return path
