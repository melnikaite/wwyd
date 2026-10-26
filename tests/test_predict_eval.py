# SPDX-License-Identifier: AGPL-3.0-or-later
"""Live evaluation of predict over fixed scenarios (``uv run pytest -m llm -k predict_eval -s``).

Scenarios live in ``tests/data/predict_scenarios.json``; ``WWYD_EVAL_FILE=<name>`` picks another
file in ``tests/data`` (e.g. the blind holdout ``predict_holdout.json``). Each runs ``WWYD_EVAL_RUNS`` times
(default 3); structural checks are done in code, the stance by an LLM judge on the same
backend (``wwyd.eval.judge_stance``). The test prints a per-scenario table and the overall
pass rate and fails only below ``WWYD_EVAL_MIN_PASS`` (0..1, default 0 = report only).
``WWYD_EVAL_ONLY=id1,id2`` runs a subset.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

import wwyd.predict as P
from wwyd.analyzers.builtin import stylometry
from wwyd.eval import judge_stance
from wwyd.mcp.caller import Caller
from wwyd.predict.language import script
from wwyd.store.audiences import get_audience
from wwyd.store.categories import category_ids_by_path, upsert_category
from wwyd.store.claims import add_claims, insert_claim
from wwyd.store.personas import create_user, update_settings

pytestmark = [pytest.mark.llm, pytest.mark.anyio]
EVAL_FILE = os.environ.get("WWYD_EVAL_FILE") or "predict_scenarios.json"
DATA = json.loads((Path(__file__).parent / "data" / Path(EVAL_FILE).name).read_text("utf-8"))
_GERMAN = re.compile(
    r"[äöüß]|\b(ich|nicht|morgen|zu|kalt|leider|laufen|heute|bin|das|ja|gerne?|klappt)\b", re.I
)
# Languages the eval checks only by script (a coarse check; ru/en/de have a finer one).
_SCRIPTS = {"uk": "CYRILLIC", "es": "LATIN", "fr": "LATIN", "pl": "LATIN", "ja": "CJK"}


def language_of(text: str) -> str:
    if re.search(r"[а-яё]", text, re.I):
        return "ru"
    return "de" if _GERMAN.search(text) else "en"


def style_fails(exp: dict[str, Any], answer: str) -> list[str]:
    """Deterministic style expectations of a ``type: answer`` result."""
    fails = []
    text = answer.strip()
    if (limit := exp.get("max_chars")) and len(text) > limit:
        fails.append(f"style: {len(text)} chars > {limit}")
    if exp.get("lowercase_start"):
        first = next((ch for ch in text if ch.isalpha()), "")
        if first.isupper():
            fails.append("style: capital start")
    if exp.get("capital_start"):
        first = next((ch for ch in text if ch.isalpha()), "")
        if first.islower():
            fails.append("style: lowercase start")
    if exp.get("no_final_period") and text.endswith(".") and not text.endswith(".."):
        fails.append("style: final period")
    banned = [w for w in exp.get("absent_any", []) if w.lower() in text.lower()]
    if banned:
        fails.append(f"style: contains {banned[0]!r}")
    return fails


def answer_language(text: str, expected: str) -> str:
    if expected in _SCRIPTS:
        found = script(text)
        if expected == "uk" and re.search(r"[ыэъё]", text, re.I):
            return "ru"
        # Too few letters to tell: not a failure.
        return expected if found in (None, _SCRIPTS[expected]) else (found or "").lower()
    return language_of(text)


async def check(conn, pid: str, scenario: dict[str, Any], out: dict[str, Any]) -> list[str]:
    exp = scenario["expect"]
    if out["type"] != exp["type"]:
        return [f"type {out['type']}/{out.get('escalate')} != {exp['type']}"]
    if out["type"] == "escalate":
        if exp.get("escalate") and out.get("escalate") != exp["escalate"]:
            return [f"escalate {out.get('escalate')} != {exp['escalate']}"]
        return []
    fails = []
    answer = out.get("answer") or ""
    dep = exp.get("depends_on", "any")
    if dep == "null" and out.get("depends_on"):
        fails.append("depends_on set")
    if dep == "set" and not out.get("depends_on"):
        fails.append("depends_on null")
    if (
        exp.get("language")
        and (lang := answer_language(answer, exp["language"])) != exp["language"]
    ):
        fails.append(f"language {lang}")
    words = exp.get("mentions_any")
    if words and not any(w.lower() in answer.lower() for w in words):
        fails.append("missing mention")
    fails.extend(style_fails({**DATA.get("style", {}).get(scenario["persona"], {}), **exp}, answer))
    if exp.get("stance", "any") != "any":
        stance = await judge_stance(conn, pid, scenario["situation"], answer)
        if stance != exp["stance"]:
            fails.append(f"stance {stance}")
    return fails


async def test_predict_eval(conn, monkeypatch):
    runs = int(os.environ.get("WWYD_EVAL_RUNS", "3"))
    threshold = float(os.environ.get("WWYD_EVAL_MIN_PASS", "0"))
    only = {s for s in os.environ.get("WWYD_EVAL_ONLY", "").split(",") if s}
    show = os.environ.get("WWYD_EVAL_SHOW") == "1"  # print every answer (debugging)
    if show:
        from wwyd.predict import answer as answer_step

        analyze = answer_step.analyze_call

        async def spy(llm, user):
            data, tokens = await analyze(llm, user)
            print("  analyze:", json.dumps(data, ensure_ascii=False))
            return data, tokens

        monkeypatch.setattr(answer_step, "analyze_call", spy)
    now = datetime.fromisoformat(DATA["now"])
    monkeypatch.setattr(P, "_now", lambda: now)

    owners: dict[str, Caller] = {}
    for name, persona in DATA["personas"].items():
        user = create_user(conn, f"{name}@example.com")
        update_settings(conn, user.persona_id, timezone=persona["timezone"])
        for c in persona["claims"]:
            parts = c["category"].split("/")
            for i in range(1, len(parts) + 1):
                with contextlib.suppress(Exception):  # already exists
                    upsert_category(conn, user.persona_id, "/".join(parts[:i]))
        claims = [{"status": "approved", **c} for c in persona["claims"]]
        result = add_claims(conn, user.persona_id, claims, absorb=False, allow_approved=True)
        assert not result["dropped"], result["dropped"]
        if stats := persona.get("stylometry"):
            audience = get_audience(conn, user.persona_id, "default")["id"]
            insert_claim(
                conn,
                user.persona_id,
                kind="style",
                category_id=category_ids_by_path(conn, user.persona_id)["communication/style"],
                text=stats["text"],
                audiences_all=False,
                audience_ids=[audience],
                visibility="shared",
                status="approved",
                confidence=0.9,
                evidence={
                    "sources": {"analyzer": int(stats["stats"]["n"])},
                    "stats": stats["stats"],
                },
                origin="derived",
                key=f"builtin:{stylometry.NAME}:{audience}",
            )
        owners[name] = Caller(user.user_id, user.persona_id, "owner")

    scenarios = [s for s in DATA["scenarios"] if not only or s["id"] in only]
    assert scenarios, "WWYD_EVAL_ONLY matched no scenario"
    rows, passed, total = [], 0, 0
    for sc in scenarios:
        caller = owners[sc["persona"]]
        at = datetime.fromisoformat(sc.get("now") or DATA["now"])
        monkeypatch.setattr(P, "_now", lambda at=at: at)
        ok, reasons = 0, []
        for _ in range(runs):
            out = await P.predict(
                conn,
                caller,
                situation=sc["situation"],
                audience="default",
                context=sc.get("context"),
                kind=sc.get("kind", "reply"),
                audit=False,
            )
            fails = await check(conn, caller.persona_id, sc, out)
            if show:
                print(f"{sc['id']}: {out.get('answer') or out.get('escalate')!r} {fails}")
            ok += not fails
            reasons.extend(fails)
        passed, total = passed + ok, total + runs
        rows.append((sc["id"], ok, "; ".join(dict.fromkeys(reasons))))

    width = max(len(r[0]) for r in rows)
    print(f"\n{'scenario':<{width}}  pass  failures")
    for sid, ok, why in rows:
        print(f"{sid:<{width}}  {ok}/{runs}   {why}")
    rate = passed / total
    print(f"overall: {passed}/{total} = {rate:.0%}")
    assert rate >= threshold, f"pass rate {rate:.0%} below {threshold:.0%}"
