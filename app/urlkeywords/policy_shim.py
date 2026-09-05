"""Lazy-seam over AISeoTools' `seo_audit.policy`, scoped to the URL
Inspector's keyword panel (task-64).

A sibling of `app.contentqa.policy_bridge` kept deliberately separate:
extending that module would mean editing task-53's owned file for a use
case (tokenizing a whole crawled page body, not QA-checking authored copy
against a target keyword) it was never scoped for. Same lazy-import-at-
call-time pattern as that module and `app.integrations.{ahrefs,blogs}`:
importing this module never touches the AISeoTools checkout, and callers
degrade to "no data" (`PolicyUnavailable`) instead of crashing when it
isn't bind-mounted.
"""

from __future__ import annotations

from typing import Protocol


class PolicyUnavailable(RuntimeError):
    """`seo_audit.policy` isn't importable — AISeoTools isn't mounted."""


class _Policy(Protocol):
    def topic_tokens(self, text: str) -> list[str]: ...
    def is_brand_query(self, keyword: str, *, brand_name: str, domain: str) -> bool: ...


def _policy() -> _Policy:
    try:
        from seo_audit import policy
    except ImportError as exc:
        raise PolicyUnavailable(
            "seo_audit.policy is not importable: AISeoTools is not bind-mounted "
            "in this container (compose.yaml AISEOTOOLS_PATH). On-page keyword "
            "extraction and site-query matching are skipped for this URL; every "
            "other URL Inspector panel still works."
        ) from exc
    return policy  # type: ignore[no-any-return]


def probe() -> None:
    """Raises `PolicyUnavailable` without doing any tokenizing work — lets a
    caller fail fast once per request instead of per token call."""
    _policy()


def topic_tokens(text: str) -> list[str]:
    return _policy().topic_tokens(text)


def is_brand_query(keyword: str, *, brand_name: str = "", domain: str = "") -> bool:
    return _policy().is_brand_query(keyword, brand_name=brand_name, domain=domain)
