# SPDX-License-Identifier: AGPL-3.0-or-later
"""ingest_raw / job_status end to end over MCP with a fake LLM (SPEC §6.1, §7)."""

from __future__ import annotations

import json
from datetime import date

import pytest
from conftest import call, call_error
from ingest_data import calendar, messages

from wwyd.errors import SpendCap
from wwyd.ingest import jobs
from wwyd.store import jobs as job_store
from wwyd.store.audiences import upsert_audience

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def setup(conn, pid, fake_llm):
    conn.execute("UPDATE personas SET timezone = 'Europe/Berlin' WHERE id = ?", (pid,))
    upsert_audience(conn, pid, "work-slack")


def style_reply(text="Short, lowercase, no greeting.", category="communication/style"):
    return {
        "claims": [
            {
                "kind": "style",
                "category": category,
                "proposed_category": None,
                "text": text,
                "confidence": 0.7,
            }
        ]
    }


def derivation_calls(fake_llm):
    """Calls that see raw input (compaction's judge only sees derived claim texts)."""
    return [c for c in fake_llm.calls if c["purpose"] in ("style", "notes", "decisions")]


async def run(client, records, **kw):
    out = await call(client, "ingest_raw", {"records": records, **kw})
    assert out["accepted"] == len(records)
    await jobs.wait(out["job_id"])
    return out["job_id"], await call(client, "job_status", {"job_id": out["job_id"]})


async def test_code_and_llm_derivation(client, conn, fake_llm):
    fake_llm.on("style", style_reply())
    fake_llm.on("notes", style_reply("Drinks only black coffee in the morning.", "food"))
    records = (
        calendar()
        + messages("work-slack")
        + [{"type": "note", "text": "I never eat breakfast, just black coffee."}]
    )
    _, status = await run(client, records)
    assert status["status"] == "done" and status["progress"] == 1
    counts = status["counts"]
    assert counts["records"] == {
        "message": 100,
        "call_transcript": 0,
        "calendar_event": len(calendar()),
        "decision": 0,
        "note": 1,
    }
    assert counts["claims_created"] == 8 + 2  # 8 detector keys + style + note
    assert counts["unknown_audiences"] == 0
    # Style prompts are grouped per audience, chunked, and carry the Russian example.
    style_calls = fake_llm.by_purpose("style")
    assert len(style_calls) >= 2
    assert all(c["user"].startswith("Audience: work-slack") for c in style_calls)
    assert all(len(c["user"]) <= 2600 for c in style_calls)
    assert "Example (Russian)" in style_calls[0]["system"]
    assert "communication/style" in style_calls[0]["system"]
    # Duplicates across chunks became one claim.
    listed = await call(client, "list_claims", {"status": "pending", "limit": 200})
    texts = [c["text"] for c in listed["claims"]]
    assert texts.count("Short, lowercase, no greeting.") == 1
    style = next(c for c in listed["claims"] if c["text"] == "Short, lowercase, no greeting.")
    assert style["audiences"] == ["work-slack"] and style["origin"] == "derived"
    assert style["evidence"]["sources"]["message"] == 50


async def test_rerun_updates_keyed_claims(client, conn, fake_llm):
    fake_llm.on("style", {"claims": []})
    await run(client, calendar(first=date(2026, 2, 2), weeks=4))
    _, status = await run(client, calendar(first=date(2026, 3, 2), weeks=4))
    assert status["counts"]["claims_created"] == 0
    assert status["counts"]["claims_updated"] >= 5
    keys = [
        r["key"] for r in conn.execute("SELECT key FROM claims WHERE key IS NOT NULL ORDER BY key")
    ]
    assert len(keys) == len(set(keys))


async def test_llm_off_runs_detectors_only(client, conn, pid, fake_llm):
    _, status = await run(client, messages("work-slack"), llm_for_ingest=False)
    assert status["status"] == "done" and derivation_calls(fake_llm) == []
    assert status["counts"]["claims_created"] == 3
    conn.execute(
        "UPDATE personas SET settings = json_set(settings, '$.llm_for_ingest', json('false'))"
        " WHERE id = ?",
        (pid,),
    )
    # The call cannot turn LLM derivation back on.
    _, status = await run(client, messages("work-slack"), llm_for_ingest=True)
    assert derivation_calls(fake_llm) == []


