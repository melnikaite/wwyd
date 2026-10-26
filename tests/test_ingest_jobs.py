# SPDX-License-Identifier: AGPL-3.0-or-later
"""ingest_raw / job_status end to end over MCP with a fake LLM (SPEC §6.1, §7)."""

from __future__ import annotations

import json
from datetime import UTC

import pytest
from conftest import call, call_error
from ingest_data import calendar, messages

from wwyd.errors import SpendCap
from wwyd.ingest import jobs
from wwyd.ingest.episodes import parse_batch
from wwyd.store import jobs as job_store
from wwyd.store.audiences import upsert_audience
from wwyd.store.categories import normalize_category_path

pytestmark = pytest.mark.anyio

PASSES = ("statements", "replies", "decisions", "describe_categories")


@pytest.fixture(autouse=True)
def setup(conn, pid, fake_llm):
    conn.execute("UPDATE personas SET timezone = 'Europe/Berlin' WHERE id = ?", (pid,))
    upsert_audience(conn, pid, "work-slack")
    fake_llm.on("describe_categories", {"descriptions": []})


def style_reply(text="Short, lowercase, no greeting.", category="communication/chat"):
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
    return [c for c in fake_llm.calls if c["purpose"] in PASSES]


async def run(client, episodes, **kw):
    out = await call(client, "ingest_raw", {"episodes": episodes, **kw})
    assert out["accepted"] == len(episodes)
    await jobs.wait(out["job_id"])
    return out["job_id"], await call(client, "job_status", {"job_id": out["job_id"]})


def quiet_m7c(fake_llm):
    """Annotation, authoring and usage notes answer with nothing (tested elsewhere)."""
    fake_llm.on("annotate", {"labels": [], "tags": []})
    fake_llm.on("author_analyzers", {"hypotheses": []})
    fake_llm.on("word_usage", {"note": ""})


async def test_llm_passes_and_counts(client, conn, fake_llm):
    quiet_m7c(fake_llm)
    fake_llm.on("replies", style_reply())
    fake_llm.on("statements", style_reply("Drinks only black coffee in the morning.", "food"))
    episodes = (
        calendar()
        + messages("work-slack")
        + [{"category": "food", "said": "I never eat breakfast, just black coffee."}]
    )
    _, status = await run(client, episodes)
    assert status["status"] == "done" and status["progress"] == 1
    counts = status["counts"]
    n_cal = len(calendar())
    assert counts["episodes"] == {
        "total": n_cal + 51,
        "statement": 1,
        "reply": 50,
        "decision": n_cal,
    }
    # communication/chat was created; the calendar category has >= 100 episodes, so its
    # decisions are left to the analyzers (no decision call).
    assert counts["categories_created"] == 1 and counts["unmapped_categories"] == 0
    assert fake_llm.by_purpose("decisions") == []
    assert counts["claims_created"] == 2
    # Reply prompts are grouped per audience, chunked, and carry the Russian example; the
    # model sees the episode's subtree only.
    reply_calls = fake_llm.by_purpose("replies")
    assert len(reply_calls) >= 2
    assert all(c["user"].startswith("Audience: work-slack") for c in reply_calls)
    assert all(len(c["user"]) <= 2600 for c in reply_calls)
    assert "Example (Russian)" in reply_calls[0]["system"]
    assert "- communication/chat" in reply_calls[0]["system"]
    assert "- food" not in reply_calls[0]["system"]
    # Duplicates across chunks became one claim.
    listed = await call(client, "list_claims", {"status": "pending", "limit": 200})
    texts = [c["text"] for c in listed["claims"]]
    assert texts.count("Short, lowercase, no greeting.") == 1
    style = next(c for c in listed["claims"] if c["text"] == "Short, lowercase, no greeting.")
    assert style["audiences"] == ["work-slack"] and style["origin"] == "derived"
    assert style["category"] == "communication/chat"
    assert style["evidence"]["sources"]["reply"] == 50


async def test_llm_off_runs_no_llm_step(client, conn, pid, fake_llm):
    _, status = await run(client, messages("work-slack"), llm_for_ingest=False)
    assert status["status"] == "done" and derivation_calls(fake_llm) == []
    conn.execute(
        "UPDATE personas SET settings = json_set(settings, '$.llm_for_ingest', json('false'))"
        " WHERE id = ?",
        (pid,),
    )
    # The call cannot turn LLM derivation back on.
    _, status = await run(client, messages("work-slack"), llm_for_ingest=True)
    assert derivation_calls(fake_llm) == []


