# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import logging
import re
from typing import Any

import pytest
from conftest import call, make_server
from mcp import Client

from wwyd.errors import LLMUnavailable, RateLimited, SpendCap
from wwyd.limits import get_limiter
from wwyd.mcp.caller import Caller
from wwyd.predict import predict
from wwyd.predict.disclosure import TEMPLATES
from wwyd.predict.prompts import (
    COMMIT_RETRY_NOTE,
    INSTRUCTIONS,
    LANGUAGE_RETRY_NOTE,
    RULED_OUT_RETRY_NOTE,
)
from wwyd.store import audit
from wwyd.store.audiences import upsert_audience
from wwyd.store.claims import add_claims
from wwyd.store.personas import update_settings

pytestmark = pytest.mark.anyio


def seed(conn, pid: str, items: list[dict[str, Any]]) -> list[str]:
    result = add_claims(
        conn, pid, [{"status": "approved", **i} for i in items], absorb=False, allow_approved=True
    )
    assert not result["dropped"], result["dropped"]
    return result["created"]


def answer(text: str = "Sure, Tuesday works.", confidence: float = 0.9, used=None, lang="en"):
    def handler(call: dict[str, Any]) -> dict[str, Any]:
        ids = (
            used
            if used is not None
            else [w[1:-1] for w in call["user"].split() if w[:2] == "[c" and w[-1] == "]"]
        )
        return {
            "type": "answer",
            "answer": text,
            "confidence": confidence,
            "used_claims": ids,
            "language": lang,
            "a_depends_on": "",
            "escalate": "",
            "reason": "",
        }

    return handler


def owner(user, **kw) -> Caller:
    return Caller(user.user_id, user.persona_id, "owner", **kw)


def respond(user, audiences=("work",), **kw) -> Caller:
    return Caller(user.user_id, user.persona_id, "respond", audiences=audiences, **kw)


@pytest.fixture
def audiences(conn, pid):
    for name in ("work", "family"):
        upsert_audience(conn, pid, name, description=f"{name} people")


STYLE = {"kind": "style", "category": "communication/style", "text": "Short, no greeting."}
BOUNDARY = {"kind": "boundary", "category": "communication/boundaries", "text": "No salary."}
MEETING = {
    "kind": "decision_rule",
    "category": "work/meetings",
    "text": "Accepts a meeting only with 20 free minutes and 10 minutes of buffer.",
}


# --- flow --------------------------------------------------------------------------------


async def test_small_set_skips_routing(conn, user, pid, fake_llm):
    ids = seed(conn, pid, [STYLE, MEETING])
    fake_llm.on("answer", answer())
    out = await predict(conn, owner(user), situation="Call on Tuesday?", audience="everyone")
    assert out["type"] == "answer"
    assert out["answer"] == "Sure, Tuesday works."
    assert set(out["used_claims"]) == set(ids)
    assert out["disclosure"] == TEMPLATES["en"].replace("{owner}", "owner")
    assert out["hints"] == []
    assert not fake_llm.by_purpose("route")
    # Claims are shown under short labels, never their IDs.
    prompt = fake_llm.by_purpose("analyze")[0]["user"]
    assert "[c1]" in prompt and "[c2]" in prompt
    assert not any(i in prompt for i in ids)


def big_set(conn, pid) -> dict[str, str]:
    food = [
        {"kind": "preference", "category": "food", "text": f"Food habit number {i}. " + "x" * 200}
        for i in range(40)
    ]
    travel = {"kind": "preference", "category": "travel", "text": "Likes trains."}
    ids = seed(conn, pid, [STYLE, MEETING, *food, travel])
    return {"style": ids[0], "meeting": ids[1], "food": ids[2]}


def shown(conn, prompt: str) -> set[str]:
    """IDs of the claims whose text is in a prompt (claims appear under labels, not IDs)."""
    rows = conn.execute("SELECT id, text FROM claims").fetchall()
    return {r["id"] for r in rows if f") {r['text']}\n" in prompt + "\n"}


async def test_few_categories_skip_routing(conn, user, pid, fake_llm):
    food = [
        {"kind": "preference", "category": "food", "text": f"Food habit number {i}. " + "x" * 200}
        for i in range(40)
    ]
    seed(conn, pid, [STYLE, MEETING, *food])  # large, but only 3 categories
    fake_llm.on("answer", answer())
    await predict(conn, owner(user), situation="Meet Tuesday?", audience="everyone")
    assert not fake_llm.by_purpose("route")


async def test_routing_selects_categories_and_core(conn, user, pid, fake_llm):
    ids = big_set(conn, pid)
    fake_llm.on("route", {"categories": ["work/meetings", "nonexistent/path"]})
    fake_llm.on("answer", answer())
    out = await predict(conn, owner(user), situation="Meet Tuesday?", audience="everyone")
    assert out["type"] == "answer"
    route_call = fake_llm.by_purpose("route")[0]
    assert "work/meetings" in route_call["user"] and "clm_" not in route_call["user"]
    ctx = shown(conn, fake_llm.by_purpose("answer")[0]["user"])
    assert ids["meeting"] in ctx and ids["style"] in ctx and ids["food"] not in ctx


@pytest.mark.parametrize("route_reply", ["fail", "empty"])
async def test_fts_fallback(conn, user, pid, fake_llm, route_reply):
    ids = big_set(conn, pid)

    def failing(call):
        raise LLMUnavailable("down")

    fake_llm.on("route", failing if route_reply == "fail" else {"categories": []})
    fake_llm.on("answer", answer())
    await predict(
        conn, owner(user), situation="Is a meeting with buffer possible?", audience="everyone"
    )
    ctx = shown(conn, fake_llm.by_purpose("answer")[0]["user"])
    assert ids["meeting"] in ctx and ids["style"] in ctx and ids["food"] not in ctx


def _reply(text: str = "let me check", **kw: Any) -> dict[str, Any]:
    """An answer-call reply (call 2)."""
    return {
        "answer": text,
        "confidence": 0.9,
        "language": "en",
        "used_claims": [],
        "z_commits": False,
        **kw,
    }


def _analysis(verdict: str = "settled", applicable=("c2",), label="", depends="", **kw: Any):
    """An analyze-call reply (call 1)."""
    return {
        "a_check": "scratch note",
        "b_applicable": list(applicable),
        "c_excluded_by": "",
        "d_verdict": verdict,
        "e_label": label,
        "f_depends_on": depends,
        **kw,
    }


