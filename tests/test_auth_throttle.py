# SPDX-License-Identifier: AGPL-3.0-or-later
"""Brute-force and abuse limits on the whole auth surface."""

from __future__ import annotations

import pytest
from authkit import (  # noqa: F401
    PASSWORD,
    authorize,
    csrf_of,
    fake_clock,
    login,
    make_web,
    oauth_flow,
    pkce,
    register,
    web,
)

from wwyd.auth import accounts, tokens
from wwyd.auth.scopes import parse_scope
from wwyd.auth.throttle import Throttle, client_ip

pytestmark = pytest.mark.anyio


def test_throttle_window_and_lockout(fake_clock):  # noqa: F811
    t = Throttle("x", 3, 60)
    for _ in range(3):
        t.check("k")
        t.hit("k")
    assert t.retry_after("k") == 60
    assert t.retry_after("other") == 0
    fake_clock.advance(seconds=61)
    assert t.retry_after("k") == 0
    locked = Throttle("y", 2, 60, lockout=900)
    locked.hit("k")
    fake_clock.advance(seconds=50)
    locked.hit("k")
    fake_clock.advance(seconds=100)  # window passed, lockout still on
    assert locked.retry_after("k") > 0
    fake_clock.advance(seconds=900)
    assert locked.retry_after("k") == 0


def test_client_ip_trusted_proxies():
    assert client_ip("203.0.113.1", "198.51.100.7", []) == "203.0.113.1"
    assert client_ip("203.0.113.1", "198.51.100.7", ["127.0.0.1"]) == "203.0.113.1"
    assert client_ip("127.0.0.1", "198.51.100.7", ["127.0.0.1"]) == "198.51.100.7"
    # Spoofed left-most hops are ignored: the right-most untrusted hop wins.
    assert (
        client_ip("10.0.0.2", "6.6.6.6, 198.51.100.7, 10.0.0.3", ["10.0.0.0/8"]) == "198.51.100.7"
    )


async def test_login_locked_per_login(web, fake_clock):  # noqa: F811
    web.user()
    for i in range(10):
        async with web.browser(ip=f"198.51.100.{i}") as b:
            assert (await login(b, password="wrong password!")).status_code == 401
    async with web.browser(ip="198.51.100.200") as b:
        locked = await login(b)  # right password, still locked
        assert locked.status_code == 429 and int(locked.headers["retry-after"]) > 0
        ghost = await login(b, "ghost@example.com", "x" * 12)
        assert ghost.status_code == 401  # other logins unaffected; same generic error
    fake_clock.advance(minutes=16)
    async with web.browser(ip="198.51.100.200") as b:
        assert (await login(b)).status_code == 303


async def test_login_limited_per_ip(web, fake_clock):  # noqa: F811
    web.user()
    async with web.browser(ip="198.51.100.9") as b:
        for i in range(10):
            assert (await login(b, f"u{i}@example.com", "wrong password")).status_code == 401
        assert (await login(b)).status_code == 429
    async with web.browser(ip="198.51.100.10") as b:
        assert (await login(b)).status_code == 303
    fake_clock.advance(minutes=16)
    async with web.browser(ip="198.51.100.9") as b:
        assert (await login(b)).status_code == 303


async def test_password_change_limited(web, fake_clock):  # noqa: F811
    web.user()
    async with web.browser() as b:
        await login(b)
        csrf = csrf_of((await b.get("/account")).text)
        for _ in range(5):
            r = await b.post(
                "/account/password", data={"csrf": csrf, "current": "guess guess", "new": "x" * 12}
            )
            assert r.status_code == 400
        r = await b.post(
            "/account/password", data={"csrf": csrf, "current": PASSWORD, "new": "y" * 12}
        )
        assert r.status_code == 429 and r.headers["retry-after"]
        fake_clock.advance(minutes=16)
        r = await b.post(
            "/account/password", data={"csrf": csrf, "current": PASSWORD, "new": "y" * 12}
        )
        assert r.status_code == 200


async def test_one_time_link_guessing_limited(web, fake_clock):  # noqa: F811
    token = accounts.create_link(web.conn, "invite")
    async with web.browser(ip="198.51.100.20") as b:
        for i in range(10):
            assert (await b.get("/invite", params={"t": f"guess{i}"})).status_code == 400
        r = await b.get("/invite", params={"t": token})
        assert r.status_code == 429 and r.headers["retry-after"]
    async with web.browser(ip="198.51.100.21") as b:
        assert (await b.get("/invite", params={"t": token})).status_code == 200
    fake_clock.advance(minutes=16)
    async with web.browser(ip="198.51.100.20") as b:
        assert (await b.get("/invite", params={"t": token})).status_code == 200


async def _bad_token(b, client_id: str):  # noqa: ANN202
    return await b.post(
        "/token",
        data={"grant_type": "refresh_token", "refresh_token": "nope", "client_id": client_id},
    )


