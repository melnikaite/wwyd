# SPDX-License-Identifier: AGPL-3.0-or-later
"""Analyzer authoring and the hypothesis loop (SPEC §7.4 steps 1–4, D20).

**Trigger:** a category with at least ``analyzer_min_episodes`` processed episodes *of its
own* in the job (exact category, so a parent does not re-trigger on its children's
episodes), at most one round per category per job, and room under ``max_analyzers``
(non-retired LLM analyzers per persona). The analyzer is bound to the category's subtree.

**Authoring:** one LLM call sees a host-computed profile of the subtree's processed episodes
(aggregates only), at most 20 truncated samples, the helper API, the statements of the
category's existing LLM analyzers (retired ones included) and its claims, and returns at
most 5 hypotheses ``{statement_template, kind, source}``. Every statement and source passes
the verbatim guard and the concentrated-word check before anything is stored; a dropped one
is not stored at all. A statement that restates an existing LLM analyzer of the category
(any status, equal normalised text or token Jaccard >= 0.6, placeholders removed) is dropped
by code too (``analyzers_duplicate``).

**Test and decision:** :mod:`wwyd.analyzers.hypothesis`. Accepted -> ``active`` (it then
runs in this job's analyzer step, which writes its state and its ``anl:<id>`` claim);
rejected -> ``retired`` with the statement only (negative memory).

Budget: the authoring call covers no episodes, so when it does not fit the token budget or
the backend fails, the round is skipped (``skipped_llm_budget`` / ``authoring_failed``) and
the job is not made ``partial`` (there is nothing to resend).
"""

from __future__ import annotations

import builtins
import io
import json
import keyword
import re
import sqlite3
import statistics
import tokenize
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from wwyd.analyzers import hypothesis
from wwyd.analyzers.builtin.base import WEEKDAY_KEYS, Item
from wwyd.analyzers.contract import ContractError
from wwyd.analyzers.llm import (
    PLACEHOLDERS,
    SandboxRunner,
    in_subtree,
    keep_metrics,
    payload,
    template_ok,
)
from wwyd.db import transaction
from wwyd.errors import LLMUnavailable, RateLimited, SpendCap
from wwyd.ingest.guard import BatchGuard
from wwyd.ingest.rare import RareWords
from wwyd.store import analyzers as store
from wwyd.store.common import KINDS, normalize_text

MAX_HYPOTHESES = 5
MAX_SAMPLES = 20
SAMPLE_CHARS = 200
MAX_SOURCE_BYTES = 20 * 1024
MAX_TEMPLATE = 300
DUPLICATE_JACCARD = 0.6
CHARS_PER_TOKEN = 3
OUTPUT_TOKENS = 4000

AUTHOR_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["hypotheses"],
    "properties": {
        "hypotheses": {
            "type": "array",
            "maxItems": MAX_HYPOTHESES,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["statement_template", "kind", "source"],
                "properties": {
                    "statement_template": {"type": "string"},
                    "kind": {"type": "string", "enum": list(KINDS)},
                    "source": {"type": "string"},
                },
            },
        }
    },
}

HELPER_API = """Input: stdin holds JSON {"episodes": [...]}; each episode has
local_start (ISO datetime or null), weekday (0 = Monday), hour, month ("YYYY-MM" or null),
duration_s (or null; the span of when), latency_s (or null; when minus situation_at),
labels ({"decision": "accepted|declined|other", "situation": ...}),
tags (list), category, audience, where, situation, decision, said (strings or null).
Output: print ONE JSON object {"n": {month: {"_": count}}, "hits": {month: {"_": count}}}
and optionally "base_n" / "base_hits" (the base rate to beat). n = episodes the hypothesis
applies to, hits = those where it holds. Skip episodes whose month is null.
Module wwyd_helpers (import it): words(text) -> lowercase words; sentences(text);
emojis(text); ending(text) -> one of period/exclamation/question/ellipsis/smiley/emoji/
other/none; count_words(texts); count_bigrams(texts); month_key(iso) -> "YYYY-MM";
bucket(value, edges); histogram(values, edges); add_counts(acc, month, metric, key, n=1).
Standard library only (json, sys, re, collections, math). No files, network or decorators."""

