# SPDX-License-Identifier: AGPL-3.0-or-later
"""The four web pages: sign in, account, admin, setup/invite/reset landing."""

from __future__ import annotations

import re

import pytest
from authkit import PASSWORD, PUBLIC_URL, csrf_of, fake_clock, login, make_web, oauth_flow, web  # noqa: F401

from wwyd.app import startup_notice
from wwyd.auth import accounts, tokens

pytestmark = pytest.mark.anyio

NEW_PASSWORD = "another long passphrase"


async def _landing(b, path: str, token: str, **data):  # noqa: ANN202
    page = await b.get(path, params={"t": token})
    assert page.status_code == 200, page.text
    return await b.post(
        path.split("?")[0],
        data={
            "csrf": csrf_of(page.text),
            "t": token,
            "password": data.pop("password", PASSWORD),
            "password2": data.pop("password2", None) or PASSWORD,
            **data,
        },
    )


async def test_root_redirects(web):  # noqa: F811
    web.user()
    async with web.browser() as b:
        assert (await b.get("/")).headers["location"] == "/login"
        await login(b)
        assert (await b.get("/")).headers["location"] == "/account"


async def test_setup_link_creates_admin_once(web):  # noqa: F811
    token = accounts.setup_link_if_needed(web.conn)
    async with web.browser() as b:
        resp = await _landing(b, "/setup", token, login="admin@example.com")
        assert resp.status_code == 303 and resp.headers["location"] == "/account?welcome=1"
        page = await b.get("/account?welcome=1")
        assert "Now connect an app" in page.text
        assert f"claude mcp add --transport http wwyd {PUBLIC_URL}/mcp" in page.text
        again = await b.get("/setup", params={"t": token})
    assert again.status_code == 400 and "no longer works" in again.text
    user = accounts.user_by_login(web.conn, "admin@example.com")
    assert user.is_admin
    assert accounts.setup_link_if_needed(web.conn) is None


async def test_setup_link_expires(web, fake_clock):  # noqa: F811
    token = accounts.setup_link_if_needed(web.conn)
    fake_clock.advance(hours=25)
    async with web.browser() as b:
        assert (await b.get("/setup", params={"t": token})).status_code == 400


async def test_startup_notice(web, capsys):  # noqa: F811
    notice = startup_notice(web.conn, web.config)
    assert notice.startswith(f"Open this link to create the admin account: {PUBLIC_URL}/setup?t=")
    from wwyd.store.personas import create_user

    create_user(web.conn, "stdio@example.com")  # M1 stdio: admin without password
    assert "wwyd reset-password stdio@example.com" in startup_notice(web.conn, web.config)


async def test_invite_link_single_use_and_expiry(web, fake_clock):  # noqa: F811
    web.user()
    token = accounts.create_link(web.conn, "invite", created_by="cli")
    async with web.browser() as b:
        resp = await _landing(b, "/invite", token, login="friend@example.com")
        assert resp.status_code == 303
    async with web.browser() as b:
        page = await b.get("/invite", params={"t": token})
        assert page.status_code == 400
    friend = accounts.user_by_login(web.conn, "friend@example.com")
    assert friend and not friend.is_admin
    expiring = accounts.create_link(web.conn, "invite")
    fake_clock.advance(days=7, minutes=1)
    async with web.browser() as b:
        assert (await b.get("/invite", params={"t": expiring})).status_code == 400


async def test_invite_rejects_taken_login_and_short_password(web):  # noqa: F811
    web.user()
    token = accounts.create_link(web.conn, "invite")
    async with web.browser() as b:
        taken = await _landing(b, "/invite", token, login="owner@example.com")
        assert taken.status_code == 400 and "already taken" in taken.text
        short = await _landing(
            b, "/invite", token, login="x@example.com", password="short", password2="short"
        )
        assert short.status_code == 400 and "at least 10" in short.text
        ok = await _landing(b, "/invite", token, login="x@example.com")
        assert ok.status_code == 303


async def test_wrong_link_kind_rejected(web):  # noqa: F811
    token = accounts.create_link(web.conn, "invite")
    async with web.browser() as b:
        assert (await b.get("/setup", params={"t": token})).status_code == 400


async def test_admin_reset_link_ends_sessions_and_shows_notice(web):  # noqa: F811
    user = web.user()
    async with web.browser() as old:
        await login(old)
        assert (await old.get("/account")).status_code == 200
        token = accounts.create_link(web.conn, "reset", user_id=user.id, created_by="cli")
        async with web.browser() as b:
            page = await b.get("/reset", params={"t": token})
            assert "owner@example.com" in page.text and "readonly" in page.text
            resp = await _landing(b, "/reset", token, password=NEW_PASSWORD, password2=NEW_PASSWORD)
            assert resp.status_code == 303
            account = await b.get("/account")
            assert "reset by an administrator" in account.text
            assert (await b.get("/reset", params={"t": token})).status_code == 400
        # The old session ended.
        assert (await old.get("/account")).status_code == 303
    async with web.browser() as b:
        assert (await login(b, password=NEW_PASSWORD)).status_code == 303


