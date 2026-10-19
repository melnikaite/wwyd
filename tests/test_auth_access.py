# SPDX-License-Identifier: AGPL-3.0-or-later
"""Scope matrix over HTTP, API keys, immediate revocation, disabled users, tools/list isolation."""

from __future__ import annotations

from typing import Any

import pytest
from authkit import login, oauth_flow, web  # noqa: F401

from wwyd.auth import accounts, tokens
from wwyd.auth.scopes import parse_scope
from wwyd.mcp.caller import current_caller

pytestmark = pytest.mark.anyio

# A test-only tool (name cannot collide with a real one) that reports the caller, so the
# subject and audiences can be observed; visible to every scope.
WHOAMI = "test_only_whoami"
READ_TOOLS = {"get_persona", WHOAMI}
RESPOND_TOOLS = {"predict", WHOAMI}


def _whoami(server) -> None:
    @server.scoped_tool(["owner", "read", "respond"], name=WHOAMI)
    async def whoami() -> dict[str, Any]:
        """Test only: report the current caller."""
        c = current_caller()
        return {"scope": c.scope, "audiences": list(c.audiences or ()), "subject_id": c.subject_id}


def _code(res) -> str | None:  # noqa: ANN001
    return res.structured_content["error"]["code"] if res.is_error else None


def _key(web, scope: str, user_id: str | None = None) -> tuple[str, str]:  # noqa: F811
    uid = user_id or web.conn.execute("SELECT id FROM users").fetchone()["id"]
    return tokens.create_api_key(web.conn, uid, "test", parse_scope(scope))


@pytest.fixture
def owner(web):  # noqa: F811
    _whoami(web.state.mcp)
    return web.user()


def _first_audience(web) -> str:  # noqa: F811
    return web.conn.execute("SELECT name FROM audiences ORDER BY name").fetchone()["name"]


@pytest.mark.parametrize("scope", ["owner", "read", "respond"])
async def test_scope_matrix(web, owner, scope):  # noqa: F811
    aud = _first_audience(web)
    scope_str = f"respond:{aud}" if scope == "respond" else scope
    key_id, key = _key(web, scope_str)
    async with web.mcp(key) as c:
        names = {t.name for t in (await c.list_tools()).tools}
        persona = await c.call_tool("get_persona", {})
        # No arguments: a caller allowed to call predict gets "invalid" (no LLM call),
        # everyone else "forbidden".
        predict = await c.call_tool("predict", {})
        add = await c.call_tool("add_claims", {"claims": []})
        me = (await c.call_tool(WHOAMI, {})).structured_content
    assert me["subject_id"] == key_id and me["scope"] == scope
    if scope == "owner":
        assert {"add_claims", "get_persona", "predict", "list_audit"} <= names
        assert not persona.is_error and _code(predict) == "invalid" and not add.is_error
    elif scope == "read":
        assert names == READ_TOOLS
        assert not persona.is_error and _code(predict) == "forbidden"
        assert _code(add) == "forbidden"
    else:
        assert names == RESPOND_TOOLS
        assert _code(persona) == "forbidden" and _code(add) == "forbidden"
        assert _code(predict) == "invalid"
        assert me["audiences"] == [aud]


async def test_oauth_token_caller_has_grant_subject(web, owner):  # noqa: F811
    async with web.browser() as b:
        await login(b)
        tok = await oauth_flow(b, scope="read")
    async with web.mcp(tok["access_token"]) as c:
        assert {t.name for t in (await c.list_tools()).tools} == READ_TOOLS
        assert not (await c.call_tool("get_persona", {})).is_error
        res = await c.call_tool(WHOAMI, {})
    assert res.structured_content["subject_id"].startswith("grt_")


async def test_tools_list_isolated_between_tokens(web, owner):  # noqa: F811
    _, owner_key = _key(web, "owner")
    _, respond_key = _key(web, f"respond:{_first_audience(web)}")
    async with web.mcp(owner_key) as a, web.mcp(respond_key) as b:
        la = await a.list_tools()
        lb = await b.list_tools()
        la2 = await a.list_tools()
    assert {t.name for t in lb.tools} == RESPOND_TOOLS
    assert len(la.tools) == len(la2.tools) > len(lb.tools)
    # The result says it must not be shared across authorization contexts.
    assert lb.cache_scope == "private"


