# SPDX-License-Identifier: AGPL-3.0-or-later
"""LLM analyzers end to end (SPEC §7.4, §7.5): authoring with a fake LLM, the hypothesis
test in the real sandbox, acceptance, negative memory, later runs, weakening, disabling and
the sandbox budget. Skipped when the sandbox assets are not built."""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest
from conftest import call

from wwyd import sandbox
from wwyd.analyzers import llm as llm_analyzers
from wwyd.ingest import jobs
from wwyd.ingest.episodes import Episode
from wwyd.limits import get_limiter
from wwyd.store import analyzers as store
from wwyd.store.categories import category_ids_by_path

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(not sandbox.available(), reason="sandbox assets not built"),
]

GOOD = """
import json, sys
data = json.load(sys.stdin)
out = {"n": {}, "hits": {}}
for ep in data["episodes"]:
    m = ep["month"]
    if m is None or ep["hour"] is None or ep["hour"] < 17:
        continue
    out["n"].setdefault(m, {"_": 0})["_"] += 1
    if ep["labels"].get("decision") == "declined":
        out["hits"].setdefault(m, {"_": 0})["_"] += 1
print(json.dumps(out))
"""
BAD = GOOD.replace('ep["hour"] < 17', 'ep["weekday"] != 0').replace('"declined"', '"accepted"')
CRASH = "raise ValueError('boom')"
GOOD_STATEMENT = "Declines {share} of meetings starting at 17:00 or later ({n} invitations)."
BAD_STATEMENT = "Accepts {share} of Monday meetings that start late ({n} invitations)."


def meetings(first: date, days: int, late_declined: bool = True) -> list[dict]:
    out = []
    d = first
    while len(out) < days * 2:
        if d.weekday() < 5:
            day = d.isoformat()
            out.append(
                {
                    "when": {"start": f"{day}T10:00:00+01:00", "end": f"{day}T10:30:00+01:00"},
                    "category": "work/meetings",
                    "decision": "accepted the invite",
                }
            )
            out.append(
                {
                    "when": {"start": f"{day}T18:30:00+01:00", "end": f"{day}T19:00:00+01:00"},
                    "category": "work/meetings",
                    "decision": "declined, too late" if late_declined else "accepted the invite",
                }
            )
        d += timedelta(days=1)
    return out


def label_decisions(call_):
    lines = [ln for ln in call_["user"].splitlines() if ln.strip()]
    return {
        "labels": [
            {"i": i, "label": "declined" if "declin" in ln else "accepted"}
            for i, ln in enumerate(lines, 1)
        ]
    }


def setup(fake_llm, hypotheses=None):
    fake_llm.on("annotate", label_decisions)
    fake_llm.on(
        "author_analyzers",
        {
            "hypotheses": hypotheses
            if hypotheses is not None
            else [
                {"statement_template": GOOD_STATEMENT, "kind": "decision_rule", "source": GOOD},
                {"statement_template": BAD_STATEMENT, "kind": "habit", "source": BAD},
            ]
        },
    )
    fake_llm.on("word_usage", {"note": ""})
    fake_llm.on("judge", {"verdicts": []})
    fake_llm.on("decisions", {"claims": []})


async def run(client, episodes, **kw):
    out = await call(client, "ingest_raw", {"episodes": episodes, **kw})
    await jobs.wait(out["job_id"], timeout=120)
    return await call(client, "job_status", {"job_id": out["job_id"]})


def llm_rows(conn, pid):
    return conn.execute(
        "SELECT * FROM analyzers WHERE persona_id = ? AND origin = 'llm' ORDER BY seq", (pid,)
    ).fetchall()


def anl_claim(conn, pid, analyzer_id):
    return conn.execute(
        "SELECT * FROM claims WHERE persona_id = ? AND key = ?", (pid, f"anl:{analyzer_id}")
    ).fetchone()


