# SPDX-License-Identifier: AGPL-3.0-or-later
"""OAuth 2.1 authorization server: DCR, PKCE, redirect URIs, consent, codes, refresh."""

from __future__ import annotations

import contextlib
from urllib.parse import parse_qs, urlsplit

import pytest
from authkit import (  # noqa: F401
    PUBLIC_URL,
    authorize,
    consent,
    fake_clock,
    login,
    oauth_flow,
    pkce,
    register,
    web,
)

from wwyd.auth import tokens

pytestmark = pytest.mark.anyio

CLAUDE = "https://claude.ai/api/mcp/auth_callback"
CHATGPT = "https://chatgpt.com/connector/oauth/abc123XYZ"


@contextlib.asynccontextmanager
async def _signed_in(web):  # noqa: F811
    web.user()
    async with web.browser() as b:
        assert (await login(b)).status_code == 303
        yield b


async def test_metadata(web):  # noqa: F811
    async with web.browser() as b:
        meta = (await b.get("/.well-known/oauth-authorization-server")).json()
        prm = (await b.get("/.well-known/oauth-protected-resource/mcp")).json()
    assert meta["issuer"] == PUBLIC_URL
    assert meta["authorization_endpoint"] == PUBLIC_URL + "/authorize"
    assert meta["registration_endpoint"] == PUBLIC_URL + "/register"
    assert "none" in meta["token_endpoint_auth_methods_supported"]
    assert meta["scopes_supported"] == ["owner", "read", "respond"]
    assert meta["code_challenge_methods_supported"] == ["S256"]
    assert meta["client_id_metadata_document_supported"] is True
    assert prm["resource"] == PUBLIC_URL + "/mcp"
    assert prm["authorization_servers"] == [PUBLIC_URL]
    assert "offline_access" not in (prm.get("scopes_supported") or [])


async def test_unauthenticated_mcp_gets_401_with_resource_metadata(web):  # noqa: F811
    async with web.browser() as b:
        resp = await b.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert resp.status_code == 401
    assert (
        f'resource_metadata="{PUBLIC_URL}/.well-known/oauth-protected-resource/mcp"'
        in resp.headers["www-authenticate"]
    )


async def test_full_flow_and_mcp(web):  # noqa: F811
    async with _signed_in(web) as b:
        tok = await oauth_flow(b)
    assert tok["token_type"] == "Bearer"
    assert tok["scope"] == "owner"
    assert tok["expires_in"] == 3600
    async with web.mcp(tok["access_token"]) as c:
        assert "add_claims" in {t.name for t in (await c.list_tools()).tools}
    grant = web.conn.execute("SELECT * FROM grants").fetchone()
    assert grant["client_name"] == "Test App" and grant["last_used_at"]
    # Nothing secret is stored in clear.
    dump = "\n".join(web.conn.iterdump())
    for secret in (tok["access_token"], tok["refresh_token"], tok["code"]):
        assert secret not in dump


@pytest.mark.parametrize(
    ("registered", "used"),
    [
        (CLAUDE, CLAUDE),
        (CHATGPT, CHATGPT),
        ("http://localhost:33418/callback", "http://localhost:33418/callback"),
        ("http://localhost:33418/callback", "http://localhost:51234/callback"),
        ("http://127.0.0.1/callback", "http://127.0.0.1:60123/callback"),
        ("http://127.0.0.1:8000/cb", "http://127.0.0.1:9000/cb"),
    ],
)
async def test_redirect_uri_patterns(web, registered, used):  # noqa: F811
    async with _signed_in(web) as b:
        client = await register(b, registered)
        verifier, challenge = pkce()
        resp = await authorize(b, client["client_id"], used, challenge)
        assert resp.status_code == 302 and "/consent?req=" in resp.headers["location"]
        resp = await consent(b, resp.headers["location"])
        assert resp.headers["location"].startswith(used)
        code = parse_qs(urlsplit(resp.headers["location"]).query)["code"][0]
        tok = await b.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": used,
                "client_id": client["client_id"],
                "code_verifier": verifier,
            },
        )
        assert tok.status_code == 200, tok.text


@pytest.mark.parametrize(
    ("registered", "used"),
    [
        (CLAUDE, "https://evil.example/api/mcp/auth_callback"),
        ("http://localhost:3000/callback", "http://localhost:3000/other"),
        ("http://localhost:3000/callback", "http://127.0.0.1:3000/callback"),
        ("https://app.example/cb", "https://app.example:8443/cb"),  # port only free on loopback
    ],
)
async def test_unregistered_redirect_rejected(web, registered, used):  # noqa: F811
    async with web.browser() as b:
        client = await register(b, registered)
        resp = await authorize(b, client["client_id"], used, pkce()[1])
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_request"


