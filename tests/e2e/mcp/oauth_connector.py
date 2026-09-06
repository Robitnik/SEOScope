#!/usr/bin/env python3
"""Живий наскрізний прогін OAuth-конектора (task-78).

Не юніт-тест і не частина `pytest` за замовчуванням: він ходить у РЕАЛЬНИЙ
піднятий стек через публічний домен, бо саме там живуть речі, яких у
процесі не видно — тунель, заголовки проксі, `AuthMiddleware` без
localhost-байпасу, і те, що Claude бачить своїм HTTP-клієнтом.

Проходить увесь ланцюг рівно так, як його проходить claude.ai:
DCR-реєстрація -> /authorize -> екран згоди -> обмін коду з PKCE ->
initialize -> tools/list -> tools/call -> ротація refresh, і окремо
перевіряє, що код і refresh спрацьовують РІВНО один раз, а анонім
отримує 401.

Запуск (у контейнері, з одноразовим акаунтом):

    docker compose exec api poetry run yovth-report user-create \
        --email qa-oauth@example.com --name "QA OAuth" --role member
    docker compose exec -e QA_EMAIL=qa-oauth@example.com \
        -e QA_PASSWORD=<із виводу вище> api \
        poetry run python tests/e2e/mcp/oauth_connector.py
    docker compose exec api poetry run yovth-report user-deactivate \
        --email qa-oauth@example.com
"""
import base64
import hashlib
import os
import secrets
import urllib.parse

import httpx2 as httpx

BASE = os.environ.get("BASE_URL", "https://seo.theyovth.space")
EMAIL = os.environ.get("QA_EMAIL", "qa-oauth@example.com")
PASSWORD = os.environ["QA_PASSWORD"]

verifier = secrets.token_urlsafe(48)
_digest = hashlib.sha256(verifier.encode()).digest()
challenge = base64.urlsafe_b64encode(_digest).decode().rstrip("=")

