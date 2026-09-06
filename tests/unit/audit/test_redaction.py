"""Редакція секретів у журналі (task-72) — інваріант сильніший за повноту."""

from __future__ import annotations

from app.audit.redaction import MAX_DETAILS_BYTES, prepare


def test_top_level_secret_is_masked() -> None:
    result = prepare({"email": "a@b.com", "password": "hunter2"})
    assert result["email"] == "a@b.com"
    assert result["password"] == "***REDACTED***"


def test_nested_secret_is_masked() -> None:
    result = prepare({"request": {"api_key": "sk-live-123", "name": "ok"}})
    assert result["request"]["api_key"] == "***REDACTED***"
    assert result["request"]["name"] == "ok"


def test_secret_inside_a_list_of_dicts_is_masked() -> None:
    result = prepare({"items": [{"token": "abc"}, {"ok": 1}]})
    assert result["items"][0]["token"] == "***REDACTED***"
    assert result["items"][1]["ok"] == 1


def test_oversized_payload_is_truncated_not_dropped() -> None:
    result = prepare({"big": "x" * (MAX_DETAILS_BYTES * 2)})
    assert result["truncated"] is True
    assert result["original_bytes"] > MAX_DETAILS_BYTES
    assert len(result["preview"]) <= MAX_DETAILS_BYTES


def test_unserializable_payload_does_not_raise() -> None:
    result = prepare({"bad": object()})
    # `object()` не серіалізується в JSON напряму без `default=str` у dumps
    # виклику самого prepare — тут перевіряємо, що функція взагалі не впала.
    assert isinstance(result, dict)


def test_empty_details_stay_empty() -> None:
    assert prepare(None) == {}
    assert prepare({}) == {}
