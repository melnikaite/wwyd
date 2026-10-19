# SPDX-License-Identifier: AGPL-3.0-or-later
"""Category tree (materialised paths, at most 3 levels), path normalisation and organic
creation from ingest episodes (SPEC §4.4)."""

from __future__ import annotations

import sqlite3
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from wwyd.db import transaction
from wwyd.errors import Conflict, Invalid, NotFound
from wwyd.ids import new_id, now_iso
from wwyd.ingest.guard import find_identifier
from wwyd.store.common import (
    MAX_CATEGORY_DEPTH,
    MAX_CATEGORY_SEGMENT,
    check_category_path,
    check_description,
    parent_path,
    record_change,
)

ORIGINS = ("seed", "owner", "episode")
FALLBACK_CATEGORY = "other"
_PATH_RULE = (
    "category path segments must be Unicode letters, digits, '-' or '_', start with a letter "
    f"or digit and be at most {MAX_CATEGORY_SEGMENT} characters, separated by '/'"
)


def _segment_ok(seg: str) -> bool:
    return (
        0 < len(seg) <= MAX_CATEGORY_SEGMENT
        and seg[0].isalnum()
        and all(c.isalnum() or c in "-_" for c in seg)
    )


def normalize_category_path(path: Any) -> str:
    """The one category path normalisation (every tool and every episode, SPEC §4.4).

    NFKC, casefold, whitespace around a segment dropped and inner whitespace runs turned
    into ``-``; segments of Unicode letters, digits, ``-`` and ``_`` that start with a
    letter or digit, at most 40 characters; at most 3 levels. A superset of the former
    ASCII rule, so existing paths normalise to themselves. Errors never quote the path.
    """
    if not isinstance(path, str):
        raise Invalid("category path must be a string", field="path")
    text = unicodedata.normalize("NFKC", path).casefold()
    segments = ["-".join(seg.split()) for seg in text.split("/")]
    if len(segments) > MAX_CATEGORY_DEPTH:
        raise Invalid(f"category path deeper than {MAX_CATEGORY_DEPTH} levels", field="path")
    if not all(_segment_ok(seg) for seg in segments):
        raise Invalid(_PATH_RULE, field="path")
    return "/".join(segments)


def _row(conn: sqlite3.Connection, persona_id: str, path: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM categories WHERE persona_id = ? AND path = ?", (persona_id, path)
    ).fetchone()


def get_category(conn: sqlite3.Connection, persona_id: str, path: str) -> sqlite3.Row:
    row = _row(conn, persona_id, path)
    if row is None:
        raise NotFound(f"category {path!r} not found", path=path)
    return row


def category_ids_by_path(conn: sqlite3.Connection, persona_id: str) -> dict[str, str]:
    return {
        r["path"]: r["id"]
        for r in conn.execute("SELECT id, path FROM categories WHERE persona_id = ?", (persona_id,))
    }


def _subtree(conn: sqlite3.Connection, persona_id: str, path: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM categories WHERE persona_id = ? AND (path = ? OR path LIKE ? ESCAPE '\\')"
        " ORDER BY length(path) - length(replace(path, '/', ''))",
        (persona_id, path, path.replace("_", "\\_") + "/%"),
    ).fetchall()


def list_categories(conn: sqlite3.Connection, persona_id: str) -> list[dict[str, Any]]:
    """Return the tree; ``claim_count`` counts pending/approved claims directly in a node,
    ``total_claim_count`` includes its descendants."""
    rows = conn.execute(
        "SELECT c.id, c.parent_id, c.name, c.path, c.description,"
        " (SELECT COUNT(*) FROM claims k WHERE k.category_id = c.id"
        "  AND k.status IN ('pending', 'approved')) AS n"
        " FROM categories c WHERE c.persona_id = ? ORDER BY c.path",
        (persona_id,),
    ).fetchall()
    nodes: dict[str, dict[str, Any]] = {}
    roots: list[dict[str, Any]] = []
    for r in rows:
        nodes[r["id"]] = {
            "path": r["path"],
            "name": r["name"],
            "description": r["description"],
            "claim_count": r["n"],
            "total_claim_count": r["n"],
            "children": [],
        }
    for r in rows:
        node = nodes[r["id"]]
        parent = nodes.get(r["parent_id"]) if r["parent_id"] else None
        (parent["children"] if parent else roots).append(node)

    def total(node: dict[str, Any]) -> int:
        node["total_claim_count"] = node["claim_count"] + sum(total(c) for c in node["children"])
        return node["total_claim_count"]

    for root in roots:
        total(root)
    return roots


