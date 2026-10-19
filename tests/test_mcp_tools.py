# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

from typing import Any

import pytest
from conftest import call, call_error, make_server
from mcp import Client

pytestmark = pytest.mark.anyio

OWNER_TOOLS = {
    "add_claims",
    "list_claims",
    "search_claims",
    "update_claim",
    "delete_claims",
    "review_claims",
    "list_audiences",
    "upsert_audience",
    "delete_audience",
    "list_categories",
    "upsert_category",
    "merge_categories",
    "compact",
    "list_changes",
    "undo_change",
    "export_persona",
    "import_persona",
    "delete_persona",
    "get_settings",
    "update_settings",
    "ingest_raw",
    "job_status",
    "list_sources",
    "reset_source",
    "list_audit",
    "get_persona",
    "predict",
    "list_analyzers",
    "get_analyzer",
    "update_analyzer",
}
# SPEC §5: everything is owner-only except get_persona (read) and predict (respond).
SCOPED_TOOLS = {"respond": {"predict"}, "read": {"get_persona"}}


async def test_owner_tools_listed(client):
    tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools) == OWNER_TOOLS
    assert tools["merge_categories"].input_schema["required"] == ["from", "into"]
    assert tools["list_claims"].annotations.read_only_hint is True
    assert tools["delete_claims"].annotations.destructive_hint is True
    assert all(t.description and t.output_schema for t in tools.values())


def _register_unscoped(server):
    async def test_only_unscoped() -> dict[str, Any]:
        return {}

    server.add_tool(test_only_unscoped, structured_output=True)  # no scope: hidden from all


@pytest.mark.parametrize("scope", ["respond", "read", "owner"])
async def test_tools_filtered_by_scope(conn, user, scope):
    server = make_server(
        conn, user, scope=scope, audiences=("family",) if scope == "respond" else None
    )
    _register_unscoped(server)
    assert {n for n, s in server.tool_scopes().items() if scope in s} == (
        SCOPED_TOOLS.get(scope) or OWNER_TOOLS
    )
    async with Client(server) as c:
        names = {t.name for t in (await c.list_tools()).tools}
        assert names == (SCOPED_TOOLS.get(scope) or OWNER_TOOLS)
        assert (await call_error(c, "test_only_unscoped"))["code"] == "not_found"
        if scope == "owner":
            return
        assert (await call_error(c, "list_claims"))["code"] == "forbidden"
        (visible,) = SCOPED_TOOLS[scope]
        (other,) = {"get_persona", "predict"} - {visible}
        assert (await call_error(c, other))["code"] == "forbidden"
        # The visible tool is reachable: no forbidden, at most an argument error.
        res = await c.call_tool(visible, {})
        if res.is_error:
            assert res.structured_content["error"]["code"] == "invalid"


async def test_no_caller_sees_nothing(conn, user):
    from wwyd.mcp.server import build_server

    async with Client(build_server(conn, lambda: None)) as c:
        assert (await c.list_tools()).tools == []
        assert (await call_error(c, "list_claims"))["code"] == "forbidden"


async def test_unknown_tool(client):
    assert (await call_error(client, "nope"))["code"] == "not_found"


async def test_claim_flow(client):
    res = await call(
        client,
        "add_claims",
        {
            "claims": [
                {
                    "kind": "style",
                    "category": "communication/style",
                    "text": "Short, no greeting.",
                    "status": "approved",
                },
                {
                    "kind": "boundary",
                    "category": "communication/boundaries",
                    "text": "Never discusses salary in writing.",
                },
                {"kind": "trait", "category": "nope", "text": "x"},
                {"kind": "trait", "category": "food", "text": "Ask @someone"},
            ]
        },
    )
    assert len(res["created"]) == 2
    assert [d["reason"] for d in res["dropped"]] == ["invalid", "guard"]
    approved, pending = res["created"]

    listed = await call(client, "list_claims", {})
    assert [c["id"] for c in listed["claims"]] == [approved]  # owner may add approved directly

    review = await call(client, "review_claims", {"approve": [pending, "chg_x"]})
    assert review["approved"] == 1 and review["not_found"] == ["chg_x"]

    found = await call(client, "search_claims", {"query": 'salary "writing'})
    assert [c["id"] for c in found["claims"]] == [pending] and "score" in found["claims"][0]

    updated = await call(client, "update_claim", {"id": approved, "text": "Short."})
    assert updated["claim"]["origin"] == "owner" and updated["claim"]["status"] == "approved"

    assert await call(client, "delete_claims", {"ids": [approved]}) == {"deleted": 1}
    err = await call_error(client, "update_claim", {"id": approved, "text": "x"})
    assert err["code"] == "not_found"


