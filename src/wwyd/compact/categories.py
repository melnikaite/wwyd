# SPDX-License-Identifier: AGPL-3.0-or-later
"""Nightly category upkeep (SPEC §10.6): ``category_merge`` and ``category_split``.

Both apply automatically, one ``changes`` row per merge or split, undoable (D21).

* **Merge.** For the episode-created categories made since the previous nightly run
  (``compaction_runs.last_nightly_at``; every one on the first run), code proposes
  candidate pairs (same or close leaf name, overlapping description words) and one
  batched call confirms each yes/no. One open-search call then shows the new categories
  no candidate covered next to all others, by path and description, and asks for synonym
  pairs (also across languages); it is skipped when nothing is left. Direction: an episode category merges into a seed or owner one, never the
  other way; two seed/owner categories are never merged automatically (the owner made
  them); between two episode categories the newer merges into the older. Ancestor and
  descendant pairs are refused. A merge collapses the source subtree into the target, so
  it never deepens the tree. A manual ``compact`` uses the same watermark, so it may judge
  the same new categories again until the next nightly run.
* **Split.** A category above ``split_threshold`` non-archived claims and above depth 3:
  one call groups its unkeyed claims (shown as ``c1``, ``c2``…; keyed analyzer claims stay
  with the parent, as do its analyzers) into 2–5 sub-categories. Names must normalise to
  one path segment and pass the guard's regex part, like descriptions. Partial groupings
  are kept (see :func:`parse_groups`); only a degenerate one is discarded.

``apply=False`` makes no LLM call: it reports what would be sent. LLM errors and limits
skip the step and are counted in ``skipped_steps``; they never fail the run.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any

from wwyd.compact.run import Run
from wwyd.db import transaction
from wwyd.errors import Conflict, Invalid, LLMUnavailable, NotFound, RateLimited, SpendCap
from wwyd.ingest.guard import find_identifier
from wwyd.store import categories as cats
from wwyd.store.claims import text_tokens
from wwyd.store.common import MAX_CATEGORY_DEPTH, MAX_DESCRIPTION

log = logging.getLogger(__name__)

CATEGORY_STEPS = ("category_merge", "category_split")
MAX_MERGE_PAIRS = 50
MAX_SPLIT_CLAIMS = 200
SPLIT_GROUPS = (2, 5)
MIN_CLOSE_NAME = 4  # shorter leaves are only candidates when identical
MIN_DESCRIPTION_TOKENS = 3
DESCRIPTION_JACCARD = 0.5
_LLM_ERRORS = (LLMUnavailable, SpendCap, RateLimited)

MERGE_SYSTEM = """\
You keep a person's category tree tidy. Each category has a path (segments separated by
"/") and a one-line description. Find categories in "new" that mean the same thing as
another category in "new" or in "others": synonyms, singular/plural, or the same idea in
another language (e.g. "beekeeping", "apiculture" and "пчеловодство"). A category and its own parent or
child are never synonyms. Different but related topics are not synonyms.
Answer only with JSON: {"pairs": [{"a": "<path from new>", "b": "<other path>"}]};
an empty list when nothing matches.
"""

MERGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "pairs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"a": {"type": "string"}, "b": {"type": "string"}},
                "required": ["a", "b"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["pairs"],
    "additionalProperties": False,
}

CONFIRM_SYSTEM = """\
You keep a person's category tree tidy. Each numbered pair shows two categories (a path
with segments separated by "/" and a one-line description). For each pair answer
"same": true when both mean the same topic (synonyms, singular/plural, a spelling
variant, the same idea in another language), false when they are different topics, even
related ones.
Answer only with JSON: {"answers": [{"pair": <number>, "same": true|false}]} with exactly
one entry per pair.
"""

CONFIRM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "answers": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"pair": {"type": "integer"}, "same": {"type": "boolean"}},
                "required": ["pair", "same"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["answers"],
    "additionalProperties": False,
}

SPLIT_SYSTEM = """\
A category of statements about one person (the owner) has grown too large. Group its
statements (labelled c1, c2, ...) into 2 to 5 sub-categories. Every statement goes into
exactly one group. Give each group a short lowercase name (one word or words joined by
"-", no "/", in the language of the statements) and a one-line description of the topic.
Never use names of people, places, contacts or other identifiers.
Answer only with JSON:
{"groups": [{"name": "...", "description": "...", "claims": ["c1", "c4"]}]}
"""

SPLIT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "groups": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "claims": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["name", "description", "claims"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["groups"],
    "additionalProperties": False,
}


def _skip(run: Run, step: str) -> None:
    run.skipped_steps[step] = run.skipped_steps.get(step, 0) + 1


def _watermark(conn: sqlite3.Connection, persona_id: str) -> str | None:
    row = conn.execute(
        "SELECT last_nightly_at FROM compaction_runs WHERE persona_id = ?", (persona_id,)
    ).fetchone()
    return row["last_nightly_at"] if row else None


def _categories(conn: sqlite3.Connection, persona_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT rowid AS seq, id, path, description, origin, created_at FROM categories"
        " WHERE persona_id = ? ORDER BY path",
        (persona_id,),
    ).fetchall()


def merge_candidates(conn: sqlite3.Connection, persona_id: str) -> tuple[list, list]:
    """``(new, others)``: episode categories created since the previous nightly run, and
    every other category."""
    since = _watermark(conn, persona_id)
    rows = _categories(conn, persona_id)
    new = [
        r for r in rows if r["origin"] == "episode" and (since is None or r["created_at"] >= since)
    ]
    new_ids = {r["id"] for r in new}
    return new, [r for r in rows if r["id"] not in new_ids]


def _related(a: str, b: str) -> bool:
    return a.startswith(b + "/") or b.startswith(a + "/")


def plan_merge(a: sqlite3.Row, b: sqlite3.Row) -> tuple[str, str] | None:
    """``(source, target)`` paths for a synonym pair, or None when the rules refuse it."""
    if a["id"] == b["id"] or _related(a["path"], b["path"]):
        return None
    a_fixed, b_fixed = a["origin"] != "episode", b["origin"] != "episode"
    if a_fixed and b_fixed:
        return None
    if a_fixed != b_fixed:
        src, dst = (b, a) if a_fixed else (a, b)
    else:
        src, dst = sorted((a, b), key=lambda r: (r["created_at"], r["seq"]), reverse=True)
    return src["path"], dst["path"]


def _edit_distance(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _close_names(a: str, b: str) -> bool:
    """Same leaf, or leaves a typo or an inflection apart ("film"/"films")."""
    if a == b:
        return True
    short = min(len(a), len(b))
    if short < MIN_CLOSE_NAME:
        return False
    return _edit_distance(a, b) <= (1 if short < 8 else 2)


def _close_descriptions(a: str, b: str) -> bool:
    ta, tb = text_tokens(a or ""), text_tokens(b or "")
    if len(ta) < MIN_DESCRIPTION_TOKENS or len(tb) < MIN_DESCRIPTION_TOKENS:
        return False
    return len(ta & tb) / len(ta | tb) >= DESCRIPTION_JACCARD


def candidate_pairs(
    new: list[sqlite3.Row], others: list[sqlite3.Row]
) -> list[tuple[sqlite3.Row, sqlite3.Row]]:
    """Code-proposed merge pairs (each has a new category): the same or a close leaf name,
    or largely the same description words. Pairs the merge rules refuse are left out;
    the model only confirms them. At most ``MAX_MERGE_PAIRS``."""
    out: list[tuple[sqlite3.Row, sqlite3.Row]] = []
    seen: set[tuple[str, str]] = set()
    for a in new:
        for b in new + others:
            key = tuple(sorted((a["id"], b["id"])))
            if a["id"] == b["id"] or key in seen:
                continue
            seen.add(key)
            if plan_merge(a, b) is None:
                continue
            leaf_a, leaf_b = a["path"].rsplit("/", 1)[-1], b["path"].rsplit("/", 1)[-1]
            if _close_names(leaf_a, leaf_b) or _close_descriptions(
                a["description"], b["description"]
            ):
                out.append((a, b))
                if len(out) >= MAX_MERGE_PAIRS:
                    return out
    return out


async def run_merge(run: Run) -> None:
    new, others = merge_candidates(run.conn, run.persona_id)
    if not new or len(new) + len(others) < 2:
        return
    if not run.apply:
        run.category_pending["category_merge"] = [r["path"] for r in new]
        return
    if not run.use_llm:
        _skip(run, "category_merge")
        return
    from wwyd.llm import get_llm

    def show(rows: list[sqlite3.Row]) -> list[dict[str, str]]:
        return [{"path": r["path"], "description": r["description"]} for r in rows]

    llm = None
    candidates = candidate_pairs(new, others)
    confirmed: list[tuple[str, str]] = []
    if candidates:
        items = [
            {"pair": i + 1, "a": show([a])[0], "b": show([b])[0]}
            for i, (a, b) in enumerate(candidates)
        ]
        try:
            llm = get_llm(run.conn, run.persona_id)
            result = await llm.complete_json(
                purpose="category_confirm",
                system=CONFIRM_SYSTEM,
                user=json.dumps(items, ensure_ascii=False),
                schema=CONFIRM_SCHEMA,
                max_tokens=64 + 20 * len(candidates),
            )
        except _LLM_ERRORS as exc:
            log.info("category merge skipped: %s", exc.code)
            _skip(run, "category_merge")
            return
        data = result.data if isinstance(result.data, dict) else {}
        yes = {
            v["pair"]
            for v in data.get("answers") or []
            if isinstance(v, dict) and isinstance(v.get("pair"), int) and v.get("same") is True
        }
        confirmed = [(a["path"], b["path"]) for i, (a, b) in enumerate(candidates) if i + 1 in yes]
    # The open search (synonyms the code cannot see, e.g. across languages) only shows
    # the new categories that no code-proposed pair covered.
    covered = {r["id"] for pair in candidates for r in pair}
    rest = [r for r in new if r["id"] not in covered]
    found: list[Any] = []
    if rest:
        try:
            llm = llm or get_llm(run.conn, run.persona_id)
            others_shown = [r for r in new + others if r["id"] not in {x["id"] for x in rest}]
            result = await llm.complete_json(
                purpose="category_merge",
                system=MERGE_SYSTEM,
                user=json.dumps(
                    {"new": show(rest), "others": show(others_shown)}, ensure_ascii=False
                ),
                schema=MERGE_SCHEMA,
                max_tokens=64 + 40 * min(len(rest), MAX_MERGE_PAIRS),
            )
        except _LLM_ERRORS as exc:
            log.info("category merge skipped: %s", exc.code)
            _skip(run, "category_merge")
            result = None
        data = result.data if result is not None and isinstance(result.data, dict) else {}
        found = data.get("pairs") if isinstance(data.get("pairs"), list) else []
    rest_paths = {r["path"] for r in rest}
    pairs: list[tuple[str, str]] = list(confirmed)
    for pair in found[:MAX_MERGE_PAIRS]:
        if not isinstance(pair, dict):
            continue
        a, b = pair.get("a"), pair.get("b")
        if isinstance(a, str) and isinstance(b, str) and (a in rest_paths or b in rest_paths):
            pairs.append((a, b))
    for a, b in pairs:
        rows = {r["path"]: r for r in _categories(run.conn, run.persona_id)}
        if a not in rows or b not in rows:
            continue  # gone (merged earlier in this run)
        planned = plan_merge(rows[a], rows[b])
        if planned is None:
            continue
        src, dst = planned
        try:
            with transaction(run.conn):
                out = cats.merge_into(run.conn, run.persona_id, [src], dst, reason="synonym")
        except (Conflict, Invalid, NotFound):
            continue
        claims = json.loads(
            run.conn.execute(
                "SELECT payload FROM changes WHERE id = ?", (out["change_id"],)
            ).fetchone()["payload"]
        )["claims"]
        run.changes.append(
            {
                "id": out["change_id"],
                "op": "category_merge",
                "state": "applied",
                "claims": claims,
                "reason": "synonym",
            }
        )


def split_candidates(
    conn: sqlite3.Connection, persona_id: str, threshold: int
) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT c.id, c.path, c.description FROM categories c WHERE c.persona_id = ?"
        " AND length(c.path) - length(replace(c.path, '/', '')) < ?"
        " AND (SELECT COUNT(*) FROM claims k WHERE k.category_id = c.id"
        "      AND k.status <> 'archived') > ? ORDER BY c.path",
        (persona_id, MAX_CATEGORY_DEPTH - 1, threshold),
    ).fetchall()


def _segment(name: Any) -> str | None:
    if not isinstance(name, str):
        return None
    try:
        seg = cats.normalize_category_path(name.strip())
    except Invalid:
        return None
    if "/" in seg or find_identifier(seg) is not None:
        return None
    return seg


def parse_groups(data: Any, labels: dict[str, str]) -> list[dict[str, Any]] | None:
    """Validated groups (claim IDs instead of labels), or None to discard the grouping.

    Partial answers are accepted: a group with an invalid name or description, or a
    repeated name, is dropped (its claims stay in the parent); unknown labels are
    ignored; a claim listed twice keeps its first group; unlisted claims stay in the
    parent. Only a degenerate grouping (fewer than two non-empty valid groups, or more
    groups than allowed) is discarded."""
    groups = data.get("groups") if isinstance(data, dict) else None
    if not isinstance(groups, list) or len(groups) > SPLIT_GROUPS[1]:
        return None
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    names: set[str] = set()
    for g in groups:
        if not isinstance(g, dict):
            continue
        name = _segment(g.get("name"))
        desc = g.get("description")
        members = g.get("claims")
        if name is None or name in names:
            continue
        if not isinstance(desc, str) or len(desc) > MAX_DESCRIPTION:
            continue
        desc = " ".join(desc.split())
        if find_identifier(desc) is not None or not isinstance(members, list):
            continue
        ids = []
        for label in members:
            if isinstance(label, str) and label in labels and label not in seen:
                seen.add(label)
                ids.append(labels[label])
        if not ids:
            continue
        names.add(name)
        out.append({"name": name, "description": desc, "claims": ids})
    if len(out) < SPLIT_GROUPS[0]:
        return None
    return out


def _max_categories(conn: sqlite3.Connection, persona_id: str) -> int | None:
    from wwyd.limits import get_limiter

    row = conn.execute("SELECT user_id FROM personas WHERE id = ?", (persona_id,)).fetchone()
    if row is None:
        return None
    value = get_limiter(conn).effective_limits(row["user_id"]).get("max_categories")
    return int(value) if value is not None else None


async def run_split(run: Run) -> None:
    from wwyd.config import thresholds

    candidates = split_candidates(run.conn, run.persona_id, thresholds().split_threshold)
    if not candidates:
        return
    if not run.apply:
        run.category_pending["category_split"] = [r["path"] for r in candidates]
        return
    if not run.use_llm:
        _skip(run, "category_split")
        return
    from wwyd.llm import get_llm

    llm = None
    for cat in candidates:
        rows = run.conn.execute(
            "SELECT id, kind, text FROM claims WHERE category_id = ? AND status <> 'archived'"
            " AND key IS NULL ORDER BY seq DESC LIMIT ?",
            (cat["id"], MAX_SPLIT_CLAIMS),
        ).fetchall()
        if len(rows) < SPLIT_GROUPS[0]:
            continue
        labels = {f"c{i + 1}": r["id"] for i, r in enumerate(rows)}
        items = [
            {"id": f"c{i + 1}", "kind": r["kind"], "text": r["text"]} for i, r in enumerate(rows)
        ]
        try:
            llm = llm or get_llm(run.conn, run.persona_id)
            result = await llm.complete_json(
                purpose="category_split",
                system=SPLIT_SYSTEM,
                user=json.dumps(
                    {"category": cat["path"], "description": cat["description"], "claims": items},
                    ensure_ascii=False,
                ),
                schema=SPLIT_SCHEMA,
                max_tokens=200 + 8 * len(rows),
            )
        except _LLM_ERRORS as exc:
            log.info("category split skipped: %s", exc.code)
            _skip(run, "category_split")
            return
        groups = parse_groups(result.data, labels)
        if groups is None:
            run.discarded_splits += 1
            continue
        limit = _max_categories(run.conn, run.persona_id)
        if limit is not None:
            existing = cats.category_ids_by_path(run.conn, run.persona_id)
            fresh = [g for g in groups if f"{cat['path']}/{g['name']}" not in existing]
            if len(existing) + len(fresh) > limit:
                _skip(run, "category_split")
                continue
        try:
            with transaction(run.conn):
                out = cats.split_into(
                    run.conn, run.persona_id, cat["path"], groups, reason="too_large"
                )
        except (Conflict, Invalid, NotFound):
            run.discarded_splits += 1
            continue
        run.changes.append(
            {
                "id": out["change_id"],
                "op": "category_split",
                "state": "applied",
                "claims": out["claims"],
                "reason": "too_large",
            }
        )


async def run_steps(run: Run, steps: tuple[str, ...]) -> None:
    """Run the requested category steps; an unexpected failure of one step is logged
    (type only) and counted, never raised."""
    for step, fn in (("category_merge", run_merge), ("category_split", run_split)):
        if step not in steps:
            continue
        try:
            await fn(run)
        except Exception as exc:  # noqa: BLE001 - a step never fails the run
            log.warning("%s failed: %s", step, type(exc).__name__)
            _skip(run, step)
