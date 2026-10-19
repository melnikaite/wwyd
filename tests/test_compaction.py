# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from conftest import call, call_error

from wwyd.compact import compact, run_after_job
from wwyd.compact import scheduler as sched
from wwyd.compact.constants import MAX_CLAIMS_PER_CATEGORY
from wwyd.errors import Conflict, LLMUnavailable
from wwyd.ids import format_ts
from wwyd.store import changes as chg
from wwyd.store import claims as cl
from wwyd.store.categories import merge_categories, upsert_category

pytestmark = pytest.mark.anyio

CAT = "communication/style"


def add(conn, pid, text, *, status="pending", absorb=False, **kw):
    item = {"kind": "style", "category": CAT, "text": text, "status": status, **kw}
    res = cl.add_claims(conn, pid, [item], absorb=absorb, allow_approved=True)
    return (res["created"] or [None])[0], res


def get(conn, pid, cid):
    return cl.get_claim(conn, pid, cid)


def set_cols(conn, cid, **cols):
    conn.execute(
        f"UPDATE claims SET {', '.join(f'{k} = ?' for k in cols)} WHERE id = ?",
        [*cols.values(), cid],
    )


def snapshot_db(conn):
    return (
        conn.execute("SELECT * FROM claims ORDER BY seq").fetchall(),
        conn.execute("SELECT * FROM changes ORDER BY rowid").fetchall(),
    )


# --- add_claims absorb (§10.1) ----------------------------------------------------------


def test_absorb_into_approved_keeps_text_and_sums_evidence(conn, pid):
    a, _ = add(
        conn,
        pid,
        "Short replies, no greeting, during work hours.",
        status="approved",
        evidence={"sources": {"message": 4}},
    )
    _, res = add(
        conn,
        pid,
        "Short replies with no greeting during work hours.",
        absorb=True,
        evidence={"sources": {"message": 3}},
    )
    assert res["absorbed"] == [{"index": 0, "into": a}]
    c = get(conn, pid, a)
    assert c["text"] == "Short replies, no greeting, during work hours."
    assert c["evidence"]["sources"] == {"message": 7}
    assert c["status"] == "approved" and c["confidence"] > 0.5
    log = chg.list_changes(conn, pid, op="absorb")["changes"]
    assert log[0]["claims"] == [a] and log[0]["state"] == "applied"


def test_absorb_into_pending_takes_newer_text(conn, pid):
    a, _ = add(conn, pid, "Short replies, no greeting, during work hours.")
    add(conn, pid, "Short replies with no greeting during work hours.", absorb=True)
    assert get(conn, pid, a)["text"] == "Short replies with no greeting during work hours."


def test_rejected_is_negative_memory_with_jaccard(conn, pid):
    a, _ = add(conn, pid, "Always answers calls on weekends without delay.")
    cl.review_claims(conn, pid, reject=[a])
    for absorb in (True, False):
        _, res = add(
            conn, pid, "Always answers calls on weekends, without any delay.", absorb=absorb
        )
        assert res["dropped"] == [{"index": 0, "reason": "previously_rejected"}]
    # Low overlap is a different claim.
    created, _ = add(conn, pid, "Prefers long weekend walks.")
    assert created is not None


# --- compaction: dedupe -----------------------------------------------------------------


async def test_dedupe_pending_pending(conn, pid):
    a, _ = add(conn, pid, "Short replies, no greeting.", evidence={"sources": {"message": 2}})
    b, _ = add(conn, pid, "Short replies; no greeting!!", evidence={"sources": {"message": 5}})
    res = await compact(conn, pid, apply=True, steps=["dedupe"])
    assert [(c["op"], c["state"]) for c in res["changes"]] == [("merge", "applied")]
    keep, drop = get(conn, pid, b), get(conn, pid, a)  # newer one keeps its text
    assert keep["status"] == "pending" and keep["evidence"]["sources"] == {"message": 7}
    assert drop["status"] == "archived" and drop["archive_reason"] == "merged"
    assert drop["superseded_by"] == b


