# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import logging
from typing import Any

import pytest
from conftest import call, make_server
from mcp import Client

from wwyd.errors import LLMUnavailable, RateLimited, SpendCap
from wwyd.limits import get_limiter
from wwyd.mcp.caller import Caller
from wwyd.predict import predict
from wwyd.predict.disclosure import TEMPLATES
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
    assert "[c1]" in fake_llm.calls[0]["user"] and "[c2]" in fake_llm.calls[0]["user"]
    assert not any(i in fake_llm.calls[0]["user"] for i in ids)


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


def _reply(depends: Any = "", text: str = "let me check", **kw: Any) -> dict[str, Any]:
    return {
        "a_depends_on": depends,
        "type": "answer",
        "answer": text,
        "confidence": 0.9,
        "used_claims": [],
        "language": "en",
        "escalate": "",
        "reason": "",
        **kw,
    }


async def test_depends_on_passes_through(conn, user, pid, fake_llm):
    seed(conn, pid, [STYLE, MEETING])
    rule = "Agrees if 14:55-15:25 is free. Otherwise offers the nearest slot."
    fake_llm.on("answer", lambda call: _reply(rule))
    out = await predict(conn, owner(user), situation="Call at 15?", audience="default")
    assert out["type"] == "answer" and out["depends_on"] == rule
    assert out["answer"] == "let me check"
    assert "needs" not in out and "a_depends_on" not in out


@pytest.mark.parametrize("raw", ["", "   ", None, 5])
async def test_depends_on_null_when_absent(conn, user, pid, fake_llm, raw):
    seed(conn, pid, [STYLE])
    fake_llm.on("answer", lambda call: _reply(raw, text="sure"))
    out = await predict(conn, owner(user), situation="Hi", audience="default")
    assert out["type"] == "answer" and out["depends_on"] is None


def test_depends_on_cut_to_400():
    from wwyd.predict.answer import depends_on

    sentences = "Talks to friends after 18:00. " * 20
    cut = depends_on(sentences)
    assert len(cut) <= 400 and cut.endswith("18:00.")
    words = "word " * 200
    cut = depends_on(words)
    assert len(cut) <= 401 and cut.endswith("word…")
    assert depends_on("  short  ") == "short"


def test_answer_schema_key_sorts_before_answer():
    from wwyd.predict.prompts import ANSWER_SCHEMA, DEPENDS_KEY

    assert DEPENDS_KEY < "answer"
    assert DEPENDS_KEY in ANSWER_SCHEMA["properties"] and DEPENDS_KEY in ANSWER_SCHEMA["required"]
    assert "absent_facts" not in ANSWER_SCHEMA["properties"]
    assert ANSWER_SCHEMA["properties"]["type"]["enum"] == ["answer", "escalate"]


def test_interpret_maps_internal_key():
    from wwyd.predict.answer import interpret

    out = interpret(_reply("depends on the weather"), set())
    assert out["depends_on"] == "depends on the weather" and "a_depends_on" not in out
    # A non-empty depends_on is never a reason to escalate.
    assert out["type"] == "answer"
    # The old "needs" type (or any unknown type) is a failure to commit: low_confidence.
    for unknown in ("needs", "maybe", None):
        out = interpret({**_reply(), "type": unknown, "answer": ""}, set())
        assert (out["type"], out["escalate"]) == ("escalate", "low_confidence")


async def test_context_in_user_message_not_system(conn, user, pid, fake_llm):
    seed(conn, pid, [STYLE, MEETING])
    fake_llm.on("answer", lambda call: _reply())
    await predict(conn, owner(user), situation="Call?", audience="default")
    await predict(
        conn, owner(user), situation="Call?", audience="default", context="free from 19:30"
    )
    first, second = fake_llm.calls[-2:]
    assert "Context: none" in first["user"]
    assert "Context: free from 19:30" in second["user"]
    assert "19:30" not in second["system"]
    assert first["system"] == second["system"]


async def test_predict_tool_schema_has_context_not_facts(client, conn, pid, fake_llm):
    seed(conn, pid, [STYLE])
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
    seed(conn, pid, [STYLE])

    def handler(call):
        raise exc

    fake_llm.on("answer", handler)
    out = await predict(conn, owner(user), situation="Hi", audience="everyone")
    assert out["type"] == "escalate" and out["escalate"] == code
    assert audit.list_audit(conn, pid)["entries"][0]["outcome"] == f"escalate:{code}"


