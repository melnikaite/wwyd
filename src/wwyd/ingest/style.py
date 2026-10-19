# SPDX-License-Identifier: AGPL-3.0-or-later
"""LLM derivation of style, notes and decisions (SPEC §7.3).

Builds prompts from the in-memory batch, chunked to about 2500 characters, and turns the
model's JSON into candidate claims. Candidates are guarded (§7.4), deduplicated among
themselves and returned; the job stores them through ``store.claims.add_claims``.
Nothing here logs or stores prompt or response text.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from wwyd.errors import Invalid
from wwyd.ingest.guard import BatchGuard
from wwyd.ingest.records import Batch, Decision, Message
from wwyd.store.common import KINDS, MAX_TEXT, check_category_path, normalize_text, parent_path

CHUNK_CHARS = 2500
# Token estimate per call (SPEC §7.1): (chars of system + user) / CHARS_PER_TOKEN plus a
# fixed output allowance per purpose. A conservative estimate, not a bound: the job stops
# before a call whose estimate does not fit the remaining daily budget, so the cap can be
# overrun by at most one call's actual excess over its estimate.
CHARS_PER_TOKEN = 3  # conservative for Cyrillic and most non-English text
OUTPUT_TOKENS = {"style": 800, "notes": 1000, "decisions": 1000}
MAX_CLAIMS_PER_CALL = 8
DEDUPE_JACCARD = 0.6
SATURATION_CALLS = 5  # consecutive style calls of one audience with nothing new (§7.1)
SINGLE_DECISION_CONFIDENCE = 0.3
RULE_CONFIDENCE = 0.6

_ITEM_PROPS: dict[str, Any] = {
    "kind": {"type": "string", "enum": list(KINDS)},
    "category": {"type": "string"},
    "proposed_category": {"type": ["string", "null"]},
    "text": {"type": "string"},
    "confidence": {"type": "number"},
}


def _schema(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    props = {**_ITEM_PROPS, **(extra or {})}
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["claims"],
        "properties": {
            "claims": {
                "type": "array",
                "maxItems": MAX_CLAIMS_PER_CALL,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": list(props),
                    "properties": props,
                },
            }
        },
    }


STYLE_SCHEMA = _schema()
NOTES_SCHEMA = _schema()
DECISIONS_SCHEMA = _schema({"support": {"type": "integer"}})

_COMMON_RULES = """Rules:
- One statement per claim, at most 200 characters, about the owner in the third person or
  neutral wording ("Short, no greeting").
- Describe patterns, never content. Never copy wording from the input.
- Never mention people's names, companies, places, links, emails, phone numbers or
  account names. Write "a colleague", "family", "a client" instead.
- category: one path from the list below. If none fits, use the closest one and put a new
  path (lowercase segments joined by "/") in proposed_category; otherwise null.
- confidence: 0..1, how clearly the input shows it.
- Write each claim in the language the owner writes in.
- Return {"claims": []} if nothing is clear.

Categories:
"""

STYLE_SYSTEM = (
    """You describe how one person (the owner) writes and talks to one group of people (an
"audience"). The input contains excerpts: lines starting with OWNER are the owner's words,
lines starting with OTHER are the other side's words, given only as context.
Return JSON {"claims": [...]} with up to 5 claims about the owner's style (tone, length,
greetings, language, emoji, pace), boundaries (what the owner refuses or deflects) or
preferences. kind is one of: style, boundary, preference.
"""
    + _COMMON_RULES
    + """{categories}

Example (Russian):
Input:
Audience: family
OTHER: Ты приедешь в субботу?
OWNER: да) к обеду
OTHER: Купишь хлеб по дороге?
OWNER: ок
Answer:
{"claims": [{"kind": "style", "category": "communication/style", "proposed_category": null,
"text": "Отвечает очень коротко, строчными буквами, без приветствия, иногда со скобкой-смайликом.",
"confidence": 0.7}]}
"""
)

NOTES_SYSTEM = (
    """The owner wrote notes about themselves. Extract claims about the owner's character,
preferences, habits, rules for decisions, communication style and boundaries.
Return JSON {"claims": [...]}; kind is one of: trait, preference, habit, decision_rule,
style, boundary.
"""
    + _COMMON_RULES
    + """{categories}