async def test_analyzers_run_over_processed_episodes(client, monkeypatch, fake_llm):
    seen = []

    def fake_run(conn, *, persona_id, episodes, tz, counts, job_id=None):
        seen.append(sorted(ep.index for ep in episodes))
        return set()

    monkeypatch.setattr(jobs, "run_analyzers", fake_run)

    def boom(call):
        raise SpendCap("monthly spend cap reached")

    fake_llm.on("replies", boom)
    statements = [{"category": "food", "said": "I like tea."}] * 3
    _, status = await run(client, statements + messages("work-slack")[:4], llm_for_ingest=False)
    assert status["status"] == "done" and seen[-1] == list(range(7))
    fake_llm.on("statements", {"claims": []})
    _, status = await run(client, statements + messages("work-slack")[:4])
    assert status["status"] == "partial" and status["remaining_records"] == [3, 4, 5, 6]
    assert seen[-1] == [0, 1, 2]


async def test_unknown_audience_maps_to_default(client, fake_llm):
    fake_llm.on("replies", {"claims": []})
    _, status = await run(client, messages("no-such-group"))
    assert status["counts"]["unknown_audiences"] == 50
    assert all(c["user"].startswith("Audience: everyone") for c in fake_llm.by_purpose("replies"))


async def test_reply_without_audience_uses_default_for_style(client, fake_llm):
    fake_llm.on("replies", {"claims": []})
    fake_llm.on("statements", {"claims": []})
    eps = [{"category": "chat", "situation": "hi?", "said": "hey"}, {"category": "x", "said": "a"}]
    _, status = await run(client, eps)
    assert status["counts"]["unknown_audiences"] == 0
    (reply,) = fake_llm.by_purpose("replies")
    assert reply["user"].startswith("Audience: everyone")
    (statement,) = fake_llm.by_purpose("statements")
    assert not statement["user"].startswith("Audience:")


async def test_guard_drops_echoed_text(client, fake_llm):
    fake_llm.on("replies", lambda c: style_reply(c["user"][-200:]))
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
                    "used_items": [1, 2],
                },
                {
                    "kind": "decision_rule",
                    "category": "nope",
                    "proposed_category": "work/meetings/evening",
                    "text": "Takes morning slots.",
                    "confidence": 0.9,
                    "used_items": [1],
                },
                {
                    "kind": "decision_rule",
                    "category": "travel",  # outside the episode's subtree
                    "proposed_category": None,
                    "text": "Prefers trains.",
                    "confidence": 0.9,
                    "used_items": [1, 2],
                },
            ]
        }

    fake_llm.on("decisions", reply)
    eps = [
        {"category": "work/meetings", "situation": "call at 19:30", "decision": "moved"},
        {"category": "work/meetings", "situation": "meeting at 20:00", "decision": "declined"},
        {"category": "work/meetings", "decision": "booked a morning slot"},  # no situation
    ]
    await run(client, eps)
    (dcall,) = fake_llm.by_purpose("decisions")
    assert "Category: work/meetings" in dcall["user"]
    assert "3. CHOICE: booked a morning slot" in dcall["user"]
    assert "- work/meetings" in dcall["system"] and "- travel" not in dcall["system"]
    listed = (await call(client, "list_claims", {"status": "pending"}))["claims"]
    rule = next(c for c in listed if c["text"].startswith("Moves"))
    single = next(c for c in listed if c["text"].startswith("Takes"))
    outside = next(c for c in listed if c["text"].startswith("Prefers"))
    assert rule["kind"] == "decision_rule" and rule["confidence"] >= 0.6
    assert single["kind"] == "preference" and single["confidence"] <= 0.3
    assert single["category"] == "work/meetings"
    assert single["proposed_category"] == "work/meetings/evening"
    assert outside["category"] == "work/meetings" and outside["proposed_category"] == "travel"


async def test_spend_cap_ends_job_partial(client, conn, fake_llm):
    def boom(call):
        raise SpendCap("monthly spend cap reached")

    quiet_m7c(fake_llm)
    fake_llm.on("replies", boom)
    _, status = await run(client, calendar() + messages("work-slack"))
    assert status["status"] == "partial" and status["error_code"] == "spend_cap"
    assert status["retry_after"] > 0
    # Every reply is still to be resent; the calendar (no call) was processed.
    n = len(calendar())
    assert status["remaining_records"] == list(range(n, n + 50))


