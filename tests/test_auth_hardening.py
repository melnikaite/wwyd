# SPDX-License-Identifier: AGPL-3.0-or-later
"""Request body caps, reset-time revocation, reset links, audience revocation, consent."""

from __future__ import annotations

import json
import re

import pytest
from authkit import (  # noqa: F401
    PASSWORD,
    csrf_of,
    login,
    make_web,
    oauth_flow,
    register,
    web,
)

from wwyd.auth import accounts, tokens
from wwyd.auth.middleware import FORM_BODY_CAP, MCP_BODY_OVERHEAD, mcp_body_cap
from wwyd.auth.scopes import parse_scope
from wwyd.limits import DEFAULTS

pytestmark = pytest.mark.anyio

MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


async def _chunks(total: int, size: int = 16 * 1024):
    sent = 0
    while sent < total:
        n = min(size, total - sent)
        sent += n
        yield b" " * n


# --- body caps ----------------------------------------------------------------------------


async def test_form_body_cap_by_content_length(web):  # noqa: F811
    async with web.browser() as b:
        r = await b.post("/login", content=b"a=" + b"x" * FORM_BODY_CAP)
        assert r.status_code == 413
        r = await b.post("/token", content=b"a=" + b"x" * FORM_BODY_CAP)
        assert r.status_code == 413
        ok = await b.post("/login", data={"login": "x@example.com", "password": "y" * 12})
        assert ok.status_code != 413


