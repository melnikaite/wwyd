# SPDX-License-Identifier: AGPL-3.0-or-later
"""Partial jobs, flushes, token budget, saturation and source watermarks (SPEC §7.1)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from conftest import call, call_error, make_server
from mcp import Client

from wwyd.errors import RateLimited
from wwyd.ingest import jobs, style
from wwyd.ingest.records import parse_batch
from wwyd.limits import get_limiter
from wwyd.store.audiences import upsert_audience

pytestmark = pytest.mark.anyio

BASE = datetime(2026, 3, 2, 9, 0, tzinfo=UTC)
CANARY = "zq7x-canary-41b9"


@pytest.fixture(autouse=True)
def setup(conn, pid, fake_llm, monkeypatch):
    upsert_audience(conn, pid, "work-slack")
    monkeypatch.setattr(jobs, "FLUSH_EVERY", 2)
    fake_llm.on("judge", {"verdicts": []})
    fake_llm.on("notes", {"claims": []})


def pairs(n, start=0, audience="work-slack", word="ok"):
    """``n`` other -> owner pairs, one hour apart; each owner reply is ~1100 chars, so one
    style call covers two pairs (record indices 4k..4k+3)."""
    out = []
    for i in range(start, start + n):
        ts = BASE + timedelta(hours=i)
        out.append(
            {
                "type": "message",
                "from_owner": False,
                "audience": audience,
                "text": f"question {i}",
                "ts": ts.isoformat(),
                "thread": f"t{i}",
            }
        )
        out.append(
            {
                "type": "message",
                "from_owner": True,
                "audience": audience,
                "text": f"reply {i} " + f"{word} " * 360,
                "ts": (ts + timedelta(minutes=1)).isoformat(),
                "thread": f"t{i}",
            }
        )
    return out


def claim(text):
    return {
        "claims": [
            {
                "kind": "style",
                "category": "communication/style",
                "proposed_category": None,
                "text": text,
                "confidence": 0.7,
            }
        ]
    }


DISTINCT = [
    "Answers in long paragraphs.",
    "Never uses emoji with colleagues.",
    "Signs off with initials.",
    "Prefers voice notes over text.",
    "Asks follow-up questions before agreeing.",
    "Writes numbered lists for plans.",
    "Uses formal address with clients.",
    "Replies only during work hours.",
    "Avoids exclamation marks entirely.",
    "Quotes deadlines as exact dates.",
]


def distinct_replies(fake_llm, fail_at=None, exc=None):
    def reply(c):
        n = len(fake_llm.by_purpose("style"))
        if fail_at is not None and n == fail_at:
            raise exc or RateLimited("daily LLM token limit reached", retry_after=60)
        return claim(DISTINCT[n - 1])

    fake_llm.on("style", reply)


async def submit(client, records, **kw):
    out = await call(client, "ingest_raw", {"records": records, **kw})
    await jobs.wait(out["job_id"])
    return out, await call(client, "job_status", {"job_id": out["job_id"]})


def style_texts(conn):
    return {
        r["text"]
        for r in conn.execute("SELECT text FROM claims WHERE origin = 'derived' AND key IS NULL")
    }


async def test_flush_survives_rate_limit_mid_job(client, conn, fake_llm):
    distinct_replies(fake_llm, fail_at=4)
    _, status = await submit(client, pairs(10))
    assert status["status"] == "partial" and status["error_code"] == "rate_limited"
    assert status["retry_after"] > 0
    # Calls 1-3 completed (two flushed by the periodic flush, one on the way out).
    assert style_texts(conn) == set(DISTINCT[:3])
    assert status["counts"]["llm_calls"] == 4
    assert status["remaining_records"] == list(range(12, 20))


async def test_llm_unavailable_is_partial(client, conn, fake_llm):
    from wwyd.errors import LLMUnavailable

    distinct_replies(fake_llm, fail_at=2, exc=LLMUnavailable("down"))
    _, status = await submit(client, pairs(4))
    assert status["status"] == "partial" and status["error_code"] == "llm_unavailable"
    assert "retry_after" not in status
    assert status["remaining_records"] == [4, 5, 6, 7]
    assert style_texts(conn) == {DISTINCT[0]}


def call_estimates(conn, pid, records):
    batch = parse_batch(
        records, tz=UTC, audiences={"work-slack", "everyone"}, default_audience="everyone"
    )
    return [c.estimated_tokens for c in style.build_calls(batch, jobs._categories(conn, pid))]


async def test_budget_stops_before_call_that_would_not_fit(client, conn, user, fake_llm):
    records = pairs(10)
    est = call_estimates(conn, user.persona_id, records)
    assert len(est) == 5
    limiter = get_limiter(conn)
    limiter.set_own_limits(user.user_id, {"llm_tokens_per_day": est[0] + est[1] + est[2] // 2})

    def reply(c):
        # Spend exactly the estimate, like a backend reporting its usage.
        n = len(fake_llm.by_purpose("style"))
        limiter.record_llm_usage(user.user_id, user.persona_id, est[n - 1], 0, None)
        return claim(DISTINCT[n - 1])

    fake_llm.on("style", reply)
    out, status = await submit(client, records)
    assert out["estimated_tokens"] == sum(est)
    assert out["remaining_tokens"] == est[0] + est[1] + est[2] // 2
    assert len(fake_llm.by_purpose("style")) == 2  # the third would not have fit
    assert status["status"] == "partial" and status["error_code"] == "rate_limited"
    assert status["remaining_records"] == list(range(8, 20))
    assert style_texts(conn) == set(DISTINCT[:2])


async def test_no_budget_left_runs_detectors_only(client, conn, user, fake_llm):
    limiter = get_limiter(conn)
    limiter.set_own_limits(user.user_id, {"llm_tokens_per_day": 1000})
    limiter.record_llm_usage(user.user_id, user.persona_id, 1000, 0, None)
    distinct_replies(fake_llm)
    note = {"type": "note", "text": "I like tea.", "ts": BASE.isoformat()}
    out, status = await submit(client, [*pairs(2), note])
    assert out["accepted"] == 5 and out["estimated_tokens"] > 0
    assert out["remaining_tokens"] == 0
    assert fake_llm.by_purpose("style") == [] and fake_llm.by_purpose("notes") == []
    assert status["status"] == "partial" and status["remaining_records"] == [0, 1, 2, 3, 4]


async def test_no_llm_step_estimates_zero(client):
    out, status = await submit(client, pairs(2), llm_for_ingest=False)
    assert out["estimated_tokens"] == 0 and status["status"] == "done"
    assert "remaining_records" not in status


async def test_saturation_skips_rest_of_audience_but_not_notes(client, conn, fake_llm):
    fake_llm.on("style", claim("Writes long replies padded with filler words."))
    notes = []

    def note_reply(c):
        notes.append(c)
        return claim("Writes long replies padded with filler words.")

    fake_llm.on("notes", note_reply)
    records = [*pairs(20), {"type": "note", "text": "I write a lot."}]
    _, status = await submit(client, records)
    assert status["status"] == "done"
    # Call 1 is new, calls 2-6 add nothing: calls 7-10 are skipped.
    assert len(fake_llm.by_purpose("style")) == 1 + style.SATURATION_CALLS
    counts = status["counts"]
    assert counts["saturated_audiences"] == 1
    assert counts["skipped_saturated"] == 4 * 4
    assert len(notes) == 1  # notes are never skipped


async def test_saturation_counts_existing_claims_as_known(client, conn, fake_llm):
    await call(
        client,
        "add_claims",
        {"claims": [{"kind": "style", "category": "communication/style", "text": "Terse."}]},
    )
    fake_llm.on("style", claim("Terse."))
    _, status = await submit(client, pairs(14))
    assert len(fake_llm.by_purpose("style")) == style.SATURATION_CALLS
    assert status["counts"]["skipped_saturated"] == 2 * 4
    assert status["counts"]["absorbed"] >= 1


async def test_source_watermark(client, conn, fake_llm):
    distinct_replies(fake_llm)
    note = {"type": "note", "text": "I like tea."}  # no ts: never skipped
    out, status = await submit(client, [*pairs(4), note], source="tg")
    assert status["status"] == "done" and status["counts"]["skipped_seen"] == 0
    (src,) = (await call(client, "list_sources"))["sources"]
    last = BASE + timedelta(hours=3, minutes=1)
    assert src["source"] == "tg" and src["records_total"] == 9
    assert datetime.fromisoformat(src["last_ts"]) == last

    fake_llm.calls.clear()
    out, status = await submit(client, [*pairs(6), note], source="tg")
    assert out["accepted"] == 13
    assert status["counts"]["skipped_seen"] == 8
    assert status["counts"]["records"]["message"] == 12  # records received, by type
    (src,) = (await call(client, "list_sources"))["sources"]
    assert datetime.fromisoformat(src["last_ts"]) == BASE + timedelta(hours=5, minutes=1)
    assert src["records_total"] == 9 + 5

    # An older batch under another source label does not move "tg"; an older one under
    # "tg" is skipped entirely and never moves the watermark backwards.
    _, status = await submit(client, pairs(2), source="tg")
    assert status["counts"]["skipped_seen"] == 4
    (src,) = (await call(client, "list_sources"))["sources"]
    assert datetime.fromisoformat(src["last_ts"]) == BASE + timedelta(hours=5, minutes=1)

    # Without source nothing is skipped.
    _, status = await submit(client, pairs(2))
    assert status["counts"]["skipped_seen"] == 0

    assert (await call(client, "reset_source", {"source": "tg"}))["deleted"] is True
    assert (await call(client, "list_sources"))["sources"] == []
    assert (await call(client, "reset_source", {"source": "tg"}))["deleted"] is False
    assert (await call_error(client, "ingest_raw", {"records": pairs(1), "source": "Bad!"}))[
        "code"
    ] == "invalid"


async def test_partial_watermark_stays_below_remaining(client, conn, fake_llm):
    distinct_replies(fake_llm, fail_at=3)
    late_event = {
        "type": "calendar_event",
        "start": (BASE + timedelta(days=30)).isoformat(),
        "end": (BASE + timedelta(days=30, hours=1)).isoformat(),
        "attendees": 2,
        "response": "accepted",
    }
    _, status = await submit(client, [*pairs(6), late_event], source="tg")
    assert status["status"] == "partial" and status["remaining_records"] == list(range(8, 12))
    (src,) = (await call(client, "list_sources"))["sources"]
    # The event (processed, but later than the remaining records) does not count.
    assert datetime.fromisoformat(src["last_ts"]) == BASE + timedelta(hours=3, minutes=1)

    # Resending the remaining records is not skipped.
    records = [*pairs(6), late_event]
    fake_llm.on("style", claim("Terse."))
    _, status = await submit(client, [records[i] for i in status["remaining_records"]], source="tg")
    assert status["status"] == "done" and status["counts"]["skipped_seen"] == 0
    (src,) = (await call(client, "list_sources"))["sources"]
    assert datetime.fromisoformat(src["last_ts"]) == BASE + timedelta(hours=5, minutes=1)


async def test_source_tools_owner_only(conn, user):
    for scope, kw in (("read", {}), ("respond", {"audiences": ("work-slack",)})):
        async with Client(make_server(conn, user, scope=scope, **kw)) as c:
            assert (await call_error(c, "list_sources"))["code"] == "forbidden"
            assert (await call_error(c, "reset_source", {"source": "tg"}))["code"] == "forbidden"


async def test_progress_rows_hold_no_record_text(client, conn, fake_llm):
    distinct_replies(fake_llm, fail_at=2)
    records = pairs(4)
    records[0]["thread"] = CANARY
    records[0]["text"] = CANARY
    records[3]["text"] = f"{CANARY} " + records[3]["text"]
    records.append({"type": "note", "text": f"{CANARY} note", "ts": BASE.isoformat()})
    out, status = await submit(client, records, source="tg")
    assert status["status"] == "partial"
    rows = [dict(r) for r in conn.execute("SELECT * FROM jobs")]
    rows += [dict(r) for r in conn.execute("SELECT * FROM ingest_sources")]
    dumped = json.dumps(rows)
    assert CANARY not in dumped and "reply" not in dumped
    remaining = json.loads(conn.execute("SELECT remaining_records FROM jobs").fetchone()[0])
    assert all(isinstance(i, int) for i in remaining)


def habit_evidence(conn):
    return {
        r["key"]: json.loads(r["evidence"])
        for r in conn.execute("SELECT key, evidence FROM claims WHERE key LIKE 'habit:%'")
    }


def chatty(n):
    """``n`` threads on workdays: two other messages, then the owner's reply 3 min later."""
    out = []
    day = datetime(2026, 2, 2, 10, 0, tzinfo=UTC)
    for i in range(n):
        ts = day + timedelta(days=i, hours=4 * (i % 2))
        for k, (owner, text) in enumerate(((False, "ping"), (False, "any news?"), (True, "ok"))):
            out.append(
                {
                    "type": "message",
                    "from_owner": owner,
                    "audience": "work-slack",
                    "text": text + " " + "x " * (500 if owner else 0),
                    "ts": (ts + timedelta(minutes=k + (1 if owner else 0))).isoformat(),
                    "thread": f"t{i}",
                }
            )
    return out


