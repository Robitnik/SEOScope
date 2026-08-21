"""Правила, що спираються на `seo_audit.policy` через policy_bridge.py:
meta-довжини, AI_SLOP_PHRASES/BANNED_OPENERS/FILLER_TERMS, em-dash,
process-leak, tool-failure narrative, keyword-in-title, title/desc-дублі.

Кожна функція тут може кинути PolicyUnavailable — виклик обгортається один
раз в engine.py (`policy_bridge.probe()`), не на кожному ContentItem.
"""

from __future__ import annotations

from app.contentqa import policy_bridge as pb
from app.contentqa.findings import Finding, excerpt
from app.contentqa.model import ContentDoc, ContentItem
from app.contentqa.rules import DESC_MIN, TITLE_MIN, Profile, _fields


def check_meta(item: ContentItem, profile: Profile) -> list[Finding]:
    f: list[Finding] = []

    if item.title == "":
        f.append(Finding(
            "meta-title-missing", "error", item.ref,
            "Title field is present and empty.",
        ))
    elif item.title:
        tl = len(item.title)
        title_max = pb.title_max()
        title_value = excerpt(item.title)
        if tl > title_max:
            f.append(Finding(
                "meta-title-length", "error", item.ref,
                f"Title is {tl} chars; ceiling is {title_max}.",
                value=title_value,
            ))
        elif tl < TITLE_MIN:
            f.append(Finding(
                "meta-title-length", "warn", item.ref,
                f"Title is {tl} chars; floor is {TITLE_MIN}.",
                value=title_value,
            ))

        for brand in profile.brand_names:
            suffix = f"| {brand}"
            if item.title.strip().endswith(suffix) and tl > title_max:
                f.append(Finding(
                    "meta-title-brand-padding", "error", item.ref,
                    f"Title exceeds {title_max} chars and ends with {suffix!r} "
                    f"— brand padding pushed it over.",
                    value=title_value,
                ))

        if item.keywords and not pb.keyword_in_title(item.title, item.keywords[0]):
            f.append(Finding(
                "meta-title-keyword-missing", "warn", item.ref,
                f"Primary keyword {item.keywords[0]!r} not found in title.",
                value=title_value,
            ))

    if item.description == "":
        f.append(Finding(
            "meta-desc-missing", "error", item.ref,
            "Description field is present and empty.",
        ))
    elif item.description:
        dl = len(item.description)
        desc_max = pb.desc_max()
        desc_value = excerpt(item.description)
        if dl > desc_max:
            f.append(Finding(
                "meta-desc-length", "error", item.ref,
                f"Description is {dl} chars; ceiling is {desc_max}.",
                value=desc_value,
            ))
        elif dl < DESC_MIN:
            f.append(Finding(
                "meta-desc-length", "warn", item.ref,
                f"Description is {dl} chars; floor is {DESC_MIN}.",
                value=desc_value,
            ))
        elif dl > pb.desc_authored_max():
            f.append(Finding(
                "meta-desc-authored-long", "info", item.ref,
                f"Description is {dl} chars; Google renders ~{pb.desc_authored_max()}, "
                f"tighten if freshly written.",
                value=desc_value,
            ))

        opener = pb.banned_opener(item.description)
        if opener:
            f.append(Finding(
                "meta-desc-banned-opener", "error", item.ref,
                f"Description opens with banned opener {opener!r}.",
                value=desc_value,
            ))

    return f


def check_slop_and_voice_policy(item: ContentItem, profile: Profile) -> list[Finding]:
    f: list[Finding] = []
    for field_name, text in _fields(item):
        value = excerpt(text)
        for phrase in pb.slop_phrases(text):
            f.append(Finding(
                "slop-phrase", "warn", item.ref,
                f"{field_name}: {phrase!r} is interchangeable AI-marketing copy.",
                value=value,
            ))
        for term in pb.filler_terms(text):
            f.append(Finding(
                "slop-filler-term", "warn", item.ref,
                f"{field_name}: {term!r} asserts quality without proving it.",
                value=value,
            ))
        for dash in pb.em_dash_hits(text):
            f.append(Finding(
                "voice-em-dash", "error", item.ref,
                f"{field_name}: {dash} present — house rule bans it.",
                value=value,
            ))
        for hit in pb.process_leak_hits(text):
            f.append(Finding(
                "process-leak", "error", item.ref,
                f"{field_name}: {hit!r} names our tooling/process, not the page.",
                value=value,
            ))
        for hit in pb.tool_failure_phrases(text):
            f.append(Finding(
                "tool-failure-narrative", "error", item.ref,
                f"{field_name}: {hit!r} leaks a research-session failure into copy.",
                value=value,
            ))
    return f


def check_meta_duplicates(doc: ContentDoc, profile: Profile) -> list[Finding]:
    f: list[Finding] = []
    items = [i for i in doc.items if i.title or i.description]
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            a, b = items[i], items[j]
            if a.title and b.title:
                sim = pb.text_similarity(a.title, b.title)
                if sim >= pb.title_similarity_max():
                    f.append(Finding(
                        "meta-title-duplicate", "error", f"{a.ref}, {b.ref}",
                        f"Titles {sim:.2f} similar: {a.title!r} / {b.title!r}",
                        value=excerpt(a.title),
                    ))
            if a.description and b.description:
                sim = pb.text_similarity(a.description, b.description)
                if sim >= pb.description_similarity_max():
                    f.append(Finding(
                        "meta-desc-duplicate", "error", f"{a.ref}, {b.ref}",
                        f"Descriptions {sim:.2f} similar.",
                        value=excerpt(a.description),
                    ))
    return f