AUTHOR_SYSTEM = (
    """You propose testable hypotheses about how one person (the owner) acts in one area of
life, and write a small Python 3 analyzer for each that counts how often it holds.
Return JSON {"hypotheses": [...]} with at most 5 items, each:
- statement_template: one sentence about the owner with placeholders {share} (e.g. "87%"),
  {n} (episodes counted), {hits}, {months}; e.g. "Answers {share} of invitations to
  ring the bells within a day ({n} invitations)." Describe a rule-like regularity (it should hold in at least 75%).
  State what the owner does, not a label or identity ("Does not drink alcohol.", not
  "Teetotaler.").
- kind: habit, decision_rule, preference, style, trait or boundary. boundary only for a
  topic the owner does not let anyone answer for them at all; a rule for declining
  something under a condition is a decision_rule.
- source: the analyzer, a complete script (see the API below).
Rules: never mention people's names, companies, places, links, emails or phone numbers,
neither in statements nor in code; never copy wording from the samples; do not repeat or
rephrase the existing analyzers listed in the input (including retired ones).
"""
    + HELPER_API
)

_TEXT_FIELDS = ("situation", "decision", "said", "where")
_ALLOWED_NAMES = {
    *keyword.kwlist,
    *(n.casefold() for n in dir(builtins)),
    "json",
    "sys",
    "re",
    "math",
    "collections",
    "stdin",
    "stdout",
    "loads",
    "dumps",
    "load",
    "dump",
    "read",
    "write",
    "get",
    "items",
    "keys",
    "values",
    "append",
    "setdefault",
    "lower",
    "upper",
    "strip",
    "split",
    "startswith",
    "endswith",
    "counter",
    "defaultdict",
    "wwyd",
    "helpers",
    "episodes",
    "episode",
    "local",
    "start",
    "weekday",
    "hour",
    "month",
    "duration",
    "labels",
    "tags",
    "category",
    "audience",
    "where",
    "situation",
    "decision",
    "said",
    "base",
    "hits",
    "acc",
    "out",
    "result",
    "main",
    "accepted",
    "declined",
    "other",
    "words",
    "sentences",
    "emojis",
    "ending",
    "count",
    "bigrams",
    "key",
    "bucket",
    "histogram",
    "add",
    "counts",
}


@dataclass
class Hypothesis:
    statement_template: str
    kind: str
    source: str


@dataclass
class RoundContext:
    conn: sqlite3.Connection
    persona_id: str
    user_id: str
    counts: dict[str, Any]
    guard: BatchGuard
    rare: RareWords
    llm: Any
    limiter: Any
    runner: SandboxRunner


def _bump(counts: dict[str, Any], name: str, n: int = 1) -> None:
    counts[name] = counts.get(name, 0) + n


def triggered(items: Sequence[Item], min_episodes: int) -> list[str]:
    """Category IDs with enough processed episodes of their own in the job."""
    per = Counter(it.episode.category_id for it in items if it.episode.category_id)
    return sorted(c for c, n in per.items() if n >= min_episodes)


# --- the profile and the prompt -------------------------------------------------------


def _length_stats(values: list[int]) -> dict[str, int]:
    if not values:
        return {}
    return {"min": min(values), "median": int(statistics.median(values)), "max": max(values)}


def profile(items: Sequence[Item], path: str) -> dict[str, Any]:
    """Aggregates only: counts, fill rates, distinct counts, label/tag distributions, time
    histograms and length stats (no text)."""
    n = len(items)
    fields: dict[str, Any] = {}
    for name in _TEXT_FIELDS:
        vals = [getattr(it.episode, name) for it in items if getattr(it.episode, name)]
        fields[name] = {
            "filled": round(len(vals) / n, 2) if n else 0.0,
            "distinct": len({normalize_text(v) for v in vals}),
            "chars": _length_stats([len(v) for v in vals]),
        }
    labels: dict[str, Counter[str]] = defaultdict(Counter)
    tags: Counter[str] = Counter()
    for it in items:
        for k, v in it.episode.labels.items():
            labels[k][v] += 1
        tags.update(it.episode.tags)
    weekday = [0] * 7
    hours = [0] * 24
    months: Counter[str] = Counter()
    for it in items:
        if it.weekday is not None and it.hour is not None and it.month is not None:
            weekday[it.weekday] += 1
            hours[it.hour] += 1
            months[it.month] += 1
    durations = [int(it.duration_s / 60) for it in items if it.duration_s is not None]
    audiences = Counter(it.episode.audience or "(none)" for it in items)
    return {
        "category": path,
        "episodes": n,
        "timed": sum(months.values()),
        "months": dict(sorted(months.items())),
        "weekday": dict(zip(WEEKDAY_KEYS, weekday, strict=True)),
        "hour": hours,
        "duration_minutes": _length_stats(durations),
        "fields": fields,
        "labels": {k: dict(v.most_common(30)) for k, v in labels.items()},
        "tags": dict(tags.most_common(30)),
        "audiences": dict(audiences.most_common(20)),
    }