async def test_partial_then_resend_counts_each_record_once(conn, user, fake_llm, monkeypatch):
    monkeypatch.setattr(style, "SATURATION_CALLS", 1000)
    from wwyd.store.personas import create_user

    records = chatty(60)
    fake_llm.on("style", {"claims": []})
    # A single full run, on a separate persona.
    other = create_user(conn, "other@example.com")
    upsert_audience(conn, other.persona_id, "work-slack")
    async with Client(make_server(conn, other)) as c:
        _, status = await submit(c, records)
    assert status["status"] == "done"
    full = {
        k: v
        for k, v in habit_evidence(conn).items()
        if conn.execute("SELECT persona_id FROM claims WHERE key = ?", (k,)).fetchone()[0]
        == other.persona_id
    }
    assert any(k.startswith("habit:reply_latency") for k in full)

    # Partial run on the owner's persona, then a resend of remaining_records.
    conn.execute("DELETE FROM claims WHERE persona_id = ?", (other.persona_id,))
    fake_llm.calls.clear()

    def reply(c):
        # Stop late enough that the processed part alone clears the detector thresholds
        # (a below-threshold finding without an existing keyed claim is not kept).
        if len(fake_llm.by_purpose("style")) == 24:
            raise RateLimited("daily LLM token limit reached", retry_after=60)
        return {"claims": []}

    fake_llm.on("style", reply)
    async with Client(make_server(conn, user)) as c:
        _, status = await submit(c, records)
        assert status["status"] == "partial" and 0 < len(status["remaining_records"]) < 180
        fake_llm.on("style", {"claims": []})
        _, status = await submit(c, [records[i] for i in status["remaining_records"]])
    assert status["status"] == "done"
    resent = habit_evidence(conn)
    for key, ev in full.items():
        assert resent[key]["stats"] == ev["stats"], key
