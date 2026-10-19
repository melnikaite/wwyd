# SPDX-License-Identifier: AGPL-3.0-or-later
"""D23 on the ingest side: optional category with server placement, ``situation_at`` and
latency, corrections (SPEC §4.4, §6.1, §7.1, §7.6, §4.5)."""

from __future__ import annotations

import json
import math
from datetime import UTC, date, datetime, timedelta

import pytest
from analyzer_data import BERLIN, add_audience, cat_id, replies, workdays
from conftest import call, call_error

from wwyd.analyzers.builtin import time_duration, time_gaps
from wwyd.analyzers.builtin.base import Context, host_fields
from wwyd.config import Thresholds, configure_thresholds
from wwyd.errors import Invalid, LLMUnavailable
from wwyd.ingest import jobs, passes, place
from wwyd.ingest.episodes import Episode, parse_batch, validate_episode
from wwyd.limits import get_limiter
from wwyd.store import audit
from wwyd.store.claims import add_claims
from wwyd.store.evidence import evidence_weight

pytestmark = pytest.mark.anyio

BASE = datetime(2026, 3, 2, 9, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def setup(fake_llm):
    fake_llm.on("statements", {"claims": []})
    fake_llm.on("replies", {"claims": []})
    fake_llm.on("decisions", {"claims": []})
    fake_llm.on("describe_categories", {"descriptions": []})
    fake_llm.on("judge", {"verdicts": []})


async def ingest(client, episodes, **kw):
    out = await call(client, "ingest_raw", {"episodes": episodes, **kw})
    await jobs.wait(out["job_id"])
    return out, await call(client, "job_status", {"job_id": out["job_id"]})


def path_of(conn, pid, path):
    return conn.execute(
        "SELECT id, description FROM categories WHERE persona_id = ? AND path = ?", (pid, path)
    ).fetchone()


# --- schema ------------------------------------------------------------------------------


def test_category_is_optional():
    validate_episode(0, {"said": "hi"})


@pytest.mark.parametrize("field", ["situation_at", "corrects"])
def test_needs_situation(field):
    value = "2026-03-02T09:00:00Z" if field == "situation_at" else "adt_" + "a" * 16
    with pytest.raises(Invalid) as info:
        validate_episode(3, {"said": "hi", field: value})
    payload = info.value.to_payload()
    assert (payload["index"], payload["path"]) == (3, field)
    assert "needs situation" in payload["message"]
    validate_episode(3, {"said": "hi", "situation": "q", field: value})


@pytest.mark.parametrize("value", ["adt_short", "clm_" + "a" * 16, "adt_" + "A" * 16, "x" * 300])
def test_corrects_must_be_a_prediction_id(value):
    with pytest.raises(Invalid) as info:
        validate_episode(0, {"said": "hi", "situation": "q", "corrects": value})
    assert value not in json.dumps(info.value.to_payload())


def test_latency_property():
    tz = BERLIN
    eps = parse_batch(
        [
            {"situation": "q", "said": "a", "situation_at": "2026-03-02T09:00:00Z",
             "when": "2026-03-02T09:04:00Z"},
            {"situation": "q", "decision": "d", "situation_at": "2026-03-02T09:00:00Z",
             "when": {"start": "2026-03-02T10:00:00Z", "end": "2026-03-02T10:30:00Z"}},
            {"situation": "q", "said": "a", "situation_at": "2026-03-02T09:00:00Z",
             "when": "2026-03-02T08:00:00Z"},
            {"situation": "q", "said": "a", "situation_at": "2026-03-02T09:00:00Z"},
            # Across the DST switch (2026-03-29 in Berlin): UTC instants.
            {"situation": "q", "said": "a", "situation_at": "2026-03-29T01:30:00+01:00",
             "when": "2026-03-29T03:30:00+02:00"},
        ],
        tz=tz,
        audiences=set(),
        default_audience="everyone",
        normalize_category=lambda p: p,
    ).episodes  # fmt: skip
    assert [ep.latency_s for ep in eps] == [240.0, 3600.0, None, None, 3600.0]
    assert all(ep.category is None and ep.category_id == "" for ep in eps)


# --- analyzers ---------------------------------------------------------------------------


def _ctx(conn, pid, path="communication/style"):
    return Context("anl_x", "category", cat_id(conn, pid, path), path, BERLIN)


def test_latency_from_situation_at_for_replies_and_decisions(conn, pid):
    aud = add_audience(conn, pid, "work-chat")
    style = cat_id(conn, pid, "communication/style")
    days = workdays(date(2026, 3, 2), 30)
    eps = replies(style, days, "work-chat")
    # Invitations answered 10 min later, outside work hours (arrived at 20:00).
    for i, d in enumerate(days):
        arrived = datetime(d.year, d.month, d.day, 20, 0, tzinfo=BERLIN)
        eps.append(
            Episode(2000 + i, style, "communication/style", "work-chat",
                    start=arrived + timedelta(minutes=10), situation_at=arrived,
                    situation="invite", decision="declined")
        )  # fmt: skip
    items = [host_fields(ep, BERLIN, {"work-chat": aud}) for ep in eps]
    assert items[0].latency_s == 180 and items[0].local_situation_at.hour == 10
    out = time_duration.run(items, _ctx(conn, pid))
    latency = [(k, v) for month in out["latency"].values() for k, v in month.items()]
    assert sum(v for k, v in latency if k.startswith(f"{aud}.1.")) == 30
    assert sum(v for k, v in latency if k.startswith(f"{aud}.0.")) == 30
    assert "length" not in out  # moments only: no spans


def test_reply_intervals_feed_lengths_and_gaps(conn, pid):
    """A call (situation + said) with a span is a span now, not latency (D23)."""
    aud = add_audience(conn, pid, "work-chat")
    style = cat_id(conn, pid, "communication/style")
    eps = []
    for i, d in enumerate(workdays(date(2026, 3, 2), 5)):
        for k, hour in enumerate((10, 11)):
            start = datetime(d.year, d.month, d.day, hour, 0, tzinfo=BERLIN)
            eps.append(
                Episode(i * 2 + k, style, "communication/style", "work-chat", start=start,
                        end=start + timedelta(minutes=45), situation="others", said="mine")
            )  # fmt: skip
    items = [host_fields(ep, BERLIN, {"work-chat": aud}) for ep in eps]
    out = time_duration.run(items, _ctx(conn, pid))
    assert "latency" not in out
    assert sum(v for month in out["length"].values() for v in month.values()) == 10
    gaps = time_gaps.run(items, _ctx(conn, pid))
    assert sum(v for month in gaps["gap"].values() for v in month.values()) == 5


def test_host_fields_in_sandbox_input(conn, pid):
    from wwyd.analyzers.llm import episode_json

    ep = Episode(0, "", "x", None, start=BASE + timedelta(minutes=2), situation_at=BASE,
                 situation="q", said="a")  # fmt: skip
    assert episode_json(host_fields(ep, BERLIN, {}), "x")["latency_s"] == 120


# --- placement ---------------------------------------------------------------------------


def films(n=3, **extra):
    return [
        {"when": (BASE + timedelta(days=i)).isoformat(), "situation": f"evening {i}",
         "decision": "watched to the end", **extra}
        for i in range(n)
    ]  # fmt: skip


def placing(fake_llm, paths, new=()):
    described = dict(new)

    def handler(c):
        lines = c["user"].split("Episodes:\n", 1)[1].splitlines()
        items = []
        for i in range(1, len(lines) + 1):
            path = paths[(i - 1) % len(paths)]
            items.append({"i": i, "path": path, "path_description": described.get(path, "")})
        return {"placements": items}

    fake_llm.on("place", handler)


async def test_place_existing_path(client, conn, pid, fake_llm):
    placing(fake_llm, ["food"])
    out, status = await ingest(client, films())
    calls = fake_llm.by_purpose("place")
    assert len(calls) == 1
    assert calls[0]["system"] == place.SYSTEM  # static: prefix caching
    assert "- work/meetings" in calls[0]["user"] and "3. SITUATION: evening 2" in calls[0]["user"]
    c = status["counts"]
    assert c["unplaced_episodes"] == 0 and c["categories_created"] == 0
    assert out["estimated_tokens"] > 0
    # The decision pass ran on the placed episodes, in their category.
    (decision,) = fake_llm.by_purpose("decisions")
    assert "Category: food" in decision["user"]


async def test_place_new_path_with_description(client, conn, pid, fake_llm):
    placing(fake_llm, ["hobbies/cinema"], [("hobbies/cinema", "Which movies are seen.")])
    out, status = await ingest(client, films())
    row = path_of(conn, pid, "hobbies/cinema")
    assert row is not None and row["description"] == "Which movies are seen."
    assert status["counts"]["categories_created"] == 1
    assert fake_llm.by_purpose("describe_categories") == []  # no separate description call
    change = conn.execute(
        "SELECT job_id, payload FROM changes WHERE op = 'category_create'"
    ).fetchone()
    assert change["job_id"] == out["job_id"]
    assert json.loads(change["payload"])["categories"][0]["path"] == "hobbies/cinema"


async def test_place_empty_description_gets_describe_call(client, conn, pid, fake_llm):
    placing(fake_llm, ["hobbies/cinema"])
    fake_llm.on(
        "describe_categories",
        {"descriptions": [{"path": "hobbies/cinema", "description": "Which films are seen."}]},
    )
    _, status = await ingest(client, films())
    (describe,) = fake_llm.by_purpose("describe_categories")
    assert describe["user"] == "- hobbies/cinema"
    assert path_of(conn, pid, "hobbies/cinema")["description"] == "Which films are seen."
    assert status["counts"]["categories_created"] == 1


async def test_place_change_is_undoable(client, conn, pid, fake_llm):
    placing(fake_llm, ["hobbies/cinema"], [("hobbies/cinema", "What the owner watches.")])
    await ingest(client, films())
    change = conn.execute("SELECT id FROM changes WHERE op = 'category_create'").fetchone()
    await call(client, "undo_change", {"id": change["id"]})
    assert path_of(conn, pid, "hobbies/cinema") is None


async def test_place_guard_failure_maps_to_ancestor(client, conn, pid, fake_llm):
    # "evening" occurs in 3 episodes only: a concentrated batch word (possibly a name).
    placing(fake_llm, ["hobbies/evening"], [("hobbies/evening", "Evening plans.")])
    _, status = await ingest(client, films())
    assert path_of(conn, pid, "hobbies/evening") is None
    c = status["counts"]
    assert c["unplaced_episodes"] == 3 and c["dropped_by_guard"] >= 1
    (decision,) = fake_llm.by_purpose("decisions")
    assert "Category: hobbies" in decision["user"]


async def test_place_guard_failure_without_ancestor_goes_to_other(client, conn, pid, fake_llm):
    placing(fake_llm, ["zq7xcanary/films"])
    _, status = await ingest(client, films(situation="zq7xcanary"))
    assert path_of(conn, pid, "zq7xcanary") is None
    dump = "\n".join(conn.iterdump())
    assert "zq7xcanary" not in dump
    assert status["counts"]["unplaced_episodes"] == 3
    assert "Category: other" in fake_llm.by_purpose("decisions")[0]["user"]


async def test_place_description_guarded(client, conn, pid, fake_llm):
    placing(fake_llm, ["hobbies/cinema"], [("hobbies/cinema", "Watches with mail@example.com.")])
    _, status = await ingest(client, films())
    assert path_of(conn, pid, "hobbies/cinema")["description"] == ""
    assert status["counts"]["dropped_by_guard"] == 1


async def test_place_no_usable_path(client, conn, pid, fake_llm):
    fake_llm.on("place", {"placements": [{"i": 1, "path": "a//b", "path_description": ""}]})
    _, status = await ingest(client, films())
    assert status["status"] == "done" and status["counts"]["unplaced_episodes"] == 3


async def test_place_limits(client, conn, pid, user, fake_llm):
    get_limiter(conn).set_own_limits(user.user_id, {"new_categories_per_job": 1})
    placing(fake_llm, ["hobbies/cinema"])
    episodes = [{"category": "tech/phones", "said": "x"}, *films()]
    _, status = await ingest(client, episodes)
    c = status["counts"]
    # tech/phones used the job's one creation; the placed path maps to its ancestor.
    assert path_of(conn, pid, "tech/phones") is not None
    assert path_of(conn, pid, "hobbies/cinema") is None
    assert c["categories_created"] == 1 and c["unmapped_categories"] == 3


async def test_client_paths_never_replaced(client, conn, pid, fake_llm):
    placing(fake_llm, ["food"])
    episodes = [*films(2), {"category": "hobbies", "situation": "q", "decision": "d"}]
    await ingest(client, episodes)
    (call_,) = fake_llm.by_purpose("place")
    assert "2. " in call_["user"] and "3. " not in call_["user"]


async def test_place_batches(client, conn, pid, fake_llm):
    placing(fake_llm, ["food"])
    long = "word " * 120
    eps = [{"situation": f"{i} {long}", "said": "x"} for i in range(60)]
    await ingest(client, eps)
    calls = fake_llm.by_purpose("place")
    assert len(calls) > 3  # 60 episodes of ~600 chars: chars bound, not only 25
    for c in calls:
        episodes = c["user"].split("Episodes:\n", 1)[1]
        assert len(episodes) <= place.MAX_CHARS + 50 * 5
        assert all(len(line) < 700 for line in episodes.splitlines())
    eps = [{"situation": f"s{i}", "said": "x"} for i in range(120)]
    fake_llm.calls.clear()
    await ingest(client, eps)
    assert len(fake_llm.by_purpose("place")) == 5  # 120 / MAX_EPISODES (25)


async def test_place_extra_paths_create_nothing(client, conn, pid, fake_llm):
    items = [{"i": i, "path": "food", "path_description": ""} for i in (1, 2, 3)]
    items += [{"i": 9, "path": "hobbies/tv", "path_description": "Which shows the owner watches."},
              {"i": 10, "path": "hobbies/music", "path_description": "What the owner hears."}]  # fmt: skip
    fake_llm.on("place", {"placements": items})
    _, status = await ingest(client, films())
    assert path_of(conn, pid, "hobbies/tv") is None and path_of(conn, pid, "hobbies/music") is None
    assert status["counts"]["categories_created"] == 0
    assert status["counts"]["unplaced_episodes"] == 0


async def test_place_duplicate_new_path_first_description_wins(client, conn, pid, fake_llm):
    items = [
        {"i": 1, "path": "hobbies/cinema", "path_description": ""},
        {"i": 2, "path": "hobbies/cinema", "path_description": "Watches with mail@example.com."},
        {"i": 3, "path": "hobbies/cinema", "path_description": "Which films the owner sees."},
    ]
    fake_llm.on("place", {"placements": items})
    _, status = await ingest(client, films())
    row = path_of(conn, pid, "hobbies/cinema")
    assert row["description"] == "Which films the owner sees."
    assert status["counts"]["categories_created"] == 1


async def test_no_llm_goes_to_other(client, conn, pid, fake_llm):
    _, status = await ingest(client, films(), llm_for_ingest=False)
    assert fake_llm.calls == []
    assert status["counts"]["unplaced_episodes"] == 3
    out = await call(client, "ingest_raw", {"episodes": films(), "llm_for_ingest": False})
    assert out["estimated_tokens"] == 0


async def test_place_llm_error_leaves_remaining(client, conn, pid, fake_llm):
    def fail(c):
        raise LLMUnavailable("down")

    fake_llm.on("place", fail)
    episodes = [{"category": "food", "said": "likes tea"}, *films()]
    _, status = await ingest(client, episodes)
    assert status["status"] == "partial" and status["error_code"] == "llm_unavailable"
    assert status["remaining_records"] == [0, 1, 2, 3]
    assert fake_llm.by_purpose("statements") == []


async def test_estimate_covers_placement_and_passes(conn, pid, user, fake_llm):
    placing(fake_llm, ["food"])
    eps = films(20)
    out = jobs.submit(conn, user.user_id, pid, eps)
    await jobs.wait(out["job_id"])
    used = sum(
        -(-len(c["system"] + c["user"]) // passes.CHARS_PER_TOKEN)
        + passes.OUTPUT_TOKENS[c["purpose"]]
        for c in fake_llm.calls
    )
    assert out["estimated_tokens"] >= used


# --- corrections -------------------------------------------------------------------------


def seed_claim(conn, pid, text, **kw):
    item = {"kind": "decision_rule", "category": "work/meetings", "text": text,
            "status": "approved", "confidence": 0.6, **kw}  # fmt: skip
    return add_claims(conn, pid, [item], absorb=False, allow_approved=True)["created"][0]


def prediction(conn, pid, used):
    return audit.record(conn, pid, scope="owner", outcome="answer", used_claims=used)


def correction(pred, **kw):
    return {"category": "work/meetings", "situation": "Invite at 19:30 for a call.",
            "decision": "Accepted, evening calls are fine.", "corrects": pred, **kw}  # fmt: skip


async def test_unknown_prediction(client, conn, pid, fake_llm):
    _, status = await ingest(client, [correction("adt_" + "a" * 16)])
    c = status["counts"]
    assert c["unknown_predictions"] == 1 and c["corrections"] == 0
    (decision,) = fake_llm.by_purpose("decisions")
    assert passes.CORRECTION_MARKER not in decision["user"]


async def test_prediction_of_other_persona_is_unknown(client, conn, pid, fake_llm):
    from wwyd.store.personas import create_user

    other = create_user(conn, "someone-else")
    pred = prediction(conn, other.persona_id, [])
    _, status = await ingest(client, [correction(pred)])
    assert status["counts"]["unknown_predictions"] == 1


async def test_correction_own_call_marker_and_source(client, conn, pid, fake_llm):
    old = seed_claim(conn, pid, "Declines meetings after 19:00.")
    pred = prediction(conn, pid, [old])
    fake_llm.on(
        "decisions",
        {"claims": [{"kind": "decision_rule", "category": "work/meetings",
                     "proposed_category": None, "text": "Accepts evening calls.",
                     "confidence": 0.7, "support": 2}]},
    )  # fmt: skip
    plain = {"category": "work/meetings", "situation": "Standup at 9", "decision": "went"}
    _, status = await ingest(client, [plain, correction(pred)])
    assert status["counts"]["corrections"] == 1
    calls = fake_llm.by_purpose("decisions")
    assert len(calls) == 2
    marked = [c for c in calls if passes.CORRECTION_MARKER in c["user"]]
    assert len(marked) == 1 and "Standup" not in marked[0]["user"]
    rows = conn.execute(
        "SELECT evidence FROM claims WHERE text = 'Accepts evening calls.'"
    ).fetchall()
    sources = [json.loads(r["evidence"])["sources"] for r in rows]
    assert {"correction": 1} in sources or any(s.get("correction") == 1 for s in sources)


async def test_correction_forced_judge_and_weight_wins(client, conn, pid, fake_llm):
    old = seed_claim(conn, pid, "Declines meetings after 19:00.", confidence=0.6,
                     evidence={"sources": {"decision": 2}})  # fmt: skip
    pred = prediction(conn, pid, [old])
    fake_llm.on(
        "replies",
        {"claims": [{"kind": "preference", "category": "communication/style",
                     "proposed_category": None, "text": "Happy to talk shop at night.",
                     "confidence": 0.6}]},
    )  # fmt: skip
    judged = []

    def judge(c):
        items = json.loads(c["user"])
        judged.extend(items)
        return {"verdicts": [{"pair": it["pair"], "verdict": "contradicts"} for it in items]}

    fake_llm.on("judge", judge)
    ep = {"category": "communication/style", "situation": "Call tonight?", "said": "sure",
          "corrects": pred, "audience": "default"}  # fmt: skip
    _, status = await ingest(client, [ep])
    assert status["status"] == "done"
    # Different kind and category, no shared words: judged only because it is forced.
    assert any({it["a"], it["b"]} == {"Declines meetings after 19:00.",
                                      "Happy to talk shop at night."} for it in judged)  # fmt: skip
    # 0.6 × log(1 + 3) beats 0.6 × log(1 + 2): the approved loser becomes a proposal.
    change = conn.execute("SELECT state, payload FROM changes WHERE op = 'supersede'").fetchone()
    assert change["state"] == "proposed"
    assert json.loads(change["payload"])["plan"]["loser"] == old


async def test_correction_not_saturated(client, conn, pid, fake_llm, monkeypatch):
    monkeypatch.setattr(passes, "SATURATION_CALLS", 1)
    old = seed_claim(conn, pid, "Declines meetings after 19:00.")
    pred = prediction(conn, pid, [old])
    long = "ok " * 900
    eps = [
        {"when": (BASE + timedelta(hours=i)).isoformat(), "category": "communication/style",
         "situation": f"q{i}", "said": f"r{i} {long}"}
        for i in range(3)
    ]  # fmt: skip
    eps.append({"when": (BASE + timedelta(hours=9)).isoformat(),
                "category": "communication/style", "situation": "q", "said": "fine",
                "corrects": pred})  # fmt: skip
    _, status = await ingest(client, eps)
    assert status["counts"]["saturated_audiences"] == 1
    replies_ = fake_llm.by_purpose("replies")
    assert any(passes.CORRECTION_MARKER in c["user"] for c in replies_)


def test_weighted_evidence_weight():
    ev = {"sources": {"decision": 2, "correction": 1}}
    assert evidence_weight(0.5, ev) == pytest.approx(0.5 * math.log(6))
    configure_thresholds(Thresholds(correction_weight=1.0))
    try:
        assert evidence_weight(0.5, json.dumps(ev)) == pytest.approx(0.5 * math.log(4))
    finally:
        configure_thresholds(Thresholds())
    assert evidence_weight(0.9, None) == 0.0


def test_config_correction_weight(monkeypatch):
    from wwyd.config import ConfigError, load_config

    monkeypatch.setenv("WWYD_CORRECTION_WEIGHT", "4.5")
    assert load_config().thresholds.correction_weight == 4.5
    monkeypatch.setenv("WWYD_CORRECTION_WEIGHT", "0.5")
    with pytest.raises(ConfigError):
        load_config()


async def test_tool_rejects_situation_at_without_situation(client):
    err = await call_error(
        client, "ingest_raw", {"episodes": [{"said": "x", "situation_at": BASE.isoformat()}]}
    )
    assert err["code"] == "invalid"


async def test_forced_same_across_kinds_is_different(client, conn, pid, fake_llm):
    old = seed_claim(conn, pid, "Declines meetings after 19:00.")
    pred = prediction(conn, pid, [old])
    fake_llm.on(
        "replies",
        {"claims": [{"kind": "preference", "category": "communication/style",
                     "proposed_category": None, "text": "Happy to talk shop at night.",
                     "confidence": 0.6}]},
    )  # fmt: skip

    def judge(c):
        items = json.loads(c["user"])
        return {"verdicts": [{"pair": it["pair"], "verdict": "same"} for it in items]}

    fake_llm.on("judge", judge)
    ep = {"category": "communication/style", "situation": "Call tonight?", "said": "sure",
          "corrects": pred}  # fmt: skip
    _, status = await ingest(client, [ep])
    assert status["status"] == "done"
    assert (
        conn.execute("SELECT COUNT(*) FROM changes WHERE op IN ('merge', 'absorb')").fetchone()[0]
        == 0
    )
    statuses = {r[0]: r[1] for r in conn.execute("SELECT text, status FROM claims")}
    assert statuses["Declines meetings after 19:00."] == "approved"
    assert statuses["Happy to talk shop at night."] == "pending"


def test_context_rank():
    assert evidence_weight(0.9, None) == 0.0
    from wwyd.store.evidence import context_rank

    assert context_rank(0.9, None) == pytest.approx(0.9)
    ev = {"sources": {"decision": 6, "correction": 1}}
    assert context_rank(0.5, ev) == pytest.approx(0.5 * (1 + math.log10(10)))
