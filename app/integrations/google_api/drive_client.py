"""Drive API client — file resolution, browsing and blob download for
Strategy Package Check (task-90 §1.1/§1.3/§1.6/§4.1).

Every call carries `supportsAllDrives=true`; `list_files` additionally
carries `includeItemsFromAllDrives=true` + `corpora=allDrives` — without
`corpora` the API defaults to `corpora=user` (My Drive only) and files on a
shared drive are silently invisible. Verified live: the agency's own
"THE YOVTH DRIVE" only surfaced with `corpora=allDrives` set (task-90 §1.6).

`about_user()` doubles as the live proof-of-scope probe from task-90 §1.1
and the source of the address shown in `not_accessible` remediation text
("share this file with <address>", task-90 §1.3) — measured live:
`seo@theyovth.com`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.integrations.google_api.auth import GoogleApiSession
from app.integrations.google_api.errors import GoogleApiError, GoogleApiPermissionError

_DRIVE_API_BASE = "https://www.googleapis.com/drive/v3"
_COMMON_PARAMS = {"supportsAllDrives": "true"}
_FILE_FIELDS = "id,name,mimeType,webViewLink,modifiedTime,driveId"

GOOGLE_SHEET_MIME = "application/vnd.google-apps.spreadsheet"
GOOGLE_DOC_MIME = "application/vnd.google-apps.document"
GOOGLE_FOLDER_MIME = "application/vnd.google-apps.folder"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

# Live URL forms this project has actually seen (task-90 §5): the classic
# `/spreadsheets/d/<id>` / `/document/d/<id>`, the generic `/file/d/<id>`,
# and the older `?id=<id>` query form. `#gid=`/`?tab=t.<id>` suffixes are
# deliberately NOT stripped here — the caller may still want them as a
# sheet/tab hint (task-90 §5, `/resolve`).
_ID_IN_URL_PATTERNS = (
    re.compile(r"/spreadsheets/d/([A-Za-z0-9_-]+)"),
    re.compile(r"/document/d/([A-Za-z0-9_-]+)"),
    re.compile(r"/file/d/([A-Za-z0-9_-]+)"),
    re.compile(r"[?&]id=([A-Za-z0-9_-]+)"),
)
# A bare Drive file id: no live example seen under 25 chars.
_BARE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{15,}$")


def extract_file_id(url_or_id: str) -> str | None:
    """Pull a Drive file id out of any live URL form the operator might
    paste, or pass a bare id through unchanged. `None` when nothing matches —
    the caller turns that into `invalid_drive_url` (task-90 §5), never a
    guess."""
    text = url_or_id.strip()
    if not text:
        return None
    for pattern in _ID_IN_URL_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(1)
    if _BARE_ID_RE.fullmatch(text):
        return text
    return None


@dataclass(frozen=True)
class DriveFile:
    id: str
    name: str
    mime_type: str
    web_view_link: str | None
    modified_time: str | None
    drive_id: str | None  # shared-drive id; None means My Drive


@dataclass(frozen=True)
class DriveFilePage:
    files: tuple[DriveFile, ...]
    next_page_token: str | None


class DriveClient:
    def __init__(self, session: GoogleApiSession) -> None:
        self._session = session

    def about_user(self) -> str | None:
        data = self._session.get_json(
            f"{_DRIVE_API_BASE}/about", params={"fields": "user(emailAddress)"}
        )
        user = data.get("user")
        email = user.get("emailAddress") if isinstance(user, dict) else None
        return email if isinstance(email, str) and email else None

    def get_file(self, file_id: str) -> DriveFile | None:
        """`None` — not raised — when the id is well-formed but the agency
        account cannot see it. 403 and 404 both fold into this: Drive
        answers a file you have no access to with 404 (not "this exists but
        you can't see it", by design), so a genuinely missing file and an
        unshared one look identical from here, and both need the exact same
        `not_accessible` remediation (task-90 §1.3).

        NOTE on the 404 branch: `GoogleApiSession._request` (task-83/88)
        does not carry the HTTP status on its exception types — an
        "unexpected" status becomes a generic `GoogleApiError` whose message
        is literally `f"unexpected HTTP {status}: ..."`. Matching that text
        is fragile in principle; it is covered by
        `test_drive_client.py::test_get_file_404_message_shape` so a change
        to that message format in `auth.py` fails CI here instead of
        silently breaking file resolution.
        """
        try:
            data = self._session.get_json(
                f"{_DRIVE_API_BASE}/files/{file_id}",
                params={**_COMMON_PARAMS, "fields": _FILE_FIELDS},
            )
        except GoogleApiPermissionError:
            return None
        except GoogleApiError as exc:
            if "HTTP 404" in str(exc):
                return None
            raise
        return _parse_file(data)

    def list_files(
        self, *, query: str, page_size: int = 25, page_token: str | None = None
    ) -> DriveFilePage:
        """Browse/search across My Drive and every shared drive the agency
        account can see (task-90 §1.6/§5, `GET /drive/files`)."""
        params: dict[str, str] = {
            **_COMMON_PARAMS,
            "includeItemsFromAllDrives": "true",
            "corpora": "allDrives",
            "q": query,
            "pageSize": str(page_size),
            "fields": f"nextPageToken,files({_FILE_FIELDS})",
        }
        if page_token:
            params["pageToken"] = page_token
        data = self._session.get_json(f"{_DRIVE_API_BASE}/files", params=params)
        files = tuple(
            _parse_file(f) for f in data.get("files", []) or [] if isinstance(f, dict)
        )
        next_token = data.get("nextPageToken")
        return DriveFilePage(
            files=files, next_page_token=next_token if isinstance(next_token, str) else None
        )

    def download_media(self, file_id: str) -> bytes:
        """Raw bytes of a non-Workspace file (e.g. an uploaded `.xlsx` blob
        living on Drive) via `files.get?alt=media` — NOT `files.export`
        (Workspace-only, capped at 10 MB, task-90 §1.5). The caller applies
        its own size guard before calling this; there is no size cap here."""
        return self._session.get_bytes(
            f"{_DRIVE_API_BASE}/files/{file_id}", params={**_COMMON_PARAMS, "alt": "media"}
        )


def _str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _parse_file(data: dict[str, Any]) -> DriveFile:
    return DriveFile(
        id=str(data.get("id") or ""),
        name=str(data.get("name") or ""),
        mime_type=str(data.get("mimeType") or ""),
        web_view_link=_str_or_none(data.get("webViewLink")),
        modified_time=_str_or_none(data.get("modifiedTime")),
        drive_id=_str_or_none(data.get("driveId")),
    )
