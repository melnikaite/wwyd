# SPDX-License-Identifier: AGPL-3.0-or-later
"""The change log (SPEC §4.6, §10): every compaction write, proposals, undo.

Payload of a claim change::

    {"claims": [clm_…], "plan": {...}, "before": {id: snapshot}, "after": {id: snapshot}}

Snapshots are full claim rows (derived claims, never raw input) plus ``audience_ids``.
A ``plan`` describes the operation so a ``proposed`` change can be (re)computed against the
claims' state at approval time:

- ``{"kind": "merge", "keep": id, "drop": id}`` — drop is archived (``merged``) into keep;
  evidence is summed unless keep is a rejected claim (negative memory stays text-only).
- ``{"kind": "supersede", "winner": id, "loser": id}``
- ``{"kind": "archive", "claim": id, "reason": "decayed"|"budget", "confidence"?: float}``
- ``{"kind": "confidence", "values": {id: float}}`` — decay step, sets ``decayed_at``.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Sequence
from typing import Any

from wwyd.compact.constants import absorbed_confidence
from wwyd.db import transaction
from wwyd.errors import Conflict, Invalid, NotFound
from wwyd.ids import new_id, now_iso
from wwyd.store.common import check_limit, decode_cursor, dumps, encode_cursor, normalize_text

OPS = (
    "merge",
    "absorb",
    "supersede",
    "decay",
    "archive_budget",
    "purge",
    "restore",
    "category_create",
    "category_merge",
    "category_rename",
    "category_split",
)
STATES = ("applied", "proposed", "dismissed", "undone", "expired")
# Tree changes that must be undone newest first. ``category_create`` is not among them:
# its undo checks the live state of the categories it created instead (SPEC §4.4).
_CATEGORY_OPS = ("category_merge", "category_rename", "category_split")
_SKIP_COLUMNS = {"seq", "norm_text"}


# --- snapshots --------------------------------------------------------------------------


def snapshot(conn: sqlite3.Connection, claim_ids: Iterable[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for claim_id in dict.fromkeys(claim_ids):
        row = conn.execute("SELECT * FROM claims WHERE id = ?", (claim_id,)).fetchone()
        if row is None:
            out[claim_id] = None
            continue
        snap = {k: row[k] for k in row.keys() if k not in _SKIP_COLUMNS}  # noqa: SIM118
        snap["audience_ids"] = [
            r[0]
            for r in conn.execute(
                "SELECT audience_id FROM claim_audiences WHERE claim_id = ? ORDER BY audience_id",
                (claim_id,),
            )
        ]
        out[claim_id] = snap
    return out


def _restore(conn: sqlite3.Connection, persona_id: str, claim_id: str, snap: Any) -> None:
    if snap is None:  # the change created this claim
        conn.execute("DELETE FROM claims WHERE persona_id = ? AND id = ?", (persona_id, claim_id))
        return
    cols = {k: v for k, v in snap.items() if k not in ("audience_ids", "id", "persona_id")}
    cols["norm_text"] = normalize_text(cols["text"])
    try:
        cur = conn.execute(
            f"UPDATE claims SET {', '.join(f'{k} = ?' for k in cols)}"
            " WHERE persona_id = ? AND id = ?",
            [*cols.values(), persona_id, claim_id],
        )
        if cur.rowcount == 0:
            raise Conflict(f"claim {claim_id!r} no longer exists", id=claim_id)
        conn.execute("DELETE FROM claim_audiences WHERE claim_id = ?", (claim_id,))
        conn.executemany(
            "INSERT INTO claim_audiences(claim_id, audience_id) VALUES (?, ?)",
            [(claim_id, a) for a in snap["audience_ids"]],
        )
    except sqlite3.IntegrityError:
        raise Conflict(
            f"claim {claim_id!r} cannot be restored (key, category or audience changed)",
            id=claim_id,
        ) from None


# --- recording --------------------------------------------------------------------------


def record(
    conn: sqlite3.Connection,
    persona_id: str,
    op: str,
    payload: dict[str, Any],
    *,
    state: str = "applied",
    reason: str | None = None,
    job_id: str | None = None,
) -> str:
    change_id = new_id("chg")
    conn.execute(
        "INSERT INTO changes(id, persona_id, ts, op, state, reason, job_id, payload)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (change_id, persona_id, now_iso(), op, state, reason, job_id, dumps(payload)),
    )
    return change_id


def apply_updates(
    conn: sqlite3.Connection,
    persona_id: str,
    op: str,
    updates: dict[str, dict[str, Any]],
    *,
    plan: dict[str, Any] | None = None,
    reason: str | None = None,
    job_id: str | None = None,
) -> str:
    """Apply column updates to claims and record an ``applied`` change with snapshots."""
    ids = list(updates)
    before = snapshot(conn, ids)
    for claim_id, cols in updates.items():
        if not cols:
            continue
        cols = dict(cols)
        if "text" in cols:
            cols["norm_text"] = normalize_text(cols["text"])
        conn.execute(
            f"UPDATE claims SET {', '.join(f'{k} = ?' for k in cols)}"
            " WHERE persona_id = ? AND id = ?",
            [*cols.values(), persona_id, claim_id],
        )
    payload = {"claims": ids, "before": before, "after": snapshot(conn, ids)}
    if plan is not None:
        payload["plan"] = plan
    return record(conn, persona_id, op, payload, reason=reason, job_id=job_id)


def plan_claims(plan: dict[str, Any]) -> list[str]:
    kind = plan["kind"]
    if kind == "merge":
        return [plan["keep"], plan["drop"]]
    if kind == "supersede":
        return [plan["winner"], plan["loser"]]
    if kind == "archive":
        return [plan["claim"]]
    if kind == "confidence":
        return list(plan["values"])
    raise Invalid(f"unknown plan kind {kind!r}")


def _live(conn: sqlite3.Connection, persona_id: str, claim_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM claims WHERE persona_id = ? AND id = ?", (persona_id, claim_id)
    ).fetchone()
    if row is None or row["status"] == "archived":
        raise Conflict(f"claim {claim_id!r} is gone or archived", id=claim_id)
    return row


def plan_updates(
    conn: sqlite3.Connection, persona_id: str, plan: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    """Column updates for a plan against the claims' current state (Conflict if stale)."""
    from wwyd.store.claims import merge_evidence

    ts = now_iso()
    kind = plan["kind"]

    def archived(reason: str, **extra: Any) -> dict[str, Any]:
        return {
            "status": "archived",
            "archive_reason": reason,
            "archived_at": ts,
            "updated_at": ts,
            **extra,
        }

    if kind == "merge":
        keep, drop = _live(conn, persona_id, plan["keep"]), _live(conn, persona_id, plan["drop"])
        keep_cols: dict[str, Any] = {}
        if keep["status"] != "rejected":
            last = max(
                (x for x in (keep["last_evidence_at"], drop["last_evidence_at"]) if x),
                default=None,
            )
            keep_cols = {
                "evidence": dumps(
                    merge_evidence(json.loads(keep["evidence"]), json.loads(drop["evidence"]))
                ),
                "confidence": absorbed_confidence(keep["confidence"], drop["confidence"]),
                "last_evidence_at": last,
                "updated_at": ts,
            }
        return {keep["id"]: keep_cols, drop["id"]: archived("merged", superseded_by=keep["id"])}
    if kind == "supersede":
        winner = _live(conn, persona_id, plan["winner"])
        loser = _live(conn, persona_id, plan["loser"])
        return {winner["id"]: {}, loser["id"]: archived("superseded", superseded_by=winner["id"])}
    if kind == "archive":
        row = _live(conn, persona_id, plan["claim"])
        extra = {"confidence": plan["confidence"]} if "confidence" in plan else {}
        return {row["id"]: archived(plan["reason"], **extra)}
    if kind == "confidence":
        out = {}
        for claim_id, value in plan["values"].items():
            _live(conn, persona_id, claim_id)
            out[claim_id] = {"confidence": value, "decayed_at": ts}
        return out
    raise Invalid(f"unknown plan kind {kind!r}")


