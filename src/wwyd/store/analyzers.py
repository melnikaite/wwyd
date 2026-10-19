# SPDX-License-Identifier: AGPL-3.0-or-later
"""Analyzer rows (SPEC §4.6, §6.7, §7.2): lazy builtin rows, listing, status changes.

The scope is polymorphic (``scope_kind`` category|audience + ``scope_id``), not an FK, so it
is resolved here: a scope whose category or audience no longer exists shows ``scope: null``.

Status rules: builtins may be ``active`` or ``disabled`` (they are shipped code, so there is
nothing to retire: retiring is ``invalid``); LLM analyzers may move between ``candidate``,
``active`` and ``disabled`` and to ``retired``, which is terminal and clears the source
while keeping the statement as negative memory (§7.4).
"""

from __future__ import annotations

import sqlite3
from typing import Any

from wwyd.analyzers.state import summary
from wwyd.db import transaction
from wwyd.errors import Conflict, Invalid, NotFound
from wwyd.ids import new_id, now_iso
from wwyd.store.common import check_limit, decode_cursor, encode_cursor

STATUSES = ("candidate", "active", "disabled", "retired")
SETTABLE = ("active", "disabled", "retired")
SCOPE_KINDS = ("category", "audience")

_SELECT = (
    "SELECT a.*, CASE a.scope_kind"
    " WHEN 'category' THEN (SELECT path FROM categories WHERE id = a.scope_id)"
    " ELSE (SELECT name FROM audiences WHERE id = a.scope_id) END AS scope"
    " FROM analyzers a"
)


def ensure_builtin(
    conn: sqlite3.Connection,
    persona_id: str,
    *,
    name: str,
    scope_kind: str,
    scope_id: str,
    kind: str,
    statement_template: str,
) -> sqlite3.Row:
    """Return the builtin row for (name, scope), creating it (``active``) on first use."""
    row = conn.execute(
        "SELECT * FROM analyzers WHERE persona_id = ? AND origin = 'builtin' AND name = ?"
        " AND scope_kind = ? AND scope_id = ?",
        (persona_id, name, scope_kind, scope_id),
    ).fetchone()
    if row is not None:
        return row
    analyzer_id = new_id("anl")
    conn.execute(
        "INSERT INTO analyzers(id, persona_id, scope_kind, scope_id, origin, name,"
        " statement_template, kind, status, created_at)"
        " VALUES (?, ?, ?, ?, 'builtin', ?, ?, ?, 'active', ?)",
        (analyzer_id, persona_id, scope_kind, scope_id, name, statement_template, kind, now_iso()),
    )
    return conn.execute("SELECT * FROM analyzers WHERE id = ?", (analyzer_id,)).fetchone()


def record_run(
    conn: sqlite3.Connection, analyzer_id: str, *, failed: bool, claim_id: str | None
) -> None:
    conn.execute(
        "UPDATE analyzers SET runs = runs + 1, failures = failures + ?, last_run_at = ?,"
        " claim_id = COALESCE(?, claim_id) WHERE id = ?",
        (int(failed), now_iso(), claim_id, analyzer_id),
    )


def _public(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "origin": row["origin"],
        "name": row["name"],
        "scope": row["scope"],
        "scope_kind": row["scope_kind"],
        "kind": row["kind"],
        "status": row["status"],
        "runs": row["runs"],
        "failures": row["failures"],
        "last_run_at": row["last_run_at"],
        "claim_id": row["claim_id"],
    }


def _row(conn: sqlite3.Connection, persona_id: str, analyzer_id: str) -> sqlite3.Row:
    row = conn.execute(
        f"{_SELECT} WHERE a.persona_id = ? AND a.id = ?", (persona_id, analyzer_id)
    ).fetchone()
    if row is None:
        raise NotFound(f"analyzer {analyzer_id!r} not found", id=analyzer_id)
    return row