async def test_reset_link_expires(web, fake_clock):  # noqa: F811
    user = web.user()
    token = accounts.create_link(web.conn, "reset", user_id=user.id)
    fake_clock.advance(hours=24, seconds=1)
    async with web.browser() as b:
        assert (await b.get("/reset", params={"t": token})).status_code == 400


async def test_login_wrong_password_generic(web):  # noqa: F811
    web.user()
    async with web.browser() as b:
        wrong = await login(b, password="nope nope nope")
        unknown = await login(b, login="ghost@example.com")
    assert wrong.status_code == unknown.status_code == 401
    assert "do not match" in wrong.text and "do not match" in unknown.text


async def test_login_next_must_be_local(web):  # noqa: F811
    web.user()
    async with web.browser() as b:
        page = await b.get("/login")
        resp = await b.post(
            "/login",
            data={
                "csrf": csrf_of(page.text),
                "login": "owner@example.com",
                "password": PASSWORD,
                "next": "//evil.example/x",
            },
        )
    assert resp.headers["location"] == "/account"


async def test_session_cookie_flags(web):  # noqa: F811
    web.user()
    async with web.browser() as b:
        resp = await login(b)
    cookie = resp.headers["set-cookie"]
    assert "wwyd_session=" in cookie and "HttpOnly" in cookie and "SameSite=lax" in cookie
    assert "Secure" not in cookie  # http://127.0.0.1 public URL


async def test_secure_cookie_for_https():
    w, stack = await make_web(public_url="https://wwyd.example")
    async with stack:
        w.user()
        transport = __import__("httpx").ASGITransport(app=w.app, client=("203.0.113.9", 1))
        async with __import__("httpx").AsyncClient(
            transport=transport, base_url="https://wwyd.example"
        ) as b:
            resp = await login(b)
    assert "Secure" in resp.headers["set-cookie"]


async def test_csrf_rejected(web):  # noqa: F811
    web.user()
    async with web.browser() as b:
        await b.get("/login")
        resp = await b.post(
            "/login", data={"csrf": "forged", "login": "owner@example.com", "password": PASSWORD}
        )
        assert resp.status_code == 403
        await login(b)
        resp = await b.post("/account/keys", data={"csrf": "forged", "name": "x", "scope": "owner"})
        assert resp.status_code == 403
        resp = await b.post("/account/password", data={"current": PASSWORD, "new": NEW_PASSWORD})
        assert resp.status_code == 403
    assert web.conn.execute("SELECT COUNT(*) FROM api_keys").fetchone()[0] == 0


async def test_password_change_ends_other_sessions_not_grants(web):  # noqa: F811
    web.user()
    key_owner = web.conn.execute("SELECT id FROM users").fetchone()["id"]
    from wwyd.auth.scopes import parse_scope

    _, key = tokens.create_api_key(web.conn, key_owner, "k", parse_scope("owner"))
    async with web.browser() as other, web.browser() as b:
        await login(other)
        await login(b)
        tok = await oauth_flow(b)
        page = await b.get("/account")
        bad = await b.post(
            "/account/password",
            data={"csrf": csrf_of(page.text), "current": "wrong wrong", "new": NEW_PASSWORD},
        )
        assert bad.status_code == 400 and "not right" in bad.text
        resp = await b.post(
            "/account/password",
            data={"csrf": csrf_of(page.text), "current": PASSWORD, "new": NEW_PASSWORD},
        )
        assert resp.status_code == 200 and "Password changed" in resp.text
        assert (await b.get("/account")).status_code == 200  # this session stays
        assert (await other.get("/account")).status_code == 303  # the other one ended
    assert tokens.verify_access_token(web.conn, tok["access_token"]) is not None
    assert tokens.verify_api_key(web.conn, key) is not None


async def test_admin_page_forbidden_to_non_admin(web):  # noqa: F811
    web.user("admin@example.com")
    web.user("user@example.com")
    async with web.browser() as b:
        assert (await b.get("/admin")).status_code == 303  # not signed in
        await login(b, "user@example.com")
        assert (await b.get("/admin")).status_code == 403
        page = await b.get("/account")
        resp = await b.post("/admin/invite", data={"csrf": csrf_of(page.text)})
        assert resp.status_code == 403
        assert "Users</a>" not in page.text


