"""HTML signal extraction (task-35 §5)."""

from __future__ import annotations

from app.monitor.page import decode_body, extract_signals, is_indexable


def test_noindex_in_general_meta_robots_is_detected():
    html = '<html><head><meta name="robots" content="noindex, follow"></head></html>'
    signals = extract_signals(html)
    assert signals.meta_robots == "noindex, follow"


def test_googlebot_specific_meta_overrides_general_meta():
    html = (
        '<html><head><meta name="robots" content="index, follow">'
        '<meta name="googlebot" content="noindex"></head></html>'
    )
    signals = extract_signals(html)
    assert signals.meta_robots == "noindex"


def test_x_robots_tag_header_is_considered_by_is_indexable():
    assert is_indexable(meta_robots="index, follow", x_robots_tag="noindex", status=200) is False


def test_canonical_variants():
    self_canonical = extract_signals(
        '<link rel="canonical" href="https://example.com/page/">'
    ).canonical
    assert self_canonical == "https://example.com/page/"

    offsite_canonical = extract_signals(
        '<link rel="canonical" href="https://other.com/page/">'
    ).canonical
    assert offsite_canonical == "https://other.com/page/"

    assert extract_signals("<html></html>").canonical is None


def test_extracts_ga4_and_gtm_ids():
    html = (
        "<script>gtag('config', 'G-ABC1234XYZ');</script>"
        "<script>(function(w,d,s,l,i){})(window,document,'script','dataLayer','GTM-ABCD123');</script>"
    )
    signals = extract_signals(html)
    assert signals.analytics_ids == ["G-ABC1234XYZ", "GTM-ABCD123"]


def test_multiple_google_site_verification_tags_are_all_collected():
    # Live regression: alphastyle.store has two verification tokens (task-35 §1.1).
    html = (
        '<meta name="google-site-verification" content="tokenA">'
        '<meta name="google-site-verification" content="tokenB">'
    )
    assert extract_signals(html).google_site_verifications == ["tokenA", "tokenB"]


def test_no_verification_tag_is_an_empty_list_not_a_missing_signal():
    assert extract_signals("<html></html>").google_site_verifications == []


def test_malformed_tags_do_not_crash_extraction():
    html = "<html><head><title>Unterminated<meta name=robots content=noindex></html"
    signals = extract_signals(html)
    assert signals is not None


def test_script_and_style_text_excluded_from_visible_text():
    html = "<body><p>Hello world</p><script>hidden script text</script></body>"
    signals = extract_signals(html)
    assert "hidden script text" not in signals.visible_text
    assert "Hello world" in signals.visible_text


def test_is_indexable_none_when_never_checked():
    assert is_indexable(meta_robots=None, x_robots_tag=None, status=None) is None


def test_is_indexable_false_for_non_2xx_status():
    assert is_indexable(meta_robots=None, x_robots_tag=None, status=404) is False


def test_is_indexable_true_for_clean_2xx():
    assert is_indexable(meta_robots="index, follow", x_robots_tag=None, status=200) is True


def test_decode_body_uses_content_type_charset():
    body = "Café".encode("iso-8859-1")
    assert decode_body(body, content_type="text/html; charset=iso-8859-1") == "Café"


def test_decode_body_falls_back_to_utf8():
    body = "Café".encode()
    assert decode_body(body, content_type=None) == "Café"


def test_json_ld_type_extraction():
    html = (
        '<script type="application/ld+json">'
        '{"@context":"https://schema.org","@type":"LocalBusiness","name":"Acme"}'
        "</script>"
    )
    assert extract_signals(html).json_ld_types == ["LocalBusiness"]