def _scripted(*replies: dict[str, Any]):
    queue = list(replies)

    def handler(call: dict[str, Any]) -> dict[str, Any]:
        return queue.pop(0) if len(queue) > 1 else queue[0]

    return handler


RULE = "Agrees if 14:55-15:25 is free. Otherwise offers the nearest slot."


async def test_depends_on_comes_from_the_analysis(conn, user, pid, fake_llm):
    seed(conn, pid, [STYLE, MEETING])
    fake_llm.on("analyze", _analysis("open", depends=RULE))
    fake_llm.on("answer", _reply())
    out = await predict(conn, owner(user), situation="Call at 15?", audience="default")
    assert out["type"] == "answer" and out["depends_on"] == RULE
    assert out["answer"] == "let me check"
    for internal in ("a_check", "b_applicable", "d_verdict", "e_label", "f_depends_on"):
        assert internal not in out
    assert "scratch note" not in str(out)
    second = fake_llm.by_purpose("answer")[0]["user"]
    assert f"Depends on: {RULE}\n" in second and INSTRUCTIONS["open"] in second


@pytest.mark.parametrize("verdict", ["settled", "ruled_out"])
@pytest.mark.parametrize("raw", ["", "   ", None, 5, RULE])
async def test_depends_on_null_unless_open(conn, user, pid, fake_llm, raw, verdict):
    seed(conn, pid, [STYLE, MEETING])
    fake_llm.on("analyze", _analysis(verdict, depends=raw))
    fake_llm.on("answer", _reply("sure"))
    out = await predict(conn, owner(user), situation="Hi", audience="default")
    assert out["type"] == "answer" and out["depends_on"] is None


@pytest.mark.parametrize("raw", ["", "   ", None])
async def test_open_without_condition_is_settled(conn, user, pid, fake_llm, raw):
    seed(conn, pid, [STYLE, MEETING])
    fake_llm.on("analyze", _analysis("open", depends=raw))
    fake_llm.on("answer", _reply("sure", z_commits=True))
    out = await predict(conn, owner(user), situation="Hi", audience="default")
    assert out["depends_on"] is None and len(fake_llm.by_purpose("answer")) == 1
    assert INSTRUCTIONS["settled"] in fake_llm.by_purpose("answer")[0]["user"]


def test_depends_on_cut_to_400():
    from wwyd.predict.answer import depends_on

    sentences = "Talks to friends after 18:00. " * 20
    cut = depends_on(sentences)
    assert len(cut) <= 400 and cut.endswith("18:00.")
    words = "word " * 200
    cut = depends_on(words)
    assert len(cut) <= 401 and cut.endswith("word…")
    assert depends_on("  short  ") == "short"


def test_schema_keys_sort_into_generation_order():
    from wwyd.predict.prompts import ANALYZE_SCHEMA, ANSWER_SCHEMA, COMMITS_KEY

    keys = list(ANALYZE_SCHEMA["properties"])
    assert keys == sorted(keys) and keys[0] == "a_check" and keys[-1] == "g_language"
    assert "c_excluded_by" in keys
    assert set(ANALYZE_SCHEMA["required"]) == set(keys)
    assert ANALYZE_SCHEMA["properties"]["d_verdict"]["enum"] == [
        "none",
        "boundary",
        "ruled_out",
        "open",
        "settled",
    ]
    keys = list(ANSWER_SCHEMA["properties"])
    assert keys == sorted(keys) and keys[0] == "answer" and keys[-1] == COMMITS_KEY
    assert set(ANSWER_SCHEMA["required"]) == set(keys)
    assert ANSWER_SCHEMA["properties"][COMMITS_KEY] == {"type": "boolean"}


KINDS = {"c1": "style", "c2": "decision_rule", "c3": "boundary"}


@pytest.mark.parametrize(
    ("data", "verdict", "applicable", "label"),
    [
        (_analysis("settled", ["c1", "c2", "zz", "c2"]), "settled", ["c2"], None),
        (_analysis("settled", ["c1"]), "none", [], None),  # style only
        (_analysis("open", [], depends="x"), "none", [], None),
        (_analysis("none", ["c2"]), "settled", ["c2"], None),  # claims win
        (_analysis("boundary", ["c3"], "c3"), "boundary", ["c3"], "c3"),
        (_analysis("boundary", ["c2", "c3"], "c2"), "boundary", ["c2", "c3"], "c3"),
        (_analysis("boundary", ["c2"], "c2"), "settled", ["c2"], None),  # no boundary claim
        (_analysis("ruled_out", [], "[c2]"), "none", [], None),  # e_label alone is ignored
        (_analysis("ruled_out", ["c2"], "q7"), "ruled_out", ["c2"], "c2"),
        (_analysis("banana", ["c2"]), "settled", ["c2"], None),
        ({}, "none", [], None),
        (_analysis("open", ["c2"], c_excluded_by="c2", depends="x"), "ruled_out", ["c2"], "c2"),
        (_analysis("settled", [], c_excluded_by="c2"), "ruled_out", ["c2"], "c2"),
        (_analysis("settled", ["c2"], c_excluded_by="c1"), "settled", ["c2"], None),  # style
        # A refusal rule beats a boundary verdict; a boundary claim excluding does not.
        (_analysis("boundary", ["c3"], "c3", c_excluded_by="c2"), "ruled_out", ["c3", "c2"], "c2"),
        (_analysis("boundary", ["c3"], "c3", c_excluded_by="c3"), "boundary", ["c3"], "c3"),
        (_analysis("boundary", ["c3"], "c3", c_excluded_by="c1"), "boundary", ["c3"], "c3"),
    ],
)
def test_read_analysis(data, verdict, applicable, label):
    from wwyd.predict.answer import read_analysis

    found = read_analysis(data, KINDS)
    assert (found.verdict, found.applicable, found.label) == (verdict, applicable, label)


def test_refusal_rule_in_the_boundary_category_beats_boundary():
    from wwyd.predict.answer import read_analysis

    data = _analysis("boundary", ["c3", "c2"], "c3", c_excluded_by="c3")
    same = read_analysis(data, KINDS, {"c2": "work/salary", "c3": "work/salary"})
    assert (same.verdict, same.label) == ("ruled_out", "c2")
    other = read_analysis(data, KINDS, {"c2": "work/meetings", "c3": "work/salary"})
    assert (other.verdict, other.label) == ("boundary", "c3")
    assert read_analysis(data, KINDS).verdict == "boundary"


