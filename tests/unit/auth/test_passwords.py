"""Паролі (task-71): формат, звірка, стійкість до сміття, генерація."""

from __future__ import annotations

import pytest

from app.core.auth.passwords import (
    generate_password,
    hash_password,
    needs_rehash,
    verify_password,
)


def test_hash_round_trip() -> None:
    password = generate_password()
    encoded = hash_password(password)
    assert encoded.startswith("scrypt$")
    assert verify_password(password, encoded) is True


def test_wrong_password_is_rejected() -> None:
    encoded = hash_password("correct-horse")
    assert verify_password("correct-horsE", encoded) is False
    assert verify_password("", encoded) is False


def test_plaintext_never_appears_in_the_encoded_form() -> None:
    password = "very-secret-value"
    assert password not in hash_password(password)


def test_two_hashes_of_the_same_password_differ() -> None:
    """Різна сіль: однакові паролі двох людей не видно як однакові рядки."""
    assert hash_password("same") != hash_password("same")


@pytest.mark.parametrize(
    "garbage",
    ["", "not-a-hash", "scrypt$x$8$1$aa$bb", "scrypt$16384$8$1$@@@$###", "bcrypt$1$2$3$4$5"],
)
def test_corrupted_hash_returns_false_instead_of_raising(garbage: str) -> None:
    """Зіпсований рядок не має перетворювати форму логіну на 500."""
    assert verify_password("anything", garbage) is False


def test_needs_rehash_reacts_to_parameters() -> None:
    encoded = hash_password("pw", n=2**14)
    assert needs_rehash(encoded, n=2**14) is False
    assert needs_rehash(encoded, n=2**15) is True
    assert needs_rehash("garbage") is True


def test_generated_password_shape() -> None:
    password = generate_password()
    groups = password.split("-")
    assert len(groups) == 4
    assert all(len(group) == 4 for group in groups)
    # Алфавіт без візуально неоднозначних символів — пароль диктують уголос.
    assert not set(password) & set("0O1lI")
    assert generate_password() != generate_password()
