# SPDX-License-Identifier: AGPL-3.0-or-later
"""The answer call and its post-processing (SPEC §8.1 steps 6-7)."""

from __future__ import annotations

from typing import Any

from wwyd.errors import LLMUnavailable
from wwyd.llm import LLM
from wwyd.predict.prompts import ANSWER_SCHEMA, ANSWER_SYSTEM

MAX_NEEDS = 10
MAX_FIELD = 500


async def ask(llm: LLM, user: str) -> tuple[dict[str, Any], int]:
    result = await llm.complete_json(
        purpose="answer", system=ANSWER_SYSTEM, user=user, schema=ANSWER_SCHEMA, max_tokens=2048
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


def used_claims(data: dict[str, Any], context_ids: set[str]) -> list[str]:
    """Drop IDs the model was not shown (hallucinated or from examples)."""
    raw = data.get("used_claims")
    out: list[str] = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, str) and item in context_ids and item not in out:
            out.append(item)
    return out


def interpret(
    data: dict[str, Any], context_ids: set[str], *, has_boundary: bool = True
) -> dict[str, Any]:
    """Normalise the model's reply into answer | needs | escalate (before thresholds).

    A "boundary" escalation without any boundary claim in the context is reported as
    out_of_persona (small models use it for "I lack information").
    """
    kind = data.get("type")
    used = used_claims(data, context_ids)
    if kind == "needs":
        needs = []
        raw = data.get("needs")
        for item in raw if isinstance(raw, list) else []:
            if isinstance(item, dict) and _str(item.get("what")):
                needs.append({"what": _str(item.get("what")), "why": _str(item.get("why"))})
            if len(needs) >= MAX_NEEDS:
                break
        if needs:
            return {"type": "needs", "needs": needs, "used_claims": used}
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