async def test_none_verdict_escalates_without_answer_call(conn, user, pid, fake_llm):
    seed(conn, pid, [STYLE, MEETING])
    fake_llm.on("analyze", _analysis("none", []))
    out = await predict(conn, owner(user), situation="Help me move?", audience="default")
    assert (out["type"], out["escalate"]) == ("escalate", "out_of_persona")
    assert not fake_llm.by_purpose("answer")
    assert "used_claims" not in out


async def test_style_only_persona_is_out_of_persona(conn, user, pid, fake_llm):
    seed(conn, pid, [STYLE])
    out = await predict(conn, owner(user), situation="Help me move?", audience="default")
    assert out["escalate"] == "out_of_persona"
    assert [c["purpose"] for c in fake_llm.calls] == ["when", "analyze"]


async def test_boundary_escalates_without_answer_call(conn, user, pid, fake_llm):
    seed(conn, pid, [STYLE, MEETING, BOUNDARY])

    def by_kind(kind: str):
        def handler(call):
            (label,) = re.findall(rf"^\[(c\d)\] \({kind},", call["user"], re.M)
            return _analysis("boundary", [label], label)

        return handler

    fake_llm.on("analyze", by_kind("boundary"))
    out = await predict(conn, owner(user), situation="Salary?", audience="everyone")
    assert (out["escalate"], out["type"]) == ("boundary", "escalate")
    assert not fake_llm.by_purpose("answer")
    # Without a boundary claim on the topic the answer call runs as "settled".
    fake_llm.on("analyze", by_kind("decision_rule"))
    fake_llm.on("answer", _reply("no"))
    out = await predict(conn, owner(user), situation="Salary?", audience="everyone")
    assert out["type"] == "answer"


async def test_ruled_out_instruction_names_the_claim(conn, user, pid, fake_llm):
    seed(conn, pid, [STYLE, MEETING])
    fake_llm.on("analyze", _analysis("ruled_out", ["c2"], "c2"))
    fake_llm.on("answer", _reply("can't, sorry", z_commits=True))
    out = await predict(conn, owner(user), situation="Call at 7?", audience="default")
    assert out["answer"] == "can't, sorry" and out["depends_on"] is None
    second = fake_llm.by_purpose("answer")[0]["user"]
    assert INSTRUCTIONS["ruled_out"].replace("{label}", "c2") in second
    assert "Depends on:" not in second
    assert len(fake_llm.by_purpose("answer")) == 1  # committing is fine when ruled out


async def test_answer_call_sees_style_and_applicable_claims_only(conn, user, pid, fake_llm):
    seed(conn, pid, [STYLE, MEETING, {**MEETING, "category": "food", "text": "Vegetarian."}])
    fake_llm.on("analyze", _analysis("settled", ["c2"]))
    fake_llm.on("answer", _reply("sure"))
    await predict(conn, owner(user), situation="Call?", audience="default")
    first = fake_llm.by_purpose("analyze")[0]["user"]
    second = fake_llm.by_purpose("answer")[0]["user"]
    assert "Vegetarian." in first and "Vegetarian." not in second
    assert "[c1] (style" in second and "[c2] (decision_rule" in second


async def test_used_claims_decided_in_code(conn, user, pid, fake_llm):
    """Applicable claims first, then the style claims shown; whatever the model says."""
    ids = seed(conn, pid, [STYLE, MEETING])
    fake_llm.on("answer", _reply("sure", used_claims=["c9"]))
    out = await predict(conn, owner(user), situation="Call?", audience="default")
    assert out["used_claims"] == [ids[1], ids[0]]


async def test_empty_answer_is_low_confidence(conn, user, pid, fake_llm):
    seed(conn, pid, [STYLE, MEETING])
    fake_llm.on("answer", _reply(""))
    out = await predict(conn, owner(user), situation="Call?", audience="default")
    assert out["escalate"] == "low_confidence"


