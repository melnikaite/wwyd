# SPDX-License-Identifier: AGPL-3.0-or-later
"""Style in predict (SPEC §8.1 steps 6 and 8, D22, §13 "Style in predict")."""

from __future__ import annotations

import functools
from datetime import date

import pytest
from analyzer_data import BERLIN, add_audience, cat_id, workdays
from test_predict import answer, owner, respond
from test_stylometry import FAMILY, WORK, said_episodes

import wwyd.analyzers as analyzers_mod
from wwyd.analyzers import run_analyzers
from wwyd.analyzers.builtin import base
from wwyd.errors import LLMUnavailable
from wwyd.predict import predict
from wwyd.predict.prompts import ANSWER_SYSTEM
from wwyd.store.claims import insert_claim

pytestmark = pytest.mark.anyio

STATS = {
    "n": 120,
    "chars_p50": 20.0,
    "chars_p90": 30.0,
    "sentences_p50": 1.0,
    "sentences_p90": 2.0,
    "lowercase_share": 0.95,
    "capital_share": 0.05,
    "ending_dominant": 7,  # none
    "ending_share": 0.9,
    "period_share": 0.05,
    "emoji_share": 0.1,
}
STYLE_HEADER = "Owner's style with this audience"


@pytest.fixture
def auds(conn, pid):
    return {name: add_audience(conn, pid, name) for name in ("family", "work")}


def style_claim(conn, pid, aud, *, status="approved", visibility="shared", **stats):
    return insert_claim(
        conn,
        pid,
        kind="style",
        category_id=cat_id(conn, pid, "communication/style"),
        text="Writes to family in 1 short line, lowercase, no final period (120 messages).",
        audiences_all=False,
        audience_ids=[aud],
        visibility=visibility,
        status=status,
        confidence=0.9,
        evidence={"sources": {"analyzer": 120}, "stats": {**STATS, **stats}},
        origin="derived",
        key=f"builtin:stylometry:{aud}",
    )


def _user(fake_llm) -> str:
    return fake_llm.by_purpose("answer")[-1]["user"]


async def test_parameters_only_for_approved_visible_reply(conn, user, pid, fake_llm, auds):
    style_claim(conn, pid, auds["family"])
    fake_llm.on("answer", answer("ok"))
    await predict(conn, owner(user), situation="dinner?", audience="family")
    first = fake_llm.by_purpose("answer")[-1]
    assert STYLE_HEADER in first["user"]
    assert "90% under 30" in first["user"] and "starts lowercase in 95%" in first["user"]
    assert "final period in 5%" in first["user"]
    assert first["system"] == ANSWER_SYSTEM  # static, byte-identical

    # decision: no parameters
    await predict(conn, owner(user), situation="dinner?", audience="family", kind="decision")
    assert STYLE_HEADER not in _user(fake_llm)
    assert fake_llm.by_purpose("answer")[-1]["system"] == ANSWER_SYSTEM


async def test_other_audience_and_unapproved_are_ignored(conn, user, pid, fake_llm, auds):
    style_claim(conn, pid, auds["family"])
    style_claim(conn, pid, auds["work"], status="pending", lowercase_share=0.0)
    insert_claim(  # something visible for work so predict calls the LLM
        conn,
        pid,
        kind="style",
        category_id=cat_id(conn, pid, "communication/style"),
        text="Short, no greeting.",
        audiences_all=True,
        audience_ids=[],
        visibility="shared",
        status="approved",
        confidence=0.9,
        evidence={},
        origin="added",
    )
    fake_llm.on("answer", answer("Ok."))
    out = await predict(conn, owner(user), situation="meeting?", audience="work")
    assert STYLE_HEADER not in _user(fake_llm)
    assert out["answer"] == "Ok."  # no post-processing without a profile


async def test_owner_only_profile_needs_private_owner(conn, user, pid, fake_llm, auds):
    style_claim(conn, pid, auds["family"], visibility="owner_only")
    insert_claim(
        conn,
        pid,
        kind="boundary",
        category_id=cat_id(conn, pid, "communication/style"),
        text="Never shares the address.",
        audiences_all=True,
        audience_ids=[],
        visibility="shared",
        status="approved",
        confidence=0.9,
        evidence={},
        origin="added",
    )
    fake_llm.on("answer", answer("ok"))
    await predict(conn, respond(user, audiences=("family",)), situation="hi", audience="family")
    assert STYLE_HEADER not in _user(fake_llm)
    await predict(conn, owner(user), situation="hi", audience="family", include_private=True)
    assert STYLE_HEADER in _user(fake_llm)


