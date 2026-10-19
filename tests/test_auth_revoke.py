# SPDX-License-Identifier: AGPL-3.0-or-later
"""RFC 7009 /revoke (our handler), API key self-revocation, insecure redirect URIs."""

from __future__ import annotations

import httpx
import pytest
from authkit import authorize, login, make_web, oauth_flow, pkce, register, web  # noqa: F401
from test_auth_cimd import CID, Server, _doc

from wwyd.auth import tokens
from wwyd.auth.scopes import parse_scope

pytestmark = pytest.mark.anyio


async def _ping(w, token: str) -> int:  # noqa: ANN001
    async with w.browser() as b:
        resp = await b.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json, text/event-stream",
            },
        )
    return resp.status_code


async def test_public_client_revokes_with_client_id_only(web):  # noqa: F811
    web.user()
    async with web.browser() as b:
        await login(b)
        tok = await oauth_flow(b)
    assert await _ping(web, tok["access_token"]) != 401
    async with web.browser() as b:
        resp = await b.post(
            "/revoke", data={"token": tok["access_token"], "client_id": tok["client_id"]}
        )
    assert resp.status_code == 200, resp.text
    assert await _ping(web, tok["access_token"]) == 401


async def test_public_client_revokes_refresh_token(web):  # noqa: F811
    web.user()
    async with web.browser() as b:
        await login(b)
        tok = await oauth_flow(b)
        resp = await b.post(
            "/revoke",
            data={
                "token": tok["refresh_token"],
                "token_type_hint": "refresh_token",
                "client_id": tok["client_id"],
            },
        )
    assert resp.status_code == 200
    assert await _ping(web, tok["access_token"]) == 401


async def test_confidential_client_without_secret_is_401(web):  # noqa: F811
    async with web.browser() as b:
        client = await register(b, token_endpoint_auth_method="client_secret_post")
        assert client.get("client_secret")
        resp = await b.post("/revoke", data={"token": "whatever", "client_id": client["client_id"]})
        assert resp.status_code == 401
        assert resp.json()["error"] == "invalid_client"
        ok = await b.post(
            "/revoke",
            data={
                "token": "whatever",
                "client_id": client["client_id"],
                "client_secret": client["client_secret"],
            },
        )
        assert ok.status_code == 200


async def test_token_of_another_client_is_not_revoked(web):  # noqa: F811
    web.user()
    async with web.browser() as b:
        await login(b)
        tok = await oauth_flow(b)
        other = await register(b)
        resp = await b.post(
            "/revoke", data={"token": tok["access_token"], "client_id": other["client_id"]}
        )
        unknown = await b.post("/revoke", data={"token": "nope", "client_id": other["client_id"]})
    assert resp.status_code == 200
    assert unknown.status_code == 200
    assert await _ping(web, tok["access_token"]) != 401


async def test_api_key_revokes_itself(web):  # noqa: F811
    user = web.user()
    key_id, key = tokens.create_api_key(web.conn, user.id, "k", parse_scope("owner"))
    assert await _ping(web, key) != 401
    async with web.browser() as b:
        resp = await b.post("/revoke", data={"token": key})
    assert resp.status_code == 200
    assert await _ping(web, key) == 401


async def test_garbage_api_key_revokes_nothing(web):  # noqa: F811
    user = web.user()
    _, key = tokens.create_api_key(web.conn, user.id, "k", parse_scope("owner"))
    prefix = key.split("_")[1]
    async with web.browser() as b:
        for bad in (f"wwyd_{prefix}_{'x' * 40}", "wwyd_garbage", "wwyd_abcdefgh_" + "y" * 30):
            resp = await b.post("/revoke", data={"token": bad, "client_id": "unknown"})
            assert resp.status_code == 200
    assert await _ping(web, key) != 401


# --- insecure redirect URIs -------------------------------------------------------------

LAN = "http://192.168.1.20:8080/callback"


async def _register_status(w, uri: str) -> httpx.Response:  # noqa: ANN001
    async with w.browser() as b:
        return await b.post(
            "/register",
            json={
                "redirect_uris": [uri],
                "token_endpoint_auth_method": "none",
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
            },
        )


async def test_lan_http_redirect_allowed_by_default_with_warning(web):  # noqa: F811
    web.user()
    assert (await _register_status(web, LAN)).status_code == 201
    async with web.browser() as b:
        await login(b)
        client = await register(b, LAN)
        _, challenge = pkce()
        resp = await authorize(b, client["client_id"], LAN, challenge)
        page = await b.get(resp.headers["location"])
    assert "unencrypted http" in page.text
    assert "192.168.1.20:8080" in page.text


async def test_https_redirect_has_no_warning(web):  # noqa: F811
    web.user()
    async with web.browser() as b:
        await login(b)
        uri = "https://claude.ai/api/mcp/auth_callback"
        client = await register(b, uri)
        _, challenge = pkce()
        resp = await authorize(b, client["client_id"], uri, challenge)
        page = await b.get(resp.headers["location"])
    assert "unencrypted http" not in page.text


@pytest.mark.parametrize(
    ("uri", "ok"),
    [
        (LAN, False),
        ("http://example.com/cb", False),
        ("http://127.0.0.1:3000/cb", True),
        ("http://localhost/cb", True),
        ("http://[::1]:9/cb", True),
        ("https://example.com/cb", True),
    ],
)
async def test_strict_mode_registration(uri, ok):
    w, stack = await make_web(allow_insecure_redirects=False)
    async with stack:
        resp = await _register_status(w, uri)
        if ok:
            assert resp.status_code == 201, resp.text
        else:
            assert resp.status_code == 400
            assert resp.json()["error"] == "invalid_redirect_uri"
    w.conn.close()


async def test_strict_mode_rejects_cimd_with_http_redirect():
    srv = Server(doc=_doc(redirect_uris=[LAN]))
    w, stack = await make_web(
        allow_insecure_redirects=False,
        cimd_transport=httpx.MockTransport(srv.handler),
        cimd_resolver=srv.resolve,
    )
    async with stack:
        assert await w.state.provider.get_client(CID) is None
    w.conn.close()


def test_config_flag_from_env(monkeypatch, tmp_path):
    from wwyd.config import ConfigError, load_config

    monkeypatch.setenv("WWYD_DATA_DIR", str(tmp_path))
    assert load_config().allow_insecure_redirects is True
    monkeypatch.setenv("WWYD_AUTH_ALLOW_INSECURE_REDIRECTS", "false")
    assert load_config().allow_insecure_redirects is False
    (tmp_path / "wwyd.toml").write_text("[auth]\nallow_insecure_redirects = false\n")
    monkeypatch.delenv("WWYD_AUTH_ALLOW_INSECURE_REDIRECTS")
    assert load_config().allow_insecure_redirects is False
    (tmp_path / "wwyd.toml").write_text("[auth]\nbogus = 1\n")
    with pytest.raises(ConfigError):
        load_config()
