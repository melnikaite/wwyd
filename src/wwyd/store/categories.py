# SPDX-License-Identifier: AGPL-3.0-or-later
"""Category tree (materialised paths, at most 3 levels)."""

from __future__ import annotations

import sqlite3
from typing import Any

from wwyd.db import transaction
from wwyd.errors import Conflict, Invalid, NotFound
from wwyd.ids import new_id, now_iso
from wwyd.store.common import (
    MAX_CATEGORY_DEPTH,
    check_category_path,
    check_description,
    parent_path,
    record_change,
)


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
) -> str:
    """Insert a category (inside the caller's transaction) and return its ID."""
    check_category_path(path)
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
            parent_id = create_category(conn, persona_id, parent, create_parents=True)
        else:
            parent_id = parent_row["id"]
    ts = now_iso()
    cat_id = new_id("cat")
    conn.execute(
        "INSERT INTO categories(id, persona_id, parent_id, name, description, path, created_at,"
        " updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (cat_id, persona_id, parent_id, path.rsplit("/", 1)[-1], description, path, ts, ts),
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
    within 3 levels.
    """
    check_category_path(path)
    check_description(description)
    with transaction(conn):
        row = _row(conn, persona_id, path)
        if row is None:
            if rename_to is not None:
                raise NotFound(f"category {path!r} not found", path=path)
            create_category(conn, persona_id, path, description or "")
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
    check_category_path(new_path)
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
    for p in [*sources, into]:
        check_category_path(p)
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
                removed.append({"path": cat["path"], "description": cat["description"]})
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