def execute(
    conn: sqlite3.Connection,
    persona_id: str,
    op: str,
    plan: dict[str, Any],
    *,
    state: str = "applied",
    reason: str | None = None,
    job_id: str | None = None,
) -> str:
    """Apply a plan (``applied``) or store it for the owner (``proposed``)."""
    if state == "proposed":
        ids = plan_claims(plan)
        payload = {"claims": ids, "plan": plan, "before": snapshot(conn, ids)}
        return record(conn, persona_id, op, payload, state="proposed", reason=reason, job_id=job_id)
    updates = plan_updates(conn, persona_id, plan)
    return apply_updates(conn, persona_id, op, updates, plan=plan, reason=reason, job_id=job_id)


def has_open_proposal(conn: sqlite3.Connection, persona_id: str, claim_ids: Sequence[str]) -> bool:
    """True if a proposed change already covers any of these claims."""
    for row in conn.execute(
        "SELECT payload FROM changes WHERE persona_id = ? AND state = 'proposed'", (persona_id,)
    ):
        if set(json.loads(row["payload"]).get("claims", ())) & set(claim_ids):
            return True
    return False


def expire_for_claims(conn: sqlite3.Connection, persona_id: str, claim_ids: Iterable[str]) -> None:
    for claim_id in claim_ids:
        conn.execute(
            "UPDATE changes SET state = 'expired' WHERE persona_id = ?"
            " AND state <> 'expired' AND instr(payload, ?) > 0",
            (persona_id, f'"{claim_id}"'),
        )


