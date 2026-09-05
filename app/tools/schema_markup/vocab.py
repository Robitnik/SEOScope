"""Завантаження вендореного вокабуляру schema.org і Google rich-results
профілю (task-56 §2.1). Нуль мережевих викликів у рантаймі — обидва файли
читаються з диска, кешуються модульним рівнем один раз за процес.
"""

from __future__ import annotations

import gzip
import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

_DATA_DIR = Path(__file__).resolve().parent / "data"
_VOCAB_PATH = _DATA_DIR / "vocab.json.gz"
_GOOGLE_PROFILES_PATH = _DATA_DIR / "google_profiles.json"

# Типи, які генератор НІКОЛИ не пропонує, незалежно від Google-профілю
# (task-56 §1, "Правила генерації" п.3) — навіть якщо колись до
# `google_profiles.json` додадуть запис із доброю recommended-секцією.
FORBIDDEN_GENERATE_TYPES = frozenset({
    "AggregateRating",
    "Review",
    "HowTo",
    "FAQPage",
})


@dataclass(frozen=True)
class TypeInfo:
    name: str
    parents: tuple[str, ...]
    ancestors: tuple[str, ...]
    properties: frozenset[str]


@dataclass(frozen=True)
class PropertyInfo:
    name: str
    ranges: tuple[str, ...]
    domains: tuple[str, ...]


@dataclass(frozen=True)
class GoogleProfile:
    type_name: str
    rich_result_status: str  # "live" | "no_rich_result_but_valid" | "retired"
    docs_url: str
    checked_at: str
    required: tuple[str, ...]
    recommended: tuple[str, ...]
    notes: str | None = None


@dataclass(frozen=True)
class Vocabulary:
    version: str
    generated_at: str
    source_sha256: str
    types: dict[str, TypeInfo]
    properties: dict[str, PropertyInfo]
    google_profiles: dict[str, GoogleProfile]

    def type_exists(self, name: str) -> bool:
        return name in self.types

    def property_exists(self, name: str) -> bool:
        return name in self.properties

    def property_allowed_on(self, prop_name: str, type_name: str) -> bool:
        """Чи приймає `type_name` (або будь-який його предок) цю властивість.

        `domainIncludes` у schema.org — це "OR", не "AND": властивість,
        оголошена на `CreativeWork`, застосовна до `BlogPosting`, бо
        `BlogPosting` — підклас `CreativeWork`. Дистилят уже розгорнув це в
        `properties` кожного типу (`build_schema_vocab.py::distill`), тож тут
        — проста перевірка належності множині, без рекурсії по предках.
        """
        info = self.types.get(type_name)
        if info is None:
            return False
        return prop_name in info.properties

    def profile_for(self, type_name: str) -> GoogleProfile | None:
        return self.google_profiles.get(type_name)


def _load_vocab_json() -> dict[str, Any]:
    with gzip.open(_VOCAB_PATH, "rt", encoding="utf-8") as fh:
        data: dict[str, Any] = json.load(fh)
        return data


def _load_google_profiles_json() -> dict[str, Any]:
    with _GOOGLE_PROFILES_PATH.open(encoding="utf-8") as fh:
        data: dict[str, Any] = json.load(fh)
        return data


@lru_cache(maxsize=1)
def load_vocabulary() -> Vocabulary:
    raw = _load_vocab_json()
    types = {
        name: TypeInfo(
            name=name,
            parents=tuple(t["parents"]),
            ancestors=tuple(t["ancestors"]),
            properties=frozenset(t["properties"]),
        )
        for name, t in raw["types"].items()
    }
    properties = {
        name: PropertyInfo(name=name, ranges=tuple(p["ranges"]), domains=tuple(p["domains"]))
        for name, p in raw["properties"].items()
    }
    profiles_raw = _load_google_profiles_json()
    google_profiles = {
        name: GoogleProfile(
            type_name=name,
            rich_result_status=entry["rich_result_status"],
            docs_url=entry["docs_url"],
            checked_at=entry["checked_at"],
            required=tuple(entry.get("required", [])),
            recommended=tuple(entry.get("recommended", [])),
            notes=entry.get("notes"),
        )
        for name, entry in profiles_raw.items()
        if not name.startswith("_")
    }
    return Vocabulary(
        version=raw["version"],
        generated_at=raw["generated_at"],
        source_sha256=raw["source_sha256"],
        types=types,
        properties=properties,
        google_profiles=google_profiles,
    )
