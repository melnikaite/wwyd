# SPDX-License-Identifier: AGPL-3.0-or-later
"""LLM passes over episodes (SPEC §7.6) and the category description call (§4.4).

Builds prompts from the in-memory batch, chunked to about 2500 characters, and turns the
model's JSON into candidate claims: (a) statements -> claims of any kind; (b) replies per
audience in chronological order, ``said`` with its ``situation`` as context; (c) decisions
-> ``decision_rule`` when a pattern repeats, else low-confidence ``preference``. The model
chooses a category within the episode's subtree (the only part of the tree it sees) or
proposes one. Candidates are guarded (§7.7), deduplicated among themselves and returned;
the job stores them through ``store.claims.add_claims``. Nothing here logs or stores prompt
or response text.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from wwyd.errors import Invalid
from wwyd.ingest.episodes import Batch
from wwyd.ingest.guard import BatchGuard
from wwyd.store.categories import normalize_category_path
from wwyd.store.common import KINDS, MAX_TEXT, normalize_text, parent_path

CHUNK_CHARS = 2500
CONTEXT_CHARS = 600  # a reply's situation is context only: kept short
# Token estimate per call (SPEC §7.1): (chars of system + user) / CHARS_PER_TOKEN plus a
# fixed output allowance per purpose. A conservative estimate, not a bound: the job stops
# before a call whose estimate does not fit the remaining daily budget, so the cap can be
# overrun by at most one call's actual excess over its estimate.
CHARS_PER_TOKEN = 3  # conservative for Cyrillic and most non-English text
OUTPUT_TOKENS = {
    "replies": 800,
    "statements": 1000,
    "decisions": 1000,
    "describe_categories": 600,
    "annotate": 1200,
    "author_analyzers": 4000,
    "word_usage": 300,
}
MAX_CLAIMS_PER_CALL = 8
MAX_DESCRIPTION_CHARS = 200
DEDUPE_JACCARD = 0.6
SATURATION_CALLS = 5  # consecutive reply calls of one audience with nothing new (§7.1)
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


REPLIES_SCHEMA = _schema()
STATEMENTS_SCHEMA = _schema()
DECISIONS_SCHEMA = _schema({"support": {"type": "integer"}})
DESCRIBE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["descriptions"],
    "properties": {
        "descriptions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["path", "description"],
                "properties": {"path": {"type": "string"}, "description": {"type": "string"}},
            },
        }
    },
}

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

REPLIES_SYSTEM = (
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

STATEMENTS_SYSTEM = (
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

DESCRIBE_SYSTEM = """You write a one-line description for each new category in the
knowledge base of one person (the owner). A category groups what the owner does and
prefers in one area of life. You see only the category paths (segments joined by "/").
Rules:
- One sentence per path, at most 120 characters, saying what the category holds.
- Never mention people's names, companies, places, links, emails or phone numbers.
- Write the description in the language of the path.
- Return JSON {"descriptions": [{"path": ..., "description": ...}]}, one item per path.

Example:
Input:
- hobbies/films
- кино/сериалы
Answer:
{"descriptions": [{"path": "hobbies/films", "description": "Which films the owner watches and how they rate them."},
{"path": "кино/сериалы", "description": "Какие сериалы владелец смотрит и досматривает ли их."}]}
"""

ALLOWED_KINDS = {
    "replies": ("style", "boundary", "preference"),
    "statements": KINDS,
    "decisions": ("decision_rule", "preference"),
}


# (timestamp, prompt segment, class, batch index, episode category)
Item = tuple[datetime | None, str, str, int, str]


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
    # The category a claim lands in when the model names none it may use (the call's most
    # frequent episode category), and the paths it may choose (the episodes' subtrees).
    default_category: str | None = None
    allowed: frozenset[str] = frozenset()
    # describe_categories: path -> category ID of the categories to describe.
    targets: dict[str, str] = field(default_factory=dict)
    # annotate: what the call labels (an ``annotate.Annotation``; in memory only).
    meta: Any = None

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


def _label_note(ep: Any) -> str:
    """The episode's annotation (SPEC §7.3) for the pass prompts: guarded names only."""
    parts = [f"{k}={v}" for k, v in sorted(ep.labels.items())]
    if ep.tags:
        parts.append("tags=" + ",".join(ep.tags))
    return f" [labels: {'; '.join(parts)}]" if parts else ""


