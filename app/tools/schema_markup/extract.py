"""Екстракція schema.org розмітки з HTML: JSON-LD і Microdata (task-56 §1).

Stdlib `html.parser`/`json` only — свідомо без `extruct` (тягне `lxml` +
`pyrdfa3` + `mf2py`, і `lxml` у нашому образі присутній лише транзитивно
через optional-групу `local`; той самий вибір, що в `alt_text/verify.py`,
`redirects/trace.py`, `monitor/page.py`). `monitor/page.py` уже витягує
`json_ld_types` тим самим підходом (JSON-LD-блоки як CDATA у `<script>`) —
тут та сама техніка, поглиблена до повного графа, а не лише списку типів.

RDFa свідомо НЕ підтримується в MVP: на практиці серед клієнтських сайтів
RDFa зустрічається одиниці разів (здебільшого це помилково прийняті за RDFa
`og:`/`twitter:`-теги, які й так поза схемою) — повний RDFa-парсер (chaining,
`vocab`/`prefix`/`resource`) виправдав би `pyrdfa3`, якого ми свідомо
уникаємо. Якщо реальний клієнтський сайт колись покаже RDFa-розмітку без
дублюючого JSON-LD — розширювати сюди, а не тягнути залежність заздалегідь.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any

# schema.org допускає розмітку через кілька CURIE/URL форм одного терміна —
# нормалізуємо всі до "голого" імені, як і дистилятор вокабуляру
# (`scripts/build_schema_vocab.py::_strip_prefix`), щоб валідатор і генератор
# працювали з одним канонічним ключем незалежно від того, який запис обрав
# конкретний плагін/тема.
_TYPE_PREFIXES = ("https://schema.org/", "http://schema.org/", "schema:")

_VOID_TAGS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
})

# Теги, чиє microdata-значення читається з атрибута, а не з текстового
# вмісту (WHATWG Microdata §2.5 "elements with `itemprop` and no
# `itemscope`").
_ATTR_VALUE_TAGS: dict[str, str] = {
    "meta": "content",
    "link": "href",
    "a": "href",
    "area": "href",
    "img": "src",
    "audio": "src",
    "video": "src",
    "source": "src",
    "iframe": "src",
    "embed": "src",
    "track": "src",
    "object": "data",
    "time": "datetime",
    "data": "value",
    "meter": "value",
}


def normalize_term(value: str) -> str:
    v = value.strip()
    for prefix in _TYPE_PREFIXES:
        if v.startswith(prefix):
            return v[len(prefix) :]
    return v


@dataclass
class ExtractedEntity:
    """Один вузол розмітки (JSON-LD-обʼєкт або microdata `itemscope`),
    нормалізований до спільної форми незалежно від синтаксису джерела.
    """

    types: tuple[str, ...]
    properties: dict[str, list[Any]] = field(default_factory=dict)
    source_format: str = "json-ld"  # "json-ld" | "microdata"
    node_id: str | None = None
    raw: dict[str, Any] | None = None  # лише для json-ld — оригінальний вузол

    def to_jsonable(self) -> dict[str, Any]:
        """Плоский dict, придатний для `json.dumps`/API-відповіді — потрібен
        лише microdata (JSON-LD і так уже JSON, `raw` віддається як є): немає
        нативного JSON-подання, тож "сирий" перегляд у UI будує його з
        екстрагованого дерева (task-56, "Raw markup" view).
        """
        node: dict[str, Any] = {}
        if self.types:
            node["@type"] = list(self.types) if len(self.types) > 1 else self.types[0]
        for key, values in self.properties.items():
            converted = [_value_to_jsonable(v) for v in values]
            node[key] = converted if len(converted) > 1 else converted[0]
        return node


def _value_to_jsonable(value: Any) -> Any:
    if isinstance(value, ExtractedEntity):
        return value.to_jsonable()
    return value


@dataclass
class JsonLdBlock:
    raw_text: str
    parsed: Any | None
    error: str | None
    entities: list[ExtractedEntity]


@dataclass
class ExtractionResult:
    json_ld_blocks: list[JsonLdBlock]
    microdata_entities: list[ExtractedEntity]

    @property
    def all_entities(self) -> list[ExtractedEntity]:
        jsonld_entities = [e for block in self.json_ld_blocks for e in block.entities]
        return jsonld_entities + self.microdata_entities

    @property
    def has_valid_markup(self) -> bool:
        return bool(self.all_entities)

    @property
    def has_broken_markup(self) -> bool:
        """Розмітка присутня (є `<script type="application/ld+json">` або
        microdata-атрибути), але не дала жодної валідної сутності — це не те
        саме, що «розмітки немає взагалі» (PLAN §12).
        """
        return bool(self.json_ld_blocks) and not self.has_valid_markup


# --- JSON-LD --------------------------------------------------------------


class _JsonLdCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[str] = []
        self._in_ld_json = False
        self._buffer: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "script":
            return
        attrs_dict = {k.lower(): (v or "") for k, v in attrs}
        if attrs_dict.get("type", "").strip().lower() == "application/ld+json":
            self._in_ld_json = True
            self._buffer = []

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self._in_ld_json:
            self.blocks.append("".join(self._buffer))
            self._in_ld_json = False

    def handle_data(self, data: str) -> None:
        if self._in_ld_json:
            self._buffer.append(data)


def _node_types(node: dict[str, Any]) -> tuple[str, ...]:
    raw_type = node.get("@type")
    if raw_type is None:
        return ()
    values = raw_type if isinstance(raw_type, list) else [raw_type]
    return tuple(normalize_term(str(v)) for v in values if isinstance(v, str))


def _convert_value(value: Any) -> Any:
    if isinstance(value, dict):
        if set(value.keys()) <= {"@id"}:
            return {"@id_ref": value.get("@id")}
        return _node_to_entity(value)
    if isinstance(value, list):
        return [_convert_value(v) for v in value]
    return value


def _node_to_entity(node: dict[str, Any]) -> ExtractedEntity:
    properties: dict[str, list[Any]] = {}
    for key, value in node.items():
        if key in ("@type", "@context", "@id"):
            continue
        name = normalize_term(key)
        values = value if isinstance(value, list) else [value]
        properties[name] = [_convert_value(v) for v in values]
    return ExtractedEntity(
        types=_node_types(node),
        properties=properties,
        source_format="json-ld",
        node_id=node.get("@id") if isinstance(node.get("@id"), str) else None,
        raw=node,
    )


def flatten_jsonld(parsed: Any) -> list[ExtractedEntity]:
    """`@graph`-обгортка, голий масив вузлів або один вузол — усі три форми
    трапляються на живих клієнтських сайтах (Yoast: один обʼєкт з `@graph`
    всередині; RankMath: кілька окремих `<script>`-блоків по одному вузлу
    кожен). Вкладені посилання за `@id` між різними `<script>`-блоками
    навмисно не резолвляться в MVP (task-56 §7, L3 follow-up) — кожен блок
    читається незалежно.
    """
    if isinstance(parsed, dict) and isinstance(parsed.get("@graph"), list):
        nodes: list[Any] = parsed["@graph"]
    elif isinstance(parsed, list):
        nodes = parsed
    else:
        nodes = [parsed]
    return [_node_to_entity(node) for node in nodes if isinstance(node, dict)]


def extract_json_ld(html: str) -> list[JsonLdBlock]:
    collector = _JsonLdCollector()
    collector.feed(html)
    collector.close()
    blocks: list[JsonLdBlock] = []
    for raw_text in collector.blocks:
        stripped = raw_text.strip()
        if not stripped:
            continue
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError as exc:
            blocks.append(JsonLdBlock(raw_text=raw_text, parsed=None, error=str(exc), entities=[]))
            continue
        entities = flatten_jsonld(parsed)
        blocks.append(JsonLdBlock(raw_text=raw_text, parsed=parsed, error=None, entities=entities))
    return blocks


# --- Microdata --------------------------------------------------------------


class _Frame:
    __slots__ = ("kind", "item", "prop_name", "text_parts")

    def __init__(
        self, kind: str, item: ExtractedEntity | None = None, prop_name: str | None = None
    ) -> None:
        self.kind = kind  # "item" | "prop" | "plain"
        self.item = item
        self.prop_name = prop_name
        self.text_parts: list[str] = []


class _MicrodataParser(HTMLParser):
    """Обхід дерева з явним стеком відкритих елементів (task-56 §1): microdata
    прив'язана до DOM-вкладеності, а не до порядку тегів у потоці, тож на
    відміну від `_JsonLdCollector` тут потрібен повноцінний стек, а не один
    прапорець "усередині/зовні".
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.top_level_items: list[ExtractedEntity] = []
        self._stack: list[_Frame] = []

    def _current_item(self) -> ExtractedEntity | None:
        for frame in reversed(self._stack):
            if frame.kind == "item":
                return frame.item
        return None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attrs_dict = {k.lower(): (v or "") for k, v in attrs}
        has_itemscope = "itemscope" in attrs_dict
        itemprop = attrs_dict.get("itemprop", "").strip()
        itemtype = attrs_dict.get("itemtype", "").strip()

        if has_itemscope:
            item_type = normalize_term(itemtype.split()[0]) if itemtype else None
            new_item = ExtractedEntity(
                types=(item_type,) if item_type else (), source_format="microdata"
            )
            parent = self._current_item()
            if itemprop and parent is not None:
                for prop in itemprop.split():
                    parent.properties.setdefault(prop, []).append(new_item)
            else:
                self.top_level_items.append(new_item)
            if tag in _VOID_TAGS:
                return  # void itemscope element (malformed but be defensive) — nothing to close
            self._stack.append(_Frame("item", item=new_item))
            return

        if itemprop:
            parent = self._current_item()
            attr_name = _ATTR_VALUE_TAGS.get(tag)
            immediate_value = attrs_dict.get(attr_name, "").strip() if attr_name else ""
            if immediate_value and parent is not None:
                for prop in itemprop.split():
                    parent.properties.setdefault(prop, []).append(immediate_value)
                if tag in _VOID_TAGS:
                    return
                # елемент має і атрибутне значення (content=…), і потенційний
                # текст усередині — атрибутне значення перемагає (WHATWG
                # порядок пріоритету для meta/link/…); не накопичуємо текст.
                self._stack.append(_Frame("plain"))
                return
            if tag in _VOID_TAGS:
                return
            self._stack.append(_Frame("prop", prop_name=itemprop))
            return

        if tag not in _VOID_TAGS:
            self._stack.append(_Frame("plain"))

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in _VOID_TAGS or not self._stack:
            return
        frame = self._stack.pop()
        if frame.kind == "prop" and frame.prop_name:
            parent = self._current_item()
            text = "".join(frame.text_parts).strip()
            if parent is not None and text:
                for prop in frame.prop_name.split():
                    parent.properties.setdefault(prop, []).append(text)

    def handle_data(self, data: str) -> None:
        if self._stack and self._stack[-1].kind == "prop":
            self._stack[-1].text_parts.append(data)


def extract_microdata(html: str) -> list[ExtractedEntity]:
    parser = _MicrodataParser()
    parser.feed(html)
    parser.close()
    return parser.top_level_items


def extract_all(html: str) -> ExtractionResult:
    return ExtractionResult(
        json_ld_blocks=extract_json_ld(html), microdata_entities=extract_microdata(html)
    )
