# SPDX-License-Identifier: AGPL-3.0-or-later
"""Placement of episodes the client sent without a category (SPEC §4.4, §7.1 step 0, D23).

Batched calls (at most :data:`MAX_EPISODES` episodes and about :data:`MAX_CHARS` characters
of episode text each; ``situation`` and ``said`` cut to 300 chars, ``decision`` to 200) see
the persona's category paths with their descriptions and return, per episode, an existing
or a new path, with a one-line description when the path is new (one item per episode). The system prompt is static
(prefix caching); the category list and the episodes are in the user message.

A returned path goes through the job's :class:`~wwyd.store.categories.CategoryResolver`
(existing -> alias -> created within the limits). A path that would create categories
passes the full verbatim guard including the concentrated-word check
(:mod:`wwyd.ingest.rare`); a failing path maps to the deepest existing ancestor or
``other``. A new description passes guard parts 1-3; a failing one is dropped and the path
kept. An episode whose call returned no usable path goes to ``other``. Failing paths and
missing placements count in ``unplaced_episodes``. Nothing here logs or stores prompt or
response text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from wwyd.errors import Invalid
from wwyd.ingest.episodes import Batch, Episode
from wwyd.ingest.guard import BatchGuard
from wwyd.ingest.rare import RareWords
from wwyd.store.categories import CategoryResolver, normalize_category_path

# 25 episodes per call bounds the reply size: each item is short and the list is one item
# per episode, so a small model cannot run on to max_tokens.
MAX_EPISODES = 25
MAX_CHARS = 6000
SITUATION_CHARS = 300
SAID_CHARS = 300
DECISION_CHARS = 200
MAX_DESCRIPTION_CHARS = 200

# One item per episode. Key order matters: JSON-schema grammars emit object keys in
# alphabetical order, so "i" < "path" < "path_description" makes the model commit to the
# episode number, then the path, and only then describe a new path (same reason as
# predict's ``a_depends_on``). A separate list of new categories generated first made
# small models invent categories in a loop before placing anything.
SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["placements"],
    "properties": {
        "placements": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["i", "path", "path_description"],
                "properties": {
                    "i": {"type": "integer"},
                    "path": {"type": "string"},
                    "path_description": {"type": "string"},
                },
            },
        },
    },
}

SYSTEM = """You sort episodes from one person's life (the owner) into the categories of the
owner's knowledge base. A category groups what the owner does and prefers in one area of
life; paths are lowercase segments joined by "/", at most 3 levels.
The input lists the existing categories (path: description), then numbered episodes:
SITUATION is what happened or was said to the owner, SAID the owner's words, CHOICE what
the owner did.
Rules:
- Return exactly one item per episode, in order: {"i": <episode number>, "path": ...,
  "path_description": ...}. Never add items for paths no episode needs.
- Prefer an existing category. Propose a new path only when none fits, preferably below an
  existing parent.
- Before proposing a new path, check whether an existing category already covers that area
  under another parent, and use it.
- Place by what the owner is doing, not by who is involved. A friend, relative, colleague or
  company in the situation does not make it family or work: going fishing with a friend is
  a hobby, a lunch invitation is food or social life, a recruiter's job offer is work.
- New paths name an area of life, never a person, company, place, product or account.
- path_description is "" when the path already exists. For a new path it is one line (at
  most 120 characters, in the language of the path) saying what the category holds, written
  for this episode's area; do not copy descriptions from this prompt. Never mention names,
  companies, places, links, emails or phone numbers.

