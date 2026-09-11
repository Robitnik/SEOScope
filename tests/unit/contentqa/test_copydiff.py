"""copydiff.py — посегментна звірка Copy ↔ живий сайт (task-92).

Фікстури — прямі DocBlock/SiteBlock (не через readers/gdoc.py чи HTML-
парсер), той самий принцип ізоляції, що інші юніти цього пакета: тестуємо
`align_segments`/`normalize`/`genre_of` як чисту функцію над готовим
входом.
"""

from __future__ import annotations

from app.contentqa.copydiff import (
    build_copy_tab_result,
    diff_summary,
    diff_words,
    genre_of,
    normalize,
    segment_doc_blocks,
    segment_site_blocks,
    split_sentences,
    stats_of,
)
from app.contentqa.readers.gdoc import DocBlock
from app.contentqa.site import SiteBlock


def _doc(*items: tuple[str, str | None, bool]) -> list[DocBlock]:
    """`(text, level, has_marker)` -> `DocBlock(kind inferred)`."""
    out = []
    for text, level, has_marker in items:
        kind = "heading" if level else "para"
        out.append(DocBlock(kind=kind, level=level, text=text, has_marker=has_marker))
    return out


def _site(*texts: str, level: str | None = None) -> list[SiteBlock]:
    return [SiteBlock(kind="para", level=None, text=t) for t in texts]


# ── §1.5: similarity is never the verdict — the core design decision ───────


def test_typo_similar_but_semantically_different_is_changed_not_match() -> None:
    """Live (EFFYS FAQ): doc says "8 AM to 5 PM", site says "7 AM to 5
    PM" — `SequenceMatcher.ratio() == 0.99`. Any threshold that forgives
    typography would hide exactly this."""
    doc = [DocBlock(
        kind="para", level=None, text="Open daily from 8 AM to 5 PM.", has_marker=False,
    )]
    site = [SiteBlock(kind="para", level=None, text="Open daily from 7 AM to 5 PM.")]
    rows = _align(doc, site)
    assert len(rows) == 1
    assert rows[0].state == "changed"
    assert rows[0].confidence == "strong"
    assert rows[0].diff_summary == "8 → 7"


def test_pure_typography_difference_is_a_match() -> None:
    """Curly quotes/en-dash/NBSP/casefold differences are NEVER a
    difference (task-92 §2.3) — only semantic content is."""
    doc = [DocBlock(kind="para", level=None, text="Effy’s café—Columbus", has_marker=False)]
    site = [SiteBlock(kind="para", level=None, text="Effy's Café-Columbus")]
    rows = _align(doc, site)
    assert rows[0].state == "match"


def test_case_only_difference_is_a_match_with_a_note() -> None:
    doc = [DocBlock(kind="para", level=None, text="Choose your table", has_marker=False)]
    site = [SiteBlock(kind="para", level=None, text="choose your table")]
    rows = _align(doc, site)
    assert rows[0].state == "match"
    assert rows[0].note == "case-only"


def test_spelling_change_is_flagged() -> None:
    doc = [DocBlock(kind="para", level=None, text="Suitable for coeliac guests.", has_marker=False)]
    site = [SiteBlock(kind="para", level=None, text="Suitable for celiac guests.")]
    rows = _align(doc, site)
    assert rows[0].state == "changed"


def _align(doc_blocks, site_blocks, *, cross_language: bool = False):
    from app.contentqa.copydiff import align_segments

    return align_segments(
        segment_doc_blocks(doc_blocks), segment_site_blocks(site_blocks),
        cross_language=cross_language,
    )


# ── missing / extra / excluded ──────────────────────────────────────────────


def test_sentence_in_doc_but_not_on_site_is_missing() -> None:
    doc = _doc(("This sentence was cut from the live page.", None, False))
    rows = _align(doc, [])
    assert rows[0].state == "missing"
    assert rows[0].site_text is None


def test_sentence_on_site_but_not_in_doc_is_extra() -> None:
    site = _site("This sentence only exists on the live page.")
    rows = _align([], site)
    assert rows[0].state == "extra"
    assert rows[0].doc_text is None


def test_directive_is_excluded_not_compared() -> None:
    doc = [
        DocBlock(kind="directive", level=None, text="[IMAGE SECTION: hero]", has_marker=False),
        DocBlock(kind="para", level=None, text="Real prose sentence.", has_marker=False),
    ]
    site = _site("Real prose sentence.")
    rows = _align(doc, site)
    states = {r.state for r in rows}
    assert "excluded" in states
    excluded = next(r for r in rows if r.state == "excluded")
    assert excluded.doc_text == "[IMAGE SECTION: hero]"
    # Excluded rows never count toward missing/coverage.
    stats = stats_of(rows)
    assert stats.excluded == 1
    assert stats.missing == 0


# ── список пунктів не ділиться на речення ───────────────────────────────────


