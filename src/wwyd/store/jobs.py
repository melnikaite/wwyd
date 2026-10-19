# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ``jobs`` table (SPEC §4.6): status, progress and counters of ingest jobs, no content."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from wwyd.errors import NotFound
from wwyd.ids import new_id, now_iso
from wwyd.store.common import dumps

STATUSES = ("queued", "running", "done", "failed", "interrupted")


def create_job(conn: sqlite3.Connection, persona_id: str, counts: dict[str, Any]) -> str:
    job_id = new_id("job")
    conn.execute(
        "INSERT INTO jobs(id, persona_id, status, counts, created_at) VALUES (?, ?, 'queued', ?, ?)",
        (job_id, persona_id, dumps(counts), now_iso()),
    )
    return job_id


def update_job(
    conn: sqlite3.Connection,
    job_id: str,
    *,
    status: str | None = None,
    progress: float | None = None,
    counts: dict[str, Any] | None = None,
    error_code: str | None = None,
) -> None:
    sets: list[str] = []
    args: list[Any] = []
    if status is not None:
        sets.append("status = ?")
        args.append(status)
        if status == "running":
            sets.append("started_at = ?")
            args.append(now_iso())
        elif status in ("done", "failed", "interrupted"):
            sets.append("finished_at = ?")
            args.append(now_iso())
    if progress is not None:
        sets.append("progress = ?")
        args.append(round(min(1.0, max(0.0, progress)), 4))
    if counts is not None:
        sets.append("counts = ?")
        args.append(dumps(counts))
    if error_code is not None:
        sets.append("error_code = ?")
        args.append(error_code)
    if sets:
        conn.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE id = ?", (*args, job_id))


def get_job(conn: sqlite3.Connection, persona_id: str, job_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM jobs WHERE id = ? AND persona_id = ?", (job_id, persona_id)
    ).fetchone()
    if row is None:
        raise NotFound("job not found")
    out: dict[str, Any] = {
        "job_id": row["id"],
        "status": row["status"],
        "progress": row["progress"],
        "counts": json.loads(row["counts"]),
        "created_at": row["created_at"],
        "finished_at": row["finished_at"],
    }
    if row["error_code"]:
        out["error_code"] = row["error_code"]
    return out


def mark_interrupted(conn: sqlite3.Connection) -> int:
    """At startup: jobs left queued/running by a previous process become ``interrupted``.

    Nothing can be resumed because the records were never saved (P1); the harness resends.
    """
    cur = conn.execute(
        "UPDATE jobs SET status = 'interrupted', finished_at = ?"
        " WHERE status IN ('queued', 'running')",
        (now_iso(),),
    )
    return cur.rowcount
