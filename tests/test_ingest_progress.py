# SPDX-License-Identifier: AGPL-3.0-or-later
"""Partial jobs, flushes, token budget, saturation and source watermarks (SPEC §7.1)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from conftest import call, call_error, make_server
from mcp import Client

from wwyd.errors import RateLimited
from wwyd.ingest import jobs, passes
from wwyd.ingest.episodes import parse_batch
from wwyd.limits import get_limiter
from wwyd.store.audiences import upsert_audience
from wwyd.store.categories import normalize_category_path

pytestmark = pytest.mark.anyio

BASE = datetime(2026, 3, 2, 9, 0, tzinfo=UTC)
CANARY = "zq7x-canary-41b9"


@pytest.fixture(autouse=True)
def setup(conn, pid, fake_llm, monkeypatch):
    upsert_audience(conn, pid, "work-slack")
    monkeypatch.setattr(jobs, "FLUSH_EVERY", 2)
    fake_llm.on("judge", {"verdicts": []})
    fake_llm.on("statements", {"claims": []})
    fake_llm.on("describe_categories", {"descriptions": []})


def pairs(n, start=0, audience="work-slack", word="ok"):
    """``n`` chat replies, one hour apart; each ``said`` is ~1100 chars, so one reply call
    covers two episodes (indices 2k, 2k+1)."""
    out = []
    for i in range(start, start + n):
        ts = BASE + timedelta(hours=i)
        out.append(
            {
                "when": {"start": ts.isoformat(), "end": (ts + timedelta(minutes=1)).isoformat()},
                "audience": audience,
                "category": "communication/style",
                "situation": f"question {i}",
                "said": f"reply {i} " + f"{word} " * 360,
            }
        )
    return out


def note(text, ts=None):
    out = {"category": "food", "said": text}
    if ts is not None:
        out["when"] = ts
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
        n = len(fake_llm.by_purpose("replies"))
        if fail_at is not None and n == fail_at:
            raise exc or RateLimited("daily LLM token limit reached", retry_after=60)
        return claim(DISTINCT[n - 1])

    fake_llm.on("replies", reply)


async def submit(client, records, **kw):
    out = await call(client, "ingest_raw", {"episodes": records, **kw})
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
    assert status["remaining_records"] == list(range(6, 10))


async def test_llm_unavailable_is_partial(client, conn, fake_llm):
    from wwyd.errors import LLMUnavailable

    distinct_replies(fake_llm, fail_at=2, exc=LLMUnavailable("down"))
    _, status = await submit(client, pairs(4))
    assert status["status"] == "partial" and status["error_code"] == "llm_unavailable"
    assert "retry_after" not in status
    assert status["remaining_records"] == [2, 3]
    assert style_texts(conn) == {DISTINCT[0]}


def call_estimates(conn, pid, records):
    batch = parse_batch(
        records,
        tz=UTC,
        audiences={"work-slack", "everyone"},
        default_audience="everyone",
        normalize_category=normalize_category_path,
    )
    calls = passes.build_calls(
        batch, jobs._categories(conn, pid), default_audience="everyone", decision_min_episodes=100
    )
    return [c.estimated_tokens for c in calls]


async def test_budget_stops_before_call_that_would_not_fit(client, conn, user, fake_llm):
    records = pairs(10)
    est = call_estimates(conn, user.persona_id, records)
    assert len(est) == 5
    limiter = get_limiter(conn)
    limiter.set_own_limits(user.user_id, {"llm_tokens_per_day": est[0] + est[1] + est[2] // 2})

    def reply(c):
        # Spend exactly the estimate, like a backend reporting its usage.
        n = len(fake_llm.by_purpose("replies"))
        limiter.record_llm_usage(user.user_id, user.persona_id, est[n - 1], 0, None)
        return claim(DISTINCT[n - 1])

    fake_llm.on("replies", reply)
    out, status = await submit(client, records)
    assert out["estimated_tokens"] == sum(est)
    assert out["remaining_tokens"] == est[0] + est[1] + est[2] // 2
    assert len(fake_llm.by_purpose("replies")) == 2  # the third would not have fit
    assert status["status"] == "partial" and status["error_code"] == "rate_limited"
    assert status["remaining_records"] == list(range(4, 10))
    assert style_texts(conn) == set(DISTINCT[:2])


async def test_no_budget_left_runs_analyzers_only(client, conn, user, fake_llm):
    limiter = get_limiter(conn)
    limiter.set_own_limits(user.user_id, {"llm_tokens_per_day": 1000})
    limiter.record_llm_usage(user.user_id, user.persona_id, 1000, 0, None)
    distinct_replies(fake_llm)
    out, status = await submit(client, [*pairs(2), note("I like tea.", BASE.isoformat())])
    assert out["accepted"] == 3 and out["estimated_tokens"] > 0
    assert out["remaining_tokens"] == 0
    assert fake_llm.by_purpose("replies") == [] and fake_llm.by_purpose("statements") == []
    assert status["status"] == "partial" and status["remaining_records"] == [0, 1, 2]


async def test_no_llm_step_estimates_zero(client):
    out, status = await submit(client, pairs(2), llm_for_ingest=False)
    assert out["estimated_tokens"] == 0 and status["status"] == "done"
    assert "remaining_records" not in status


async def test_saturation_skips_rest_of_audience_but_not_statements(client, conn, fake_llm):
    fake_llm.on("replies", claim("Writes long replies padded with filler words."))
    notes = []

    def note_reply(c):
        notes.append(c)
        return claim("Writes long replies padded with filler words.")

    fake_llm.on("statements", note_reply)
    records = [*pairs(20), note("I write a lot.")]
    _, status = await submit(client, records)
    assert status["status"] == "done"
    # Call 1 is new, calls 2-6 add nothing: calls 7-10 are skipped.
    assert len(fake_llm.by_purpose("replies")) == 1 + passes.SATURATION_CALLS
    counts = status["counts"]
    assert counts["saturated_audiences"] == 1
    assert counts["skipped_saturated"] == 4 * 2
    assert len(notes) == 1  # statements are never skipped


async def test_saturation_counts_existing_claims_as_known(client, conn, fake_llm):
    await call(
        client,
        "add_claims",
        {"claims": [{"kind": "style", "category": "communication/style", "text": "Terse."}]},
    )
    fake_llm.on("replies", claim("Terse."))
    _, status = await submit(client, pairs(14))
    assert len(fake_llm.by_purpose("replies")) == passes.SATURATION_CALLS
    assert status["counts"]["skipped_saturated"] == 2 * 2
    assert status["counts"]["absorbed"] >= 1


async def test_source_watermark(client, conn, fake_llm):
    distinct_replies(fake_llm)
    tea = note("I like tea.")  # no timestamp: never skipped
    out, status = await submit(client, [*pairs(4), tea], source="tg")
    assert status["status"] == "done" and status["counts"]["skipped_seen"] == 0
    (src,) = (await call(client, "list_sources"))["sources"]
    last = BASE + timedelta(hours=3)  # the episode timestamp is when.start
    assert src["source"] == "tg" and src["records_total"] == 5
    assert datetime.fromisoformat(src["last_ts"]) == last

    fake_llm.calls.clear()
    out, status = await submit(client, [*pairs(6), tea], source="tg")
    assert out["accepted"] == 7
    assert status["counts"]["skipped_seen"] == 4
    assert status["counts"]["episodes"]["reply"] == 6  # episodes received, by class
    (src,) = (await call(client, "list_sources"))["sources"]
    assert datetime.fromisoformat(src["last_ts"]) == BASE + timedelta(hours=5)
    assert src["records_total"] == 5 + 3

    # An older batch under another source label does not move "tg"; an older one under
    # "tg" is skipped entirely and never moves the watermark backwards.
    _, status = await submit(client, pairs(2), source="tg")
    assert status["counts"]["skipped_seen"] == 2
    (src,) = (await call(client, "list_sources"))["sources"]
    assert datetime.fromisoformat(src["last_ts"]) == BASE + timedelta(hours=5)

    # Without source nothing is skipped.
    _, status = await submit(client, pairs(2))
    assert status["counts"]["skipped_seen"] == 0

    assert (await call(client, "reset_source", {"source": "tg"}))["deleted"] is True
    assert (await call(client, "list_sources"))["sources"] == []
    assert (await call(client, "reset_source", {"source": "tg"}))["deleted"] is False
    assert (await call_error(client, "ingest_raw", {"episodes": pairs(1), "source": "Bad!"}))[
        "code"
    ] == "invalid"


async def test_partial_watermark_stays_below_remaining(client, conn, fake_llm):
    distinct_replies(fake_llm, fail_at=3)
    start = BASE + timedelta(days=30)
    late_event = {
        "when": {"start": start.isoformat(), "end": (start + timedelta(hours=1)).isoformat()},
        "category": "work/meetings",
        "situation": "invite",
        "decision": "accepted",
    }
    fake_llm.on("decisions", {"claims": []})
    _, status = await submit(client, [*pairs(6), late_event], source="tg")
    assert status["status"] == "partial" and status["remaining_records"] == [4, 5, 6]
    (src,) = (await call(client, "list_sources"))["sources"]
    # The event's decision call never ran; the processed replies count.
    assert datetime.fromisoformat(src["last_ts"]) == BASE + timedelta(hours=3)

    # Resending the remaining episodes is not skipped.
    records = [*pairs(6), late_event]
    fake_llm.on("replies", claim("Terse."))
    _, status = await submit(client, [records[i] for i in status["remaining_records"]], source="tg")
    assert status["status"] == "done" and status["counts"]["skipped_seen"] == 0
    (src,) = (await call(client, "list_sources"))["sources"]
    assert datetime.fromisoformat(src["last_ts"]) == start


async def test_source_tools_owner_only(conn, user):
    for scope, kw in (("read", {}), ("respond", {"audiences": ("work-slack",)})):
        async with Client(make_server(conn, user, scope=scope, **kw)) as c:
            assert (await call_error(c, "list_sources"))["code"] == "forbidden"
            assert (await call_error(c, "reset_source", {"source": "tg"}))["code"] == "forbidden"


async def test_progress_rows_hold_no_episode_text(client, conn, fake_llm):
    distinct_replies(fake_llm, fail_at=2)
    records = pairs(4)
    records[0]["situation"] = CANARY
    records[0]["where"] = CANARY
    records[3]["said"] = f"{CANARY} " + records[3]["said"]
    records.append(note(f"{CANARY} note", BASE.isoformat()))
    out, status = await submit(client, records, source="tg")
    assert status["status"] == "partial"
    rows = [dict(r) for r in conn.execute("SELECT * FROM jobs")]
    rows += [dict(r) for r in conn.execute("SELECT * FROM ingest_sources")]
    dumped = json.dumps(rows)
    assert CANARY not in dumped and "reply 1 ok" not in dumped and "question" not in dumped
    remaining = json.loads(conn.execute("SELECT remaining_records FROM jobs").fetchone()[0])
    assert all(isinstance(i, int) for i in remaining)


async def test_partial_then_resend_counts_each_episode_once(
    client, conn, user, fake_llm, monkeypatch
):
    monkeypatch.setattr(passes, "SATURATION_CALLS", 1000)
    seen: list[str] = []

    def fake_run(conn, *, persona_id, episodes, tz, counts, job_id=None):
        seen.extend(ep.said or "" for ep in episodes)
        return set()

    monkeypatch.setattr(jobs, "run_analyzers", fake_run)
    records = pairs(30)

    def reply(c):
        if len(fake_llm.by_purpose("replies")) == 6:
            raise RateLimited("daily LLM token limit reached", retry_after=60)
        return {"claims": []}

    fake_llm.on("replies", reply)
    _, status = await submit(client, records)
    assert status["status"] == "partial" and status["remaining_records"] == list(range(10, 30))
    fake_llm.on("replies", {"claims": []})
    _, status = await submit(client, [records[i] for i in status["remaining_records"]])
    assert status["status"] == "done"
    assert sorted(seen) == sorted(r["said"] for r in records)
