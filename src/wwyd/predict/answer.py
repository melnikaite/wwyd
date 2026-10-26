# SPDX-License-Identifier: AGPL-3.0-or-later
"""The analyze and answer calls and their post-processing (SPEC §8.1 steps 6-7)."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from wwyd.errors import LLMUnavailable
from wwyd.llm import LLM, Purpose
from wwyd.predict.language import normalise
from wwyd.predict.prompts import (
    ANALYZE_APPLICABLE,
    ANALYZE_CHECK,
    ANALYZE_DEPENDS,
    ANALYZE_EXCLUDED,
    ANALYZE_LANGUAGE,
    ANALYZE_SCHEMA,
    ANALYZE_SYSTEM,
    ANALYZE_VERDICT,
    ANSWER_SCHEMA,
    ANSWER_SYSTEM,
    VERDICTS,
)

MAX_FIELD = 500
MAX_DEPENDS_ON = 400  # SPEC §8.1
# Output budgets (SPEC §8.1): a normal analysis is 70-210 tokens, a reply well under 100.
# The cap is what a degenerate repetition loop costs before the client retries.
ANALYZE_MAX_TOKENS = 384


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
    return await _call(llm, "analyze", ANALYZE_SYSTEM, user, ANALYZE_SCHEMA, ANALYZE_MAX_TOKENS)


async def ask(llm: LLM, user: str, *, kind: str = "reply") -> tuple[dict[str, Any], int]:
    # A reply is a chat message; a decision may explain itself a little more.
    n = 384 if kind == "reply" else 640
    return await _call(llm, "answer", ANSWER_SYSTEM, user, ANSWER_SCHEMA, n)


def _str(value: Any, limit: int = MAX_FIELD) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def _label(item: Any) -> str | None:
    return item.strip().strip("[]") if isinstance(item, str) else None


# Sentence terminals by punctuation, not words, so every script splits alike: Latin and
# Cyrillic .!?… plus Arabic ؟ and ۔, Devanagari । ॥, Ethiopic ።, Armenian ։, Myanmar ။
# need a following space (or the end); CJK full-width 。！？ and the half-width ｡ end a
# sentence on their own, since those scripts put no space after them.
_SENTENCE_END = re.compile(r"[.!?…؟۔।॥።։။](?=\s|$)|[。！？｡]")
# Question marks of the same scripts (Latin, Arabic, full-width).
QUESTION_MARKS = ("?", "؟", "？")


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
    language: str | None = None  # g_language: the situation's language, as the model named it


def read_analysis(
    data: dict[str, Any],
    kinds: Mapping[str, str],
    categories: Mapping[str, str] | None = None,
) -> Analysis:
    """Normalise the analyze reply. ``kinds`` maps prompt labels to claim kinds.

    Labels the model was not shown and style claims are dropped from the applicable set.
    A ``boundary`` verdict needs a boundary claim on the topic, and yields to a refusal
    rule: an excluding non-boundary claim makes it ``ruled_out``, and so does an excluding
    boundary claim when an applicable non-boundary claim shares its category (``categories``
    maps labels to category paths); a ``ruled_out`` or
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
    label = None  # derived below: the excluding claim, or the applicable boundary claim
    excluded = _label(data.get(ANALYZE_EXCLUDED))
    if verdict == "boundary" and categories and excluded in kinds:
        # The model named the boundary claim as excluding although a refusal rule on the
        # same topic (same category) applies too: the refusal rule beats the boundary.
        same = [
            x
            for x in applicable
            if kinds[x] not in ("style", "boundary")
            and kinds[excluded] == "boundary"
            and categories.get(x) == categories.get(excluded)
        ]
        if same:
            excluded = same[0]
    if (
        excluded in kinds
        and kinds[excluded] != "style"
        and (verdict != "boundary" or kinds[excluded] != "boundary")
    ):
        # One excluding claim settles it, whatever the verdict said; a refusal rule
        # (a non-boundary claim that declines) beats a boundary verdict too.
        if excluded not in applicable:
            applicable.append(excluded)
        verdict, label = "ruled_out", excluded
    lang = normalise(data.get(ANALYZE_LANGUAGE))
    if not applicable:
        return Analysis("none", language=lang)
    if verdict == "none":
        # The model listed applicable claims but called it "none": the claims win.
        verdict = "settled"
    if verdict == "boundary":
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
    return Analysis(verdict, applicable, label, deps, check, lang)


def score(found: Analysis, weights: Mapping[str, float], *, reasked: bool) -> float:
    """The public ``confidence``, decided in code (SPEC §8.1 step 7): ``0.5 + 0.5 * s``
    where ``s`` is the context rank (claim confidence × evidence, capped at 1) of the
    deciding claim (the excluding claim for ``ruled_out``, else the strongest applicable
    one); +0.05 when two or more claims apply and none excludes, -0.05 for ``open``,
    -0.1 when a guard re-asked the answer call. Clamped to 0..1."""
    if found.label is not None and found.verdict == "ruled_out":
        support = weights.get(found.label, 0.0)
    else:
        support = max((weights.get(x, 0.0) for x in found.applicable), default=0.0)
    value = 0.5 + 0.5 * min(1.0, max(0.0, support))
    if len(found.applicable) >= 2 and found.verdict != "ruled_out":
        value += 0.05
    if found.verdict == "open":
        value -= 0.05
    if reasked:
        value -= 0.1
    return round(min(1.0, max(0.0, value)), 2)


def answer_text(data: dict[str, Any]) -> str:
    return _str(data.get("answer"), 8000)


def language(data: dict[str, Any]) -> str | None:
    return _str(data.get("language"), 16) or None