# --- reading ----------------------------------------------------------------------------


def _row(conn: sqlite3.Connection, persona_id: str, change_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT rowid AS seq, * FROM changes WHERE persona_id = ? AND id = ?",
        (persona_id, change_id),
    ).fetchone()
    if row is None:
        raise NotFound(f"change {change_id!r} not found", id=change_id)
    return row


def _public(row: sqlite3.Row) -> dict[str, Any]:
    payload = json.loads(row["payload"])
    out = {
        "id": row["id"],
        "ts": row["ts"],
        "op": row["op"],
        "state": row["state"],
        "reason": row["reason"],
        "job_id": row["job_id"],
        "claims": payload.get("claims", []),
    }
    for key in ("before", "after", "plan", "into", "renamed", "category", "created"):
        if key in payload:
            out[key] = payload[key]
    return out


def get_change(conn: sqlite3.Connection, persona_id: str, change_id: str) -> dict[str, Any]:
    return _public(_row(conn, persona_id, change_id))


def list_changes(
    conn: sqlite3.Connection,
    persona_id: str,
    *,
    state: str | None = None,
    op: str | None = None,
    limit: int | None = None,
    cursor: str | None = None,
) -> dict[str, Any]:
    """Newest first."""
    limit = check_limit(limit)
    where, args = ["persona_id = ?"], [persona_id]
    if state is not None:
        if state not in STATES:
            raise Invalid(f"state must be one of {', '.join(STATES)}", field="state")
        where.append("state = ?")
        args.append(state)
    if op is not None:
        if op not in OPS:
            raise Invalid(f"op must be one of {', '.join(OPS)}", field="op")
        where.append("op = ?")
        args.append(op)
    before = decode_cursor(cursor)
    if before:
        where.append("rowid < ?")
        args.append(before)
    rows = conn.execute(
        f"SELECT rowid AS seq, * FROM changes WHERE {' AND '.join(where)}"
        " ORDER BY rowid DESC LIMIT ?",
        [*args, limit + 1],
    ).fetchall()
    result: dict[str, Any] = {"changes": [_public(r) for r in rows[:limit]]}
    if len(rows) > limit:
        result["next_cursor"] = encode_cursor(rows[limit - 1]["seq"])
    return result


# --- proposals --------------------------------------------------------------------------


