# SPDX-License-Identifier: AGPL-3.0-or-later
"""In-process nightly scheduler (SPEC §10): decay, budget, purge and the category steps
(§10.6) at 03:30 persona-local."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from wwyd.compact.categories import CATEGORY_STEPS
from wwyd.compact.constants import NIGHTLY_HOUR, NIGHTLY_MINUTE, SCHEDULER_TICK_SECONDS
from wwyd.compact.maintenance import run_steps
from wwyd.compact.run import Run
from wwyd.db import transaction
from wwyd.ids import format_ts, parse_ts

log = logging.getLogger(__name__)

NightlyHook = Callable[[sqlite3.Connection, str, datetime], None]
_hooks: list[NightlyHook] = []


def register_nightly(fn: NightlyHook) -> NightlyHook:
    """Add ``fn(conn, persona_id, now)`` to every persona's nightly run (usable as a decorator)."""
    if fn not in _hooks:
        _hooks.append(fn)
    return fn


def _purge_audit(conn: sqlite3.Connection, persona_id: str, now: datetime) -> None:
    from wwyd.store.audit import purge_old_audit

    purge_old_audit(conn, persona_id=persona_id, now=now)


# Module-level and deduplicated by identity in register_nightly, so repeated imports and
# test runs never register it twice.
register_nightly(_purge_audit)


def last_due(now: datetime, timezone: str) -> datetime:
    """The most recent 03:30 in ``timezone`` at or before ``now`` (as UTC)."""
    try:
        tz = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        tz = ZoneInfo("UTC")
    local = now.astimezone(tz)
    target = local.replace(hour=NIGHTLY_HOUR, minute=NIGHTLY_MINUTE, second=0, microsecond=0)
    if target > local:
        day = (local - timedelta(days=1)).date()
        target = datetime(day.year, day.month, day.day, NIGHTLY_HOUR, NIGHTLY_MINUTE, tzinfo=tz)
    return target.astimezone(UTC)


def _due(conn: sqlite3.Connection, now: datetime) -> list[sqlite3.Row]:
    rows = conn.execute(
        "SELECT p.id, p.timezone, r.last_nightly_at FROM personas p"
        " LEFT JOIN compaction_runs r ON r.persona_id = p.id ORDER BY p.id"
    ).fetchall()
    return [
        r
        for r in rows
        if not (
            r["last_nightly_at"] and parse_ts(r["last_nightly_at"]) >= last_due(now, r["timezone"])
        )
    ]


def _maintain(conn: sqlite3.Connection, persona_id: str, now: datetime) -> None:
    try:
        run_steps(Run(conn, persona_id, apply=True, now=now), ("decay", "budget", "purge"))
        for hook in list(_hooks):
            hook(conn, persona_id, now)
    except Exception as exc:  # noqa: BLE001 - keep the scheduler alive
        log.warning("nightly compaction failed for %s: %s", persona_id, type(exc).__name__)


def _mark(conn: sqlite3.Connection, persona_id: str, now: datetime) -> None:
    with transaction(conn):
        conn.execute(
            "INSERT INTO compaction_runs(persona_id, last_nightly_at) VALUES (?, ?)"
            " ON CONFLICT(persona_id) DO UPDATE SET last_nightly_at = excluded.last_nightly_at",
            (persona_id, format_ts(now)),
        )


def run_nightly(conn: sqlite3.Connection, now: datetime | None = None) -> list[str]:
    """Run nightly maintenance (decay, budget, purge, hooks) for every persona whose local
    03:30 passed since its last run. Returns the persona IDs that ran. One persona's
    failure does not stop the rest. The LLM category steps run in :func:`run_nightly_async`."""
    now = now or datetime.now(UTC)
    ran: list[str] = []
    for row in _due(conn, now):
        _maintain(conn, row["id"], now)
        _mark(conn, row["id"], now)
        ran.append(row["id"])
    return ran


async def run_nightly_async(conn: sqlite3.Connection, now: datetime | None = None) -> list[str]:
    """:func:`run_nightly` plus the category steps (SPEC §10.6), which run before the
    persona's ``last_nightly_at`` moves (it is their "since the previous run" watermark)."""
    from wwyd.compact.run import compact

    now = now or datetime.now(UTC)
    ran: list[str] = []
    for row in _due(conn, now):
        _maintain(conn, row["id"], now)
        try:
            await compact(conn, row["id"], apply=True, steps=CATEGORY_STEPS, now=now)
        except Exception as exc:  # noqa: BLE001
            log.warning("nightly category upkeep failed for %s: %s", row["id"], type(exc).__name__)
        _mark(conn, row["id"], now)
        ran.append(row["id"])
    return ran


async def scheduler_task(conn: sqlite3.Connection) -> None:
    """Long-running coroutine for the HTTP app lifespan; cancel it on shutdown."""
    while True:
        try:
            await run_nightly_async(conn)
        except Exception as exc:  # noqa: BLE001
            log.warning("nightly scheduler tick failed: %s", type(exc).__name__)
        await asyncio.sleep(SCHEDULER_TICK_SECONDS)