async def test_error_codes(client):
    assert (await call_error(client, "list_claims", {"limit": 500}))["code"] == "invalid"
    assert (await call_error(client, "list_claims", {"kind": "mood"}))["code"] == "invalid"
    assert (await call_error(client, "add_claims", {"claims": "nope"}))["code"] == "invalid"
    big = {"claims": [{"kind": "trait", "category": "food", "text": str(i)} for i in range(201)]}
    assert (await call_error(client, "add_claims", big))["code"] == "too_large"
    err = await call_error(client, "delete_audience", {"name": "everyone"})
    assert err["code"] == "conflict"
    await call(client, "upsert_audience", {"name": "family"})
    await call(
        client,
        "add_claims",
        {
            "claims": [
                {
                    "kind": "trait",
                    "category": "family",
                    "text": "Calls mum weekly",
                    "audiences": ["family"],
                }
            ]
        },
    )
    err = await call_error(client, "delete_audience", {"name": "family"})
    assert err["code"] == "conflict" and err["claim_count"] == 1
    assert (await call_error(client, "upsert_category", {"path": "x/y"}))["code"] == "not_found"
    err = await call_error(client, "import_persona", {"document": {"format": "x"}})
    assert err["code"] == "invalid" and err["errors"]
    assert (await call_error(client, "delete_persona", {"confirm": "nope"}))["code"] == "invalid"
    err = await call_error(client, "update_settings", {"timezone": "Nowhere/City"})
    assert err["code"] == "invalid"


async def test_audiences_and_categories(client):
    res = await call(
        client, "upsert_audience", {"name": "family", "description": "kin", "is_default": True}
    )
    assert res["audience"]["is_default"]
    auds = (await call(client, "list_audiences"))["audiences"]
    assert [(a["name"], a["is_default"]) for a in auds] == [("family", True), ("everyone", False)]
    await call(client, "upsert_category", {"path": "hobbies/chess", "description": "Chess"})
    merged = await call(client, "merge_categories", {"from": ["hobbies/chess"], "into": "hobbies"})
    assert merged["removed_categories"] == ["hobbies/chess"]
    tree = (await call(client, "list_categories"))["categories"]
    assert "hobbies" in {n["path"] for n in tree}


async def test_persona_tools_round_trip(client, user):
    await call(client, "update_settings", {"timezone": "Europe/Berlin", "display_name": "E"})
    await call(
        client,
        "add_claims",
        {
            "claims": [
                {"kind": "trait", "category": "food", "text": "Likes tea", "status": "approved"}
            ]
        },
    )
    doc = (await call(client, "export_persona"))["document"]
    assert await call(client, "delete_persona", {"confirm": user.login}) == {"deleted": True}
    assert (await call(client, "list_claims", {"status": "any"}))["claims"] == []
    counts = await call(client, "import_persona", {"document": doc, "mode": "replace"})
    assert counts["claims_created"] == 1
    again = (await call(client, "export_persona"))["document"]
    doc.pop("exported_at"), again.pop("exported_at")
    assert again == doc
    settings = await call(client, "get_settings")
    assert settings["timezone"] == "Europe/Berlin" and settings["display_name"] == "E"
