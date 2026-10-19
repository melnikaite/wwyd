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
from wwyd.auth.throttle import Throttle, client_ip, ip_key

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
    # Every hop trusted: the peer, never the client-controlled left-most hop.
    assert client_ip("10.0.0.2", "10.0.0.9, 10.0.0.3", ["10.0.0.0/8"]) == "10.0.0.2"


def test_ip_key_groups_ipv6_by_64():
    assert ip_key("198.51.100.7") == "198.51.100.7"
    assert ip_key("2001:db8:1:2:aaaa::1") == ip_key("2001:db8:1:2:ffff::9") == "2001:db8:1:2::/64"
    assert ip_key("2001:db8:1:3::1") != ip_key("2001:db8:1:2::1")
    assert ip_key("::ffff:198.51.100.7") == "198.51.100.7"
    assert ip_key("unknown") == "unknown"


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


async def test_token_endpoint_limited_per_ip(web, fake_clock):  # noqa: F811
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
    # The same (possibly shared, public) client from elsewhere is not locked out.
    async with web.browser(ip="198.51.100.31") as b:
        assert (await _bad_token(b, cid)).status_code == 400
    fake_clock.advance(minutes=11)
    async with web.browser(ip="198.51.100.30") as b:
        assert (await _bad_token(b, cid)).status_code == 400


async def test_token_client_throttle_keyed_on_client_and_ip(web):  # noqa: F811
    web.state.throttles.token_client.limit = 5
    async with web.browser(ip="198.51.100.32") as b:
        cid = (await register(b))["client_id"]
        other = (await register(b))["client_id"]
        for _ in range(5):
            assert (await _bad_token(b, cid)).status_code == 400
        assert (await _bad_token(b, cid)).status_code == 429
        assert (await _bad_token(b, other)).status_code == 400  # other client, same IP
    async with web.browser(ip="198.51.100.33") as b:
        assert (await _bad_token(b, cid)).status_code == 400  # same client, other IP


async def test_token_client_ignores_unknown_clients(web):  # noqa: F811
    web.state.throttles.token_client.limit = 3
    async with web.browser(ip="198.51.100.34") as b:
        for _ in range(5):
            assert (await _bad_token(b, "https://unknown.example/app.json")).status_code in (
                400,
                401,
            )
    assert not web.state.throttles.token_client._events


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


async def test_known_but_stale_bearers_not_counted(web):  # noqa: F811
    user = web.user()
    key_id, key = tokens.create_api_key(web.conn, user.id, "k", parse_scope("owner"))
    tokens.revoke_api_key(web.conn, key_id, user.id)
    _, live = tokens.create_api_key(web.conn, user.id, "k2", parse_scope("owner"))
    async with web.browser(ip="198.51.100.62") as b:
        async with web.browser() as w:
            await login(w)
            tok = await oauth_flow(w)
        tokens.revoke_grant(web.conn, tokens.grant_of_token(web.conn, tok["access_token"]))
        for _ in range(40):
            assert (await _mcp(b, key)).status_code == 401
            assert (await _mcp(b, tok["access_token"])).status_code == 401
        assert (await _mcp(b, live)).status_code not in (401, 429)
        # A forged key reusing a real prefix is still a guess.
        forged = key.rsplit("_", 1)[0] + "_" + "x" * 43
        for _ in range(30):
            await _mcp(b, forged)
        assert (await _mcp(b, live)).status_code == 429


async def test_warns_once_when_proxy_untrusted(web, caplog):  # noqa: F811
    async with web.browser(ip="127.0.0.1") as b:
        for _ in range(2):
            await b.get("/healthz", headers={"X-Forwarded-For": "198.51.100.1"})
    warnings = [r for r in caplog.records if "trusted_proxies is empty" in r.getMessage()]
    assert len(warnings) == 1
    caplog.clear()
    async with web.browser(ip="198.51.100.80") as b:
        await b.get("/healthz", headers={"X-Forwarded-For": "198.51.100.1"})
    assert not [r for r in caplog.records if "trusted_proxies" in r.getMessage()]


async def test_reset_request_limited_per_login_silently(monkeypatch):
    from wwyd.web import pages

    sent: list[str] = []
    monkeypatch.setattr(pages, "send_reset_link", lambda cfg, login, url: sent.append(login))
    w, stack = await make_web(smtp_host="smtp.example.com", smtp_from="wwyd@example.com")
    async with stack:
        w.user()
        for i in range(5):
            async with w.browser(ip=f"198.51.100.{90 + i}") as b:
                csrf = csrf_of((await b.get("/reset")).text)
                r = await b.post(
                    "/reset/request", data={"csrf": csrf, "login": "owner@example.com"}
                )
                ghost = await b.post(
                    "/reset/request", data={"csrf": csrf, "login": "no@example.com"}
                )
                assert r.status_code == ghost.status_code == 200
                assert "If that login exists" in r.text and "If that login exists" in ghost.text
        assert sent == ["owner@example.com"] * 3
        # Only the newest of those links still works.
        live = w.conn.execute(
            "SELECT COUNT(*) FROM one_time_links WHERE kind = 'reset' AND used_at IS NULL"
        ).fetchone()[0]
        assert live == 1
