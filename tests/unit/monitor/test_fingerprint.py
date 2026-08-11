"""Three-tier fingerprint (task-35 §1.3/§5) — the key decision this task
rests on: raw-byte hashing must ignore known per-request noise.
"""

from __future__ import annotations

from app.monitor.fingerprint import (
    build_signals,
    mask_known_noise,
    raw_fingerprint,
    signals_fingerprint,
    text_fingerprint,
)


def test_cloudflare_cv_params_noise_is_masked_before_hashing():
    # Live regression: goreport.com's raw bytes change every request via
    # `window.__CF$cv$params={r:'...'}`  (task-35 §1.3).
    body_1 = b"<html><script>window.__CF$cv$params={r:'a296eb4b7f40ca38',t:'x'}</script>hi</html>"
    body_2 = b"<html><script>window.__CF$cv$params={r:'a296eb58c97377a4',t:'x'}</script>hi</html>"
    assert raw_fingerprint(body_1) == raw_fingerprint(body_2)


def test_real_content_change_still_changes_the_raw_fingerprint():
    assert raw_fingerprint(b"<html>A</html>") != raw_fingerprint(b"<html>B</html>")


def test_nonce_attribute_is_masked():
    a = b'<script nonce="aaaaaaaa">x</script>'
    b = b'<script nonce="bbbbbbbb">x</script>'
    assert raw_fingerprint(a) == raw_fingerprint(b)


def test_csrf_token_value_is_masked():
    a = b'<meta name="csrf-token" content="AAA111">'
    b = b'<meta name="csrf-token" content="BBB222">'
    assert raw_fingerprint(a) == raw_fingerprint(b)


def test_mask_known_noise_is_idempotent_on_clean_input():
    clean = b"<html>nothing volatile here</html>"
    assert mask_known_noise(clean) == clean


def test_text_fingerprint_is_stable_and_change_sensitive():
    assert text_fingerprint("hello world") == text_fingerprint("hello world")
    assert text_fingerprint("hello world") != text_fingerprint("hello there")


def _sample_signals(**overrides):
    base = dict(
        status=200,
        final_url="https://x.com/",
        meta_robots=None,
        x_robots_tag=None,
        canonical="https://x.com/",
        title="T",
        description="D",
        h1="H",
        hreflang=[("en", "https://x.com/en/")],
        json_ld_types=["Organization"],
        analytics_ids=["G-ABC"],
        google_site_verifications=["tok"],
        indexable=True,
    )
    base.update(overrides)
    return build_signals(**base)


def test_signals_fingerprint_is_deterministic():
    assert signals_fingerprint(_sample_signals()) == signals_fingerprint(_sample_signals())


def test_signals_fingerprint_changes_when_meta_robots_changes():
    a = signals_fingerprint(_sample_signals())
    b = signals_fingerprint(_sample_signals(meta_robots="noindex", indexable=False))
    assert a != b


def test_signals_fingerprint_ignores_hreflang_list_order():
    a = _sample_signals(hreflang=[("en", "https://x.com/en/"), ("fr", "https://x.com/fr/")])
    b = _sample_signals(hreflang=[("fr", "https://x.com/fr/"), ("en", "https://x.com/en/")])
    assert signals_fingerprint(a) == signals_fingerprint(b)
