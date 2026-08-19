"""Application Passwords поза БД (task-51 §3.3) — файл, права, і головне:

пароль ніколи не з'являється в repr/логах/помилках.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

from app.monitor.redirects.credentials import (
    CredentialsStatus,
    WpCredentials,
    WpLoginCredentials,
    basic_auth_header,
    credentials_status,
    load_credentials,
    load_login_credentials,
    login_credentials_status,
    write_credentials,
    write_login_credentials,
)


def test_missing_file_is_status_missing_without_raising(tmp_path: Path) -> None:
    path = tmp_path / "nope" / "wp_credentials.json"
    assert credentials_status(path, "client-1") is CredentialsStatus.missing
    assert load_credentials(path, "client-1") is None


def test_write_then_load_roundtrips(tmp_path: Path) -> None:
    path = tmp_path / "secrets" / "wp_credentials.json"
    write_credentials(path, "client-1", user="monitor-bot", app_password="abcd efgh ijkl mnop")
    creds = load_credentials(path, "client-1")
    assert creds is not None
    assert creds.user == "monitor-bot"
    assert creds.app_password == "abcd efgh ijkl mnop"
    assert credentials_status(path, "client-1") is CredentialsStatus.configured


def test_written_file_has_owner_only_permissions(tmp_path: Path) -> None:
    path = tmp_path / "wp_credentials.json"
    write_credentials(path, "client-1", user="u", app_password="p")
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == (stat.S_IRUSR | stat.S_IWUSR)


def test_client_without_an_entry_is_missing_not_invalid(tmp_path: Path) -> None:
    path = tmp_path / "wp_credentials.json"
    write_credentials(path, "client-1", user="u", app_password="p")
    assert credentials_status(path, "client-2") is CredentialsStatus.missing
    assert load_credentials(path, "client-2") is None


def test_corrupt_json_file_is_invalid_not_an_exception(tmp_path: Path) -> None:
    path = tmp_path / "wp_credentials.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not valid json", encoding="utf-8")
    assert credentials_status(path, "client-1") is CredentialsStatus.invalid
    assert load_credentials(path, "client-1") is None


def test_entry_missing_app_password_field_is_invalid(tmp_path: Path) -> None:
    path = tmp_path / "wp_credentials.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"client-1": {"user": "u"}}), encoding="utf-8")
    assert credentials_status(path, "client-1") is CredentialsStatus.invalid
    assert load_credentials(path, "client-1") is None


def test_multiple_clients_coexist_in_one_file(tmp_path: Path) -> None:
    path = tmp_path / "wp_credentials.json"
    write_credentials(path, "client-1", user="u1", app_password="p1")
    write_credentials(path, "client-2", user="u2", app_password="p2")
    creds1 = load_credentials(path, "client-1")
    creds2 = load_credentials(path, "client-2")
    assert creds1 is not None and creds1.user == "u1"
    assert creds2 is not None and creds2.user == "u2"


class TestSecretNeverLeaks:
    def test_repr_masks_the_password(self) -> None:
        creds = WpCredentials(user="monitor-bot", app_password="super-secret-value")
        assert "super-secret-value" not in repr(creds)
        assert "monitor-bot" in repr(creds)

    def test_str_via_default_dataclass_str_also_masks(self) -> None:
        # dataclass не перевизначає __str__, тож str() падає назад на
        # __repr__ — маскування працює й тут без окремого перевизначення.
        creds = WpCredentials(user="monitor-bot", app_password="super-secret-value")
        assert "super-secret-value" not in str(creds)

    def test_basic_auth_header_is_well_formed_and_not_plaintext(self) -> None:
        creds = WpCredentials(user="monitor-bot", app_password="super-secret-value")
        header = basic_auth_header(creds)
        assert header.startswith("Basic ")
        assert "super-secret-value" not in header
        assert "monitor-bot" not in header


# --- regular wp-admin login credentials (browser fallback) -----------------


class TestLoginCredentials:
    def test_missing_file_is_status_missing(self, tmp_path: Path) -> None:
        path = tmp_path / "wp_login_credentials.json"
        assert login_credentials_status(path, "client-1") is CredentialsStatus.missing
        assert load_login_credentials(path, "client-1") is None

    def test_write_then_load_roundtrips(self, tmp_path: Path) -> None:
        path = tmp_path / "wp_login_credentials.json"
        write_login_credentials(path, "client-1", login="admin@client.com", password="Str0ng!Pass")
        creds = load_login_credentials(path, "client-1")
        assert creds is not None
        assert creds.login == "admin@client.com"
        assert creds.password == "Str0ng!Pass"
        assert login_credentials_status(path, "client-1") is CredentialsStatus.configured

    def test_written_file_has_owner_only_permissions(self, tmp_path: Path) -> None:
        path = tmp_path / "wp_login_credentials.json"
        write_login_credentials(path, "client-1", login="u", password="p")
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == (stat.S_IRUSR | stat.S_IWUSR)

    def test_missing_password_field_is_invalid(self, tmp_path: Path) -> None:
        path = tmp_path / "wp_login_credentials.json"
        path.write_text(json.dumps({"client-1": {"login": "admin"}}), encoding="utf-8")
        assert login_credentials_status(path, "client-1") is CredentialsStatus.invalid
        assert load_login_credentials(path, "client-1") is None

    def test_app_password_file_and_login_file_are_independent(self, tmp_path: Path) -> None:
        # The two credential kinds are never stored together, on purpose —
        # a client with one configured is not implicitly "configured" for
        # the other.
        app_password_path = tmp_path / "wp_credentials.json"
        login_path = tmp_path / "wp_login_credentials.json"
        write_credentials(app_password_path, "client-1", user="u", app_password="p")
        assert login_credentials_status(login_path, "client-1") is CredentialsStatus.missing
        assert credentials_status(app_password_path, "client-1") is CredentialsStatus.configured

    def test_repr_masks_the_password(self) -> None:
        creds = WpLoginCredentials(login="admin@client.com", password="super-secret-value")
        assert "super-secret-value" not in repr(creds)
        assert "admin@client.com" in repr(creds)
