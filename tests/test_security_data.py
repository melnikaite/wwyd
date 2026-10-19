# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regression tests for the data-side security review."""

from __future__ import annotations

import time

import pytest
from conftest import call, call_error

from wwyd.auth import tokens
from wwyd.auth.scopes import parse_scope
from wwyd.errors import Invalid
from wwyd.ingest.guard import find_identifier
from wwyd.ingest.records import validate_record
from wwyd.mcp.caller import Caller
from wwyd.predict import predict
from wwyd.store.audiences import delete_audience, upsert_audience
from wwyd.store.categories import upsert_category
from wwyd.store.claims import add_claims, list_claims, search_claims
from wwyd.store.personas import delete_persona
from wwyd.store.portable import export_persona, import_persona

pytestmark = pytest.mark.anyio


def approved(conn, pid, items):
    out = add_claims(
        conn, pid, [{"status": "approved", **i} for i in items], absorb=False, allow_approved=True
    )
    assert not out["dropped"], out["dropped"]
    return out["created"]


# --- list_claims category filter escapes LIKE wildcards ----------------------------------------


def test_list_claims_category_like_is_escaped(conn, pid):
    for path in ("food_x", "foodax", "foodax/sub"):
        upsert_category(conn, pid, path)
    approved(conn, pid, [{"kind": "trait", "category": "foodax/sub", "text": "Likes tea."}])
    assert list_claims(conn, pid, category="food_x")["claims"] == []
    assert len(list_claims(conn, pid, category="foodax")["claims"]) == 1


# --- bounded tool parameters -------------------------------------------------------------------


async def test_list_params_are_bounded(client):
    many = [f"clm_{i}" for i in range(1001)]
    assert (await call_error(client, "delete_claims", {"ids": many}))["code"] == "too_large"
    assert (await call_error(client, "review_claims", {"approve": many}))["code"] == "too_large"
    assert (await call_error(client, "review_claims", {"reject": many}))["code"] == "too_large"
    paths = [f"p{i}" for i in range(1001)]
    assert (await call_error(client, "get_persona", {"categories": paths}))["code"] == "too_large"
    err = await call_error(client, "list_claims", {"cursor": "x" * 10_000})
    assert err["code"] == "invalid"
    err = await call_error(client, "delete_claims", {"ids": ["x" * 10_000]})
    assert err["code"] == "invalid"


async def test_tool_schemas_declare_bounds(client):
    tools = {t.name: t.input_schema for t in (await client.list_tools()).tools}

    def bounded(schema: dict) -> bool:
        if "anyOf" in schema:
            return all(bounded(s) for s in schema["anyOf"] if s.get("type") != "null")
        if schema.get("type") == "array":
            return "maxItems" in schema
        if schema.get("type") == "string":
            return "maxLength" in schema or "enum" in schema or "const" in schema
        return True

    for name, params in (
        ("delete_claims", ["ids"]),
        ("review_claims", ["approve", "reject"]),
        ("get_persona", ["categories", "audience"]),
        ("search_claims", ["query", "audience"]),
        ("undo_change", ["id"]),
        ("job_status", ["job_id"]),
        ("ingest_raw", ["records"]),
    ):
        for param in params:
            assert bounded(tools[name]["properties"][param]), (name, param)


# --- guard regexes run in linear time ----------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    ["a." * 2000, "a-" * 2000, "a" * 4000, "x." * 2000 + "://", "_a." * 1400, "a@" * 2000],
)
def test_guard_is_linear(text):
    start = time.perf_counter()
    for _ in range(10):
        find_identifier(text * 5)  # 20k chars
    assert time.perf_counter() - start < 0.5


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("mail john.doe+x@mail.example.com now", "email"),
        ("see https://x.io/a", "url"),
        ("go to www.foo.bar", "url"),
        ("visit example.com/path", "url"),
        ("a..example.org", "url"),
        ("foo_bar.example.com", "url"),
        ("Done. example.org", "url"),
        ("version 1.2.3, e.g. this", None),
    ],
)
def test_guard_still_finds_identifiers(text, kind):
    assert find_identifier(text) == kind


# --- predict audience fallback ---------------------------------------------------------------