async def test_unknown_audience_maps_to_default(client, conn, fake_llm):
    fake_llm.on("style", {"claims": []})
    _, status = await run(client, messages("no-such-group"))
    assert status["counts"]["unknown_audiences"] == 100
    assert all(c["user"].startswith("Audience: everyone") for c in fake_llm.by_purpose("style"))
    assert (
        conn.execute(
            "SELECT count(*) FROM claims WHERE key = 'habit:reply_latency:everyone'"
        ).fetchone()[0]
        == 1
    )


async def test_guard_drops_echoed_text(client, fake_llm):
    fake_llm.on("style", lambda c: style_reply(c["user"][-200:]))
    _, status = await run(client, messages("work-slack")[:6])
    assert status["counts"]["dropped_by_guard"] >= 1
    assert status["counts"]["claims_created"] == 0


async def test_decisions_rules_and_singles(client, fake_llm):
    def reply(call):
        return {
            "claims": [
                {
                    "kind": "decision_rule",
                    "category": "work/meetings",
                    "proposed_category": None,
                    "text": "Moves evening calls to the next morning.",
                    "confidence": 0.9,
                    "support": 2,
                },
                {
                    "kind": "decision_rule",
                    "category": "nope",
                    "proposed_category": "travel/flights",
                    "text": "Takes morning flights.",
                    "confidence": 0.9,
                    "support": 1,
                },
            ]
        }

    fake_llm.on("decisions", reply)
    recs = [
        {"type": "decision", "situation": "call at 19:30", "choice": "moved to morning"},
        {"type": "decision", "situation": "meeting at 20:00", "choice": "declined"},
        {"type": "decision", "situation": "flight", "choice": "morning one"},
    ]
    await run(client, recs)
    listed = (await call(client, "list_claims", {"status": "pending"}))["claims"]
    rule = next(c for c in listed if c["text"].startswith("Moves"))
    single = next(c for c in listed if c["text"].startswith("Takes"))
    assert rule["kind"] == "decision_rule" and rule["confidence"] >= 0.6
    assert single["kind"] == "preference" and single["confidence"] <= 0.3
    assert single["category"] == "travel" and single["proposed_category"] == "travel/flights"


async def test_spend_cap_fails_job(client, fake_llm):
    def boom(call):
        raise SpendCap("monthly spend cap reached")

    fake_llm.on("style", boom)
    _, status = await run(client, messages("work-slack"))
    assert status["status"] == "failed" and status["error_code"] == "spend_cap"
    assert status["counts"]["claims_created"] == 3  # detectors ran before the LLM


async def test_invalid_records_rejected_without_echo(client):
    canary = "zq7x-canary-41b9"
    err = await call_error(
        client,
        "ingest_raw",
        {
            "records": [
                {
                    "type": "calendar_event",
                    "start": "2026-02-02T09:00:00Z",
                    "end": "2026-02-02T10:00:00Z",
                    "attendees": 2,
                    "response": "accepted",
                    "title": canary,
                }
            ]
        },
    )
    assert err["code"] == "invalid" and canary not in json.dumps(err)
    err = await call_error(client, "ingest_raw", {"records": [{"type": "message", "text": canary}]})
    assert err["code"] == "invalid" and canary not in json.dumps(err)
    err = await call_error(client, "ingest_raw", {"records": []})
    assert err["code"] == "invalid"
    big = [{"type": "note", "text": "x" * 3000}] * 1001
    assert (await call_error(client, "ingest_raw", {"records": big}))["code"] == "too_large"


async def test_job_status_unknown_and_mark_interrupted(client, conn, pid):
    assert (await call_error(client, "job_status", {"job_id": "job_x"}))["code"] == "not_found"
    job_id = job_store.create_job(conn, pid, {"records": {}})
    job_store.update_job(conn, job_id, status="running")
    assert jobs.mark_interrupted(conn) == 1
    status = await call(client, "job_status", {"job_id": job_id})
    assert status["status"] == "interrupted"


def test_default_audience_alias_in_records():
    from datetime import UTC

    from wwyd.ingest.records import parse_batch

    note = {"type": "decision", "situation": "Lunch?", "choice": "yes", "audience": "default"}
    batch = parse_batch([note], tz=UTC, audiences={"anyone"}, default_audience="anyone")
    assert batch.decisions[0].audience == "anyone" and batch.unknown_audiences == 0
