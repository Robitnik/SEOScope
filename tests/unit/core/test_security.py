import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.security import PathTraversalError, configure_security, safe_resolve


def test_safe_resolve_allows_path_within_root(tmp_path):
    root = tmp_path
    (root / "reports").mkdir()
    (root / "reports" / "a.json").write_text("{}")

    resolved = safe_resolve(root, "reports/a.json")

    assert resolved == (root / "reports" / "a.json").resolve()


@pytest.mark.parametrize(
    "relative_path",
    [
        "../escape.json",
        "reports/../../escape.json",
        "/etc/passwd",
    ],
)
def test_safe_resolve_blocks_traversal(tmp_path, relative_path):
    with pytest.raises(PathTraversalError):
        safe_resolve(tmp_path, relative_path)


def test_security_headers_present_on_response():
    app = FastAPI()
    configure_security(app, Settings(_env_file=None))

    @app.get("/ping")
    async def ping():
        return {"ok": True}

    client = TestClient(app)
    response = client.get("/ping")

    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    # Скрипти/стилі/fetch лишаються під `'self'`; лише `img-src` розширений
    # (task-27: прев'ю картинок клієнта в інструменті Image Alt Text).
    policy = response.headers["content-security-policy"]
    assert policy.startswith("default-src 'self'")
    assert "img-src 'self' data: https: http:" in policy
    assert "script-src" not in policy
