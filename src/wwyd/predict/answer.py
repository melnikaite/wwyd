# SPDX-License-Identifier: AGPL-3.0-or-later
"""The analyze and answer calls and their post-processing (SPEC §8.1 steps 6-7)."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from wwyd.errors import LLMUnavailable
from wwyd.llm import LLM, Purpose
from wwyd.predict.prompts import (
    ANALYZE_APPLICABLE,
    ANALYZE_CHECK,
    ANALYZE_DEPENDS,
    ANALYZE_EXCLUDED,
    ANALYZE_LABEL,
    ANALYZE_SCHEMA,
    ANALYZE_SYSTEM,
    ANALYZE_VERDICT,
    ANSWER_SCHEMA,
    ANSWER_SYSTEM,
    VERDICTS,
)

MAX_FIELD = 500
MAX_DEPENDS_ON = 400  # SPEC §8.1


async def _call(llm: LLM, purpose: Purpose, system: str, user: str, schema: dict[str, Any], n: int):
    result = await llm.complete_json(
        purpose=purpose,
        system=system,
        user=user,
        schema=schema,
        max_tokens=n,
    )
    if not isinstance(result.data, dict):
        raise LLMUnavailable("LLM reply has an unexpected shape")
    return result.data, result.tokens_in + result.tokens_out


async def analyze_call(llm: LLM, user: str) -> tuple[dict[str, Any], int]:
    return await _call(llm, "analyze", ANALYZE_SYSTEM, user, ANALYZE_SCHEMA, 768)


async def ask(llm: LLM, user: str, *, kind: str = "reply") -> tuple[dict[str, Any], int]:
    # A reply is a chat message; a decision may explain itself a little more.
    n = 512 if kind == "reply" else 1024
    return await _call(llm, "answer", ANSWER_SYSTEM, user, ANSWER_SCHEMA, n)


def _str(value: Any, limit: int = MAX_FIELD) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def confidence(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0.0
    return min(1.0, max(0.0, float(value)))


def _label(item: Any) -> str | None:
    return item.strip().strip("[]") if isinstance(item, str) else None


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
        claim_id = ids.get(_label(item) or "")
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


@dataclass
class Analysis:
    """What the analyze call decided, in claim terms (internal only, never returned)."""

    verdict: str
    applicable: list[str] = field(default_factory=list)  # prompt labels, style excluded
    label: str | None = None  # deciding claim for boundary / ruled_out
    depends_on: str | None = None  # only for "open"
    check: str | None = None  # a_check, only for "settled": passed to the answer call


def read_analysis(data: dict[str, Any], kinds: Mapping[str, str]) -> Analysis:
    """Normalise the analyze reply. ``kinds`` maps prompt labels to claim kinds.

    Labels the model was not shown and style claims are dropped from the applicable set.
    A ``boundary`` verdict needs a boundary claim on the topic; a ``ruled_out`` or
    ``open`` verdict needs its parts, otherwise it degrades to ``settled``. No applicable
    claim means ``none`` whatever the verdict said.
    """
    raw = data.get(ANALYZE_APPLICABLE)
    applicable: list[str] = []
    for item in raw if isinstance(raw, list) else []:
        label = _label(item)
        if label in kinds and kinds[label] != "style" and label not in applicable:
            applicable.append(label)
    verdict = data.get(ANALYZE_VERDICT)
    verdict = verdict if verdict in VERDICTS else "settled"
    label = _label(data.get(ANALYZE_LABEL))
    label = label if label in kinds else None
    if label is not None and label not in applicable and kinds[label] != "style":
        applicable.append(label)
    excluded = _label(data.get(ANALYZE_EXCLUDED))
    if excluded in kinds and kinds[excluded] != "style" and verdict != "boundary":
        # One excluding claim settles it, whatever the verdict said.
        if excluded not in applicable:
            applicable.append(excluded)
        verdict, label = "ruled_out", excluded
    if not applicable:
        return Analysis("none")
    if verdict == "none":
        # The model listed applicable claims but called it "none": the claims win.
        verdict = "settled"
    if verdict == "boundary":
        if label is None or kinds[label] != "boundary":
            label = next((x for x in applicable if kinds[x] == "boundary"), None)
        if label is None:
            verdict = "settled"
    if verdict == "ruled_out" and label is None:
        label = applicable[0]
    deps = depends_on(data.get(ANALYZE_DEPENDS)) if verdict == "open" else None
    if verdict == "open" and deps is None:
        verdict = "settled"
    if verdict not in ("boundary", "ruled_out"):
        label = None
    check = (_str(data.get(ANALYZE_CHECK)) or None) if verdict == "settled" else None
    return Analysis(verdict, applicable, label, deps, check)


def answer_text(data: dict[str, Any]) -> str:
    return _str(data.get("answer"), 8000)


def language(data: dict[str, Any]) -> str | None:
    return _str(data.get("language"), 16) or None