def create_category(
    conn: sqlite3.Connection,
    persona_id: str,
    path: str,
    description: str = "",
    *,
    create_parents: bool = False,
    origin: str = "owner",
) -> str:
    """Insert a category (inside the caller's transaction) and return its ID. Missing
    parents created with ``create_parents`` get the same ``origin``."""
    path = check_category_path(path)
    if origin not in ORIGINS:
        raise Invalid("unknown category origin", field="origin")
    existing = _row(conn, persona_id, path)
    if existing is not None:
        return existing["id"]
    parent = parent_path(path)
    parent_id = None
    if parent is not None:
        parent_row = _row(conn, persona_id, parent)
        if parent_row is None:
            if not create_parents:
                raise NotFound(f"parent category {parent!r} not found", path=parent)
            parent_id = create_category(
                conn, persona_id, parent, create_parents=True, origin=origin
            )
        else:
            parent_id = parent_row["id"]
    ts = now_iso()
    cat_id = new_id("cat")
    conn.execute(
        "INSERT INTO categories(id, persona_id, parent_id, name, description, path, origin,"
        " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            cat_id,
            persona_id,
            parent_id,
            path.rsplit("/", 1)[-1],
            description,
            path,
            origin,
            ts,
            ts,
        ),
    )
    return cat_id


def _public(row: sqlite3.Row) -> dict[str, Any]:
    return {"path": row["path"], "name": row["name"], "description": row["description"]}


def upsert_category(
    conn: sqlite3.Connection,
    persona_id: str,
    path: str,
    *,
    description: str | None = None,
    rename_to: str | None = None,
) -> dict[str, Any]:
    """Create a category (its parent must exist), or update its description and/or move it.

    ``rename_to`` moves the whole subtree; the new parent must exist and the result must stay
    within 3 levels. Categories created here are owner vocabulary for the guard (§4.4).
    """
    path = check_category_path(path)
    if rename_to is not None:
        rename_to = check_category_path(rename_to)
    check_description(description)
    with transaction(conn):
        row = _row(conn, persona_id, path)
        if row is None:
            if rename_to is not None:
                raise NotFound(f"category {path!r} not found", path=path)
            create_category(conn, persona_id, path, description or "", origin="owner")
            return {"category": _public(get_category(conn, persona_id, path)), "created": True}
        ts = now_iso()
        if description is not None:
            conn.execute(
                "UPDATE categories SET description = ?, updated_at = ? WHERE id = ?",
                (description, ts, row["id"]),
            )
        final_path = path
        if rename_to is not None and rename_to != path:
            _rename(conn, persona_id, row, rename_to)
            final_path = rename_to
        return {"category": _public(get_category(conn, persona_id, final_path)), "created": False}


def _rename(
    conn: sqlite3.Connection,
    persona_id: str,
    row: sqlite3.Row,
    new_path: str,
    *,
    record: bool = True,
) -> None:
    old_path = row["path"]
    new_path = check_category_path(new_path)
    if new_path.startswith(old_path + "/"):
        raise Invalid("a category cannot be moved into its own subtree", field="rename_to")
    if _row(conn, persona_id, new_path) is not None:
        raise Conflict(f"category {new_path!r} already exists", path=new_path)
    new_parent = parent_path(new_path)
    parent_id = None
    if new_parent is not None:
        parent_id = get_category(conn, persona_id, new_parent)["id"]
    subtree = _subtree(conn, persona_id, old_path)
    old_depth = old_path.count("/")
    deepest = max(r["path"].count("/") for r in subtree) - old_depth
    if new_path.count("/") + deepest + 1 > MAX_CATEGORY_DEPTH:
        raise Invalid(
            f"the moved subtree would be deeper than {MAX_CATEGORY_DEPTH} levels",
            field="rename_to",
        )
    ts = now_iso()
    renamed = []
    for r in subtree:
        moved = new_path + r["path"][len(old_path) :]
        conn.execute(
            "UPDATE categories SET path = ?, name = ?, updated_at = ? WHERE id = ?",
            (moved, moved.rsplit("/", 1)[-1], ts, r["id"]),
        )
        renamed.append({"from": r["path"], "to": moved})
    conn.execute("UPDATE categories SET parent_id = ? WHERE id = ?", (parent_id, row["id"]))
    for item in renamed:
        conn.execute(
            "UPDATE claims SET proposed_category = ? WHERE persona_id = ? AND proposed_category = ?",
            (item["to"], persona_id, item["from"]),
        )
    if record:
        record_change(conn, persona_id, "category_rename", {"renamed": renamed})


