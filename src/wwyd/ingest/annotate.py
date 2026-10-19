# SPDX-License-Identifier: AGPL-3.0-or-later
"""Annotation (SPEC §7.3, D20): the LLM labels, code counts.

Per category per job, before the LLM passes:

* **Few distinct values.** When ``decision`` has at most 50 distinct normalised values (or
  ``situation`` at most 200), one call maps the distinct values to labels: decision ->
  ``accepted`` / ``declined`` / ``other``; situation -> a short type (``1-1``, ``standup``).
* **Free text** (more distinct values): batched tagging, at most 20 episodes per call and
  5 tags per episode, reusing the category's tag vocabulary.

**When a category is annotated** (the cheap rule): only when its labels will be counted by
code, i.e. the category has at least ``analyzer_min_episodes`` episodes in the job (an
authoring round can run on it, §7.4) or an active LLM analyzer is bound to it or to one of
its ancestors (the analyzer reads ``labels`` / ``tags``). Small increments of categories
without LLM analyzers cost no annotation tokens; their decisions still go to the decision
pass, which reads the decision text itself (§7.6).

Label and tag *names* are normalised to one path segment (lowercase, ``[\\w-]``, at most
40 chars), checked by the verbatim guard and the concentrated-word check
(:mod:`wwyd.ingest.rare`) unless already in the category's vocabulary, and stored in
``category_labels`` (at most :data:`MAX_LABELS` per category and field). The value -> label
mappings and the per-episode labels live only in memory (``Episode.labels`` / ``tags``).
"""

from __future__ import annotations

import re
import sqlite3
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from wwyd.ingest.episodes import Batch, Episode
from wwyd.ingest.guard import BatchGuard
from wwyd.ingest.rare import RareWords
from wwyd.store.common import normalize_text

DECISION_LABELS = ("accepted", "declined", "other")
MAX_DECISION_DISTINCT = 50
MAX_SITUATION_DISTINCT = 200
TAG_BATCH = 20
MAX_TAGS = 5
MAX_LABELS = 60  # per category and field
MAX_LABEL_CHARS = 40
VALUE_CHARS = 160
TAG_TEXT_CHARS = 400
FIELDS = ("decision", "situation", "tag")

_LABEL_JUNK = re.compile(r"[^\w-]+", re.UNICODE)
_DASHES = re.compile(r"-{2,}")

MAP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["labels"],
    "properties": {
        "labels": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["i", "label"],
                "properties": {"i": {"type": "integer"}, "label": {"type": "string"}},
            },
        }
    },
}
TAG_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["tags"],
    "properties": {
        "tags": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["i", "tags"],
                "properties": {
                    "i": {"type": "integer"},
                    "tags": {"type": "array", "maxItems": MAX_TAGS, "items": {"type": "string"}},
                },
            },
        }
    },
}

_RULES = """Rules:
- Labels are generic: lowercase, one or two words joined by "-", at most 30 characters.
- Never use people's names, companies, places, links, emails, phone numbers or account
  names in a label, and never copy wording from the input.
"""

DECISION_SYSTEM = (
    """You label the owner's decisions. Each numbered line is one distinct way a choice was
described. Map every line to exactly one label: "accepted" (agreed, went, bought, watched,
attended), "declined" (refused, cancelled, skipped, did not go) or "other".
Return JSON {"labels": [{"i": 1, "label": "accepted"}, ...]}, one item per line.
"""
    + _RULES
)

SITUATION_SYSTEM = (
    """You classify situations the owner was in. Each numbered line is one distinct
situation of the category "{category}". Map every line to a short type label such as
"1-1", "standup", "interview", "invitation". Use few labels (at most 20 in total), and
reuse these existing labels where they fit: {vocabulary}.
Return JSON {"labels": [{"i": 1, "label": "standup"}, ...]}, one item per line.
"""
    + _RULES
)

TAG_SYSTEM = (
    """You tag episodes of the category "{category}" with topics. For each numbered episode
give up to 5 short topic tags (for films: genres, mood; for purchases: kind of item).
Reuse these existing tags where they fit: {vocabulary}.
Return JSON {"tags": [{"i": 1, "tags": ["drama", "slow"]}, ...]}, one item per episode.
"""
    + _RULES
)


