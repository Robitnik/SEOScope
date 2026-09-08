"""Оркестратор: ContentDoc + Profile -> list[Finding]. `seo_audit.policy`
перевіряється рівно один раз (probe) — якщо він недоступний, увесь прогін не
падає: власні лейни (structure/links/brand-voice-own/data-integrity/
readability/wikipedia-slop) усе одно виконуються, а policy-залежні лейни
(meta, AI_SLOP_PHRASES, em-dash, process-leak) дають один `policy-unavailable`
finding замість мовчазного «чисто» (PLAN §12: не перевірили ≠ порушень немає).
"""

from __future__ import annotations

from app.contentqa import policy_bridge as pb
from app.contentqa import policy_rules, rules
from app.contentqa.findings import Finding
from app.contentqa.model import ContentDoc
from app.contentqa.rules import Profile


def run_checks(doc: ContentDoc, profile: Profile | None = None) -> list[Finding]:
    profile = profile or Profile()
    findings: list[Finding] = []

    if profile.language != "en":
        lexicon = profile.lexicon
        if lexicon is not None and lexicon.disabled_lanes:
            adjustments = "; ".join(lexicon.disabled_lanes)
            findings.append(Finding(
                "locale-rules-adjusted", "info", doc.source,
                f"{lexicon.name} locale: some English-only lanes are disabled or "
                f"replaced for this run — {adjustments}.",
            ))
        elif lexicon is None:
            findings.append(Finding(
                "locale-rules-adjusted", "info", doc.source,
                f"No word lists for locale {profile.locale!r} yet — meta-length and "
                f"structure checks still ran, but slop/filler/opener/voice lanes did not "
                f"(app/contentqa/lexicon).",
            ))

    for sheet in doc.skipped_sheets:
        findings.append(Finding(
            "sheet-skipped", "info", f"{doc.source}!{sheet}",
            f"No recognized title/description columns in sheet {sheet!r} — not checked.",
        ))

    policy_ok = True
    try:
        pb.probe()
    except pb.PolicyUnavailable as exc:
        policy_ok = False
        findings.append(Finding("policy-unavailable", "warn", doc.source, str(exc)))

    for item in doc.items:
        findings.extend(rules.check_structure(item, profile))
        findings.extend(rules.check_links(item, profile))
        findings.extend(rules.check_brand_voice_own(item, profile))
        findings.extend(rules.check_wikipedia_slop(item, profile))
        findings.extend(rules.check_data_integrity(item, profile))
        from app.contentqa.readability import check_readability

        findings.extend(check_readability(item, profile))

        if policy_ok:
            findings.extend(policy_rules.check_meta(item, profile))
            findings.extend(policy_rules.check_slop_and_voice_policy(item, profile))

    findings.extend(rules.check_cross_page(doc, profile))
    if policy_ok:
        findings.extend(policy_rules.check_meta_duplicates(doc, profile))

    return findings