def merge_categories(
    conn: sqlite3.Connection, persona_id: str, sources: list[str], into: str
) -> dict[str, Any]:
    """Move all claims from ``sources`` (and their subtrees) into ``into``; delete the sources."""
    if not sources:
        raise Invalid("from must name at least one category", field="from")
    sources = [check_category_path(p) for p in sources]
    into = check_category_path(into)
    with transaction(conn):
        target = get_category(conn, persona_id, into)
        moved_claims: list[dict[str, str]] = []
        removed: list[dict[str, str]] = []
        for src in dict.fromkeys(sources):
            if src == into:
                raise Invalid("cannot merge a category into itself", field="from")
            if into.startswith(src + "/"):
                raise Invalid(f"{into!r} lies inside {src!r}", field="into")
            if any(r["path"] == src for r in removed):
                continue  # already merged as part of an ancestor named in `from`
            subtree = _subtree(conn, persona_id, src)
            if not subtree:
                raise NotFound(f"category {src!r} not found", path=src)
            for cat in subtree:
                for claim in conn.execute(
                    "SELECT id FROM claims WHERE category_id = ?", (cat["id"],)
                ).fetchall():
                    moved_claims.append({"id": claim["id"], "category": cat["path"]})
                conn.execute(
                    "UPDATE claims SET category_id = ?, updated_at = ? WHERE category_id = ?",
                    (target["id"], now_iso(), cat["id"]),
                )
            for cat in reversed(subtree):  # deepest first (parent_id is RESTRICT)
                conn.execute("DELETE FROM categories WHERE id = ?", (cat["id"],))
                removed.append(
                    {
                        "path": cat["path"],
                        "description": cat["description"],
                        "origin": cat["origin"],
                    }
                )
        change_id = record_change(
            conn,
            persona_id,
            "category_merge",
            {"into": into, "before": {"categories": removed, "claims": moved_claims}},
        )
    return {
        "into": into,
        "removed_categories": [r["path"] for r in removed],
        "moved_claims": len(moved_claims),
        "change_id": change_id,
    }


# --- organic creation from episodes (SPEC §4.4) ------------------------------------------


def guard_vocabulary(conn: sqlite3.Connection, persona_id: str) -> set[str]:
    """Names of seeded and owner-created categories: the persona's own vocabulary for the
    verbatim guard. Categories created from episodes are left out on purpose, so a client
    cannot whitelist a name by naming a category after it."""
    return {
        r["name"]
        for r in conn.execute(
            "SELECT name FROM categories WHERE persona_id = ? AND origin IN ('seed', 'owner')",
            (persona_id,),
        )
    }


def _ancestors(path: str) -> list[str]:
    """``a/b/c`` -> ``[a, a/b, a/b/c]``."""
    parts = path.split("/")
    return ["/".join(parts[: i + 1]) for i in range(len(parts))]


