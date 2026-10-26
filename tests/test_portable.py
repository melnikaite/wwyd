# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import copy
from importlib import resources
from pathlib import Path

import pytest

from wwyd.errors import Conflict, Invalid
from wwyd.store import portable
from wwyd.store.audiences import upsert_audience
from wwyd.store.categories import upsert_category
from wwyd.store.claims import add_claims, get_claim, review_claims, update_claim
from wwyd.store.personas import create_user, delete_persona, update_settings

REPO = Path(__file__).resolve().parents[1]


def strip(doc):
    doc = copy.deepcopy(doc)
    doc.pop("exported_at")
    return doc


def populate(conn, pid):
    update_settings(conn, pid, display_name="Alex", timezone="Europe/Berlin")
    upsert_audience(conn, pid, "family", description="Parents and siblings")
    upsert_audience(conn, pid, "work-slack", description="Colleagues", is_default=True)
    upsert_category(conn, pid, "hobbies/chess", description="Chess")
    ids = add_claims(
        conn,
        pid,
        [
            {
                "kind": "style",
                "category": "communication/style",
                "text": "Short, no greeting.",
                "audiences": ["work-slack"],
                "status": "approved",
                "evidence": {
                    "sources": {"message": 40},
                    "share": 0.9,
                    "period": "2026-04..2026-10",
                    "stats": {"len_hist": [1, 2, 3]},
                },
            },
            {
                "kind": "boundary",
                "category": "communication/boundaries",
                "text": "Never discusses salary in writing.",
                "visibility": "owner_only",
                "status": "approved",
                "confidence": 0.95,
            },
            {
                "kind": "habit",
                "category": "hobbies/chess",
                "text": "Plays chess on Sundays.",
                "audiences": ["family", "work-slack"],
            },
            {"kind": "trait", "category": "food", "text": "Hates cilantro."},
            {"kind": "trait", "category": "travel", "text": "Old habit."},
        ],
        allow_approved=True,
    )["created"]
    review_claims(conn, pid, reject=[ids[3]])
    update_claim(conn, pid, ids[4], status="archived")
    return ids


def test_packaged_schema_matches_published_copy():
    packaged = resources.files("wwyd.schema").joinpath("persona.v1.json").read_text()
    assert packaged == (REPO / "schema" / "persona.v1.json").read_text()


def test_export_validates_against_schema(conn, pid):
    populate(conn, pid)
    doc = portable.export_persona(conn, pid)
    portable.validate_document(doc)
    assert doc["format"] == "wwyd.persona" and doc["version"] == 1
    assert doc["persona"] == {
        "display_name": "Alex",
        "timezone": "Europe/Berlin",
        "weekend_days": ["saturday", "sunday"],
    }
    statuses = sorted(c["status"] for c in doc["claims"])
    assert statuses == ["approved", "approved", "archived", "pending", "rejected"]


@pytest.mark.parametrize("mode", ["replace", "merge"])
def test_round_trip_through_delete_persona(conn, user, mode):
    pid = user.persona_id
    populate(conn, pid)
    before = portable.export_persona(conn, pid)
    delete_persona(conn, pid, user.login)
    counts = portable.import_persona(conn, pid, before, mode=mode)
    assert counts["claims_created"] == 5
    after = portable.export_persona(conn, pid)
    assert strip(after) == strip(before)
    # FTS works on imported claims
    from wwyd.store.claims import search_claims

    assert len(search_claims(conn, pid, "salary")["claims"]) == 1


def test_merge_updates_existing_claims(conn, pid):
    ids = populate(conn, pid)
    doc = portable.export_persona(conn, pid)
    doc["claims"][0]["text"] = "Short, never a greeting."
    doc["audiences"].append({"name": "friends", "description": "", "is_default": False})
    counts = portable.import_persona(conn, pid, doc)
    assert counts["claims_updated"] == 5 and counts["claims_created"] == 0
    assert counts["audiences_created"] == 1
    assert get_claim(conn, pid, ids[0])["text"] == "Short, never a greeting."


def test_import_into_other_persona_gets_new_ids(conn, pid):
    ids = populate(conn, pid)
    doc = portable.export_persona(conn, pid)
    other = create_user(conn, "other@example.com")
    counts = portable.import_persona(conn, other.persona_id, doc)
    assert counts["claims_created"] == 5
    other_ids = {c["id"] for c in portable.export_persona(conn, other.persona_id)["claims"]}
    assert not other_ids & set(ids)
    assert get_claim(conn, pid, ids[0])["text"] == "Short, no greeting."


