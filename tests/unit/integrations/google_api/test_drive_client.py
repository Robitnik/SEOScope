"""DriveClient — file resolution, browsing, media download (task-90 §4.1)."""

from __future__ import annotations

import httpx
import pytest

from app.integrations.google_api.drive_client import DriveClient, extract_file_id
from app.integrations.google_api.errors import GoogleApiError

from .fakes import FakeGoogle

_SHARE_ID = "1AbCdEfGhIjKlMnOpQrStUvWxYz012345"


@pytest.mark.parametrize(
    "text,expected",
    [
        (
            "https://docs.google.com/spreadsheets/d/140t5rT6kf2Du9Yu5nLmFFVebAvZ2byg60gbEQsudfkI/edit?usp=sharing",
            "140t5rT6kf2Du9Yu5nLmFFVebAvZ2byg60gbEQsudfkI",
        ),
        (
            "https://docs.google.com/document/d/1aIc6JVW64XFTpqrzbwLwoj34nImxAAkXI9uSNK-N2mY/edit",
            "1aIc6JVW64XFTpqrzbwLwoj34nImxAAkXI9uSNK-N2mY",
        ),
        (f"https://drive.google.com/file/d/{_SHARE_ID}/view", _SHARE_ID),
        (f"https://drive.google.com/open?id={_SHARE_ID}", _SHARE_ID),
        (_SHARE_ID, _SHARE_ID),  # bare id
        ("not a url at all", None),
        ("", None),
    ],
)
def test_extract_file_id(text: str, expected: str | None) -> None:
    assert extract_file_id(text) == expected


def test_about_user_returns_agency_email(fake_google: FakeGoogle) -> None:
    body = {"user": {"emailAddress": "seo@theyovth.com"}}
    fake_google.on("drive/v3/about", httpx.Response(200, json=body))
    assert DriveClient(fake_google.session()).about_user() == "seo@theyovth.com"


def test_get_file_returns_metadata(fake_google: FakeGoogle) -> None:
    fake_google.on(
        "drive/v3/files/abc123",
        httpx.Response(
            200,
            json={
                "id": "abc123",
                "name": "SHB_SEO_STRATEGY_082626",
                "mimeType": "application/vnd.google-apps.spreadsheet",
                "webViewLink": "https://docs.google.com/spreadsheets/d/abc123/edit",
                "modifiedTime": "2026-08-26T10:00:00Z",
                "driveId": "0AGC3U5cQtZujUk9PVA",
            },
        ),
    )
    f = DriveClient(fake_google.session()).get_file("abc123")
    assert f is not None
    assert f.id == "abc123"
    assert f.name == "SHB_SEO_STRATEGY_082626"
    assert f.mime_type == "application/vnd.google-apps.spreadsheet"
    assert f.drive_id == "0AGC3U5cQtZujUk9PVA"


def test_get_file_supports_all_drives_param_is_set(fake_google: FakeGoogle) -> None:
    """Without `supportsAllDrives=true` a shared-drive file 404s even when
    the agency account can see it (task-90 §1.6)."""
    fake_google.on("drive/v3/files/abc123", httpx.Response(200, json={"id": "abc123"}))
    DriveClient(fake_google.session()).get_file("abc123")
    request = fake_google.requests[-1]
    assert request.url.params.get("supportsAllDrives") == "true"


def test_get_file_403_returns_none_not_accessible(fake_google: FakeGoogle) -> None:
    fake_google.on(
        "drive/v3/files/secret",
        httpx.Response(403, json={"error": {"status": "PERMISSION_DENIED", "message": "no"}}),
    )
    assert DriveClient(fake_google.session()).get_file("secret") is None


def test_get_file_404_returns_none_not_accessible(fake_google: FakeGoogle) -> None:
    """Drive answers a file the agency account cannot see with 404, not 403
    (task-90 §1.3) — both must fold into the same `None`/not_accessible
    outcome."""
    body = {"error": {"message": "File not found"}}
    fake_google.on("drive/v3/files/ghost", httpx.Response(404, json=body))
    assert DriveClient(fake_google.session()).get_file("ghost") is None


def test_get_file_404_message_shape(fake_google: FakeGoogle) -> None:
    """Pins the exact message `GoogleApiSession._request` builds for an
    "unexpected" status — `DriveClient.get_file`/`DocsClient.get_document`
    match on this text (no status code on the exception type). If this test
    fails after an `auth.py` change, the 404 matching in both clients needs
    updating too."""
    body = {"error": {"message": "File not found"}}
    fake_google.on("drive/v3/files/ghost", httpx.Response(404, json=body))
    from app.integrations.google_api.auth import GoogleApiSession

    session: GoogleApiSession = fake_google.session()
    with pytest.raises(GoogleApiError, match=r"unexpected HTTP 404"):
        session.get_json("https://www.googleapis.com/drive/v3/files/ghost")


def test_list_files_uses_all_drives_corpora(fake_google: FakeGoogle) -> None:
    """`corpora=allDrives` is mandatory for `list_files` — without it, the
    default `corpora=user` silently hides shared-drive files (task-90 §1.6,
    verified live on THE YOVTH DRIVE)."""
    file_row = {"id": "f1", "name": "SHB_SEO_STRATEGY_082626", "mimeType": "spreadsheet"}
    fake_google.on("drive/v3/files", httpx.Response(200, json={"files": [file_row]}))
    page = DriveClient(fake_google.session()).list_files(query="name contains 'STRATEGY'")
    request = fake_google.requests[-1]
    assert request.url.params.get("corpora") == "allDrives"
    assert request.url.params.get("includeItemsFromAllDrives") == "true"
    assert len(page.files) == 1
    assert page.files[0].name == "SHB_SEO_STRATEGY_082626"


def test_list_files_pagination_token_roundtrips(fake_google: FakeGoogle) -> None:
    fake_google.on(
        "drive/v3/files",
        httpx.Response(200, json={"files": [], "nextPageToken": "tok-2"}),
    )
    page = DriveClient(fake_google.session()).list_files(query="q")
    assert page.next_page_token == "tok-2"


def test_download_media_returns_raw_bytes(fake_google: FakeGoogle) -> None:
    payload = b"PK\x03\x04fake-xlsx-bytes"
    fake_google.on(
        "drive/v3/files/blob1",
        httpx.Response(200, content=payload, headers={"content-type": "application/octet-stream"}),
    )
    result = DriveClient(fake_google.session()).download_media("blob1")
    assert result == payload


def test_download_media_uses_alt_media(fake_google: FakeGoogle) -> None:
    fake_google.on("drive/v3/files/blob1", httpx.Response(200, content=b"x"))
    DriveClient(fake_google.session()).download_media("blob1")
    request = fake_google.requests[-1]
    assert request.url.params.get("alt") == "media"
    assert request.url.params.get("supportsAllDrives") == "true"