def subtree(categories: dict[str, str], roots: Iterable[str]) -> frozenset[str]:
    """Every path in ``categories`` that is one of ``roots`` or lies below one."""
    roots = set(roots)
    return frozenset(
        p for p in categories if p in roots or any(p.startswith(r + "/") for r in roots)
    )


def _category_list(categories: dict[str, str], allowed: Iterable[str]) -> str:
    return "\n".join(
        f"- {path}: {categories[path]}" if categories.get(path) else f"- {path}"
        for path in sorted(allowed)
    )


def describe_call(created: list[dict[str, str]]) -> Call | None:
    """The one batched description call for the categories a job created (covers no
    episodes)."""
    if not created:
        return None
    targets = {c["path"]: c["id"] for c in created}
    return Call(
        "describe_categories",
        DESCRIBE_SYSTEM,
        "\n".join(f"- {p}" for p in targets),
        DESCRIBE_SCHEMA,
        "all",
        {},
        None,
        targets=targets,
    )


def build_calls(
    batch: Batch,
    categories: dict[str, str],
    *,
    default_audience: str,
    decision_min_episodes: int,
) -> list[Call]:
    """All pass calls for a batch, in order: statements, replies per audience, decisions.

    Decisions only for categories with fewer than ``decision_min_episodes`` episodes in the
    batch; larger ones are left to annotation and analyzers (SPEC §7.6)."""
    calls: list[Call] = []

    def emit(
        purpose: str,
        system: str,
        schema: dict[str, Any],
        items: list[Item],
        header: str,
        audiences: str | list[str],
        *,
        numbered: bool = False,
    ) -> None:
        for chunk in _chunked_items(items):
            cats = Counter(it[4] for it in chunk)
            allowed = subtree(categories, cats)
            segs = [it[1] for it in chunk]
            if numbered:
                segs = [f"{i}. {seg}" for i, seg in enumerate(segs, start=1)]
            calls.append(
                Call(
                    purpose,
                    system.replace("{categories}", _category_list(categories, allowed)),
                    header + "\n".join(segs),
                    schema,
                    audiences,
                    {purpose_class[purpose]: len(chunk)},
                    _month_period(it[0] for it in chunk),
                    frozenset(it[3] for it in chunk),
                    default_category=cats.most_common(1)[0][0],
                    allowed=allowed,
                )
            )

    purpose_class = {"statements": "statement", "replies": "reply", "decisions": "decision"}

    # (a) Statements, grouped by audience (None: about everyone) and category.
    groups: dict[tuple[str, str], list[Item]] = defaultdict(list)
    for ep in batch.episodes:
        if ep.is_statement:
            seg = f"NOTE: {_one_line(ep.said or '')}{_label_note(ep)}"
            groups[(ep.audience or "", ep.category)].append(
                (ep.start, seg, "statement", ep.index, ep.category)
            )
    for (audience, _), items in sorted(groups.items()):
        emit(
            "statements",
            STATEMENTS_SYSTEM,
            STATEMENTS_SCHEMA,
            _chronological(items),
            f"Audience: {audience}\n" if audience else "",
            [audience] if audience else "all",
        )

    # (b) Replies per audience in chronological order (the default audience for style).
    replies: dict[str, list[Item]] = defaultdict(list)
    for ep in batch.episodes:
        if ep.is_reply:
            context = _one_line(ep.situation or "")[:CONTEXT_CHARS]
            seg = f"OTHER: {context}\nOWNER: {_one_line(ep.said or '')}{_label_note(ep)}"
            replies[ep.audience or default_audience].append(
                (ep.start, seg, "reply", ep.index, ep.category)
            )
    for audience in sorted(replies):
        emit(
            "replies",
            REPLIES_SYSTEM,
            REPLIES_SCHEMA,
            _chronological(replies[audience]),
            f"Audience: {audience}\n",
            [audience],
        )

    # (c) Decisions, per audience and category, only for small categories.
    per_category = Counter(ep.category for ep in batch.episodes)
    decisions: dict[tuple[str, str], list[Item]] = defaultdict(list)
    for ep in batch.episodes:
        if ep.decision is None or per_category[ep.category] >= decision_min_episodes:
            continue
        where = f" ({_one_line(ep.where)})" if ep.where else ""
        situation = f"SITUATION{where}: {_one_line(ep.situation)} " if ep.situation else ""
        seg = f"{situation}CHOICE: {_one_line(ep.decision)}{_label_note(ep)}"
        decisions[(ep.audience or "", ep.category)].append(
            (ep.start, seg, "decision", ep.index, ep.category)
        )
    for (audience, category), items in sorted(decisions.items()):
        emit(
            "decisions",
            DECISIONS_SYSTEM,
            DECISIONS_SCHEMA,
            _chronological(items),
            (f"Audience: {audience}\n" if audience else "") + f"Category: {category}\n",
            [audience] if audience else "all",
            numbered=True,
        )
    return calls


