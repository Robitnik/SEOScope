"""Генератор (task-56 §1, "Правила генерації"): кожне поле має `source`,
недоведене йде в `needs_input`, заборонені типи не пропонуються ніколи.
"""

from __future__ import annotations

from app.tools.schema_markup.generate import Fact, PageFacts, generate_for_kind
from app.tools.schema_markup.vocab import FORBIDDEN_GENERATE_TYPES


def test_home_generates_organization_and_website_with_sources():
    facts = PageFacts(
        url="https://example.com/",
        title=Fact("Acme Co — Home", "page:title"),
        organization_name=Fact("Acme Co", "page:og:site_name"),
        organization_logo=Fact("https://example.com/logo.png", "page:og:image"),
    )
    result = generate_for_kind("home", facts)
    types = {e.type_name for e in result.entities}
    assert types == {"Organization", "WebSite"}

    org = next(e for e in result.entities if e.type_name == "Organization")
    assert org.properties["name"] == "Acme Co"
    assert org.sources["name"] == "page:og:site_name"
    assert org.properties["url"] == "https://example.com/"
    assert org.sources["url"] == "url_root"
    assert org.properties["logo"] == "https://example.com/logo.png"
    assert org.needs_input == []


def test_missing_facts_go_to_needs_input_never_fabricated():
    facts = PageFacts(url="https://example.com/")
    result = generate_for_kind("home", facts)
    org = next(e for e in result.entities if e.type_name == "Organization")
    assert "name" not in org.properties
    assert "logo" not in org.properties
    assert "name" in org.needs_input
    assert "logo" in org.needs_input


def test_blog_post_generates_breadcrumb_from_url_path():
    facts = PageFacts(
        url="https://example.com/blog/how-we-shipped-it",
        title=Fact("How we shipped it", "page:title"),
        published_time=Fact("2026-08-01T10:00:00+00:00", "page:article:published_time"),
    )
    result = generate_for_kind("blog_post", facts)
    types = {e.type_name for e in result.entities}
    assert types == {"BlogPosting", "BreadcrumbList"}

    breadcrumb = next(e for e in result.entities if e.type_name == "BreadcrumbList")
    items = breadcrumb.properties["itemListElement"]
    assert len(items) == 2
    assert items[0]["name"] == "Blog"
    assert items[1]["name"] == "How We Shipped It"
    assert breadcrumb.sources["itemListElement"] == "url_path"

    posting = next(e for e in result.entities if e.type_name == "BlogPosting")
    assert posting.properties["headline"] == "How we shipped it"
    assert posting.properties["datePublished"] == "2026-08-01T10:00:00+00:00"
    assert "author" in posting.needs_input  # ніхто не вигаданий


def test_blog_post_author_present_becomes_nested_person():
    facts = PageFacts(
        url="https://example.com/blog/post",
        title=Fact("Post", "page:title"),
        author_name=Fact("Jane Doe", "page:byline"),
    )
    result = generate_for_kind("blog_post", facts)
    posting = next(e for e in result.entities if e.type_name == "BlogPosting")
    assert posting.properties["author"]["@type"] == "Person"
    assert posting.properties["author"]["name"] == "Jane Doe"
    assert "author" not in posting.needs_input


def test_existing_type_is_skipped_not_duplicated():
    facts = PageFacts(url="https://example.com/")
    result = generate_for_kind("home", facts, existing_types=frozenset({"Organization"}))
    types = {e.type_name for e in result.entities}
    assert "Organization" not in types
    assert "WebSite" in types
    assert ("Organization", "already present on the page") in result.skipped


def test_forbidden_types_never_generated():
    # Ані kind, ані facts не мають підказувати генератору FAQPage/HowTo/
    # Review/AggregateRating — перевіряємо, що жоден suggested-набір їх не
    # містить взагалі (структурний гарант, не залежний від facts).
    from app.tools.schema_markup.classify import SUGGESTED_TYPES_BY_KIND

    for kind, types in SUGGESTED_TYPES_BY_KIND.items():
        assert not (set(types) & FORBIDDEN_GENERATE_TYPES), kind
