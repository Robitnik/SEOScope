"""Docs API client — a document's full tab tree (task-90 §1.4/§1.4a/§4.1).

`includeTabsContent=true` is not optional: verified live (task-90 §1.4a) —
without it, `document.tabs` stays empty and `document.body` holds only the
FIRST tab's content; the rest of the document is not returned at all. Every
call here always asks for all tabs.

This client is a thin transport, same shape as `GscClient`/`Ga4Client`: it
returns the tab tree flattened with raw `StructuralElement` content per tab,
and leaves paragraph/heading semantics to `app.contentqa.readers.gdoc` — the
same split as `run_report()` returning generic rows and leaving the
canonical-key mapping to its caller.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.integrations.google_api.auth import GoogleApiSession
from app.integrations.google_api.errors import GoogleApiError, GoogleApiPermissionError

_DOCS_API_BASE = "https://docs.googleapis.com/v1"


@dataclass(frozen=True)
class DocTab:
    """One tab, or child tab, of a Google Doc — unflattened into a single
    list so a reader can build a human `ref` like `"MyDoc › Page A ›
    Content"` from `path` (task-90 §2.3) without re-walking the tree."""

    tab_id: str
    title: str
    path: tuple[str, ...]  # ancestor titles + this tab's title, root-first
    body_content: tuple[dict[str, Any], ...]  # raw StructuralElement list


class DocsClient:
    def __init__(self, session: GoogleApiSession) -> None:
        self._session = session

    def get_document(self, document_id: str) -> tuple[DocTab, ...] | None:
        """All tabs, depth-first. `None` when the agency account cannot see
        this document — same 403/404 fold-together as
        `DriveClient.get_file` and the same message-matching caveat for 404
        (see its docstring); covered here by
        `test_docs_client.py::test_get_document_404_message_shape`."""
        try:
            data = self._session.get_json(
                f"{_DOCS_API_BASE}/documents/{document_id}",
                params={"includeTabsContent": "true"},
            )
        except GoogleApiPermissionError:
            return None
        except GoogleApiError as exc:
            if "HTTP 404" in str(exc):
                return None
            raise
        tabs = data.get("tabs")
        return tuple(_walk_tabs(tabs if isinstance(tabs, list) else [], path=()))


def _walk_tabs(tabs: list[Any], *, path: tuple[str, ...]) -> list[DocTab]:
    out: list[DocTab] = []
    for tab in tabs:
        if not isinstance(tab, dict):
            continue
        props = tab.get("tabProperties") or {}
        raw_title = str(props.get("title") or "").strip()
        title = raw_title or "(untitled)"
        tab_id = str(props.get("tabId") or "")
        here = path + (title,)
        body = ((tab.get("documentTab") or {}).get("body") or {}).get("content")
        content = tuple(e for e in body if isinstance(e, dict)) if isinstance(body, list) else ()
        out.append(DocTab(tab_id=tab_id, title=title, path=here, body_content=content))
        children = tab.get("childTabs")
        if isinstance(children, list):
            out.extend(_walk_tabs(children, path=here))
    return out
