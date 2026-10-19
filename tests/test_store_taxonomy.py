# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import json
import sqlite3

import pytest

from wwyd.errors import Conflict, Invalid, NotFound
from wwyd.store import audiences as au
from wwyd.store import categories as ca
from wwyd.store.claims import add_claims, get_claim


def defaults(conn, pid):
    return [a["name"] for a in au.list_audiences(conn, pid) if a["is_default"]]


# --- audiences ---------------------------------------------------------------------------


def test_audience_create_update_rename(conn, pid):
    res = au.upsert_audience(conn, pid, "work-slack", description="colleagues")
    assert res["created"] and res["audience"] == {
        "name": "work-slack",
        "description": "colleagues",
        "is_default": False,
        "claim_count": 0,
    }
    res = au.upsert_audience(conn, pid, "work-slack", description="peers", rename_to="work")
    assert not res["created"] and res["audience"]["name"] == "work"
    assert {a["name"] for a in au.list_audiences(conn, pid)} == {"everyone", "work"}
    with pytest.raises(Conflict):
        au.upsert_audience(conn, pid, "work", rename_to="everyone")
    with pytest.raises(NotFound):
        au.upsert_audience(conn, pid, "ghost", rename_to="x")
    for bad in ["Work", "-x", "a" * 41, "with space", ""]:
        with pytest.raises(Invalid):
            au.upsert_audience(conn, pid, bad)


def test_exactly_one_default(conn, pid):
    au.upsert_audience(conn, pid, "family", is_default=True)
    assert defaults(conn, pid) == ["family"]
    au.upsert_audience(conn, pid, "everyone", is_default=True)
    assert defaults(conn, pid) == ["everyone"]
    with pytest.raises(Invalid):
        au.upsert_audience(conn, pid, "everyone", is_default=False)
    au.upsert_audience(conn, pid, "everyone", rename_to="strangers")
    assert defaults(conn, pid) == ["strangers"]
    with pytest.raises(sqlite3.IntegrityError):  # the partial unique index backs the invariant
        conn.execute("UPDATE audiences SET is_default = 1 WHERE persona_id = ?", (pid,))


def test_delete_audience(conn, pid):
    au.upsert_audience(conn, pid, "family")
    au.upsert_audience(conn, pid, "friends")
    only, both, _ = add_claims(
        conn,
        pid,
        [
            {"kind": "style", "category": "family", "text": "Only family", "audiences": ["family"]},
            {
                "kind": "style",
                "category": "family",
                "text": "Both",
                "audiences": ["family", "friends"],
            },
            {"kind": "style", "category": "family", "text": "All"},
        ],
    )["created"]
    counts = {a["name"]: a["claim_count"] for a in au.list_audiences(conn, pid)}
    assert counts == {"everyone": 0, "family": 2, "friends": 1}
    with pytest.raises(Conflict) as exc:
        au.delete_audience(conn, pid, "everyone")
    with pytest.raises(Conflict) as exc:
        au.delete_audience(conn, pid, "family")
    assert exc.value.details["claim_count"] == 1
    with pytest.raises(NotFound):
        au.delete_audience(conn, pid, "family", reassign_to="ghost")
    assert au.delete_audience(conn, pid, "family", reassign_to="friends") == {
        "deleted": "family",
        "reassigned": 1,
    }
    assert get_claim(conn, pid, only)["audiences"] == ["friends"]
    assert get_claim(conn, pid, both)["audiences"] == ["friends"]
    au.upsert_audience(conn, pid, "empty")
    assert au.delete_audience(conn, pid, "empty")["reassigned"] == 0


# --- categories --------------------------------------------------------------------------


def paths(conn, pid):
    return set(ca.category_ids_by_path(conn, pid))


def test_category_create_requires_parent_and_depth(conn, pid):
    assert ca.upsert_category(conn, pid, "work/meetings/one-on-one", description="1:1s")["created"]
    with pytest.raises(NotFound):
        ca.upsert_category(conn, pid, "garden/roses")
    for bad in ["a/b/c/d", "Work", "work//x", "work/", "a b"]:
        with pytest.raises(Invalid):
            ca.upsert_category(conn, pid, bad)
    res = ca.upsert_category(conn, pid, "food", description="Eating")
    assert not res["created"] and res["category"]["description"] == "Eating"


