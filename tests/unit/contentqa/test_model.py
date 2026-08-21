from __future__ import annotations

from app.contentqa.model import ContentDoc, ContentItem


def test_none_vs_empty_string_are_distinct() -> None:
    absent = ContentItem(ref="r1", title=None)
    empty = ContentItem(ref="r2", title="")

    assert absent.title is None
    assert empty.title == ""
    assert absent.title != empty.title


def test_content_doc_holds_skipped_sheets() -> None:
    doc = ContentDoc(source="x.xlsx", doc_type="strategy_xlsx", items=(), skipped_sheets=("Notes",))
    assert doc.skipped_sheets == ("Notes",)
    assert doc.items == ()
