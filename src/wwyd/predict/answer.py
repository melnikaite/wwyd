# SPDX-License-Identifier: AGPL-3.0-or-later
"""The answer call and its post-processing (SPEC §8.1 steps 6-7)."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from wwyd.errors import LLMUnavailable
from wwyd.llm import LLM
from wwyd.predict.prompts import ANSWER_SCHEMA, ANSWER_SYSTEM

MAX_NEEDS = 10
# Machine-readable need kinds (SPEC §6.5). Clients treat unknown kinds as "other".
NEED_KINDS = ("calendar.free_busy", "contact.relationship", "other")
MAX_FIELD = 500


async def ask(llm: LLM, user: str, *, kind: str = "reply") -> tuple[dict[str, Any], int]:
    # A reply is a chat message; a decision may explain itself a little more.
    result = await llm.complete_json(
        purpose="answer",
        system=ANSWER_SYSTEM,
        user=user,
        schema=ANSWER_SCHEMA,
        max_tokens=1024 if kind == "reply" else 2048,
    )
    if not isinstance(result.data, dict):
        raise LLMUnavailable("LLM reply has an unexpected shape")
    return result.data, result.tokens_in + result.tokens_out


def _str(value: Any, limit: int = MAX_FIELD) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def _confidence(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0.0
    return min(1.0, max(0.0, float(value)))


def _id_map(context: Mapping[str, str] | Iterable[str]) -> dict[str, str]:
    """Prompt label -> claim ID. The prompt shows short labels (``c1``), not claim IDs."""
    if isinstance(context, Mapping):
        return {**{v: v for v in context.values()}, **context}
    return {i: i for i in context}


def used_claims(data: dict[str, Any], context: Mapping[str, str] | Iterable[str]) -> list[str]:
    """Map labels back to claim IDs; drop ones the model was not shown (hallucinated or from
    examples)."""
    ids = _id_map(context)
    raw = data.get("used_claims")
    out: list[str] = []
    for item in raw if isinstance(raw, list) else []:
        claim_id = ids.get(item.strip().strip("[]")) if isinstance(item, str) else None
        if claim_id is not None and claim_id not in out:
            out.append(claim_id)
    return out


_LABEL = re.compile(r"\[?\b(c\d+)\b\]?")


def _why(value: Any, ids: dict[str, str]) -> str:
    return _LABEL.sub(lambda m: ids.get(m.group(1), m.group(0)), _str(value))


def _same(a: str, b: str) -> bool:
    return a.casefold().strip(" .") == b.casefold().strip(" .")


def _has_facts(raw: Any) -> bool:
    return isinstance(raw, list) and any(isinstance(i, dict) and _str(i.get("what")) for i in raw)


def interpret(
    data: dict[str, Any],
    context: Mapping[str, str] | Iterable[str],
    *,
    has_boundary: bool = True,
    unavailable: Iterable[dict[str, Any]] = (),
) -> dict[str, Any]:
    """Normalise the model's reply into answer | needs | escalate (before thresholds).

    ``context`` maps the prompt's claim labels to claim IDs (a plain set of IDs also works).
    A "boundary" escalation without any boundary claim in the context is reported as
    out_of_persona (small models use it for "I lack information"). ``unavailable`` are the
    facts the harness answered with ``value: null``: a need for one of them (same text, or
    same specific kind) is dropped, and if nothing is left the result is low_confidence.
    """
    ids = _id_map(context)
    gone = [f for f in unavailable if isinstance(f.get("what"), str)]
    kind = data.get("type")
    if (
        kind == "escalate"
        and data.get("escalate") != "boundary"
        and _has_facts(data.get("absent_facts"))
    ):
        # Small models list the missing facts and still pick escalate; the facts win,
        # except over a boundary, which always stands.
        kind = "needs"
    used = used_claims(data, ids)
    if kind == "needs":
        needs = []
        dropped = False
        raw = data.get("absent_facts", data.get("needs"))
        for item in raw if isinstance(raw, list) else []:
            if isinstance(item, dict) and _str(item.get("what")):
                need_kind = item.get("kind") if item.get("kind") in NEED_KINDS else "other"
                what = _str(item.get("what"))
                if any(
                    _same(what, f["what"]) or (need_kind != "other" and f.get("kind") == need_kind)
                    for f in gone
                ):
                    dropped = True
                    continue
                needs.append({"kind": need_kind, "what": what, "why": _why(item.get("why"), ids)})
            if len(needs) >= MAX_NEEDS:
                break
        if needs:
            return {"type": "needs", "needs": needs, "used_claims": used}
        if dropped:
            return {
                "type": "escalate",
                "escalate": "low_confidence",
                "reason": "a fact the decision needs is not available",
                "_confidence": 0.0,
            }
        kind = "escalate"
    if kind == "answer" and _str(data.get("answer"), 8000):
        return {
            "type": "answer",
            "answer": _str(data.get("answer"), 8000),
            "confidence": _confidence(data.get("confidence")),
            "used_claims": used,
            "language": _str(data.get("language"), 16) or None,
        }
    code = data.get("escalate")
    if code == "boundary" and not has_boundary:
        code = "out_of_persona"
    if kind == "escalate" and code in ("boundary", "out_of_persona"):
        return {"type": "escalate", "escalate": code, "reason": _str(data.get("reason"))}
    return {
        "type": "escalate",
        "escalate": "out_of_persona",
        "reason": _str(data.get("reason")) or "no usable prediction",
    }