async def test_pkce_required_and_s256_only(web):  # noqa: F811
    async with web.browser() as b:
        client = await register(b, CLAUDE)
        cid = client["client_id"]
        missing = await b.get(
            "/authorize",
            params={
                "response_type": "code",
                "client_id": cid,
                "redirect_uri": CLAUDE,
                "state": "s",
            },
        )
        plain = await authorize(b, cid, CLAUDE, "x" * 43, method="plain")
    for resp in (missing, plain):
        assert resp.status_code == 302
        q = parse_qs(urlsplit(resp.headers["location"]).query)
        assert q["error"] == ["invalid_request"]


async def _code(b, cid: str, challenge: str, redirect: str = CLAUDE) -> str:
    resp = await authorize(b, cid, redirect, challenge)
    resp = await consent(b, resp.headers["location"])
    return parse_qs(urlsplit(resp.headers["location"]).query)["code"][0]


async def _exchange(b, cid: str, code: str, verifier: str, redirect: str = CLAUDE):  # noqa: ANN202
    return await b.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect,
            "client_id": cid,
            "code_verifier": verifier,
        },
    )


async def test_wrong_verifier_and_code_single_use(web):  # noqa: F811
    async with _signed_in(web) as b:
        cid = (await register(b, CLAUDE))["client_id"]
        verifier, challenge = pkce()
        code = await _code(b, cid, challenge)
        bad = await _exchange(b, cid, code, pkce()[0])
        assert bad.status_code == 400 and bad.json()["error"] == "invalid_grant"
        ok = await _exchange(b, cid, code, verifier)
        assert ok.status_code == 200
        again = await _exchange(b, cid, code, verifier)
        assert again.status_code == 400 and again.json()["error"] == "invalid_grant"
    # Code replay revokes what the code produced.
    assert web.conn.execute("SELECT revoked_at FROM grants").fetchone()["revoked_at"]


async def test_code_expires(web, fake_clock):  # noqa: F811
    async with _signed_in(web) as b:
        cid = (await register(b, CLAUDE))["client_id"]
        verifier, challenge = pkce()
        code = await _code(b, cid, challenge)
        fake_clock.advance(minutes=11)
        resp = await _exchange(b, cid, code, verifier)
    assert resp.status_code == 400 and resp.json()["error"] == "invalid_grant"


async def test_code_bound_to_client(web):  # noqa: F811
    async with _signed_in(web) as b:
        cid = (await register(b, CLAUDE))["client_id"]
        other = (await register(b, CLAUDE))["client_id"]
        verifier, challenge = pkce()
        code = await _code(b, cid, challenge)
        resp = await _exchange(b, other, code, verifier)
    assert resp.json()["error"] == "invalid_grant"


async def _refresh(b, cid: str, rt: str):  # noqa: ANN202
    return await b.post(
        "/token", data={"grant_type": "refresh_token", "refresh_token": rt, "client_id": cid}
    )


async def test_refresh_rotation_and_reuse_detection(web):  # noqa: F811
    async with _signed_in(web) as b:
        tok = await oauth_flow(b)
        cid = tok["client_id"]
        r1 = await _refresh(b, cid, tok["refresh_token"])
        assert r1.status_code == 200, r1.text
        new = r1.json()
        assert new["refresh_token"] != tok["refresh_token"]
        assert new["access_token"] != tok["access_token"]
        assert new["scope"] == "owner"
        async with web.mcp(new["access_token"]) as c:
            await c.list_tools()
        # Reusing the rotated refresh token revokes the whole grant.
        reuse = await _refresh(b, cid, tok["refresh_token"])
        assert reuse.status_code == 400 and reuse.json()["error"] == "invalid_grant"
        after = await _refresh(b, cid, new["refresh_token"])
        assert after.json()["error"] == "invalid_grant"
        mcp = await b.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers={"Authorization": f"Bearer {new['access_token']}"},
        )
        assert mcp.status_code == 401


async def test_bad_refresh_token(web):  # noqa: F811
    async with _signed_in(web) as b:
        tok = await oauth_flow(b)
        resp = await _refresh(b, tok["client_id"], "not-a-real-token")
    assert resp.status_code == 400 and resp.json()["error"] == "invalid_grant"


async def test_refresh_expires(web, fake_clock):  # noqa: F811
    async with _signed_in(web) as b:
        tok = await oauth_flow(b)
        fake_clock.advance(days=31)
        resp = await _refresh(b, tok["client_id"], tok["refresh_token"])
    assert resp.json()["error"] == "invalid_grant"


async def test_access_token_expires(web, fake_clock):  # noqa: F811
    async with _signed_in(web) as b:
        tok = await oauth_flow(b)
    assert tokens.verify_access_token(web.conn, tok["access_token"]) is not None
    fake_clock.advance(minutes=61)
    assert tokens.verify_access_token(web.conn, tok["access_token"]) is None