def test_category_rename_moves_subtree(conn, pid):
    ca.upsert_category(conn, pid, "work/meetings/one-on-one")
    cid = add_claims(
        conn,
        pid,
        [
            {
                "kind": "habit",
                "category": "work/meetings/one-on-one",
                "text": "Weekly 1:1s",
                "proposed_category": "work/meetings/one-on-one",
            }
        ],
    )["created"][0]
    ca.upsert_category(conn, pid, "work/meetings", rename_to="schedule/meetings")
    assert "schedule/meetings/one-on-one" in paths(conn, pid)
    assert not any(p.startswith("work/meetings") for p in paths(conn, pid))
    c = get_claim(conn, pid, cid)
    assert c["category"] == "schedule/meetings/one-on-one"
    assert c["proposed_category"] == "schedule/meetings/one-on-one"
    row = conn.execute(
        "SELECT p.path FROM categories c JOIN categories p ON p.id = c.parent_id"
        " WHERE c.persona_id = ? AND c.path = 'schedule/meetings'",
        (pid,),
    ).fetchone()
    assert row[0] == "schedule"
    change = conn.execute("SELECT op, payload FROM changes WHERE persona_id = ?", (pid,)).fetchone()
    assert change["op"] == "category_rename"
    assert {"from": "work/meetings", "to": "schedule/meetings"} in json.loads(change["payload"])[
        "renamed"
    ]
    # top-level move, too deep, into own subtree, onto existing, missing parent
    ca.upsert_category(conn, pid, "schedule/meetings", rename_to="meetings")
    assert {"meetings", "meetings/one-on-one"} <= paths(conn, pid)
    with pytest.raises(Invalid):
        ca.upsert_category(conn, pid, "meetings", rename_to="work/decisions/meetings")
    with pytest.raises(Invalid):
        ca.upsert_category(conn, pid, "meetings", rename_to="meetings/one-on-one/x")
    with pytest.raises(Conflict):
        ca.upsert_category(conn, pid, "meetings", rename_to="food")
    with pytest.raises(NotFound):
        ca.upsert_category(conn, pid, "meetings", rename_to="nothing/meetings")


def test_merge_categories(conn, pid):
    ca.upsert_category(conn, pid, "food/drinks")
    ids = add_claims(
        conn,
        pid,
        [
            {"kind": "preference", "category": "food", "text": "Vegetarian"},
            {"kind": "preference", "category": "food/drinks", "text": "No coffee after 3pm"},
            {"kind": "preference", "category": "travel", "text": "Window seat"},
        ],
    )["created"]
    res = ca.merge_categories(conn, pid, ["food", "food/drinks", "travel"], "hobbies")
    assert res["moved_claims"] == 3
    assert set(res["removed_categories"]) == {"food", "food/drinks", "travel"}
    assert not {"food", "food/drinks", "travel"} & paths(conn, pid)
    assert {get_claim(conn, pid, i)["category"] for i in ids} == {"hobbies"}
    row = conn.execute("SELECT * FROM changes WHERE id = ?", (res["change_id"],)).fetchone()
    assert (row["op"], row["state"]) == ("category_merge", "applied")
    before = json.loads(row["payload"])["before"]
    assert {c["id"]: c["category"] for c in before["claims"]} == {
        ids[0]: "food",
        ids[1]: "food/drinks",
        ids[2]: "travel",
    }
    tree = {n["path"]: n for n in ca.list_categories(conn, pid)}
    assert tree["hobbies"]["claim_count"] == 3
    with pytest.raises(Invalid):
        ca.merge_categories(conn, pid, ["hobbies"], "hobbies")
    with pytest.raises(Invalid):
        ca.merge_categories(conn, pid, ["work"], "work/meetings")
    with pytest.raises(NotFound):
        ca.merge_categories(conn, pid, ["ghost"], "hobbies")


def test_list_categories_tree(conn, pid):
    add_claims(conn, pid, [{"kind": "habit", "category": "work/meetings", "text": "Short calls"}])
    tree = {n["path"]: n for n in ca.list_categories(conn, pid)}
    work = tree["work"]
    assert work["claim_count"] == 0 and work["total_claim_count"] == 1
    assert [c["path"] for c in work["children"]] == ["work/decisions", "work/meetings"]
    assert all(n["description"] for n in tree.values())


# --- the "default" alias (SPEC §4.3) ---------------------------------------------------------


def test_default_alias_is_reserved(conn, pid):
    au.upsert_audience(conn, pid, "work")
    with pytest.raises(Invalid) as exc:
        au.upsert_audience(conn, pid, "work", rename_to="default")
    assert exc.value.details["field"] == "rename_to"
    assert {a["name"] for a in au.list_audiences(conn, pid)} == {"everyone", "work"}


def test_default_alias_resolves_to_default_audience(conn, pid):
    au.upsert_audience(conn, pid, "everyone", rename_to="anyone")
    out = au.upsert_audience(conn, pid, "default", description="whoever")
    assert out["created"] is False and out["audience"]["name"] == "anyone"
    assert au.get_audience(conn, pid, "default")["name"] == "anyone"
    created = add_claims(
        conn,
        pid,
        [{"kind": "trait", "category": "food", "text": "Likes tea.", "audiences": ["default"]}],
    )["created"]
    assert get_claim(conn, pid, created[0])["audiences"] == ["anyone"]
    with pytest.raises(Conflict):
        au.delete_audience(conn, pid, "default")


def test_unknown_category_points_to_list_categories(conn, pid):
    out = add_claims(conn, pid, [{"kind": "trait", "category": "nope", "text": "Likes tea."}])
    assert out["dropped"][0]["reason"] == "invalid"
    assert "list_categories" in out["dropped"][0]["detail"]
