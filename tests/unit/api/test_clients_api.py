from __future__ import annotations

from app.db.repositories.clients import ClientRepository
from app.schemas.enums import Provider, SourceMode


def _make_png(width: int = 64, height: int = 64) -> bytes:
    sig = b"\x89PNG\r\n\x1a\n"
    chunk_len = (13).to_bytes(4, "big")
    chunk_type = b"IHDR"
    dims = width.to_bytes(4, "big") + height.to_bytes(4, "big")
    # depth/color/comp/filter/interlace + fake CRC
    rest = bytes([8, 2, 0, 0, 0]) + b"\x00\x00\x00\x00"
    return sig + chunk_len + chunk_type + dims + rest


def _make_jpeg(width: int = 300, height: int = 150) -> bytes:
    data = bytearray(20)
    data[0:2] = b"\xff\xd8"
    data[2:4] = b"\xff\xc0"
    data[4:6] = (17).to_bytes(2, "big")
    data[6] = 8
    data[7:9] = height.to_bytes(2, "big")
    data[9:11] = width.to_bytes(2, "big")
    data[11] = 3
    return bytes(data)


def _create_client(db, *, slug: str = "acme") -> str:
    client = ClientRepository(db).create(
        name="Acme", slug=slug, domain=f"{slug}.com", timezone="America/New_York"
    )
    db.commit()
    return client.id


def test_create_and_get_client(client):
    response = client.post(
        "/api/v1/clients",
        json={
            "name": "Acme",
            "slug": "acme",
            "domain": "acme.com",
            "timezone": "America/New_York",
        },
    )
    assert response.status_code == 201
    body = response.json()
    assert body["slug"] == "acme"
    assert body["logo_path"] is None

    got = client.get(f"/api/v1/clients/{body['id']}")
    assert got.status_code == 200
    assert got.json()["domain"] == "acme.com"


def test_create_client_duplicate_slug_is_409(client):
    payload = {
        "name": "Acme",
        "slug": "acme",
        "domain": "acme.com",
        "timezone": "America/New_York",
    }
    assert client.post("/api/v1/clients", json=payload).status_code == 201
    dup = client.post(
        "/api/v1/clients",
        json={**payload, "domain": "acme2.com"},
    )
    assert dup.status_code == 409
    assert "error_code" in dup.json()["detail"]


def test_get_missing_client_is_404_and_safe(client):
    response = client.get("/api/v1/clients/does-not-exist")
    assert response.status_code == 404
    body = response.json()["detail"]
    assert body["error_code"] == "client_not_found"
    assert "traceback" not in str(body).lower()


def test_patch_client_partial_update(client, db):
    client_id = _create_client(db)
    response = client.patch(f"/api/v1/clients/{client_id}", json={"market": "US"})
    assert response.status_code == 200
    assert response.json()["market"] == "US"
    # Untouched fields survive the partial update.
    assert response.json()["name"] == "Acme"


def test_upsert_integration_and_read_back(client, db):
    client_id = _create_client(db)
    response = client.put(
        f"/api/v1/clients/{client_id}/integrations/{Provider.gsc.value}",
        json={
            "enabled": True,
            "source_mode": SourceMode.api.value,
            "integration_config": {"property_id": "sc-domain:acme.com"},
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["provider"] == "gsc"
    assert body["integration_config"] == {"property_id": "sc-domain:acme.com"}
    # No secret-shaped keys ever accepted/echoed back.
    assert "api_key" not in body["integration_config"]


def test_preflight_not_configured_never_crashes(client, db):
    client_id = _create_client(db)
    response = client.post(
        f"/api/v1/clients/{client_id}/integrations/{Provider.ahrefs.value}/preflight"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["reason_code"] == "not_configured"


def test_preflight_missing_client_is_404(client):
    response = client.post(f"/api/v1/clients/nope/integrations/{Provider.ahrefs.value}/preflight")
    assert response.status_code == 404


def test_logo_upload_rejects_bad_signature(client, db):
    client_id = _create_client(db)
    response = client.post(
        f"/api/v1/clients/{client_id}/logo",
        content=b"not-an-image",
        headers={"Content-Type": "image/png"},
    )
    assert response.status_code == 400
    assert response.json()["detail"]["error_code"] == "invalid_logo"


def test_logo_upload_rejects_undersized_dimensions(client, db):
    client_id = _create_client(db)
    response = client.post(
        f"/api/v1/clients/{client_id}/logo",
        content=_make_png(width=4, height=4),
        headers={"Content-Type": "image/png"},
    )
    assert response.status_code == 400
    assert response.json()["detail"]["error_code"] == "invalid_logo_dimensions"


def test_logo_upload_png_success_and_download(client, db):
    client_id = _create_client(db)
    upload = client.post(
        f"/api/v1/clients/{client_id}/logo",
        content=_make_png(),
        headers={"Content-Type": "image/png"},
    )
    assert upload.status_code == 200
    logo_path = upload.json()["logo_path"]
    assert logo_path == f"/api/v1/clients/{client_id}/logo"

    download = client.get(logo_path)
    assert download.status_code == 200
    assert download.headers["content-type"] == "image/png"


def test_logo_upload_jpeg_success(client, db):
    client_id = _create_client(db)
    upload = client.post(
        f"/api/v1/clients/{client_id}/logo",
        content=_make_jpeg(),
        headers={"Content-Type": "image/jpeg"},
    )
    assert upload.status_code == 200


def test_logo_upload_too_large_is_rejected(client, db):
    client_id = _create_client(db)
    huge = _make_png() + b"\x00" * (3 * 1024 * 1024)
    response = client.post(
        f"/api/v1/clients/{client_id}/logo",
        content=huge,
        headers={"Content-Type": "image/png"},
    )
    assert response.status_code == 400
    assert response.json()["detail"]["error_code"] == "logo_too_large"


def test_get_logo_missing_is_404(client, db):
    client_id = _create_client(db)
    response = client.get(f"/api/v1/clients/{client_id}/logo")
    assert response.status_code == 404
