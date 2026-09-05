"""Три рівні валідації (task-56 §5): L0 синтаксис, L1 вокабуляр, L2 Google
rich-results профіль. Кожен рівень незалежний і має власне юніт-покриття —
L1 не приховує L0-помилки, L2 не приховує L1.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.tools.schema_markup.extract import ExtractedEntity, ExtractionResult
from app.tools.schema_markup.vocab import Vocabulary


class Severity(StrEnum):
    error = "error"
    warn = "warn"
    info = "info"


class Level(StrEnum):
    l0_syntax = "L0"
    l1_vocabulary = "L1"
    l2_google = "L2"


@dataclass(frozen=True)
class Finding:
    level: Level
    severity: Severity
    code: str
    message: str
    entity_types: tuple[str, ...] = ()
    property_name: str | None = None


def _walk(entities: list[ExtractedEntity]) -> list[ExtractedEntity]:
    """Плоский список сутності + усі вкладені (author, offers, address, …) —
    вкладені перевіряються тими самими правилами, бо `Offer` без `price`
    приховаиний усередині `Product` так само вводить в оману, як і на
    верхньому рівні.
    """
    out: list[ExtractedEntity] = []
    stack = list(entities)
    seen: set[int] = set()
    while stack:
        entity = stack.pop()
        if id(entity) in seen:
            continue
        seen.add(id(entity))
        out.append(entity)
        for values in entity.properties.values():
            for value in values:
                if isinstance(value, ExtractedEntity):
                    stack.append(value)
    return out


def validate_syntax(result: ExtractionResult) -> list[Finding]:
    findings: list[Finding] = []
    for block in result.json_ld_blocks:
        if block.error is not None:
            findings.append(
                Finding(
                    level=Level.l0_syntax,
                    severity=Severity.error,
                    code="invalid_json_ld",
                    message=(
                        'This <script type="application/ld+json"> block is not valid JSON: '
                        f"{block.error}"
                    ),
                )
            )
    return findings


def validate_vocabulary(entities: list[ExtractedEntity], vocab: Vocabulary) -> list[Finding]:
    findings: list[Finding] = []
    for entity in _walk(entities):
        if not entity.types:
            continue
        for type_name in entity.types:
            if not vocab.type_exists(type_name):
                findings.append(
                    Finding(
                        level=Level.l1_vocabulary,
                        severity=Severity.error,
                        code="unknown_type",
                        message=(
                            f'"{type_name}" is not a known schema.org type '
                            f"(vocab {vocab.version})."
                        ),
                        entity_types=entity.types,
                    )
                )
        known_types = [t for t in entity.types if vocab.type_exists(t)]
        if not known_types:
            continue
        for prop_name in entity.properties:
            if prop_name.startswith("@"):
                continue
            if not vocab.property_exists(prop_name):
                findings.append(
                    Finding(
                        level=Level.l1_vocabulary,
                        severity=Severity.warn,
                        code="unknown_property",
                        message=f'"{prop_name}" is not a known schema.org property.',
                        entity_types=entity.types,
                        property_name=prop_name,
                    )
                )
                continue
            if not any(vocab.property_allowed_on(prop_name, t) for t in known_types):
                findings.append(
                    Finding(
                        level=Level.l1_vocabulary,
                        severity=Severity.warn,
                        code="property_not_in_domain",
                        message=(
                            f'"{prop_name}" is not declared for {"/".join(known_types)} '
                            "in schema.org (works, but Google may ignore it)."
                        ),
                        entity_types=entity.types,
                        property_name=prop_name,
                    )
                )
    return findings


def validate_google_profile(entities: list[ExtractedEntity], vocab: Vocabulary) -> list[Finding]:
    findings: list[Finding] = []
    for entity in _walk(entities):
        for type_name in entity.types:
            profile = vocab.profile_for(type_name)
            if profile is None:
                continue
            if profile.rich_result_status == "retired":
                findings.append(
                    Finding(
                        level=Level.l2_google,
                        severity=Severity.warn,
                        code="retired_rich_result",
                        message=(
                            f"Google retired the {type_name} rich result "
                            f"(checked {profile.checked_at}). The markup stays valid schema.org "
                            f"but Google Search will not show a rich result for it."
                        ),
                        entity_types=entity.types,
                    )
                )
            elif profile.rich_result_status == "no_rich_result_but_valid":
                findings.append(
                    Finding(
                        level=Level.l2_google,
                        severity=Severity.info,
                        code="no_rich_result",
                        message=(
                            f"{type_name} has no Google Search rich result "
                            f"(checked {profile.checked_at}). Still valid for other consumers "
                            f"(Bing, AI crawlers)."
                        ),
                        entity_types=entity.types,
                    )
                )
            for prop in profile.required:
                if prop not in entity.properties:
                    findings.append(
                        Finding(
                            level=Level.l2_google,
                            severity=Severity.error,
                            code="missing_required_property",
                            message=(
                                f'{type_name} is missing "{prop}" — Google requires it for a '
                                f"{type_name} rich result ({profile.docs_url})."
                            ),
                            entity_types=entity.types,
                            property_name=prop,
                        )
                    )
            for prop in profile.recommended:
                if prop not in entity.properties:
                    findings.append(
                        Finding(
                            level=Level.l2_google,
                            severity=Severity.info,
                            code="missing_recommended_property",
                            message=(
                                f'{type_name} is missing "{prop}" — recommended by Google '
                                f"for a stronger {type_name} rich result ({profile.docs_url})."
                            ),
                            entity_types=entity.types,
                            property_name=prop,
                        )
                    )
    return findings


def validate_all(result: ExtractionResult, vocab: Vocabulary) -> list[Finding]:
    findings = validate_syntax(result)
    findings.extend(validate_vocabulary(result.all_entities, vocab))
    findings.extend(validate_google_profile(result.all_entities, vocab))
    return findings