async def test_model_escalations_and_junk(conn, user, pid, fake_llm):
    seed(conn, pid, [STYLE])
    reply = {
        "type": "escalate",
        "answer": "",
        "confidence": 0,
        "used_claims": [],
        "language": "en",
        "a_depends_on": "",
        "escalate": "boundary",
        "reason": "never in writing",
    }
    fake_llm.on("answer", reply)
    out = await predict(conn, owner(user), situation="Salary?", audience="everyone")
    assert out["escalate"] == "out_of_persona"  # no boundary claim in the context
    seed(
        conn,
        pid,
        [{"kind": "boundary", "category": "communication/boundaries", "text": "No salary."}],
    )
    out = await predict(conn, owner(user), situation="Salary?", audience="everyone")
    assert (out["escalate"], out["reason"]) == ("boundary", "never in writing")
    fake_llm.on("answer", {"type": "banana"})
    out = await predict(conn, owner(user), situation="Salary?", audience="everyone")
    assert out["escalate"] == "low_confidence"
    fake_llm.on("answer", ["not", "an", "object"])
    out = await predict(conn, owner(user), situation="Salary?", audience="everyone")
    assert out["escalate"] == "llm_unavailable"


async def test_no_visible_claims_escalates_without_llm(conn, user, pid, fake_llm):
    out = await predict(conn, owner(user), situation="Hi", audience="everyone")
    assert out["escalate"] == "out_of_persona"
    assert fake_llm.calls == []


async def test_low_confidence(conn, user, pid, fake_llm):
    seed(conn, pid, [STYLE])
    fake_llm.on("answer", answer(confidence=0.4))
    out = await predict(conn, owner(user), situation="Hi", audience="everyone")
    assert out["type"] == "escalate" and out["escalate"] == "low_confidence"
    assert "answer" not in out
    assert audit.list_audit(conn, pid)["entries"][0]["confidence"] == 0.4
    update_settings(conn, pid, escalate_below=0.3)
    out = await predict(conn, owner(user), situation="Hi", audience="everyone")
    assert out["type"] == "answer"


@pytest.mark.parametrize("lang", ["en", "ru", "de", "uk", "es", "fr", "pt-BR", ""])
async def test_disclosure_languages(conn, user, pid, fake_llm, lang):
    seed(conn, pid, [STYLE])
    update_settings(conn, pid, display_name="Eugene")
    fake_llm.on("answer", answer(lang=lang))
    out = await predict(conn, owner(user), situation="Hi", audience="everyone")
    expected = TEMPLATES.get(lang, TEMPLATES["en"]).replace("{owner}", "Eugene")
    assert out["disclosure"] == expected
    assert "Eugene" in out["disclosure"] and "{owner}" not in out["disclosure"]


async def test_disclosure_override(conn, user, pid, fake_llm):
    seed(conn, pid, [STYLE])
    update_settings(conn, pid, display_name="Eugene", disclosure_template="Bot of {owner}:")
    fake_llm.on("answer", answer(lang="ru"))
    out = await predict(conn, owner(user), situation="Hi", audience="everyone")
    assert out["disclosure"] == "Bot of Eugene:"


async def test_used_claims_not_in_context_dropped(conn, user, pid, fake_llm, audiences):
    (shown,) = seed(conn, pid, [STYLE])
    (hidden,) = seed(conn, pid, [{**MEETING, "visibility": "owner_only"}])
    fake_llm.on("answer", answer(used=[shown, hidden, "clm_a1", shown]))
    out = await predict(conn, owner(user), situation="Hi", audience="work")
    assert out["used_claims"] == [shown]


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
    seed(conn, pid, [STYLE])
    fake_llm.on("answer", answer())
    out = await predict(conn, owner(user), situation="Hi", audience="strangers")
    assert out["type"] == "answer"
    assert any("default audience" in h for h in out["hints"])
    assert "Talking to: everyone" in fake_llm.calls[-1]["user"]


