from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.main import RevalidatingStaticFiles


def test_frontend_code_requires_browser_revalidation(tmp_path):
    (tmp_path / "index.html").write_text("<!doctype html><title>App</title>")
    (tmp_path / "app.js").write_text("export const version = 1;")
    (tmp_path / "app.css").write_text("body { color: white; }")

    app = FastAPI()
    app.mount("/", RevalidatingStaticFiles(directory=tmp_path, html=True))

    with TestClient(app) as client:
        for path in ("/", "/app.js", "/app.css"):
            response = client.get(path)
            assert response.status_code == 200
            assert response.headers["cache-control"] == (
                "no-cache, max-age=0, must-revalidate"
            )

        js_response = client.get("/app.js")
        revalidated = client.get(
            "/app.js", headers={"If-None-Match": js_response.headers["etag"]}
        )
        assert revalidated.status_code == 304
        assert revalidated.headers["cache-control"] == (
            "no-cache, max-age=0, must-revalidate"
        )