def _chronological(items: list[Item]) -> list[Item]:
    """By timestamp (untimed ones keep batch order, after the timed ones)."""
    return sorted(items, key=lambda it: (it[0] is None, it[0] or datetime.min, it[3]))


def _chunked_items(items: list[Item]) -> list[list[Item]]:
    out: list[list[Item]] = []
    current: list[Item] = []
    size = 0
    for ts, seg, kind, index, category in items:
        seg = seg[:CHUNK_CHARS]
        if current and size + len(seg) + 1 > CHUNK_CHARS:
            out.append(current)
            current, size = [], 0
        current.append((ts, seg, kind, index, category))
        size += len(seg) + 1
    if current:
        out.append(current)
    return out


def _normalized(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return normalize_category_path(value.strip())
    except Invalid:
        return None


def _resolve_category(
    raw_category: Any, raw_proposed: Any, call: Call
) -> tuple[str, str | None] | None:
    """A path the call may use (within its episodes' subtrees) and an optional proposal.

    A path outside the allowed subtrees becomes ``proposed_category``; the claim is stored
    under its closest allowed ancestor, else under the call's default category."""
    allowed = call.allowed
    category = _normalized(raw_category)
    proposed = next(
        (p for p in (_normalized(raw_proposed), category) if p is not None and p not in allowed),
        None,
    )
    if category is not None and category in allowed:
        return category, proposed
    current = proposed
    while current is not None:
        if current in allowed:
            return current, (proposed if current != proposed else None)
        current = parent_path(current)
    if call.default_category is not None:
        return call.default_category, proposed
    return None


def to_candidates(
    call: Call,
    data: Any,
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
        resolved = _resolve_category(item.get("category"), item.get("proposed_category"), call)
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


def to_descriptions(
    call: Call, data: Any, guard: BatchGuard, counts: DeriveCounts
) -> dict[str, str]:
    """category ID -> guarded one-line description from a describe_categories reply."""
    if not isinstance(data, dict) or not isinstance(data.get("descriptions"), list):
        counts.failed_calls += 1
        return {}
    out: dict[str, str] = {}
    for item in data["descriptions"]:
        if not isinstance(item, dict):
            counts.invalid += 1
            continue
        path = _normalized(item.get("path"))
        text = item.get("description")
        if path not in call.targets or not isinstance(text, str):
            counts.invalid += 1
            continue
        text = _one_line(text)
        if not text or len(text) > MAX_DESCRIPTION_CHARS:
            counts.invalid += 1
            continue
        if guard.check(text) is not None:
            counts.dropped_by_guard += 1
            continue
        out[call.targets[path]] = text
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


def guard_for(batch: Batch, vocabulary: set[str]) -> BatchGuard:
    """The batch guard (§7.7): ``said`` is the only owner text; ``situation``,
    ``decision`` and ``where`` are non-owner text (5-gram check). Part 3 scans both."""
    other: list[str] = []
    owner: list[str] = []
    for ep in batch.episodes:
        other.extend(t for t in (ep.situation, ep.decision, ep.where) if t)
        if ep.said:
            owner.append(ep.said)
    return BatchGuard(other_texts=other, all_texts=other + owner, vocabulary=vocabulary)
