"""Юніти джерела стратегії (task-102 §2): розпізнавання входу, межа шляху, Drive."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from openpyxl import Workbook

from app.core.config import Settings
from app.integrations.google_api import GoogleApiConfigError
from app.integrations.google_api.drive_client import (
    GOOGLE_DOC_MIME,
    GOOGLE_SHEET_MIME,
    XLSX_MIME,
    DriveClient,
    DriveFile,
)
from app.integrations.google_api.sheets_client import CellLink, SheetCell, SheetGrid, SheetsClient
from app.prompts.strategy_source import (
    MAX_XLSX_BYTES,
    StrategySourceError,
    list_local_strategies,
    load_strategy,
)

FILE_ID = "1UKjvBsyiNxgPDpseDJxPdNjwZ78HYOYtcaeIuT8PmdQ"


def _settings(tmp_path: Path) -> Settings:
    root = tmp_path / "aiseotools"
    (root / "out" / "GRP_2026-08-26_blog_strategy").mkdir(parents=True)
    return Settings(
        aiseotools_source_path=root, aiseotools_host_path="/home/operator/seo/AISeoTools"
    )  # type: ignore[call-arg]


def _write_xlsx(path: Path) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "Blogs"
    ws.append(["Article Topic", "Keyword", "Title"])
    ws.append(["Topic", "kw", "T"])
    wb.save(path)


class _Drive(DriveClient):
    def __init__(self, files: dict[str, DriveFile], blob: bytes = b"") -> None:
        self._files = files
        self._blob = blob

    def about_user(self) -> str | None:
        return "seo@theyovth.com"

    def get_file(self, file_id: str) -> DriveFile | None:
        return self._files.get(file_id)

    def download_media(self, file_id: str) -> bytes:
        return self._blob


class _Sheets(SheetsClient):
    def get_grid(self, spreadsheet_id: str, *, links_only: bool = False) -> tuple[SheetGrid, ...]:
        chip = CellLink(form="chip", uri="https://docs.google.com/document/d/x")
        return (
            SheetGrid(title="B", index=1, rows=((SheetCell("h", ()),),)),
            SheetGrid(title="A", index=0, rows=((SheetCell("h", (chip,)),),)),
        )


class _Clients:
    def __init__(self, drive: DriveClient) -> None:
        self.drive = drive
        self.sheets = _Sheets.__new__(_Sheets)


def _file(mime: str = GOOGLE_SHEET_MIME) -> DriveFile:
    return DriveFile(
        id=FILE_ID,
        name="GRP_SEOStrategy",
        mime_type=mime,
        web_view_link="https://docs.google.com/x",
        modified_time="2026-09-30T12:00:00Z",
        drive_id=None,
    )


def _no_google() -> Any:
    raise AssertionError("Google must not be opened for a path")


def test_google_sheets_link_reads_grid_in_sheet_order_with_links(tmp_path: Path) -> None:
    clients = _Clients(_Drive({FILE_ID: _file()}))

    loaded = load_strategy(
        f"https://docs.google.com/spreadsheets/d/{FILE_ID}/edit?usp=sharing",
        settings=_settings(tmp_path),
        open_clients=lambda: clients,
    )

    assert loaded.kind == "gsheet" and loaded.ref == FILE_ID and loaded.name == "GRP_SEOStrategy"
    assert [name for name, _ in loaded.sheets] == ["A", "B"]
    assert loaded.sheets[0][1][0][0].links == ("https://docs.google.com/document/d/x",)


def test_bare_drive_id_is_accepted(tmp_path: Path) -> None:
    clients = _Clients(_Drive({FILE_ID: _file()}))

    loaded = load_strategy(FILE_ID, settings=_settings(tmp_path), open_clients=lambda: clients)

    assert loaded.ref == FILE_ID


@pytest.mark.parametrize("junk", ["", "   ", "hello world", "short"])
def test_unrecognized_input_is_refused(tmp_path: Path, junk: str) -> None:
    with pytest.raises(StrategySourceError) as err:
        load_strategy(junk, settings=_settings(tmp_path), open_clients=_no_google)
    assert err.value.code == "unrecognized_source"


def test_inaccessible_file_names_the_account_to_share_with(tmp_path: Path) -> None:
    with pytest.raises(StrategySourceError) as err:
        load_strategy(
            FILE_ID, settings=_settings(tmp_path), open_clients=lambda: _Clients(_Drive({}))
        )
    assert (err.value.status, err.value.code) == (404, "file_not_accessible")
    assert "seo@theyovth.com" in err.value.message


def test_google_doc_is_not_a_strategy(tmp_path: Path) -> None:
    clients = _Clients(_Drive({FILE_ID: _file(GOOGLE_DOC_MIME)}))
    with pytest.raises(StrategySourceError) as err:
        load_strategy(FILE_ID, settings=_settings(tmp_path), open_clients=lambda: clients)
    assert err.value.code == "unsupported_source"


def test_drive_not_configured_is_a_503(tmp_path: Path) -> None:
    def _raise() -> Any:
        raise GoogleApiConfigError("no token")

    with pytest.raises(StrategySourceError) as err:
        load_strategy(FILE_ID, settings=_settings(tmp_path), open_clients=_raise)
    assert (err.value.status, err.value.code) == (503, "drive_not_configured")


def test_xlsx_blob_on_drive_is_size_guarded(tmp_path: Path) -> None:
    clients = _Clients(_Drive({FILE_ID: _file(XLSX_MIME)}, blob=b"x" * (MAX_XLSX_BYTES + 1)))
    with pytest.raises(StrategySourceError) as err:
        load_strategy(FILE_ID, settings=_settings(tmp_path), open_clients=lambda: clients)
    assert err.value.status == 413


@pytest.mark.parametrize(
    "spelling",
    [
        "/home/operator/seo/AISeoTools/out/GRP_2026-08-26_blog_strategy/GRP_BLOG.xlsx",  # хостовий
        "out/GRP_2026-08-26_blog_strategy/GRP_BLOG.xlsx",  # відносний
        "./out/GRP_2026-08-26_blog_strategy/GRP_BLOG.xlsx",
    ],
)
def test_path_spellings_resolve_to_the_same_file(tmp_path: Path, spelling: str) -> None:
    settings = _settings(tmp_path)
    _write_xlsx(
        settings.aiseotools_source_path / "out" / "GRP_2026-08-26_blog_strategy" / "GRP_BLOG.xlsx"
    )

    loaded = load_strategy(spelling, settings=settings, open_clients=_no_google)

    assert loaded.kind == "xlsx" and loaded.name == "GRP_BLOG"
    assert loaded.ref == "out/GRP_2026-08-26_blog_strategy/GRP_BLOG.xlsx"
    assert loaded.sheets[0][0] == "Blogs"
    assert loaded.sheets[0][1][1][0].text == "Topic"


@pytest.mark.parametrize(
    "escape",
    ["../../etc/passwd.xlsx", "/etc/passwd.xlsx", "/home/operator/seo/AISeoTools/../secret.xlsx"],
)
def test_path_outside_the_root_is_refused(tmp_path: Path, escape: str) -> None:
    with pytest.raises(StrategySourceError) as err:
        load_strategy(escape, settings=_settings(tmp_path), open_clients=_no_google)
    assert err.value.code == "path_outside_aiseotools"


def test_symlink_escaping_the_root_is_refused(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    outside = tmp_path / "secret.xlsx"
    _write_xlsx(outside)
    (settings.aiseotools_source_path / "out" / "link.xlsx").symlink_to(outside)

    with pytest.raises(StrategySourceError) as err:
        load_strategy("out/link.xlsx", settings=settings, open_clients=_no_google)
    assert err.value.code == "path_outside_aiseotools"


def test_non_xlsx_missing_and_corrupt_files(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    out = settings.aiseotools_source_path / "out"
    (out / "notes.txt").write_text("x")
    (out / "bad.xlsx").write_bytes(b"not a workbook")

    for spelling, code in (
        ("out/notes.txt", "unsupported_source"),
        ("out/missing.xlsx", "strategy_file_not_found"),
        ("out/bad.xlsx", "unreadable_strategy"),
    ):
        with pytest.raises(StrategySourceError) as err:
            load_strategy(spelling, settings=settings, open_clients=_no_google)
        assert err.value.code == code, spelling
    # Повідомлення про відсутній файл називає ХОСТОВИЙ шлях, а не контейнерний.
    with pytest.raises(StrategySourceError) as err:
        load_strategy("out/missing.xlsx", settings=settings, open_clients=_no_google)
    assert "/home/operator/seo/AISeoTools/out/missing.xlsx" in err.value.message


def test_local_listing_skips_check_artifacts_and_sorts_newest_first(tmp_path: Path) -> None:
    import os

    settings = _settings(tmp_path)
    folder = settings.aiseotools_source_path / "out" / "GRP_2026-08-26_blog_strategy"
    for name, mtime in (
        ("GRP_BLOG_STRATEGY_Q32026.xlsx", 1_000),
        ("GRP_BLOG_STRATEGY_Q42026.xlsx", 2_000),
        ("GRP_STRATEGY_CHECK_X.xlsx", 3_000),
        ("GRP_PAGE_STRATEGY_before_fix.xlsx", 3_000),
        ("summary.xlsx", 3_000),
    ):
        _write_xlsx(folder / name)
        os.utime(folder / name, (mtime, mtime))

    listing = list_local_strategies(settings)

    assert [f.name for f in listing] == [
        "GRP_BLOG_STRATEGY_Q42026.xlsx",
        "GRP_BLOG_STRATEGY_Q32026.xlsx",
    ]
    assert listing[0].path_host == (
        "/home/operator/seo/AISeoTools/out/GRP_2026-08-26_blog_strategy/GRP_BLOG_STRATEGY_Q42026.xlsx"
    )
