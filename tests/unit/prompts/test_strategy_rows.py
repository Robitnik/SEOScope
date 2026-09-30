"""Юніти розбору аркушів стратегії (task-102 §3.1–3.2)."""

from __future__ import annotations

from app.prompts.strategy_rows import parse_sheet

from .strategy_fixtures import (
    blog_sheet_goreport,
    blog_sheet_grp,
    cells,
    page_sheet,
    sheet,
)


def test_page_rows_keep_live_url_and_new_url_apart() -> None:
    parsed = parse_sheet(page_sheet())

    assert parsed.kind == "page" and parsed.role == "page"
    by_name = {p.page_name: p for p in parsed.pages}
    # Легенда кольорів і порожній рядок не стають сторінками.
    assert list(by_name) == ["Queens Menu", "Home", "Menu", "Locations"]

    new_page = by_name["Menu"]
    assert new_page.mode == "new"
    assert new_page.url is None  # "doesn't exist" — не URL
    assert new_page.new_url == "https://19cleveland.com/menu/"

    renamed = by_name["Locations"]
    assert renamed.mode == "existing"
    assert renamed.url == "https://x.com/contact/"  # жива сторінка — старий URL
    assert renamed.new_url == "https://x.com/locations/"


def test_page_framework_marker_and_dash_placeholder() -> None:
    by_name = {p.page_name: p for p in parse_sheet(page_sheet()).pages}

    assert by_name["Queens Menu"].framework_link == "https://docs.google.com/document/d/abc"
    assert by_name["Queens Menu"].framework_text == "GRP_PAGE_FRAMEWORK_QUEENS_MENU_08272026"
    # `—` у колонці Framework — не значення.
    assert by_name["Home"].framework_text is None
    assert by_name["Home"].secondary is None


def test_empty_primary_does_not_shift_secondary() -> None:
    header = ["Page Name", "URL", "Primary Key", "Secondary Key", "Title"]
    parsed = parse_sheet(
        sheet("Strategy", cells(*header), cells("Home", "https://a.com/", None, "second", "T"))
    )

    row = parsed.pages[0]
    assert row.primary is None
    assert row.secondary == "second"


def test_status_wins_over_url_fact_and_falls_back_to_url() -> None:
    header = ["Page Status", "Page Name", "URL", "Title"]
    parsed = parse_sheet(
        sheet(
            "Strategy",
            cells(*header),
            cells("NEW PAGE", "A", "https://a.com/a/", "T"),
            cells(None, "B", "https://a.com/b/", "T"),
            cells(None, "C", "n/a", "T"),
        )
    )

    assert [p.mode for p in parsed.pages] == ["new", "existing", "new"]


def test_multiple_header_blocks_on_one_sheet() -> None:
    header = ["Page Name", "URL", "Title"]
    parsed = parse_sheet(
        sheet(
            "Q3 Strategy",
            cells(*header),
            cells("A", "https://a.com/a/", "T1"),
            cells(None, None, None),
            cells(*header),
            cells("B", "https://a.com/b/", "T2"),
        )
    )

    assert [p.page_name for p in parsed.pages] == ["A", "B"]


def test_blog_grp_layout_groups_rows_into_articles() -> None:
    parsed = parse_sheet(blog_sheet_grp())

    assert parsed.kind == "blog" and parsed.role == "blog"
    first, second = parsed.articles
    assert first.topic == "Chicken Schnitzel: Breaded vs Grilled"
    assert first.keywords == (
        "chicken schnitzel",
        "what is chicken schnitzel",
        "chicken schnitzel pita",
    )
    assert first.anchors == ("full Grill Point menu", "Queens breaded options")
    assert first.internal_links == (
        "https://grillpointnyc.com/menu/",
        "https://grillpointnyc.com/menu/queens/",
    )
    assert first.comments == "WHY: Ranking pages teach.\nPublish by: Dec 31"
    assert first.framework_link == "https://docs.google.com/document/d/blog1"
    assert first.commercial is None  # колонки немає — не вгадуємо

    # Анкор без URL не йде в пари, але не губиться мовчки.
    assert second.anchors == ("breakfast menu",)
    assert second.dropped_link_pairs == 1
    assert second.framework_text is None


def test_blog_goreport_layout_splits_newline_joined_cells() -> None:
    (article,) = parse_sheet(blog_sheet_goreport()).articles

    assert article.anchors == ("dilapidations survey reporting", "photographic schedule")
    assert article.internal_links == ("https://goreport.com/a/", "https://goreport.com/b/")
    assert article.commercial == "https://goreport.com/commercial-survey/"
    assert article.keywords == ("dilapidations meaning", "meaning of dilapidations")
    assert article.framework_text is None  # `—`


def test_blog_rows_before_first_article_are_counted() -> None:
    header = ["Article Topic", "Keyword", "Title"]
    parsed = parse_sheet(
        sheet(
            "Blogs", cells(*header), cells(None, "stray keyword", None), cells("Topic", "kw", "T")
        )
    )

    assert parsed.orphan_rows == 1
    assert len(parsed.articles) == 1


def test_sheet_without_header_has_no_kind() -> None:
    parsed = parse_sheet(sheet("Notes", cells("just", "text"), cells("more", "text")))

    assert parsed.kind is None and parsed.row_count == 0


def test_structure_sheet_is_not_a_page_source() -> None:
    header = [
        "Page Status",
        "Intent",
        "Page Name",
        "URL",
        "Recommended URL",
        "Breadcrumbs",
        "Comments",
    ]
    parsed = parse_sheet(
        sheet(
            "Structure",
            cells(*header),
            cells("OPTIMIZE PAGE", "Commercial", "Home", "https://a.com/"),
        )
    )

    assert parsed.role == "structure"
