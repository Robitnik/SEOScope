"""Hardening-гейт: секрети, path traversal, заголовки, безпечні помилки
(PLAN §13, §15, §18).

Головний тест тут — «протікаючий адаптер»: у нього навмисно закладено все,
що адаптеру заборонено віддавати назовні (Bearer-токен у повідомленні,
`api_key`/`cookie` в `details`, секрет у тексті винятку). Далі перевіряється,
що жодне з цих значень не осіло ні в БД, ні в події, ні у відповіді API.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from harness import (
    KNOWN_SECRET_MATERIAL,
    FakeScenario,
    register_fakes,
    reset_registry,
    running_worker,
    scan_sqlite_for_secrets,
    scan_text,
    wait_for,
)
from sqlalchemy import select

from app.core.security import PathTraversalError, safe_resolve
from app.db.models import Artifact, JobEvent
from app.db.repositories.cells import CellRepository
from app.db.repositories.jobs import JobRepository
from app.schemas.adapter import PreflightResult, ProgressEvent
from app.schemas.enums import CellStatus, ComparisonMode, PeriodKind, Provider

pytestmark = pytest.mark.integration

API_KEY, PASSWORD, SESSION_COOKIE = KNOWN_SECRET_MATERIAL
REPO_ROOT = Path(__file__).resolve().parents[2]


class LeakyAdapter:
    """Адаптер, який порушує контракт усіма доступними способами."""

    def __init__(self, provider: Provider) -> None:
        self.provider = provider

    def preflight(self, context):  # noqa: ANN001, ANN201 - протокол task-02
        return PreflightResult(provider=self.provider, ok=True)

    def collect(self, context, emit, is_cancel_requested):  # noqa: ANN001, ANN201
        emit(
            ProgressEvent(
                provider=self.provider,
                stage="authenticate",
                percent=10.0,
                message=f"GET /v1/report Authorization: Bearer {API_KEY}",
                details={
                    "api_key": API_KEY,
                    "cookie": f"ninja_session={SESSION_COOKIE}",
                    "session_path": "/seo/runtime/profiles/semrush/Default/Cookies",
                    "domain": "acme.example.com",
                },
            )
        )
        raise RuntimeError(f"login failed for user acme with password={PASSWORD}")


def _run_leaky_cell(temp_db, monkeypatch, seed_client):
    reset_registry(monkeypatch)
    from app.integrations import registry

    registry.register_adapter(LeakyAdapter(Provider.semrush))

    client_id = seed_client(scenarios={Provider.semrush: FakeScenario()})
    with temp_db.session_scope() as session:
        job = JobRepository(session).create_run(
            client_ids=[client_id],
            providers=[Provider.semrush],
            period_kind=PeriodKind.week,
            comparison_mode=ComparisonMode.closed,
            anchor_date=datetime(2026, 6, 15, tzinfo=UTC).date(),
        )[0]
        job_id = job.id

    with running_worker(temp_db.session_scope, providers=(Provider.semrush,)):
        wait_for(
            lambda: _cell_status(temp_db, job_id) == CellStatus.failed,
            timeout=30,
            what="клітинка впала",
        )
    return job_id


def _cell_status(temp_db, job_id):
    with temp_db.session_scope() as session:
        return CellRepository(session).list_for_job(job_id)[0].status


def test_leaked_secrets_never_reach_the_database(temp_db, monkeypatch, seed_client):
    _run_leaky_cell(temp_db, monkeypatch, seed_client)

    findings = scan_sqlite_for_secrets(temp_db.path)
    assert findings == {}, f"секрети осіли в БД: {findings}"


def test_failed_cell_stores_a_safe_error_not_the_raw_exception(
    temp_db, monkeypatch, seed_client, api_client
):
    job_id = _run_leaky_cell(temp_db, monkeypatch, seed_client)

    with temp_db.session_scope() as session:
        cell = CellRepository(session).list_for_job(job_id)[0]
        assert cell.error_code == "RuntimeError"
        assert cell.error_message == "collection failed; see server logs for detail"
        assert PASSWORD not in (cell.error_message or "")

    body = api_client.get(f"/api/v1/sync-jobs/{job_id}").text
    assert scan_text(body) == [], "відповідь API не має містити ані секрету, ані сирого трейсу"


def test_emitted_event_is_redacted_key_by_key(temp_db, monkeypatch, seed_client):
    job_id = _run_leaky_cell(temp_db, monkeypatch, seed_client)

    with temp_db.session_scope() as session:
        events = list(session.scalars(select(JobEvent).where(JobEvent.job_id == job_id)))

    assert events, "подія стадії має зберегтись — редакція не означає втрату аудиту"
    event = events[0]
    assert "***REDACTED***" in event.message
    assert event.details_json["api_key"] == "***REDACTED***"
    assert event.details_json["cookie"] == "***REDACTED***"
    assert event.details_json["session_path"] == "***REDACTED***"
    assert event.details_json["domain"] == "acme.example.com", (
        "редакція не має з'їдати несекретні поля"
    )


def test_artifact_download_refuses_paths_outside_the_root(temp_db, api_client):
    """`../` у `path` не має віддати файл із-поза artifacts root (PLAN §13)."""
    with pytest.raises(PathTraversalError):
        safe_resolve(temp_db.artifacts_dir, "../../etc/passwd")
    with pytest.raises(PathTraversalError):
        safe_resolve(temp_db.artifacts_dir, "/etc/passwd")

    # Рядок, що обійшов запис (наприклад, лишився з давнішої версії схеми),
    # все одно не має конвертуватись у видачу файлу.
    escape_target = temp_db.artifacts_dir.parent / "outside.txt"
    escape_target.write_text("must never be served")

    with temp_db.session_scope() as session:
        client_id = None
        from app.db.models import Client

        client = Client(name="Acme", slug="acme", domain="acme.example.com")
        session.add(client)
        session.flush()
        client_id = client.id
        job = JobRepository(session).create_run(
            client_ids=[client_id],
            providers=[Provider.ahrefs],
            period_kind=PeriodKind.week,
            comparison_mode=ComparisonMode.closed,
            anchor_date=datetime(2026, 6, 15, tzinfo=UTC).date(),
        )[0]
        artifact = Artifact(
            job_id=job.id,
            provider=Provider.ahrefs,
            kind="raw",
            path="../outside.txt",
            sha256="0" * 64,
            mime="text/plain",
        )
        session.add(artifact)
        session.flush()
        artifact_id = artifact.id

    response = api_client.get(f"/api/v1/artifacts/{artifact_id}")
    assert response.status_code == 404
    assert response.json()["detail"]["error_code"] == "artifact_not_found"


def test_responses_carry_security_headers_and_no_permissive_cors(api_client):
    response = api_client.get("/healthz")
    assert response.status_code == 200
    headers = response.headers
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "DENY"
    assert headers["Referrer-Policy"] == "no-referrer"
    assert "default-src 'self'" in headers["Content-Security-Policy"]

    preflight = api_client.options(
        "/api/v1/dashboard",
        headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"},
    )
    assert preflight.headers.get("access-control-allow-origin") is None, (
        "CORS за замовчуванням має бути same-origin"
    )


def test_unknown_client_yields_a_structured_error_without_internals(api_client, fake_adapters):
    response = api_client.post(
        "/api/v1/sync-jobs",
        json={
            "client_ids": ["00000000-0000-0000-0000-000000000000"],
            "providers": "all",
            "period_kind": "week",
        },
    )
    assert response.status_code == 404
    detail = response.json()["detail"]
    assert detail["error_code"] == "client_not_found"
    assert "Traceback" not in response.text
    assert scan_text(response.text) == []


def test_committed_fixtures_and_env_example_carry_no_secrets():
    """Sanitized-фікстури мусять бути справді sanitized (ризик із плану таски)."""
    scanned = 0
    for path in (REPO_ROOT / "tests" / "fixtures").rglob("*"):
        if not path.is_file() or path.suffix in {".xlsx", ".png", ".webp"}:
            continue
        scanned += 1
        assert scan_text(path.read_text(encoding="utf-8")) == [], f"секрет у фікстурі {path}"
    assert scanned > 0

    for path in (REPO_ROOT / "frontend" / "fixtures").glob("*.json"):
        assert scan_text(path.read_text(encoding="utf-8")) == [], f"секрет у фікстурі {path}"

    env_example = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    filled = [
        line
        for line in env_example.splitlines()
        if "=" in line
        and not line.strip().startswith("#")
        and line.split("=", 1)[1].strip()
        and not line.startswith("AISEOTOOLS_SOURCE_PATH")
    ]
    assert filled == [], f".env.example має містити лише імена змінних, а не значення: {filled}"


def test_registered_fake_adapters_are_the_only_ones_used_in_ci(monkeypatch):
    """CI не має права смикати справжніх провайдерів (PLAN §15 live)."""
    reset_registry(monkeypatch)
    adapters = register_fakes()
    assert {type(a).__name__ for a in adapters.values()} == {"FakeAdapter"}
