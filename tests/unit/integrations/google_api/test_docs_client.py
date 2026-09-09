"""DocsClient — full tab tree via `includeTabsContent=true` (task-90 §1.4/§4.1)."""

from __future__ import annotations

import httpx
import pytest

from app.integrations.google_api.docs_client import DocsClient

from .fakes import FakeGoogle


def _para(text: str, style: str | None = None) -> dict[str, object]:
    style_block: dict[str, object] = {"namedStyleType": style} if style else {}
    return {
        "paragraph": {
            "paragraphStyle": style_block,
            "elements": [{"textRun": {"content": text}}],
        }
    }


def test_get_document_requires_include_tabs_content(fake_google: FakeGoogle) -> None:
    """Live-verified (task-90 §1.4a): without `includeTabsContent=true`,
    `tabs` stays empty and only the first tab's content comes back."""
    fake_google.on("documents/doc1", httpx.Response(200, json={"tabs": []}))
    DocsClient(fake_google.session()).get_document("doc1")
    request = fake_google.requests[-1]
    assert request.url.params.get("includeTabsContent") == "true"


def test_single_unnamed_tab(fake_google: FakeGoogle) -> None:
    """Live state: 11 of 40 real documents have only a default `Tab 1`
    (task-90 §2.3)."""
    fake_google.on(
        "documents/doc1",
        httpx.Response(
            200,
            json={
                "tabs": [
                    {
                        "tabProperties": {"tabId": "t.0", "title": "Tab 1"},
                        "documentTab": {"body": {"content": [_para("Meta Page Title: Example")]}},
                    }
                ]
            },
        ),
    )
    tabs = DocsClient(fake_google.session()).get_document("doc1")
    assert tabs is not None
    assert len(tabs) == 1
    assert tabs[0].title == "Tab 1"
    assert tabs[0].path == ("Tab 1",)
    assert len(tabs[0].body_content) == 1


def test_framework_plus_content_tabs(fake_google: FakeGoogle) -> None:
    """Live majority shape: `Framework` + `Content` as siblings (task-90 §2.3)."""
    fake_google.on(
        "documents/doc1",
        httpx.Response(
            200,
            json={
                "tabs": [
                    {
                        "tabProperties": {"tabId": "t.0", "title": "Framework"},
                        "documentTab": {
                            "body": {"content": [_para("H2: What Is It?", "HEADING_2")]}
                        },
                    },
                    {
                        "tabProperties": {"tabId": "t.1", "title": "Content"},
                        "documentTab": {"body": {"content": [_para("Real published prose.")]}},
                    },
                ]
            },
        ),
    )
    tabs = DocsClient(fake_google.session()).get_document("doc1")
    assert tabs is not None
    titles = [t.title for t in tabs]
    assert titles == ["Framework", "Content"]


def test_child_tabs_are_flattened_with_path(fake_google: FakeGoogle) -> None:
    fake_google.on(
        "documents/doc1",
        httpx.Response(
            200,
            json={
                "tabs": [
                    {
                        "tabProperties": {"tabId": "t.0", "title": "Page A"},
                        "documentTab": {"body": {"content": []}},
                        "childTabs": [
                            {
                                "tabProperties": {"tabId": "t.0.0", "title": "Content"},
                                "documentTab": {"body": {"content": [_para("nested prose")]}},
                            }
                        ],
                    }
                ]
            },
        ),
    )
    tabs = DocsClient(fake_google.session()).get_document("doc1")
    assert tabs is not None
    assert len(tabs) == 2
    assert tabs[0].path == ("Page A",)
    assert tabs[1].path == ("Page A", "Content")


def test_get_document_403_returns_none(fake_google: FakeGoogle) -> None:
    fake_google.on(
        "documents/secret",
        httpx.Response(403, json={"error": {"status": "PERMISSION_DENIED", "message": "no"}}),
    )
    assert DocsClient(fake_google.session()).get_document("secret") is None


def test_get_document_404_returns_none(fake_google: FakeGoogle) -> None:
    fake_google.on("documents/ghost", httpx.Response(404, json={"error": {"message": "not found"}}))
    assert DocsClient(fake_google.session()).get_document("ghost") is None


def test_get_document_404_message_shape(fake_google: FakeGoogle) -> None:
    """Same fragility note as `test_drive_client.py`: the 404 branch matches
    on `_request`'s generated message text, not a status code on the
    exception."""
    from app.integrations.google_api.auth import GoogleApiSession
    from app.integrations.google_api.errors import GoogleApiError

    fake_google.on("documents/ghost", httpx.Response(404, json={"error": {"message": "not found"}}))
    session: GoogleApiSession = fake_google.session()
    with pytest.raises(GoogleApiError, match=r"unexpected HTTP 404"):
        session.get_json("https://docs.googleapis.com/v1/documents/ghost")


def test_document_without_tabs_key_returns_empty_tuple(fake_google: FakeGoogle) -> None:
    """A malformed/legacy response with no `tabs` field at all — must not
    crash, must not silently look like a real one-tab document."""
    fake_google.on("documents/doc1", httpx.Response(200, json={}))
    tabs = DocsClient(fake_google.session()).get_document("doc1")
    assert tabs == ()