async def test_fallback_hint_names_default_only_for_owner(conn, user, pid, fake_llm):
    upsert_audience(conn, pid, "work")
    approved(conn, pid, [{"kind": "style", "category": "communication", "text": "Writes short."}])
    fake_llm.on(
        "answer",
        lambda call: {
            "type": "answer",
            "answer": "ok",
            "confidence": 0.9,
            "used_claims": [],
            "language": "en",
            "absent_facts": [],
            "escalate": "",
            "reason": "",
        },
    )
    respond = Caller(user.user_id, pid, "respond", audiences=("work",))
    out = await predict(conn, respond, situation="Hi", audience="nope")
    assert out["escalate"] == "audience_not_permitted"
    assert not any("everyone" in h for h in out["hints"])
    out = await predict(conn, Caller(user.user_id, pid, "owner"), situation="Hi", audience="nope")
    assert any("'everyone'" in h for h in out["hints"])


async def test_predict_without_default_audience_escalates(conn, user, pid, fake_llm):
    conn.execute("DELETE FROM audiences WHERE persona_id = ?", (pid,))  # hand-edited DB
    out = await predict(conn, Caller(user.user_id, pid, "owner"), situation="Hi", audience="x")
    assert out["type"] == "escalate" and out["escalate"] == "out_of_persona"
    assert fake_llm.calls == []


def test_replace_import_keeps_a_default_audience(conn, pid):
    doc = export_persona(conn, pid)
    doc["audiences"] = [{"name": "work", "description": "", "is_default": False}]
    import_persona(conn, pid, doc, mode="replace")
    row = conn.execute(
        "SELECT name FROM audiences WHERE persona_id = ? AND is_default = 1", (pid,)
    ).fetchone()
    assert row is not None


# --- ingest validation does not echo caller keys -----------------------------------------------


def test_unknown_field_error_has_no_key_name():
    secret_key = "alice_smith_phone"
    with pytest.raises(Invalid) as info:
        validate_record(3, {"type": "note", "text": "hi", secret_key: "x"})
    assert secret_key not in str(info.value.to_payload())
    assert info.value.details == {"index": 3, "path": ""}
    turns = [{"speaker": "owner", "text": "hi", secret_key: 1}]
    with pytest.raises(Invalid) as info:
        validate_record(0, {"type": "call_transcript", "ts": "2026-01-01T10:00", "turns": turns})
    assert secret_key not in str(info.value.to_payload())


# --- audience deletion / rename revokes respond tokens -----------------------------------------


def _key(conn, user, scope):
    return tokens.create_api_key(conn, user.user_id, "k", parse_scope(scope))[1]


def test_audience_delete_and_rename_revoke_tokens(conn, user, pid):
    for name in ("family", "work", "friends"):
        upsert_audience(conn, pid, name)
    fam = _key(conn, user, "respond:family")
    work = _key(conn, user, "respond:work")
    friends = _key(conn, user, "respond:friends")
    delete_audience(conn, pid, "family")
    assert tokens.verify_bearer(conn, fam) is None
    assert tokens.verify_bearer(conn, work) is not None
    upsert_audience(conn, pid, "work", rename_to="office")
    assert tokens.verify_bearer(conn, work) is None
    assert tokens.verify_bearer(conn, friends) is not None
    # Replace-import that drops "friends" revokes it; delete_persona revokes the rest.
    doc = export_persona(conn, pid)
    doc["audiences"] = [a for a in doc["audiences"] if a["name"] != "friends"]
    import_persona(conn, pid, doc, mode="replace")
    assert tokens.verify_bearer(conn, friends) is None
    everyone = _key(conn, user, "respond:everyone")
    office = _key(conn, user, "respond:office")
    delete_persona(conn, pid, "owner@example.com")
    assert tokens.verify_bearer(conn, office) is None
    assert tokens.verify_bearer(conn, everyone) is not None  # re-seeded with the same name


# --- search score ------------------------------------------------------------------------------


async def test_search_score_is_relative(conn, pid, client):
    approved(
        conn,
        pid,
        [
            {"kind": "trait", "category": "food", "text": "Drinks green tea every morning."},
            {"kind": "trait", "category": "food", "text": "Prefers tea over coffee, tea always."},
            {"kind": "trait", "category": "food", "text": "Eats oatmeal."},
        ],
    )
    hits = search_claims(conn, pid, "tea")["claims"]
    assert len(hits) == 2 and hits[0]["score"] == 1.0
    assert all(0 < h["score"] <= 1 for h in hits)
    out = await call(client, "search_claims", {"query": "oatmeal"})
    assert [c["score"] for c in out["claims"]] == [1.0]


def test_respond_scope_cannot_name_the_default_alias():
    assert parse_scope("respond:default") is None
    assert parse_scope("respond:work,default") is None
    assert parse_scope("respond:work") is not None