async def test_consent_scope_differs_from_request(web):  # noqa: F811
    web.user()
    pid = web.conn.execute("SELECT id FROM personas").fetchone()["id"]
    names = [
        r["name"]
        for r in web.conn.execute("SELECT name FROM audiences WHERE persona_id = ?", (pid,))
    ]
    assert names, "seeded persona has audiences"
    async with web.browser() as b:
        await login(b)
        client = await register(b, CLAUDE, scope="owner")
        verifier, challenge = pkce()
        resp = await authorize(b, client["client_id"], CLAUDE, challenge, scope="owner")
        page = await b.get(resp.headers["location"])
        assert 'value="owner" checked' in page.text  # requested scope is preselected
        resp = await consent(b, resp.headers["location"], scope="respond", audiences=names[:1])
        code = parse_qs(urlsplit(resp.headers["location"]).query)["code"][0]
        tok = (await _exchange(b, client["client_id"], code, verifier)).json()
    assert tok["scope"] == f"respond:{names[0]}"


async def test_requested_respond_scope_preselected(web):  # noqa: F811
    web.user()
    names = [r["name"] for r in web.conn.execute("SELECT name FROM audiences ORDER BY name")]
    async with web.browser() as b:
        await login(b)
        client = await register(b, CLAUDE)
        resp = await authorize(
            b, client["client_id"], CLAUDE, pkce()[1], scope=f"respond:{names[0]} offline_access"
        )
        page = await b.get(resp.headers["location"])
    assert 'value="respond" checked' in page.text
    assert f'value="{names[0]}" checked' in page.text


async def test_respond_needs_known_audience(web):  # noqa: F811
    async with _signed_in(web) as b:
        client = await register(b, CLAUDE)
        resp = await authorize(b, client["client_id"], CLAUDE, pkce()[1])
        url = resp.headers["location"]
        none = await consent(b, url, scope="respond")
        unknown = await consent(b, url, scope="respond", audiences=["nope"])
    assert none.status_code == 400 and unknown.status_code == 400


async def test_deny(web):  # noqa: F811
    async with _signed_in(web) as b:
        client = await register(b, CLAUDE)
        resp = await authorize(b, client["client_id"], CLAUDE, pkce()[1])
        resp = await consent(b, resp.headers["location"], decision="deny")
    q = parse_qs(urlsplit(resp.headers["location"]).query)
    assert q["error"] == ["access_denied"] and q["state"] == ["st4te"]
    assert web.conn.execute("SELECT COUNT(*) FROM grants").fetchone()[0] == 0


async def test_consent_requires_sign_in_and_shows_client(web):  # noqa: F811
    web.user()
    async with web.browser() as b:
        client = await register(b, CHATGPT, client_name="ChatGPT")
        resp = await authorize(b, client["client_id"], CHATGPT, pkce()[1])
        consent_url = resp.headers["location"]
        resp = await b.get(consent_url)
        assert resp.status_code == 303 and resp.headers["location"].startswith("/login?next=")
        await login(b)
        page = await b.get(consent_url)
    assert "ChatGPT" in page.text and "chatgpt.com" in page.text


async def test_wrong_resource_rejected(web):  # noqa: F811
    async with web.browser() as b:
        client = await register(b, CLAUDE)
        resp = await authorize(
            b, client["client_id"], CLAUDE, pkce()[1], resource="https://other.example/mcp"
        )
    q = parse_qs(urlsplit(resp.headers["location"]).query)
    assert q["error"] == ["invalid_target"]


async def test_registration_rejects_unknown_scope(web):  # noqa: F811
    async with web.browser() as b:
        resp = await b.post(
            "/register",
            json={
                "redirect_uris": [CLAUDE],
                "token_endpoint_auth_method": "none",
                "scope": "admin",
            },
        )
    assert resp.status_code == 400 and resp.json()["error"] == "invalid_client_metadata"


async def test_revocation_endpoint(web):  # noqa: F811
    async with _signed_in(web) as b:
        tok = await oauth_flow(b)
        resp = await b.post(
            "/revoke",
            data={
                "token": tok["refresh_token"],
                "client_id": tok["client_id"],
                "client_secret": "",
            },
        )
        assert resp.status_code == 200
    assert tokens.verify_access_token(web.conn, tok["access_token"]) is None


async def test_dcr_purge(web, fake_clock):  # noqa: F811
    async with _signed_in(web) as b:
        used = (await oauth_flow(b))["client_id"]
        unused = (await register(b, CLAUDE))["client_id"]
    fake_clock.advance(hours=23)
    assert web.state.provider.purge() == 0
    fake_clock.advance(hours=2)
    assert web.state.provider.purge() == 1
    ids = {r[0] for r in web.conn.execute("SELECT client_id FROM oauth_clients")}
    assert ids == {used}
    assert unused not in ids