async def test_approved_approved_is_proposed_and_review_applies(conn, pid):
    a, _ = add(conn, pid, "Short replies, no greeting.", status="approved")
    b, _ = add(conn, pid, "Short replies; no greeting!!", status="approved")
    res = await compact(conn, pid, apply=True, steps=["dedupe"])
    (change,) = res["changes"]
    assert change["state"] == "proposed" and change["op"] == "merge"
    assert get(conn, pid, a)["status"] == get(conn, pid, b)["status"] == "approved"
    # Not proposed twice.
    assert (await compact(conn, pid, apply=True, steps=["dedupe"]))["changes"] == []
    out = cl.review_claims(conn, pid, approve=[change["id"]])
    assert out["applied_changes"] == 1
    statuses = sorted(get(conn, pid, x)["status"] for x in (a, b))
    assert statuses == ["approved", "archived"]
    assert chg.get_change(conn, pid, change["id"])["state"] == "applied"


async def test_proposal_dismissed(conn, pid):
    add(conn, pid, "Short replies, no greeting.", status="approved")
    add(conn, pid, "Short replies; no greeting!!", status="approved")
    (change,) = (await compact(conn, pid, apply=True))["changes"]
    out = cl.review_claims(conn, pid, reject=[change["id"]])
    assert out["dismissed_changes"] == 1
    assert chg.get_change(conn, pid, change["id"])["state"] == "dismissed"
    assert cl.review_claims(conn, pid, reject=[change["id"]])["skipped"] == [change["id"]]


async def test_pending_near_rejected_is_dropped(conn, pid):
    r, _ = add(conn, pid, "Always answers calls on weekends without delay.")
    cl.review_claims(conn, pid, reject=[r])
    # Inserted directly (bypassing add_claims' negative memory), as an older job might have.
    p, _ = add(conn, pid, "Different text first.")
    set_cols(conn, p, text="Always answers calls on weekends, no delay.")
    res = await compact(conn, pid, apply=True, steps=["dedupe"])
    assert res["changes"][0]["reason"] == "previously_rejected"
    assert get(conn, pid, p)["status"] == "archived"
    assert get(conn, pid, r)["evidence"] == {}  # negative memory stays text-only


# --- judge ------------------------------------------------------------------------------


async def test_judge_same_and_contradicts(conn, pid, fake_llm):
    a, _ = add(conn, pid, "Replies quickly to work messages.", evidence={"sources": {"message": 1}})
    b, _ = add(
        conn, pid, "Replies to work messages within minutes.", evidence={"sources": {"message": 9}}
    )
    c, _ = add(
        conn,
        pid,
        "Avoids meetings on Friday evening.",
        confidence=0.9,
        evidence={"sources": {"calendar_event": 50}},
    )
    d, _ = add(
        conn,
        pid,
        "Accepts meetings on Friday evening gladly.",
        confidence=0.4,
        evidence={"sources": {"calendar_event": 2}},
    )

    def answer(call):
        items = json.loads(call["user"])
        out = []
        for it in items:
            verdict = "contradicts" if "Friday" in it["a"] else "same"
            out.append({"pair": it["pair"], "verdict": verdict})
        return {"verdicts": out}

    fake_llm.on("judge", answer)
    res = await compact(conn, pid, apply=True, steps=["dedupe", "contradictions"])
    assert len(fake_llm.by_purpose("judge")) == 1  # one batched call for the category
    assert "Отвечает" in fake_llm.calls[0]["system"]  # Russian worked example
    ops = sorted(c["op"] for c in res["changes"])
    assert ops == ["merge", "supersede"]
    assert get(conn, pid, d)["status"] == "archived"
    assert get(conn, pid, d)["superseded_by"] == c
    assert res["judged_pairs"] == 2 and res["skipped_pairs"] == 0
    assert {get(conn, pid, a)["status"], get(conn, pid, b)["status"]} == {"pending", "archived"}


async def test_contradiction_loser_approved_is_proposed(conn, pid, fake_llm):
    w, _ = add(
        conn,
        pid,
        "Avoids meetings on Friday evening.",
        confidence=0.9,
        evidence={"sources": {"calendar_event": 50}},
    )
    loser, _ = add(
        conn,
        pid,
        "Accepts meetings on Friday evening gladly.",
        status="approved",
        confidence=0.4,
        evidence={"sources": {"calendar_event": 2}},
    )
    fake_llm.on("judge", lambda call: {"verdicts": [{"pair": 1, "verdict": "contradicts"}]})
    (change,) = (await compact(conn, pid, apply=True))["changes"]
    assert change["op"] == "supersede" and change["state"] == "proposed"
    assert get(conn, pid, loser)["status"] == "approved"
    cl.review_claims(conn, pid, approve=[change["id"]])
    assert get(conn, pid, loser)["superseded_by"] == w