async def test_commit_guard_retries_once(conn, user, pid, fake_llm):
    seed(conn, pid, [STYLE, MEETING])
    fake_llm.on("analyze", _analysis("open", depends="free at 15?"))
    committed = _reply("sure, 15 works", z_commits=True)
    fixed = _reply("let me check")
    fake_llm.on("answer", _scripted(committed, fixed))
    out = await predict(conn, owner(user), situation="Call at 15?", audience="default")
    calls = fake_llm.by_purpose("answer")
    assert len(calls) == 2
    assert calls[0]["user"] in calls[1]["user"] and COMMIT_RETRY_NOTE in calls[1]["user"]
    assert out["type"] == "answer" and out["answer"] == "let me check"
    assert "z_commits" not in out
    row = conn.execute("SELECT llm_tokens FROM audit ORDER BY rowid DESC LIMIT 1").fetchone()
    expected = sum(len(c["system"] + c["user"]) // 4 + 10 for c in fake_llm.calls)
    assert row[0] == expected  # analyze and both answer calls counted


async def test_commit_guard_escalates_after_second_commit(conn, user, pid, fake_llm):
    seed(conn, pid, [STYLE, MEETING])
    fake_llm.on("analyze", _analysis("open", depends="free at 15?"))
    fake_llm.on("answer", _reply("sure, 15 works", z_commits=True))
    out = await predict(conn, owner(user), situation="Call at 15?", audience="default")
    assert len(fake_llm.by_purpose("answer")) == 2
    assert (out["type"], out["escalate"]) == ("escalate", "low_confidence")
    assert "committed" in out["reason"]


@pytest.mark.parametrize(
    ("verdict", "commits"),
    [("settled", True), ("ruled_out", True), ("open", False), ("open", None)],
)
async def test_commit_guard_no_retry(conn, user, pid, fake_llm, verdict, commits):
    seed(conn, pid, [STYLE, MEETING])
    fake_llm.on("analyze", _analysis(verdict, label="c2", depends="free at 15?"))
    fake_llm.on("answer", _reply("ok", z_commits=commits))
    out = await predict(conn, owner(user), situation="Call at 15?", audience="default")
    assert len(fake_llm.by_purpose("answer")) == 1
    assert out["type"] == "answer" and "z_commits" not in out


RU_SITUATION = "Одолжишь мне пятьдесят евро до пятницы?"


async def test_language_line_from_the_analysis(conn, user, pid, fake_llm):
    seed(conn, pid, [STYLE, MEETING])
    fake_llm.on("analyze", _scripted(_analysis(g_language="pl"), _analysis(g_language="??")))
    fake_llm.on("answer", _reply("jasne, pasuje", language="pl"))
    await predict(conn, owner(user), situation="Pożyczysz mi 50 euro?", audience="default")
    await predict(conn, owner(user), situation="Ok?", audience="default")
    pl, unsure = fake_llm.by_purpose("answer")
    assert "\nAnswer language: pl\nSituation: " in pl["user"]
    assert "Answer language" not in unsure["user"]
    assert pl["system"] == unsure["system"]
    assert all("Answer language" not in c["user"] for c in fake_llm.by_purpose("analyze"))


async def test_language_guard_reasks_once_on_script(conn, user, pid, fake_llm):
    """Cyrillic situation, Latin answer: caught by the script alone."""
    seed(conn, pid, [STYLE, MEETING])
    wrong = _reply("sure, no problem at all", language="ru")
    right = _reply("конечно, без проблем", language="ru")
    fake_llm.on("answer", _scripted(wrong, right))
    out = await predict(conn, owner(user), situation=RU_SITUATION, audience="default")
    calls = fake_llm.by_purpose("answer")
    assert len(calls) == 2 and LANGUAGE_RETRY_NOTE in calls[1]["user"]
    assert COMMIT_RETRY_NOTE not in calls[1]["user"]
    assert len(fake_llm.by_purpose("analyze")) == 1  # the analysis is never re-asked
    assert out["answer"] == "конечно, без проблем"


async def test_language_guard_same_script(conn, user, pid, fake_llm):
    """English situation, German answer: the script is the same, the language fields differ."""
    seed(conn, pid, [STYLE, MEETING])
    fake_llm.on("analyze", _analysis(g_language="en"))
    wrong = _reply("ja, gerne", language="de")
    right = _reply("sure, happy to", language="en")
    fake_llm.on("answer", _scripted(wrong, right))
    out = await predict(
        conn, owner(user), situation="Can you lend me 30 euros until Friday?", audience="default"
    )
    assert len(fake_llm.by_purpose("answer")) == 2
    assert out["answer"] == "sure, happy to"


async def test_language_guard_any_script(conn, user, pid, fake_llm):
    """No per-language code: a Japanese situation answered in English is re-asked."""
    seed(conn, pid, [STYLE, MEETING])
    wrong = _reply("sure, see you tomorrow", language="ja")
    right = _reply("はい、明日大丈夫です", language="ja")
    fake_llm.on("answer", _scripted(wrong, right))
    out = await predict(
        conn, owner(user), situation="明日の17時に電話できますか？", audience="default"
    )
    assert len(fake_llm.by_purpose("answer")) == 2
    assert out["answer"] == "はい、明日大丈夫です"


async def test_language_mismatch_accepted_on_second_reply(conn, user, pid, fake_llm):
    seed(conn, pid, [STYLE, MEETING])
    fake_llm.on("answer", _reply("sure, no problem at all"))
    out = await predict(conn, owner(user), situation=RU_SITUATION, audience="default")
    assert len(fake_llm.by_purpose("answer")) == 2
    assert out["type"] == "answer" and out["answer"] == "sure, no problem at all"


async def test_combined_notes_single_reask(conn, user, pid, fake_llm):
    seed(conn, pid, [STYLE, MEETING])
    fake_llm.on("analyze", _analysis("open", depends="если деньги есть"))
    fake_llm.on("answer", _reply("sure, here you go", z_commits=True))
    out = await predict(conn, owner(user), situation=RU_SITUATION, audience="default")
    calls = fake_llm.by_purpose("answer")
    assert len(calls) == 2  # at most one re-ask
    assert COMMIT_RETRY_NOTE in calls[1]["user"] and LANGUAGE_RETRY_NOTE in calls[1]["user"]
    assert (out["type"], out["escalate"]) == ("escalate", "low_confidence")


@pytest.mark.parametrize(
    ("situation", "text", "lang"),
    [
        ("Ок?", "sure, no problem at all", "en"),  # situation too short to judge
        ("Встреча с team lead and the whole crew?", "sure, no problem", "en"),  # mixed
        ("Lunch on Thursday at one?", "sure, sounds good to me", "en"),  # same language
        (RU_SITUATION, "ok", "ru"),  # answer too short, language field agrees
        (RU_SITUATION, "ok", ""),  # nothing to compare with
    ],
)
async def test_language_guard_ignores_unclear(conn, user, pid, fake_llm, situation, text, lang):
    seed(conn, pid, [STYLE, MEETING])
    fake_llm.on("analyze", _analysis(g_language="ru" if situation == RU_SITUATION else ""))
    fake_llm.on("answer", _reply(text, language=lang))
    await predict(conn, owner(user), situation=situation, audience="default")
    assert len(fake_llm.by_purpose("answer")) == 1


@pytest.mark.parametrize(
    ("text", "name"),
    [
        ("Can you lend me 30 euros until Friday?", "LATIN"),
        ("Pożyczysz mi 50 euro do piątku?", "LATIN"),
        ("Одолжишь 150 евро до зарплаты?", "CYRILLIC"),
        ("明日の17時に電話できますか？", "CJK"),
        ("明天下午五点打电话可以吗", "CJK"),
        ("내일 다섯 시에 통화할 수 있어요?", "HANGUL"),
        ("Καλημέρα, τι κάνεις;", "GREEK"),
        ("ok", None),
        ("Привет hello мир world", None),
    ],
)
def test_script(text, name):
    from wwyd.predict.language import script

    assert script(text) == name


def test_language_normalise():
    from wwyd.predict.language import normalise

    assert normalise("de-DE") == "de" and normalise(" RU ") == "ru"
    assert normalise("pl") == "pl" and normalise("fil") == "fil"
    assert normalise("Polish") is None and normalise("") is None and normalise(None) is None


def test_prompts_static_and_no_favour_fallback():
    from wwyd.predict.prompts import ANALYZE_SYSTEM, ANSWER_SYSTEM

    assert "Never guess" in ANALYZE_SYSTEM
    assert "style and claims suggest" not in ANALYZE_SYSTEM + ANSWER_SYSTEM
    for common in ("salary", "lunch", "meeting", "Vienna", "vegetar"):
        assert common not in ANALYZE_SYSTEM + ANSWER_SYSTEM


async def test_now_line_in_persona_timezone(conn, user, pid, fake_llm, monkeypatch):
    from datetime import UTC, datetime

    import wwyd.predict as predict_pkg
    from wwyd.store.personas import update_settings as _update

    monkeypatch.setattr(predict_pkg, "_now", lambda: datetime(2026, 10, 19, 16, 40, tzinfo=UTC))
    seed(conn, pid, [STYLE, MEETING])
    fake_llm.on("answer", lambda call: _reply())
    await predict(conn, owner(user), situation="Call?", audience="default")
    _update(conn, pid, timezone="Europe/Berlin")
    await predict(conn, owner(user), situation="Call?", audience="default")
    conn.execute("UPDATE personas SET timezone = 'Not/AZone' WHERE id = ?", (pid,))
    await predict(conn, owner(user), situation="Call?", audience="default")
    utc, berlin, bad = fake_llm.by_purpose("answer")[-3:]
    assert "Now: Monday 2026-10-19 16:40 (UTC)\n" in utc["user"]
    assert all("Now: " in c["user"] for c in fake_llm.by_purpose("analyze"))
    assert "Now: Monday 2026-10-19 18:40 (Europe/Berlin)\n" in berlin["user"]
    assert "Now: Monday 2026-10-19 16:40 (UTC)\n" in bad["user"]
    assert utc["system"] == berlin["system"] and "2026-10-19" not in utc["system"]


async def test_context_in_user_message_not_system(conn, user, pid, fake_llm):
    seed(conn, pid, [STYLE, MEETING])
    fake_llm.on("answer", lambda call: _reply())
    await predict(conn, owner(user), situation="Call?", audience="default")
    await predict(
        conn, owner(user), situation="Call?", audience="default", context="free from 19:30"
    )
    first, second = fake_llm.by_purpose("answer")[-2:]
    assert "Context: none" in first["user"]
    a1, a2 = fake_llm.by_purpose("analyze")[-2:]
    assert "Context: none" in a1["user"] and "Context: free from 19:30" in a2["user"]
    assert a1["system"] == a2["system"] and "19:30" not in a2["system"]
    assert "Context: free from 19:30" in second["user"]
    assert "19:30" not in second["system"]
    assert first["system"] == second["system"]


async def test_predict_tool_schema_has_context_not_facts(client, conn, pid, fake_llm):
    seed(conn, pid, [STYLE, MEETING])
    fake_llm.on("answer", lambda call: _reply())
    (tool,) = [t for t in (await client.list_tools()).tools if t.name == "predict"]
    props = tool.input_schema["properties"]
    assert "facts" not in props and "context" in props
    assert props["context"]["anyOf"][0]["maxLength"] == 2000
    out = await call(
        client, "predict", {"situation": "Hi", "audience": "default", "context": "busy today"}
    )
    assert out["type"] == "answer" and "Context: busy today" in fake_llm.calls[-1]["user"]


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (SpendCap("cap"), "spend_cap"),
        (RateLimited("tokens", retry_after=5), "rate_limited"),
        (LLMUnavailable("down"), "llm_unavailable"),
    ],
)
async def test_llm_errors_escalate(conn, user, pid, fake_llm, exc, code):
    seed(conn, pid, [STYLE, MEETING])

    def handler(call):
        raise exc

    fake_llm.on("answer", handler)
    out = await predict(conn, owner(user), situation="Hi", audience="everyone")
    assert out["type"] == "escalate" and out["escalate"] == code
    assert audit.list_audit(conn, pid)["entries"][0]["outcome"] == f"escalate:{code}"


