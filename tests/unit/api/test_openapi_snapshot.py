"""OpenAPI schema snapshot test (PLAN §10 DoD: "OpenAPI snapshot стабільний").

Any route/DTO shape change under `app/api/**` shows up as a diff against
`data/openapi_snapshot.json` instead of silently rippling into the frontend
(task-12) or integration tests (task-13).

To intentionally update the snapshot after a reviewed API change:
    docker compose run --rm api poetry run python -m tests.unit.api.test_openapi_snapshot
"""

from __future__ import annotations

import json
from pathlib import Path

SNAPSHOT_PATH = Path(__file__).parent / "data" / "openapi_snapshot.json"


def _build_schema() -> dict:
    from app.main import app

    return app.openapi()


def test_openapi_schema_matches_snapshot(client):
    current = client.get("/openapi.json").json()
    recorded = json.loads(SNAPSHOT_PATH.read_text())

    assert current == recorded, (
        "OpenAPI shape changed — this is the contract task-12/task-13 code "
        "against (CLAUDE.md). If reviewed and intentional, regenerate "
        "data/openapi_snapshot.json (see module docstring)."
    )


if __name__ == "__main__":
    SNAPSHOT_PATH.write_text(json.dumps(_build_schema(), indent=2, sort_keys=True) + "\n")
    print(f"wrote {SNAPSHOT_PATH}")