@dataclass
class CategoryResolver:
    """Resolves episode category paths for one job, creating missing ones within the
    limits (inside the caller's transaction). Holds paths and IDs only."""

    conn: sqlite3.Connection
    persona_id: str
    max_categories: int | None = None
    new_per_job: int | None = None
    ids: dict[str, str] = field(default_factory=dict)
    aliases: dict[str, str] = field(default_factory=dict)  # path -> category_id
    created: list[dict[str, str]] = field(default_factory=list)
    unmapped: int = 0
    _cache: dict[str, tuple[str, str, bool]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.ids = category_ids_by_path(self.conn, self.persona_id)
        self.aliases = {
            r["path"]: r["category_id"]
            for r in self.conn.execute(
                "SELECT path, category_id FROM category_aliases WHERE persona_id = ?",
                (self.persona_id,),
            )
        }

    def _path_of(self, category_id: str) -> str | None:
        return next((p for p, i in self.ids.items() if i == category_id), None)

    def _route_alias(self, path: str) -> str:
        """Longest alias prefix of ``path`` (a whole number of segments), rest appended."""
        for prefix in reversed(_ancestors(path)):
            target_id = self.aliases.get(prefix)
            if target_id is None:
                continue
            base = self._path_of(target_id)
            if base is not None:
                return base + path[len(prefix) :]
        return path

    def _fits(self, n: int) -> bool:
        if self.max_categories is not None and len(self.ids) + n > self.max_categories:
            return False
        return self.new_per_job is None or len(self.created) + n <= self.new_per_job

    def _fallback(self, path: str) -> str:
        """The deepest existing ancestor of ``path``, else ``other`` (created as a seed if
        it is missing; it does not count against the per-job limit)."""
        for prefix in reversed(_ancestors(path)[:MAX_CATEGORY_DEPTH]):
            if prefix in self.ids:
                return prefix
        if FALLBACK_CATEGORY not in self.ids:
            self.ids[FALLBACK_CATEGORY] = create_category(
                self.conn, self.persona_id, FALLBACK_CATEGORY, origin="seed"
            )
        return FALLBACK_CATEGORY

    def resolve(self, path: str) -> tuple[str, str]:
        """``path`` (already normalised) -> ``(category_id, resolved path)``. Counts an
        episode mapped elsewhere because of a limit in ``unmapped``."""
        hit = self._cache.get(path)
        if hit is None:
            hit = self._resolve(path)
            self._cache[path] = hit
        if hit[2]:
            self.unmapped += 1
        return hit[0], hit[1]

    def _resolve(self, path: str) -> tuple[str, str, bool]:
        if path in self.ids:
            return self.ids[path], path, False
        target = self._route_alias(path)
        if target in self.ids:
            return self.ids[target], target, False
        # Too deep after alias routing, or a path holding a phone-like identifier (the
        # guard's regex part, SPEC §2): never created.
        if target.count("/") >= MAX_CATEGORY_DEPTH or find_identifier(target) is not None:
            found = self._fallback(target)
            return self.ids[found], found, True
        missing = [p for p in _ancestors(target) if p not in self.ids]
        if not self._fits(len(missing)):
            found = self._fallback(target)
            return self.ids[found], found, True
        for p in missing:
            cat_id = create_category(self.conn, self.persona_id, p, origin="episode")
            self.ids[p] = cat_id
            self.created.append({"id": cat_id, "path": p})
        return self.ids[target], target, False

    def record(self, job_id: str | None) -> str | None:
        """One ``category_create`` change for everything this job created; its ID."""
        if not self.created:
            return None
        payload = {"categories": [{**c, "description": ""} for c in self.created]}
        change_id = record_change(self.conn, self.persona_id, "category_create", payload)
        if job_id is not None:
            self.conn.execute("UPDATE changes SET job_id = ? WHERE id = ?", (job_id, change_id))
        return change_id


def set_descriptions(
    conn: sqlite3.Connection, persona_id: str, descriptions: dict[str, str]
) -> int:
    """Fill empty descriptions of categories by ID (the job's description call); returns
    how many were written. Never overwrites a description the owner has set meanwhile."""
    n = 0
    ts = now_iso()
    with transaction(conn):
        for cat_id, text in descriptions.items():
            cur = conn.execute(
                "UPDATE categories SET description = ?, updated_at = ?"
                " WHERE id = ? AND persona_id = ? AND description = ''",
                (text, ts, cat_id, persona_id),
            )
            n += cur.rowcount
    return n


def undo_create(conn: sqlite3.Connection, persona_id: str, payload: dict[str, Any]) -> None:
    """Undo a ``category_create`` change (inside the caller's transaction): delete the
    categories it created, if none of them holds claims, analyzers or foreign children."""
    created = {c["id"] for c in payload.get("categories", ())}
    rows = [
        r
        for r in (
            conn.execute(
                "SELECT id, path FROM categories WHERE id = ? AND persona_id = ?",
                (cat_id, persona_id),
            ).fetchone()
            for cat_id in created
        )
        if r is not None
    ]
    # Analyzers (another subsystem) bind to a category by ID. LLM analyzers block the
    # undo; builtin rows (created lazily per scope, SPEC §7.2) go with the category.
    has_analyzers = bool(
        conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'analyzers'").fetchone()
    )
    for r in rows:
        if conn.execute(
            "SELECT 1 FROM claims WHERE category_id = ? LIMIT 1", (r["id"],)
        ).fetchone():
            raise Conflict("a created category holds claims now", path=r["path"])
        child = conn.execute("SELECT id FROM categories WHERE parent_id = ?", (r["id"],)).fetchall()
        if any(c["id"] not in created for c in child):
            raise Conflict("a created category has sub-categories now", path=r["path"])
        if (
            has_analyzers
            and conn.execute(
                "SELECT 1 FROM analyzers WHERE scope_kind = 'category' AND scope_id = ?"
                " AND origin <> 'builtin' LIMIT 1",
                (r["id"],),
            ).fetchone()
        ):
            raise Conflict("a created category has learned analyzers now", path=r["path"])
    for r in sorted(rows, key=lambda r: -r["path"].count("/")):  # deepest first
        if has_analyzers:
            conn.execute(
                "DELETE FROM analyzers WHERE scope_kind = 'category' AND scope_id = ?"
                " AND origin = 'builtin'",
                (r["id"],),
            )
        conn.execute("DELETE FROM categories WHERE id = ?", (r["id"],))
