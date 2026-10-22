# SPDX-License-Identifier: AGPL-3.0-or-later
"""Style in ``predict`` (SPEC §8.1 steps 6 and 8, D22): explicit parameters from the
audience's approved stylometry claim, and deterministic post-processing of replies.

Only the claim keyed ``builtin:stylometry:<audience_id>`` counts, and only when it passes
the same SQL visibility filter as every other context claim (§8.3): approved, applying to
the resolved audience, ``owner_only`` only on the owner's explicit request. Nothing here
logs or stores the situation or the answer.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field

from wwyd.analyzers.builtin import stylometry
from wwyd.errors import LLMUnavailable, RateLimited, SpendCap
from wwyd.llm import LLM
from wwyd.predict.context import _AUDIENCE, _VISIBILITY

STRONG = 0.8  # a post-processing rule applies only where the profile is this consistent
SHORTEN_FACTOR = 3  # re-ask when the answer is longer than 3 x the owner's p90 length

SHORTEN_SYSTEM = """You shorten a chat message to the requested length.
Keep its meaning, language, tone and any commitments; drop filler, greetings and repetition.
Do not add anything. Return JSON: {"answer": "<the shortened message>"}."""

SHORTEN_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["answer"],
    "properties": {"answer": {"type": "string"}},
}


@dataclass(frozen=True)
class StyleProfile:
    claim_id: str
    n: int
    chars_p50: float
    chars_p90: float
    sentences_p50: float
    sentences_p90: float
    lowercase_share: float
    ending: str
    ending_share: float
    period_share: float
    emoji_share: float
    words: list[str] = field(default_factory=list)
    phrases: list[str] = field(default_factory=list)


def _num(stats: dict, name: str) -> float:
    v = stats.get(name)
    return float(v) if isinstance(v, int | float) and not isinstance(v, bool) else 0.0


def load_profile(
    conn: sqlite3.Connection, persona_id: str, audience_id: str, *, include_private: bool
) -> StyleProfile | None:
    """The approved, visible stylometry claim of exactly this audience, or None."""
    row = conn.execute(
        "SELECT c.id, c.text, c.origin, c.evidence FROM claims c"
        " WHERE c.persona_id = :persona AND c.status = 'approved' AND c.key = :key"
        + _AUDIENCE
        + _VISIBILITY,
        {
            "persona": persona_id,
            "audience": audience_id,
            "private": int(include_private),
            "key": f"builtin:{stylometry.NAME}:{audience_id}",
        },
    ).fetchone()
    if row is None:
        return None
    try:
        stats = (json.loads(row["evidence"] or "{}") or {}).get("stats") or {}
    except ValueError:
        stats = {}
    if not isinstance(stats, dict) or _num(stats, "n") <= 0:
        return None
    words = stylometry.favourite_words(conn, persona_id, audience_id)
    phrases: list[str] = []
    if row["origin"] == "derived":
        phrases = stylometry.favourite_phrases(conn, persona_id, audience_id)
    else:  # the owner edited the text: only the words it still names
        said = set(re.findall(r"\w+", (row["text"] or "").casefold()))
        words = [w for w in words if w in said]
    idx = int(_num(stats, "ending_dominant"))
    ending = stylometry.ENDING_CLASSES[idx] if 0 <= idx < len(stylometry.ENDING_CLASSES) else "none"
    return StyleProfile(
        claim_id=row["id"],
        n=int(_num(stats, "n")),
        chars_p50=_num(stats, "chars_p50"),
        chars_p90=_num(stats, "chars_p90"),
        sentences_p50=_num(stats, "sentences_p50"),
        sentences_p90=_num(stats, "sentences_p90"),
        lowercase_share=_num(stats, "lowercase_share"),
        ending=ending,
        ending_share=_num(stats, "ending_share"),
        period_share=_num(stats, "period_share"),
        emoji_share=_num(stats, "emoji_share"),
        words=words[:10],
        phrases=phrases,
    )


def _pct(x: float) -> str:
    return f"{int(x * 100 + 0.5)}%"


def parameters(p: StyleProfile) -> str:
    """The style block of the answer's user message (the system prompt stays static)."""
    lines = [
        f"- length: typically {int(p.chars_p50)} characters, 90% under {int(p.chars_p90)};"
        f" {int(p.sentences_p50)} sentence(s), at most {int(p.sentences_p90)} usually",
        f"- starts lowercase in {_pct(p.lowercase_share)} of messages",
        f"- usual ending: {p.ending.replace('_', ' ')} ({_pct(p.ending_share)});"
        f" final period in {_pct(p.period_share)}",
        f"- emoji in {_pct(p.emoji_share)} of messages",
    ]
    if p.words:
        lines.append("- favourite words: " + ", ".join(p.words))
    if p.phrases:
        lines.append("- favourite phrases: " + ", ".join(p.phrases))
    return "Owner's style with this audience (match it in the answer):\n" + "\n".join(lines)