async def test_session_cannot_be_hijacked_by_other_token(web, owner):  # noqa: F811
    _, owner_key = _key(web, "owner")
    _, read_key = _key(web, "read")
    async with web.mcp(owner_key) as a:
        await a.list_tools()
    async with web.browser() as b:
        init = await b.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "t", "version": "1"},
                },
            },
            headers={
                "Authorization": f"Bearer {owner_key}",
                "Accept": "application/json, text/event-stream",
            },
        )
        sid = init.headers.get("mcp-session-id")
        if sid:  # stateful session: another credential must not use it
            other = await b.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                headers={
                    "Authorization": f"Bearer {read_key}",
                    "Mcp-Session-Id": sid,
                    "Accept": "application/json, text/event-stream",
                },
            )
            assert other.status_code == 404


async def _ping(web, token: str) -> int:  # noqa: F811
    async with web.browser() as b:
        resp = await b.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json, text/event-stream",
            },
        )
    return resp.status_code


async def test_api_key_revocation_is_immediate(web, owner):  # noqa: F811
    key_id, key = _key(web, "owner")
    async with web.mcp(key) as c:
        await c.list_tools()
        assert tokens.revoke_api_key(web.conn, key_id, owner.id)
        with pytest.raises(Exception):  # noqa: B017 - the transport raises on 401
            await c.list_tools()
    assert await _ping(web, key) == 401


async def test_grant_revocation_is_immediate(web, owner):  # noqa: F811
    async with web.browser() as b:
        await login(b)
        tok = await oauth_flow(b)
    async with web.mcp(tok["access_token"]) as c:
        await c.list_tools()
        grant_id = web.conn.execute("SELECT id FROM grants").fetchone()["id"]
        tokens.revoke_grant(web.conn, grant_id, owner.id)
        with pytest.raises(Exception):  # noqa: B017
            await c.list_tools()
    assert await _ping(web, tok["access_token"]) == 401


async def test_revoke_via_account_page(web, owner):  # noqa: F811
    from authkit import csrf_of

    key_id, key = _key(web, "owner")
    async with web.browser() as b:
        await login(b)
        tok = await oauth_flow(b)
        page = await b.get("/account")
        assert "Test App" in page.text and key_id in page.text
        csrf = csrf_of(page.text)
        grant_id = web.conn.execute("SELECT id FROM grants").fetchone()["id"]
        assert (
            await b.post(f"/account/grants/{grant_id}/revoke", data={"csrf": csrf})
        ).status_code == 303
        assert (
            await b.post(f"/account/keys/{key_id}/revoke", data={"csrf": csrf})
        ).status_code == 303
    assert await _ping(web, tok["access_token"]) == 401
    assert await _ping(web, key) == 401


async def test_cannot_revoke_someone_elses_key(web, owner):  # noqa: F811
    other = web.user("other@example.com")
    key_id, key = _key(web, "owner", other.id)
    assert not tokens.revoke_api_key(web.conn, key_id, owner.id)
    assert await _ping(web, key) != 401


async def test_disabled_user_blocked(web, owner):  # noqa: F811
    _, key = _key(web, "owner")
    async with web.browser() as b:
        await login(b)
        tok = await oauth_flow(b)
    assert await _ping(web, key) != 401
    accounts.set_disabled(web.conn, owner.id, True)
    assert await _ping(web, key) == 401
    assert await _ping(web, tok["access_token"]) == 401
    accounts.set_disabled(web.conn, owner.id, False)
    assert await _ping(web, key) != 401


async def test_api_key_format_and_storage(web, owner):  # noqa: F811
    key_id, key = _key(web, "read")
    assert tokens.API_KEY_RE.match(key)
    prefix = key.split("_")[1]
    row = web.conn.execute("SELECT * FROM api_keys WHERE id = ?", (key_id,)).fetchone()
    assert row["prefix"] == prefix and key not in "\n".join(web.conn.iterdump())
    tampered = key[:-2] + ("AA" if not key.endswith("AA") else "BB")
    assert tokens.verify_api_key(web.conn, tampered) is None


async def test_create_api_key_on_account_page(web, owner):  # noqa: F811
    from authkit import csrf_of

    async with web.browser() as b:
        await login(b)
        page = await b.get("/account")
        resp = await b.post(
            "/account/keys", data={"csrf": csrf_of(page.text), "name": "script", "scope": "read"}
        )
        assert resp.status_code == 200
        import re

        key = re.search(r"wwyd_[a-z0-9]{8}_[A-Za-z0-9_-]+", resp.text).group(0)
        again = await b.get("/account")
    assert key not in again.text  # shown once
    async with web.mcp(key) as c:
        assert {t.name for t in (await c.list_tools()).tools} == READ_TOOLS