async def test_accept_reject_and_later_jobs(client, conn, pid, fake_llm):
    setup(fake_llm)
    status = await run(client, meetings(date(2026, 1, 5), 65))
    assert status["status"] == "done", status
    c = status["counts"]
    assert c["analyzers_proposed"] == 2
    assert c["analyzers_accepted"] == 1 and c["analyzers_rejected"] == 1
    assert c["sandbox_errors"] == 0 and c["annotation_calls"] >= 1
    assert c["labels_created"] >= 2
    labels = {r[0] for r in conn.execute("SELECT label FROM category_labels")}
    assert labels == {"accepted", "declined"}

    # The authoring prompt saw aggregates and labels; nothing but the call itself.
    prompt = fake_llm.by_purpose("author_analyzers")[0]["user"]
    assert "PROFILE:" in prompt and '"declined"' in prompt

    good, bad = llm_rows(conn, pid)
    assert good["status"] == "active" and good["source"] == GOOD
    assert bad["status"] == "retired" and bad["source"] == ""
    assert bad["statement_template"] == BAD_STATEMENT
    claim = anl_claim(conn, pid, good["id"])
    assert claim["status"] == "pending" and claim["kind"] == "decision_rule"
    assert claim["text"].startswith("Declines 100% of meetings starting at 17:00 or later (65")
    ev = json.loads(claim["evidence"])
    assert ev["stats"]["n"] == 65 and ev["stats"]["hits"] == 65
    assert len(ev["stats"]["n_by_month"]) == len(ev["stats"]["hits_by_month"]) >= 3
    assert ev["sources"] == {"analyzer": 65}
    # The analyzer step counted each processed episode once (test runs wrote no state).
    state = conn.execute(
        "SELECT sum(value) FROM analyzer_state WHERE analyzer_id = ? AND metric = 'n'",
        (good["id"],),
    ).fetchone()[0]
    assert state == 65

    # A later batch: the same hypotheses come back; the retired one is not re-proposed
    # (dropped as a duplicate by code), the active one merges new state.
    status = await run(client, meetings(date(2026, 4, 6), 60))
    c = status["counts"]
    assert c["analyzers_duplicate"] == 2 and c["analyzers_accepted"] == 0
    prompt = fake_llm.by_purpose("author_analyzers")[-1]["user"]
    assert "[retired] " + BAD_STATEMENT in prompt
    assert len(llm_rows(conn, pid)) == 2
    claim = anl_claim(conn, pid, good["id"])
    assert json.loads(claim["evidence"])["stats"]["n"] == 125

    # Then the owner starts accepting late meetings: the last 3 months fall below the
    # threshold and the pending claim weakens (archived).
    weakened = 0
    for first in (date(2026, 7, 6), date(2026, 8, 3), date(2026, 9, 7)):
        status = await run(client, meetings(first, 20, late_declined=False))
        assert status["status"] == "done"
        weakened += status["counts"]["analyzer_claims_weakened"]
    assert weakened == 1
    live = conn.execute(
        "SELECT status FROM claims WHERE key = ?", (f"anl:{good['id']}",)
    ).fetchall()
    assert [r[0] for r in live] == ["archived"]

    detail = await call(client, "get_analyzer", {"id": good["id"]})
    assert detail["source"] == GOOD
    assert {m["metric"] for m in detail["state"]["metrics"]} == {"n", "hits"}


async def test_llm_for_ingest_false_runs_active_but_authors_nothing(client, conn, pid, fake_llm):
    cat = category_ids_by_path(conn, pid)["work/meetings"]
    aid = store.insert_llm(
        conn,
        pid,
        category_id=cat,
        statement_template=GOOD_STATEMENT,
        kind="habit",
        source=GOOD.replace('ep["labels"].get("decision") == "declined"', "True"),
        status="active",
    )
    fake_llm.on("judge", {"verdicts": []})
    status = await run(client, meetings(date(2026, 1, 5), 60), llm_for_ingest=False)
    assert status["status"] == "done"
    assert [c["purpose"] for c in fake_llm.calls if c["purpose"] != "judge"] == []
    assert status["counts"]["analyzers_proposed"] == 0
    assert anl_claim(conn, pid, aid) is not None


def _eps(cat_path, cat_id, n=40):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Europe/Berlin")
    base = datetime(2026, 3, 2, 18, 0, tzinfo=tz)
    return [
        Episode(i, cat_id, cat_path, None, start=base + timedelta(days=i), decision="declined")
        for i in range(n)
    ]


async def test_three_errors_disable_and_budget_skip(conn, pid, user):
    from zoneinfo import ZoneInfo

    cat = category_ids_by_path(conn, pid)["work/meetings"]
    crash = store.insert_llm(
        conn,
        pid,
        category_id=cat,
        statement_template="x {n}",
        kind="habit",
        source=CRASH,
        status="active",
    )
    eps = _eps("work/meetings", cat)
    counts: dict = {}
    for _ in range(3):
        await llm_analyzers.run_active(
            conn,
            persona_id=pid,
            user_id=user.user_id,
            episodes=eps,
            tz=ZoneInfo("Europe/Berlin"),
            counts=counts,
        )
    assert counts["sandbox_errors"] == 3 and counts["analyzers_disabled"] == 1
    row = conn.execute("SELECT status, failures FROM analyzers WHERE id = ?", (crash,)).fetchone()
    assert (row["status"], row["failures"]) == ("disabled", 3)
    usage = get_limiter(conn).usage(user.user_id)
    assert usage["sandbox_seconds"] > 0

    store.insert_llm(
        conn,
        pid,
        category_id=cat,
        statement_template="y {n}",
        kind="habit",
        source=GOOD,
        status="active",
    )
    get_limiter(conn).set_own_limits(user.user_id, {"sandbox_cpu_seconds_per_day": 0})
    counts = {}
    await llm_analyzers.run_active(
        conn,
        persona_id=pid,
        user_id=user.user_id,
        episodes=eps,
        tz=ZoneInfo("Europe/Berlin"),
        counts=counts,
    )
    assert counts["skipped_sandbox_budget"] == 1 and counts["sandbox_errors"] == 0


async def test_success_resets_failure_streak(conn, pid, user):
    from zoneinfo import ZoneInfo

    cat = category_ids_by_path(conn, pid)["work/meetings"]
    aid = store.insert_llm(
        conn,
        pid,
        category_id=cat,
        statement_template="z {n}",
        kind="habit",
        source=GOOD,
        status="active",
    )
    conn.execute("UPDATE analyzers SET failures = 2 WHERE id = ?", (aid,))
    counts: dict = {}
    await llm_analyzers.run_active(
        conn,
        persona_id=pid,
        user_id=user.user_id,
        episodes=_eps("work/meetings", cat),
        tz=ZoneInfo("Europe/Berlin"),
        counts=counts,
    )
    row = conn.execute("SELECT status, failures FROM analyzers WHERE id = ?", (aid,)).fetchone()
    assert (row["status"], row["failures"]) == ("active", 0)
