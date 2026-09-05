"""Дистилює офіційний вокабуляр schema.org у компактний офлайн-артефакт
(task-56, §2.1).

**Навіщо окремий крок, а не пряме читання оригіналу в рантаймі.**
`schemaorg-current-https.jsonld` (джерело: https://schema.org/version/latest/)
важить ~1.5 МБ і містить RDF-трійки в форматі, незручному для прямого пошуку
«яким властивостям належить цей тип» — обхід `rdfs:subClassOf` і
`schema:domainIncludes`/`rangeIncludes` довелося б робити щоразу в рантаймі.
Дистилят робить це один раз, офлайн, і комітиться в репо: рантайм валідатора
(`app/tools/schema_markup/vocab.py`) відкриває готовий JSON без жодного
мережевого виклику — детермінований, тестований, не залежить від
доступності schema.org.

**Запуск (ручний, не автоматизований — вокабуляр міняється раз на кілька
місяців):**

    docker compose exec api poetry run python scripts/build_schema_vocab.py

Результат — `app/tools/schema_markup/data/vocab.json` з полями `version`,
`generated_at`, `source_sha256`, `types` (parents + властивості, які цей тип
приймає **разом зі всіма успадкованими** — subclass-ланцюг уже розгорнутий,
щоб рантайму не доводилось рекурсивно підійматись по `parents` на кожен
запит), `properties` (`ranges`/`domains` — які типи очікує значення і які
типи цю властивість приймають).
"""

from __future__ import annotations

import gzip
import hashlib
import json
import sys
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

VOCAB_URL = "https://schema.org/version/latest/schemaorg-current-https.jsonld"
# Дистилят важить ~1.9 МБ у сирому JSON — gzip тримає його ~100 КБ у репо
# (виміряно: 1.9 МБ -> 97 КБ, -9). Рантайм-завантажувач (`vocab.py`) читає
# gzip напряму, без розпаковки на диск.
OUT_PATH = Path(__file__).resolve().parent.parent / "app/tools/schema_markup/data/vocab.json.gz"

# schema.org — не єдиний namespace у файлі: `@graph` змішує основний шар з
# bibo:, dcat:, cmns-*: та іншими зовнішніми онтологіями (@context мапить
# кожен префікс на власний namespace URI; тут не резолвимо їх узагалі — нам
# треба лише відсіяти все, що не є "schema:"). `@id`/`@type` у цьому дампі —
# завжди CURIE (`"schema:Article"`, не повний URL), тому `https?://schema.org/`
# теж лишаємо в списку про всяк випадок (інші дампи schema.org це віддають).
_SCHEMA_PREFIXES = ("schema:", "http://schema.org/", "https://schema.org/")

_TYPE_TYPES = {"rdfs:Class"}
_PROP_TYPES = {"rdf:Property"}


def _strip_prefix(value: str) -> str:
    for prefix in _SCHEMA_PREFIXES:
        if value.startswith(prefix):
            return value[len(prefix) :]
    return value


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _ref_ids(value: Any) -> list[str]:
    """`{"@id": "schema:Thing"}` / список таких / голий рядок → голі імена."""
    out: list[str] = []
    for item in _as_list(value):
        if isinstance(item, dict) and "@id" in item:
            out.append(_strip_prefix(item["@id"]))
        elif isinstance(item, str):
            out.append(_strip_prefix(item))
    return out


def _node_types(node: dict[str, Any]) -> set[str]:
    return set(_ref_ids(node.get("@type")))


def fetch_source(url: str = VOCAB_URL) -> bytes:
    request = urllib.request.Request(url, headers={"Accept": "application/ld+json"})
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 - fixed https URL
        data: bytes = response.read()
        return data


def distill(raw: bytes) -> dict[str, Any]:
    doc = json.loads(raw)
    graph = doc.get("@graph", doc if isinstance(doc, list) else [])

    types_raw: dict[str, dict[str, Any]] = {}
    props_raw: dict[str, dict[str, Any]] = {}

    for node in graph:
        if not isinstance(node, dict) or "@id" not in node:
            continue
        node_id = _strip_prefix(node["@id"])
        if not node_id or ":" in node_id:  # інший namespace (bib:, meta*)
            continue
        kinds = _node_types(node)
        if kinds & _TYPE_TYPES:
            types_raw[node_id] = node
        elif kinds & _PROP_TYPES:
            props_raw[node_id] = node

    # Властивість -> (ranges, domains) з rangeIncludes/domainIncludes.
    properties: dict[str, dict[str, list[str]]] = {}
    for name, node in props_raw.items():
        properties[name] = {
            "ranges": sorted(_ref_ids(node.get("schema:rangeIncludes"))),
            "domains": sorted(_ref_ids(node.get("schema:domainIncludes"))),
        }

    # Тип -> прямі батьки (subClassOf), лише в межах основного шару.
    direct_parents: dict[str, list[str]] = {}
    for name, node in types_raw.items():
        parents = [p for p in _ref_ids(node.get("rdfs:subClassOf")) if p in types_raw]
        direct_parents[name] = parents

    def ancestors(name: str, seen: set[str] | None = None) -> set[str]:
        seen = seen or set()
        for parent in direct_parents.get(name, []):
            if parent not in seen:
                seen.add(parent)
                ancestors(parent, seen)
        return seen

    # Тип -> усі властивості, чий domainIncludes покриває цей тип або будь-
    # якого його предка (rangeIncludes навпаки — обходимо тут само, дешевше
    # один раз офлайн, ніж рекурсивно в рантаймі валідатора).
    props_by_domain: dict[str, list[str]] = {}
    for prop_name, prop in properties.items():
        for domain in prop["domains"]:
            props_by_domain.setdefault(domain, []).append(prop_name)

    types: dict[str, dict[str, Any]] = {}
    for name in types_raw:
        all_ancestors = sorted(ancestors(name))
        own_and_inherited = set(props_by_domain.get(name, []))
        for anc in all_ancestors:
            own_and_inherited.update(props_by_domain.get(anc, []))
        types[name] = {
            "parents": direct_parents.get(name, []),
            "ancestors": all_ancestors,
            "properties": sorted(own_and_inherited),
        }

    return {
        # Дамп не несе номер версії як окреме поле (перевірено: немає ні
        # top-level ключа, ні спеціального `schema:VERSION`-вузла в `@graph`).
        # `/version/latest/` завжди віддає поточний реліз, тож `source_sha256`
        # + `generated_at` разом — єдиний надійний спосіб довести, який саме
        # зріз вокабуляру лежить у файлі.
        "version": "latest",
        "generated_at": datetime.now(UTC).isoformat(),
        "source_sha256": hashlib.sha256(raw).hexdigest(),
        "source_url": VOCAB_URL,
        "type_count": len(types),
        "property_count": len(properties),
        "types": types,
        "properties": properties,
    }


def main() -> int:
    source_path = sys.argv[1] if len(sys.argv) > 1 else None
    raw = Path(source_path).read_bytes() if source_path else fetch_source()
    vocab = distill(raw)
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(vocab, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
    OUT_PATH.write_bytes(gzip.compress(payload, compresslevel=9, mtime=0))
    print(
        f"wrote {OUT_PATH} — version={vocab['version']} "
        f"types={vocab['type_count']} properties={vocab['property_count']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