with httpx.Client(follow_redirects=False, timeout=30) as c:
    # 1. DCR
    r = c.post(f"{BASE}/register", json={
        "client_name": "Claude", "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
        "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
        "token_endpoint_auth_method": "none", "scope": "yovth:use",
    })
    print("register:", r.status_code)
    assert r.status_code in (200, 201), r.text
    client_id = r.json()["client_id"]
    print("  client_id:", client_id, "secret:", r.json().get("client_secret"))

    # 2. /authorize -> редірект на наш екран згоди
    q = urllib.parse.urlencode({
        "response_type": "code", "client_id": client_id,
        "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
        "code_challenge": challenge, "code_challenge_method": "S256",
        "state": "xyz123", "scope": "yovth:use", "resource": f"{BASE}/mcp",
    })
    r = c.get(f"{BASE}/authorize?{q}")
    print("authorize:", r.status_code, "->", r.headers.get("location"))
    assert r.status_code in (302, 307), r.text
    loc = r.headers["location"]
    assert "#/oauth/consent?request=" in loc, loc
    request_id = loc.split("request=")[1]

    # 3. логін людини + екран згоди
    r = c.post(f"{BASE}/api/v1/auth/login", json={"email": EMAIL, "password": PASSWORD})
    print("login:", r.status_code)
    assert r.status_code == 200, r.text
    csrf = c.cookies.get("yovth_csrf") or ""
    hdr = {"X-CSRF-Token": csrf} if csrf else {}

    r = c.get(f"{BASE}/api/v1/oauth/pending/{request_id}", headers=hdr)
    print("pending:", r.status_code, r.text[:160])
    assert r.status_code == 200, r.text

    r = c.post(f"{BASE}/api/v1/oauth/consent", json={"request_id": request_id}, headers=hdr)
    print("consent:", r.status_code, r.text[:160])
    assert r.status_code == 200, r.text
    redirect_to = r.json()["redirect_to"]
    parsed = urllib.parse.parse_qs(urllib.parse.urlparse(redirect_to).query)
    assert parsed["state"] == ["xyz123"], parsed
    code = parsed["code"][0]

    # 4. обмін коду (public client + PKCE)
    r = c.post(f"{BASE}/token", data={
        "grant_type": "authorization_code", "code": code, "client_id": client_id,
        "redirect_uri": "https://claude.ai/api/mcp/auth_callback", "code_verifier": verifier,
        "resource": f"{BASE}/mcp",
    }, headers={"Content-Type": "application/x-www-form-urlencoded"})
    print("token:", r.status_code, r.text[:200])
    assert r.status_code == 200, r.text
    tok = r.json()
    access, refresh = tok["access_token"], tok["refresh_token"]

    # 5. код одноразовий
    r2 = c.post(f"{BASE}/token", data={
        "grant_type": "authorization_code", "code": code, "client_id": client_id,
        "redirect_uri": "https://claude.ai/api/mcp/auth_callback", "code_verifier": verifier,
    }, headers={"Content-Type": "application/x-www-form-urlencoded"})
    print("token replay:", r2.status_code, "(має бути 400)")
    assert r2.status_code == 400, r2.text

    # 6. MCP initialize + tools/list із цим токеном
    h = {"Authorization": f"Bearer {access}", "Content-Type": "application/json",
         "Accept": "application/json, text/event-stream"}
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "qa", "version": "1"}}}
    r = c.post(f"{BASE}/mcp", json=init, headers=h)
    print("mcp initialize:", r.status_code)
    assert r.status_code == 200, r.text[:400]
    sid = r.headers.get("mcp-session-id")
    h["mcp-session-id"] = sid
    c.post(f"{BASE}/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=h)
    r = c.post(f"{BASE}/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, headers=h)
    body = r.text
    names = [n.split('"')[0] for n in body.split('"name":"')[1:]]
    print("mcp tools/list:", r.status_code, "tools:", len(names), names[:4])
    assert r.status_code == 200 and len(names) >= 10, body[:400]

    # 7. без токена — 401
    r = c.post(f"{BASE}/mcp", json=init, headers={"Content-Type": "application/json",
                                                 "Accept": "application/json, text/event-stream"})
    print("mcp anon:", r.status_code, r.headers.get("www-authenticate", "")[:60])
    assert r.status_code == 401

    # 8. refresh ротується
    r = c.post(f"{BASE}/token", data={"grant_type": "refresh_token", "refresh_token": refresh,
                                      "client_id": client_id},
               headers={"Content-Type": "application/x-www-form-urlencoded"})
    print("refresh:", r.status_code)
    assert r.status_code == 200, r.text
    assert r.json()["refresh_token"] != refresh, "refresh має ротуватись"
    r = c.post(f"{BASE}/token", data={"grant_type": "refresh_token", "refresh_token": refresh,
                                      "client_id": client_id},
               headers={"Content-Type": "application/x-www-form-urlencoded"})
    print("old refresh replay:", r.status_code, "(має бути 400)")
    assert r.status_code == 400

print("\nOAUTH FLOW OK")

# 9. Реальний виклик тула — має відпрацювати від імені людини, не з .env
with httpx.Client(follow_redirects=False, timeout=60) as c:
    h = {"Authorization": f"Bearer {access}", "Content-Type": "application/json",
         "Accept": "application/json, text/event-stream"}
    r = c.post(f"{BASE}/mcp", json=init, headers=h)
    h["mcp-session-id"] = r.headers.get("mcp-session-id")
    c.post(f"{BASE}/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=h)
    r = c.post(f"{BASE}/mcp", json={"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                    "params": {"name": "list_clients", "arguments": {}}}, headers=h)
    print("tools/call list_clients:", r.status_code)
    txt = r.text
    assert r.status_code == 200, txt[:400]
    assert '"isError":true' not in txt.replace(" ", ""), txt[:600]
    print("  payload head:", txt[txt.find('"text"'):][:220])
print("\nTOOL CALL OK")