async def test_model_junk(conn, user, pid, fake_llm):
    seed(conn, pid, [STYLE, MEETING])
    fake_llm.on("analyze", {"d_verdict": "banana"})  # no applicable claim
    out = await predict(conn, owner(user), situation="Salary?", audience="everyone")
    assert out["escalate"] == "out_of_persona"
    fake_llm.on("analyze", ["not", "an", "object"])
    out = await predict(conn, owner(user), situation="Salary?", audience="everyone")
    assert out["escalate"] == "llm_unavailable"
    fake_llm.on("analyze", _analysis())
    fake_llm.on("answer", ["not", "an", "object"])
    out = await predict(conn, owner(user), situation="Salary?", audience="everyone")
    assert out["escalate"] == "llm_unavailable"
    fake_llm.on("answer", {"type": "banana"})
    out = await predict(conn, owner(user), situation="Salary?", audience="everyone")
    assert out["escalate"] == "low_confidence"


async def test_no_visible_claims_escalates_without_llm(conn, user, pid, fake_llm):
    out = await predict(conn, owner(user), situation="Hi", audience="everyone")
    assert out["escalate"] == "out_of_persona"
    assert fake_llm.calls == []


async def test_low_confidence(conn, user, pid, fake_llm):
    """Confidence comes from the claims' support, not from the model."""
    seed(conn, pid, [STYLE, {**MEETING, "confidence": 0.1}])
    fake_llm.on("answer", answer(confidence=0.99))
    out = await predict(conn, owner(user), situation="Hi", audience="everyone")
    assert out["type"] == "escalate" and out["escalate"] == "low_confidence"
    assert "answer" not in out
    assert audit.list_audit(conn, pid)["entries"][0]["confidence"] == 0.55
    update_settings(conn, pid, escalate_below=0.3)
    out = await predict(conn, owner(user), situation="Hi", audience="everyone")
    assert out["type"] == "answer"


@pytest.mark.parametrize("lang", ["en", "ru", "de", "uk", "es", "fr", "pt-BR", ""])
async def test_disclosure_languages(conn, user, pid, fake_llm, lang):
    seed(conn, pid, [STYLE, MEETING])
    update_settings(conn, pid, display_name="Eugene")
    fake_llm.on("answer", answer(lang=lang))
    out = await predict(conn, owner(user), situation="Hi", audience="everyone")
    expected = TEMPLATES.get(lang, TEMPLATES["en"]).replace("{owner}", "Eugene")
    assert out["disclosure"] == expected
    assert "Eugene" in out["disclosure"] and "{owner}" not in out["disclosure"]


