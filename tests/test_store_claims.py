# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import pytest

from wwyd.errors import Conflict, Invalid, NotFound, TooLarge
from wwyd.store import claims as cl
from wwyd.store.audiences import upsert_audience
from wwyd.store.categories import merge_categories


def add(conn, pid, *items, **kw):
    return cl.add_claims(conn, pid, list(items), **kw)


def claim(text="Short, no greeting.", kind="style", category="communication/style", **kw):
    return {"kind": kind, "category": category, "text": text, **kw}


def fts_ids(conn, word):
    return {
        r[0]
        for r in conn.execute(
            "SELECT c.id FROM claims_fts JOIN claims c ON c.seq = claims_fts.rowid"
            " WHERE claims_fts MATCH ?",
            (f'"{word}"',),
        )
    }


def test_add_defaults_and_get(conn, pid):
    res = add(conn, pid, claim())
    assert res["absorbed"] == [] and res["dropped"] == []
    c = cl.get_claim(conn, pid, res["created"][0])
    assert c["status"] == "pending" and c["origin"] == "added"
    assert c["audiences"] == "all" and c["visibility"] == "shared"
    assert c["confidence"] == 0.5 and c["evidence"] == {"sources": {}}
    assert c["created_at"].endswith("Z")


def test_add_approved_only_when_allowed(conn, pid):
    a = add(conn, pid, claim("A", status="approved"))["created"][0]
    b = add(conn, pid, claim("B", status="approved"), allow_approved=True)["created"][0]
    assert cl.get_claim(conn, pid, a)["status"] == "pending"
    assert cl.get_claim(conn, pid, b)["status"] == "approved"


def test_add_drops_with_reasons(conn, pid):
    upsert_audience(conn, pid, "family")
    res = add(
        conn,
        pid,
        claim("Fine", audiences=["family"]),
        claim("Unknown category", category="nope"),
        claim("Unknown audience", audiences=["work"]),
        claim("Mail me at x@example.com"),
        claim("Bad kind", kind="mood"),
        claim("x" * 501),
        claim("Evidence", evidence={"sources": {"message": 1}, "names": ["Bob"]}),
    )
    assert len(res["created"]) == 1
    reasons = {d["index"]: d["reason"] for d in res["dropped"]}
    assert reasons == {
        1: "invalid",
        2: "unknown_audience",
        3: "guard",
        4: "invalid",
        5: "invalid",
        6: "invalid",
    }
    assert cl.get_claim(conn, pid, res["created"][0])["audiences"] == ["family"]


def test_add_limit(conn, pid):
    with pytest.raises(TooLarge):
        add(conn, pid, *[claim(str(i)) for i in range(201)])


def test_absorb_merges_evidence(conn, pid):
    first = add(
        conn,
        pid,
        claim(
            "Short, no greeting.",
            confidence=0.4,
            evidence={"sources": {"message": 3}, "period": "2026-04..2026-06"},
        ),
    )["created"][0]
    res = add(
        conn,
        pid,
        claim(
            "  short,   NO greeting!! ",
            confidence=0.7,
            evidence={"sources": {"message": 2, "note": 1}, "period": "2026-02..2026-05"},
        ),
    )
    assert res["created"] == [] and res["absorbed"] == [{"index": 0, "into": first}]
    c = cl.get_claim(conn, pid, first)
    assert c["evidence"] == {"sources": {"message": 5, "note": 1}, "period": "2026-02..2026-06"}
    assert c["confidence"] == 0.76  # goes up (SPEC §10.1)
    assert c["text"] == "Short, no greeting."


def test_absorb_requires_same_kind_and_category_and_can_be_disabled(conn, pid):
    add(conn, pid, claim("Same text"))
    assert add(conn, pid, claim("Same text", kind="trait"))["created"]
    assert add(conn, pid, claim("Same text", category="work"))["created"]
    assert add(conn, pid, claim("Same text"), absorb=False)["created"]


def test_absorb_within_one_batch(conn, pid):
    res = add(conn, pid, claim("Twice"), claim("twice."))
    assert len(res["created"]) == 1 and res["absorbed"][0]["into"] == res["created"][0]


def test_previously_rejected_is_dropped(conn, pid):
    cid = add(conn, pid, claim("Hates mornings", kind="trait", category="schedule/routine"))[
        "created"
    ][0]
    cl.review_claims(conn, pid, reject=[cid])
    assert cl.get_claim(conn, pid, cid)["evidence"] == {}
    res = add(conn, pid, claim("hates mornings.", kind="trait", category="schedule/routine"))
    assert res["dropped"] == [{"index": 0, "reason": "previously_rejected"}]
    res = add(
        conn, pid, claim("hates mornings", kind="trait", category="schedule/routine"), absorb=False
    )
    assert res["dropped"][0]["reason"] == "previously_rejected"


