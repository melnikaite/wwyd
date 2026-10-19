# SPDX-License-Identifier: AGPL-3.0-or-later
"""list_analyzers / get_analyzer / update_analyzer (SPEC §6.7)."""

from __future__ import annotations

from datetime import date

import pytest
from analyzer_data import BERLIN, add_audience, cat_id, meetings, workdays
from conftest import call, call_error, make_server
from mcp import Client

from wwyd.analyzers import run_analyzers
from wwyd.ids import new_id, now_iso

pytestmark = pytest.mark.anyio


def _seed(conn, pid):
    meet = cat_id(conn, pid)
    eps = meetings(meet, workdays(date(2026, 9, 7), 30))
    run_analyzers(conn, persona_id=pid, episodes=eps, tz=BERLIN, counts={})
    return meet


def _llm_row(conn, pid, scope_id, status="active"):
    aid = new_id("anl")
    conn.execute(
        "INSERT INTO analyzers(id, persona_id, scope_kind, scope_id, origin, name,"
        " statement_template, kind, source, status, created_at)"
        " VALUES (?, ?, 'audience', ?, 'llm', 'films', 'Watches {x}', 'habit', 'def run(): 1',"
        " ?, ?)",
        (aid, pid, scope_id, status, now_iso()),
    )
    return aid


async def test_list_get_update(client, conn, pid):
    _seed(conn, pid)
    out = await call(client, "list_analyzers")
    names = sorted(a["name"] for a in out["analyzers"])
    assert names == ["time_activity", "time_duration", "time_gaps"]
    gaps = next(a for a in out["analyzers"] if a["name"] == "time_gaps")
    assert gaps["scope"] == "work/meetings" and gaps["scope_kind"] == "category"
    assert gaps["origin"] == "builtin" and gaps["runs"] == 1 and gaps["claim_id"]

    detail = await call(client, "get_analyzer", {"id": gaps["id"]})
    assert "source" not in detail
    assert detail["statement_template"]
    assert detail["claim_ids"] == [gaps["claim_id"]]
    metrics = {m["metric"]: m for m in detail["state"]["metrics"]}
    assert set(metrics) == {"gap", "days"}
    assert metrics["gap"]["keys"] == ["30"] and metrics["gap"]["key_count"] == 1
    assert detail["state"]["months"] == ["2026-09", "2026-10"]

    act = next(a for a in out["analyzers"] if a["name"] == "time_activity")
    assert len((await call(client, "get_analyzer", {"id": act["id"]}))["claim_ids"]) == 3

    upd = await call(client, "update_analyzer", {"id": gaps["id"], "status": "disabled"})
    assert upd["analyzer"]["status"] == "disabled"
    only = await call(client, "list_analyzers", {"status": "disabled"})
    assert [a["id"] for a in only["analyzers"]] == [gaps["id"]]
    err = await call_error(client, "update_analyzer", {"id": gaps["id"], "status": "retired"})
    assert err["code"] == "invalid"
    err = await call_error(client, "get_analyzer", {"id": "anl_missing"})
    assert err["code"] == "not_found"


async def test_scope_filter_and_pagination(client, conn, pid):
    _seed(conn, pid)
    aud = add_audience(conn, pid, "family")
    llm = _llm_row(conn, pid, aud)
    by_aud = await call(client, "list_analyzers", {"scope": "family"})
    assert [a["id"] for a in by_aud["analyzers"]] == [llm]
    assert by_aud["analyzers"][0]["scope_kind"] == "audience"
    by_cat = await call(client, "list_analyzers", {"scope": "work/meetings"})
    assert len(by_cat["analyzers"]) == 3
    assert (await call(client, "list_analyzers", {"scope": "work"}))["analyzers"] == []
    err = await call_error(client, "list_analyzers", {"scope": "nope"})
    assert err["code"] == "not_found"

    page1 = await call(client, "list_analyzers", {"limit": 2})
    assert len(page1["analyzers"]) == 2 and page1["next_cursor"]
    page2 = await call(client, "list_analyzers", {"limit": 2, "cursor": page1["next_cursor"]})
    assert len(page2["analyzers"]) == 2 and "next_cursor" not in page2
    seen = {a["id"] for a in page1["analyzers"] + page2["analyzers"]}
    assert len(seen) == 4


async def test_retire_llm_clears_source(client, conn, pid):
    aud = add_audience(conn, pid, "family")
    llm = _llm_row(conn, pid, aud, status="candidate")
    detail = await call(client, "get_analyzer", {"id": llm})
    assert detail["source"] == "def run(): 1" and detail["state"] == {"months": [], "metrics": []}
    await call(client, "update_analyzer", {"id": llm, "status": "retired"})
    detail = await call(client, "get_analyzer", {"id": llm})
    assert detail["status"] == "retired" and detail["source"] == ""
    assert detail["statement_template"] == "Watches {x}"
    err = await call_error(client, "update_analyzer", {"id": llm, "status": "active"})
    assert err["code"] == "conflict"


async def test_deleted_scope_shows_null(client, conn, pid):
    aud = add_audience(conn, pid, "family")
    _llm_row(conn, pid, aud)
    conn.execute("DELETE FROM audiences WHERE id = ?", (aud,))
    out = await call(client, "list_analyzers")
    assert out["analyzers"][0]["scope"] is None


@pytest.mark.parametrize("scope", ["respond", "read"])
async def test_not_visible_to_other_scopes(conn, user, scope):
    server = make_server(
        conn, user, scope=scope, audiences=("everyone",) if scope == "respond" else None
    )
    async with Client(server) as c:
        names = {t.name for t in (await c.list_tools()).tools}
    assert not names & {"list_analyzers", "get_analyzer", "update_analyzer"}


async def test_other_persona_cannot_see(conn, pid, tmp_path):
    from wwyd.store.personas import create_user

    _seed(conn, pid)
    other = create_user(conn, "other@example.com")
    gaps = conn.execute("SELECT id FROM analyzers WHERE name = 'time_gaps'").fetchone()[0]
    async with Client(make_server(conn, other)) as c:
        assert (await call(c, "list_analyzers"))["analyzers"] == []
        assert (await call_error(c, "get_analyzer", {"id": gaps}))["code"] == "not_found"
        err = await call_error(c, "update_analyzer", {"id": gaps, "status": "disabled"})
        assert err["code"] == "not_found"