async def test_form_body_cap_chunked(web):  # noqa: F811
    async with web.browser() as b:
        r = await b.post(
            "/login",
            content=_chunks(FORM_BODY_CAP + 1),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert r.status_code == 413
        r = await b.post(
            "/register",
            content=_chunks(FORM_BODY_CAP * 3),
            headers={"Content-Type": "application/json"},
        )
        assert r.status_code == 413


async def test_mcp_body_cap(web):  # noqa: F811
    user = web.user()
    _, key = tokens.create_api_key(web.conn, user.id, "k", parse_scope("owner"))
    cap = DEFAULTS.batch_bytes + MCP_BODY_OVERHEAD
    headers = {**MCP_HEADERS, "Authorization": f"Bearer {key}"}
    async with web.browser() as b:
        big = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {"pad": "x" * (cap // 2)}}
        )
        assert (await b.post("/mcp", content=big, headers=headers)).status_code != 413
        r = await b.post("/mcp", content=b" " * (cap + 1), headers=headers)
        assert r.status_code == 413
        r = await b.post("/mcp", content=_chunks(cap + 1, 256 * 1024), headers=headers)
        assert r.status_code == 413


async def test_mcp_body_cap_follows_admin_ceiling(web):  # noqa: F811
    user = web.user()
    assert mcp_body_cap(web.conn, 2_000_000) == 2_000_000 + MCP_BODY_OVERHEAD
    web.conn.execute(
        "INSERT INTO limit_overrides(user_id, set_by, limits, updated_at) VALUES (?, 'admin', ?,"
        " '2026-01-01T00:00:00Z')",
        (user.id, json.dumps({"batch_bytes": 9_000_000})),
    )
    assert mcp_body_cap(web.conn, 2_000_000) == 9_000_000 + MCP_BODY_OVERHEAD
    assert mcp_body_cap(web.conn, None) is None


# --- password reset -------------------------------------------------------------------------


async def _reset(web, user_id: str, **extra: str):  # noqa: ANN001, ANN202, F811
    token = accounts.create_link(web.conn, "reset", user_id=user_id, created_by="admin")
    async with web.browser() as b:
        page = await b.get("/reset", params={"t": token})
        assert 'name="disconnect_apps"' in page.text and "checked" in page.text
        data = {"csrf": csrf_of(page.text), "t": token, "password": "n" * 12, "password2": "n" * 12}
        return await b.post("/reset", data={**data, **extra})


async def test_reset_disconnects_apps_by_choice(web):  # noqa: F811
    user = web.user()
    _, key = tokens.create_api_key(web.conn, user.id, "k", parse_scope("owner"))
    async with web.browser() as b:
        await login(b)
        tok = await oauth_flow(b)
    r = await _reset(web, user.id)  # box unticked: nothing sent for it
    assert r.status_code == 303
    assert tokens.verify_bearer(web.conn, key) and tokens.verify_bearer(
        web.conn, tok["access_token"]
    )
    r = await _reset(web, user.id, disconnect_apps="1")
    assert r.status_code == 303
    assert tokens.verify_bearer(web.conn, key) is None
    assert tokens.verify_bearer(web.conn, tok["access_token"]) is None
    assert tokens.list_grants(web.conn, user.id) == []


async def test_password_set_and_new_link_invalidate_reset_links(web):  # noqa: F811
    user = web.user()
    first = accounts.create_link(web.conn, "reset", user_id=user.id, created_by="self")
    second = accounts.create_link(web.conn, "reset", user_id=user.id, created_by="self")
    assert accounts.peek_link(web.conn, "reset", first) is None
    assert accounts.peek_link(web.conn, "reset", second) is not None
    invite = accounts.create_link(web.conn, "invite")
    accounts.set_password(web.conn, user.id, accounts.hash_password("z" * 12))
    assert accounts.peek_link(web.conn, "reset", second) is None
    assert accounts.peek_link(web.conn, "invite", invite) is not None


async def test_password_change_invalidates_reset_links(web):  # noqa: F811
    user = web.user()
    link = accounts.create_link(web.conn, "reset", user_id=user.id, created_by="self")
    async with web.browser() as b:
        await login(b)
        csrf = csrf_of((await b.get("/account")).text)
        r = await b.post(
            "/account/password", data={"csrf": csrf, "current": PASSWORD, "new": "q" * 12}
        )
        assert r.status_code == 200
    assert accounts.peek_link(web.conn, "reset", link) is None


# --- audiences --------------------------------------------------------------------------


async def test_revoke_for_audience(web):  # noqa: F811
    user = web.user()
    other = web.user("other@example.com")
    _, fam = tokens.create_api_key(web.conn, user.id, "a", parse_scope("respond:family,work"))
    _, work = tokens.create_api_key(web.conn, user.id, "b", parse_scope("respond:work"))
    _, owner = tokens.create_api_key(web.conn, user.id, "c", parse_scope("owner"))
    _, theirs = tokens.create_api_key(web.conn, other.id, "d", parse_scope("respond:family"))
    _, fam_like = tokens.create_api_key(web.conn, user.id, "e", parse_scope("respond:family2"))
    web.conn.execute(
        "INSERT INTO grants(id, user_id, client_id, scope, created_at)"
        " VALUES ('grt_x', ?, 'c', 'respond:family', '2026-01-01T00:00:00Z')",
        (user.id,),
    )
    assert tokens.revoke_for_audience(web.conn, user.id, "family") == 2
    assert tokens.verify_bearer(web.conn, fam) is None
    for live in (work, owner, theirs, fam_like):
        assert tokens.verify_bearer(web.conn, live) is not None
    assert tokens.principal_by_subject(web.conn, "grt_x") is None
    assert tokens.revoke_for_audience(web.conn, user.id, "family") == 0


# --- consent ----------------------------------------------------------------------------


async def test_consent_marks_dcr_name_unverified(web):  # noqa: F811
    web.user()
    async with web.browser() as b:
        await login(b)
        cid = (await register(b))["client_id"]
        from authkit import authorize, pkce

        r = await authorize(b, cid, "https://claude.ai/api/mcp/auth_callback", pkce()[1])
        loc = r.headers["location"]
        req = re.search(r"req=([^&]+)", loc)
        page = await b.get(loc if req is None else f"/consent?req={req.group(1)}")
        assert page.status_code == 200, page.text
        h1 = re.search(r"<h1>(.*?)</h1>", page.text, re.S).group(1)
        assert "claude.ai" in h1
        assert "not verified" in page.text
