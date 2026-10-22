# SPDX-License-Identifier: AGPL-3.0-or-later
"""Nightly category upkeep (SPEC §10.6): ``category_merge`` and ``category_split``.

Both apply automatically, one ``changes`` row per merge or split, undoable (D21).

* **Merge.** One judge call shows the episode-created categories made since the previous
  nightly run (``compaction_runs.last_nightly_at``; every one on the first run) next to all
  other categories, by path and description, and asks for synonym pairs (also across
  languages). Direction: an episode category merges into a seed or owner one, never the
  other way; two seed/owner categories are never merged automatically (the owner made
  them); between two episode categories the newer merges into the older. Ancestor and
  descendant pairs are refused. A merge collapses the source subtree into the target, so
  it never deepens the tree. A manual ``compact`` uses the same watermark, so it may judge
  the same new categories again until the next nightly run.
* **Split.** A category above ``split_threshold`` non-archived claims and above depth 3:
  one call groups its unkeyed claims (shown as ``c1``, ``c2``…; keyed analyzer claims stay
  with the parent, as do its analyzers) into 2–5 sub-categories. Names must normalise to
  one path segment and pass the guard's regex part, like descriptions; a grouping that
  misses or repeats a claim, or names an unknown one, is discarded whole.

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
from wwyd.store.common import MAX_CATEGORY_DEPTH, MAX_DESCRIPTION

log = logging.getLogger(__name__)

CATEGORY_STEPS = ("category_merge", "category_split")
MAX_MERGE_PAIRS = 50
MAX_SPLIT_CLAIMS = 200
SPLIT_GROUPS = (2, 5)
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

    try:
        result = await get_llm(run.conn, run.persona_id).complete_json(
            purpose="category_merge",
            system=MERGE_SYSTEM,
            user=json.dumps({"new": show(new), "others": show(others)}, ensure_ascii=False),
            schema=MERGE_SCHEMA,
            max_tokens=64 + 40 * min(len(new), MAX_MERGE_PAIRS),
        )
    except _LLM_ERRORS as exc:
        log.info("category merge skipped: %s", exc.code)
        _skip(run, "category_merge")
        return
    data = result.data if isinstance(result.data, dict) else {}
    pairs = data.get("pairs") if isinstance(data.get("pairs"), list) else []
    new_paths = {r["path"] for r in new}
    for pair in pairs[:MAX_MERGE_PAIRS]:
        if not isinstance(pair, dict):
            continue
        a, b = pair.get("a"), pair.get("b")
        if not isinstance(a, str) or not isinstance(b, str):
            continue
        if a not in new_paths and b not in new_paths:
            continue
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
    """Validated groups (claim IDs instead of labels), or None to discard the grouping."""
    groups = data.get("groups") if isinstance(data, dict) else None
    if not isinstance(groups, list) or not SPLIT_GROUPS[0] <= len(groups) <= SPLIT_GROUPS[1]:
        return None
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    names: set[str] = set()
    for g in groups:
        if not isinstance(g, dict):
            return None
        name = _segment(g.get("name"))
        desc = g.get("description")
        members = g.get("claims")
        if name is None or name in names:
            return None
        if not isinstance(desc, str) or len(desc) > MAX_DESCRIPTION:
            return None
        desc = " ".join(desc.split())
        if find_identifier(desc) is not None:
            return None
        if not isinstance(members, list) or not members:
            return None
        ids = []
        for label in members:
            if not isinstance(label, str) or label not in labels or label in seen:
                return None
            seen.add(label)
            ids.append(labels[label])
        names.add(name)
        out.append({"name": name, "description": desc, "claims": ids})
    if seen != set(labels):
        return None  # partial grouping
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
