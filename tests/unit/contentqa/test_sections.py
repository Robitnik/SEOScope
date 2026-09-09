"""contentqa/sections.py — chunk_into_sections (task-90 §4.4)."""

from __future__ import annotations

from app.contentqa.sections import ContentSection, chunk_into_sections


def test_body_paragraphs_attach_to_the_preceding_heading() -> None:
    items = [
        ("H1", "Title"), (None, "Intro paragraph."),
        ("H2", "Section A"), (None, "A body one."), (None, "A body two."),
        ("H2", "Section B"), (None, "B body."),
    ]
    sections = chunk_into_sections(items)
    assert sections == (
        ContentSection(level="H1", heading="Title", text="Intro paragraph."),
        ContentSection(level="H2", heading="Section A", text="A body one.\n\nA body two."),
        ContentSection(level="H2", heading="Section B", text="B body."),
    )


def test_body_before_any_heading_is_dropped() -> None:
    items = [(None, "orphan text"), ("H1", "Title"), (None, "real body")]
    sections = chunk_into_sections(items)
    assert len(sections) == 1
    assert sections[0].text == "real body"


def test_heading_with_no_following_body_has_empty_text() -> None:
    items = [("H1", "Title"), ("H2", "Empty Section"), ("H2", "Next Section"), (None, "body")]
    sections = chunk_into_sections(items)
    assert sections[0].text == ""
    assert sections[1].text == ""
    assert sections[2].text == "body"


def test_empty_input_returns_empty_tuple() -> None:
    assert chunk_into_sections([]) == ()


def test_no_headings_at_all_returns_empty_tuple() -> None:
    assert chunk_into_sections([(None, "just some text")]) == ()