def test_legacy_visibility_is_normalised(conn, pid):
    doc = {
        "format": "wwyd.persona",
        "version": 1,
        "persona": {},
        "audiences": [],
        "categories": [],
        "claims": [
            {"kind": "trait", "category": "food", "text": "Likes tea", "visibility": "anyone"},
            {"kind": "trait", "category": "food", "text": "Likes figs", "visibility": "audiences"},
        ],
    }
    portable.import_persona(conn, pid, doc)
    exported = portable.export_persona(conn, pid)["claims"]
    assert {c["visibility"] for c in exported} == {"shared"}
    assert {c["origin"] for c in exported} == {"imported"}


def base_doc():
    return {
        "format": "wwyd.persona",
        "version": 1,
        "persona": {},
        "audiences": [],
        "categories": [],
        "claims": [],
    }


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(format="other"),
        lambda d: d.update(version=2),
        lambda d: d.update(extra=True),
        lambda d: d["claims"].append({"kind": "mood", "category": "food", "text": "x"}),
        lambda d: d["claims"].append(
            {"kind": "trait", "category": "food", "text": "x", "contact_email": "a@b.c"}
        ),
        lambda d: d["claims"].append({"kind": "trait", "category": "Food", "text": "x"}),
        lambda d: d["claims"].append({"kind": "trait", "category": "food", "text": "x" * 501}),
        lambda d: d["claims"].append(
            {"kind": "trait", "category": "food", "text": "x", "evidence": {"names": ["Bob"]}}
        ),
        lambda d: d["claims"].append(
            {"kind": "trait", "category": "food", "text": "x", "visibility": "public"}
        ),
        lambda d: d["audiences"].append({"name": "Bad Name"}),
        lambda d: d.pop("claims"),
    ],
)
def test_schema_failures(conn, pid, mutate):
    doc = base_doc()
    mutate(doc)
    with pytest.raises(Invalid) as exc:
        portable.import_persona(conn, pid, doc)
    assert exc.value.details["errors"]


def test_reference_failures_roll_back(conn, pid):
    doc = base_doc()
    doc["claims"] = [
        {"kind": "trait", "category": "food", "text": "fine"},
        {"kind": "trait", "category": "garden", "text": "unknown category"},
    ]
    with pytest.raises(Invalid):
        portable.import_persona(conn, pid, doc)
    assert portable.export_persona(conn, pid)["claims"] == []
    doc["claims"] = [{"kind": "trait", "category": "food", "text": "x", "audiences": ["ghost"]}]
    with pytest.raises(Invalid):
        portable.import_persona(conn, pid, doc)
    doc["claims"] = [{"kind": "trait", "category": "food", "text": "mail x@example.com"}]
    with pytest.raises(Invalid):
        portable.import_persona(conn, pid, doc)
    doc["claims"] = []
    doc["audiences"] = [{"name": "a", "is_default": True}, {"name": "b", "is_default": True}]
    with pytest.raises(Invalid):
        portable.import_persona(conn, pid, doc)
    doc = base_doc()
    doc["claims"] = [
        {
            "kind": "habit",
            "category": "food",
            "text": "one",
            "key": "habit:x",
            "status": "approved",
        },
        {
            "kind": "habit",
            "category": "food",
            "text": "two",
            "key": "habit:x",
            "status": "approved",
        },
    ]
    with pytest.raises(Conflict):
        portable.import_persona(conn, pid, doc)


def test_replace_without_default_creates_everyone(conn, pid):
    doc = base_doc()
    doc["audiences"] = [{"name": "family"}]
    doc["categories"] = [{"path": "food/fruit"}]
    portable.import_persona(conn, pid, doc, mode="replace")
    exported = portable.export_persona(conn, pid)
    assert [(a["name"], a["is_default"]) for a in exported["audiences"]] == [
        ("everyone", True),
        ("family", False),
    ]
    assert [c["path"] for c in exported["categories"]] == ["food", "food/fruit"]


# --- keyed claims and analyzers survive export/import (SPEC §4.7) ----------------------------


def _keyed(conn, pid):
    rows = conn.execute(
        "SELECT key, status FROM claims WHERE persona_id = ? AND key IS NOT NULL", (pid,)
    ).fetchall()
    return sorted((r["key"], r["status"]) for r in rows)


def _analyze(conn, pid, days, start_index=0):
    from analyzer_data import BERLIN, cat_id, meetings, replies

    from wwyd.analyzers import run_analyzers

    eps = meetings(cat_id(conn, pid), days, start_index=start_index)
    eps += replies(cat_id(conn, pid, "communication"), days, "colleagues")
    for i, ep in enumerate(eps):
        ep.index = start_index + i
    run_analyzers(conn, persona_id=pid, episodes=eps, tz=BERLIN, counts={})