async def test_disclosure_override(conn, user, pid, fake_llm):
    seed(conn, pid, [STYLE, MEETING])
    update_settings(conn, pid, display_name="Eugene", disclosure_template="Bot of {owner}:")
    fake_llm.on("answer", answer(lang="ru"))
    out = await predict(conn, owner(user), situation="Hi", audience="everyone")
    assert out["disclosure"] == "Bot of Eugene:"


async def test_used_claims_not_in_context_dropped(conn, user, pid, fake_llm, audiences):
    style, shown = seed(conn, pid, [STYLE, MEETING])
    (hidden,) = seed(conn, pid, [{**MEETING, "text": "Hidden.", "visibility": "owner_only"}])
    fake_llm.on("answer", answer(used=[shown, hidden, "clm_a1", shown]))
    out = await predict(conn, owner(user), situation="Hi", audience="work")
    assert out["used_claims"] == [shown, style]


# --- scopes and visibility (asserted on what the model saw) -------------------------------


def visibility_set(conn, pid) -> dict[str, str]:
    items = {
        "shared_all": {**STYLE, "text": "Shared with everyone."},
        "private_all": {**STYLE, "text": "Private note.", "visibility": "owner_only"},
        "shared_work": {**MEETING, "audiences": ["work"]},
        "shared_family": {**MEETING, "text": "Family rule.", "audiences": ["family"]},
        "private_work": {
            **MEETING,
            "text": "Private work.",
            "audiences": ["work"],
            "visibility": "owner_only",
        },
    }
    ids = seed(conn, pid, list(items.values()))
    add_claims(conn, pid, [{**STYLE, "text": "Pending one."}])  # pending: never visible
    return dict(zip(items, ids, strict=True))


@pytest.mark.parametrize("include_private", [False, True])
async def test_respond_scope_never_sees_private_or_other_audiences(
    conn, user, pid, fake_llm, audiences, include_private
):
    ids = visibility_set(conn, pid)
    fake_llm.on("answer", answer())
    out = await predict(
        conn,
        respond(user),
        situation="Hi",
        audience="work",
        include_private=include_private,
    )
    prompt = fake_llm.calls[-1]["user"]
    ctx = shown(conn, prompt)
    assert ids["shared_all"] in ctx and ids["shared_work"] in ctx
    for key in ("private_all", "private_work", "shared_family"):
        assert ids[key] not in ctx
    assert "Private" not in prompt and "Family rule" not in prompt and "Pending one" not in prompt
    if include_private:
        assert any("include_private" in h for h in out["hints"])


async def test_owner_audience_view_and_include_private(conn, user, pid, fake_llm, audiences):
    ids = visibility_set(conn, pid)
    fake_llm.on("answer", answer())
    await predict(conn, owner(user), situation="Hi", audience="work")
    ctx = shown(conn, fake_llm.calls[-1]["user"])
    assert ids["private_all"] not in ctx and ids["private_work"] not in ctx
    await predict(conn, owner(user), situation="Hi", audience="work", include_private=True)
    ctx = shown(conn, fake_llm.calls[-1]["user"])
    assert ids["private_all"] in ctx and ids["private_work"] in ctx
    assert ids["shared_family"] not in ctx


async def test_respond_audience_not_permitted(conn, user, pid, fake_llm, audiences):
    visibility_set(conn, pid)
    out = await predict(conn, respond(user), situation="Hi", audience="family")
    assert out["escalate"] == "audience_not_permitted"
    # Unknown audience -> default "everyone", which this grant does not cover either.
    out = await predict(conn, respond(user), situation="Hi", audience="nope")
    assert out["escalate"] == "audience_not_permitted"
    assert fake_llm.calls == []
    entries = audit.list_audit(conn, pid)["entries"]
    assert [e["outcome"] for e in entries] == ["escalate:audience_not_permitted"] * 2
    assert entries[0]["audience_fallback"] is True


async def test_unknown_audience_falls_back_with_hint(conn, user, pid, fake_llm):
    seed(conn, pid, [STYLE, MEETING])
    fake_llm.on("answer", answer())
    out = await predict(conn, owner(user), situation="Hi", audience="strangers")
    assert out["type"] == "answer"
    assert any("default audience" in h for h in out["hints"])
    assert "Talking to: everyone" in fake_llm.calls[-1]["user"]


async def test_default_alias_resolves_without_fallback(conn, user, pid, fake_llm):
    seed(conn, pid, [STYLE, MEETING])
    upsert_audience(conn, pid, "everyone", rename_to="anyone")
    fake_llm.on("answer", answer())
    out = await predict(conn, owner(user), situation="Hi", audience="default")
    assert out["type"] == "answer" and out["hints"] == []
    assert "Talking to: anyone" in fake_llm.calls[-1]["user"]
    assert audit.list_audit(conn, pid)["entries"][0]["audience_fallback"] is False


async def test_default_alias_respond_checks_real_name(conn, user, pid, fake_llm, audiences):
    seed(conn, pid, [STYLE, MEETING])
    fake_llm.on("answer", answer())
    # A scope naming "default" does not cover the default audience: scopes name real ones.
    out = await predict(conn, respond(user, ("default",)), situation="Hi", audience="default")
    assert out["escalate"] == "audience_not_permitted"
    out = await predict(conn, respond(user, ("everyone",)), situation="Hi", audience="default")
    assert out["type"] == "answer"


# --- limits and audit ----------------------------------------------------------------------


async def test_predict_limits(conn, user, pid, fake_llm, audiences):
    seed(conn, pid, [STYLE, MEETING])
    fake_llm.on("answer", answer())
    limiter = get_limiter(conn)
    limiter.set_own_limits(user.user_id, {"predict_per_audience_per_day": 1})
    assert (await predict(conn, owner(user), situation="Hi", audience="work"))["type"] == "answer"
    out = await predict(conn, owner(user), situation="Hi", audience="work")
    assert out["escalate"] == "rate_limited"
    assert len(fake_llm.by_purpose("answer")) == 1
    # Another audience has its own counter.
    assert (await predict(conn, owner(user), situation="Hi", audience="family"))["type"] == "answer"
    limiter.set_own_limits(user.user_id, {"predict_per_day": 2})
    out = await predict(conn, owner(user), situation="Hi", audience="everyone")
    assert out["escalate"] == "rate_limited"
    assert len(fake_llm.by_purpose("answer")) == 2
    assert limiter.usage(user.user_id)["predict"] == 2


