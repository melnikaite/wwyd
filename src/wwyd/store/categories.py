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
from wwyd.store import changes
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
    ``total_claim_count`` includes its descendants, ``aliases`` lists the old paths that
    route to the node (after renames and merges)."""
    rows = conn.execute(
        "SELECT c.id, c.parent_id, c.name, c.path, c.description,"
        " (SELECT COUNT(*) FROM claims k WHERE k.category_id = c.id"
        "  AND k.status IN ('pending', 'approved')) AS n"
        " FROM categories c WHERE c.persona_id = ? ORDER BY c.path",
        (persona_id,),
    ).fetchall()
    aliases: dict[str, list[str]] = {}
    for a in conn.execute(
        "SELECT path, category_id FROM category_aliases WHERE persona_id = ? ORDER BY path",
        (persona_id,),
    ):
        aliases.setdefault(a["category_id"], []).append(a["path"])
    nodes: dict[str, dict[str, Any]] = {}
    roots: list[dict[str, Any]] = []
    for r in rows:
        nodes[r["id"]] = {
            "path": r["path"],
            "name": r["name"],
            "description": r["description"],
            "aliases": aliases.get(r["id"], []),
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
    # A real category at an alias path wins at resolution anyway; keep the table clean.
    conn.execute(
        "DELETE FROM category_aliases WHERE persona_id = ? AND path = ?", (persona_id, path)
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


def _alias_rows(
    conn: sqlite3.Connection,
    persona_id: str,
    *,
    paths: list[str] = (),  # type: ignore[assignment]
    category_ids: list[str] = (),  # type: ignore[assignment]
) -> list[dict[str, str]]:
    """Alias rows at ``paths`` or pointing to ``category_ids`` (a change snapshot)."""
    out: dict[str, dict[str, str]] = {}
    for r in conn.execute(
        "SELECT path, category_id FROM category_aliases WHERE persona_id = ? ORDER BY path",
        (persona_id,),
    ):
        if r["path"] in paths or r["category_id"] in category_ids:
            out[r["path"]] = {"path": r["path"], "category_id": r["category_id"]}
    return list(out.values())


def _restore_aliases(
    conn: sqlite3.Connection, persona_id: str, paths: list[str], before: list[dict[str, str]]
) -> None:
    """Drop every alias at ``paths`` and at the snapshot's paths, then re-insert the
    snapshot (aliases whose category is gone are left out)."""
    for path in {*paths, *(a["path"] for a in before)}:
        conn.execute(
            "DELETE FROM category_aliases WHERE persona_id = ? AND path = ?", (persona_id, path)
        )
    for a in before:
        conn.execute(
            "INSERT OR IGNORE INTO category_aliases(persona_id, path, category_id)"
            " SELECT ?, ?, id FROM categories WHERE id = ? AND persona_id = ?"
            " AND NOT EXISTS (SELECT 1 FROM categories WHERE persona_id = ? AND path = ?)",
            (persona_id, a["path"], a["category_id"], persona_id, persona_id, a["path"]),
        )


def _set_aliases(conn: sqlite3.Connection, persona_id: str, mapping: dict[str, str]) -> None:
    """``old path -> category_id``; a path that is a real category again is skipped."""
    for path, cat_id in mapping.items():
        if _row(conn, persona_id, path) is not None:
            continue
        conn.execute(
            "INSERT OR REPLACE INTO category_aliases(persona_id, path, category_id)"
            " VALUES (?, ?, ?)",
            (persona_id, path, cat_id),
        )


def _rename(
    conn: sqlite3.Connection,
    persona_id: str,
    row: sqlite3.Row,
    new_path: str,
    *,
    record: bool = True,
) -> None:
    """Move a subtree. With ``record`` (an owner rename) every old path becomes an alias of
    its moved category, aliases at the new paths are dropped, and a ``category_rename``
    change snapshots the aliases; the undo passes ``record=False`` and restores them."""
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
        renamed.append({"from": r["path"], "to": moved, "id": r["id"]})
    aliases_before = _alias_rows(
        conn, persona_id, paths=[x["from"] for x in renamed] + [x["to"] for x in renamed]
    )
    for item in renamed:
        conn.execute(
            "UPDATE categories SET path = ?, name = ?, updated_at = ? WHERE id = ?",
            (item["to"], item["to"].rsplit("/", 1)[-1], ts, item["id"]),
        )
    conn.execute("UPDATE categories SET parent_id = ? WHERE id = ?", (parent_id, row["id"]))
    for item in renamed:
        conn.execute(
            "UPDATE claims SET proposed_category = ? WHERE persona_id = ? AND proposed_category = ?",
            (item["to"], persona_id, item["from"]),
        )
    if record:
        for item in renamed:
            conn.execute(
                "DELETE FROM category_aliases WHERE persona_id = ? AND path = ?",
                (persona_id, item["to"]),
            )
        _set_aliases(conn, persona_id, {x["from"]: x["id"] for x in renamed})
        record_change(
            conn,
            persona_id,
            "category_rename",
            {
                "renamed": renamed,
                "aliases_added": [x["from"] for x in renamed],
                "aliases_before": aliases_before,
            },
        )


def undo_rename(conn: sqlite3.Connection, persona_id: str, payload: dict[str, Any]) -> None:
    renamed = payload["renamed"]
    if not renamed:
        return
    root = min(renamed, key=lambda r: r["to"].count("/"))
    row = get_category(conn, persona_id, root["to"])
    _rename(conn, persona_id, row, root["from"], record=False)
    paths = [x["from"] for x in renamed] + [x["to"] for x in renamed]
    _restore_aliases(conn, persona_id, paths, payload.get("aliases_before", []))


# --- merge (owner and nightly, SPEC §6.3, §10.6) --------------------------------------------


def _dicts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(r) for r in rows]


def _in(ids: list[str]) -> str:
    return ",".join("?" * len(ids)) or "NULL"


def _rekey(key: str | None, removed: set[str], target_id: str) -> str | None:
    """``builtin:<name>:<removed id>[:<facet>]`` -> the same key under the target."""
    if not key or not key.startswith("builtin:"):
        return None
    parts = key.split(":", 3)
    if len(parts) < 3 or parts[2] not in removed:
        return None
    parts[2] = target_id
    return ":".join(parts)


def merge_into(
    conn: sqlite3.Connection,
    persona_id: str,
    sources: list[str],
    into: str,
    *,
    reason: str | None = None,
) -> dict[str, Any]:
    """Merge ``sources`` (with their subtrees) into ``into`` inside the caller's transaction
    and record one ``category_merge`` change (SPEC §10.6).

    Claims move; builtin keyed claims are re-keyed to the target's key, or archived
    (``merged``) into the target's live claim with that key. Analyzers move: a same-name
    builtin adds its state into the target's row (summed per month/metric/key) and goes;
    the rest change scope. Labels move; aliases pointing at the sources are re-pointed and
    every removed path becomes an alias of the target. The payload snapshots categories,
    claims, analyzers, state, labels and aliases for :func:`undo_merge`."""
    target = get_category(conn, persona_id, into)
    removed_rows: list[sqlite3.Row] = []
    for src in dict.fromkeys(sources):
        if src == into:
            raise Invalid("cannot merge a category into itself", field="from")
        if into.startswith(src + "/"):
            raise Invalid(f"{into!r} lies inside {src!r}", field="into")
        if any(r["path"] == src for r in removed_rows):
            continue  # already merged as part of an ancestor named in `from`
        subtree = _subtree(conn, persona_id, src)
        if not subtree:
            raise NotFound(f"category {src!r} not found", path=src)
        removed_rows.extend(r for r in subtree if r["id"] not in {x["id"] for x in removed_rows})
    removed_rows.sort(key=lambda r: r["path"].count("/"))  # parents first
    removed = [r["id"] for r in removed_rows]
    removed_set = set(removed)
    marks = _in(removed)
    tid = target["id"]
    ts = now_iso()

    claim_rows = conn.execute(
        f"SELECT id, key, status FROM claims WHERE category_id IN ({marks}) ORDER BY seq",
        removed,
    ).fetchall()
    claim_ids = [r["id"] for r in claim_rows]
    src_an = conn.execute(
        f"SELECT * FROM analyzers WHERE persona_id = ? AND scope_kind = 'category'"
        f" AND scope_id IN ({marks}) ORDER BY seq",
        [persona_id, *removed],
    ).fetchall()
    names = sorted({a["name"] for a in src_an if a["origin"] == "builtin"})
    tgt_an = conn.execute(
        f"SELECT * FROM analyzers WHERE persona_id = ? AND scope_kind = 'category'"
        f" AND scope_id = ? AND origin = 'builtin' AND name IN ({_in(names)})",
        [persona_id, tid, *names],
    ).fetchall()
    an_ids = [a["id"] for a in (*src_an, *tgt_an)]
    before = {
        "categories": _dicts(removed_rows),
        "claims": changes.snapshot(conn, claim_ids),
        "analyzers": _dicts([*src_an, *tgt_an]),
        "state": _dicts(
            conn.execute(
                f"SELECT * FROM analyzer_state WHERE analyzer_id IN ({_in(an_ids)})", an_ids
            ).fetchall()
        ),
        "labels": _dicts(
            conn.execute(
                f"SELECT * FROM category_labels WHERE category_id IN ({_in([*removed, tid])})",
                [*removed, tid],
            ).fetchall()
        ),
        "aliases": _alias_rows(
            conn,
            persona_id,
            paths=[r["path"] for r in removed_rows],
            category_ids=removed,
        ),
    }

    # Claims, then keyed claims re-keyed or archived into the target's live one.
    conn.execute(
        f"UPDATE claims SET category_id = ?, updated_at = ? WHERE category_id IN ({marks})",
        [tid, ts, *removed],
    )
    for c in claim_rows:
        new_key = _rekey(c["key"], removed_set, tid)
        if new_key is None:
            continue
        live = conn.execute(
            "SELECT id FROM claims WHERE persona_id = ? AND key = ? AND status <> 'archived'"
            " AND id <> ?",
            (persona_id, new_key, c["id"]),
        ).fetchone()
        if live is not None and c["status"] != "archived":
            conn.execute(
                "UPDATE claims SET status = 'archived', archive_reason = 'merged',"
                " superseded_by = ?, archived_at = ?, updated_at = ? WHERE id = ?",
                (live["id"], ts, ts, c["id"]),
            )
        elif live is None:
            conn.execute("UPDATE claims SET key = ? WHERE id = ?", (new_key, c["id"]))
    # Analyzers: same-name builtins sum into the target's row; the rest change scope.
    for a in src_an:
        existing = None
        if a["origin"] == "builtin":
            existing = conn.execute(
                "SELECT id FROM analyzers WHERE persona_id = ? AND origin = 'builtin'"
                " AND name = ? AND scope_kind = 'category' AND scope_id = ?",
                (persona_id, a["name"], tid),
            ).fetchone()
        if existing is None:
            conn.execute("UPDATE analyzers SET scope_id = ? WHERE id = ?", (tid, a["id"]))
            continue
        conn.execute(
            "INSERT INTO analyzer_state(analyzer_id, month, metric, key, value)"
            " SELECT ?, month, metric, key, value FROM analyzer_state WHERE analyzer_id = ?"
            " ON CONFLICT(analyzer_id, month, metric, key)"
            " DO UPDATE SET value = analyzer_state.value + excluded.value",
            (existing["id"], a["id"]),
        )
        conn.execute("DELETE FROM analyzers WHERE id = ?", (a["id"],))
    conn.execute(
        f"INSERT OR IGNORE INTO category_labels(persona_id, category_id, field, label)"
        f" SELECT persona_id, ?, field, label FROM category_labels WHERE category_id IN ({marks})",
        [tid, *removed],
    )
    conn.execute(
        f"UPDATE category_aliases SET category_id = ? WHERE category_id IN ({marks})",
        [tid, *removed],
    )
    for r in reversed(removed_rows):  # deepest first (parent_id is RESTRICT)
        conn.execute("DELETE FROM categories WHERE id = ?", (r["id"],))
    _set_aliases(conn, persona_id, {r["path"]: tid for r in removed_rows})
    payload = {
        "into": into,
        "into_id": tid,
        "claims": claim_ids,
        "aliases_added": [r["path"] for r in removed_rows],
        "before": before,
    }
    change_id = record_change(conn, persona_id, "category_merge", payload)
    if reason is not None:
        conn.execute("UPDATE changes SET reason = ? WHERE id = ?", (reason, change_id))
    return {
        "into": into,
        "removed_categories": [r["path"] for r in removed_rows],
        "moved_claims": len(claim_ids),
        "change_id": change_id,
    }


def merge_categories(
    conn: sqlite3.Connection, persona_id: str, sources: list[str], into: str
) -> dict[str, Any]:
    """Owner merge: everything in ``sources`` (and their subtrees) moves into ``into``; the
    sources are deleted and their paths become aliases (see :func:`merge_into`)."""
    if not sources:
        raise Invalid("from must name at least one category", field="from")
    sources = [check_category_path(p) for p in sources]
    into = check_category_path(into)
    with transaction(conn):
        return merge_into(conn, persona_id, sources, into)


def _insert_category(conn: sqlite3.Connection, persona_id: str, cat: dict[str, Any]) -> None:
    if _row(conn, persona_id, cat["path"]) is not None:
        raise Conflict(f"category {cat['path']!r} exists again", path=cat["path"])
    parent = parent_path(cat["path"])
    parent_id = None
    if parent is not None:
        parent_id = get_category(conn, persona_id, parent)["id"]
    cols = {k: v for k, v in cat.items() if k not in ("parent_id", "persona_id")}
    cols.update(persona_id=persona_id, parent_id=parent_id)
    conn.execute(
        f"INSERT INTO categories({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
        list(cols.values()),
    )


def _insert_rows(conn: sqlite3.Connection, table: str, rows: list[dict[str, Any]]) -> None:
    for row in rows:
        conn.execute(
            f"INSERT OR REPLACE INTO {table}({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
            list(row.values()),
        )


def undo_merge(conn: sqlite3.Connection, persona_id: str, payload: dict[str, Any]) -> None:
    """Restore a ``category_merge`` snapshot (inside the caller's transaction)."""
    before = payload["before"]
    if "analyzers" not in before:  # pre-M7 payload: paths and claim IDs only
        for cat in reversed(before["categories"]):
            if _row(conn, persona_id, cat["path"]) is not None:
                raise Conflict(f"category {cat['path']!r} exists again", path=cat["path"])
            create_category(
                conn,
                persona_id,
                cat["path"],
                cat.get("description") or "",
                origin=cat.get("origin", "owner"),
            )
        ids = category_ids_by_path(conn, persona_id)
        for claim in before["claims"]:
            conn.execute(
                "UPDATE claims SET category_id = ?, updated_at = ? WHERE persona_id = ? AND id = ?",
                (ids[claim["category"]], now_iso(), persona_id, claim["id"]),
            )
        return
    for path in payload.get("aliases_added", []):
        conn.execute(
            "DELETE FROM category_aliases WHERE persona_id = ? AND path = ?", (persona_id, path)
        )
    for cat in before["categories"]:  # parents first, original IDs
        _insert_category(conn, persona_id, cat)
    for claim_id, snap in before["claims"].items():
        changes._restore(conn, persona_id, claim_id, snap)
    for a in before["analyzers"]:
        conn.execute("DELETE FROM analyzers WHERE id = ? AND persona_id = ?", (a["id"], persona_id))
    _insert_rows(conn, "analyzers", before["analyzers"])
    _insert_rows(conn, "analyzer_state", before["state"])
    conn.execute(
        "DELETE FROM category_labels WHERE persona_id = ? AND category_id = ?",
        (persona_id, payload["into_id"]),
    )
    _insert_rows(conn, "category_labels", before["labels"])
    _restore_aliases(conn, persona_id, payload.get("aliases_added", []), before["aliases"])


# --- split (nightly, SPEC §10.6) -----------------------------------------------------------


def split_into(
    conn: sqlite3.Connection,
    persona_id: str,
    parent: str,
    groups: list[dict[str, Any]],
    *,
    reason: str | None = None,
) -> dict[str, Any]:
    """Move claims of ``parent`` into sub-categories (inside the caller's transaction).

    ``groups``: ``[{"name": segment, "description": str, "claims": [claim ids]}]``, already
    validated. Missing sub-categories are created with origin ``episode``; analyzers stay
    bound to the parent. One ``category_split`` change."""
    row = get_category(conn, persona_id, parent)
    created: list[dict[str, str]] = []
    moves: dict[str, str] = {}
    for g in groups:
        path = f"{parent}/{g['name']}"
        existing = _row(conn, persona_id, path)
        if existing is None:
            cat_id = create_category(
                conn, persona_id, path, g.get("description") or "", origin="episode"
            )
            created.append({"id": cat_id, "path": path})
        else:
            cat_id = existing["id"]
        for claim_id in g["claims"]:
            moves[claim_id] = cat_id
    ids = list(moves)
    before = changes.snapshot(conn, ids)
    ts = now_iso()
    for claim_id, cat_id in moves.items():
        conn.execute(
            "UPDATE claims SET category_id = ?, updated_at = ?"
            " WHERE persona_id = ? AND id = ? AND category_id = ?",
            (cat_id, ts, persona_id, claim_id, row["id"]),
        )
    change_id = record_change(
        conn,
        persona_id,
        "category_split",
        {"category": parent, "claims": ids, "created": created, "before": before},
    )
    if reason is not None:
        conn.execute("UPDATE changes SET reason = ? WHERE id = ?", (reason, change_id))
    return {"change_id": change_id, "created": [c["path"] for c in created], "claims": ids}


def undo_split(conn: sqlite3.Connection, persona_id: str, payload: dict[str, Any]) -> None:
    """Move the claims back and delete the created categories that are empty now."""
    for claim_id, snap in payload["before"].items():
        changes._restore(conn, persona_id, claim_id, snap)
    for c in sorted(payload.get("created", []), key=lambda c: -c["path"].count("/")):
        cat_id = c["id"]
        busy = (
            conn.execute("SELECT 1 FROM claims WHERE category_id = ? LIMIT 1", (cat_id,)).fetchone()
            or conn.execute(
                "SELECT 1 FROM categories WHERE parent_id = ? LIMIT 1", (cat_id,)
            ).fetchone()
            or conn.execute(
                "SELECT 1 FROM analyzers WHERE scope_kind = 'category' AND scope_id = ?"
                " AND origin <> 'builtin' LIMIT 1",
                (cat_id,),
            ).fetchone()
        )
        if busy:
            continue
        conn.execute(
            "DELETE FROM analyzers WHERE scope_kind = 'category' AND scope_id = ?"
            " AND origin = 'builtin'",
            (cat_id,),
        )
        conn.execute("DELETE FROM categories WHERE id = ? AND persona_id = ?", (cat_id, persona_id))


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