def test_bullet_item_is_compared_whole_not_split() -> None:
    doc = [DocBlock(kind="item", level=None, text="Real-time EVP scanning", has_marker=False)]
    site = [SiteBlock(kind="item", level=None, text="Real-time EVP scanning")]
    rows = _align(doc, site)
    assert len(rows) == 1
    assert rows[0].state == "match"
    assert rows[0].kind == "item"


# ── сегментація речень ───────────────────────────────────────────────────────


def test_split_sentences_basic() -> None:
    text = "First sentence. Second sentence! Third one?"
    assert split_sentences(text) == ["First sentence.", "Second sentence!", "Third one?"]


def test_split_sentences_does_not_break_on_common_abbreviations() -> None:
    text = "Ask Dr. Smith about it."
    assert split_sentences(text) == ["Ask Dr. Smith about it."]


def test_split_sentences_empty_text() -> None:
    assert split_sentences("") == []
    assert split_sentences("   ") == []


# ── нормалізація ─────────────────────────────────────────────────────────────


def test_normalize_folds_curly_quotes_and_dashes() -> None:
    assert normalize("Effy’s – café") == normalize("Effy's - cafe".replace("cafe", "café"))


def test_normalize_collapses_whitespace_and_case() -> None:
    assert normalize("  Hello   WORLD  ") == "hello world"


# ── послівний diff ───────────────────────────────────────────────────────────


def test_diff_words_isolates_only_the_changed_span() -> None:
    parts = diff_words("Open daily from 8 AM to 5 PM.", "Open daily from 7 AM to 5 PM.")
    changed = [p for p in parts if p.tag == "changed"]
    assert len(changed) == 1
    assert changed[0].doc_text == "8"
    assert changed[0].site_text == "7"
    assert diff_summary(parts) == "8 → 7"


# ── жанр: copy vs edit_brief (task-92 §1.6) ─────────────────────────────────


def test_genre_is_copy_when_no_markers() -> None:
    blocks = _doc(("Finished prose, no markers.", None, False))
    assert genre_of(blocks) == "copy"


def test_genre_stays_copy_with_a_single_stray_marker() -> None:
    """Live correction (found via UI verification, not the original
    portfolio scan): LAXTON_PAGE_FRAMEWORK_UTRUSTNING's Content tabs carry
    exactly ONE inline editorial note
    (`[Keep existing OBS-rad + "Se Köpvillkor" link below the FAQ]`)
    inside 23 otherwise-finished blocks — a single marker must not flip a
    finished tab into "brief"."""
    blocks = _doc(
        ("Some finished prose.", None, False),
        ("More finished prose.", None, False),
        ("[Keep existing note below the FAQ]", None, True),
    )
    assert genre_of(blocks) == "copy"


def test_genre_is_edit_brief_with_two_or_more_markers() -> None:
    """Live: every measured 3XE brief tab carries >=2 markers."""
    blocks = _doc(
        ("Some finished prose.", None, False),
        ("Position and content unchanged:", None, True),
        ("Replace with the venue name.", None, True),
    )
    assert genre_of(blocks) == "edit_brief"


# ── build_copy_tab_result: cross-language suppresses weak matches ──────────


def test_cross_language_suppresses_weak_token_overlap_matches() -> None:
    """Live (LAXTON): English "Searches for EVP in real time" vs Swedish
    "Letar EVP i realtid" scores 0.67 on shared token "EVP" alone — a
    false pair that must NOT surface when the tab's language does not
    match the site's."""
    doc_blocks = [DocBlock(
        kind="para", level=None, text="Searches for EVP in real time", has_marker=False,
    )]
    site_blocks = [SiteBlock(kind="para", level=None, text="Letar EVP i realtid")]

    same_lang = build_copy_tab_result(
        tab_title="t", doc_blocks=doc_blocks, site_blocks=site_blocks, cross_language=False,
    )
    cross_lang = build_copy_tab_result(
        tab_title="t", doc_blocks=doc_blocks, site_blocks=site_blocks, cross_language=True,
    )
    # Same-language: weak floor (0.55) lets the 0.67 pair through as "changed".
    assert same_lang.stats.changed == 1
    # Cross-language: only the strong floor (0.75) applies -> no pair, "missing".
    assert cross_lang.stats.missing == 1
    assert cross_lang.stats.changed == 0


def test_build_copy_tab_result_coverage_and_stats() -> None:
    doc_blocks = _doc(
        ("Matched sentence one.", None, False),
        ("Matched sentence two.", None, False),
        ("Never published sentence.", None, False),
    )
    site_blocks = _site("Matched sentence one.", "Matched sentence two.", "Extra site sentence.")
    result = build_copy_tab_result(
        tab_title="Content", doc_blocks=doc_blocks, site_blocks=site_blocks,
    )
    assert result.stats.match == 2
    assert result.stats.missing == 1
    assert result.stats.extra == 1
    assert result.stats.coverage == 2 / 3
    assert result.genre == "copy"
    assert result.role == "primary"  # default before package.py's role selection
