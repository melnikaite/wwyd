# SPDX-License-Identifier: AGPL-3.0-or-later
"""Audit log of ``predict`` calls (SPEC §4.6, P6): who, when, outcome; never any text."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from wwyd.db import transaction
from wwyd.errors import Invalid
from wwyd.ids import format_ts, new_id, now_iso, parse_ts
from wwyd.store.common import check_limit, decode_cursor, encode_cursor

RETENTION_DAYS = 90
OUTCOMES = (
    "answer",
    "needs",
    "escalate:low_confidence",
    "escalate:out_of_persona",
    "escalate:boundary",
    "escalate:rate_limited",
    "escalate:spend_cap",
    "escalate:llm_unavailable",
    "escalate:audience_not_permitted",
)


def record(
    conn: sqlite3.Connection,
    persona_id: str,
    *,
    scope: str,
    outcome: str,
    client_name: str | None = None,
    subject_id: str | None = None,
    audience_id: str | None = None,
    audience_fallback: bool = False,
    confidence: float | None = None,
    llm_tokens: int = 0,
    latency_ms: int = 0,
) -> str:
    if outcome not in OUTCOMES:
        raise ValueError(f"unknown audit outcome {outcome!r}")
    grant_id = subject_id if subject_id and subject_id.startswith("grt_") else None
    api_key_id = subject_id if subject_id and subject_id.startswith("key_") else None
    audit_id = new_id("adt")
    with transaction(conn):
        conn.execute(
            "INSERT INTO audit(id, persona_id, ts, client_name, grant_id, api_key_id, scope,"
            " audience_id, audience_fallback, outcome, confidence, llm_tokens, latency_ms)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                audit_id,
                persona_id,
                now_iso(),
                (client_name or None) and client_name[:200],
                grant_id,
                api_key_id,
                scope,
                audience_id,
                int(audience_fallback),
                outcome,
                None if confidence is None else round(float(confidence), 3),
                max(0, int(llm_tokens)),
                max(0, int(latency_ms)),
            ),
        )
    return audit_id


def list_audit(
    conn: sqlite3.Connection,
    persona_id: str,
    *,
    since: str | None = None,
    audience: str | None = None,
    outcome: str | None = None,
    limit: int | None = None,
    cursor: str | None = None,
) -> dict[str, Any]:
    """Newest first. ``outcome`` is an exact code, or ``escalate`` for every escalation."""
    limit = check_limit(limit)
    where = ["a.persona_id = ?"]
    params: list[Any] = [persona_id]
    after = decode_cursor(cursor)
    if after:
        where.append("a.seq < ?")
        params.append(after)
    if since is not None:
        try:
            since_ts = format_ts(parse_ts(since))
        except (ValueError, TypeError):
            raise Invalid("since must be an ISO 8601 timestamp", field="since") from None
        where.append("a.ts >= ?")
        params.append(since_ts)
    if audience is not None:
        where.append("au.name = ?")
        params.append(audience)
    if outcome is not None:
        if outcome == "escalate":
            where.append("a.outcome LIKE 'escalate:%'")
        elif outcome in OUTCOMES:
            where.append("a.outcome = ?")
            params.append(outcome)
        else:
            raise Invalid("unknown outcome", field="outcome")
    rows = conn.execute(
        "SELECT a.*, au.name AS audience FROM audit a"
        " LEFT JOIN audiences au ON au.id = a.audience_id"
        f" WHERE {' AND '.join(where)} ORDER BY a.seq DESC LIMIT ?",
        (*params, limit + 1),
    ).fetchall()
    more = len(rows) > limit
    rows = rows[:limit]
    return {
        "entries": [
            {
                "id": r["id"],
                "ts": r["ts"],
                "client_name": r["client_name"],
                "grant_id": r["grant_id"],
                "api_key_id": r["api_key_id"],
                "scope": r["scope"],
                "audience": r["audience"],
                "audience_fallback": bool(r["audience_fallback"]),
                "outcome": r["outcome"],
                "confidence": r["confidence"],
                "llm_tokens": r["llm_tokens"],
                "latency_ms": r["latency_ms"],
            }
            for r in rows
        ],
        "next_cursor": encode_cursor(rows[-1]["seq"]) if more else None,
    }


def purge_old_audit(
    conn: sqlite3.Connection,
    days: int = RETENTION_DAYS,
    *,
    persona_id: str | None = None,
    now: datetime | None = None,
) -> int:
    """Delete audit rows older than ``days`` (nightly), for one persona or all; returns how
    many were removed."""
    cutoff = format_ts((now or datetime.now(UTC)) - timedelta(days=days))
    sql, args = "DELETE FROM audit WHERE ts < ?", [cutoff]
    if persona_id is not None:
        sql += " AND persona_id = ?"
        args.append(persona_id)
    with transaction(conn):
        return conn.execute(sql, args).rowcount