Example (Russian):
Input:
NOTE: По утрам не ем, только чёрный кофе. Созвоны до 10 утра не ставлю.
Answer:
{"claims": [{"kind": "habit", "category": "food", "proposed_category": null,
"text": "Не завтракает, по утрам пьёт только чёрный кофе.", "confidence": 0.8},
{"kind": "decision_rule", "category": "work/meetings", "proposed_category": null,
"text": "Не принимает созвоны раньше 10 утра.", "confidence": 0.8}]}
"""
)

DECISIONS_SYSTEM = (
    """The input lists decisions the owner made: a situation and the owner's choice.
Find the rules behind them. For each rule set support to the number of listed decisions
that follow it. Return JSON {"claims": [...]}; kind is decision_rule or preference.
"""
    + _COMMON_RULES
    + """{categories}

Example (Russian):
Input:
1. SITUATION: Коллега просит созвониться в 19:30. CHOICE: Предложил перенести на утро.
2. SITUATION: Клиент зовёт на встречу в 20:00. CHOICE: Отказался, предложил завтра.
3. SITUATION: Выбор рейса. CHOICE: Взял утренний рейс подороже.
Answer:
{"claims": [{"kind": "decision_rule", "category": "work/meetings", "proposed_category": null,
"text": "Не соглашается на рабочие встречи вечером, переносит их на утро или следующий день.",
"confidence": 0.7, "support": 2},
{"kind": "preference", "category": "travel", "proposed_category": null,
"text": "Предпочитает утренние рейсы, даже если они дороже.", "confidence": 0.4, "support": 1}]}
"""
)

ALLOWED_KINDS = {
    "style": ("style", "boundary", "preference"),
    "notes": KINDS,
    "decisions": ("decision_rule", "preference"),
}
DEFAULT_CATEGORY = {
    "style": "communication/style",
    "notes": None,
    "decisions": "work/decisions",
}


# (timestamp, prompt segment, record type, batch indices of the records in the segment)
Item = tuple[datetime | None, str, str, tuple[int, ...]]


@dataclass
class Candidate:
    kind: str
    category: str
    text: str
    audiences: str | list[str]
    confidence: float
    sources: dict[str, int]
    period: str | None
    proposed_category: str | None = None

    def as_item(self) -> dict[str, Any]:
        evidence: dict[str, Any] = {"sources": dict(self.sources)}
        if self.period:
            evidence["period"] = self.period
        item: dict[str, Any] = {
            "kind": self.kind,
            "category": self.category,
            "text": self.text,
            "audiences": self.audiences,
            "confidence": round(self.confidence, 2),
            "evidence": evidence,
        }
        if self.proposed_category:
            item["proposed_category"] = self.proposed_category
        return item


@dataclass
class Call:
    purpose: str
    system: str
    user: str
    schema: dict[str, Any]
    audiences: str | list[str]
    sources: dict[str, int]
    period: str | None
    records: frozenset[int] = frozenset()  # batch indices covered by this call

    @property
    def estimated_tokens(self) -> int:
        chars = len(self.system) + len(self.user)
        return -(-chars // CHARS_PER_TOKEN) + OUTPUT_TOKENS.get(self.purpose, 1000)


@dataclass
class DeriveCounts:
    dropped_by_guard: int = 0
    invalid: int = 0
    failed_calls: int = 0
    deduplicated: int = 0


def _month_period(stamps: Iterable[datetime | None]) -> str | None:
    months = sorted(s.strftime("%Y-%m") for s in stamps if s is not None)
    return f"{months[0]}..{months[-1]}" if months else None


def _one_line(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def build_calls(batch: Batch, categories: dict[str, str]) -> list[Call]:
    """All LLM calls for a batch, in order: style per audience, notes, decisions."""
    cat_list = "\n".join(
        f"- {path}: {desc}" if desc else f"- {path}" for path, desc in categories.items()
    )
    calls: list[Call] = []

    # Style: owner messages with the preceding non-owner message, and owner call turns.
    per_audience: dict[str, list[Item]] = defaultdict(list)
    threads: dict[tuple[str, str], list] = defaultdict(list)
    for m in batch.messages:
        threads[(m.audience, m.thread)].append(m)
    for (audience, _), msgs in threads.items():
        msgs.sort(key=lambda m: (m.ts, m.position if m.position is not None else 0))
        context: Message | None = None
        waiting: list[int] = []  # all non-owner messages since the last owner message
        for m in msgs:
            if not m.from_owner:
                context = m
                waiting.append(m.index)
                continue
            seg = (
                f"OTHER: {_one_line(context.text)}\n" if context else ""
            ) + f"OWNER: {_one_line(m.text)}"
            # The whole run of waiting messages shares the reply's fate (processed or
            # remaining): the reply-latency detector pairs the first of them with it.
            per_audience[audience].append((m.ts, seg, "message", (m.index, *waiting)))
            context, waiting = None, []
    for t in batch.transcripts:
        context = None
        for speaker, text in t.turns:
            if speaker != "owner":
                context = text
                continue
            seg = (
                f"OTHER: {_one_line(context)}\n" if context else ""
            ) + f"OWNER: {_one_line(text)}"
            per_audience[t.audience].append((t.ts, seg, "call_transcript", (t.index,)))
            context = None
    system = STYLE_SYSTEM.replace("{categories}", cat_list)
    for audience in sorted(per_audience):
        items = sorted(per_audience[audience], key=lambda x: x[0])
        for chunk in _chunked_items(items):
            sources: dict[str, int] = defaultdict(int)
            for _, _, kind, _ in chunk:
                sources[kind] += 1
            calls.append(
                Call(
                    "style",
                    system,
                    f"Audience: {audience}\n" + "\n".join(seg for _, seg, _, _ in chunk),
                    STYLE_SCHEMA,
                    [audience],
                    dict(sources),
                    _month_period(it[0] for it in chunk),
                    _indices(chunk),
                )
            )

    if batch.notes:
        system = NOTES_SYSTEM.replace("{categories}", cat_list)
        items = [(n.ts, f"NOTE: {_one_line(n.text)}", "note", (n.index,)) for n in batch.notes]
        for chunk in _chunked_items(items):
            calls.append(
                Call(
                    "notes",
                    system,
                    "\n".join(seg for _, seg, _, _ in chunk),
                    NOTES_SCHEMA,
                    "all",
                    {"note": len(chunk)},
                    _month_period(it[0] for it in chunk),
                    _indices(chunk),
                )
            )

    by_audience: dict[str | None, list[Decision]] = defaultdict(list)
    for d in batch.decisions:
        by_audience[d.audience].append(d)
    system = DECISIONS_SYSTEM.replace("{categories}", cat_list)
    for audience in sorted(by_audience, key=lambda a: a or ""):
        items = [
            (
                d.ts,
                f"SITUATION: {_one_line(d.situation)} CHOICE: {_one_line(d.choice)}",
                "decision",
                (d.index,),
            )
            for d in by_audience[audience]
        ]
        for chunk in _chunked_items(items):
            lines = [f"{i}. {it[1]}" for i, it in enumerate(chunk, start=1)]
            calls.append(
                Call(
                    "decisions",
                    system,
                    (f"Audience: {audience}\n" if audience else "") + "\n".join(lines),
                    DECISIONS_SCHEMA,
                    [audience] if audience else "all",
                    {"decision": len(chunk)},
                    _month_period(it[0] for it in chunk),
                    _indices(chunk),
                )
            )
    return calls


def _indices(chunk: list[Item]) -> frozenset[int]:
    return frozenset(i for it in chunk for i in it[3])


def _chunked_items(items: list[Item]) -> list[list[Item]]:
    out: list[list[Item]] = []
    current: list[Item] = []
    size = 0
    for ts, seg, kind, indices in items:
        seg = seg[:CHUNK_CHARS]
        if current and size + len(seg) + 1 > CHUNK_CHARS:
            out.append(current)
            current, size = [], 0
        current.append((ts, seg, kind, indices))
        size += len(seg) + 1
    if current:
        out.append(current)
    return out


def _resolve_category(
    raw_category: Any, raw_proposed: Any, purpose: str, categories: dict[str, str]
) -> tuple[str, str | None] | None:
    proposed: str | None = None
    for candidate in (raw_proposed, raw_category):
        if isinstance(candidate, str) and candidate and candidate not in categories:
            try:
                proposed = check_category_path(candidate.strip().lower())
                break
            except Invalid:
                continue
    if isinstance(raw_category, str) and raw_category in categories:
        return raw_category, proposed
    current = proposed
    while current is not None:
        if current in categories:
            return current, (proposed if current != proposed else None)
        current = parent_path(current)
    default = DEFAULT_CATEGORY[purpose]
    if default is not None and default in categories:
        return default, proposed
    return None


def to_candidates(
    call: Call,
    data: Any,
    categories: dict[str, str],
    guard: BatchGuard,
    counts: DeriveCounts,
) -> list[Candidate]:
    if not isinstance(data, dict) or not isinstance(data.get("claims"), list):
        counts.failed_calls += 1
        return []
    out: list[Candidate] = []
    for item in data["claims"][:MAX_CLAIMS_PER_CALL]:
        if not isinstance(item, dict):
            counts.invalid += 1
            continue
        text = item.get("text")
        kind = item.get("kind")
        if not isinstance(text, str) or not text.strip() or len(text.strip()) > MAX_TEXT:
            counts.invalid += 1
            continue
        text = text.strip()
        if guard.check(text) is not None:
            counts.dropped_by_guard += 1
            continue
        confidence = item.get("confidence")
        if not isinstance(confidence, int | float) or isinstance(confidence, bool):
            confidence = 0.5
        confidence = min(1.0, max(0.0, float(confidence)))
        if call.purpose == "decisions":
            support = item.get("support")
            support = support if isinstance(support, int) and not isinstance(support, bool) else 1
            if support >= 2 and kind == "decision_rule":
                confidence = max(min(confidence, 0.8), RULE_CONFIDENCE)
            else:
                kind = "preference"
                confidence = min(confidence, SINGLE_DECISION_CONFIDENCE)
        if kind not in ALLOWED_KINDS[call.purpose]:
            counts.invalid += 1
            continue
        resolved = _resolve_category(
            item.get("category"), item.get("proposed_category"), call.purpose, categories
        )
        if resolved is None:
            counts.invalid += 1
            continue
        category, proposed = resolved
        if proposed is not None and guard.check(proposed) is not None:
            proposed = None
        out.append(
            Candidate(
                kind=kind,
                category=category,
                text=text,
                audiences=call.audiences,
                confidence=confidence,
                sources=dict(call.sources),
                period=call.period,
                proposed_category=proposed,
            )
        )
    return out


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"\w+", normalize_text(text)))


def _widen(a: str | None, b: str | None) -> str | None:
    if not a or not b:
        return a or b
    a0, a1 = a.split("..")
    b0, b1 = b.split("..")
    return f"{min(a0, b0)}..{max(a1, b1)}"


def dedupe(candidates: list[Candidate], counts: DeriveCounts) -> list[Candidate]:
    """Merge candidates from different chunks that say the same thing (same kind, category
    and audiences; equal normalised text or token-set Jaccard >= 0.6)."""
    kept: list[tuple[Candidate, set[str]]] = []
    for cand in candidates:
        tokens = _tokens(cand.text)
        for other, other_tokens in kept:
            if (other.kind, other.category, json.dumps(other.audiences)) != (
                cand.kind,
                cand.category,
                json.dumps(cand.audiences),
            ):
                continue
            union = tokens | other_tokens
            same = normalize_text(other.text) == normalize_text(cand.text)
            if same or (union and len(tokens & other_tokens) / len(union) >= DEDUPE_JACCARD):
                for name, n in cand.sources.items():
                    other.sources[name] = other.sources.get(name, 0) + n
                other.period = _widen(other.period, cand.period)
                other.confidence = max(other.confidence, cand.confidence)
                other.proposed_category = other.proposed_category or cand.proposed_category
                counts.deduplicated += 1
                break
        else:
            kept.append((cand, tokens))
    return [c for c, _ in kept]


def same_claim(a: Candidate, b: Candidate) -> bool:
    """The within-job duplicate test of :func:`dedupe` (kind, category, audiences, text)."""
    if (a.kind, a.category, json.dumps(a.audiences)) != (
        b.kind,
        b.category,
        json.dumps(b.audiences),
    ):
        return False
    if normalize_text(a.text) == normalize_text(b.text):
        return True
    ta, tb = _tokens(a.text), _tokens(b.text)
    union = ta | tb
    return bool(union) and len(ta & tb) / len(union) >= DEDUPE_JACCARD