async def test_judge_failure_skips(conn, pid, fake_llm):
    a, _ = add(conn, pid, "Replies quickly to work messages.")
    b, _ = add(conn, pid, "Replies to work messages within minutes.")

    def fail(call):
        raise LLMUnavailable("down")

    fake_llm.on("judge", fail)
    res = await compact(conn, pid, apply=True)
    assert res["changes"] == [] and res["skipped_pairs"] == 1
    assert get(conn, pid, a)["status"] == get(conn, pid, b)["status"] == "pending"


async def test_run_after_job_skips_judge_without_llm_for_ingest(conn, pid, fake_llm):
    from wwyd.store.personas import update_settings

    update_settings(conn, pid, llm_for_ingest=False)
    a, _ = add(conn, pid, "Replies quickly to work messages.")
    b, _ = add(conn, pid, "Replies to work messages within minutes.")
    # No fake_llm.on("judge", ...): any judge call would fail the test.
    counts = await run_after_job(conn, pid, [b], job_id="job_1")
    assert counts == {"skipped_pairs": 1}
    assert fake_llm.calls == []


async def test_run_after_job_carries_job_id(conn, pid):
    a, _ = add(conn, pid, "Short replies, no greeting.")
    b, _ = add(conn, pid, "Short replies; no greeting!!")
    counts = await run_after_job(conn, pid, [b], job_id="job_1")
    assert counts == {"merge": 1}
    assert chg.list_changes(conn, pid)["changes"][0]["job_id"] == "job_1"


# --- dry run ----------------------------------------------------------------------------


async def test_apply_false_writes_nothing(conn, pid, fake_llm):
    add(conn, pid, "Short replies, no greeting.")
    add(conn, pid, "Short replies; no greeting!!")
    add(conn, pid, "Replies quickly to work messages.")
    add(conn, pid, "Replies to work messages within minutes.")
    old, _ = add(conn, pid, "Old claim.")
    set_cols(conn, old, last_evidence_at="2020-01-01T00:00:00Z")
    before = [tuple(r) for r in snapshot_db(conn)[0]], [tuple(r) for r in snapshot_db(conn)[1]]
    res = await compact(conn, pid, apply=False)
    after = [tuple(r) for r in snapshot_db(conn)[0]], [tuple(r) for r in snapshot_db(conn)[1]]
    assert before == after
    assert fake_llm.calls == []
    assert {c["op"] for c in res["changes"]} == {"merge", "decay"}
    assert all("id" not in c for c in res["changes"]) and res["judge_pending"] == 1


# --- decay ------------------------------------------------------------------------------


async def test_decay(conn, pid):
    now = datetime(2026, 10, 18, tzinfo=UTC)
    six_months = format_ts(now - timedelta(days=6 * 30.4375))
    half, _ = add(conn, pid, "Half life claim.", confidence=0.8)
    gone, _ = add(conn, pid, "Very old pending.", confidence=0.3)
    appr, _ = add(conn, pid, "Very old approved.", confidence=0.3, status="approved")
    own, _ = add(conn, pid, "Owner written.", confidence=0.8)
    set_cols(conn, half, last_evidence_at=six_months)
    for cid in (gone, appr):
        set_cols(conn, cid, last_evidence_at="2025-01-01T00:00:00Z")
    set_cols(conn, own, last_evidence_at="2020-01-01T00:00:00Z", origin="owner")
    res = await compact(conn, pid, apply=True, steps=["decay"], now=now)
    assert get(conn, pid, half)["confidence"] == pytest.approx(0.4, abs=0.001)
    assert get(conn, pid, gone)["status"] == "archived"
    assert get(conn, pid, gone)["archive_reason"] == "decayed"
    assert get(conn, pid, appr)["status"] == "approved"
    assert get(conn, pid, own)["confidence"] == 0.8
    states = sorted((c["op"], c["state"]) for c in res["changes"])
    assert states == [("decay", "applied"), ("decay", "applied"), ("decay", "proposed")]
    # Running again the same instant does not decay twice (decayed_at is the new reference).
    await compact(conn, pid, apply=True, steps=["decay"], now=now)
    assert get(conn, pid, half)["confidence"] == pytest.approx(0.4, abs=0.001)


# --- budget -----------------------------------------------------------------------------


