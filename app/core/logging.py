"""Structured (JSON) logging with a redaction filter.

This module only wires the redaction *mechanism* (a logging.Filter that
masks known-sensitive keys/patterns in log records). Modules that handle
secrets (auth headers, cookies, provider API keys) are responsible for
registering their own redaction patterns via ``add_redact_keys`` /
``add_redact_patterns``.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

REDACTED = "***REDACTED***"

_DEFAULT_REDACT_KEYS = {
    "authorization",
    "cookie",
    "set-cookie",
    "password",
    "api_key",
    "apikey",
    "api-key",
    "token",
    "secret",
    "session",
}


class RedactionFilter(logging.Filter):
    """Masks known-sensitive dict keys and regex patterns in log records."""

    def __init__(self) -> None:
        super().__init__()
        self.redact_keys: set[str] = set(_DEFAULT_REDACT_KEYS)
        self.redact_patterns: list[re.Pattern[str]] = []

    def add_keys(self, keys: Iterable[str]) -> None:
        self.redact_keys.update(k.lower() for k in keys)

    def add_patterns(self, patterns: Iterable[str | re.Pattern[str]]) -> None:
        self.redact_patterns.extend(
            p if isinstance(p, re.Pattern) else re.compile(p) for p in patterns
        )

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self._redact(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = self._redact(record.args)
            else:
                record.args = tuple(self._redact(a) for a in record.args)
        return True

    def _redact(self, value: Any) -> Any:
        if isinstance(value, str):
            for pattern in self.redact_patterns:
                value = pattern.sub(REDACTED, value)
            return value
        if isinstance(value, dict):
            return {
                k: (REDACTED if k.lower() in self.redact_keys else self._redact(v))
                for k, v in value.items()
            }
        if isinstance(value, list | tuple):
            return type(value)(self._redact(v) for v in value)
        return value


_redaction_filter = RedactionFilter()


def add_redact_keys(keys: Iterable[str]) -> None:
    """Register additional dict keys whose values must be masked in logs."""
    _redaction_filter.add_keys(keys)


def add_redact_patterns(patterns: Iterable[str | re.Pattern[str]]) -> None:
    """Register additional regex patterns to mask within log message strings."""
    _redaction_filter.add_patterns(patterns)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


_configured = False


def configure_logging(level: str = "INFO") -> None:
    global _configured
    root = logging.getLogger()
    root.setLevel(level.upper())

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(_redaction_filter)

    root.handlers.clear()
    root.addHandler(handler)
    _configured = True


def get_logger(name: str) -> logging.Logger:
    if not _configured:
        configure_logging()
    return logging.getLogger(name)