@pytest.mark.parametrize(
    ("stats", "given", "expected"),
    [
        ({}, "Sure, see you.", "sure, see you"),
        ({"lowercase_share": 0.79}, "Sure, see you.", "Sure, see you"),
        ({"period_share": 0.21}, "Sure, see you.", "sure, see you."),
        ({}, "OK, see you...", "OK, see you..."),
        ({"lowercase_share": 0.79, "period_share": 0.5}, "Sure.", "Sure."),
    ],
)
async def test_post_processing_rules(conn, user, pid, fake_llm, auds, stats, given, expected):
    style_claim(conn, pid, auds["family"], **stats)
    fake_llm.on("answer", answer(given))
    out = await predict(conn, owner(user), situation="dinner?", audience="family")
    assert out["answer"] == expected
    assert not fake_llm.by_purpose("shorten")


async def test_decision_is_untouched(conn, user, pid, fake_llm, auds):
    style_claim(conn, pid, auds["family"])
    long = "Yes. " * 40
    fake_llm.on("answer", answer(long.strip()))
    out = await predict(conn, owner(user), situation="go?", audience="family", kind="decision")
    assert out["answer"] == long.strip()
    assert not fake_llm.by_purpose("shorten")


async def test_shorten_reask_is_counted(conn, user, pid, fake_llm, auds):
    style_claim(conn, pid, auds["family"], chars_p90=15.0)
    fake_llm.on("answer", answer("Of course, I would be happy to come to dinner tonight, thanks!"))
    fake_llm.on("shorten", {"answer": "Sure, coming tonight!"})
    out = await predict(conn, owner(user), situation="dinner?", audience="family")
    assert out["answer"] == "sure, coming tonight!"
    calls = fake_llm.by_purpose("shorten")
    assert len(calls) == 1 and "at most 15 characters" in calls[0]["user"]
    answer_tokens = len(fake_llm.by_purpose("answer")[0]["system"] + _user(fake_llm)) // 4 + 10
    shorten_tokens = len(calls[0]["system"] + calls[0]["user"]) // 4 + 10
    row = conn.execute("SELECT llm_tokens FROM audit ORDER BY rowid DESC LIMIT 1").fetchone()
    assert row["llm_tokens"] == answer_tokens + shorten_tokens


@pytest.mark.parametrize("reply", ["fail", "long", "bad"])
async def test_shorten_failure_keeps_original(conn, user, pid, fake_llm, auds, reply):
    style_claim(conn, pid, auds["family"], chars_p90=15.0)
    original = "Of course, I would be happy to come to dinner tonight, thanks"

    def shorten(call):
        if reply == "fail":
            raise LLMUnavailable("down")
        return {"answer": original + " a lot"} if reply == "long" else {"nope": 1}

    fake_llm.on("answer", answer(original + "."))
    fake_llm.on("shorten", shorten)
    out = await predict(conn, owner(user), situation="dinner?", audience="family")
    assert out["answer"] == "of course, I would be happy to come to dinner tonight, thanks"
    assert len(fake_llm.by_purpose("shorten")) == 1


async def test_words_from_state_behind_approved_claim(conn, user, pid, fake_llm, monkeypatch):
    monkeypatch.setattr(analyzers_mod, "Context", functools.partial(base.Context, conn=conn))
    cat = cat_id(conn, pid, "communication/style")
    fam = add_audience(conn, pid, "family")
    add_audience(conn, pid, "colleagues")
    days = workdays(date(2026, 9, 7), 30)
    eps = said_episodes(cat, "family", FAMILY, days) + said_episodes(
        cat, "colleagues", WORK, days, start_index=100
    )
    run_analyzers(conn, persona_id=pid, episodes=eps, tz=BERLIN, counts={})
    key = f"builtin:stylometry:{fam}"
    fake_llm.on("answer", answer("ok"))
    # pending: nothing visible at all for family
    out = await predict(conn, owner(user), situation="hi", audience="family")
    assert out["type"] == "escalate" and not fake_llm.calls
    conn.execute("UPDATE claims SET status = 'approved' WHERE key = ?", (key,))
    await predict(conn, owner(user), situation="hi", audience="family")
    user_msg = _user(fake_llm)
    assert "favourite words: darling, hugs" in user_msg
    assert "favourite phrases:" in user_msg


async def test_depends_on_is_not_styled(conn, user, pid, fake_llm, auds):
    style_claim(conn, pid, auds["family"])

    def handler(call):
        return {**answer("Let me check.")(call), "a_depends_on": "Agrees if free after 18:00."}

    fake_llm.on("answer", handler)
    out = await predict(conn, owner(user), situation="dinner?", audience="family")
    assert out["answer"] == "let me check"
    assert out["depends_on"] == "Agrees if free after 18:00."
