# SPDX-License-Identifier: AGPL-3.0-or-later
"""Builtin time analyzers with synthetic ground truth (SPEC §7.2, §13; docs/habits.md)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from analyzer_data import BERLIN, add_audience, cat_id, meetings, replies, workdays

from wwyd.analyzers import run_analyzers
from wwyd.analyzers.builtin import host_fields
from wwyd.analyzers.state import read_state
from wwyd.ingest.episodes import Episode

SLOTS = ((10, 0, 25), (11, 0, 50), (14, 0, 25))
START = date(2026, 9, 7)  # a Monday


def _run(conn, pid, eps, counts=None):
    counts = {} if counts is None else counts
    return run_analyzers(conn, persona_id=pid, episodes=eps, tz=BERLIN, counts=counts), counts


def _claims(conn, pid, status=None):
    sql = "SELECT key, text, status, evidence FROM claims WHERE persona_id = ? AND key IS NOT NULL"
    rows = conn.execute(sql + " ORDER BY key", (pid,)).fetchall()
    return {r["key"]: r for r in rows if status is None or r["status"] == status}


def _analyzer(conn, pid, name, scope_id):
    return conn.execute(
        "SELECT * FROM analyzers WHERE persona_id = ? AND name = ? AND scope_id = ?",
        (pid, name, scope_id),
    ).fetchone()


def test_ground_truth_meetings_and_replies(conn, pid):
    meet = cat_id(conn, pid)
    style = cat_id(conn, pid, "communication/style")
    aud = add_audience(conn, pid, "work-chat")
    days = workdays(START, 30)  # 6 weeks
    eps = meetings(meet, days, slots=SLOTS) + replies(style, days, "work-chat")
    touched, counts = _run(conn, pid, eps)
    claims = _claims(conn, pid)
    # Stylometry (audience scope) is covered in tests/test_stylometry.py.
    texts = {k: r["text"] for k, r in claims.items() if not k.startswith("builtin:stylometry:")}
    assert any(k.startswith("builtin:stylometry:") for k in claims)
    assert texts == {
        f"builtin:time_activity:{meet}:hours": (
            "work/meetings: active 10:00–15:00 on weekdays (100% of 84 weekday episodes)."
        ),
        f"builtin:time_activity:{meet}:slots": (
            "work/meetings: none on Friday afternoons (0 of 84 episodes)."
        ),
        f"builtin:time_activity:{meet}:weekends": (
            "work/meetings: none on weekends (0 of 84 episodes)."
        ),
        f"builtin:time_duration:{meet}:length": "work/meetings: typically 25 or 50 min (100% of 84).",
        f"builtin:time_gaps:{meet}": (
            "work/meetings: keeps at least 15 min between items (100% of 30 same-day gaps)."
        ),
        f"builtin:time_activity:{style}:hours": (
            "communication/style: active 10:00–11:00 on weekdays (100% of 30 weekday episodes)."
        ),
        f"builtin:time_activity:{style}:slots": (
            "communication/style: none on Monday afternoons and Tuesday afternoons"
            " (0 of 30 episodes)."
        ),
        f"builtin:time_activity:{style}:weekends": (
            "communication/style: none on weekends (0 of 30 episodes)."
        ),
        f"builtin:time_duration:{style}:latency:{aud}": (
            "communication/style: responds within 5 min in work hours (100% of 30 responses)."
        ),
    }
    assert all(r["status"] == "pending" for r in claims.values())
    assert touched == {r[0] for r in conn.execute("SELECT id FROM claims WHERE key IS NOT NULL")}
    latency = conn.execute(
        "SELECT c.audiences_all, ca.audience_id, c.kind FROM claims c"
        " JOIN claim_audiences ca ON ca.claim_id = c.id WHERE c.key LIKE '%:latency:%'"
    ).fetchone()
    assert (latency[0], latency[1], latency[2]) == (0, aud, "style")
    ev = json.loads(claims[f"builtin:time_gaps:{meet}"]["evidence"])
    assert ev["sources"] == {"analyzer": 30}
    assert ev["share"] == 1.0
    assert ev["period"] == f"{days[0].isoformat()}..{days[-1].isoformat()}"
    assert ev["stats"]["n"] == 30 and ev["stats"]["hits"] == 30
    assert counts["analyzers_run"] == 8  # 3 time builtins x 2 categories + 2 stylometry rows
    assert counts["analyzer_claims_created"] == 10  # 9 time claims + 1 stylometry
    assert counts["analyzer_keys_dropped"] == 0
    assert counts["analyzer_errors"] == 0
    row = _analyzer(conn, pid, "time_gaps", meet)
    assert row["runs"] == 1 and row["status"] == "active" and row["origin"] == "builtin"
    assert row["claim_id"] in touched and row["source"] == ""


def test_below_threshold_accumulates_then_claims(conn, pid):
    meet = cat_id(conn, pid)
    days = workdays(START, 30)
    first = meetings(meet, days[:10], slots=SLOTS)  # 2 weeks: period too short
    touched, counts = _run(conn, pid, first)
    assert touched == set() and _claims(conn, pid) == {}
    gaps = read_state(conn, _analyzer(conn, pid, "time_gaps", meet)["id"])
    assert sum(gaps["gap"]["2026-09"].values()) == 10
    # Small later batches still add up; the claim appears once the totals pass.
    for chunk in (days[10:15], days[15:20], days[20:30]):
        _run(conn, pid, meetings(meet, chunk, slots=SLOTS, start_index=500))
    claims = _claims(conn, pid)
    assert claims[f"builtin:time_gaps:{meet}"]["text"].endswith("(100% of 30 same-day gaps).")
    assert _analyzer(conn, pid, "time_gaps", meet)["runs"] == 4


def test_rerun_updates_numbers_without_duplicates(conn, pid):
    meet = cat_id(conn, pid)
    days = workdays(START, 60)
    _run(conn, pid, meetings(meet, days[:30], slots=SLOTS))
    _run(conn, pid, meetings(meet, days[30:], slots=SLOTS))
    rows = conn.execute(
        "SELECT text FROM claims WHERE key = ?", (f"builtin:time_gaps:{meet}",)
    ).fetchall()
    assert [r[0] for r in rows] == [
        "work/meetings: keeps at least 15 min between items (100% of 60 same-day gaps)."
    ]


def test_partial_job_and_resend_count_each_episode_once(conn, pid, tmp_path):
    from wwyd.db import open_db
    from wwyd.store.personas import create_user

    meet = cat_id(conn, pid)
    style = cat_id(conn, pid, "communication/style")
    add_audience(conn, pid, "work-chat")
    days = workdays(START, 30)
    eps = meetings(meet, days, slots=SLOTS) + replies(style, days, "work-chat")
    processed = [e for e in eps if e.index % 3 != 0]
    remaining = [e for e in eps if e.index % 3 == 0]
    _run(conn, pid, processed)  # the partial job: only processed episodes
    _run(conn, pid, remaining)  # the resend

    other = open_db(tmp_path / "other.db")
    u = create_user(other, "x@example.com")
    add_audience(other, u.persona_id, "work-chat")
    meet2 = cat_id(other, u.persona_id)
    style2 = cat_id(other, u.persona_id, "communication/style")
    remap = {meet: meet2, style: style2}
    for e in eps:
        e.category_id = remap[e.category_id]
    _run(other, u.persona_id, eps)

    for name in ("time_activity", "time_duration"):
        a = read_state(conn, _analyzer(conn, pid, name, meet)["id"])
        b = read_state(other, _analyzer(other, u.persona_id, name, meet2)["id"])
        assert a == b, name
    other.close()


def test_dst_edges():
    tz = ZoneInfo("Europe/Berlin")
    # 2026-03-29: clocks jump 02:00 -> 03:00. 01:30 CET to 03:30 CEST is one real hour.
    start = datetime(2026, 3, 29, 1, 30, tzinfo=tz)
    end = datetime(2026, 3, 29, 3, 30, tzinfo=tz)
    ep = Episode(0, "c", "x", None, start=start, end=end)
    it = host_fields(ep, tz, {})
    assert it.duration_s == 3600
    assert (it.weekday, it.hour, it.month, it.day) == (6, 1, "2026-03", "29")
    # A UTC timestamp is bucketed in local time: 23:30Z on Oct 31 is Nov 1 00:30 in Berlin.
    utc = Episode(1, "c", "x", None, start=datetime(2026, 10, 31, 23, 30, tzinfo=UTC))
    it = host_fields(utc, tz, {})
    assert (it.month, it.day, it.hour, it.weekday) == ("2026-11", "01", 0, 6)
    # Fall back: 02:30 happens twice; fold=1 (CET) to 03:30 is one hour.
    a = datetime(2026, 10, 25, 2, 30, fold=0, tzinfo=tz)
    b = datetime(2026, 10, 25, 3, 30, tzinfo=tz)
    assert host_fields(Episode(2, "c", "x", None, start=a, end=b), tz, {}).duration_s == 7200


def test_hours_stay_local_across_dst(conn, pid):
    meet = cat_id(conn, pid)
    days = workdays(date(2026, 10, 12), 20)  # spans the 2026-10-25 change
    _run(conn, pid, meetings(meet, days, slots=((10, 0, 25),)))
    state = read_state(conn, _analyzer(conn, pid, "time_activity", meet)["id"])
    hours = {k.split(".")[1] for per in state["wd_hour"].values() for k in per}
    assert hours == {"10"}
    length = read_state(conn, _analyzer(conn, pid, "time_duration", meet)["id"])["length"]
    assert {k for per in length.values() for k in per} == {"25"}


def test_weakening_approved_becomes_proposal_pending_archived(conn, pid):
    meet = cat_id(conn, pid)
    days = workdays(START, 30)
    _run(conn, pid, meetings(meet, days, slots=SLOTS))
    gaps_key = f"builtin:time_gaps:{meet}"
    length_key = f"builtin:time_duration:{meet}:length"
    conn.execute("UPDATE claims SET status = 'approved' WHERE key = ?", (gaps_key,))
    # Gaps of 0 and 20 min, lengths 60/40: neither the buffer nor the lengths still hold.
    later = workdays(days[-1] + timedelta(days=1), 30)
    tight = ((9, 0, 60), (10, 0, 40), (11, 0, 60))
    _run(conn, pid, meetings(meet, later, slots=tight, start_index=900, skip_friday_pm=False))
    claims = _claims(conn, pid)
    assert claims[gaps_key]["status"] == "approved"  # text kept, proposal for the owner
    prop = conn.execute(
        "SELECT op, reason, payload FROM changes WHERE state = 'proposed'"
    ).fetchall()
    assert len(prop) == 1 and prop[0]["reason"] == "analyzer_weakened"
    assert json.loads(prop[0]["payload"])["plan"]["kind"] == "archive"
    ev = json.loads(claims[gaps_key]["evidence"])
    assert ev["stats"]["n"] == 30 + 60  # numbers refreshed
    assert claims[length_key]["status"] == "archived"  # pending claim archived
    archived = conn.execute(
        "SELECT status, archive_reason FROM claims WHERE key = ?", (length_key,)
    ).fetchone()
    assert tuple(archived) == ("archived", "decayed")
    # A further weak run does not stack proposals.
    _run(conn, pid, meetings(meet, workdays(later[-1] + timedelta(days=1), 5), slots=tight,
                             start_index=2000, skip_friday_pm=False))  # fmt: skip
    assert conn.execute("SELECT count(*) FROM changes WHERE state = 'proposed'").fetchone()[0] == 1


def test_rejected_keyed_claim_is_negative_memory(conn, pid):
    meet = cat_id(conn, pid)
    days = workdays(START, 60)
    _run(conn, pid, meetings(meet, days[:30], slots=SLOTS))
    key = f"builtin:time_gaps:{meet}"
    conn.execute("UPDATE claims SET status = 'rejected' WHERE key = ?", (key,))
    before = conn.execute("SELECT text, evidence FROM claims WHERE key = ?", (key,)).fetchone()
    _run(conn, pid, meetings(meet, days[30:], slots=SLOTS))
    rows = conn.execute("SELECT text, evidence FROM claims WHERE key = ?", (key,)).fetchall()
    assert [tuple(r) for r in rows] == [tuple(before)]


def test_owner_edited_text_is_kept(conn, pid):
    meet = cat_id(conn, pid)
    days = workdays(START, 60)
    _run(conn, pid, meetings(meet, days[:30], slots=SLOTS))
    key = f"builtin:time_gaps:{meet}"
    conn.execute(
        "UPDATE claims SET text = 'Keeps buffers.', origin = 'owner' WHERE key = ?", (key,)
    )
    _run(conn, pid, meetings(meet, days[30:], slots=SLOTS))
    row = conn.execute("SELECT text, evidence FROM claims WHERE key = ?", (key,)).fetchone()
    assert row["text"] == "Keeps buffers." and json.loads(row["evidence"])["stats"]["n"] == 60


def test_disabled_analyzer_is_skipped(conn, pid):
    meet = cat_id(conn, pid)
    days = workdays(START, 30)
    _run(conn, pid, meetings(meet, days[:5], slots=SLOTS))
    conn.execute("UPDATE analyzers SET status = 'disabled' WHERE name = 'time_gaps'")
    _, counts = _run(conn, pid, meetings(meet, days[5:], slots=SLOTS))
    assert counts["analyzers_skipped_disabled"] == 1
    row = _analyzer(conn, pid, "time_gaps", meet)
    assert row["runs"] == 1
    assert sum(read_state(conn, row["id"])["gap"]["2026-09"].values()) == 5


def test_exact_category_scope_and_no_episodes(conn, pid):
    touched, counts = _run(conn, pid, [])
    assert touched == set() and counts["analyzers_run"] == 0
    meet = cat_id(conn, pid)
    work = cat_id(conn, pid, "work")
    _run(conn, pid, meetings(meet, workdays(START, 3)) + meetings(work, workdays(START, 2)))
    scopes = {
        (r["name"], r["scope_id"])
        for r in conn.execute(
            "SELECT name, scope_id FROM analyzers WHERE persona_id = ? AND scope_kind = 'category'",
            (pid,),
        )
    }
    assert {s for _, s in scopes} == {meet, work}
    assert len(scopes) == 6


def test_episode_without_time_is_ignored(conn, pid):
    meet = cat_id(conn, pid)
    _run(conn, pid, [Episode(0, meet, "work/meetings", None, said="hello")])
    assert conn.execute("SELECT count(*) FROM analyzer_state").fetchone()[0] == 0


def test_invalid_output_and_dropped_keys_are_counted(conn, pid, monkeypatch):
    import dataclasses

    from wwyd.analyzers import builtin as reg

    meet = cat_id(conn, pid)
    gaps = reg.BUILTINS["time_gaps"]
    bad = dataclasses.replace(
        gaps, run=lambda items, ctx: {"gap": {"2026-09": {"5": float("nan")}}}
    )
    leaky = dataclasses.replace(
        reg.BUILTINS["time_duration"],
        run=lambda items, ctx: {"length": {"2026-09": {"25": 1, "Velmorine": 2, "a@b": 1}}},
    )
    monkeypatch.setitem(reg.BUILTINS, "time_gaps", bad)
    monkeypatch.setitem(reg.BUILTINS, "time_duration", leaky)
    _, counts = _run(conn, pid, meetings(meet, workdays(START, 2)))
    assert counts["analyzer_errors"] == 1
    assert counts["analyzer_keys_dropped"] == 2
    row = _analyzer(conn, pid, "time_gaps", meet)
    assert (row["runs"], row["failures"]) == (1, 1)
    assert read_state(conn, row["id"]) == {}
    keys = {r[0] for r in conn.execute("SELECT key FROM analyzer_state")}
    assert "Velmorine" not in keys and "a@b" not in keys