async def test_category_budget(conn, pid):
    ids = []
    for i in range(MAX_CLAIMS_PER_CATEGORY + 2):
        cid, _ = add(conn, pid, f"Distinct claim number {i} zz{i}", confidence=0.5 + i / 100)
        ids.append(cid)
    set_cols(conn, ids[1], status="approved")
    res = await compact(conn, pid, apply=True, steps=["budget"])
    got = sorted((c["state"], c["claims"][0]) for c in res["changes"])
    # Lightest two: ids[0] (pending, archived) and ids[1] (approved, proposed).
    assert got == sorted([("applied", ids[0]), ("proposed", ids[1])])
    assert get(conn, pid, ids[0])["archive_reason"] == "budget"


async def test_token_budget(conn, pid, monkeypatch):
    from wwyd.compact import maintenance

    monkeypatch.setattr(maintenance, "MAX_APPROVED_TOKENS", 10)
    a, _ = add(conn, pid, "x" * 30 + " alpha", status="approved", confidence=0.2)
    b, _ = add(conn, pid, "y" * 30 + " beta", status="approved", confidence=0.9)
    res = await compact(conn, pid, apply=True, steps=["budget"])
    assert [(c["claims"], c["state"]) for c in res["changes"]] == [([a], "proposed")]
    assert get(conn, pid, b)["status"] == "approved"


# --- undo -------------------------------------------------------------------------------


async def test_undo_and_conflict(conn, pid):
    a, _ = add(conn, pid, "Short replies, no greeting.")
    b, _ = add(conn, pid, "Short replies; no greeting!!")
    (merge,) = (await compact(conn, pid, apply=True, steps=["dedupe"]))["changes"]
    restore = cl.update_claim(conn, pid, a, status="pending")  # restore the merged claim
    assert restore["status"] == "pending"
    with pytest.raises(Conflict):
        chg.undo_change(conn, pid, merge["id"])
    (restore_change,) = chg.list_changes(conn, pid, op="restore")["changes"]
    chg.undo_change(conn, pid, restore_change["id"])
    assert get(conn, pid, a)["status"] == "archived"
    undone = chg.undo_change(conn, pid, merge["id"])
    assert undone["state"] == "undone"
    assert get(conn, pid, a)["status"] == get(conn, pid, b)["status"] == "pending"
    assert get(conn, pid, b)["evidence"] == {"sources": {}}
    with pytest.raises(Conflict):
        chg.undo_change(conn, pid, merge["id"])


def test_undo_category_merge_and_rename(conn, pid):
    upsert_category(conn, pid, "food")
    upsert_category(conn, pid, "food/sweet")
    cid, _ = add(conn, pid, "Likes cake.", category="food/sweet")
    merged = merge_categories(conn, pid, ["food"], CAT)
    assert get(conn, pid, cid)["category"] == CAT
    chg.undo_change(conn, pid, merged["change_id"])
    assert get(conn, pid, cid)["category"] == "food/sweet"
    upsert_category(conn, pid, "food", rename_to="meals")
    (ren,) = chg.list_changes(conn, pid, op="category_rename")["changes"]
    assert get(conn, pid, cid)["category"] == "meals/sweet"
    chg.undo_change(conn, pid, ren["id"])
    assert get(conn, pid, cid)["category"] == "food/sweet"


# --- purge ------------------------------------------------------------------------------


async def test_purge_expires_changes(conn, pid):
    a, _ = add(conn, pid, "Short replies, no greeting.")
    add(conn, pid, "Short replies; no greeting!!")
    (merge,) = (await compact(conn, pid, apply=True, steps=["dedupe"]))["changes"]
    later = datetime.now(UTC) + timedelta(days=31)
    res = await compact(conn, pid, apply=True, steps=["purge"], now=later)
    (purge,) = res["changes"]
    assert purge["op"] == "purge" and purge["claims"] == [a]
    assert conn.execute("SELECT 1 FROM claims WHERE id = ?", (a,)).fetchone() is None
    assert chg.get_change(conn, pid, merge["id"])["state"] == "expired"
    with pytest.raises(Conflict):
        chg.undo_change(conn, pid, merge["id"])


# --- scheduler --------------------------------------------------------------------------


