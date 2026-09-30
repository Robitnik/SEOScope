"""Синтетичні мінімальні аркуші стратегій (task-102 §8) — форми взято з живих
таблиць, що ламали б наївний розбір, але жодного тесту не прив'язано до живих файлів."""

from __future__ import annotations

from app.prompts.strategy_source import Cell, RawSheet

PAGE_HEADER = [
    "Page Status",
    "Intent",
    "Page Name",
    "URL",
    "New URL",
    "Primary Key",
    "Secondary Key",
    "Longtail Key",
    "H1",
    "Title",
    "Description",
    "Breadcrumbs",
    "comment",
    "Framework / Copy Link",
]


def cells(*values: str | tuple[str, str] | None) -> tuple[Cell, ...]:
    """`"text"`, `None` (порожня) або `("text", "https://link")` (клітинка з посиланням/чіпом)."""
    out: list[Cell] = []
    for v in values:
        if v is None:
            out.append(Cell(None))
        elif isinstance(v, tuple):
            out.append(Cell(v[0], (v[1],)))
        else:
            out.append(Cell(v))
    return tuple(out)


def sheet(name: str, *rows: tuple[Cell, ...]) -> RawSheet:
    return (name, list(rows))


def page_sheet() -> RawSheet:
    return sheet(
        "Q42026",
        cells(*PAGE_HEADER),
        cells(
            "OPTIMIZE PAGE",
            "Commercial",
            "Queens Menu",
            "https://grillpointnyc.com/menu/queens/",
            "https://grillpointnyc.com/menu/queens/",
            "grill point queens menu",
            "kosher breakfast nyc",
            "grill point queens menu prices",
            "Grill Point Queens Menu",
            "Grill Point Queens Menu & Prices",
            "Full Grill Point Queens menu.",
            "Home > Menu > Queens",
            "WHY: Clicks fell 26%.\nGOAL: Win the menu queries back.",
            ("GRP_PAGE_FRAMEWORK_QUEENS_MENU_08272026", "https://docs.google.com/document/d/abc"),
        ),
        cells(
            "OPTIMIZE PAGE",
            "Commercial",
            "Home",
            "https://grillpointnyc.com/",
            "https://grillpointnyc.com/",
            "grill point nyc",
            None,
            None,
            None,
            None,
            None,
            "Home",
            None,
            "—",
        ),
        cells(
            "NEW PAGE",
            "Commercial",
            "Menu",
            "doesn't exist",
            "https://19cleveland.com/menu/",
            "cleveland menu",
            None,
            None,
            None,
            None,
            None,
            None,
            "Build it.",
            None,
        ),
        cells(
            "OPTIMIZE PAGE",
            "Commercial",
            "Locations",
            "https://x.com/contact/",
            "https://x.com/locations/",
            "x locations",
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
        ),
        cells("Red Means Update"),
        cells(None, None, None),
    )


BLOG_HEADER_GRP = [
    "Quarter",
    "Article Topic",
    "Article Short Description",
    "Keyword",
    "Volume",
    "KD",
    "CPC",
    "Title",
    "Description",
    "Anchor Text",
    "Internal Link URL",
    "Comments",
    "Frameworks",
]


def blog_sheet_grp() -> RawSheet:
    """GRP: по одній парі анкор/лінк у кожному рядку, стаття = кілька рядків."""
    return sheet(
        "Blogs",
        cells(*BLOG_HEADER_GRP),
        cells(
            "Q4 2026",
            "Chicken Schnitzel: Breaded vs Grilled",
            "For diners deciding.",
            "chicken schnitzel",
            "20000",
            "7",
            None,
            "Chicken Schnitzel: What to Order",
            "Chicken schnitzel explained.",
            "full Grill Point menu",
            "https://grillpointnyc.com/menu/",
            "WHY: Ranking pages teach.\nPublish by: Dec 31",
            ("GRP_BLOG_FRAMEWORK_CHICKEN_SCHNITZEL", "https://docs.google.com/document/d/blog1"),
        ),
        cells(
            None,
            None,
            None,
            "what is chicken schnitzel",
            "900",
            "7",
            None,
            None,
            None,
            "Queens breaded options",
            "https://grillpointnyc.com/menu/queens/",
        ),
        cells(None, None, None, "chicken schnitzel pita", "40", "0"),
        cells(
            "Q4 2026",
            "Best Breakfast in NYC",
            "For someone deciding.",
            "best breakfast nyc",
            "4000",
            "5",
            None,
            "Best Breakfast in NYC",
            "Where to eat breakfast.",
            "breakfast menu",
            "https://grillpointnyc.com/menu/",
        ),
        cells(
            None,
            None,
            None,
            "best breakfast in nyc",
            None,
            None,
            None,
            None,
            None,
            "an anchor with no url",
            None,
        ),
    )


def blog_sheet_goreport() -> RawSheet:
    """GOREPORT: усі анкори й лінки однією клітинкою через `\\n`, є Commercial destination."""
    header = [
        "Quarter",
        "Article Topic",
        "Keyword",
        "Volume",
        "Commercial destination",
        "Title",
        "Description",
        "Anchors",
        "Internal links",
        "Framework",
    ]
    return sheet(
        "Blog Strategy",
        cells(*header),
        cells(
            "Q3 2026",
            "dilapidations meaning",
            "dilapidations meaning",
            "1000",
            "https://goreport.com/commercial-survey/",
            "Dilapidations: What It Means",
            "What dilapidations means.",
            "dilapidations survey reporting\nphotographic schedule",
            "https://goreport.com/a/\nhttps://goreport.com/b/",
            "—",
        ),
        cells(None, None, "meaning of dilapidations", "50"),
    )