def _bump(conn: sqlite3.Connection, row: sqlite3.Row, state: str, payload: dict[str, Any]) -> None:
    """Rewrite a change so it sorts as the newest (undo ordering follows rowid)."""
    conn.execute("DELETE FROM changes WHERE id = ?", (row["id"],))
    conn.execute(
        "INSERT INTO changes(id, persona_id, ts, op, state, reason, job_id, payload)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            row["id"],
            row["persona_id"],
            now_iso(),
            row["op"],
            state,
            row["reason"],
            row["job_id"],
            dumps(payload),
        ),
    )


def apply_proposal(conn: sqlite3.Connection, persona_id: str, change_id: str) -> bool:
    """Apply a proposed change inside the caller's transaction. False if not proposed.

    Raises Conflict if its claims changed meanwhile (gone or archived)."""
    row = _row(conn, persona_id, change_id)
    if row["state"] != "proposed":
        return False
    plan = json.loads(row["payload"])["plan"]
    updates = plan_updates(conn, persona_id, plan)
    tmp = apply_updates(conn, persona_id, row["op"], updates, plan=plan)
    payload = json.loads(_row(conn, persona_id, tmp)["payload"])
    conn.execute("DELETE FROM changes WHERE id = ?", (tmp,))
    _bump(conn, row, "applied", payload)
    return True


def dismiss_proposal(conn: sqlite3.Connection, persona_id: str, change_id: str) -> bool:
    cur = conn.execute(
        "UPDATE changes SET state = 'dismissed' WHERE persona_id = ? AND id = ?"
        " AND state = 'proposed'",
        (persona_id, change_id),
    )
    return cur.rowcount > 0


# --- undo -------------------------------------------------------------------------------


def undo_change(conn: sqlite3.Connection, persona_id: str, change_id: str) -> dict[str, Any]:
    with transaction(conn):
        row = _row(conn, persona_id, change_id)
        if row["state"] != "applied":
            raise Conflict(
                f"only applied changes can be undone (this one is {row['state']})", id=change_id
            )
        payload = json.loads(row["payload"])
        later = conn.execute(
            "SELECT id, op, payload FROM changes WHERE persona_id = ? AND state = 'applied'"
            " AND rowid > ? ORDER BY rowid",
            (persona_id, row["seq"]),
        ).fetchall()
        if row["op"] == "category_create":
            from wwyd.store import categories as cats

            cats.undo_create(conn, persona_id, payload)
        elif row["op"] in _CATEGORY_OPS:
            blocking = [r["id"] for r in later if r["op"] in _CATEGORY_OPS]
            if blocking:
                raise Conflict(
                    "a later category change must be undone first", id=change_id, later=blocking
                )
            mine = set(payload.get("claims", ()))
            blocking = [
                r["id"] for r in later if mine & set(json.loads(r["payload"]).get("claims", ()))
            ]
            if blocking:
                raise Conflict(
                    "a later change touched the same claims; undo it first",
                    id=change_id,
                    later=blocking,
                )
            _undo_category(conn, persona_id, row["op"], payload)
        else:
            if "before" not in payload or not isinstance(payload["before"], dict):
                raise Conflict("this change has no snapshot to restore", id=change_id)
            mine = set(payload.get("claims", ()))
            blocking = [
                r["id"] for r in later if mine & set(json.loads(r["payload"]).get("claims", ()))
            ]
            if blocking:
                raise Conflict(
                    "a later change touched the same claims; undo it first",
                    id=change_id,
                    later=blocking,
                )
            for claim_id, snap in payload["before"].items():
                _restore(conn, persona_id, claim_id, snap)
        conn.execute("UPDATE changes SET state = 'undone' WHERE id = ?", (change_id,))
        return get_change(conn, persona_id, change_id)


def _undo_category(
    conn: sqlite3.Connection, persona_id: str, op: str, payload: dict[str, Any]
) -> None:
    from wwyd.store import categories as cats

    if op == "category_rename":
        cats.undo_rename(conn, persona_id, payload)
    elif op == "category_merge":
        cats.undo_merge(conn, persona_id, payload)
    elif op == "category_split":
        cats.undo_split(conn, persona_id, payload)
    else:
        raise Conflict(f"{op} cannot be undone")