async def test_invalid_episodes_rejected_without_echo(client):
    canary = "zq7x-canary-41b9"
    bad = [
        {"category": "work", "said": "hi", "title": canary},
        {"category": "work", "said": "hi", "when": {"start": "2026-02-02T09:00", canary: 1}},
        {"category": "work", "situation": canary},  # neither decision nor said
        {"category": f"work/{canary}!", "said": "hi"},
        {"category": "a/b/c/d", "said": "hi"},
        {"category": "work", "said": "hi", "when": canary},
        {"category": "work", "said": "hi", "where": "x" * 101},
        {"category": "work", "said": "hi", "when": {"start": "2026-02-02T10:00", "end": "2026"}},
        {
            "category": "work",
            "said": "x",
            "when": {"start": "2026-02-02T10:00Z", "end": "2026-02-02T09:00Z"},
        },
    ]
    for episode in bad:
        err = await call_error(client, "ingest_raw", {"episodes": [episode]})
        assert err["code"] == "invalid" and canary not in json.dumps(err), episode
    assert (await call_error(client, "ingest_raw", {"episodes": []}))["code"] == "invalid"
    big = [{"category": "food", "said": "x" * 3000}] * 1001
    assert (await call_error(client, "ingest_raw", {"episodes": big}))["code"] == "too_large"


async def test_job_status_unknown_and_mark_interrupted(client, conn, pid):
    assert (await call_error(client, "job_status", {"job_id": "job_x"}))["code"] == "not_found"
    job_id = job_store.create_job(conn, pid, {"episodes": {}})
    job_store.update_job(conn, job_id, status="running")
    assert jobs.mark_interrupted(conn) == 1
    status = await call(client, "job_status", {"job_id": job_id})
    assert status["status"] == "interrupted"


def parse(episodes, **kw):
    return parse_batch(
        episodes,
        tz=kw.get("tz", UTC),
        audiences=kw.get("audiences", {"anyone"}),
        default_audience="anyone",
        normalize_category=normalize_category_path,
    )


def test_default_audience_alias_and_none():
    batch = parse(
        [
            {"category": "x", "situation": "Lunch?", "decision": "yes", "audience": "default"},
            {"category": "x", "said": "a"},
        ]
    )
    assert batch.episodes[0].audience == "anyone" and batch.unknown_audiences == 0
    assert batch.episodes[1].audience is None


def test_classification():
    batch = parse(
        [
            {"category": "x", "said": "about me"},
            {"category": "x", "situation": "q?", "said": "a"},
            {"category": "x", "situation": "invite", "decision": "declined"},
            {"category": "x", "situation": "q?", "said": "no", "decision": "declined"},
            {"category": "x", "decision": "watched to the end"},
            {"category": "x", "decision": "bought it", "said": "love it"},
        ]
    )
    assert [ep.classes for ep in batch.episodes] == [
        ("statement",),
        ("reply",),
        ("decision",),
        ("reply", "decision"),
        ("decision",),
        ("statement", "decision"),
    ]
    assert batch.counts() == {"total": 6, "statement": 2, "reply": 2, "decision": 4}


def test_times_are_converted_to_the_persona_timezone():
    from zoneinfo import ZoneInfo

    berlin = ZoneInfo("Europe/Berlin")
    batch = parse(
        [
            {"category": "x", "said": "a", "when": "2026-02-02T09:00:00Z"},
            {"category": "x", "said": "a", "when": {"start": "2026-02-02T09:00"}},
        ],
        tz=berlin,
    )
    first, second = batch.episodes
    assert first.start.utcoffset().total_seconds() == 3600 and first.start.hour == 10
    assert second.start.tzinfo is berlin and second.start.hour == 9 and second.end is None


def test_duration_across_dst():
    from zoneinfo import ZoneInfo

    batch = parse(
        [
            {
                "category": "x",
                "said": "a",
                "when": {"start": "2026-03-29T01:30:00", "end": "2026-03-29T03:30:00"},
            }
        ],
        tz=ZoneInfo("Europe/Berlin"),
    )
    assert batch.episodes[0].duration_s == 3600  # clocks jump 02:00 -> 03:00
