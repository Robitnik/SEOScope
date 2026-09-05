"""`app.urlkeywords.policy_shim` — the lazy AISeoTools seam degrades to
`PolicyUnavailable`, it never crashes the caller, when the sibling checkout
isn't importable."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app.urlkeywords import policy_shim


class TestPolicyAvailable:
    def test_topic_tokens_delegates_to_seo_audit_policy(self):
        tokens = policy_shim.topic_tokens("hearing aid cleaning services")
        assert "hearing" in tokens

    def test_probe_does_not_raise_when_available(self):
        policy_shim.probe()


class TestPolicyUnavailable:
    def test_import_error_becomes_policy_unavailable(self):
        with patch.dict("sys.modules", {"seo_audit": None, "seo_audit.policy": None}):
            with pytest.raises(policy_shim.PolicyUnavailable):
                policy_shim.probe()

    def test_topic_tokens_raises_policy_unavailable_when_unmounted(self):
        with patch.dict("sys.modules", {"seo_audit": None, "seo_audit.policy": None}):
            with pytest.raises(policy_shim.PolicyUnavailable):
                policy_shim.topic_tokens("anything")
