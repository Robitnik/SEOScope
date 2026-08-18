"""`yovth-report audit-ninja-bindings` / `accuracy-report` (task-48 §2.2/§2.4).

Exercised through `app.cli.main` against an isolated temp DB (`cli_env`) —
these are the CLI's own contract, not `app/api/reports.py`'s (which has its
own tests in `tests/unit/api/test_reports_api.py` for `list_client_accuracy`
and the discovery/save endpoints these commands share logic with).
"""

from __future__ import annotations

from app.cli import main


def _seed_client(*, name: str, slug: str, ninja_report_name: str | None = None) -> str:
    from app.db.repositories.clients import ClientRepository
    from app.db.session import get_sync_sessionmaker
    from app.schemas.enums import Provider, SourceMode

    with get_sync_sessionmaker()() as session:
        repo = ClientRepository(session)
        client = repo.create(name=name, slug=slug, domain=f"{slug}.example.com")
        session.flush()
        config = {"connection_key": "seo@x", "account_id": "properties/1"}
        if ninja_report_name is not None:
            config["ninja_report_name"] = ninja_report_name
        repo.upsert_integration(
            client.id, Provider.reporting_ninja, source_mode=SourceMode.api, config=config,
        )
        session.commit()
        return client.id


# --- audit-ninja-bindings -----------------------------------------------------


def test_audit_finds_no_binding_case(cli_env, capsys, monkeypatch):
    monkeypatch.delenv("REPORTING_NINJA_EMAIL", raising=False)
    monkeypatch.delenv("REPORTING_NINJA_PASSWORD", raising=False)
    _seed_client(name="EFFYS", slug="effys")

    exit_code = main(["audit-ninja-bindings"])
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "EFFYS" in out
    assert "no binding" in out


def test_audit_flags_a_swm_style_foreign_binding_without_fixing_it(cli_env, capsys, monkeypatch):
    monkeypatch.delenv("REPORTING_NINJA_EMAIL", raising=False)
    monkeypatch.delenv("REPORTING_NINJA_PASSWORD", raising=False)
    client_id = _seed_client(name="SWM", slug="swm", ninja_report_name="EFFYS-Internal report")

    exit_code = main(["audit-ninja-bindings"])
    out = capsys.readouterr().out
    assert exit_code == 1  # an unfixed foreign binding is a signal worth a non-zero exit
    assert "FOREIGN BINDING" in out
    assert "EFFYS-Internal report" in out

    from app.db.repositories.clients import ClientRepository
    from app.db.session import get_sync_sessionmaker
    from app.schemas.enums import Provider

    with get_sync_sessionmaker()() as session:
        integration = ClientRepository(session).get_integration(
            client_id, Provider.reporting_ninja
        )
        assert integration.config_json["ninja_report_name"] == "EFFYS-Internal report"  # untouched


def test_audit_fix_clears_the_foreign_binding_and_never_guesses_a_new_one(
    cli_env, capsys, monkeypatch
):
    monkeypatch.delenv("REPORTING_NINJA_EMAIL", raising=False)
    monkeypatch.delenv("REPORTING_NINJA_PASSWORD", raising=False)
    client_id = _seed_client(name="SWM", slug="swm", ninja_report_name="EFFYS-Internal report")

    exit_code = main(["audit-ninja-bindings", "--fix"])
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "cleared" in out

    from app.db.repositories.clients import ClientRepository
    from app.db.session import get_sync_sessionmaker
    from app.schemas.enums import Provider

    with get_sync_sessionmaker()() as session:
        integration = ClientRepository(session).get_integration(
            client_id, Provider.reporting_ninja
        )
        assert "ninja_report_name" not in integration.config_json


def test_audit_leaves_a_correctly_bound_client_untouched(cli_env, capsys, monkeypatch):
    monkeypatch.delenv("REPORTING_NINJA_EMAIL", raising=False)
    monkeypatch.delenv("REPORTING_NINJA_PASSWORD", raising=False)
    _seed_client(name="EFFYS", slug="effys", ninja_report_name="EFFYS-Internal report")

    exit_code = main(["audit-ninja-bindings", "--fix"])
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "FOREIGN" not in out
    assert "ok:" in out


# --- accuracy-report -----------------------------------------------------------


def test_accuracy_report_cli_lists_bindings_and_exits_nonzero_on_a_foreign_one(
    cli_env, capsys, monkeypatch
):
    monkeypatch.delenv("REPORTING_NINJA_EMAIL", raising=False)
    monkeypatch.delenv("REPORTING_NINJA_PASSWORD", raising=False)
    _seed_client(name="EFFYS", slug="effys", ninja_report_name="EFFYS-Internal report")
    _seed_client(name="SWM", slug="swm", ninja_report_name="EFFYS-Internal report")

    exit_code = main(["accuracy-report"])
    out = capsys.readouterr().out
    assert "EFFYS" in out and "binding=ok" in out
    assert "SWM" in out and "binding=foreign" in out
    assert exit_code == 1


def test_accuracy_report_cli_is_clean_when_nothing_needs_attention(cli_env, capsys, monkeypatch):
    monkeypatch.delenv("REPORTING_NINJA_EMAIL", raising=False)
    monkeypatch.delenv("REPORTING_NINJA_PASSWORD", raising=False)
    _seed_client(name="EFFYS", slug="effys", ninja_report_name="EFFYS-Internal report")

    exit_code = main(["accuracy-report"])
    assert exit_code == 0
    assert "no PDF run yet" in capsys.readouterr().out