@dataclass
class Annotation:
    """What one annotate call labels (in memory only)."""

    category_id: str
    field: str  # decision | situation | tag
    values: list[str] = field(default_factory=list)  # mapping: distinct normalised values
    episodes: list[Episode] = field(default_factory=list)  # every episode the call labels


@dataclass
class AnnotateCounts:
    labels_created: int = 0
    labels_dropped: int = 0  # over the per-category limit
    dropped_by_guard: int = 0
    invalid: int = 0
    failed_calls: int = 0


def normalize_value(text: str) -> str:
    return normalize_text(text)


def normalize_label(raw: Any) -> str | None:
    """One path segment: lowercase, spaces/underscores/slashes/dots -> "-", ``[\\w-]`` only."""
    if not isinstance(raw, str):
        return None
    text = raw.strip().casefold().replace("_", "-")
    text = _LABEL_JUNK.sub("-", text)
    text = _DASHES.sub("-", text).strip("-")
    if not text or len(text) > MAX_LABEL_CHARS:
        return None
    return text


def load_vocabulary(conn: sqlite3.Connection, persona_id: str) -> dict[tuple[str, str], set[str]]:
    out: dict[tuple[str, str], set[str]] = defaultdict(set)
    for r in conn.execute(
        "SELECT category_id, field, label FROM category_labels WHERE persona_id = ?",
        (persona_id,),
    ):
        out[(r["category_id"], r["field"])].add(r["label"])
    return out


def eligible_categories(
    conn: sqlite3.Connection, persona_id: str, batch: Batch, min_episodes: int
) -> set[str]:
    """Category IDs worth annotating in this job (see the module docstring)."""
    per_cat: dict[str, int] = defaultdict(int)
    paths: dict[str, str] = {}
    for ep in batch.episodes:
        if ep.category_id:
            per_cat[ep.category_id] += 1
            paths[ep.category_id] = ep.category
    out = {c for c, n in per_cat.items() if n >= min_episodes}
    roots = [
        r["path"]
        for r in conn.execute(
            "SELECT c.path FROM analyzers a JOIN categories c ON c.id = a.scope_id"
            " WHERE a.persona_id = ? AND a.origin = 'llm' AND a.status = 'active'"
            " AND a.scope_kind = 'category'",
            (persona_id,),
        )
    ]
    for cat_id, path in paths.items():
        if any(path == r or path.startswith(r + "/") for r in roots):
            out.add(cat_id)
    return out


def _vocab_text(words: Iterable[str]) -> str:
    words = sorted(words)
    return ", ".join(words) if words else "(none yet)"


def plan(
    batch: Batch,
    categories: set[str],
    vocabulary: dict[tuple[str, str], set[str]],
    make_call: Any,
) -> list[Any]:
    """The annotate calls for the eligible ``categories`` (IDs). ``make_call(system, user,
    schema, records, meta)`` builds a ``passes.Call``."""
    calls: list[Any] = []
    by_cat: dict[str, list[Episode]] = defaultdict(list)
    for ep in batch.episodes:
        if ep.category_id in categories:
            by_cat[ep.category_id].append(ep)
    for cat_id in sorted(by_cat):
        eps = by_cat[cat_id]
        path = eps[0].category
        free: dict[int, list[str]] = defaultdict(list)  # batch index -> free-text fields
        for fld, limit in (
            ("decision", MAX_DECISION_DISTINCT),
            ("situation", MAX_SITUATION_DISTINCT),
        ):
            with_value = [ep for ep in eps if getattr(ep, fld)]
            if not with_value:
                continue
            distinct = list(dict.fromkeys(normalize_value(getattr(ep, fld)) for ep in with_value))
            if len(distinct) <= limit:
                lines = "\n".join(f"{i}. {v[:VALUE_CHARS]}" for i, v in enumerate(distinct, 1))
                if fld == "decision":
                    system = DECISION_SYSTEM
                else:
                    system = SITUATION_SYSTEM.replace("{category}", path).replace(
                        "{vocabulary}", _vocab_text(vocabulary.get((cat_id, fld), ()))
                    )
                meta = Annotation(cat_id, fld, distinct, with_value)
                calls.append(
                    make_call(system, lines, MAP_SCHEMA, {ep.index for ep in with_value}, meta)
                )
            else:
                for ep in with_value:
                    free[ep.index].append(fld)
        if free:
            tagged = [ep for ep in eps if ep.index in free]
            system = TAG_SYSTEM.replace("{category}", path).replace(
                "{vocabulary}", _vocab_text(vocabulary.get((cat_id, "tag"), ()))
            )
            for start in range(0, len(tagged), TAG_BATCH):
                chunk = tagged[start : start + TAG_BATCH]
                lines = []
                for i, ep in enumerate(chunk, 1):
                    parts = []
                    if ep.situation and "situation" in free[ep.index]:
                        parts.append("SITUATION: " + " ".join(ep.situation.split()))
                    if ep.decision and "decision" in free[ep.index]:
                        parts.append("CHOICE: " + " ".join(ep.decision.split()))
                    lines.append(f"{i}. " + " ".join(parts)[:TAG_TEXT_CHARS])
                meta = Annotation(cat_id, "tag", [], chunk)
                calls.append(
                    make_call(system, "\n".join(lines), TAG_SCHEMA, {e.index for e in chunk}, meta)
                )
    return calls


