# SPDX-License-Identifier: AGPL-3.0-or-later
"""The answer call and its post-processing (SPEC §8.1 steps 6-7)."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from wwyd.errors import LLMUnavailable
from wwyd.llm import LLM
from wwyd.predict.prompts import ANSWER_SCHEMA, ANSWER_SYSTEM, DEPENDS_KEY

MAX_FIELD = 500
MAX_DEPENDS_ON = 400  # SPEC §8.1


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


_SENTENCE_END = re.compile(r"[.!?…](?=\s|$)")


def depends_on(value: Any, limit: int = MAX_DEPENDS_ON) -> str | None:
    """The public ``depends_on``: stripped, ``None`` when empty, cut to ``limit`` chars at
    a sentence boundary if one is in the second half, else at a word boundary."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = " ".join(value.split())
    if len(text) <= limit:
        return text
    head = text[:limit]
    ends = [m.end() for m in _SENTENCE_END.finditer(head)]
    if ends and ends[-1] >= limit // 2:
        return head[: ends[-1]]
    cut = head.rsplit(" ", 1)[0] if " " in head else head
    return cut.rstrip(" ,;:-") + "…"


def interpret(
    data: dict[str, Any],
    context: Mapping[str, str] | Iterable[str],
    *,
    has_boundary: bool = True,
) -> dict[str, Any]:
    """Normalise the model's reply into answer | escalate (before thresholds).

    ``context`` maps the prompt's claim labels to claim IDs (a plain set of IDs also works).
    A "boundary" escalation without any boundary claim in the context is reported as
    out_of_persona (small models use it for "I lack information"). A non-empty
    ``depends_on`` is never a reason to escalate by itself.
    """
    ids = _id_map(context)
    kind = data.get("type")
    used = used_claims(data, ids)
    if kind == "answer" and _str(data.get("answer"), 8000):
        return {
            "type": "answer",
            "answer": _str(data.get("answer"), 8000),
            "depends_on": depends_on(data.get(DEPENDS_KEY)),
            "confidence": _confidence(data.get("confidence")),
            "used_claims": used,
            "language": _str(data.get("language"), 16) or None,
        }
    if kind not in ("answer", "escalate"):
        # The model did not commit to an answer (e.g. the pre-D23 "needs" type); it did not
        # say the situation is outside the persona.
        return {
            "type": "escalate",
            "escalate": "low_confidence",
            "reason": "the model did not commit to an answer",
            "_confidence": 0.0,
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