async def test_admin_invite_reset_disable(web):  # noqa: F811
    admin = web.user("admin@example.com")
    user = web.user("user@example.com")
    assert admin.is_admin and not user.is_admin
    async with web.browser() as u:
        await login(u, "user@example.com")
        async with web.browser() as b:
            await login(b, "admin@example.com")
            page = await b.get("/admin")
            assert "user@example.com" in page.text
            csrf = csrf_of(page.text)
            inv = await b.post("/admin/invite", data={"csrf": csrf})
            assert re.search(re.escape(PUBLIC_URL) + r"/invite\?t=[\w-]+", inv.text)
            rst = await b.post(f"/admin/users/{user.id}/reset", data={"csrf": csrf})
            assert re.search(r"/reset\?t=[\w-]+", rst.text)
            self_disable = await b.post(f"/admin/users/{admin.id}/disable", data={"csrf": csrf})
            assert self_disable.status_code == 400
            assert (
                await b.post(f"/admin/users/{user.id}/disable", data={"csrf": csrf})
            ).status_code == 303
            assert accounts.user_by_id(web.conn, user.id).disabled_at
            assert (await u.get("/account")).status_code == 303  # signed out
            assert (await login(u, "user@example.com")).status_code == 401
            assert (
                await b.post(f"/admin/users/{user.id}/enable", data={"csrf": csrf})
            ).status_code == 303
        assert (await login(u, "user@example.com")).status_code == 303


async def test_admin_cannot_reach_persona_data(web):  # noqa: F811
    canary = "zq7x-canary-41b9 Velmorine"
    web.user("admin@example.com")
    user = web.user("user@example.com")
    from wwyd.store import claims

    claims.add_claims(
        web.conn, user.persona_id, [{"text": f"Prefers {canary}", "kind": "preference"}]
    )
    async with web.browser() as b:
        await login(b, "admin@example.com")
        page = await b.get("/admin")
        csrf = csrf_of(page.text)
        bodies = [page.text]
        bodies.append((await b.post(f"/admin/users/{user.id}/reset", data={"csrf": csrf})).text)
        bodies.append((await b.post("/admin/invite", data={"csrf": csrf})).text)
        bodies.append((await b.get("/account")).text)
    for body in bodies:
        assert "zq7x" not in body and "Velmorine" not in body
    paths = {getattr(r, "path", "") for r in web.app.routes}
    assert not any("persona" in p or "claim" in p for p in paths)


async def test_logout(web):  # noqa: F811
    web.user()
    async with web.browser() as b:
        await login(b)
        page = await b.get("/account")
        assert (await b.post("/logout", data={"csrf": csrf_of(page.text)})).status_code == 303
        assert (await b.get("/account")).status_code == 303
    assert web.conn.execute("SELECT COUNT(*) FROM web_sessions").fetchone()[0] == 0


async def test_session_expires_and_slides(web, fake_clock):  # noqa: F811
    web.user()
    async with web.browser() as b:
        await login(b)
        fake_clock.advance(days=13)
        assert (await b.get("/account")).status_code == 200  # slides
        fake_clock.advance(days=13)
        assert (await b.get("/account")).status_code == 200
        fake_clock.advance(days=15)
        assert (await b.get("/account")).status_code == 303


async def test_security_headers(web):  # noqa: F811
    async with web.browser() as b:
        resp = await b.get("/login")
    assert resp.headers["referrer-policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in resp.headers["content-security-policy"]
    assert resp.headers["cache-control"] == "no-store"


async def test_self_service_reset_with_smtp(monkeypatch):
    sent = []
    monkeypatch.setattr(
        "wwyd.web.pages.send_reset_link", lambda config, to, url: sent.append((to, url))
    )
    w, stack = await make_web(smtp_host="smtp.example", smtp_from="wwyd@example.com")
    async with stack:
        w.user()
        async with w.browser() as b:
            page = await b.get("/reset")
            assert "Send me a reset link" in page.text
            for login_ in ("owner@example.com", "ghost@example.com"):
                resp = await b.post(
                    "/reset/request", data={"csrf": csrf_of(page.text), "login": login_}
                )
                assert "If that login exists" in resp.text
        assert len(sent) == 1 and sent[0][0] == "owner@example.com"
        token = sent[0][1].split("t=")[1]
        async with w.browser() as b:
            resp = await _landing(b, "/reset", token, password=NEW_PASSWORD, password2=NEW_PASSWORD)
            assert resp.status_code == 303
            assert "reset by an administrator" not in (await b.get("/account")).text


async def test_reset_without_smtp_explains(web):  # noqa: F811
    async with web.browser() as b:
        page = await b.get("/reset")
    assert "Ask your administrator" in page.text and "Send me" not in page.text


async def test_cli_invite_and_reset(web, capsys, monkeypatch):  # noqa: F811
    from wwyd.cli import main

    assert main(["invite"]) == 0
    out = capsys.readouterr().out
    assert re.match(r"http://127\.0\.0\.1:8740/invite\?t=[\w-]+", out)
    assert main(["reset-password", "nobody@example.com"]) == 1
