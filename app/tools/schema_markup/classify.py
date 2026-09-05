"""Класифікатор «типу сторінки» (task-56 §5): визначає, які Google-фічі
взагалі релевантні URL, до фетчу генератора. Евристика на URL-шляху й
базових HTML-сигналах — без жодної мережі чи ML, той самий рівень
складності, що severity-класифікація в `app/monitor/severity.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

PageKind = str  # "home" | "blog_post" | "product" | "service" | "contact" | "other"

_BLOG_SEGMENTS = ("blog", "news", "articles", "post", "posts")
_PRODUCT_SEGMENTS = ("product", "products", "shop", "store")
_SERVICE_SEGMENTS = ("service", "services")
_CONTACT_SEGMENTS = ("contact", "contact-us")


@dataclass(frozen=True)
class ClassifyResult:
    kind: PageKind
    confidence: str  # "high" | "medium" | "low"
    reason: str


def classify_page(url: str, *, existing_types: tuple[str, ...] = ()) -> ClassifyResult:
    """`existing_types` — типи, які вже знайдені на сторінці (JSON-LD/
    Microdata): якщо там уже є `Product`/`BlogPosting`, це сильніший сигнал
    за URL-шлях, бо власник сторінки сам собі не бреше про тип контенту.
    """
    for strong_type, kind in (
        ("Product", "product"),
        ("BlogPosting", "blog_post"),
        ("Article", "blog_post"),
        ("LocalBusiness", "contact"),
    ):
        if strong_type in existing_types:
            return ClassifyResult(
                kind=kind, confidence="high", reason=f"existing @type={strong_type}"
            )

    parts = urlsplit(url)
    path = parts.path.strip("/").lower()
    segments = [s for s in path.split("/") if s]

    if not segments:
        return ClassifyResult(kind="home", confidence="high", reason="empty path")

    for seg in segments:
        reason = f"url segment '{seg}'"
        if seg in _CONTACT_SEGMENTS:
            return ClassifyResult(kind="contact", confidence="medium", reason=reason)
        if seg in _PRODUCT_SEGMENTS:
            return ClassifyResult(kind="product", confidence="low", reason=reason)
        if seg in _SERVICE_SEGMENTS:
            return ClassifyResult(kind="service", confidence="low", reason=reason)
        if seg in _BLOG_SEGMENTS:
            return ClassifyResult(kind="blog_post", confidence="medium", reason=reason)

    return ClassifyResult(kind="other", confidence="low", reason="no matching signal")


# Які типи генератор пропонує для кожного kind (task-56 §1, "Правила
# генерації"). `home` завжди отримує Organization+WebSite незалежно від
# знайденого kind, бо вони описують сайт у цілому, не сторінку.
SUGGESTED_TYPES_BY_KIND: dict[PageKind, tuple[str, ...]] = {
    "home": ("Organization", "WebSite"),
    "blog_post": ("BlogPosting", "BreadcrumbList"),
    "product": ("Product", "BreadcrumbList"),
    "service": ("Service", "BreadcrumbList"),
    "contact": ("LocalBusiness", "BreadcrumbList"),
    "other": ("BreadcrumbList",),
}
