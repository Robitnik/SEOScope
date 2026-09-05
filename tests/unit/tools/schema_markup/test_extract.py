"""JSON-LD + Microdata екстракція (task-56 §1) на золотих HTML-фікстурах:
Yoast (`@graph`-обгортка), RankMath (кілька окремих блоків), WooCommerce
(microdata Product), зламаний JSON, порожня сторінка.
"""

from __future__ import annotations

from app.tools.schema_markup.extract import extract_all, extract_json_ld, extract_microdata

YOAST_HTML = """
<html><head>
<script type="application/ld+json">
{"@context":"https://schema.org","@graph":[
  {"@type":"Organization","@id":"https://example.com/#org","name":"Acme Co",
   "url":"https://example.com/","logo":{"@id":"https://example.com/#logo"}},
  {"@type":"WebSite","@id":"https://example.com/#website",
   "url":"https://example.com/","name":"Acme"},
  {"@type":["BlogPosting","Article"],"headline":"How we did it",
   "datePublished":"2026-08-01T10:00:00+00:00",
   "author":{"@type":"Person","name":"Jane Doe"}}
]}
</script>
</head><body>hello</body></html>
"""

RANKMATH_HTML = """
<html><head>
<script type="application/ld+json">{"@context":"https://schema.org","@type":"Organization","name":"Acme"}</script>
<script type="application/ld+json">{"@context":"https://schema.org","@type":"BreadcrumbList","itemListElement":[{"@type":"ListItem","position":1,"name":"Home","item":"https://example.com/"}]}</script>
</head><body></body></html>
"""

BROKEN_JSON_HTML = """
<html><head>
<script type="application/ld+json">{"@type": "Organization", "name": "Acme",}</script>
</head></html>
"""

EMPTY_HTML = "<html><head><title>No schema here</title></head><body><p>Just text.</p></body></html>"

WOOCOMMERCE_HTML = """
<html><body>
<div itemscope itemtype="https://schema.org/Product">
  <span itemprop="name">Red Mug</span>
  <img itemprop="image" src="https://example.com/mug.jpg">
  <div itemprop="offers" itemscope itemtype="https://schema.org/Offer">
    <meta itemprop="priceCurrency" content="USD">
    <span itemprop="price">19.99</span>
    <link itemprop="availability" href="https://schema.org/InStock">
  </div>
</div>
</body></html>
"""


def test_yoast_graph_flattens_all_three_nodes():
    blocks = extract_json_ld(YOAST_HTML)
    assert len(blocks) == 1
    block = blocks[0]
    assert block.error is None
    assert len(block.entities) == 3
    types = [e.types for e in block.entities]
    assert ("Organization",) in types
    assert ("WebSite",) in types
    assert ("BlogPosting", "Article") in types


def test_yoast_nested_author_becomes_entity():
    block = extract_json_ld(YOAST_HTML)[0]
    posting = next(e for e in block.entities if "BlogPosting" in e.types)
    author_values = posting.properties["author"]
    assert len(author_values) == 1
    author = author_values[0]
    assert author.types == ("Person",)
    assert author.properties["name"] == ["Jane Doe"]


def test_yoast_id_reference_kept_as_marker_not_resolved():
    block = extract_json_ld(YOAST_HTML)[0]
    org = next(e for e in block.entities if e.types == ("Organization",))
    logo_ref = org.properties["logo"][0]
    assert logo_ref == {"@id_ref": "https://example.com/#logo"}


def test_rankmath_multiple_blocks_each_read_independently():
    blocks = extract_json_ld(RANKMATH_HTML)
    assert len(blocks) == 2
    assert blocks[0].entities[0].types == ("Organization",)
    assert blocks[1].entities[0].types == ("BreadcrumbList",)


def test_broken_json_reported_not_silently_dropped():
    blocks = extract_json_ld(BROKEN_JSON_HTML)
    assert len(blocks) == 1
    assert blocks[0].error is not None
    assert blocks[0].entities == []
    result = extract_all(BROKEN_JSON_HTML)
    assert result.has_broken_markup is True
    assert result.has_valid_markup is False


def test_empty_page_has_no_markup_and_no_broken_markup():
    result = extract_all(EMPTY_HTML)
    assert result.json_ld_blocks == []
    assert result.microdata_entities == []
    assert result.has_valid_markup is False
    assert result.has_broken_markup is False


def test_woocommerce_microdata_nested_offer():
    items = extract_microdata(WOOCOMMERCE_HTML)
    assert len(items) == 1
    product = items[0]
    assert product.types == ("Product",)
    assert product.properties["name"] == ["Red Mug"]
    assert product.properties["image"] == ["https://example.com/mug.jpg"]
    offer_values = product.properties["offers"]
    assert len(offer_values) == 1
    offer = offer_values[0]
    assert offer.types == ("Offer",)
    assert offer.properties["priceCurrency"] == ["USD"]
    assert offer.properties["price"] == ["19.99"]
    assert offer.properties["availability"] == ["https://schema.org/InStock"]


def test_microdata_absent_on_plain_page():
    assert extract_microdata(EMPTY_HTML) == []


def test_microdata_to_jsonable_builds_nested_json_tree():
    # "Raw markup" view (task-56 follow-up): microdata has no native JSON form,
    # so `to_jsonable()` builds one from the extracted tree — single-value
    # properties collapse to a bare value, multi-value ones stay a list.
    product = extract_microdata(WOOCOMMERCE_HTML)[0]
    node = product.to_jsonable()
    assert node["@type"] == "Product"
    assert node["name"] == "Red Mug"
    assert node["image"] == "https://example.com/mug.jpg"
    offer = node["offers"]
    assert offer["@type"] == "Offer"
    assert offer["priceCurrency"] == "USD"
    assert offer["price"] == "19.99"


def test_json_ld_entity_to_jsonable_mirrors_types_and_properties():
    block = extract_json_ld(YOAST_HTML)[0]
    posting = next(e for e in block.entities if "BlogPosting" in e.types)
    node = posting.to_jsonable()
    assert node["@type"] == ["BlogPosting", "Article"]
    assert node["headline"] == "How we did it"
    assert node["author"]["@type"] == "Person"
    assert node["author"]["name"] == "Jane Doe"