def test_keyed_claims_round_trip_into_fresh_persona(conn, pid):
    from datetime import date

    from analyzer_data import add_audience, cat_id, workdays

    from wwyd.analyzers.state import read_state

    add_audience(conn, pid, "colleagues")
    days = workdays(date(2026, 9, 7), 60)
    _analyze(conn, pid, days[:30])
    before = _keyed(conn, pid)
    assert any(":aud_" in k for k, _ in before) and any(":cat_" in k for k, _ in before)
    doc = portable.export_persona(conn, pid)
    exported = [c["key"] for c in doc["claims"] if c.get("key")]
    assert exported and not any("cat_" in k or "aud_" in k for k in exported)
    assert "builtin:time_gaps:{category:work/meetings}" in exported

    other = create_user(conn, "fresh@example.com").persona_id
    res = portable.import_persona(conn, other, doc, mode="replace")
    assert res["keys_dropped"] == 0
    meet_old, meet_new = cat_id(conn, pid), cat_id(conn, other)
    aud_ids = {
        r["persona_id"]: r["id"]
        for r in conn.execute("SELECT persona_id, id FROM audiences WHERE name = 'colleagues'")
    }

    def local(key):
        return key.replace(meet_old, meet_new).replace(aud_ids[pid], aud_ids[other])

    expected = sorted((local(k), s) for k, s in before)
    expected = sorted(
        (k.replace(cat_id(conn, pid, "communication"), cat_id(conn, other, "communication")), s)
        for k, s in expected
    )
    assert _keyed(conn, other) == expected
    gap_id = conn.execute(
        "SELECT id FROM analyzers WHERE persona_id = ? AND name = 'time_gaps' AND scope_id = ?",
        (other, meet_new),
    ).fetchone()[0]
    n_before = sum(read_state(conn, gap_id)["gap"]["2026-09"].values())
    _analyze(conn, other, days[30:], start_index=10_000)
    assert len(_keyed(conn, other)) == len(expected)  # re-rendered, not duplicated
    state = read_state(conn, gap_id)["gap"]
    assert sum(v for m in state.values() for v in m.values()) > n_before

    # merge mode into the same persona: the same claims and analyzers, no new ones
    res = portable.import_persona(conn, pid, doc, mode="merge")
    assert res["keys_dropped"] == 0 and res["analyzers_created"] == 0
    assert _keyed(conn, pid) == before


def test_llm_analyzer_key_and_claim_link_and_limit(conn, pid, user):
    from wwyd.limits import get_limiter

    cat = conn.execute(
        "SELECT id FROM categories WHERE persona_id = ? AND path = 'food'", (pid,)
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO analyzers(id, persona_id, scope_kind, scope_id, origin, name,"
        " statement_template, kind, source, status, created_at)"
        " VALUES ('anl_aaaa', ?, 'category', ?, 'llm', 'snacks', 'Snacks', 'habit', 'x',"
        " 'active', '2026-10-01T00:00:00Z')",
        (pid, cat),
    )
    claim = add_claims(conn, pid, [{"kind": "habit", "category": "food", "text": "Snacks"}])
    cid = claim["created"][0]
    conn.execute("UPDATE claims SET key = 'anl:anl_aaaa' WHERE id = ?", (cid,))
    conn.execute("UPDATE analyzers SET claim_id = ? WHERE id = 'anl_aaaa'", (cid,))
    conn.commit()
    doc = portable.export_persona(conn, pid)
    assert [c["key"] for c in doc["claims"] if c.get("key")] == ["anl:{analyzer:0}"]

    other = create_user(conn, "llm@example.com")
    portable.import_persona(conn, other.persona_id, doc, mode="replace")
    row = conn.execute(
        "SELECT id, claim_id FROM analyzers WHERE persona_id = ?", (other.persona_id,)
    ).fetchone()
    key, claim_id = conn.execute(
        "SELECT key, id FROM claims WHERE persona_id = ? AND key IS NOT NULL",
        (other.persona_id,),
    ).fetchone()
    assert key == f"anl:{row['id']}" and row["claim_id"] == claim_id

    # max_analyzers: excess non-retired LLM analyzers are skipped and counted
    third = create_user(conn, "limit@example.com")
    get_limiter(conn).set_own_limits(third.user_id, {"max_analyzers": 0})
    res = portable.import_persona(conn, third.persona_id, doc, mode="replace")
    assert res["analyzers_skipped"] == 1 and res["keys_dropped"] == 1
    doc["analyzers"][0]["status"] = "retired"
    res = portable.import_persona(conn, third.persona_id, doc, mode="replace")
    assert res["analyzers_created"] == 1