async def test_audit_rows(conn, user, pid, fake_llm, audiences):
    seed(conn, pid, [STYLE, MEETING])
    fake_llm.on("answer", answer(confidence=0.11))
    caller = respond(user, client_name="Responder", subject_id="grt_abc")
    await predict(conn, caller, situation="Hi", audience="work")
    (entry,) = audit.list_audit(conn, pid)["entries"]
    assert entry["outcome"] == "answer" and entry["confidence"] == 0.75
    assert (entry["scope"], entry["client_name"], entry["grant_id"]) == (
        "respond",
        "Responder",
        "grt_abc",
    )
    assert entry["audience"] == "work" and entry["llm_tokens"] > 0
    await predict(
        conn, owner(user, subject_id="key_x"), situation="Hi", audience="work", audit=False
    )
    assert len(audit.list_audit(conn, pid)["entries"]) == 1
    cols = {r["name"]: r["type"] for r in conn.execute("PRAGMA table_info(audit)")}
    assert set(cols) == {
        "seq", "id", "persona_id", "ts", "client_name", "grant_id", "api_key_id", "scope",
        "audience_id", "audience_fallback", "outcome", "confidence", "used_claims", "llm_tokens",
        "latency_ms",
    }  # fmt: skip


async def test_list_audit_filters_and_purge(conn, user, pid, fake_llm):
    for outcome in ("answer", "needs", "escalate:boundary"):
        audit.record(conn, pid, scope="owner", outcome=outcome)
    assert len(audit.list_audit(conn, pid, outcome="escalate")["entries"]) == 1
    page = audit.list_audit(conn, pid, limit=2)
    assert [e["outcome"] for e in page["entries"]] == ["escalate:boundary", "needs"]
    rest = audit.list_audit(conn, pid, limit=2, cursor=page["next_cursor"])
    assert [e["outcome"] for e in rest["entries"]] == ["answer"] and rest["next_cursor"] is None
    assert audit.list_audit(conn, pid, since="2999-01-01T00:00:00Z")["entries"] == []
    conn.execute("UPDATE audit SET ts = '2020-01-01T00:00:00Z' WHERE outcome = 'answer'")
    assert audit.purge_old_audit(conn) == 1
    assert len(audit.list_audit(conn, pid)["entries"]) == 2


# --- MCP surface ------------------------------------------------------------------------


async def test_predict_tool_as_respond(conn, user, pid, fake_llm, audiences):
    seed(conn, pid, [STYLE, MEETING])
    fake_llm.on("answer", answer())
    server = make_server(conn, user, "respond", audiences=("work",))
    async with Client(server) as c:
        names = {t.name for t in (await c.list_tools()).tools}
        assert names == {"predict"}
        out = await call(c, "predict", {"situation": "Hi", "audience": "work"})
        assert out["type"] == "answer" and out["disclosure"]
        res = await c.call_tool("predict", {"situation": "x" * 8001, "audience": "work"})
        assert res.is_error and res.structured_content["error"]["code"] == "invalid"


async def test_owner_tools_and_list_audit(client, conn, pid, fake_llm):
    seed(conn, pid, [STYLE, MEETING])
    fake_llm.on("answer", answer())
    await call(client, "predict", {"situation": "Hi", "audience": "everyone"})
    out = await call(client, "list_audit", {})
    assert out["entries"][0]["outcome"] == "answer"
    persona = await call(client, "get_persona", {})
    assert persona["claims_used"] == 2


# --- privacy canary ------------------------------------------------------------------------

CANARIES = ("zq7x-canary-41b9", "Velmorine")


async def test_predict_privacy_canary(conn, user, pid, fake_llm, audiences, caplog, tmp_path):
    caplog.set_level(logging.DEBUG)
    seed(conn, pid, [STYLE, MEETING])
    situation = "Hi, this is Velmorine, my code is zq7x-canary-41b9. Meet Tuesday?"
    context = "Velmorine calendar: zq7x-canary-41b9"

    def echo(call):  # a model that repeats its input everywhere it can
        return {
            "type": "answer",
            "answer": call["user"],
            "confidence": 0.9,
            "used_claims": [call["user"]],
            "language": call["user"][:20],
            "a_depends_on": call["user"],
            "escalate": "",
            "reason": call["user"],
        }

    def boom(call):
        raise LLMUnavailable("down")

    server = make_server(conn, user, "owner", client_name="canary-client")
    async with Client(server) as c:
        for handler in (echo, boom, answer(confidence=0.1)):
            fake_llm.on("answer", handler)
            await call(
                c, "predict", {"situation": situation, "audience": "work", "context": context}
            )
        await call(c, "list_audit", {})

    conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
    for path in tmp_path.glob("test.db*"):
        data = path.read_bytes()
        for canary in CANARIES:
            assert canary.encode() not in data, (path.name, canary)
    logged = caplog.text + "".join(r.getMessage() for r in caplog.records)
    for canary in CANARIES:
        assert canary not in logged
    for row in conn.execute("SELECT * FROM audit"):
        assert not any(canary in str(v) for v in tuple(row) for canary in CANARIES)


async def test_prediction_id_and_used_claims_in_audit(conn, user, pid, fake_llm, audiences):
    (style, meeting) = seed(conn, pid, [STYLE, MEETING])
    fake_llm.on("answer", answer("Sure.", used=[meeting]))
    out = await predict(conn, owner(user), situation="Sync Tuesday?", audience="everyone")
    assert out["type"] == "answer" and out["prediction_id"].startswith("adt_")
    entries = {e["id"]: e for e in audit.list_audit(conn, pid)["entries"]}
    assert entries[out["prediction_id"]]["used_claims"] == [meeting, style]
    stored = conn.execute(
        "SELECT used_claims FROM audit WHERE id = ?", (out["prediction_id"],)
    ).fetchone()[0]
    assert "Sure" not in stored and "Sync" not in stored

    esc = await predict(conn, respond(user, audiences=()), situation="Hi", audience="work")
    assert esc["type"] == "escalate" and esc["prediction_id"].startswith("adt_")
    assert esc["prediction_id"] != out["prediction_id"]

    quiet = await predict(conn, owner(user), situation="Hi", audience="everyone", audit=False)
    assert "prediction_id" not in quiet