CASE_NORMAL = "Case: capital at the start of each sentence"
CASE_LOWER = "Case: lowercase start"


def case_line(p: StyleProfile | None) -> str | None:
    """The answer call's ``Case:`` line, only when a strong stylometry profile decides it.
    Style claim text is never parsed (it may be in any language); without a decisive
    profile there is no line and the answer follows the style claims."""
    if p is None:
        return None
    if p.lowercase_share >= STRONG:
        return CASE_LOWER
    if p.lowercase_share <= 1 - STRONG:
        return CASE_NORMAL
    return None


def _upper_first(text: str) -> str:
    for i, ch in enumerate(text):
        if ch.isalpha():
            # Leave mixed-case words ("iPhone") alone.
            nxt = text[i + 1 : i + 2]
            if ch.islower() and not nxt.isupper():
                return text[:i] + ch.upper() + text[i + 1 :]
            return text
        if not ch.isspace() and ch not in "\"'«“(":
            return text
    return text


def _lower_first(text: str) -> str:
    for i, ch in enumerate(text):
        if ch.isalpha():
            # Leave acronyms ("OK", "NASA") and a lone "I" alone.
            nxt = text[i + 1 : i + 2]
            if ch.isupper() and not nxt.isupper() and not (ch == "I" and not nxt.isalpha()):
                return text[:i] + ch.lower() + text[i + 1 :]
            return text
        if not ch.isspace() and ch not in "\"'«“(":
            return text
    return text


def _drop_period(text: str) -> str:
    s = text.rstrip()
    if s.endswith(".") and not s.endswith(".."):
        return s[:-1].rstrip()
    return text


def apply_rules(text: str, p: StyleProfile) -> str:
    """Lowercase or capital start and no final period, each only where the profile is
    strong."""
    if p.lowercase_share >= STRONG:
        text = _lower_first(text)
    elif p.lowercase_share <= 1 - STRONG:
        text = _upper_first(text)
    if 1 - p.period_share >= STRONG:
        text = _drop_period(text)
    return text


def too_long(text: str, p: StyleProfile) -> bool:
    return p.chars_p90 > 0 and len(text) > SHORTEN_FACTOR * p.chars_p90


async def shorten(llm: LLM, text: str, p: StyleProfile) -> tuple[str | None, int]:
    """One re-ask (purpose ``shorten``); ``(None, tokens)`` when it fails or is still too
    long. Budget and spend limits apply inside the LLM client like for the answer call."""
    user = (
        f"Target: at most {int(p.chars_p90)} characters"
        f" (typically {int(p.chars_p50)}).\nMessage: {text}"
    )
    try:
        result = await llm.complete_json(
            purpose="shorten",
            system=SHORTEN_SYSTEM,
            user=user,
            schema=SHORTEN_SCHEMA,
            max_tokens=512,
        )
    except (LLMUnavailable, RateLimited, SpendCap):
        return None, 0
    tokens = result.tokens_in + result.tokens_out
    data = result.data
    out = data.get("answer") if isinstance(data, dict) else None
    if not isinstance(out, str) or not out.strip() or too_long(out.strip(), p):
        return None, tokens
    return out.strip(), tokens