def test_list_filters_and_pagination(conn, pid):
    upsert_audience(conn, pid, "work")
    ids = add(
        conn,
        pid,
        *[claim(f"Claim {i}", status="approved") for i in range(5)],
        claim(
            "For work",
            status="approved",
            audiences=["work"],
            kind="boundary",
            category="communication/boundaries",
        ),
        claim("Pending one"),
        allow_approved=True,
    )["created"]
    page1 = cl.list_claims(conn, pid, limit=4)
    assert [c["id"] for c in page1["claims"]] == ids[:4]
    page2 = cl.list_claims(conn, pid, limit=4, cursor=page1["next_cursor"])
    assert [c["id"] for c in page2["claims"]] == ids[4:6]
    assert "next_cursor" not in page2
    assert len(cl.list_claims(conn, pid, status="any")["claims"]) == 7
    assert [c["id"] for c in cl.list_claims(conn, pid, status="pending")["claims"]] == [ids[6]]
    assert [c["id"] for c in cl.list_claims(conn, pid, kind="boundary")["claims"]] == [ids[5]]
    assert len(cl.list_claims(conn, pid, category="communication")["claims"]) == 6
    assert len(cl.list_claims(conn, pid, audience="work")["claims"]) == 6  # "all" applies too
    assert len(cl.list_claims(conn, pid, audience="everyone")["claims"]) == 5
    with pytest.raises(Invalid):
        cl.list_claims(conn, pid, limit=201)
    with pytest.raises(Invalid):
        cl.list_claims(conn, pid, cursor="garbage!")
    with pytest.raises(NotFound):
        cl.list_claims(conn, pid, audience="nobody")


def test_list_archived_needs_flag(conn, pid):
    cid = add(conn, pid, claim("Old habit"))["created"][0]
    cl.update_claim(conn, pid, cid, status="archived")
    assert cl.list_claims(conn, pid, status="any")["claims"] == []
    assert len(cl.list_claims(conn, pid, status="any", include_archived=True)["claims"]) == 1
    assert len(cl.list_claims(conn, pid, status="archived")["claims"]) == 1


def test_search_ranks_and_escapes(conn, pid):
    ids = add(
        conn,
        pid,
        claim("Replies to colleagues within minutes", status="approved"),
        claim(
            "Never discusses salary in writing",
            kind="boundary",
            category="communication/boundaries",
            status="approved",
        ),
        claim(
            "Prefers café meetings", status="approved", category="work/meetings", kind="preference"
        ),
        allow_approved=True,
    )["created"]
    res = cl.search_claims(conn, pid, "salary")
    assert [c["id"] for c in res["claims"]] == [ids[1]]
    assert res["claims"][0]["score"] > 0
    # FTS syntax characters are treated as text, never raise
    for q in ['salary"', "AND OR NOT", "(salary", "sal*", "col:umn", "^-+", "NEAR(a b)"]:
        cl.search_claims(conn, pid, q)
    assert cl.search_claims(conn, pid, "!!!")["claims"] == []
    # diacritics are folded
    assert [c["id"] for c in cl.search_claims(conn, pid, "cafe")["claims"]] == [ids[2]]
    # default status filter is approved
    pending = add(conn, pid, claim("salary talk pending"))["created"][0]
    assert pending not in {c["id"] for c in cl.search_claims(conn, pid, "salary")["claims"]}
    assert pending in {
        c["id"] for c in cl.search_claims(conn, pid, "salary", status="any")["claims"]
    }


def test_search_is_scoped_to_persona(conn, pid):
    from wwyd.store.personas import create_user

    other = create_user(conn, "other@example.com")
    cl.add_claims(
        conn, other.persona_id, [claim("secret salary", status="approved")], allow_approved=True
    )
    assert cl.search_claims(conn, pid, "salary", status="any")["claims"] == []


def test_update_text_keeps_approved_and_sets_owner_origin(conn, pid):
    cid = add(conn, pid, claim("Short replies", status="approved"), allow_approved=True)["created"][
        0
    ]
    c = cl.update_claim(conn, pid, cid, text="Very short replies")
    assert c["status"] == "approved" and c["origin"] == "owner"
    assert fts_ids(conn, "very") == {cid}
    assert fts_ids(conn, "short") == {cid}
    with pytest.raises(Invalid):
        cl.update_claim(conn, pid, cid, text="call +1 555 123 4567")


def test_update_fields(conn, pid):
    upsert_audience(conn, pid, "family")
    cid = add(conn, pid, claim("Some claim"))["created"][0]
    c = cl.update_claim(
        conn,
        pid,
        cid,
        kind="trait",
        category="family",
        audiences=["family"],
        visibility="owner_only",
        confidence=0.9,
        status="approved",
    )
    assert (c["kind"], c["category"], c["audiences"], c["visibility"], c["confidence"]) == (
        "trait",
        "family",
        ["family"],
        "owner_only",
        0.9,
    )
    assert c["reviewed_at"] is not None
    assert cl.update_claim(conn, pid, cid, audiences="all")["audiences"] == "all"
    with pytest.raises(NotFound):
        cl.update_claim(conn, pid, cid, audiences=["nobody"])
    with pytest.raises(NotFound):
        cl.update_claim(conn, pid, cid, category="nope")
    with pytest.raises(NotFound):
        cl.update_claim(conn, pid, "clm_missing", text="x")