Example:
Input:
Categories:
- money: How the owner spends, saves and lends money.
- sport: Which sports the owner does and how often.
- family: How the owner handles matters with relatives.
- other
Episodes:
1. SITUATION: A cousin asks to borrow money for a car. CHOICE: lent half, no interest
2. SITUATION: Colleague suggests a 10 km run on Sunday. CHOICE: joined
3. SITUATION: Choosing a birthday present for a brother. CHOICE: bought a book voucher
4. SITUATION: Train or plane for a two-day trip. CHOICE: took the night train
Answer:
{"placements": [{"i": 1, "path": "money", "path_description": ""}, {"i": 2, "path": "sport",
"path_description": ""}, {"i": 3, "path": "money/gifts", "path_description": "What presents
the owner picks and how much they spend."}, {"i": 4, "path": "travel", "path_description":
"How the owner chooses transport and plans trips."}]}
"""


@dataclass
class Placement:
    """What one place call covers (in memory only): the episodes, numbered from 1."""

    episodes: list[Episode] = field(default_factory=list)


@dataclass
class PlaceCounts:
    unplaced: int = 0
    dropped_by_guard: int = 0
    invalid: int = 0
    failed_calls: int = 0


def _one_line(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _segment(ep: Episode) -> str:
    parts = []
    if ep.audience:
        parts.append(f"(audience: {ep.audience})")
    if ep.where:
        parts.append(f"(where: {_one_line(ep.where)[:100]})")
    if ep.situation:
        parts.append(f"SITUATION: {_one_line(ep.situation)[:SITUATION_CHARS]}")
    if ep.said:
        parts.append(f"SAID: {_one_line(ep.said)[:SAID_CHARS]}")
    if ep.decision:
        parts.append(f"CHOICE: {_one_line(ep.decision)[:DECISION_CHARS]}")
    return " ".join(parts)


def category_list(categories: dict[str, str]) -> str:
    return "\n".join(
        f"- {path}: {_one_line(desc)}" if desc else f"- {path}"
        for path, desc in sorted(categories.items())
    )


def plan(batch: Batch, categories: dict[str, str], make_call: Any) -> list[Any]:
    """The place calls for the batch's episodes without a category. ``make_call(system,
    user, schema, records, meta)`` builds a ``passes.Call``."""
    todo = [ep for ep in batch.episodes if ep.category is None]
    if not todo:
        return []
    head = "Categories:\n" + category_list(categories) + "\nEpisodes:\n"
    calls: list[Any] = []
    chunk: list[tuple[Episode, str]] = []
    size = 0

    def emit() -> None:
        lines = "\n".join(f"{i}. {seg}" for i, (_, seg) in enumerate(chunk, start=1))
        meta = Placement([ep for ep, _ in chunk])
        calls.append(make_call(SYSTEM, head + lines, SCHEMA, {ep.index for ep, _ in chunk}, meta))

    for ep in todo:
        seg = _segment(ep)
        if chunk and (len(chunk) >= MAX_EPISODES or size + len(seg) + 1 > MAX_CHARS):
            emit()
            chunk, size = [], 0
        chunk.append((ep, seg))
        size += len(seg) + 1
    if chunk:
        emit()
    return calls


def _normalized(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return normalize_category_path(value.strip())
    except Invalid:
        return None


_SEPARATORS = re.compile(r"[/_-]+")


class Placer:
    """Applies place replies to the episodes through the job's resolver (inside the
    caller's transaction). ``known_words`` are the words of the existing category paths,
    which the concentrated-word check ignores (a name the model reuses from the tree)."""

    def __init__(
        self,
        resolver: CategoryResolver,
        guard: BatchGuard,
        rare: RareWords,
        known_words: set[str],
    ) -> None:
        self.resolver, self.guard, self.rare = resolver, guard, rare
        self.known_words = {w.casefold() for w in known_words}
        self.counts = PlaceCounts()
        # category ID -> guarded description of a category this placement created.
        self.descriptions: dict[str, str] = {}

    def _safe(self, text: str) -> bool:
        if self.guard.check(text) is not None:
            return False
        return not self.rare.find(text, ignore=self.known_words)

    def _fallback(self, ep: Episode, path: str | None = None) -> None:
        ep.category_id, ep.category = self.resolver.fallback(path)
        self.counts.unplaced += 1

    def apply(self, meta: Placement, data: Any) -> None:
        resolver, counts = self.resolver, self.counts
        if not isinstance(data, dict) or not isinstance(data.get("placements"), list):
            counts.failed_calls += 1
            data = {}
        chosen: dict[int, str] = {}
        described: dict[str, str] = {}
        failed: set[str] = set()  # new paths with a description already dropped (count once)
        for item in data.get("placements") or ():
            if not isinstance(item, dict) or not isinstance(item.get("i"), int):
                counts.invalid += 1
                continue
            path = _normalized(item.get("path"))
            if path is None:
                counts.invalid += 1
                continue
            if item["i"] in chosen:
                continue
            chosen[item["i"]] = path
            text = item.get("path_description")
            if not isinstance(text, str) or path in described or resolver.known(path):
                continue
            text = _one_line(text)
            if not text:
                continue
            if len(text) > MAX_DESCRIPTION_CHARS:
                counts.invalid += 1
                continue
            if self.guard.check(text) is not None:  # parts 1-3, as every description
                if path not in failed:
                    failed.add(path)
                    counts.dropped_by_guard += 1
                continue
            described[path] = text  # the first non-empty description passing the guard wins
        rejected: set[str] = set()
        for n, ep in enumerate(meta.episodes, start=1):
            path = chosen.get(n)
            if path is None:
                self._fallback(ep)
                continue
            if not resolver.known(path):
                if path not in rejected and not self._safe(_SEPARATORS.sub(" ", path)):
                    rejected.add(path)
                    counts.dropped_by_guard += 1
                if path in rejected:
                    self._fallback(ep, path)
                    continue
            before = len(resolver.created)
            ep.category_id, ep.category = resolver.resolve(path)
            for c in resolver.created[before:]:
                if c["path"] in described:
                    self.descriptions[c["id"]] = described[c["path"]]
        described.clear()
        chosen.clear()
        failed.clear()
