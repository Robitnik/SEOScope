"""Генерація й перевірка паролів (task-71) — stdlib, без нових залежностей.

`hashlib.scrypt`, а не `passlib`/`argon2-cffi`: `poetry lock` у цьому
середовищі не проходить (сусідній чекаут AISeoTools недоступний), а scrypt —
memory-hard KDF зі stdlib, чого для внутрішнього тулу з обліковими записами,
які заводить адмін, достатньо з запасом.

Формат рядка в БД: ``scrypt$<n>$<r>$<p>$<b64 salt>$<b64 hash>``. Параметри
зберігаються ПОРЯД із хешем, а не лише в конфізі, щоб їх можна було підняти
без міграції: старі хеші перевіряються своїми параметрами й прозоро
переобчислюються при наступному успішному логіні (`needs_rehash`).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

# Пароль генерує сервер і користувач його не змінює (рішення оператора: так
# працюють інші тули компанії). Тому він має бути водночас сильним і таким,
# що його можна продиктувати/переписати без помилок: алфавіт без візуально
# неоднозначних символів (0/O, 1/l/I), розбитий на групи по 4.
_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"
_GROUPS = 4
_GROUP_LEN = 4

SCRYPT_N = 2**15
SCRYPT_R = 8
SCRYPT_P = 1
_DKLEN = 32
_SALT_BYTES = 16


def generate_password() -> str:
    """Наприклад ``k7fq-2mtx-9pld-r4wn`` — 16 символів з алфавіту на 31, ~79 біт."""
    groups = [
        "".join(secrets.choice(_ALPHABET) for _ in range(_GROUP_LEN)) for _ in range(_GROUPS)
    ]
    return "-".join(groups)


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _derive(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=_DKLEN, maxmem=n * r * 256
    )


def hash_password(
    password: str, *, n: int = SCRYPT_N, r: int = SCRYPT_R, p: int = SCRYPT_P
) -> str:
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = _derive(password, salt, n, r, p)
    return f"scrypt${n}${r}${p}${_b64(salt)}${_b64(digest)}"


def _parse(encoded: str) -> tuple[int, int, int, bytes, bytes] | None:
    parts = encoded.split("$")
    if len(parts) != 6 or parts[0] != "scrypt":
        return None
    try:
        n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
        salt = base64.b64decode(parts[4])
        digest = base64.b64decode(parts[5])
    except (ValueError, TypeError):
        return None
    return n, r, p, salt, digest


def verify_password(password: str, encoded: str) -> bool:
    """`False` на будь-який зіпсований/незнайомий рядок — ніколи не виняток.

    Виняток тут перетворив би пошкоджений рядок у 500 на формі логіну, що і
    саме по собі погано, і є сигналом атакуючому.
    """
    parsed = _parse(encoded)
    if parsed is None:
        return False
    n, r, p, salt, digest = parsed
    try:
        candidate = _derive(password, salt, n, r, p)
    except ValueError:
        return False
    return hmac.compare_digest(candidate, digest)


def needs_rehash(encoded: str, *, n: int = SCRYPT_N, r: int = SCRYPT_R, p: int = SCRYPT_P) -> bool:
    parsed = _parse(encoded)
    if parsed is None:
        return True
    return (parsed[0], parsed[1], parsed[2]) != (n, r, p)


# Один заздалегідь обчислений хеш, об який «перевіряється» пароль для
# НЕІСНУЮЧОГО користувача. Без цього логін стає оракулом переліку
# співробітників: відповідь на невідомий email поверталася б за мікросекунди,
# а на відомий — за ~50 мс scrypt.
_DUMMY_HASH = hash_password("dummy-password-for-timing-equalisation")


def waste_time_like_verify() -> None:
    verify_password("dummy-password-for-timing-equalisation", _DUMMY_HASH)