@pytest.mark.parametrize(
    ("verdict", "seen"), [("settled", True), ("open", False), ("ruled_out", False)]
)
async def test_analysis_line_only_for_settled(conn, user, pid, fake_llm, verdict, seen):
    """The analyze call's conclusion (a_check) reaches the settled answer call only; it is
    never part of the result."""
    seed(conn, pid, [STYLE, MEETING])
    fake_llm.on("analyze", _analysis(verdict, ["c2"], "c2", depends=RULE))
    fake_llm.on("answer", _reply("sure"))
    out = await predict(conn, owner(user), situation="Call at 15?", audience="default")
    second = fake_llm.by_purpose("answer")[0]["user"]
    assert ("Analysis: scratch note\n" in second) is seen
    assert "Case: " not in second  # no stylometry profile, no Case line
    if seen:
        assert second.index("Analysis: ") < second.index("Instruction:")
    assert "scratch note" not in str(out)


def test_answer_user_lines_in_order():
    from wwyd.predict.prompts import answer_user

    text = answer_user(
        owner="Ann",
        audience="work",
        audience_description="",
        claims=["[c1] (style, s) Short."],
        context=None,
        kind="reply",
        situation="Hi?",
        now="Monday",
        instruction="Instruction: x",
        analysis="It fits.",
        case="Case: normal",
    )
    assert text.endswith("Context: none\nCase: normal\nAnalysis: It fits.\nInstruction: x")
    plain = answer_user(
        owner="Ann",
        audience="work",
        audience_description="",
        claims=[],
        context=None,
        kind="reply",
        situation="Hi?",
        now="Monday",
        instruction="Instruction: x",
    )
    assert "Analysis:" not in plain and "Case:" not in plain


async def test_when_call_only_without_routing(conn, user, pid, fake_llm, monkeypatch):
    from datetime import UTC, datetime

    import wwyd.predict as predict_pkg

    # Thursday 22:20 in Berlin; "day after tomorrow" is Saturday.
    monkeypatch.setattr(predict_pkg, "_now", lambda: datetime(2026, 10, 22, 20, 20, tzinfo=UTC))
    seed(conn, pid, [STYLE, MEETING])
    update_settings(conn, pid, timezone="Europe/Berlin")
    fake_llm.on("when", {"when": "day after tomorrow 17:00"})
    fake_llm.on("answer", lambda call: _reply())
    await predict(conn, owner(user), situation="Call?", audience="default")
    assert [c["purpose"] for c in fake_llm.calls] == ["when", "analyze", "answer"]
    assert fake_llm.by_purpose("when")[0]["user"] == "Situation: Call?"
    for purpose in ("analyze", "answer"):
        user_msg = fake_llm.by_purpose(purpose)[-1]["user"]
        assert "\nWhen: Saturday 2026-10-24 (weekend)\n" in user_msg
        assert "Days:" not in user_msg
    # A custom weekend: Friday and Saturday; Sunday is a working day.
    update_settings(conn, pid, weekend_days=["Friday", "saturday"])
    fake_llm.on("when", {"when": "sun"})
    await predict(conn, owner(user), situation="Call?", audience="default")
    assert "\nWhen: Sunday 2026-10-25 (working day)\n" in fake_llm.by_purpose("analyze")[-1]["user"]


@pytest.mark.parametrize("value", ["", "whenever you like", "the 5th", None, "fail"])
async def test_no_when_line_without_a_parseable_time(conn, user, pid, fake_llm, value):
    def failing(call):
        raise LLMUnavailable("down")

    seed(conn, pid, [STYLE, MEETING])
    fake_llm.on("when", failing if value == "fail" else {"when": value})
    fake_llm.on("answer", lambda call: _reply())
    out = await predict(conn, owner(user), situation="Call?", audience="default")
    assert out["type"] == "answer"  # fail safe: predict still answers
    for purpose in ("analyze", "answer"):
        assert "\nWhen:" not in fake_llm.by_purpose(purpose)[-1]["user"]


async def test_route_when_reaches_both_calls(conn, user, pid, fake_llm, monkeypatch):
    from datetime import UTC, datetime

    import wwyd.predict as predict_pkg

    monkeypatch.setattr(predict_pkg, "_now", lambda: datetime(2026, 10, 23, 9, 0, tzinfo=UTC))
    big_set(conn, pid)
    fake_llm.on("route", {"categories": ["work/meetings"], "when": "tomorrow 17:00"})
    fake_llm.on("answer", answer())
    await predict(conn, owner(user), situation="Meet tomorrow?", audience="everyone")
    assert "when" in fake_llm.by_purpose("route")[0]["schema"]["required"]
    assert not fake_llm.by_purpose("when")  # routing carried it: no extra call
    for purpose in ("analyze", "answer"):
        user_msg = fake_llm.by_purpose(purpose)[-1]["user"]
        assert "\nWhen: Saturday 2026-10-24 (weekend)\n" in user_msg
    # A named weekday days ahead is marked as a working day on the route path too.
    monkeypatch.setattr(predict_pkg, "_now", lambda: datetime(2026, 10, 26, 8, 0, tzinfo=UTC))
    fake_llm.on("route", {"categories": ["work/meetings"], "when": "fri 17:00"})
    await predict(conn, owner(user), situation="Meet on Friday?", audience="everyone")
    for purpose in ("analyze", "answer"):
        user_msg = fake_llm.by_purpose(purpose)[-1]["user"]
        assert "\nWhen: Friday 2026-10-30 (working day)\n" in user_msg


async def test_ruled_out_answer_that_does_not_decline_is_reasked(conn, user, pid, fake_llm):
    seed(conn, pid, [STYLE, MEETING])
    fake_llm.on("analyze", _analysis("settled", c_excluded_by="c2"))
    fake_llm.on("answer", _scripted(_reply("maybe", z_commits=False), _reply("no", z_commits=True)))
    out = await predict(conn, owner(user), situation="Call at 15?", audience="default")
    calls = fake_llm.by_purpose("answer")
    assert len(calls) == 2 and RULED_OUT_RETRY_NOTE in calls[1]["user"]
    assert out["type"] == "answer" and out["answer"] == "no" and out["confidence"] == 0.65