class Labeler:
    """Applies annotate replies: checks new names, stores them, labels episodes in memory."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        persona_id: str,
        guard: BatchGuard,
        rare: RareWords,
        vocabulary: dict[tuple[str, str], set[str]],
    ) -> None:
        self.conn, self.persona_id = conn, persona_id
        self.guard, self.rare, self.vocabulary = guard, rare, vocabulary
        self.counts = AnnotateCounts()
        self._rejected: set[tuple[str, str, str]] = set()

    def _accept(self, cat_id: str, fld: str, raw: Any) -> str | None:
        label = normalize_label(raw)
        if label is None:
            self.counts.invalid += 1
            return None
        if fld == "decision" and label not in DECISION_LABELS:
            self.counts.invalid += 1
            return None
        vocab = self.vocabulary.setdefault((cat_id, fld), set())
        if label in vocab:
            return label
        if (cat_id, fld, label) in self._rejected:
            return None
        if fld != "decision" and (
            self.guard.check(label) is not None or self.rare.find(label.replace("-", " "))
        ):
            self._rejected.add((cat_id, fld, label))
            self.counts.dropped_by_guard += 1
            return None
        if len(vocab) >= MAX_LABELS:
            self._rejected.add((cat_id, fld, label))
            self.counts.labels_dropped += 1
            return None
        self.conn.execute(
            "INSERT OR IGNORE INTO category_labels(persona_id, category_id, field, label)"
            " VALUES (?, ?, ?, ?)",
            (self.persona_id, cat_id, fld, label),
        )
        vocab.add(label)
        self.counts.labels_created += 1
        return label

    def apply(self, meta: Annotation, data: Any) -> None:
        if meta.field == "tag":
            self._apply_tags(meta, data)
            return
        items = data.get("labels") if isinstance(data, dict) else None
        if not isinstance(items, list):
            self.counts.failed_calls += 1
            return
        mapping: dict[str, str] = {}
        for item in items:
            if not isinstance(item, dict):
                self.counts.invalid += 1
                continue
            i = item.get("i")
            if not isinstance(i, int) or isinstance(i, bool) or not 1 <= i <= len(meta.values):
                self.counts.invalid += 1
                continue
            label = self._accept(meta.category_id, meta.field, item.get("label"))
            if label is not None:
                mapping[meta.values[i - 1]] = label
        for ep in meta.episodes:
            value = getattr(ep, meta.field)
            label = mapping.get(normalize_value(value)) if value else None
            if label is not None:
                ep.labels[meta.field] = label
        mapping.clear()

    def _apply_tags(self, meta: Annotation, data: Any) -> None:
        items = data.get("tags") if isinstance(data, dict) else None
        if not isinstance(items, list):
            self.counts.failed_calls += 1
            return
        for item in items:
            if not isinstance(item, dict):
                self.counts.invalid += 1
                continue
            i, tags = item.get("i"), item.get("tags")
            if (
                not isinstance(i, int)
                or isinstance(i, bool)
                or not 1 <= i <= len(meta.episodes)
                or not isinstance(tags, list)
            ):
                self.counts.invalid += 1
                continue
            ep = meta.episodes[i - 1]
            for raw in tags[:MAX_TAGS]:
                tag = self._accept(meta.category_id, "tag", raw)
                if tag is not None and tag not in ep.tags and len(ep.tags) < MAX_TAGS:
                    ep.tags.append(tag)