def samples(items: Sequence[Item]) -> list[dict[str, Any]]:
    """At most 20 episodes, evenly spread over the subtree, text fields truncated."""
    if not items:
        return []
    step = max(1, len(items) // MAX_SAMPLES)
    out = []
    for it in list(items)[::step][:MAX_SAMPLES]:
        ep = it.episode
        row: dict[str, Any] = {
            "local_start": it.local_start.isoformat() if it.local_start else None,
            "duration_s": it.duration_s,
            "latency_s": it.latency_s,
            "category": ep.category,
            "audience": ep.audience,
            "labels": dict(ep.labels),
            "tags": list(ep.tags),
        }
        for name in _TEXT_FIELDS:
            value = getattr(ep, name)
            row[name] = " ".join(value.split())[:SAMPLE_CHARS] if value else None
        out.append(row)
    return out


def _existing(conn: sqlite3.Connection, persona_id: str, category_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT statement_template, status FROM analyzers WHERE persona_id = ? AND origin = 'llm'"
        " AND scope_kind = 'category' AND scope_id = ? ORDER BY seq",
        (persona_id, category_id),
    ).fetchall()


def build_prompt(
    conn: sqlite3.Connection, persona_id: str, category_id: str, path: str, items: Sequence[Item]
) -> str:
    existing = _existing(conn, persona_id, category_id)
    claims = [
        r["text"]
        for r in conn.execute(
            "SELECT text FROM claims WHERE persona_id = ? AND category_id = ?"
            " AND status IN ('pending', 'approved') ORDER BY created_at LIMIT 30",
            (persona_id, category_id),
        )
    ]
    parts = [
        "PROFILE:",
        json.dumps(profile(items, path), ensure_ascii=False),
        "SAMPLES:",
        *(json.dumps(s, ensure_ascii=False) for s in samples(items)),
        "EXISTING ANALYZERS (do not repeat):",
        *(f"- [{r['status']}] {r['statement_template']}" for r in existing),
        "CLAIMS ALREADY KNOWN:",
        *(f"- {t}" for t in claims),
    ]
    return "\n".join(parts)


# --- checks ---------------------------------------------------------------------------


def _norm_statement(template: str) -> set[str]:
    text = re.sub(r"\{[^{}]*\}", " ", template)
    return set(re.findall(r"\w+", normalize_text(text)))


def is_duplicate(template: str, others: Sequence[str]) -> bool:
    mine = _norm_statement(template)
    for other in others:
        theirs = _norm_statement(other)
        union = mine | theirs
        if mine == theirs or (union and len(mine & theirs) / len(union) >= DUPLICATE_JACCARD):
            return True
    return False


def source_texts(source: str) -> tuple[list[str], list[str]] | None:
    """(string literals and comments, identifier parts) of ``source``; None if it does not
    tokenize."""
    texts: list[str] = []
    names: list[str] = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type in (tokenize.STRING, tokenize.COMMENT):
                texts.append(tok.string)
            elif tok.type == tokenize.NAME:
                names.extend(p for p in tok.string.split("_") if p)
            elif tok.type == getattr(tokenize, "FSTRING_MIDDLE", -1):
                texts.append(tok.string)
    except (tokenize.TokenError, SyntaxError, IndentationError):
        return None
    return texts, names


def check_hypothesis(h: Hypothesis, guard: BatchGuard, rare: RareWords) -> str | None:
    """Why a hypothesis must be dropped (``guard``, ``invalid``), or None."""
    if not template_ok(h.statement_template):
        return "invalid"
    if len(h.source.encode()) > MAX_SOURCE_BYTES or not h.source.strip():
        return "invalid"
    parts = source_texts(h.source)
    if parts is None:
        return "invalid"
    texts, names = parts
    if guard.check(h.statement_template) is not None or rare.find(
        h.statement_template, ignore=PLACEHOLDERS
    ):
        return "guard"
    if guard.check(h.source) is not None or guard.check(" ".join(texts + names)):
        return "guard"
    if any(rare.find(t) for t in texts):
        return "guard"
    if rare.find(" ".join(n for n in names if n.casefold() not in _ALLOWED_NAMES)):
        return "guard"
    return None


def parse(data: Any, counts: dict[str, Any]) -> list[Hypothesis]:
    items = data.get("hypotheses") if isinstance(data, dict) else None
    if not isinstance(items, list):
        _bump(counts, "llm_calls_failed")
        return []
    out = []
    for item in items[:MAX_HYPOTHESES]:
        if not isinstance(item, dict):
            _bump(counts, "dropped_invalid")
            continue
        tpl, kind, src = item.get("statement_template"), item.get("kind"), item.get("source")
        if (
            not isinstance(tpl, str)
            or not tpl.strip()
            or len(tpl) > MAX_TEMPLATE
            or kind not in KINDS
            or not isinstance(src, str)
        ):
            _bump(counts, "dropped_invalid")
            continue
        out.append(Hypothesis(" ".join(tpl.split()), kind, src))
    return out


# --- the round ------------------------------------------------------------------------


async def _test(ctx: RoundContext, h: Hypothesis, train: list[Item], hold: list[Item]):  # noqa: ANN202
    """``hypothesis.Outcome``, or None when the sandbox budget ran out."""
    totals = []
    for part in (train, hold):
        result = await ctx.runner.run(h.source, payload(part))
        if result is None:
            return None
        if not result.ok:
            _bump(ctx.counts, "sandbox_errors")
            return hypothesis.Outcome(None, None)
        try:
            kept, _ = keep_metrics(result.output)
        except ContractError:
            _bump(ctx.counts, "sandbox_errors")
            return hypothesis.Outcome(None, None)
        totals.append(hypothesis.totals(kept))
    return hypothesis.Outcome(totals[0], totals[1])


async def author_round(
    ctx: RoundContext, category_id: str, path: str, items: Sequence[Item], max_analyzers: Any
) -> None:
    """One authoring round for ``category_id`` over the processed ``items`` (all of the
    persona's processed items of the job; the subtree is selected here)."""
    conn, pid, counts = ctx.conn, ctx.persona_id, ctx.counts
    capacity = MAX_HYPOTHESES
    if max_analyzers is not None:
        capacity = min(capacity, int(max_analyzers) - store.count_llm(conn, pid))
    if capacity <= 0:
        _bump(counts, "analyzers_skipped_limit")
        return
    if not ctx.runner.has_budget():
        _bump(counts, "skipped_sandbox_budget")
        return
    scoped = [it for it in items if in_subtree(it.episode.category, path)]
    user = build_prompt(conn, pid, category_id, path, scoped)
    estimate = -(-(len(AUTHOR_SYSTEM) + len(user)) // CHARS_PER_TOKEN) + OUTPUT_TOKENS
    try:
        ctx.limiter.check_llm_budget(ctx.user_id, pid)
        left = ctx.limiter.remaining_tokens(ctx.user_id, pid)
        if left is not None and estimate > left:
            _bump(counts, "skipped_llm_budget")
            return
        _bump(counts, "llm_calls")
        result = await ctx.llm.complete_json(
            purpose="author_analyzers", system=AUTHOR_SYSTEM, user=user, schema=AUTHOR_SCHEMA
        )
    except (RateLimited, SpendCap):
        _bump(counts, "skipped_llm_budget")
        return
    except LLMUnavailable:
        _bump(counts, "authoring_failed")
        return
    finally:
        del user
    proposed = parse(result.data, counts)
    del result
    others = [r["statement_template"] for r in _existing(conn, pid, category_id)]
    candidates: list[Hypothesis] = []
    for h in proposed:
        _bump(counts, "analyzers_proposed")
        why = check_hypothesis(h, ctx.guard, ctx.rare)
        if why == "guard":
            _bump(counts, "dropped_by_guard")
            continue
        if why is not None:
            _bump(counts, "dropped_invalid")
            continue
        if is_duplicate(h.statement_template, others + [c.statement_template for c in candidates]):
            _bump(counts, "analyzers_duplicate")
            continue
        candidates.append(h)
    candidates = candidates[:capacity]
    if not candidates:
        return
    train, hold = hypothesis.split(scoped)
    outcomes: list[hypothesis.Outcome] = []
    tested: list[Hypothesis] = []
    for i, h in enumerate(candidates):
        outcome = await _test(ctx, h, train, hold)
        if outcome is None:
            _bump(counts, "skipped_sandbox_budget", len(candidates) - i)
            break
        outcomes.append(outcome)
        tested.append(h)
    accepted = hypothesis.decide(outcomes)
    with transaction(conn):
        for h, ok in zip(tested, accepted, strict=True):
            store.insert_llm(
                conn,
                pid,
                category_id=category_id,
                statement_template=h.statement_template,
                kind=h.kind,
                source=h.source,
                status="active" if ok else "retired",
            )
            _bump(counts, "analyzers_accepted" if ok else "analyzers_rejected")
    candidates.clear()
    tested.clear()