def _resolve_scope(conn: sqlite3.Connection, persona_id: str, scope: str) -> list[tuple[str, str]]:
    """A scope string names a category path, an audience, or both (``family`` may be
    either); every match is returned."""
    out = []
    cat = conn.execute(
        "SELECT id FROM categories WHERE persona_id = ? AND path = ?", (persona_id, scope)
    ).fetchone()
    if cat is not None:
        out.append(("category", cat["id"]))
    aud = conn.execute(
        "SELECT id FROM audiences WHERE persona_id = ? AND name = ?", (persona_id, scope)
    ).fetchone()
    if aud is not None:
        out.append(("audience", aud["id"]))
    if not out:
        raise NotFound(f"scope {scope!r} is neither a category nor an audience", scope=scope)
    return out


def list_analyzers(
    conn: sqlite3.Connection,
    persona_id: str,
    *,
    scope: str | None = None,
    status: str | None = None,
    limit: int | None = None,
    cursor: str | None = None,
) -> dict[str, Any]:
    limit = check_limit(limit)
    where, args = ["a.persona_id = ?", "a.seq > ?"], [persona_id, decode_cursor(cursor)]
    if scope is not None:
        matches = _resolve_scope(conn, persona_id, scope)
        where.append(
            "(" + " OR ".join("(a.scope_kind = ? AND a.scope_id = ?)" for _ in matches) + ")"
        )
        args += [x for m in matches for x in m]
    if status is not None:
        if status not in STATUSES:
            raise Invalid(f"status must be one of {', '.join(STATUSES)}", field="status")
        where.append("a.status = ?")
        args.append(status)
    rows = conn.execute(
        f"{_SELECT} WHERE {' AND '.join(where)} ORDER BY a.seq LIMIT ?", [*args, limit + 1]
    ).fetchall()
    result: dict[str, Any] = {"analyzers": [_public(r) for r in rows[:limit]]}
    if len(rows) > limit:
        result["next_cursor"] = encode_cursor(rows[limit - 1]["seq"])
    return result


def builtin_claim_ids(conn: sqlite3.Connection, persona_id: str, row: sqlite3.Row) -> list[str]:
    """Live claims keyed ``builtin:<name>:<scope_id>`` or ``…:<facet>``."""
    if row["origin"] != "builtin":
        return [row["claim_id"]] if row["claim_id"] else []
    key = f"builtin:{row['name']}:{row['scope_id']}"
    return [
        r["id"]
        for r in conn.execute(
            "SELECT id FROM claims WHERE persona_id = ? AND status <> 'archived'"
            " AND (key = ? OR substr(key, 1, ?) = ?) ORDER BY key",
            (persona_id, key, len(key) + 1, key + ":"),
        )
    ]


def get_analyzer(conn: sqlite3.Connection, persona_id: str, analyzer_id: str) -> dict[str, Any]:
    row = _row(conn, persona_id, analyzer_id)
    out = _public(row)
    out["statement_template"] = row["statement_template"]
    out["created_at"] = row["created_at"]
    if row["origin"] == "llm":
        out["source"] = row["source"]
    out["claim_ids"] = builtin_claim_ids(conn, persona_id, row)
    out["state"] = summary(conn, row["id"])
    return out


def update_analyzer(
    conn: sqlite3.Connection, persona_id: str, analyzer_id: str, *, status: str
) -> dict[str, Any]:
    if status not in SETTABLE:
        raise Invalid(f"status must be one of {', '.join(SETTABLE)}", field="status")
    with transaction(conn):
        row = _row(conn, persona_id, analyzer_id)
        if row["origin"] == "builtin" and status == "retired":
            raise Invalid("builtin analyzers cannot be retired; disable them", field="status")
        if row["status"] == "retired" and status != "retired":
            raise Conflict(f"analyzer {analyzer_id!r} is retired", id=analyzer_id)
        if status == "retired":
            conn.execute(
                "UPDATE analyzers SET status = 'retired', source = '' WHERE id = ?", (row["id"],)
            )
        else:
            conn.execute("UPDATE analyzers SET status = ? WHERE id = ?", (status, row["id"]))
        return {"analyzer": _public(_row(conn, persona_id, analyzer_id))}
