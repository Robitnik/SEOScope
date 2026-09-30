"""Юніти рендера лаунчерів зі стратегії (task-102 §3.3)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date

from app.prompts.registry import get_template
from app.prompts.strategy_render import CLIENT_UNRESOLVED, RenderedRow, build_rows, slugify
from app.prompts.strategy_rows import parse_sheet
from app.prompts.strategy_source import RawSheet

from .strategy_fixtures import blog_sheet_goreport, blog_sheet_grp, page_sheet

DOC = "/home/elon/dev/seo/AISeoTools/docs/PageFramework.md"
BLOG_DOC = "/home/elon/dev/seo/AISeoTools/docs/BlogFramework.md"
TODAY = date(2026, 9, 30)


def _pages(label: str | None = "GRP") -> dict[str, RenderedRow]:
    template = get_template("page_framework")
    assert template is not None
    rows = build_rows(
        template, parse_sheet(page_sheet()), client_label=label, doc_host_path=DOC, today=TODAY
    )
    return {r.title: r for r in rows}


def _articles(
    sheet_fn: Callable[[], RawSheet] = blog_sheet_grp, label: str | None = "GRP"
) -> dict[str, RenderedRow]:
    template = get_template("blog_framework")
    assert template is not None
    rows = build_rows(
        template, parse_sheet(sheet_fn()), client_label=label, doc_host_path=BLOG_DOC, today=TODAY
    )
    return {r.title: r for r in rows}


def test_existing_page_launcher_is_the_documents_own_header_filled_in() -> None:
    row = _pages()["Queens Menu"]

    assert row.blocked_reason is None
    assert row.rendered == (
        f"Read {DOC} and run it end to end.\n"
        "\n"
        "MODE:    existing\n"
        "INPUT:   https://grillpointnyc.com/menu/queens/\n"
        "CLIENT:  GRP\n"
        "OUT_DIR: out/grp_menu-queens_2026-09-30_framework/\n"
        "Data:\n"
        "Primary Key: grill point queens menu\n"
        "\n"
        "Secondary Key: kosher breakfast nyc\n"
        "\n"
        "Longtail Key: grill point queens menu prices\n"
        "\n"
        "H1: Grill Point Queens Menu\n"
        "\n"
        "Title: Grill Point Queens Menu & Prices\n"
        "\n"
        "Description: Full Grill Point Queens menu.\n"
        "\n"
        "Breadcrumbs: Home > Menu > Queens\n"
        "\n"
        "Comments:\n"
        "  WHY: Clicks fell 26%.\n"
        "  GOAL: Win the menu queries back.\n"
    )


def test_empty_fields_stay_empty_labels_not_invented_values() -> None:
    row = _pages()["Home"]
    rendered = row.rendered
    assert rendered is not None

    assert "OUT_DIR: out/grp_home_2026-09-30_framework/" in rendered
    assert "Secondary Key:\n" in rendered
    assert "Title:\n" in rendered
    assert row.prefilled == ["Primary Key", "Breadcrumbs"]


def test_new_page_uses_name_as_input_and_appends_target_url() -> None:
    row = _pages()["Menu"]
    rendered = row.rendered
    assert rendered is not None

    assert row.mode == "new"
    assert "MODE:    new\nINPUT:   Menu\n" in rendered
    assert "Comments:\n  Build it.\n  Target URL: https://19cleveland.com/menu/\n" in rendered


def test_renamed_page_keeps_live_url_and_passes_the_target() -> None:
    rendered = _pages()["Locations"].rendered

    assert "INPUT:   https://x.com/contact/\n" in rendered
    assert rendered.endswith("Comments: Target URL: https://x.com/locations/\n")


def test_unresolved_client_blocks_every_row() -> None:
    for row in _pages(label=None).values():
        assert row.rendered is None
        assert row.blocked_reason == CLIENT_UNRESOLVED


def test_existing_page_without_live_url_is_blocked() -> None:
    template = get_template("page_framework")
    assert template is not None
    from .strategy_fixtures import cells, sheet

    parsed = parse_sheet(
        sheet(
            "Strategy",
            cells("Page Status", "Page Name", "URL", "Title"),
            cells("OPTIMIZE PAGE", "Ghost", "-", "T"),
        )
    )
    (row,) = build_rows(template, parsed, client_label="GRP", doc_host_path=DOC, today=TODAY)

    assert row.rendered is None
    assert row.blocked_reason == "existing page has no live URL"


def test_blog_launcher_lists_keywords_primary_first_and_pairs_in_order() -> None:
    row = _articles()["Chicken Schnitzel: Breaded vs Grilled"]

    assert row.rendered == (
        f"Read {BLOG_DOC} and run it end to end.\n"
        "\n"
        "MODE:    blog\n"
        "INPUT:   Chicken Schnitzel: Breaded vs Grilled\n"
        "CLIENT:  GRP\n"
        "OUT_DIR: out/grp_chicken-schnitzel-breaded-vs-grilled_2026-09-30_blog/\n"
        "Data:\n"
        "Article Topic: Chicken Schnitzel: Breaded vs Grilled\n"
        "\n"
        "Article short description: For diners deciding.\n"
        "\n"
        "Keywords: chicken schnitzel, what is chicken schnitzel, chicken schnitzel pita\n"
        "\n"
        "Commercial destination:\n"
        "\n"
        "Title: Chicken Schnitzel: What to Order\n"
        "\n"
        "Description: Chicken schnitzel explained.\n"
        "\n"
        "Anchors:\n"
        "  full Grill Point menu\n"
        "  Queens breaded options\n"
        "\n"
        "Internal links:\n"
        "  https://grillpointnyc.com/menu/\n"
        "  https://grillpointnyc.com/menu/queens/\n"
        "\n"
        "DO NOT CHANGE:\n"
        "\n"
        "Comments:\n"
        "  WHY: Ranking pages teach.\n"
        "  Publish by: Dec 31\n"
    )
    assert row.has_framework is True


def test_blog_skipped_link_pair_becomes_a_visible_warning() -> None:
    row = _articles()["Best Breakfast in NYC"]

    assert row.warnings == ["1 incomplete link pair skipped"]
    assert "Anchors: breakfast menu\n" in row.rendered


def test_blog_commercial_destination_only_from_the_column() -> None:
    row = _articles(blog_sheet_goreport, label="GOREPORT")["dilapidations meaning"]

    assert "Commercial destination: https://goreport.com/commercial-survey/\n" in row.rendered
    assert "OUT_DIR: out/goreport_dilapidations-meaning_2026-09-30_blog/" in row.rendered


def test_slugify_folds_to_ascii_and_caps_length() -> None:
    assert slugify("Що таке Café? 1") == "cafe-1"
    assert len(slugify("a" * 200)) == 60
    assert slugify("---") == ""