async def test_token_endpoint_limited_per_ip_and_client(web, fake_clock):  # noqa: F811
    async with web.browser(ip="198.51.100.30") as b:
        cid = (await register(b))["client_id"]
        for _ in range(20):
            assert (await _bad_token(b, cid)).status_code == 400
        r = await _bad_token(b, cid)
        assert (
            r.status_code == 429
            and r.json()["error"] == "rate_limited"
            and r.headers["retry-after"]
        )
    # Same client from elsewhere: per-client limit.
    async with web.browser(ip="198.51.100.31") as b:
        assert (await _bad_token(b, cid)).status_code == 429
        other = (await register(b))["client_id"]
        assert (await _bad_token(b, other)).status_code == 400
    fake_clock.advance(minutes=11)
    async with web.browser(ip="198.51.100.30") as b:
        assert (await _bad_token(b, cid)).status_code == 400


async def test_successful_token_requests_not_counted(web):  # noqa: F811
    web.user()
    async with web.browser() as b:
        await login(b)
        tok = await oauth_flow(b)
        rt = tok["refresh_token"]
        for _ in range(25):
            r = await b.post(
                "/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": rt,
                    "client_id": tok["client_id"],
                },
            )
            assert r.status_code == 200
            rt = r.json()["refresh_token"]


async def test_register_limited_per_ip_and_globally(web, fake_clock):  # noqa: F811
    async with web.browser(ip="198.51.100.40") as b:
        for _ in range(20):
            await register(b)
        r = await b.post(
            "/register", json={"redirect_uris": ["https://claude.ai/api/mcp/auth_callback"]}
        )
        assert r.status_code == 429 and r.headers["retry-after"]
    web.state.throttles.register_global.limit = 21
    async with web.browser(ip="198.51.100.41") as b:
        await register(b)
        r = await b.post(
            "/register", json={"redirect_uris": ["https://claude.ai/api/mcp/auth_callback"]}
        )
        assert r.status_code == 429  # global cap reached
    fake_clock.advance(hours=1, seconds=1)
    async with web.browser(ip="198.51.100.40") as b:
        await register(b)


async def test_authorize_rate_limited(web, fake_clock):  # noqa: F811
    async with web.browser(ip="198.51.100.50") as b:
        cid = (await register(b))["client_id"]
        for _ in range(60):
            await authorize(b, cid, "https://claude.ai/api/mcp/auth_callback", pkce()[1])
        r = await authorize(b, cid, "https://claude.ai/api/mcp/auth_callback", pkce()[1])
        assert r.status_code == 429
        fake_clock.advance(seconds=61)
        r = await authorize(b, cid, "https://claude.ai/api/mcp/auth_callback", pkce()[1])
        assert r.status_code == 302


async def _mcp(b, token: str):  # noqa: ANN202
    return await b.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/event-stream",
        },
    )


async def test_bearer_failures_limited_per_ip(web, fake_clock):  # noqa: F811
    user = web.user()
    _, key = tokens.create_api_key(web.conn, user.id, "k", parse_scope("owner"))
    async with web.browser(ip="198.51.100.60") as b:
        for i in range(30):
            assert (await _mcp(b, f"bogus-{i}")).status_code == 401
        r = await _mcp(b, key)
        assert r.status_code == 429 and r.headers["retry-after"]
    async with web.browser(ip="198.51.100.61") as b:
        assert (await _mcp(b, key)).status_code != 401
    fake_clock.advance(minutes=11)
    async with web.browser(ip="198.51.100.60") as b:
        assert (await _mcp(b, key)).status_code not in (401, 429)


async def test_forwarded_for_ignored_from_untrusted_peer(web):  # noqa: F811
    async with web.browser(ip="198.51.100.70") as b:
        for i in range(30):
            await b.post(
                "/mcp",
                json={},
                headers={"Authorization": f"Bearer bogus-{i}", "X-Forwarded-For": f"192.0.2.{i}"},
            )
        assert (await _mcp(b, "bogus")).status_code == 429  # counted against the peer


async def test_forwarded_for_honoured_from_trusted_proxy():
    w, stack = await make_web(trusted_proxies=["127.0.0.1"])
    async with stack:
        async with w.browser(ip="127.0.0.1") as b:
            for _ in range(30):
                await b.post(
                    "/mcp",
                    json={},
                    headers={"Authorization": "Bearer bogus", "X-Forwarded-For": "192.0.2.1"},
                )
            blocked = await b.post(
                "/mcp",
                json={},
                headers={"Authorization": "Bearer bogus", "X-Forwarded-For": "192.0.2.1"},
            )
            other = await b.post(
                "/mcp",
                json={},
                headers={"Authorization": "Bearer bogus", "X-Forwarded-For": "192.0.2.2"},
            )
        assert blocked.status_code == 429 and other.status_code == 401