def test_restore_window(conn, pid):
    cid = add(conn, pid, claim("Archived soon"))["created"][0]
    archived = cl.update_claim(conn, pid, cid, status="archived")
    assert archived["archive_reason"] == "owner" and archived["archived_at"]
    with pytest.raises(Conflict):
        cl.update_claim(conn, pid, cid, text="edit while archived")
    with pytest.raises(Invalid):
        cl.update_claim(conn, pid, cid, status="rejected")
    restored = cl.update_claim(conn, pid, cid, status="pending")
    assert restored["status"] == "pending" and restored["archived_at"] is None
    assert (
        conn.execute("SELECT op FROM changes WHERE persona_id = ?", (pid,)).fetchone()[0]
        == "restore"
    )
    cl.update_claim(conn, pid, cid, status="archived")
    conn.execute("UPDATE claims SET archived_at = '2020-01-01T00:00:00Z' WHERE id = ?", (cid,))
    with pytest.raises(Conflict):
        cl.update_claim(conn, pid, cid, status="approved")


def test_delete_removes_fts_rows(conn, pid):
    ids = add(conn, pid, claim("Zebra stripes"), claim("Zebra crossing"))["created"]
    assert fts_ids(conn, "zebra") == set(ids)
    assert cl.delete_claims(conn, pid, [ids[0], "clm_nope"]) == {"deleted": 1}
    assert fts_ids(conn, "zebra") == {ids[1]}
    assert conn.execute("SELECT COUNT(*) FROM claims_fts").fetchone()[0] == 1


def test_fts_follows_category_merge(conn, pid):
    cid = add(
        conn,
        pid,
        claim("Loves pizza", kind="preference", category="food", status="approved"),
        allow_approved=True,
    )["created"][0]
    merge_categories(conn, pid, ["food"], "hobbies")
    hits = cl.search_claims(conn, pid, "pizza")["claims"]
    assert [(c["id"], c["category"]) for c in hits] == [(cid, "hobbies")]
    conn.execute("INSERT INTO claims_fts(claims_fts) VALUES ('integrity-check')")


def test_review(conn, pid):
    a, b, c = add(conn, pid, claim("A"), claim("B"), claim("C"))["created"]
    res = cl.review_claims(conn, pid, approve=[a, "clm_missing", "chg_abc"], reject=[b])
    assert (res["approved"], res["rejected"]) == (1, 1)
    assert sorted(res["not_found"]) == ["chg_abc", "clm_missing"]
    assert res["applied_changes"] == 0 and res["dismissed_changes"] == 0
    assert cl.get_claim(conn, pid, a)["status"] == "approved"
    assert cl.get_claim(conn, pid, b)["status"] == "rejected"
    cl.update_claim(conn, pid, c, status="archived")
    assert cl.review_claims(conn, pid, approve=[c])["skipped"] == [c]
    with pytest.raises(Invalid):
        cl.review_claims(conn, pid, approve=[a], reject=[a])


def test_review_create_category(conn, pid):
    a, b = add(
        conn,
        pid,
        claim(
            "Plays chess on Sundays",
            kind="habit",
            category="hobbies",
            proposed_category="hobbies/games/chess",
        ),
        claim(
            "Collects stamps",
            kind="habit",
            category="hobbies",
            proposed_category="hobbies/collecting",
        ),
    )["created"]
    res = cl.review_claims(conn, pid, approve=[a], create_category=True)
    assert res["categories_created"] == ["hobbies/games/chess"]
    c = cl.get_claim(conn, pid, a)
    assert c["category"] == "hobbies/games/chess" and c["proposed_category"] is None
    cl.review_claims(conn, pid, approve=[b])  # without the flag: stays where it is
    c = cl.get_claim(conn, pid, b)
    assert c["category"] == "hobbies" and c["proposed_category"] == "hobbies/collecting"


def test_key_unique_among_live_claims(conn, pid):
    from wwyd.store.categories import category_ids_by_path

    cat = category_ids_by_path(conn, pid)["work/meetings"]
    kw = dict(
        kind="habit",
        category_id=cat,
        audiences_all=True,
        audience_ids=[],
        visibility="shared",
        status="approved",
        confidence=0.8,
        evidence={},
        origin="derived",
        key="habit:meeting_buffer",
    )
    first = cl.insert_claim(conn, pid, text="Keeps 10 min buffer", **kw)
    with pytest.raises(Conflict):
        cl.insert_claim(conn, pid, text="Keeps 15 min buffer", **kw)
    cl.update_claim(conn, pid, first, status="archived")
    second = cl.insert_claim(conn, pid, text="Keeps 15 min buffer", **kw)
    assert second
    with pytest.raises(Conflict):
        cl.update_claim(conn, pid, first, status="approved")