def test_last_due_timezones():
    now = datetime(2026, 10, 18, 1, 0, tzinfo=UTC)
    # Berlin (UTC+2 in October): 03:00 local, before 03:30 -> yesterday's 03:30.
    assert sched.last_due(now, "Europe/Berlin") == datetime(2026, 10, 17, 1, 30, tzinfo=UTC)
    # Tokyo (UTC+9): 10:00 local -> today's 03:30 local = 18:30 UTC the day before.
    assert sched.last_due(now, "Asia/Tokyo") == datetime(2026, 10, 17, 18, 30, tzinfo=UTC)
    assert sched.last_due(now, "Not/AZone") == datetime(2026, 10, 17, 3, 30, tzinfo=UTC)


def test_run_nightly(conn, pid, monkeypatch):
    conn.execute("UPDATE personas SET timezone = 'Europe/Berlin' WHERE id = ?", (pid,))
    seen = []
    monkeypatch.setattr(sched, "_hooks", [])
    sched.register_nightly(lambda c, p, now: seen.append(p))
    t0 = datetime(2026, 10, 18, 1, 0, tzinfo=UTC)  # 03:00 Berlin
    assert sched.run_nightly(conn, t0) == [pid]  # never ran: yesterday's 03:30 is due
    assert sched.run_nightly(conn, t0 + timedelta(minutes=20)) == []  # 03:20
    assert sched.run_nightly(conn, t0 + timedelta(minutes=31)) == [pid]  # 03:31
    assert sched.run_nightly(conn, t0 + timedelta(hours=12)) == []
    assert seen == [pid, pid]


def test_nightly_purges_old_audit(conn, pid):
    from wwyd.store import audit

    assert sched._purge_audit in sched._hooks
    sched.register_nightly(sched._purge_audit)  # idempotent
    assert sched._hooks.count(sched._purge_audit) == 1
    audit.record(conn, pid, scope="owner", outcome="answer")
    audit.record(conn, pid, scope="owner", outcome="needs")
    conn.execute("UPDATE audit SET ts = '2020-01-01T00:00:00Z' WHERE outcome = 'answer'")
    sched.run_nightly(conn, datetime(2026, 10, 18, 4, 0, tzinfo=UTC))
    assert [e["outcome"] for e in audit.list_audit(conn, pid)["entries"]] == ["needs"]


# --- MCP tools --------------------------------------------------------------------------


async def test_tools(client, conn, pid):
    add(conn, pid, "Short replies, no greeting.")
    add(conn, pid, "Short replies; no greeting!!")
    dry = await call(client, "compact", {"steps": ["dedupe"]})
    assert dry["changes"][0]["op"] == "merge" and "id" not in dry["changes"][0]
    done = await call(client, "compact", {"apply": True, "steps": ["dedupe"]})
    change_id = done["changes"][0]["id"]
    listed = await call(client, "list_changes", {"state": "applied"})
    assert listed["changes"][0]["id"] == change_id and listed["changes"][0]["before"]
    assert (await call(client, "undo_change", {"id": change_id}))["change"]["state"] == "undone"
    err = await call_error(client, "undo_change", {"id": change_id})
    assert err["code"] == "conflict"


# --- polarity ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("neg", "pos"),
    [
        ("Never takes calls on Friday.", "Takes calls on Friday."),
        ("Никогда не берёт трубку в пятницу.", "Берёт трубку в пятницу."),
    ],
)
async def test_polarity_pair_is_judged_not_merged(conn, pid, fake_llm, neg, pos):
    a, _ = add(conn, pid, neg)
    b, res = add(conn, pid, pos, absorb=True)
    assert b is not None and res["absorbed"] == []  # add_claims must not absorb it
    fake_llm.on("judge", lambda call: {"verdicts": [{"pair": 1, "verdict": "contradicts"}]})
    dry = await compact(conn, pid, apply=False, steps=["dedupe", "contradictions"])
    assert dry["changes"] == [] and dry["judge_pending"] == 1
    res = await compact(conn, pid, apply=True, steps=["dedupe", "contradictions"])
    assert len(fake_llm.by_purpose("judge")) == 1
    assert [c["op"] for c in res["changes"]] == ["supersede"]


def test_polarity_rejected_does_not_drop(conn, pid):
    a, _ = add(conn, pid, "Never takes calls on Friday.")
    cl.review_claims(conn, pid, reject=[a])
    b, res = add(conn, pid, "Takes calls on Friday.", absorb=True)
    assert b is not None and res["dropped"] == []
