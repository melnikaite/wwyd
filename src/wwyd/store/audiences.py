# SPDX-License-Identifier: AGPL-3.0-or-later
"""Audiences: owner-defined groups a claim applies to."""

from __future__ import annotations

import sqlite3
from typing import Any

from wwyd.db import transaction
from wwyd.errors import Conflict, Invalid, NotFound
from wwyd.ids import new_id, now_iso
from wwyd.store.common import check_audience_name, check_description

_LIVE = "c.status IN ('pending', 'approved')"


def _row(conn: sqlite3.Connection, persona_id: str, name: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM audiences WHERE persona_id = ? AND name = ?", (persona_id, name)
    ).fetchone()


def get_audience(conn: sqlite3.Connection, persona_id: str, name: str) -> sqlite3.Row:
    row = _row(conn, persona_id, name)
    if row is None:
        raise NotFound(f"audience {name!r} not found", name=name)
    return row


def audience_ids_by_name(conn: sqlite3.Connection, persona_id: str) -> dict[str, str]:
    return {
        r["name"]: r["id"]
        for r in conn.execute("SELECT id, name FROM audiences WHERE persona_id = ?", (persona_id,))
    }


def _public(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    count = conn.execute(
        f"SELECT COUNT(*) FROM claim_audiences ca JOIN claims c ON c.id = ca.claim_id"
        f" WHERE ca.audience_id = ? AND {_LIVE}",
        (row["id"],),
    ).fetchone()[0]
    return {
        "name": row["name"],
        "description": row["description"],
        "is_default": bool(row["is_default"]),
        "claim_count": count,
    }


def list_audiences(conn: sqlite3.Connection, persona_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM audiences WHERE persona_id = ? ORDER BY is_default DESC, name",
        (persona_id,),
    ).fetchall()
    return [_public(conn, r) for r in rows]


def _set_default(conn: sqlite3.Connection, persona_id: str, audience_id: str) -> None:
    conn.execute(
        "UPDATE audiences SET is_default = 0 WHERE persona_id = ? AND is_default = 1",
        (persona_id,),
    )
    conn.execute("UPDATE audiences SET is_default = 1 WHERE id = ?", (audience_id,))


def upsert_audience(
    conn: sqlite3.Connection,
    persona_id: str,
    name: str,
    *,
    description: str | None = None,
    is_default: bool | None = None,
    rename_to: str | None = None,
) -> dict[str, Any]:
    """Create or update an audience; returns ``{audience, created}``."""
    check_audience_name(name)
    check_description(description)
    if rename_to is not None:
        check_audience_name(rename_to)
    ts = now_iso()
    with transaction(conn):
        row = _row(conn, persona_id, name)
        created = row is None
        if created:
            if rename_to is not None:
                raise NotFound(f"audience {name!r} not found", name=name)
            audience_id = new_id("aud")
            conn.execute(
                "INSERT INTO audiences(id, persona_id, name, description, is_default, created_at,"
                " updated_at) VALUES (?, ?, ?, ?, 0, ?, ?)",
                (audience_id, persona_id, name, description or "", ts, ts),
            )
            if is_default:
                _set_default(conn, persona_id, audience_id)
        else:
            audience_id = row["id"]
            if is_default is False and row["is_default"]:
                raise Invalid(
                    "a persona always has one default audience; make another one default instead",
                    field="is_default",
                )
            if rename_to is not None and rename_to != name:
                if _row(conn, persona_id, rename_to) is not None:
                    raise Conflict(f"audience {rename_to!r} already exists", name=rename_to)
                conn.execute("UPDATE audiences SET name = ? WHERE id = ?", (rename_to, audience_id))
            if description is not None:
                conn.execute(
                    "UPDATE audiences SET description = ? WHERE id = ?", (description, audience_id)
                )
            if is_default and not row["is_default"]:
                _set_default(conn, persona_id, audience_id)
            conn.execute("UPDATE audiences SET updated_at = ? WHERE id = ?", (ts, audience_id))
        final = conn.execute("SELECT * FROM audiences WHERE id = ?", (audience_id,)).fetchone()
        return {"audience": _public(conn, final), "created": created}


def delete_audience(
    conn: sqlite3.Connection, persona_id: str, name: str, *, reassign_to: str | None = None
) -> dict[str, Any]:
    """Delete a non-default audience.

    Claims that applied only to it are moved to ``reassign_to``; without it the call fails
    with ``conflict`` and the number of such claims.
    """
    with transaction(conn):
        row = get_audience(conn, persona_id, name)
        if row["is_default"]:
            raise Conflict("the default audience cannot be deleted", name=name)
        target = None
        if reassign_to is not None:
            if reassign_to == name:
                raise Invalid(
                    "reassign_to must differ from the deleted audience", field="reassign_to"
                )
            target = get_audience(conn, persona_id, reassign_to)
        only_here = [
            r[0]
            for r in conn.execute(
                "SELECT ca.claim_id FROM claim_audiences ca JOIN claims c ON c.id = ca.claim_id"
                " WHERE ca.audience_id = ? AND c.audiences_all = 0 AND NOT EXISTS ("
                "  SELECT 1 FROM claim_audiences o WHERE o.claim_id = ca.claim_id"
                "  AND o.audience_id <> ca.audience_id)",
                (row["id"],),
            )
        ]
        if only_here and target is None:
            raise Conflict(
                f"{len(only_here)} claims apply only to audience {name!r}; pass reassign_to",
                claim_count=len(only_here),
            )
        if target is not None:
            conn.executemany(
                "INSERT OR IGNORE INTO claim_audiences(claim_id, audience_id) VALUES (?, ?)",
                [(cid, target["id"]) for cid in only_here],
            )
        conn.execute("DELETE FROM audiences WHERE id = ?", (row["id"],))
    return {"deleted": name, "reassigned": len(only_here)}
