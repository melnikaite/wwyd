# SPDX-License-Identifier: AGPL-3.0-or-later
"""Per-source ingest watermarks (SPEC §4.6, §7.1): timestamps and counts, no content."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Any

from wwyd.errors import Invalid
from wwyd.ids import now_iso
from wwyd.store.common import AUDIENCE_NAME

__all__ = ["check_source", "get_last_ts", "list_sources", "record_job", "reset_source"]


def check_source(source: Any) -> str:
    if not isinstance(source, str) or not AUDIENCE_NAME.match(source):
        raise Invalid("source must match [a-z0-9][a-z0-9_-]{0,39}", field="source")
    return source


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


def get_last_ts(conn: sqlite3.Connection, persona_id: str, source: str) -> datetime | None:
    row = conn.execute(
        "SELECT last_ts FROM ingest_sources WHERE persona_id = ? AND source = ?",
        (persona_id, source),
    ).fetchone()
    if row is None or row["last_ts"] is None:
        return None
    return datetime.fromisoformat(row["last_ts"])


def record_job(
    conn: sqlite3.Connection,
    persona_id: str,
    source: str,
    last_ts: datetime | None,
    processed: int,
) -> None:
    """After a job: move the watermark forward (never back) and add the processed count."""
    old = get_last_ts(conn, persona_id, source)
    new = last_ts if old is None else (old if last_ts is None else max(old, last_ts))
    conn.execute(
        "INSERT INTO ingest_sources(persona_id, source, last_ts, last_job_at, records_total)"
        " VALUES (?, ?, ?, ?, ?) ON CONFLICT(persona_id, source) DO UPDATE SET"
        " last_ts = excluded.last_ts, last_job_at = excluded.last_job_at,"
        " records_total = records_total + excluded.records_total",
        (persona_id, source, _iso(new) if new else None, now_iso(), max(0, processed)),
    )


def list_sources(conn: sqlite3.Connection, persona_id: str) -> list[dict[str, Any]]:
    return [
        {
            "source": r["source"],
            "last_ts": r["last_ts"],
            "last_job_at": r["last_job_at"],
            "records_total": r["records_total"],
        }
        for r in conn.execute(
            "SELECT * FROM ingest_sources WHERE persona_id = ? ORDER BY source", (persona_id,)
        )
    ]


def reset_source(conn: sqlite3.Connection, persona_id: str, source: str) -> bool:
    cur = conn.execute(
        "DELETE FROM ingest_sources WHERE persona_id = ? AND source = ?",
        (persona_id, check_source(source)),
    )
    return cur.rowcount > 0
