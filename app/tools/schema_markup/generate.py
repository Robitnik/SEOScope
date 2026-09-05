"""Генератор JSON-LD-сніпетів (task-56 §1, "Правила генерації"): кожне поле
має `source` — звідки взято значення; недоведене йде в `needs_input`, а не
у вигадане значення чи порожній рядок. Заборонені типи (`FORBIDDEN_GENERATE_
TYPES` — `AggregateRating`/`Review`/`FAQPage`/`HowTo`) ніколи не пропонуються,
навіть якщо колись з'явиться Google-профіль із доброю recommended-секцією.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from app.tools.schema_markup.classify import SUGGESTED_TYPES_BY_KIND, PageKind
from app.tools.schema_markup.vocab import FORBIDDEN_GENERATE_TYPES


@dataclass(frozen=True)
class Fact:
    """Одне доведене значення й звідки воно взяте."""

    value: str
    source: str  # "page:title" | "page:og:image" | "url_path" | "client:name" | ...


@dataclass
class PageFacts:
    """Усе, що екстрактор сигналів реально виявив на сторінці (task-56 §1:
    ніколи не вигадуємо — те, чого тут немає, іде в `needs_input`, не в
    підставне значення).
    """

    url: str
    title: Fact | None = None
    description: Fact | None = None
    h1: Fact | None = None
    og_image: Fact | None = None
    published_time: Fact | None = None
    modified_time: Fact | None = None
    author_name: Fact | None = None
    organization_name: Fact | None = None
    organization_url: Fact | None = None
    organization_logo: Fact | None = None


@dataclass
class GeneratedEntity:
    type_name: str
    properties: dict[str, Any] = field(default_factory=dict)
    sources: dict[str, str] = field(default_factory=dict)  # property -> Fact.source
    needs_input: list[str] = field(default_factory=list)

    def to_jsonld(self) -> dict[str, Any]:
        node: dict[str, Any] = {"@context": "https://schema.org", "@type": self.type_name}
        node.update(self.properties)
        return node


@dataclass
class GenerationResult:
    entities: list[GeneratedEntity]
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (type_name, reason)

    def to_jsonld_graph(self) -> dict[str, Any]:
        if len(self.entities) == 1:
            return self.entities[0].to_jsonld()
        return {
            "@context": "https://schema.org",
            "@graph": [
                {k: v for k, v in e.to_jsonld().items() if k != "@context"} for e in self.entities
            ],
        }


def _set(entity: GeneratedEntity, prop: str, fact: Fact | None) -> None:
    if fact is None or not fact.value.strip():
        entity.needs_input.append(prop)
        return
    entity.properties[prop] = fact.value.strip()
    entity.sources[prop] = fact.source


def _breadcrumb_from_url(url: str) -> GeneratedEntity | None:
    parts = urlsplit(url)
    segments = [s for s in parts.path.strip("/").split("/") if s]
    if not segments:
        return None
    items = []
    running = f"{parts.scheme}://{parts.netloc}"
    for index, segment in enumerate(segments, start=1):
        running = f"{running}/{segment}"
        label = segment.replace("-", " ").replace("_", " ").strip().title()
        items.append({"@type": "ListItem", "position": index, "name": label, "item": running})
    entity = GeneratedEntity(type_name="BreadcrumbList")
    entity.properties["itemListElement"] = items
    entity.sources["itemListElement"] = "url_path"
    return entity


def _organization_entity(facts: PageFacts) -> GeneratedEntity:
    entity = GeneratedEntity(type_name="Organization")
    _set(entity, "name", facts.organization_name)
    parts = urlsplit(facts.url)
    site_root = f"{parts.scheme}://{parts.netloc}/" if parts.scheme else None
    url_fact = facts.organization_url or (Fact(site_root, "url_root") if site_root else None)
    _set(entity, "url", url_fact)
    _set(entity, "logo", facts.organization_logo)
    return entity


def _website_entity(facts: PageFacts) -> GeneratedEntity:
    entity = GeneratedEntity(type_name="WebSite")
    _set(entity, "name", facts.organization_name or facts.title)
    parts = urlsplit(facts.url)
    site_root = f"{parts.scheme}://{parts.netloc}/" if parts.scheme else None
    _set(entity, "url", Fact(site_root, "url_root") if site_root else None)
    return entity


def _blog_posting_entity(facts: PageFacts) -> GeneratedEntity:
    entity = GeneratedEntity(type_name="BlogPosting")
    _set(entity, "headline", facts.title or facts.h1)
    _set(entity, "image", facts.og_image)
    _set(entity, "datePublished", facts.published_time)
    _set(entity, "dateModified", facts.modified_time or facts.published_time)
    if facts.author_name is not None and facts.author_name.value.strip():
        author = GeneratedEntity(type_name="Person")
        _set(author, "name", facts.author_name)
        entity.properties["author"] = author.to_jsonld()
        entity.sources["author"] = facts.author_name.source
    else:
        entity.needs_input.append("author")
    return entity


def _service_entity(facts: PageFacts) -> GeneratedEntity:
    entity = GeneratedEntity(type_name="Service")
    _set(entity, "name", facts.title or facts.h1)
    if facts.organization_name is not None:
        provider = GeneratedEntity(type_name="Organization")
        _set(provider, "name", facts.organization_name)
        entity.properties["provider"] = provider.to_jsonld()
        entity.sources["provider"] = facts.organization_name.source
    else:
        entity.needs_input.append("provider")
    return entity


def _local_business_entity(facts: PageFacts) -> GeneratedEntity:
    entity = GeneratedEntity(type_name="LocalBusiness")
    _set(entity, "name", facts.organization_name or facts.title)
    # Адреса/телефон/години — жоден HTML-сигнал зі стандартного набору
    # (title/description/og:*) їх надійно не дає (адреса в тексті footer'а —
    # не структурований факт), тож завжди needs_input, ніколи не вгадуємо.
    entity.needs_input.extend(["address", "telephone", "openingHoursSpecification"])
    return entity


_BUILDERS = {
    "Organization": _organization_entity,
    "WebSite": _website_entity,
    "BlogPosting": _blog_posting_entity,
    "Service": _service_entity,
    "LocalBusiness": _local_business_entity,
}


def generate_for_kind(
    kind: PageKind, facts: PageFacts, *, existing_types: frozenset[str] = frozenset()
) -> GenerationResult:
    """`existing_types` — типи, вже знайдені на сторінці валідатором: якщо
    вона там вже є, генератор її пропускає (merge, не replace — task-56 §1
    п.4), а не пропонує дублюючий блок.
    """
    wanted = SUGGESTED_TYPES_BY_KIND.get(kind, ())
    entities: list[GeneratedEntity] = []
    skipped: list[tuple[str, str]] = []
    for type_name in wanted:
        if type_name in FORBIDDEN_GENERATE_TYPES:
            skipped.append((type_name, "never auto-generated (policy)"))
            continue
        if type_name in existing_types:
            skipped.append((type_name, "already present on the page"))
            continue
        if type_name == "BreadcrumbList":
            breadcrumb = _breadcrumb_from_url(facts.url)
            if breadcrumb is not None:
                entities.append(breadcrumb)
            else:
                skipped.append((type_name, "URL has no path segments to build a trail from"))
            continue
        builder = _BUILDERS.get(type_name)
        if builder is None:
            skipped.append((type_name, "no generator implemented yet"))
            continue
        entities.append(builder(facts))
    return GenerationResult(entities=entities, skipped=skipped)