async def test_default_alias_resolves_without_fallback(conn, user, pid, fake_llm):
    seed(conn, pid, [STYLE])
    upsert_audience(conn, pid, "everyone", rename_to="anyone")
    fake_llm.on("answer", answer())
    out = await predict(conn, owner(user), situation="Hi", audience="default")
    assert out["type"] == "answer" and out["hints"] == []
    assert "Talking to: anyone" in fake_llm.calls[-1]["user"]
    assert audit.list_audit(conn, pid)["entries"][0]["audience_fallback"] is False


async def test_default_alias_respond_checks_real_name(conn, user, pid, fake_llm, audiences):
    seed(conn, pid, [STYLE])
    fake_llm.on("answer", answer())
    # A scope naming "default" does not cover the default audience: scopes name real ones.
    out = await predict(conn, respond(user, ("default",)), situation="Hi", audience="default")
    assert out["escalate"] == "audience_not_permitted"
    out = await predict(conn, respond(user, ("everyone",)), situation="Hi", audience="default")
    assert out["type"] == "answer"


# --- limits and audit ----------------------------------------------------------------------


async def test_predict_limits(conn, user, pid, fake_llm, audiences):
    seed(conn, pid, [STYLE])
    fake_llm.on("answer", answer())
    limiter = get_limiter(conn)
    limiter.set_own_limits(user.user_id, {"predict_per_audience_per_day": 1})
    assert (await predict(conn, owner(user), situation="Hi", audience="work"))["type"] == "answer"
    out = await predict(conn, owner(user), situation="Hi", audience="work")
    assert out["escalate"] == "rate_limited"
    assert len(fake_llm.calls) == 1
    # Another audience has its own counter.
    assert (await predict(conn, owner(user), situation="Hi", audience="family"))["type"] == "answer"
    limiter.set_own_limits(user.user_id, {"predict_per_day": 2})
    out = await predict(conn, owner(user), situation="Hi", audience="everyone")
    assert out["escalate"] == "rate_limited"
    assert len(fake_llm.calls) == 2
    assert limiter.usage(user.user_id)["predict"] == 2


async def test_audit_rows(conn, user, pid, fake_llm, audiences):
    seed(conn, pid, [STYLE])
    fake_llm.on("answer", answer(confidence=0.77))
    caller = respond(user, client_name="Responder", subject_id="grt_abc")
    await predict(conn, caller, situation="Hi", audience="work")
    (entry,) = audit.list_audit(conn, pid)["entries"]
    assert entry["outcome"] == "answer" and entry["confidence"] == 0.77
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
    seed(conn, pid, [STYLE])
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
    seed(conn, pid, [STYLE])
    fake_llm.on("answer", answer())
    await call(client, "predict", {"situation": "Hi", "audience": "everyone"})
    out = await call(client, "list_audit", {})
    assert out["entries"][0]["outcome"] == "answer"
    persona = await call(client, "get_persona", {})
    assert persona["claims_used"] == 1


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


def test_labels_map_back_to_claim_ids():
    from wwyd.predict.answer import interpret

    data = _reply(used_claims=["c2", "[c1]", "a2", "c9"])
    out = interpret(data, {"c1": "clm_one", "c2": "clm_two"})
    assert out["used_claims"] == ["clm_two", "clm_one"]


async def test_prediction_id_and_used_claims_in_audit(conn, user, pid, fake_llm, audiences):
    (style, meeting) = seed(conn, pid, [STYLE, MEETING])
    fake_llm.on("answer", answer("Sure.", used=[meeting]))
    out = await predict(conn, owner(user), situation="Sync Tuesday?", audience="everyone")
    assert out["type"] == "answer" and out["prediction_id"].startswith("adt_")
    entries = {e["id"]: e for e in audit.list_audit(conn, pid)["entries"]}
    assert entries[out["prediction_id"]]["used_claims"] == [meeting]
    stored = conn.execute(
        "SELECT used_claims FROM audit WHERE id = ?", (out["prediction_id"],)
    ).fetchone()[0]
    assert "Sure" not in stored and "Sync" not in stored

    esc = await predict(conn, respond(user, audiences=()), situation="Hi", audience="work")
    assert esc["type"] == "escalate" and esc["prediction_id"].startswith("adt_")
    assert esc["prediction_id"] != out["prediction_id"]

    quiet = await predict(conn, owner(user), situation="Hi", audience="everyone", audit=False)
    assert "prediction_id" not in quiet
